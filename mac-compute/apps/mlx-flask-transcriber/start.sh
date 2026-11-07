#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
    echo "Qwen3-ASR MLX 版需要 Apple Silicon Mac，请使用原生 arm64 Python。"
    exit 1
fi

if ! command -v ffmpeg >/dev/null || \
   ! command -v ffprobe >/dev/null; then
    echo "请先安装 FFmpeg：brew install ffmpeg"
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    if command -v python3.12 >/dev/null; then
        python3.12 -m venv .venv
    else
        python3 -m venv .venv
    fi
fi

.venv/bin/python -m pip install -r requirements.txt

exec .venv/bin/python app.py
