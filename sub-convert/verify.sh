#!/bin/bash
# Verify sub-convert end to end on the Mac:
# 1. upload /Users/zin/sub-test/sub.yaml (vyy airport) via the API
# 2. wait for the auto speed test to finish (poll up to 5 min)
# 3. fetch the converted YAML via its token URL and validate it
set -u
BASE="http://127.0.0.1:18937"
echo "== add subscription =="
ADD=$(curl -s --max-time 60 -F "file=@/Users/zin/sub-test/sub.yaml;filename=sub.yaml" -F "name=vyy" "$BASE/api/subscriptions")
echo "$ADD"
SUB_ID=$(echo "$ADD" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
TOKEN=$(echo "$ADD" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])" 2>/dev/null)
if [ -z "$SUB_ID" ]; then echo "ADD FAILED"; exit 1; fi
echo "sub_id=$SUB_ID"
echo "== wait for test (up to 300s) =="
for i in $(seq 1 60); do
  sleep 5
  ST=$(curl -s --max-time 10 "$BASE/api/subscriptions")
  DONE=$(echo "$ST" | python3 -c "
import sys, json
subs = json.load(sys.stdin)
m = next((s for s in subs if s['id'] == '$SUB_ID'), None)
print('1' if (m and m.get('last_test_at')) else '0')
" 2>/dev/null)
  if [ "$DONE" = "1" ]; then echo "test finished after ~$((i*5))s"; break; fi
done
echo "== subscription state =="
curl -s --max-time 10 "$BASE/api/subscriptions" | python3 -c "
import sys, json
subs = json.load(sys.stdin)
m = next((s for s in subs if s['id'] == '$SUB_ID'), None)
print('name:', m['name'], 'nodes:', m['node_count'])
print('last_test:', m.get('last_test'), 'at:', m.get('last_test_at'))
print('featured:', m.get('featured'))
print('convert_ready:', m.get('convert_ready'))
"
echo "== converted YAML =="
curl -s --max-time 10 "$BASE/s/$TOKEN.yaml" -o /tmp/converted-check.yaml -w "http=%{http_code}\n"
python3 -c "
import yaml
d = yaml.safe_load(open('/tmp/converted-check.yaml'))
proxies = d.get('proxies', [])
groups = d.get('proxy-groups', [])
print('proxies:', len(proxies))
for g in groups:
    print('group:', g.get('name'), g.get('type'), 'members:', len(g.get('proxies', [])), g.get('proxies', []))
assert len(proxies) > 0, 'no proxies'
jx = next((g for g in groups if g.get('name') == '精选'), None)
assert jx is not None, 'no 精选 group'
assert len(jx.get('proxies', [])) == 3, '精选 should have 3 nodes'
print('VALID: converted YAML ok, 精选 has 3 nodes')
"
