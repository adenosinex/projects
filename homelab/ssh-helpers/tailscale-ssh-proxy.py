#!/usr/bin/env python3
"""ProxyCommand for SSH over the Tailscale tunnel proxy.
Usage: tailscale-ssh-proxy.py <host> <port>"""
import os, sys, socket, base64, urllib.parse, select

def main():
    host, port = sys.argv[1], int(sys.argv[2])
    proxy = os.environ["HTTPS_PROXY"]
    u = urllib.parse.urlparse(proxy)
    s = socket.create_connection((u.hostname, 3130), timeout=30)
    auth = base64.b64encode(f"{u.username}:{u.password}".encode()).decode()
    req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\nProxy-Authorization: Basic {auth}\r\n\r\n"
    s.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            sys.exit(1)
        resp += chunk
    if b"200" not in resp.split(b"\r\n")[0]:
        sys.stderr.write(resp.decode(errors="ignore"))
        sys.exit(1)
    s.setblocking(False)
    os.set_blocking(0, False)
    out_fd = sys.stdout.fileno()
    while True:
        r, _, _ = select.select([0, s], [], [], 30)
        if not r:
            break
        if 0 in r:
            try:
                data = os.read(0, 8192)
            except BlockingIOError:
                data = b""
            if data:
                s.sendall(data)
            else:
                # stdin EOF
                try:
                    s.shutdown(socket.SHUT_WR)
                except Exception:
                    pass
        if s in r:
            try:
                data = s.recv(8192)
            except BlockingIOError:
                data = b""
            if not data:
                break
            os.write(out_fd, data)

if __name__ == "__main__":
    main()
