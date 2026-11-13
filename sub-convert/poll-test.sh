#!/bin/bash
# Poll sub-convert test progress until done (max ~3 min).
for i in $(seq 1 18); do
  s=$(curl -s --max-time 5 http://127.0.0.1:18937/api/test/progress \
    | /opt/homebrew/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("status"), d.get("done"), "/", d.get("total"))' 2>/dev/null)
  echo "poll $i: $s"
  echo "$s" | grep -q '^done' && break
  sleep 10
done
