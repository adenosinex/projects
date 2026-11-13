#!/opt/homebrew/bin/python3
"""Create the Cloudflare AAAA record for cpl.zin7.de -> Mac live IPv6.
Reads credentials from /Users/zin/mac-compute/cf-zin7.env at runtime.
Never prints the token. Idempotent: no-ops if the record already exists."""
import ipaddress
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ENV_FILE = Path("/Users/zin/mac-compute/cf-zin7.env")
DOMAIN = "cpl.zin7.de"
CF_API = "https://api.cloudflare.com/client/v4"


def load_env():
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def live_ipv6():
    out = subprocess.run(["/sbin/ifconfig", "en0"], capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise RuntimeError("ifconfig en0 failed")
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line.startswith("inet6 "):
            continue
        parts = line.split()
        addr = parts[1]
        if addr.startswith("fe80"):
            continue
        if "temporary" in line or "deprecated" in line:
            continue
        try:
            ip = ipaddress.IPv6Address(addr)
        except ValueError:
            continue
        if ip.is_global:
            return str(ip)
    raise RuntimeError("no global IPv6 on en0")


def cf(env, method, endpoint, payload=None):
    req = urllib.request.Request(
        CF_API + endpoint, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": "Bearer " + env["CF_API_TOKEN"],
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    if not body.get("success"):
        raise RuntimeError("Cloudflare API error: %s" % body.get("errors"))
    return body


def main():
    env = load_env()
    if not env.get("CF_API_TOKEN") or not env.get("CF_ZONE_ID"):
        print("ERROR: credentials missing from cf-zin7.env")
        return 2
    zone = env["CF_ZONE_ID"]
    current = live_ipv6()
    print("live IPv6:", current)
    body = cf(env, "GET", "/zones/%s/dns_records?type=AAAA&name=%s&per_page=5" % (zone, DOMAIN))
    records = body.get("result") or []
    if records:
        rec = records[0]
        print("record already exists: id=%s content=%s proxied=%s" % (rec["id"], rec.get("content"), rec.get("proxied")))
        if rec.get("content") != current:
            print("content differs; ddns-zin7.py will update it on its next run once the domain is added to DOMAINS")
        return 0
    payload = {"type": "AAAA", "name": DOMAIN, "content": current, "ttl": 300, "proxied": False}
    body = cf(env, "POST", "/zones/%s/dns_records" % zone, payload)
    rec = body["result"]
    print("created AAAA record: id=%s name=%s content=%s" % (rec["id"], rec["name"], rec["content"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
