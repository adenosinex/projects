#!/bin/bash
# test_experience_udp.sh — 补充测试：UDP + 显式 IPv4/IPv6（一次性，不改定时管线）。
# 在 v2 TCP 体验测试基础上补充：
#   - UDP：经节点的 SOCKS5 UDP ASSOCIATE 发 DNS 查询（UDP）到 8.8.8.8，3 次取中位 RTT。
#     AnyTLS 节点靠 udp:true 中继；Hysteria2 节点本身就是 UDP/QUIC 传输。
#   - IPv4/IPv6：从实时订阅 sub.yaml 取每个节点的 type/server/port/udp，
#     解析 server 的 IP 版本，与名字里的 v4/v6 标记对照。
#   - TCP：同轮重测延迟/下行/上行/负载延迟（与 v2 同方法），便于 TCP vs UDP 同轮对比。
#     结果写 ~/sub-node/experience-udp.json，不覆盖 v2 的 experience.json。
# 限制：Mac 的 curl 8.7.1 不支持 HTTP/3，故无 UDP 吞吐量测试；UDP 只测 DNS RTT。
# 链式节点（名字含 chain）不测，记“探针形态测不了”。失败字段留空，不回填旧值。
set -u
DIR="$HOME/sub-node"
DL_URL="https://speed.cloudflare.com/__down?bytes=10000000"
UP_URL="https://speed.cloudflare.com/__up"
GEN_URL="http://www.gstatic.com/generate_204"
TIMEOUT_DL=20
TIMEOUT_UP=20
TDIR="/tmp/mihomo-experience-udp"
MIXED=17895
CTRL=19095

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
"$MIHOMO" -t -d "$TDIR" >/dev/null || { echo "补充测试客户端配置校验失败"; exit 1; }
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
if [ -z "$NODES" ]; then echo "补充测试客户端 30 秒内没拉到节点"; exit 1; fi
echo "$NODES" > "$TDIR/nodes.txt"
grep -v '流量信息' "$TDIR/nodes.txt" > "$TDIR/nodes2.txt" || true
mv "$TDIR/nodes2.txt" "$TDIR/nodes.txt"
echo "拉到节点：$(grep -c . "$TDIR/nodes.txt" || true) 个"

# 盘点：从刚拉到的 sub.yaml 取 type/server/port/udp，解析 IP 版本
python3 - "$TDIR/sub.yaml" "$TDIR/nodes.txt" "$TDIR/inventory.txt" <<'PYEOF'
import sys, re, ipaddress, socket
sub_path, nodes_path, out_path = sys.argv[1:4]
text = open(sub_path, encoding='utf-8', errors='replace').read()
proxies = {}
cur = None
for line in text.splitlines():
    m = re.match(r'\s*-\s+name:\s*(.+?)\s*$', line)
    if m:
        cur = {'name': m.group(1).strip().strip('"').strip("'")}
        proxies[cur['name']] = cur
        continue
    if cur is None:
        continue
    for key in ('type', 'server', 'port', 'udp'):
        m = re.match(r'\s*' + key + r':\s*(.+?)\s*$', line)
        if m:
            cur[key] = m.group(1).strip().strip('"').strip("'")
nodes = [l.strip() for l in open(nodes_path, encoding='utf-8') if l.strip()]
with open(out_path, 'w') as f:
    for name in nodes:
        p = proxies.get(name, {})
        server = p.get('server', '')
        ver = ''
        if server:
            try:
                ip = ipaddress.ip_address(server)
                ver = '4' if isinstance(ip, ipaddress.IPv4Address) else '6'
            except ValueError:
                try:
                    infos = socket.getaddrinfo(server, None)
                    addrs = set(i[4][0] for i in infos)
                    v4 = any('.' in a for a in addrs)
                    v6 = any(':' in a for a in addrs)
                    ver = 'both' if (v4 and v6) else ('4' if v4 else ('6' if v6 else ''))
                except Exception:
                    ver = ''
        f.write('%s|%s|%s|%s|%s|%s\n' % (name, p.get('type', ''), server, p.get('port', ''), p.get('udp', ''), ver))
print('盘点完成')
PYEOF
cat "$TDIR/inventory.txt"

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
fi

# 1MB 上传体
head -c 1000000 /dev/zero > "$TDIR/upload.bin"

# UDP DNS 测试（SOCKS5 UDP ASSOCIATE，经节点向 8.8.8.8 发 DNS 查询，3 次取中位）
cat > "$TDIR/udp_dns.py" <<'PYEOF'
import socket, struct, sys, time, statistics

def build_dns_query(domain, txid):
    header = struct.pack('>HHHHHH', txid, 0x0100, 1, 0, 0, 0)
    qname = b''
    for label in domain.split('.'):
        qname += bytes([len(label)]) + label.encode()
    qname += b'\x00'
    return header + qname + struct.pack('>HH', 1, 1)

def socks5_udp_dns(proxy_host, proxy_port, dns_server, domain, count=3, timeout=5):
    rtts = []
    try:
        tcp = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
        tcp.sendall(b'\x05\x01\x00')
        resp = tcp.recv(2)
        if len(resp) < 2 or resp[0] != 5 or resp[1] != 0:
            return None, 'socks5 greeting failed'
        tcp.sendall(b'\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00')
        resp = tcp.recv(22)
        if len(resp) < 4 or resp[0] != 5 or resp[1] != 0:
            return None, 'udp associate failed'
        atyp = resp[3]
        if atyp == 1:
            relay_addr = socket.inet_ntoa(resp[4:8])
            relay_port = struct.unpack('>H', resp[8:10])[0]
        elif atyp == 3:
            dlen = resp[4]
            relay_addr = resp[5:5+dlen].decode()
            relay_port = struct.unpack('>H', resp[5+dlen:7+dlen])[0]
        elif atyp == 4:
            relay_addr = socket.inet_ntop(socket.AF_INET6, resp[4:20])
            relay_port = struct.unpack('>H', resp[20:22])[0]
        else:
            return None, 'unknown atyp'
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.settimeout(timeout)
        for i in range(count):
            txid = 0x1234 + i
            query = build_dns_query(domain, txid)
            udp_req = b'\x00\x00\x00\x01' + socket.inet_aton(dns_server) + struct.pack('>H', 53) + query
            t0 = time.time()
            udp.sendto(udp_req, (relay_addr, relay_port))
            try:
                data, _ = udp.recvfrom(4096)
                rtts.append((time.time() - t0) * 1000)
            except socket.timeout:
                pass
        udp.close()
        tcp.close()
    except Exception as e:
        return None, 'exception: %s' % e
    if not rtts:
        return None, 'no response'
    return statistics.median(rtts), None

if __name__ == '__main__':
    med, err = socks5_udp_dns('127.0.0.1', int(sys.argv[1]), '8.8.8.8', 'www.gstatic.com')
    if err:
        print('')
    else:
        print('%d' % round(med))
PYEOF

# 单次 HTTP TTFB 探针（毫秒）；失败或 0ms 返回空
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
      echo "${node}||||||||||||${TS}" >> "$RESULTS"
      echo "${node}: 探针形态测不了"
      continue
      ;;
  esac
  INV="$(awk -F'|' -v n="$node" '$1==n {print $2"|"$3"|"$4"|"$5"|"$6; exit}' "$TDIR/inventory.txt")"
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
  # UDP DNS RTT（3 次取中位，失败留空）
  UDP_RTT="$(python3 "$TDIR/udp_dns.py" "$MIXED" 2>/dev/null || true)"
  echo "${node}|${DELAY}|${DL_MBPS}|${UP_MBPS}|${DL_LAT}|${UP_LAT}|${UDP_RTT}|${INV}|${TS}" >> "$RESULTS"
  echo "${node}: 延迟 ${DELAY:-?}ms 下行 ${DL_MBPS:-?}Mbps 上行 ${UP_MBPS:-?}Mbps UDP-DNS ${UDP_RTT:-?}ms"
done < "$TDIR/nodes.txt"

kill "$TEST_PID" 2>/dev/null || true
trap - EXIT
sleep 1
python3 - "$RESULTS" "$DIR/experience-udp.json" <<'PYEOF'
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
        while len(parts) < 13:
            parts.append('')
        name, delay, dl, up, dllat, uplat, udp_rtt, ptype, server, port, udp_flag, ipver, ts = parts[:13]
        dl_v = num(dl)
        up_v = num(up)
        delay_v = num(delay)
        if delay_v == 0:
            delay_v = None
        udp_v = num(udp_rtt)
        if 'chain' in name:
            status = '探针形态测不了'
        elif dl_v is None or dl_v <= 0:
            status = 'unreachable'
        else:
            status = 'ok'
        rows.append({
            'name': name,
            'protocol': ptype,
            'server': server,
            'port': int(port) if port and port.isdigit() else None,
            'ip_version': int(ipver) if ipver in ('4', '6') else None,
            'udp_relay_config': udp_flag,
            'delay_ms': int(delay_v) if delay_v is not None else None,
            'download_mbps': dl_v,
            'upload_mbps': up_v,
            'loaded_latency_download_ms': lat_stats(dllat),
            'loaded_latency_upload_ms': lat_stats(uplat),
            'udp_dns_rtt_ms': int(udp_v) if udp_v is not None else None,
            'timestamp': ts,
            'status': status,
        })
data = {
    'updated': datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z'),
    'method': 'Supplementary UDP + IPv4/IPv6 test (temp mihomo ports 17895/19095). TCP: same round re-measure as v2 (delay from main mihomo url-test provider history, HTTP generate_204; download 10MB single-stream; upload 1MB POST; loaded latency = HTTP TTFB samples during transfer). UDP: DNS query via SOCKS5 UDP ASSOCIATE through the node to 8.8.8.8, 3 queries, median RTT (www.gstatic.com A). AnyTLS nodes relay UDP via udp:true; Hysteria2 nodes are UDP/QUIC-native. curl on Mac has no HTTP/3, so no UDP throughput test. Chain nodes not probed. v2 TCP results preserved in experience.json.',
    'nodes': rows,
}
with open(out, 'w') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print('补充测试已写：', len(rows), '个节点 ->', out)
PYEOF
rm -rf "$TDIR"
echo "完成"
