#!/bin/bash
# test_experience.sh v2 — 一次性“国内访问体验”全量测试（不改定时管线）。
# 对每个订阅节点同轮测：延迟、下行速度、上行速度、下行负载延迟、上行负载延迟。
# 做法：起临时 mihomo（端口 17894/19094，与定时测速的 17893/19093 错开，避免撞车），
# select 组逐个切节点，经它的 mixed 端口下载/上传 Cloudflare 文件。
# v2 修正（v1 教训）：延迟不再用 HTTPS TTFB 首请求——改取主 mihomo 的 url-test 口径
# （HTTP generate_204，每 5 分钟一轮，与本轮同窗口），缺失时用 HTTP TTFB 预热后 3 次取中位数；
# 全部 TTFB 探针改走 HTTP（与 url-test 同方法，免 TLS 开销）；0ms 样本视为探针失败，直接丢弃。
# 链式节点（名字含 chain）不测，记“探针形态测不了”。结果写 ~/sub-node/experience.json；
# 失败字段留空，不回填旧值。
set -u
DIR="$HOME/sub-node"
DL_URL="https://speed.cloudflare.com/__down?bytes=10000000"
UP_URL="https://speed.cloudflare.com/__up"
GEN_URL="http://www.gstatic.com/generate_204"
TIMEOUT_DL=20
TIMEOUT_UP=20
TDIR="/tmp/mihomo-experience-test"
MIXED=17894
CTRL=19094

rm -rf "$TDIR"; mkdir -p "$TDIR"
SUB_URL="$(grep -E 'url: "https' "$DIR/config.yaml" | head -1 | sed -E 's/.*url: "([^"]+)".*/\1/')"
if [ -z "$SUB_URL" ]; then echo "读不到订阅链接"; exit 1; fi
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
MIHOMO="/opt/homebrew/bin/mihomo"
"$MIHOMO" -t -d "$TDIR" >/dev/null || { echo "体验测试客户端配置校验失败"; exit 1; }
nohup "$MIHOMO" -d "$TDIR" > "$TDIR/mihomo.log" 2>&1 &
TEST_PID=$!
trap 'kill ${TEST_PID:-} 2>/dev/null || true' EXIT

# 等 provider 拉到节点（最多 30 秒）
NODES=""
for _ in $(seq 1 15); do
  curl -s --max-time 5 "http://127.0.0.1:${CTRL}/providers/proxies/sub" -o "$TDIR/provider.json" 2>/dev/null || true
  NODES="$(python3 -c "import json; d=json.load(open('$TDIR/provider.json')); print('\n'.join(p.get('name','') for p in d.get('proxies',[])))" 2>/dev/null || true)"
  if [ -n "$NODES" ]; then break; fi
  sleep 2
done
if [ -z "$NODES" ]; then echo "体验测试客户端 30 秒内没拉到节点"; exit 1; fi
echo "$NODES" > "$TDIR/nodes.txt"
grep -v '流量信息' "$TDIR/nodes.txt" > "$TDIR/nodes2.txt" || true
mv "$TDIR/nodes2.txt" "$TDIR/nodes.txt"
echo "拉到节点："
cat "$TDIR/nodes.txt"

# 延迟：优先取主 mihomo 的 url-test 口径（canonical，与状态页同源）
: > "$TDIR/delays.txt"
MSECRET="$(cat "$DIR/controller.secret" 2>/dev/null || true)"
if [ -n "$MSECRET" ]; then
  curl -s --max-time 10 -H "Authorization: Bearer $MSECRET" "http://127.0.0.1:9090/providers/proxies/sub" -o "$TDIR/main_provider.json" 2>/dev/null || true
  python3 - "$TDIR/main_provider.json" "$TDIR/delays.txt" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
with open(sys.argv[2], 'w') as f:
    for p in d.get('proxies', []):
        h = p.get('history', [])
        if h and h[-1].get('delay'):
            f.write(p.get('name','') + '|' + str(h[-1]['delay']) + '\n')
PYEOF
  echo "主 mihomo 延迟已取：$(grep -c . "$TDIR/delays.txt" || true) 个节点"
else
  echo "无控制器密钥，延迟全部现场测"
fi

# 1MB 上传体
head -c 1000000 /dev/zero > "$TDIR/upload.bin"

# 单次 HTTP TTFB 探针（毫秒）；失败或 0ms 返回空（0ms 是探针瞬间失败，不是真延迟）
sample_lat() {
  local t
  t="$(curl -o /dev/null -s -w '%{time_starttransfer}' --max-time 5 -x "http://127.0.0.1:${MIXED}" "$GEN_URL" </dev/null 2>/dev/null || true)"
  case "$t" in ''|*[!0-9.]*) return 1 ;; esac
  python3 -c "import sys; v=int(round(float('$t')*1000)); sys.exit(1) if v<=0 else print(v)"
}

RESULTS="$TDIR/results.txt"
: > "$RESULTS"
while IFS= read -r node; do
  [ -z "$node" ] && continue
  case "$node" in *流量信息*) continue ;; esac
  TS="$(date '+%Y-%m-%d %H:%M:%S')"
  case "$node" in
    *chain*)
      echo "${node}||||||${TS}" >> "$RESULTS"
      echo "${node}: 探针形态测不了"
      continue
      ;;
  esac
  curl -s --max-time 10 -X PUT -H 'Content-Type: application/json' -d "{\"name\":\"${node}\"}" "http://127.0.0.1:${CTRL}/proxies/SPEED" </dev/null >/dev/null
  sleep 1
  # 延迟：先查主 mihomo 的 url-test 结果，没有再现场 HTTP TTFB（预热 1 次后 3 次取中位数）
  DELAY="$(awk -F'|' -v n="$node" '$1==n {print $2; exit}' "$TDIR/delays.txt")"
  if [ -z "$DELAY" ]; then
    sample_lat >/dev/null 2>&1 || true
    DS=""
    for _ in 1 2 3; do
      m="$(sample_lat || true)"
      [ -n "$m" ] && DS="$DS $m"
    done
    DELAY="$(echo "$DS" | python3 -c "import statistics,sys; v=[int(x) for x in sys.stdin.read().split()]; print(int(round(statistics.median(v)))) if v else print('')" 2>/dev/null || true)"
  fi
  # 下行 + 下行负载延迟（HTTP 探针，2 秒采样）
  : > "$TDIR/dl_lat.txt"
  curl -s -o /dev/null -w '%{speed_download}' --max-time "$TIMEOUT_DL" -x "http://127.0.0.1:${MIXED}" "$DL_URL" </dev/null > "$TDIR/dl_speed.txt" 2>/dev/null &
  DL_PID=$!
  while kill -0 "$DL_PID" 2>/dev/null; do
    ms="$(sample_lat || true)"
    [ -n "$ms" ] && echo "$ms" >> "$TDIR/dl_lat.txt"
    sleep 2
  done
  wait "$DL_PID" 2>/dev/null || true
  DL_SPD="$(cat "$TDIR/dl_speed.txt" 2>/dev/null || true)"
  case "$DL_SPD" in ''|*[!0-9.]*) DL_SPD="" ;; esac
  if [ -n "$DL_SPD" ]; then
    DL_MBPS="$(python3 -c "print(round(float('$DL_SPD')*8/1e6, 1))")"
    case "$DL_MBPS" in 0|0.0) DL_MBPS="" ;; esac
  else
    DL_MBPS=""
  fi
  DL_LAT="$(tr '\n' ' ' < "$TDIR/dl_lat.txt" | sed 's/ $//')"
  # 上行 + 上行负载延迟（HTTP 探针，1 秒采样，上传短）
  : > "$TDIR/up_lat.txt"
  curl -s -o /dev/null -w '%{speed_upload}' --max-time "$TIMEOUT_UP" -x "http://127.0.0.1:${MIXED}" -X POST --data-binary @"$TDIR/upload.bin" "$UP_URL" </dev/null > "$TDIR/up_speed.txt" 2>/dev/null &
  UP_PID=$!
  while kill -0 "$UP_PID" 2>/dev/null; do
    ms="$(sample_lat || true)"
    [ -n "$ms" ] && echo "$ms" >> "$TDIR/up_lat.txt"
    sleep 1
  done
  wait "$UP_PID" 2>/dev/null || true
  UP_SPD="$(cat "$TDIR/up_speed.txt" 2>/dev/null || true)"
  case "$UP_SPD" in ''|*[!0-9.]*) UP_SPD="" ;; esac
  if [ -n "$UP_SPD" ]; then
    UP_MBPS="$(python3 -c "print(round(float('$UP_SPD')*8/1e6, 1))")"
    case "$UP_MBPS" in 0|0.0) UP_MBPS="" ;; esac
  else
    UP_MBPS=""
  fi
  UP_LAT="$(tr '\n' ' ' < "$TDIR/up_lat.txt" | sed 's/ $//')"
  echo "${node}|${DELAY}|${DL_MBPS}|${UP_MBPS}|${DL_LAT}|${UP_LAT}|${TS}" >> "$RESULTS"
  echo "${node}: 延迟 ${DELAY:-?}ms 下行 ${DL_MBPS:-?}Mbps 上行 ${UP_MBPS:-?}Mbps"
done < "$TDIR/nodes.txt"

kill "$TEST_PID" 2>/dev/null || true
trap - EXIT
sleep 1
python3 - "$RESULTS" "$DIR/experience.json" <<'PYEOF'
import json, sys, statistics
from datetime import datetime
results, out = sys.argv[1], sys.argv[2]

def num(s):
    try:
        return float(s) if s not in ('', None) else None
    except ValueError:
        return None

def lat_stats(s):
    vals = []
    for x in (s or '').split():
        try:
            v = int(x)
            if v > 0:
                vals.append(v)
        except ValueError:
            pass
    if not vals:
        return {'median': None, 'max': None, 'n': 0}
    return {'median': int(round(statistics.median(vals))), 'max': max(vals), 'n': len(vals)}

rows = []
with open(results) as f:
    for line in f:
        line = line.rstrip('\n')
        if not line:
            continue
        parts = line.split('|')
        while len(parts) < 7:
            parts.append('')
        name, delay, dl, up, dllat, uplat, ts = parts[:7]
        dl_v = num(dl)
        up_v = num(up)
        delay_v = num(delay)
        if delay_v == 0:
            delay_v = None
        if 'chain' in name:
            status = '探针形态测不了'
        elif dl_v is None or dl_v <= 0:
            status = 'unreachable'
        else:
            status = 'ok'
        rows.append({
            'name': name,
            'delay_ms': int(delay_v) if delay_v is not None else None,
            'download_mbps': dl_v,
            'upload_mbps': up_v,
            'loaded_latency_download_ms': lat_stats(dllat),
            'loaded_latency_upload_ms': lat_stats(uplat),
            'timestamp': ts,
            'status': status,
        })
data = {
    'updated': datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z'),
    'method': 'v2 (corrected rerun; v1 preserved as experience-v1.json). temp mihomo (ports 17894/19094); delay from main mihomo url-test provider history (HTTP http://www.gstatic.com/generate_204, same 5-min window), fallback median of 3 HTTP TTFB after 1 warm-up; download 10MB single-stream from https://speed.cloudflare.com/__down?bytes=10000000; upload 1MB POST to https://speed.cloudflare.com/__up; loaded latency = HTTP TTFB samples via the same node during transfer (2s interval download, 1s interval upload); 0ms samples discarded as probe failures; chain nodes not probed',
    'nodes': rows,
}
with open(out, 'w') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print('体验测试已写：', len(rows), '个节点 ->', out)
PYEOF
rm -rf "$TDIR"
echo "完成"
