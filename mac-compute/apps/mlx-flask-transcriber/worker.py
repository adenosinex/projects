"""Qwen3-ASR worker: one inference per audio block, complete text retained."""

import json
import math
import os
import re
import subprocess
import time
import wave
from pathlib import Path

from asr import BACKEND, profile_settings
from formatter import content_chars, cues_to_readable, cues_to_srt, cues_to_timeline, lexical_identity, make_cues
from jobs import JobCancelled

# Quality-first chunking (general, not tuned to one content type):
# target 45s gives a full sentence / clause more context than 30s, while
# generation stays far below MAX_TOKENS (avg ~125 tokens per 30s block).
# A block may extend to 60s to reach a pause, never beyond it in normal
# cutting; a merged short final tail may reach 70s (see choose_boundaries).
CHUNK_SECONDS = max(10, min(120, float(os.getenv("ASR_CHUNK_SECONDS", "45"))))
CHUNK_MAX_FACTOR = 1.33  # hard cap: target * 1.33 (45s -> 60s)
CHUNK_MIN_FACTOR = 0.6  # earliest cut: target * 0.6 (45s -> 27s, never too short)
SHORT_TAIL_SECONDS = 10.0  # a final tail shorter than this is merged, not a lone chunk
# Two silence tiers. Strict (-32dB) is best when audio is quiet; loose (-20dB,
# min 0.30s) finds pauses in noisy / music-backed audio where the level never
# drops below -32dB. Loose is only a fallback when no strict pause is in range.
SILENCE_STRICT = ("-32dB", 0.25)
SILENCE_LOOSE = ("-20dB", 0.30)
MAX_TOKENS = max(256, min(8192, int(os.getenv("ASR_MAX_TOKENS", "2048"))))
_LANGUAGE = os.getenv("ASR_LANGUAGE", "auto").strip()
LANGUAGE = None if _LANGUAGE.lower() in ("", "auto", "none") else _LANGUAGE


def write_text(folder, filename, text):
    temporary = folder / (filename + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(folder / filename)


def run_checked(command, job):
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            job.check_cancel()
            try:
                stdout, stderr = proc.communicate(timeout=0.3)
                return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
        raise


def decode_wav(source, wav_path, job):
    result = run_checked([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-map", "0:a:0", "-vn", "-ar", "16000",
        "-ac", "1", "-c:a", "pcm_s16le", str(wav_path),
    ], job)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-700:] or "FFmpeg 解码失败")
    with wave.open(str(wav_path), "rb") as wav:
        duration = wav.getnframes() / wav.getframerate()
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("音频时长无效")
    return duration


def _silence_ranges(result):
    ranges, start = [], None
    for line in result.stderr.splitlines():
        a = re.search(r"silence_start:\s*([\d.]+)", line)
        b = re.search(r"silence_end:\s*([\d.]+)", line)
        if a:
            start = float(a.group(1))
        if b and start is not None:
            end = float(b.group(1))
            if end > start:
                ranges.append((start, end))
            start = None
    return ranges


def detect_silences(wav_path, job):
    """Return (strict, loose) silence ranges.

    Strict first: a pause below -32dB. Loose second: a pause below -20dB,
    which in noisy / music-backed audio is still a real pause (breath,
    speaker change, music dip) even though it never reaches -32dB.
    Either list may be empty; a failed ffmpeg run yields ([], []).
    """
    out = []
    for noise, min_dur in (SILENCE_STRICT, SILENCE_LOOSE):
        result = run_checked([
            "ffmpeg", "-nostdin", "-hide_banner", "-i", str(wav_path),
            "-af", f"silencedetect=noise={noise}:d={min_dur}", "-f", "null", "-",
        ], job)
        out.append([] if result.returncode else _silence_ranges(result))
    return tuple(out)


def choose_boundaries(duration, silences, chunk_seconds=CHUNK_SECONDS):
    """Bounded contiguous blocks; no overlap or text deduplication.

    `silences` is (strict, loose) as returned by detect_silences(), or a
    plain list of (start, end) ranges, treated as strict for compatibility.
    Search order in each window: strict pause, then loose pause, then a
    hard cut at the target. A block may extend past the target up to the
    hard cap to reach a pause, but is never cut shorter than 0.6x target.
    A final tail under SHORT_TAIL_SECONDS is merged into the previous
    block instead of becoming a context-starved lone chunk.
    Returns (marks, quiet_count, loose_count); hard cuts are the rest.
    """
    if not math.isfinite(duration) or duration <= 0 or chunk_seconds <= 0:
        raise ValueError("音频时长和切片长度必须大于零")
    if isinstance(silences, tuple) and len(silences) == 2:
        strict, loose = silences
    else:
        strict, loose = silences, []
    marks, kinds = [0.0], []  # kinds[i] is the tier of boundary marks[i+1]
    maximum = chunk_seconds * CHUNK_MAX_FACTOR
    minimum = chunk_seconds * CHUNK_MIN_FACTOR
    while duration - marks[-1] > maximum:
        current = marks[-1]
        target = current + chunk_seconds
        low = current + minimum
        high = min(current + maximum, duration - 1)
        cut, kind = target, "hard"
        for tier, name in ((strict, "quiet"), (loose, "loose")):
            choices = [
                (abs((a + b) / 2 - target), (a + b) / 2)
                for a, b in tier if low <= (a + b) / 2 <= high
            ]
            if choices:
                cut, kind = min(choices)[1], name
                break
        marks.append(cut)
        kinds.append(kind)
    marks.append(duration)
    # Merge a short final tail: a <10s last block has too little context
    # and its own language / boundary errors; folding it into the previous
    # block stays under maximum + SHORT_TAIL_SECONDS.
    if len(marks) > 2 and duration - marks[-2] < SHORT_TAIL_SECONDS:
        kinds.pop()
        marks.pop(-2)
    quiet = kinds.count("quiet")
    loose_count = kinds.count("loose")
    return marks, quiet, loose_count


def audio_blocks(wav_path, folder, boundaries, job):
    """Write one block at a time, keeping every source sample exactly once."""
    path = folder / "asr_block.wav"
    try:
        with wave.open(str(wav_path), "rb") as wav:
            rate, count = wav.getframerate(), wav.getnframes()
            for start, end in zip(boundaries, boundaries[1:]):
                job.check_cancel()
                a = max(0, min(count, round(start * rate)))
                b = max(a, min(count, round(end * rate)))
                if b <= a:
                    continue
                wav.setpos(a)
                with wave.open(str(path), "wb") as out:
                    out.setparams(wav.getparams())
                    out.writeframes(wav.readframes(b - a))
                yield path, a / rate, b / rate
    finally:
        path.unlink(missing_ok=True)


def persist_outputs(folder, segments):
    cues = make_cues(segments)
    raw = "\n".join(s["text"] for s in segments if s["text"].strip()).strip()
    reading = cues_to_readable(cues)
    fallback = lexical_identity(reading) != lexical_identity(raw)
    if fallback:
        reading = raw
    for filename, text in (
        ("raw.txt", raw), ("result.txt", reading),
        ("captions.txt", cues_to_timeline(cues)), ("result.srt", cues_to_srt(cues)),
    ):
        write_text(folder, filename, text)
    return cues, reading, raw, fallback


def run_job(job, source):
    folder = Path(source).parent
    wav_path = folder / "audio_16k.wav"
    started = time.monotonic()
    completed, blocks = False, None
    try:
        job.check_cancel()
        saved = job.snapshot()
        settings = profile_settings(saved.get("model_profile"))
        if saved.get("engine") == "qwen3-asr" and saved.get("model"):
            settings["model"] = saved["model"]
        for name in ("result.txt", "raw.txt", "result.srt", "captions.txt", "stats.json", "asr_chunks.json"):
            (folder / name).unlink(missing_ok=True)
        job.update(status="preparing", percent=1, **settings, message="正在提取音频…")
        prep_started = time.monotonic()
        duration = decode_wav(source, wav_path, job)
        silences = detect_silences(wav_path, job)
        boundaries, quiet_count, loose_count = choose_boundaries(duration, silences)
        prep_elapsed = time.monotonic() - prep_started
        job.update(
            duration=round(duration, 2), chunk_count=len(boundaries) - 1,
            quiet_split_count=quiet_count, loose_split_count=loose_count,
            hard_split_count=len(boundaries) - 2 - quiet_count - loose_count,
            preprocess_elapsed=round(prep_elapsed, 2),
            message=f"正在加载 {settings['model_label']}，首次使用会下载模型…",
        )
        job.check_cancel()
        load_started = time.monotonic()
        BACKEND.load(settings["model"])
        load_elapsed = time.monotonic() - load_started
        job.check_cancel()
        job.update(status="running", percent=3, model_load_elapsed=round(load_elapsed, 2))
        segments, audit, notes = [], [], []
        inference_elapsed = format_elapsed = 0.0
        generation_tokens = prompt_tokens = 0
        layout_fallback, detected_languages = False, {}
        blocks = audio_blocks(wav_path, folder, boundaries, job)
        for index, (path, start, end) in enumerate(blocks, 1):
            job.update(message=f"{settings['model_label']}：正在识别第 {index}/{len(boundaries) - 1} 段…")
            call_started = time.monotonic()
            transcript = BACKEND.transcribe(path, language=LANGUAGE, max_tokens=MAX_TOKENS)
            inference_elapsed += time.monotonic() - call_started
            # Persist all text before cancellation or any reading-layout changes.
            chunk = {
                "index": index, "start": start, "end": end,
                "text": transcript.text, "language": transcript.language,
                "generation_tokens": transcript.generation_tokens,
                "prompt_tokens": transcript.prompt_tokens, "max_tokens": MAX_TOKENS,
                "token_limit_reached": transcript.generation_tokens >= MAX_TOKENS,
                "timing": "audio_block",
            }
            audit.append(chunk)
            write_text(folder, "asr_chunks.json", json.dumps(audit, ensure_ascii=False, indent=2))
            segments.append(chunk)
            if chunk["token_limit_reached"]:
                notes.append({"start": start, "end": end, "issues": ["达到生成长度上限，请核对是否缺字"], "preview": transcript.text[:90]})
            elif not transcript.text.strip():
                notes.append({"start": start, "end": end, "issues": ["该音频块未输出文字，可能是静音、音乐或漏识别"], "preview": ""})
            generation_tokens += transcript.generation_tokens
            prompt_tokens += transcript.prompt_tokens
            if transcript.language:
                detected_languages[transcript.language] = detected_languages.get(transcript.language, 0) + 1
            format_started = time.monotonic()
            cues, reading, raw, fallback = persist_outputs(folder, segments)
            format_elapsed += time.monotonic() - format_started
            layout_fallback |= fallback
            job.update(
                processed=round(end, 2), percent=min(97, 3 + round(end / duration * 94)),
                transcribe_elapsed=round(inference_elapsed, 2), format_elapsed=round(format_elapsed, 2),
                char_count=content_chars(raw), cue_count=len(cues),
                paragraph_count=reading.count("\n\n") + bool(reading), suspect_count=len(notes),
                token_limit_chunk_count=sum(c["token_limit_reached"] for c in audit),
                speed_x=round(end / inference_elapsed, 2) if inference_elapsed else 0,
                message=f"第 {index}/{len(boundaries) - 1} 段完成，文字已保存",
            )
            job.check_cancel()
        job.update(status="formatting", percent=98, message="正在保存文本与统计…")
        total_elapsed = time.monotonic() - started
        stats = {
            **settings, "duration": round(duration, 2),
            "preprocess_elapsed": round(prep_elapsed, 2), "model_load_elapsed": round(load_elapsed, 2),
            "transcribe_elapsed": round(inference_elapsed, 2), "format_elapsed": round(format_elapsed, 2),
            "total_elapsed": round(total_elapsed, 2),
            "speed_x": round(duration / inference_elapsed, 2) if inference_elapsed else 0,
            "char_count": content_chars(raw), "cue_count": len(cues),
            "paragraph_count": reading.count("\n\n") + bool(reading),
            "chunk_count": len(audit), "quiet_split_count": quiet_count,
            "loose_split_count": loose_count,
            "hard_split_count": len(boundaries) - 2 - quiet_count - loose_count,
            "suspect_count": len(notes), "suspect_segments": notes,
            "generation_tokens": generation_tokens, "prompt_tokens": prompt_tokens,
            "token_limit_chunk_count": sum(c["token_limit_reached"] for c in audit),
            "language_setting": LANGUAGE or "auto", "detected_languages": detected_languages,
            "timestamp_mode": "audio_block", "chunk_seconds": CHUNK_SECONDS,
            "max_tokens_per_chunk": MAX_TOKENS, "layout_fallback": layout_fallback,
            "text_mismatch": lexical_identity(reading) != lexical_identity(raw),
            "format_mode": "Qwen3-ASR 原文 · 句段排版 · 音频块时间轴",
            "file_bytes": saved.get("file_bytes", 0),
        }
        write_text(folder, "stats.json", json.dumps(stats, ensure_ascii=False, indent=2))
        job.check_cancel()
        job.update(
            status="done", percent=100, processed=round(duration, 2),
            elapsed=round(time.time() - saved["created_at"], 2) if saved.get("created_at") else stats["total_elapsed"],
            **stats, message="识别完成，文本已保存" + ("；部分音频块需要核对" if notes else ""),
        )
        completed = True
    except JobCancelled:
        job.update(status="cancelled", message="任务已取消；已识别的文字和记录已保留")
    except Exception as exc:
        job.update(status="error", message=f"处理失败：{exc}")
    finally:
        if blocks is not None:
            blocks.close()
        (folder / "asr_block.wav").unlink(missing_ok=True)
        wav_path.unlink(missing_ok=True)
        if completed:
            Path(source).unlink(missing_ok=True)
