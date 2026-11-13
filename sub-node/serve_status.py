#!/opt/homebrew/bin/python3
# serve_status.py — 对外提供 Mac 节点测试信息与 Mac 节点订阅（HTTPS）。
# 只提供 ~/sub-node/status/ 下的文件；文件名含 token（token 即凭据，防猜测）；
# 禁目录列表；双栈监听（局域网 IPv4 + 公网 IPv6），端口 18932。
# TLS 证书用 mac-compute 的 Let's Encrypt 证书（apple.su7.dpdns.org，有效期至 2027-02-05，
# acme.sh 自动续期）；每次连接都重新读证书文件，所以续期后无需重启服务。
# 注意：证书只对 apple.su7.dpdns.org 有效；用局域网 IP（192.168.31.197）访问时会报
# 证书域名不匹配——局域网内也请用域名访问，或让客户端跳过证书校验。
# 订阅文件里含 AnyTLS 节点密码，只通过 token 链接分享，不要公开转发。
import os
import socket
import ssl
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.expanduser('~/sub-node/status')
PORT = 18932
CERT = os.path.expanduser('~/mac-compute/certs/apple.su7.dpdns.org.fullchain.crt')
KEY = os.path.expanduser('~/mac-compute/certs/apple.su7.dpdns.org.key')


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        '.yaml': 'text/yaml; charset=utf-8',
        '.yml': 'text/yaml; charset=utf-8',
        '.json': 'application/json; charset=utf-8',
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def list_directory(self, path):
        self.send_error(404, 'Not found')


class DualStackServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

    def get_request(self):
        # 每次连接重新读证书：续期后无需重启服务。
        sock, addr = super().get_request()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT, KEY)
        return ctx.wrap_socket(sock, server_side=True), addr


if __name__ == '__main__':
    os.makedirs(ROOT, exist_ok=True)
    DualStackServer(('::', PORT), Handler).serve_forever()
