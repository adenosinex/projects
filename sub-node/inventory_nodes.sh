#!/bin/bash
# inventory_nodes.sh — 盘点订阅节点的协议/IP版本/端口/服务器。
# 做法：起临时 mihomo（端口 17895/19095，与定时管线 17893/19093、v2 体验测试 17894/19094 错开），
# 拉最新订阅；同时读 ~/sub-node/sub.yaml（缓存的订阅文件）取每个节点的完整定义
# （type/server/port/udp），再解析 server 的 IP 版本（v4/v6/域名解析结果）。
# 只读操作，不改任何定时管线文件，不切主 mihomo 的选中节点。
set -u
DIR="$HOME/sub-node"
TDIR="/tmp/mihomo-inventory"
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
"$MIHOMO" -t -d "$TDIR" >/dev/null || { echo "配置校验失败"; exit 1; }
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
if [ -z "$NODES" ]; then echo "30 秒内没拉到节点"; exit 1; fi
echo "$NODES" > "$TDIR/nodes.txt"
grep -v '流量信息' "$TDIR/nodes.txt" > "$TDIR/nodes2.txt" || true
mv "$TDIR/nodes2.txt" "$TDIR/nodes.txt"
echo "拉到节点：$(grep -c . "$TDIR/nodes.txt" || true) 个"
cat "$TDIR/nodes.txt"

kill "$TEST_PID" 2>/dev/null || true
trap - EXIT
sleep 1

# 解析刚拉到的 sub.yaml（临时 mihomo 的缓存 = 最新订阅）+ 主 sub.yaml 对照
python3 - "$TDIR/sub.yaml" "$DIR/sub.yaml" "$TDIR/nodes.txt" "$TDIR/inventory.json" <<'PYEOF'
import json, sys, re, socket, ipaddress

fresh_path, cache_path, nodes_path, out_path = sys.argv[1:5]

def parse_sub_yaml(path):
    """粗解析 mihomo 订阅 yaml 的 proxies 列表，取 name/type/server/port/udp/network。"""
    proxies = {}
    try:
        text = open(path, encoding='utf-8', errors='replace').read()
    except Exception as e:
        print('读不到', path, e)
        return proxies
    cur = None
    for line in text.splitlines():
        m = re.match(r'\s*-\s+name:\s*(.+?)\s*$', line)
        if m:
            name = m.group(1).strip().strip('"').strip("'")
            cur = {'name': name}
            proxies[name] = cur
            continue
        if cur is None:
            continue
        for key in ('type', 'server', 'port', 'udp', 'network'):
            m = re.match(r'\s*' + key + r':\s*(.+?)\s*$', line)
            if m:
                cur[key] = m.group(1).strip().strip('"').strip("'")
    return proxies

fresh = parse_sub_yaml(fresh_path)
cache = parse_sub_yaml(cache_path)
nodes = [l.strip() for l in open(nodes_path, encoding='utf-8') if l.strip()]

def ip_version_of(server):
    """返回 (版本, 解析出的地址列表)。server 可能是 IP 或域名。"""
    if not server:
        return None, []
    try:
        ip = ipaddress.ip_address(server)
        return (4 if isinstance(ip, ipaddress.IPv4Address) else 6), [server]
    except ValueError:
        pass
    addrs = []
    try:
        infos = socket.getaddrinfo(server, None)
        seen = set()
        for info in infos:
            fam = info[0]
            addr = info[4][0]
            if addr not in seen:
                seen.add(addr)
                addrs.append(addr)
        v4 = any('.' in a for a in addrs)
        v6 = any(':' in a for a in addrs)
        if v4 and v6:
            return 'both', addrs
        if v4:
            return 4, addrs
        if v6:
            return 6, addrs
        return None, addrs
    except Exception:
        return None, addrs

rows = []
for name in nodes:
    src = fresh.get(name) or cache.get(name) or {}
    src_name = 'fresh' if name in fresh else ('cache' if name in cache else 'none')
    server = src.get('server', '')
    ver, addrs = ip_version_of(server)
    # 名字里的 v4/v6 标记
    name_mark = None
    if re.search(r'\bv4\b', name):
        name_mark = 4
    elif re.search(r'\bv6\b', name):
        name_mark = 6
    rows.append({
        'name': name,
        'type': src.get('type', ''),
        'server': server,
        'port': src.get('port', ''),
        'udp': src.get('udp', ''),
        'network': src.get('network', ''),
        'name_ip_mark': name_mark,
        'resolved_ip_version': ver,
        'resolved_addresses': addrs,
        'source': src_name,
    })

with open(out_path, 'w') as f:
    json.dump({'nodes': rows}, f, ensure_ascii=False, indent=2)

for r in rows:
    print('%-40s type=%-10s server=%-45s port=%-6s udp=%-6s name_mark=%s resolved=%s addrs=%s src=%s' % (
        r['name'][:40], r['type'], r['server'][:45], r['port'], r['udp'],
        r['name_ip_mark'], r['resolved_ip_version'], ','.join(r['resolved_addresses'][:3]), r['source']))
print('已写', out_path, '共', len(rows), '个节点')
PYEOF
rm -rf "$TDIR"
echo "完成"
