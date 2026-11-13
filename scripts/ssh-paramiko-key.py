#!/usr/bin/env python3
"""SSH via Tailscale tunnel proxy using paramiko + local SSH key (no password).
Usage: ssh-paramiko-key.py <host> <user> <remote-command>"""
import os, sys, socket, base64, urllib.parse
import paramiko

host, user, cmd = sys.argv[1], sys.argv[2], " ".join(sys.argv[3:])
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
        print("PROXY_CONNECT_FAILED", file=sys.stderr); sys.exit(1)
    resp += chunk
if b"200" not in resp.split(b"\r\n")[0]:
    print("PROXY:", resp.decode(errors="ignore"), file=sys.stderr); sys.exit(1)

key = paramiko.Ed25519Key.from_private_key_file(key_path)
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    client.connect(host, username=user, pkey=key, sock=s, timeout=20, banner_timeout=20, auth_timeout=20, allow_agent=False, look_for_keys=False)
except Exception as e:
    print(f"SSH_AUTH_FAILED: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(3)
print("CONNECTED as", user, "on", host)
stdin, stdout, stderr = client.exec_command(cmd)
out = stdout.read().decode(errors="ignore")
err = stderr.read().decode(errors="ignore")
print(out, end="")
if err:
    print("STDERR:", err, file=sys.stderr)
print("EXIT:", stdout.channel.recv_exit_status())
client.close()
