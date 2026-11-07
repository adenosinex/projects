"""Qwen3-ASR model profiles and a single resident MLX model.

MLX is imported only when the GPU worker needs it, so queue/API and text tests
can run independently of Apple Silicon. No other recognition backend is used.
"""

import gc
import os
from dataclasses import dataclass


PROFILES = {
    "daily": {
        "label": "日常 · Qwen3-ASR 0.6B · 8-bit",
        "model": os.getenv("QWEN_DAILY_MODEL", "mlx-community/Qwen3-ASR-0.6B-8bit"),
    },
    "accurate": {
        "label": "高准确率 · Qwen3-ASR 1.7B · 8-bit",
        "model": os.getenv("QWEN_ACCURATE_MODEL", "mlx-community/Qwen3-ASR-1.7B-8bit"),
    },
}
DEFAULT_PROFILE = os.getenv("ASR_PROFILE", "daily").strip().lower()
if DEFAULT_PROFILE not in PROFILES:
    raise ValueError("ASR_PROFILE 必须是 daily 或 accurate")


def profile_settings(profile=None):
    profile = DEFAULT_PROFILE if profile is None else profile
    if profile not in PROFILES:
        raise ValueError("识别模式必须是 daily 或 accurate")
    return {
        "model_profile": profile,
        "model": PROFILES[profile]["model"],
        "model_label": PROFILES[profile]["label"],
        "engine": "qwen3-asr",
    }


@dataclass
class Transcript:
    text: str
    language: str | None
    generation_tokens: int
    prompt_tokens: int


def _count(value):
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


def normalize_output(output):
    """Use the complete model text, never reconstruct it from partial timings."""
    def field(name, default=None):
        return output.get(name, default) if isinstance(output, dict) else getattr(output, name, default)

    text = field("text")
    if not isinstance(text, str):
        raise RuntimeError("Qwen3-ASR 未返回有效的 text 字段")
    language = field("language")
    if isinstance(language, (list, tuple)):
        language = ", ".join(dict.fromkeys(str(item) for item in language if item)) or None
    elif language is not None:
        language = str(language)

    # The pinned runtime parses its own language protocol. Do not strip tags or
    # header-like phrases here: they can be literal words spoken in the audio.
    return Transcript(
        text=text, language=language,
        generation_tokens=_count(field("generation_tokens", 0)),
        prompt_tokens=_count(field("prompt_tokens", 0)),
    )


class QwenASR:
    """One resident model, reused for jobs; release it before changing profile."""

    def __init__(self, loader=None, clear_cache=None):
        self._loader = loader
        self._clear_cache = clear_cache
        self._model = None
        self.model_id = None

    def _runtime(self):
        if self._loader is None:
            import mlx.core as mx
            from mlx_audio.stt import load
            self._loader = load
            self._clear_cache = mx.clear_cache

    def release(self):
        self._model = None
        self.model_id = None
        gc.collect()
        if self._clear_cache is not None:
            self._clear_cache()

    def load(self, model_id):
        self._runtime()
        if self.model_id == model_id and self._model is not None:
            return False
        self.release()
        model = self._loader(model_id)
        if not callable(getattr(model, "generate", None)):
            raise RuntimeError("所选模型没有 Qwen3-ASR generate 接口")
        self._model = model
        self.model_id = model_id
        return True

    def transcribe(self, path, *, language=None, max_tokens=2048):
        if self._model is None:
            raise RuntimeError("请先加载 Qwen3-ASR 模型")
        try:
            output = self._model.generate(
                str(path), language=language, max_tokens=max_tokens,
                temperature=0.0, verbose=False,
                # The worker provides one bounded, contiguous audio block.
                chunk_duration=1200.0,
            )
            return normalize_output(output)
        finally:
            if self._clear_cache is not None:
                self._clear_cache()


BACKEND = QwenASR()
