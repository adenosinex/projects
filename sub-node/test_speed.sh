#!/bin/bash
# test_speed.sh — 逐个测 11 个订阅节点的下载速度，结果写 ~/sub-node/speed.json。
# 做法：起一个临时 mihomo（独立端口，不动主配置），把 select 组逐个切到每个节点，
# 经它的 mixed 端口下载 Cloudflare 10MB 文件，用 curl 的 speed_download 计速。
# 单节点超时 20 秒；速度单位 Mbps（兆比特/秒），失败记 0。
set -euo pipefail
DIR="$HOME/sub-node"
SPEED_URL="https://speed.cloudflare.com/__down?bytes=10000000"
TIMEOUT=20
TDIR="/tmp/mihomo-speed-test"
rm -rf "$TDIR"; mkdir -p "$TDIR"
SUB_URL="$(grep -E 'url: "https' "$DIR/config.yaml" | head -1 | sed -E 's/.*url: "([^"]+)".*/\1/')"
if [ -z "$SUB_URL" ]; then echo "读不到订阅链接"; exit 1; fi
cat > "$TDIR/config.yaml" <<EOF
mixed-port: 17893
allow-lan: false
bind-address: 127.0.0.1
mode: rule
log-level: warning
external-controller: 127.0.0.1:19093
proxy-providers:
  sub:
    type: http
    url: "$SUB_URL"
    path: ./sub.yaml
    interval: 3600
proxy-groups:
  - name: SPEED
    type: select
    use:
      - sub
rules:
  - MATCH,SPEED
EOF
MIHOMO="/opt/homebrew/bin/mihomo"
"$MIHOMO" -t -d "$TDIR" >/dev/null || { echo "测速客户端配置校验失败"; exit 1; }
nohup "$MIHOMO" -d "$TDIR" > "$TDIR/mihomo.log" 2>&1 &
TEST_PID=$!
# 等 provider 拉到节点（订阅要几秒；最多等 30 秒，而不是固定睡 4 秒）
NODES=""
for _ in $(seq 1 15); do
  NODES="$(curl -s --max-time 5 "http://127.0.0.1:19093/providers/proxies/sub" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(p.get('name','') for p in d.get('proxies',[])))" 2>/dev/null || true)"
  if [ -n "$NODES" ]; then break; fi
  sleep 2
done
if [ -z "$NODES" ]; then echo "测速客户端 30 秒内没拉到节点"; kill "$TEST_PID" 2>/dev/null || true; rm -rf "$TDIR"; exit 1; fi
RESULTS="$TDIR/results.txt"
: > "$RESULTS"
while IFS= read -r node; do
  [ -z "$node" ] && continue
  curl -s --max-time 10 -X PUT -H 'Content-Type: application/json' -d "{\"name\":\"${node}\"}" "http://127.0.0.1:19093/proxies/SPEED" >/dev/null
  sleep 1
  spd="$(curl -s -o /dev/null -w '%{speed_download}' --max-time "$TIMEOUT" -x http://127.0.0.1:17893 "$SPEED_URL" 2>/dev/null || echo 0)"
  case "$spd" in ''|*[!0-9.]*) spd=0 ;; esac
  mbps="$(python3 -c "print(round(float('$spd')*8/1e6, 1))")"
  echo "${node}|${mbps}" >> "$RESULTS"
  echo "${node}: ${mbps} Mbps"
done <<< "$NODES"
kill "$TEST_PID" 2>/dev/null || true
sleep 1
python3 - "$RESULTS" "$DIR/speed.json" <<'PYEOF'
import json, sys
from datetime import datetime
results, out = sys.argv[1], sys.argv[2]
rows = []
with open(results) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        name, mbps = line.rsplit('|', 1)
        rows.append({'name': name, 'speed_mbps': float(mbps), 'ok': float(mbps) > 0})
data = {'updated': datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z'), 'nodes': rows}
with open(out, 'w') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print('速度已写：', len(rows), '个节点')
PYEOF
rm -rf "$TDIR"
