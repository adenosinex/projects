#!/bin/bash
# test-nodes.sh — 互测探针（每 30 分钟由 LaunchAgent 跑一次，也可手工跑）。
# 做三件事：① 记下各节点延迟与当前选中节点；② 订阅端点失效时自动切到另一条；
# ③ 检查 AnyTLS 监听是否还在。结果写进 logs/test.log。
set -u
DIR="$HOME/sub-node"
SECRET="$(cat "$DIR/controller.secret" 2>/dev/null || echo "")"
LOG="$DIR/logs/test.log"
mkdir -p "$DIR/logs"
TS="$(date '+%Y-%m-%d %H:%M:%S')"
{
echo "[$TS] --- 互测 ---"
if [ -z "$SECRET" ]; then echo "无控制器密钥，跳过"; exit 0; fi

# ① 各节点延迟 + 当前选中节点（延迟在 /providers/proxies/sub 里，不在 /proxies）
SEL="$(curl -s --max-time 10 -H "Authorization: Bearer $SECRET" "http://127.0.0.1:9090/proxies" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('proxies',{}).get('🚀 节点选择',{}).get('now','?'))" 2>/dev/null)"
echo "当前选中: ${SEL:-?}"
curl -s --max-time 10 -H "Authorization: Bearer $SECRET" "http://127.0.0.1:9090/providers/proxies/sub" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for p in d.get('proxies',[]):
    hist=p.get('history',[])
    delay=hist[-1].get('delay') if hist else None
    name=p.get('name')
    if delay is None: print(f'  {name}: 未测')
    elif delay==0: print(f'  {name}: 不通')
    else: print(f'  {name}: {delay}ms')
" 2>/dev/null

# ② 订阅端点健康；失效则自动切换
ACTIVE="$(grep -E '^    url: ' "$DIR/config.yaml" | head -1 | awk '{print $2}' | tr -d '"')"
if [ -n "$ACTIVE" ]; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "$ACTIVE" || true)"
  host="$(echo "$ACTIVE" | sed -E 's#https://([^/]+)/.*#\1#')"
  echo "订阅端点 $host → HTTP $code"
  if [ "$code" != "200" ]; then
    echo "当前订阅端点失效，尝试切换..."
    while IFS= read -r url; do
      [ -z "$url" ] && continue
      [ "$url" = "$ACTIVE" ] && continue
      c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "$url" || true)"
      h="$(echo "$url" | sed -E 's#https://([^/]+)/.*#\1#')"
      echo "  候选 $h → HTTP $c"
      if [ "$c" = "200" ]; then
        python3 - "$DIR/config.yaml" "$url" <<'PYEOF'
import sys
path, new_url = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines(True)
out = []
done = False
for ln in lines:
    if (not done) and ln.strip().startswith('url: ') and 'http' in ln:
        indent = ln[:len(ln) - len(ln.lstrip())]
        out.append(f'{indent}url: "{new_url}"\n')
        done = True
    else:
        out.append(ln)
open(path, 'w').writelines(out)
PYEOF
        echo "已切换到 ${h}，重启 mihomo..."
        launchctl kickstart -k "gui/$(id -u)/org.zin.subnode" 2>/dev/null || true
        break
      fi
    done < "$DIR/subscription.urls"
  fi
fi

# ③ AnyTLS 监听
if lsof -nP -iTCP:8443 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "AnyTLS 监听正常（:8443）"
else
  echo "AnyTLS 监听缺失（:8443），尝试重启 mihomo..."
  launchctl kickstart -k "gui/$(id -u)/org.zin.subnode" 2>/dev/null || true
fi

# ④ 测下载速度（逐个节点，10MB，写 speed.json；供状态文件用）
bash "$DIR/test_speed.sh" 2>/dev/null || echo "速度测试失败（只影响速度字段，延迟照常）"

# ⑤ 刷新对外发布的测试信息
/opt/homebrew/bin/python3 "$DIR/gen_status.py" 2>/dev/null || echo "状态文件生成失败"
} >> "$LOG" 2>&1
echo "互测完成，结果在 $LOG"
