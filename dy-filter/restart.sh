#!/bin/bash
# dy-filter (115 云端视频预筛工作台) — restart script
# Port 18939, venv python, log to app.log
PORT=18939
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/app.log"

PID="$(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null || true)"
if [ -n "$PID" ]; then
  echo "killing old dy-filter (pid $PID) on port $PORT"
  kill "$PID" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    PID2="$(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null || true)"
    [ -z "$PID2" ] && break
  done
  PID2="$(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null || true)"
  if [ -n "$PID2" ]; then
    echo "force-killing pid $PID2"
    kill -9 "$PID2" 2>/dev/null || true
    sleep 1
  fi
fi

cd "$DIR"
VIDEO_TAG_PORT=$PORT nohup "$DIR/venv/bin/python" app.py >> "$LOG" 2>&1 &
echo "dy-filter started on port $PORT (pid $!)"
