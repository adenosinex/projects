#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  /opt/homebrew/bin/python3.12 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
exec .venv/bin/python app.py
