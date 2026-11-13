#!/opt/homebrew/bin/python3
"""DDNS for cp.zin7.de and status.mac.zin7.de: keep the Cloudflare AAAA records pointed at this Mac's
current public IPv6. Mirrors my_web_center dns_extension cf_upsert behavior:
read current record, no-op if it already matches, PUT only when it changed.

Token: ~/mac-compute/cf-zin7.env (600), CF_API_TOKEN + CF_ZONE_ID.
No token is ever printed or logged. Runs from cron every 10 minutes.
Usage: ddns-zin7.py [--check]   (--check = dry run, prints what it would do)
"""
import ipaddress
import json
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ENV_FILE = Path("/Users/zin/mac-compute/cf-zin7.env")
LOG_FILE = Path("/Users/zin/mac-compute/ddns-zin7.log")
DOMAINS = ["cp.zin7.de", "status.mac.zin7.de", "ha.zin7.de", "cpl.zin7.de"]
RECORD_TYPE = "AAAA"
CF_API = "https://api.cloudflare.com/client/v4"


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts} {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


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
    """Current public IPv6 from en0: non-temporary, non-deprecated, global."""
    out = subprocess.run(["/sbin/ifconfig", "en0"], capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise RuntimeError("ifconfig en0 failed")
    cands = []
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
            cands.append(str(ip))
    if not cands:
        raise RuntimeError("no global IPv6 found on en0")
    return cands[0]


def cf_request(env, method, endpoint, payload=None):
    req = urllib.request.Request(
        CF_API + endpoint,
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {env['CF_API_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    if not body.get("success"):
        raise RuntimeError(f"Cloudflare API error: {body.get('errors')}")
    return body


def main():
    dry = "--check" in sys.argv
    env = load_env()
    if not env.get("CF_API_TOKEN") or not env.get("CF_ZONE_ID"):
        log("ERROR: CF_API_TOKEN/CF_ZONE_ID missing from cf-zin7.env")
        return 2
    try:
        current = live_ipv6()
    except Exception as e:
        log(f"ERROR: cannot determine live IPv6: {e}")
        return 2
    zone = env["CF_ZONE_ID"]
    exit_code = 0
    for domain in DOMAINS:
        try:
            body = cf_request(env, "GET",
                              f"/zones/{zone}/dns_records?type={RECORD_TYPE}&name={domain}&per_page=5")
        except Exception as e:
            log(f"ERROR: Cloudflare GET failed for {domain}: {e}")
            exit_code = 2
            continue
        records = body.get("result") or []
        if not records:
            log(f"ERROR: no {RECORD_TYPE} record found for {domain}")
            exit_code = 2
            continue
        rec = records[0]
        remote = rec.get("content", "")
        if remote == current and rec.get("proxied") is False:
            log(f"OK no change: {domain} already {current}")
            continue
        log(f"CHANGE: {domain} {remote} -> {current} (dry_run={dry})")
        if dry:
            continue
        payload = {"type": RECORD_TYPE, "name": domain, "content": current,
                   "ttl": 300, "proxied": False}
        try:
            cf_request(env, "PUT", f"/zones/{zone}/dns_records/{rec['id']}", payload)
        except Exception as e:
            log(f"ERROR: Cloudflare PUT failed for {domain}: {e}")
            exit_code = 2
            continue
        log(f"OK updated: {domain} -> {current}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
