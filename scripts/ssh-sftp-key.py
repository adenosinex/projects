#!/usr/bin/env python3
"""SFTP put/get over the Tailscale tunnel proxy using paramiko + local SSH key.
Usage: ssh-sftp-key.py put <local> <remote> | get <remote> <local>"""
import os, sys, socket, base64, urllib.parse
import paramiko

mode, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
host, user = "100.87.118.70", "zin"
key_path = os.path.expanduser("~/.ssh/id_ed25519")
proxy = os.environ["HTTPS_PROXY"]
u = urllib.parse.urlparse(proxy)
s = socket.create_connection((u.hostname, 3130), timeout=30)
auth = base64.b64encode(f"{u.username}:{u.password}".encode()).decode()
req = f"CONNECT {host}:22 HTTP/1.1\r\nHost: {host}:22\r\nProxy-Authorization: Basic {auth}\r\n\r\n"
s.sendall(req.encode())
resp = b""
while b"\r\n\r\n" not in resp:
    chunk = s.recv(4096)
    if not chunk:
        sys.exit(1)
    resp += chunk
assert b"200" in resp.split(b"\r\n")[0], resp
key = paramiko.Ed25519Key.from_private_key_file(key_path)
t = paramiko.Transport(s)
t.connect(username=user, pkey=key)
sftp = paramiko.SFTPClient.from_transport(t)
if mode == "put":
    sftp.put(src, dst)
    print(f"PUT {src} -> {dst}")
elif mode == "get":
    sftp.get(src, dst)
    print(f"GET {src} -> {dst}")
sftp.close()
t.close()
