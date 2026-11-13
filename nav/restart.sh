#!/bin/bash
# nav restart: kill by port 18936, start uvicorn.
# Run on the Mac: bash /Users/zin/nav/restart.sh
cd /Users/zin/nav || exit 1
PID=$(lsof -ti :18936 2>/dev/null)
if [ -n "$PID" ]; then
  kill $PID 2>/dev/null
  for i in 1 2 3 4 5; do
    sleep 1
    lsof -ti :18936 >/dev/null 2>&1 || break
  done
  PID=$(lsof -ti :18936 2>/dev/null)
  [ -n "$PID" ] && kill -9 $PID 2>/dev/null
fi
nohup /Users/zin/nav/venv/bin/uvicorn app:app \
  --host 127.0.0.1 --port 18936 \
  --app-dir /Users/zin/nav \
  >> /Users/zin/nav/app.log 2>&1 &
sleep 2
curl -s --max-time 5 http://127.0.0.1:18936/health && echo " <- nav up"
