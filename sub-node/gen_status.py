#!/opt/homebrew/bin/python3
# gen_status.py — 从 mihomo 控制器 API 生成对外发布的测试信息（JSON + HTML）。
# 输出到 ~/sub-node/status/<token>.json 与 <token>.html；token 存在 publish.token（600）。
# 只含节点名、延迟与状态，不含任何密码与订阅链接。
import json
import os
import re
import socket
import subprocess
import urllib.request
from datetime import datetime

HOME = os.path.expanduser('~')
D = os.path.join(HOME, 'sub-node')
STATUS_DIR = os.path.join(D, 'status')


def read(name):
    with open(os.path.join(D, name)) as f:
        return f.read().strip()


def api(path):
    req = urllib.request.Request(
        'http://127.0.0.1:9090' + path,
        headers={'Authorization': 'Bearer ' + read('controller.secret')},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def main():
    token = read('publish.token')
    os.makedirs(STATUS_DIR, exist_ok=True)
    now = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')
    # 选中节点
    try:
        proxies = api('/proxies')
        selected = proxies.get('proxies', {}).get('🚀 节点选择', {}).get('now', '?')
    except Exception:
        selected = '?'
    # 各节点延迟
    nodes = []
    try:
        prov = api('/providers/proxies/sub')
        for p in prov.get('proxies', []):
            hist = p.get('history', [])
            delay = hist[-1].get('delay') if hist else None
            if delay is None:
                status, delay_ms = '未测', None
            elif delay == 0:
                status, delay_ms = '不通', 0
            else:
                status, delay_ms = '正常', delay
            nodes.append({'name': p.get('name'), 'delay_ms': delay_ms, 'status': status})
    except Exception:
        pass
    # 各节点下载速度（test_speed.sh 每 30 分钟写一次 speed.json；按名字合并）
    # 缺数据留空不回填旧值：speed.json 超过 40 分钟未更新视为过期，速度全部留空。
    speed_map = {}
    speed_updated = None
    try:
        with open(os.path.join(D, 'speed.json')) as f:
            sp = json.load(f)
        speed_updated = sp.get('updated')
        fresh = False
        if speed_updated:
            try:
                ts = datetime.strptime(speed_updated.rsplit(' ', 1)[0], '%Y-%m-%d %H:%M:%S')
                fresh = (datetime.now() - ts).total_seconds() <= 2400
            except Exception:
                fresh = False
        if fresh:
            for s in sp.get('nodes', []):
                speed_map[s.get('name')] = s.get('speed_mbps')
        else:
            speed_updated = None
    except Exception:
        pass
    for n in nodes:
        n['speed_mbps'] = speed_map.get(n['name'])
    # chain 节点：定义是“先经 debi 前置再出”，单节点直测没带前置必然不通——
    # 这是探针形态测不了，不是节点故障。延迟和速度都留空，状态单独标注，不和真不通混在一起。
    for n in nodes:
        if 'chain' in (n.get('name') or '').lower():
            n['status'] = '探针形态测不了'
            n['delay_ms'] = None
            n['speed_mbps'] = None
    # 订阅端点
    sub_host, sub_http = '?', None
    try:
        cfg = open(os.path.join(D, 'config.yaml')).read()
        m = re.search(r'url: "(https?://[^"]+)"', cfg)
        if m:
            url = m.group(1)
            sub_host = re.sub(r'https://([^/]+)/.*', r'\1', url)
            req = urllib.request.Request(url, method='HEAD')
            with urllib.request.urlopen(req, timeout=12) as r:
                sub_http = r.status
    except Exception:
        pass
    # Mac 节点监听（4 个：AnyTLS/Hysteria2 × IPv4/IPv6）
    lan_ip = subprocess.run(['ipconfig', 'getifaddr', 'en0'],
                            capture_output=True, text=True).stdout.strip()
    ipv6 = ''
    try:
        # IPv6 从 Mac 订阅文件读（config.yaml 里没有 mac-AnyTLS-IPv6 这个节点名）
        token0 = read('publish.token')
        sub = open(os.path.join(STATUS_DIR, token0 + '.yaml')).read()
        m6 = re.search(r'mac-AnyTLS-IPv6.*?server: "([^"]+)"', sub, re.S)
        if m6:
            ipv6 = m6.group(1)
    except Exception:
        pass

    def tcp_ok(host, port):
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except Exception:
            return False

    def udp_bound(ip, port):
        # Hysteria2 是 UDP：看 lsof 里有没有绑这个端口（IPv6 在 lsof 里是 [地址]:端口 形式）
        try:
            r = subprocess.run(['lsof', '-nP', f'-iUDP:{port}'],
                               capture_output=True, text=True)
            for line in r.stdout.splitlines():
                if f'{ip}:{port}' in line:
                    return True
                if f'[{ip}]:{port}' in line:
                    return True
                if ip == '::' and f'*:{port}' in line:
                    return True
            return False
        except Exception:
            return False

    mac_nodes = [
        {'name': 'mac-AnyTLS-IPv4', 'protocol': 'anytls', 'stack': 'ipv4',
         'address': f'{lan_ip}:8443', 'ok': tcp_ok(lan_ip, 8443)},
        {'name': 'mac-AnyTLS-IPv6', 'protocol': 'anytls', 'stack': 'ipv6',
         'address': f'[{ipv6}]:8443', 'ok': tcp_ok(ipv6, 8443) if ipv6 else False},
        {'name': 'mac-Hysteria2-IPv4', 'protocol': 'hysteria2', 'stack': 'ipv4',
         'address': f'{lan_ip}:8444', 'ok': udp_bound(lan_ip, 8444)},
        {'name': 'mac-Hysteria2-IPv6', 'protocol': 'hysteria2', 'stack': 'ipv6',
         'address': f'[{ipv6}]:8444', 'ok': udp_bound(ipv6, 8444) if ipv6 else False},
    ]
    anytls_ok = mac_nodes[0]['ok']
    anytls_addr = mac_nodes[0]['address']
    data = {
        'updated': now,
        'selected': selected,
        'nodes': nodes,
        'speed_updated': speed_updated,
        'subscription_endpoint': {'host': sub_host, 'http': sub_http},
        'anytls_listener': {'address': anytls_addr, 'ok': anytls_ok},
        'mac_nodes': mac_nodes,
    }
    with open(os.path.join(STATUS_DIR, token + '.json'), 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    rows = ''.join(
        '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
            n['name'],
            n['delay_ms'] if n['delay_ms'] is not None else '—',
            n['speed_mbps'] if n.get('speed_mbps') is not None else '—',
            n['status'],
        )
        for n in nodes
    )
    mac_rows = ''.join(
        '<tr><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
            m['name'], m['address'], '正常' if m['ok'] else '异常',
        )
        for m in mac_nodes
    )
    html = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="300">'
        '<title>Mac 节点测试信息</title>'
        '<style>body{font-family:sans-serif;max-width:640px;margin:2em auto;padding:0 1em}'
        'table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px 10px;text-align:left}</style>'
        '</head><body>'
        '<h2>Mac 节点测试信息</h2>'
        f'<p>更新：{now}（每 5 分钟自动刷新）</p>'
        f'<p>当前选中：<b>{selected}</b></p>'
        f'<p>订阅端点：{sub_host}（HTTP {sub_http}）</p>'
        '<h3>Mac 节点（双栈 × 双协议）</h3>'
        '<table><tr><th>节点</th><th>地址</th><th>状态</th></tr>'
        + mac_rows +
        '</table>'
        '<h3>订阅节点</h3>'
        '<table><tr><th>节点</th><th>延迟 ms</th><th>速度 Mbps</th><th>状态</th></tr>'
        + rows +
        '</table></body></html>'
    )
    with open(os.path.join(STATUS_DIR, token + '.html'), 'w') as f:
        f.write(html)
    print(f'状态已写：{len(nodes)} 个节点，选中 {selected}')


if __name__ == '__main__':
    main()
