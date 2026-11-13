#!/usr/bin/env python3
"""dohrelay domestic latency probe (runs on the domestic machine).

Architecture (user ruling 2026-11-11 15:17): Shock is the only
user-facing resolver; this machine does NOT serve DNS. It only
MEASURES, from inside China, how fast/reachable the candidate IPs
of the curated domain set are, and pushes the measurements to
Shock, where the gate uses them as the ranking basis for the
answers it hands to user devices.

One run:
  1. fetch the candidate set: Shock gate answers for each curated
     domain (its truth source), collected as A+AAAA answer IPs.
  2. for every (domain, ip): tcp:443 connect latency + full TLS
     handshake with SNI = the domain; record reachable / latency /
     whether the presented certificate validates for the domain
     (a poisoned candidate often completes TCP but fails this).
  3. push {version, generated_at, results} to Shock:
     POST https://shoc.zin7.de/<SEG>/probe-ingest
     header X-Probe-Key: <probe key>
     (the same credential path segment as the DoH endpoint).

Zero inbound ports, zero certificates of its own, stdlib only.
Also runnable as: probe.py --dry-run  (measure + print JSON,
no push) for installation self-checks.

Config: /etc/dohrelay-probe/config.json {ingest_base, probe_key}
  ingest_base = https://shoc.zin7.de/<SEG>   (no trailing slash)
  probe_key   = the dedicated probe key (32 hex chars)
Both values are credentials: the config file is 0600 and neither
value is ever printed by this script.
"""
import argparse
import concurrent.futures
import json
import os
import socket
import ssl
import sys
import time
import urllib.request

CONF = "/etc/dohrelay-probe/config.json"
CANDIDATES = "/etc/dohrelay-probe/candidates.json"
FALLBACK_DOH = "https://shoc.zin7.de"
CONNECT_TIMEOUT = 4.0
WORKERS = 12


def b64url(data):
    import base64
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def doh_query(base, name, qtype):
    """One RFC 8484 GET query; returns list of answer IPs (strings)."""
    import struct
    tid = int.from_bytes(os.urandom(2), "big")
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode()
                     for p in name.split(".")) + b"\x00"
    wire = header + qname + struct.pack(">HH", qtype, 1)
    url = base.rstrip("/") + "/dns-query?dns=" + b64url(wire)
    req = urllib.request.Request(
        url, headers={"Accept": "application/dns-message"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = resp.read()
    _t, _f, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    off = 12
    for _ in range(qd):
        while off < len(data) and data[off] != 0:
            off += data[off] + 1
        off += 5
    out = []
    for _ in range(an):
        if data[off] & 0xC0 == 0xC0:
            off += 2
        else:
            while off < len(data) and data[off] != 0:
                off += data[off] + 1
            off += 1
        rtype, _c, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        rdata = data[off:off + rdlen]
        off += rdlen
        if rtype == 1 and rdlen == 4:
            out.append(".".join(str(b) for b in rdata))
        elif rtype == 28 and rdlen == 16:
            import ipaddress
            out.append(str(ipaddress.IPv6Address(rdata)))
    return out


def measure(domain, ip):
    """tcp:443 connect + TLS handshake with SNI=domain."""
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    rec = {"reachable": False, "latency_ms": None, "cert_ok": False}
    started = time.time()
    try:
        raw = socket.socket(fam, socket.SOCK_STREAM)
        raw.settimeout(CONNECT_TIMEOUT)
        raw.connect((ip, 443))
    except Exception:
        return rec
    rec["reachable"] = True
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_default_certs()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        tls = ctx.wrap_socket(raw, server_hostname=domain)
        tls.close()
        rec["cert_ok"] = True
    except Exception:
        rec["cert_ok"] = False
        try:
            raw.close()
        except Exception:
            pass
    rec["latency_ms"] = int((time.time() - started) * 1000)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="measure and print JSON; do not push")
    ap.add_argument("--config", default=CONF)
    ap.add_argument("--candidates", default=CANDIDATES)
    args = ap.parse_args()

    with open(args.candidates, encoding="utf-8") as fh:
        domains = json.load(fh)["domains"]

    ingest_base = None
    probe_key = None
    truth_base = None
    try:
        with open(args.config, encoding="utf-8") as fh:
            conf = json.load(fh)
        ingest_base = conf["ingest_base"].rstrip("/")
        probe_key = conf["probe_key"]
        truth_base = ingest_base
    except Exception:
        if not args.dry_run:
            raise
        truth_base = FALLBACK_DOH

    results = {}
    cand = {}
    for name in domains:
        ips = []
        for qtype in (1, 28):
            try:
                ips += doh_query(truth_base, name, qtype)
            except Exception:
                pass
        cand[name] = sorted(set(ips))
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=WORKERS) as ex:
        futs = {}
        for name, ips in cand.items():
            results[name] = {"ips": {}}
            for ip in ips:
                futs[ex.submit(measure, name, ip)] = (name, ip)
        for fut in concurrent.futures.as_completed(futs):
            name, ip = futs[fut]
            try:
                results[name]["ips"][ip] = fut.result()
            except Exception:
                results[name]["ips"][ip] = {
                    "reachable": False, "latency_ms": None,
                    "cert_ok": False}

    payload = {"version": 1,
               "generated_at": time.strftime(
                   "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "results": results}
    if args.dry_run:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
        return 0

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        ingest_base + "/probe-ingest", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Probe-Key": probe_key})
    with urllib.request.urlopen(req, timeout=20) as resp:
        out = json.loads(resp.read().decode())
    if not out.get("ok"):
        sys.stderr.write("ingest rejected\n")
        return 1
    print("pushed: %d domains at %s" % (out.get("domains", 0),
                                        payload["generated_at"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
