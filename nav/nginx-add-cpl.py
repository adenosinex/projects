#!/opt/homebrew/bin/python3
"""Add cpl.zin7.de server block to /opt/homebrew/etc/nginx/status-443.conf.
Backs up first. Idempotent: no-ops if the block already exists."""
import shutil
from pathlib import Path

CONF = Path("/opt/homebrew/etc/nginx/status-443.conf")
BACKUP = Path("/opt/homebrew/etc/nginx/status-443.conf.bak-20261112-cpl")

CPL_BLOCK = """    # Nav page: https://cpl.zin7.de/ — service directory, login = cp credentials.
    # Proxies / to the nav FastAPI app on 127.0.0.1:18936 (HTTP loopback).
    # /proxy-test/ and /api/ forward to the proxy-test app on 127.0.0.1:18935,
    # gated by nginx auth_request against the nav app's /auth-check.
    server {
        listen 443 ssl;
        listen [::]:443 ssl;
        server_name cpl.zin7.de;

        ssl_certificate /Users/zin/mac-compute/certs/cpl.zin7.de.fullchain.crt;
        ssl_certificate_key /Users/zin/mac-compute/certs/cpl.zin7.de.key;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_prefer_server_ciphers on;
        client_max_body_size 100M;

        error_page 401 = @login_redirect;
        location @login_redirect {
            return 302 /login?next=$request_uri;
        }

        location = /auth-check {
            internal;
            proxy_pass http://127.0.0.1:18936/auth-check;
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
            proxy_set_header Host $host;
        }

        location / {
            proxy_pass http://127.0.0.1:18936;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_read_timeout 300;
        }

        location /proxy-test/ {
            auth_request /auth-check;
            proxy_pass http://127.0.0.1:18935/;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_read_timeout 300;
        }

        location /api/ {
            auth_request /auth-check;
            proxy_pass http://127.0.0.1:18935/api/;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_http_version 1.1;
            proxy_read_timeout 300;
        }
    }
"""

def main():
    text = CONF.read_text()
    if "server_name cpl.zin7.de;" in text:
        print("cpl block already present, no change")
        return 0
    shutil.copy2(CONF, BACKUP)
    print("backup:", BACKUP)
    # add cpl to the port-80 redirect list
    old80 = "server_name cp.zin7.de ha.zin7.de;"
    new80 = "server_name cp.zin7.de ha.zin7.de cpl.zin7.de;"
    assert old80 in text, "port-80 server_name line not found"
    text = text.replace(old80, new80, 1)
    # insert cpl block before the final closing brace of the http block
    assert text.rstrip().endswith("}"), "config does not end with }"
    stripped = text.rstrip()
    assert stripped.endswith("    }\n}"), "unexpected config tail"
    text = stripped[:-len("    }\n}")] + "    }\n\n" + CPL_BLOCK + "}\n"
    CONF.write_text(text)
    print("cpl block added; server_name lines:")
    for ln in text.splitlines():
        if "server_name" in ln:
            print(" ", ln.strip())
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
