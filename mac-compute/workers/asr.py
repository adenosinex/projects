"""ASR worker: Qwen3-ASR via mlx-audio.

Chunking (quality-first, general): 45s target, 60s hard cap,
27s minimum, two-tier silence (-32dB then -20dB), short tail
<10s merged into the previous block. No overlap, no dedup.
"""
import json
import math
import os
import re
import subprocess
import threading
import time
import wave
from pathlib import Path

# Model load must never touch the network: the weights are already in
# ~/.cache/huggingface. A network stall here once froze a job in
# "loading" for 17 min with an idle CPU/GPU (2026-11-07).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from config import ASR_DEFAULTS, ASR_MODELS

CHUNK_SECONDS = ASR_DEFAULTS["chunk_seconds"]
MAX_TOKENS = ASR_DEFAULTS["max_tokens"]

# Resident model pool: up to _POOL_MAX instances (one per concurrent ASR
# job). Two threads must never share one mlx model object, so each job
# checks out its own instance and returns it afterwards. Before the
# cache, every job paid a fresh load; the load itself is ~1s once
# offline (HF_HUB_OFFLINE=1) — the old 151s floor was the load stalling
# on Hugging Face network checks on every job.
_POOL_MAX = 2
_pool = []            # free [(model_name, obj)]
_pool_total = [0]     # instances alive (free + in use)
_pool_lock = threading.Lock()
_pool_cond = threading.Condition(_pool_lock)


def _acquire_model(model):
    """Check out a model instance. Returns (obj, was_cached)."""
    with _pool_cond:
        while True:
            for i, (m, obj) in enumerate(_pool):
                if m == model:
                    _pool.pop(i)
                    return obj, True
            if _pool_total[0] < _POOL_MAX:
                _pool_total[0] += 1
                break
            if _pool:  # pool full of another profile: evict one to make room
                _pool.pop(0)
                _pool_total[0] -= 1
                continue
            _pool_cond.wait()
    from mlx_audio.stt import load as stt_load
    try:
        obj = stt_load(model)
    except Exception:
        with _pool_cond:
            _pool_total[0] -= 1
            _pool_cond.notify_all()
        raise
    return obj, False


def _release_model(model, obj):
    with _pool_cond:
        _pool.append((model, obj))
        while len(_pool) > _POOL_MAX:
            _pool.pop(0)
            _pool_total[0] -= 1
        _pool_cond.notify_all()


def _silence_ranges(stderr_text):
    ranges, start = [], None
    for line in stderr_text.splitlines():
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


def detect_silences(wav_path):
    out = []
    for noise, min_dur in (("-32dB", 0.25), ("-20dB", 0.30)):
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(wav_path),
             "-af", f"silencedetect=noise={noise}:d={min_dur}", "-f", "null", "-"],
            capture_output=True, text=True, timeout=600,
        )
        out.append([] if r.returncode else _silence_ranges(r.stderr))
    return tuple(out)


def choose_boundaries(duration, silences, chunk_seconds=CHUNK_SECONDS):
    if isinstance(silences, tuple) and len(silences) == 2:
        strict, loose = silences
    else:
        strict, loose = silences, []
    marks, kinds = [0.0], []
    maximum = chunk_seconds * ASR_DEFAULTS["chunk_max_factor"]
    minimum = chunk_seconds * ASR_DEFAULTS["chunk_min_factor"]
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
    if len(marks) > 2 and duration - marks[-2] < ASR_DEFAULTS["short_tail_seconds"]:
        kinds.pop()
        marks.pop(-2)
    return marks, kinds.count("quiet"), kinds.count("loose")


def _resolve_params(params):
    model_key = params.get("model", ASR_DEFAULTS["model"])
    model = ASR_MODELS.get(model_key, ASR_MODELS["daily"])
    language = params.get("language", ASR_DEFAULTS["language"])
    language = None if str(language).lower() in ("", "auto", "none") else language
    chunk_seconds = float(params.get("chunk_seconds", CHUNK_SECONDS))
    max_tokens = int(params.get("max_tokens", MAX_TOKENS))
    return model, language, chunk_seconds, max_tokens


def _transcribe_with_model(model_obj, model_cached, load_s, src, out_dir,
                           model, language, chunk_seconds, max_tokens,
                           progress_cb):
    """Transcribe one file with an already-checked-out model instance.
    Shared by run_asr (single) and run_asr_batch (one instance per batch)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / "audio_16k.wav"
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src), "-map", "0:a:0", "-vn", "-ar", "16000",
         "-ac", "1", "-c:a", "pcm_s16le", str(wav_path)],
        capture_output=True, text=True, timeout=1200,
    )
    if r.returncode:
        raise RuntimeError((r.stderr or "ffmpeg decode failed")[-500:])
    with wave.open(str(wav_path), "rb") as wav:
        duration = wav.getnframes() / wav.getframerate()
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("invalid audio duration")

    progress_cb(0.05, "detecting silences")
    silences = detect_silences(wav_path)
    boundaries, quiet, loose = choose_boundaries(duration, silences, chunk_seconds)
    n = len(boundaries) - 1

    progress_cb(0.1, f"model ready ({'cached' if model_cached else f'loaded in {load_s}s'})")
    segments = []
    gen_tokens = 0
    block_path = out_dir / "asr_block.wav"
    with wave.open(str(wav_path), "rb") as wav:
        rate, count = wav.getframerate(), wav.getnframes()
        for i, (start, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
            a = max(0, min(count, round(start * rate)))
            b = max(a, min(count, round(end * rate)))
            if b <= a:
                continue
            wav.setpos(a)
            with wave.open(str(block_path), "wb") as out:
                out.setparams(wav.getparams())
                out.writeframes(wav.readframes(b - a))
            result = model_obj.generate(
                str(block_path), language=language, max_tokens=max_tokens,
                temperature=0.0, verbose=False, chunk_duration=1200.0,
            )
            text = getattr(result, "text", "") or ""
            gen_tokens += int(getattr(result, "generation_tokens", 0) or 0)
            segments.append({"index": i, "start": round(start, 2),
                             "end": round(end, 2), "text": text})
            progress_cb(0.1 + 0.85 * (i / n), f"transcribing chunk {i}/{n}")
    block_path.unlink(missing_ok=True)

    raw = "\n".join(s["text"] for s in segments if s["text"].strip()).strip()
    (out_dir / "result.txt").write_text(raw, encoding="utf-8")
    (out_dir / "asr_chunks.json").write_text(
        json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
    # Simple SRT: one cue per audio block.
    srt_lines = []
    for s in segments:
        def ts(t):
            h = int(t // 3600); m = int((t % 3600) // 60); sec = t % 60
            return f"{h:02d}:{m:02d}:{sec:06.3f}".replace(".", ",")
        srt_lines.append(f"{s['index']}\n{ts(s['start'])} --> {ts(s['end'])}\n{s['text']}\n")
    (out_dir / "result.srt").write_text("\n".join(srt_lines), encoding="utf-8")

    return {
        "duration": round(duration, 2),
        "chunk_count": n,
        "quiet_split_count": quiet,
        "loose_split_count": loose,
        "hard_split_count": n - 1 - quiet - loose,
        "chunk_seconds": chunk_seconds,
        "model": model,
        "model_cached": model_cached,
        "model_load_s": load_s,
        "generation_tokens": gen_tokens,
        "char_count": len(raw),
        "files": ["result.txt", "result.srt", "asr_chunks.json"],
    }


def run_asr(src, out_dir, params, progress_cb):
    """Transcribe src audio/video to out_dir. Returns stats dict."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    model, language, chunk_seconds, max_tokens = _resolve_params(params)

    # Model is resident after the first load; later jobs skip the load.
    # Each concurrent job checks out its OWN instance from the pool.
    progress_cb(0.1, f"loading {model}")
    t_load = time.time()
    model_obj, model_cached = _acquire_model(model)
    load_s = round(time.time() - t_load, 1)
    try:
        return _transcribe_with_model(
            model_obj, model_cached, load_s, src, out_dir,
            model, language, chunk_seconds, max_tokens, progress_cb)
    finally:
        _release_model(model, model_obj)


def run_asr_batch(files, out_dir, params, progress_cb):
    """Transcribe MANY files in one job, serially, with ONE model
    instance checked out for the whole batch (no per-file load cost).

    One bad file never kills the batch: it is recorded as failed in
    INDEX.csv and the rest continue. The job itself only fails if
    every file fails (or the model cannot load at all).
    Output: one subdir per file (result.txt / result.srt /
    asr_chunks.json) + INDEX.csv + batch.txt (all texts concatenated)."""
    import csv
    import io

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    model, language, chunk_seconds, max_tokens = _resolve_params(params)
    files = [Path(f) for f in files]
    n = len(files)
    if n == 0:
        raise RuntimeError("no files in batch")

    progress_cb(0.02, f"batch of {n} files, loading {model}")
    t_load = time.time()
    model_obj, model_cached = _acquire_model(model)
    load_s = round(time.time() - t_load, 1)
    rows, texts = [], []
    done = failed = 0
    total_chars, total_dur = 0, 0.0
    try:
        for idx, src in enumerate(files):
            disp = re.sub(r"^[^_]+_\d+_", "", src.name, count=1)
            sub = out_dir / Path(disp).stem
            sub.mkdir(parents=True, exist_ok=True)

            def fcb(p, m, idx=idx, disp=disp):
                progress_cb((idx + p) / n, f"[{idx + 1}/{n}] {disp}: {m}")

            try:
                st = _transcribe_with_model(
                    model_obj, model_cached, load_s, src, sub,
                    model, language, chunk_seconds, max_tokens, fcb)
                rows.append({"file": disp, "status": "done",
                             "duration": st["duration"],
                             "chunks": st["chunk_count"],
                             "chars": st["char_count"], "error": ""})
                texts.append(f"=== {disp} ===\n" +
                             (sub / "result.txt").read_text(encoding="utf-8"))
                done += 1
                total_chars += st["char_count"]
                total_dur += st["duration"]
            except Exception as e:
                rows.append({"file": disp, "status": "failed",
                             "duration": 0, "chunks": 0, "chars": 0,
                             "error": str(e)[:200]})
                failed += 1
            progress_cb((idx + 1) / n,
                        f"[{idx + 1}/{n}] {disp} {rows[-1]['status']}")
    finally:
        _release_model(model, model_obj)

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["file", "status", "duration",
                                        "chunks", "chars", "error"])
    w.writeheader()
    w.writerows(rows)
    (out_dir / "INDEX.csv").write_text(buf.getvalue(), encoding="utf-8")
    (out_dir / "batch.txt").write_text("\n\n".join(texts), encoding="utf-8")
    if done == 0:
        raise RuntimeError(f"all {n} files failed: "
                           + (rows[0]["error"] if rows else ""))
    return {
        "file_count": n, "done": done, "failed": failed,
        "total_duration": round(total_dur, 2),
        "total_chars": total_chars,
        "model": model, "model_cached": model_cached,
        "model_load_s": load_s,
        "files": (["INDEX.csv", "batch.txt"] +
                  [f"{Path(r['file']).stem}/result.txt"
                   for r in rows if r["status"] == "done"]),
    }
