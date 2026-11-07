"""Layout for Qwen3-ASR text. Keep every non-whitespace character unchanged."""

import math
import os
import re

PARAGRAPH_CHARS = max(0, int(os.getenv("PARAGRAPH_CHARS", "220")))
_SENTENCE_END = re.compile(r"[。！？]+[”’\"'）)】》」』]*|[.!?]+[”’\"'）)】》」』]*(?=\s|$)")


def content_chars(text):
    return len(re.sub(r"\s+", "", text or ""))


def lexical_identity(text):
    return re.sub(r"\s+", "", text or "")


def srt_clock(seconds):
    ms = max(0, round(float(seconds) * 1000))
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def label_clock(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}" if hours else f"{minutes:02}:{secs:02}"


def make_cues(segments):
    """Each cue covers its actual input block; never invent sentence timings."""
    cues = []
    for segment in segments:
        text = segment["text"].strip()
        if not text:
            continue
        start, end = float(segment["start"]), float(segment["end"])
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
            raise ValueError("音频块时间无效")
        cues.append({"start": start, "end": end, "text": text, "timing": "audio_block"})
    return cues


def _reading_block(text, limit):
    # The model supplies punctuation. Only whitespace is added for layout.
    breaks = {m.end() for m in _SENTENCE_END.finditer(text)}
    positions = sorted({0, len(text), *breaks, *(m.start() for m in re.finditer(r"\s+(?=\S)", text))})
    output, paragraph_size = [], 0
    for start, end in zip(positions, positions[1:]):
        part = text[start:end]
        size = content_chars(part)
        if output and limit and paragraph_size + size > limit and paragraph_size:
            output.append("\n\n")
            paragraph_size = 0
            part = part.lstrip()
        elif output and start in breaks:
            output.append("\n")
            part = part.lstrip()
        output.append(part)
        paragraph_size += size
    return "".join(output).strip()


def cues_to_readable(cues, paragraph_chars=None):
    limit = PARAGRAPH_CHARS if paragraph_chars is None else max(0, int(paragraph_chars))
    return "\n\n".join(_reading_block(cue["text"], limit) for cue in cues if cue["text"].strip())


def cues_to_srt(cues):
    return "\n".join(
        f"{index}\n{srt_clock(cue['start'])} --> {srt_clock(cue['end'])}\n{cue['text']}\n"
        for index, cue in enumerate(cues, 1)
    )


def cues_to_timeline(cues):
    return "\n".join(f"[{label_clock(cue['start'])}] {cue['text']}" for cue in cues)
