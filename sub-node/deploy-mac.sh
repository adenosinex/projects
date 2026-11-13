#!/bin/bash
# deploy-mac.sh — 在 Mac 上部署订阅节点 + 局域网 AnyTLS 节点。
# 用法：bash ~/sub-node/deploy-mac.sh
# 依赖：mihomo（brew）、subscription.urls（600，含三条订阅链接，本机专用）。
# 行为：选通的订阅链接 → 生成证书/密码/控制器密钥（如缺失）→ 写 config.yaml（600）
#       → 校验 → 经 launchd 启动/重启 mihomo → 验证节点数与 AnyTLS 监听。
# 凭据只存本机：订阅链接在 subscription.urls / config.yaml（600），
# AnyTLS 密码在 anytls.password（600），控制器密钥在 controller.secret（600）。
set -euo pipefail

DIR="$HOME/sub-node"
URLS_FILE="$DIR/subscription.urls"
CONFIG="$DIR/config.yaml"
LOG_DIR="$DIR/logs"
PLIST="$HOME/Library/LaunchAgents/org.zin.subnode.plist"
mkdir -p "$DIR" "$LOG_DIR" "$HOME/Library/LaunchAgents"

# 1. mihomo
if ! command -v mihomo >/dev/null 2>&1; then
  echo "正在安装 mihomo..."
  brew install mihomo
fi
echo "mihomo: $(mihomo -v 2>&1 | head -1)"

# 2. 局域网 IP（IPv4 节点只绑这个地址）
LAN_IP="$(ipconfig getifaddr en0)"
if [ -z "$LAN_IP" ]; then echo "拿不到局域网 IP（en0）"; exit 1; fi
echo "局域网 IP: $LAN_IP"

# 2b. 公网 IPv6（IPv6 节点用；优先用 DDNS 解析，与客户端看到的一致）
IPV6="$(dig +short apple.su7.dpdns.org AAAA 2>/dev/null | head -1)"
if [ -z "$IPV6" ]; then
  IPV6="$(ifconfig en0 | grep 'inet6' | grep -v -E 'temporary|deprecated|fe80|::1' | awk '{print $2}' | head -1)"
fi
if [ -z "$IPV6" ]; then echo "拿不到公网 IPv6"; exit 1; fi
echo "公网 IPv6: $IPV6"

# 3. 选一条通的订阅链接（三条互为备份）
if [ ! -f "$URLS_FILE" ]; then echo "缺少 $URLS_FILE（订阅链接清单，本机专用）"; exit 1; fi
SUB_URL=""
while IFS= read -r url; do
  [ -z "$url" ] && continue
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "$url" || true)"
  host="$(echo "$url" | sed -E 's#https://([^/]+)/.*#\1#')"
  echo "订阅 $host → HTTP $code"
  if [ "$code" = "200" ]; then SUB_URL="$url"; break; fi
done < "$URLS_FILE"
if [ -z "$SUB_URL" ]; then echo "三条订阅链接都不通"; exit 1; fi
SUB_HOST="$(echo "$SUB_URL" | sed -E 's#https://([^/]+)/.*#\1#')"
echo "选用订阅：$SUB_HOST"

# 4. AnyTLS 自签证书（如缺失则生成；客户端需 skip-cert-verify）
if [ ! -f "$DIR/anytls.crt" ] || [ ! -f "$DIR/anytls.key" ]; then
  echo "生成 AnyTLS 自签证书..."
  openssl req -x509 -newkey rsa:2048 -keyout "$DIR/anytls.key" -out "$DIR/anytls.crt" \
    -days 3650 -nodes -subj "/CN=$LAN_IP" \
    -addext "subjectAltName=IP:$LAN_IP" 2>/dev/null
  chmod 600 "$DIR/anytls.key" "$DIR/anytls.crt"
fi

# 5. AnyTLS 密码（只存本机，不进订阅分发）
if [ ! -f "$DIR/anytls.password" ]; then
  openssl rand -hex 12 > "$DIR/anytls.password"
  chmod 600 "$DIR/anytls.password"
fi
ANYTLS_PASS="$(cat "$DIR/anytls.password")"

# 6. 控制器密钥（只存本机）
if [ ! -f "$DIR/controller.secret" ]; then
  openssl rand -hex 16 > "$DIR/controller.secret"
  chmod 600 "$DIR/controller.secret"
fi
SECRET="$(cat "$DIR/controller.secret")"

# 6b. 发布 token（状态与订阅的文件名，token 即凭据，只存本机）
if [ ! -f "$DIR/publish.token" ]; then
  openssl rand -hex 16 > "$DIR/publish.token"
  chmod 600 "$DIR/publish.token"
fi
TOKEN="$(cat "$DIR/publish.token")"

# 7. 写 config.yaml
cat > "$CONFIG" <<EOF
# mihomo 配置（由 deploy-mac.sh 生成；含本机订阅链接，600 权限，仅本机）
mixed-port: 7890
allow-lan: false
bind-address: 127.0.0.1
mode: rule
log-level: info
ipv6: true
external-controller: 127.0.0.1:9090
secret: "$SECRET"
unified-delay: true

proxy-providers:
  sub:
    type: http
    url: "$SUB_URL"
    path: ./sub.yaml
    interval: 3600
    health-check:
      enable: true
      url: http://www.gstatic.com/generate_204
      interval: 300

proxy-groups:
  - name: "🚀 节点选择"
    type: url-test
    use:
      - sub
    url: http://www.gstatic.com/generate_204
    interval: 300
    tolerance: 50
  - name: "🌐 全部节点"
    type: select
    use:
      - sub

listeners:
  - name: mac-anytls-v4
    type: anytls
    listen: $LAN_IP
    port: 8443
    users:
      mac: "$ANYTLS_PASS"
    certificate: ./anytls.crt
    private-key: ./anytls.key
  - name: mac-anytls-v6
    type: anytls
    listen: "$IPV6"
    port: 8443
    users:
      mac: "$ANYTLS_PASS"
    certificate: ./anytls.crt
    private-key: ./anytls.key
  - name: mac-hysteria2-v4
    type: hysteria2
    listen: $LAN_IP
    port: 8444
    users:
      mac: "$ANYTLS_PASS"
    up: 100
    down: 100
    alpn:
      - h3
    certificate: ./anytls.crt
    private-key: ./anytls.key
  - name: mac-hysteria2-v6
    type: hysteria2
    listen: "$IPV6"
    port: 8444
    users:
      mac: "$ANYTLS_PASS"
    up: 100
    down: 100
    alpn:
      - h3
    certificate: ./anytls.crt
    private-key: ./anytls.key

rules:
  - MATCH,🚀 节点选择
EOF
chmod 600 "$CONFIG"
echo "已写 config.yaml（订阅：${SUB_HOST}，Mac 节点：AnyTLS/Hysteria2 双栈 4 个）"

# 7b. Mac 节点订阅（4 个节点：AnyTLS/Hysteria2 × IPv4/IPv6；文件名含 token，token 即凭据）
mkdir -p "$DIR/status"
cat > "$DIR/status/${TOKEN}.yaml" <<EOF
proxies:
  - name: mac-AnyTLS-IPv4
    type: anytls
    server: ${LAN_IP}
    port: 8443
    password: "${ANYTLS_PASS}"
    client-fingerprint: chrome
    udp: true
    skip-cert-verify: true
  - name: mac-AnyTLS-IPv6
    type: anytls
    server: "${IPV6}"
    port: 8443
    password: "${ANYTLS_PASS}"
    client-fingerprint: chrome
    udp: true
    skip-cert-verify: true
  - name: mac-Hysteria2-IPv4
    type: hysteria2
    server: ${LAN_IP}
    port: 8444
    password: "${ANYTLS_PASS}"
    skip-cert-verify: true
    alpn:
      - h3
  - name: mac-Hysteria2-IPv6
    type: hysteria2
    server: "${IPV6}"
    port: 8444
    password: "${ANYTLS_PASS}"
    skip-cert-verify: true
    alpn:
      - h3
EOF
chmod 600 "$DIR/status/${TOKEN}.yaml"
echo "已写 Mac 节点订阅（4 个节点）"

# 8. 校验配置
mihomo -t -d "$DIR" && echo "配置校验通过"

# 9. 经 launchd 启动/重启（开机自启由 LaunchAgent 负责）
if launchctl list org.zin.subnode >/dev/null 2>&1; then
  echo "重启 mihomo（launchd）..."
  launchctl kickstart -k "gui/$(id -u)/org.zin.subnode"
else
  echo "加载 LaunchAgent..."
  launchctl load "$PLIST" 2>/dev/null || launchctl bootstrap "gui/$(id -u)" "$PLIST"
fi

# 10. 等控制器就绪
ok=0
for _ in $(seq 1 30); do
  if curl -s -H "Authorization: Bearer $SECRET" --max-time 3 "http://127.0.0.1:9090/version" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ "$ok" != "1" ]; then echo "控制器 30 秒内未就绪，请看 $LOG_DIR/mihomo.log"; exit 1; fi
echo "控制器就绪"

# 10b. 生成对外状态文件，并启动状态服务（开机自启由 LaunchAgent 负责）
/opt/homebrew/bin/python3 "$DIR/gen_status.py"
SUBSTATUS_PLIST="$HOME/Library/LaunchAgents/org.zin.substatus.plist"
if launchctl list org.zin.substatus >/dev/null 2>&1; then
  echo "重启状态服务（launchd）..."
  launchctl kickstart -k "gui/$(id -u)/org.zin.substatus"
else
  echo "加载状态服务 LaunchAgent..."
  launchctl load "$SUBSTATUS_PLIST" 2>/dev/null || launchctl bootstrap "gui/$(id -u)" "$SUBSTATUS_PLIST"
fi
sleep 2
echo "状态服务：http://apple.su7.dpdns.org:18932/${TOKEN}.json"
echo "Mac 节点订阅：http://apple.su7.dpdns.org:18932/${TOKEN}.yaml"

# 11. 验证：订阅节点数与类型
curl -s --max-time 10 -H "Authorization: Bearer $SECRET" "http://127.0.0.1:9090/providers/proxies/sub" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); ps=d.get('proxies',[]); print('订阅节点数:', len(ps)); [print(' ', p.get('name'), '|', p.get('type')) for p in ps]"

# 12. 验证：AnyTLS 监听
echo "AnyTLS 监听："
lsof -nP -iTCP:8443 -sTCP:LISTEN
echo "部署完成"
