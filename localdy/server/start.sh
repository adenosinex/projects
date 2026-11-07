#!/bin/sh
# Start the Localdy split server from this directory.
#   ./start.sh                  # default port 18930
#   PORT=19000 ./start.sh       # or override the port
#   ./start.sh --port 18930 --cert /path/fullchain.pem --key /path/key.pem
# Env (see README.md):
#   LOCALDY_CODE           access code for the web UI + API (unset = no auth)
#   LOCALDY_CLIENT_TOKEN   shared secret for client <-> server API calls
#   LOCALDY_DATA_DIR       where videos.db / play_records.db live (default: here)
#   LOCALDY_SERVER_MODE=1  set by this script: disables file-touching
#                          maintenance in core.py (del_1score / copy_5score,
#                          score backfill) and switches hash lookups to the DB.
# First run on a new machine:
#   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd "$(dirname "$0")" || exit 1
export LOCALDY_SERVER_MODE=1
if [ -x ".venv/bin/python" ]; then
    PY=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    echo "未找到 python3；请先安装 Python 3.10+" >&2
    exit 1
fi
"$PY" -c "import flask, sqlalchemy, flask_cors" 2>/dev/null || {
    echo "缺少依赖（Flask/SQLAlchemy）。请先执行：" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
}
exec "$PY" app.py "$@"
