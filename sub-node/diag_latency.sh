#!/bin/bash
# diag_latency.sh — 诊断：为什么体验测试的 TTFB 延迟（~1000ms）远高于状态页的 url-test 延迟（~250ms）。
# 只读主 mihomo 的 provider 延迟；另起临时 mihomo 对比 HTTP/HTTPS generate_204 的首请求与后续请求。
# 不动主配置、不改选中节点。
set -u
DIR="$HOME/sub-node"
SECRET="$(cat "$DIR/controller.secret" 2>/dev/null || true)"
echo "== 主 mihomo provider 延迟（url-test 口径）=="
if [ -n "$SECRET" ]; then
  curl -s --max-time 10 -H "Authorization: Bearer $SECRET" "http://127.0.0.1:9090/providers/proxies/sub" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for p in d.get('proxies',[]):
    h=p.get('history',[])
    delay=h[-1].get('delay') if h else None
    print(p.get('name',''), '|', delay)
" 2>/dev/null || echo "读主 mihomo 失败"
else
  echo "无控制器密钥"
fi
echo "== 主 mihomo proxy-groups 配置 =="
sed -n '/proxy-groups/,/rules:/p' "$DIR/config.yaml" | head -30
echo "== 直连（不走代理）TTFB =="
for u in "https://www.gstatic.com/generate_204" "http://www.gstatic.com/generate_204"; do
  for i in 1 2; do
    t="$(curl -o /dev/null -s -w '%{time_starttransfer}' --max-time 8 "$u" 2>/dev/null || echo FAIL)"
    echo "直连 $u #$i: ${t}s"
  done
done

TDIR="/tmp/mihomo-diag-test"
MIXED=17895
CTRL=19095
rm -rf "$TDIR"; mkdir -p "$TDIR"
SUB_URL="$(grep -E 'url: "https' "$DIR/config.yaml" | head -1 | sed -E 's/.*url: "([^"]+)".*/\1/')"
cat > "$TDIR/config.yaml" <<EOF
mixed-port: $MIXED
allow-lan: false
bind-address: 127.0.0.1
mode: rule
log-level: warning
external-controller: 127.0.0.1:$CTRL
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
/opt/homebrew/bin/mihomo -t -d "$TDIR" >/dev/null || { echo "校验失败"; exit 1; }
nohup /opt/homebrew/bin/mihomo -d "$TDIR" > "$TDIR/mihomo.log" 2>&1 &
TEST_PID=$!
trap 'kill ${TEST_PID:-} 2>/dev/null || true' EXIT
NODES=""
for _ in $(seq 1 15); do
  NODES="$(curl -s --max-time 5 "http://127.0.0.1:${CTRL}/providers/proxies/sub" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(p.get('name','') for p in d.get('proxies',[])))" 2>/dev/null || true)"
  if [ -n "$NODES" ]; then break; fi
  sleep 2
done
NODE="$(echo "$NODES" | grep -v '流量信息' | head -1)"
echo "用节点：$NODE"
curl -s --max-time 10 -X PUT -H 'Content-Type: application/json' -d "{\"name\":\"${NODE}\"}" "http://127.0.0.1:${CTRL}/proxies/SPEED" </dev/null >/dev/null
sleep 1
for u in "http://www.gstatic.com/generate_204" "https://www.gstatic.com/generate_204" "https://speed.cloudflare.com/__down?bytes=100"; do
  for i in 1 2 3; do
    t="$(curl -o /dev/null -s -w '%{time_starttransfer}' --max-time 8 -x "http://127.0.0.1:${MIXED}" "$u" </dev/null 2>/dev/null || echo FAIL)"
    echo "经节点 $u #$i: ${t}s"
  done
done
kill "$TEST_PID" 2>/dev/null || true
trap - EXIT
rm -rf "$TDIR"
echo "诊断完成"
