#!/bin/bash
# Restart the ping-test app (FastAPI on 127.0.0.1:18938).
# Kills by port (never pkill app.py — that would match other apps).
DIR="/Users/zin/ping-test"
PID=$(lsof -ti :18938 2>/dev/null)
if [ -n "$PID" ]; then
  kill "$PID" 2>/dev/null
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    lsof -ti :18938 >/dev/null 2>&1 || break
  done
fi
cd "$DIR" || exit 1
nohup "$DIR/venv/bin/python" -m uvicorn app:app --host 127.0.0.1 --port 18938 \
  > "$DIR/app.log" 2>&1 &
sleep 3
curl -s --max-time 5 http://127.0.0.1:18938/api/health || echo "health check failed"
