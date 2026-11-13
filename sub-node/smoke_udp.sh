#!/bin/bash
# smoke_udp.sh — 验证 UDP 测试方法是否走得通（一次性，不改定时管线）。
# 起临时 mihomo（17895/19095），切到一个 AnyTLS 节点和一个 Hysteria2 节点，
# 用 SOCKS5 UDP ASSOCIATE 发 DNS 查询（UDP）到 8.8.8.8，测往返时间；
# 同时检查 curl 是否支持 HTTP/3。
set -u
DIR="$HOME/sub-node"
TDIR="/tmp/mihomo-smoke-udp"
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
MIHOMO="/opt/homebrew/bin/mihomo"
"$MIHOMO" -t -d "$TDIR" >/dev/null || { echo "配置校验失败"; exit 1; }
nohup "$MIHOMO" -d "$TDIR" > "$TDIR/mihomo.log" 2>&1 &
TEST_PID=$!
trap 'kill ${TEST_PID:-} 2>/dev/null || true' EXIT

for _ in $(seq 1 15); do
  curl -s --max-time 5 "http://127.0.0.1:${CTRL}/providers/proxies/sub" -o "$TDIR/provider.json" 2>/dev/null || true
  if python3 -c "import json; d=json.load(open('$TDIR/provider.json')); assert d.get('proxies')" 2>/dev/null; then break; fi
  sleep 2
done

echo "=== curl HTTP/3 支持 ==="
curl --version | head -1
curl --version | grep -i -o 'http3[^ ]*' || echo "无 http3"
echo ""

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
            return None, 'socks5 greeting failed: %r' % resp
        tcp.sendall(b'\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00')
        resp = tcp.recv(22)
        if len(resp) < 4 or resp[0] != 5 or resp[1] != 0:
            return None, 'udp associate failed: %r' % resp
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
            return None, 'unknown atyp %d' % atyp
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
                t1 = time.time()
                rtts.append((t1 - t0) * 1000)
            except socket.timeout:
                pass
        udp.close()
        tcp.close()
    except Exception as e:
        return None, 'exception: %s' % e
    if not rtts:
        return None, 'no response (all %d timed out)' % count
    return statistics.median(rtts), None

if __name__ == '__main__':
    proxy_port = int(sys.argv[1])
    dns_server = sys.argv[2] if len(sys.argv) > 2 else '8.8.8.8'
    domain = sys.argv[3] if len(sys.argv) > 3 else 'www.gstatic.com'
    med, err = socks5_udp_dns('127.0.0.1', proxy_port, dns_server, domain)
    if err:
        print('UDP DNS 失败:', err)
    else:
        print('UDP DNS 中位 RTT: %.0f ms (%s via %s)' % (med, domain, dns_server))
PYEOF

for NODE in "shoc v4 la 20 2，4 207" "shoc v4 la 20 2，3 205"; do
  echo "=== 节点: $NODE ==="
  curl -s --max-time 10 -X PUT -H 'Content-Type: application/json' -d "{\"name\":\"${NODE}\"}" "http://127.0.0.1:${CTRL}/proxies/SPEED" </dev/null >/dev/null
  sleep 2
  python3 "$TDIR/udp_dns.py" "$MIXED" 8.8.8.8 www.gstatic.com
  echo ""
done

kill "$TEST_PID" 2>/dev/null || true
trap - EXIT
rm -rf "$TDIR"
echo "完成"
