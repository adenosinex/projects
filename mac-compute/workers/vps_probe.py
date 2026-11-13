"""VPS probe worker: measure a foreign VPS's service accessibility
from China, directly from this Mac (China Mobile broadband).

NEVER routes through mihomo / the subscription proxy nodes -- the
whole point is the direct China -> VPS path.

No file input: the target comes from params. Returns a JSON-able
dict; also writes probe.json into out_dir so the job download zip
contains the result. Missing data stays None, never backfilled.
Only stdlib + system curl/ping, no new dependencies.

Latency method: IPv4 targets use ICMP (system ping) with a TCP
fallback; IPv6 targets use TCP connects only, because system ping6
is broken on this Mac (macOS 26) and no unprivileged process may
send ICMP. The ping object carries a "method" field ("icmp"/"tcp").
"""
import ipaddress
import json
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

_PING_SUMMARY = re.compile(
    r"(\d+) packets transmitted, (\d+) packets received, ([\d.]+)% packet loss")
_PING_RTT = re.compile(
    r"round-trip min/avg/max/stddev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms")
_CURL_TTFB = re.compile(r"code=(\d+) ttfb=([\d.]+)")
_CURL_DL = re.compile(
    r"code=(\d+) size=(\d+) time=([\d.]+) speed=([\d.]+)")


def _parse_target(target):
    """Return (scheme, host, port, path). Raises ValueError on bad input."""
    target = (target or "").strip()
    if not target:
        raise ValueError("target is required")
    if "://" in target:
        u = urlparse(target)
        if not u.hostname:
            raise ValueError(f"cannot parse target: {target}")
        port = u.port or (443 if u.scheme == "https" else 80)
        return u.scheme.lower(), u.hostname, port, u.path or ""
    try:
        ipaddress.ip_address(target)
        return "", target, 80, ""
    except ValueError:
        pass
    if target.startswith("["):
        m = re.match(r"^\[([^\]]+)\](?::(\d+))?$", target)
        if not m:
            raise ValueError(f"cannot parse target: {target}")
        return "", m.group(1), int(m.group(2)) if m.group(2) else 80, ""
    if target.count(":") == 1:
        host, _, port = target.partition(":")
        if not host or not port.isdigit():
            raise ValueError(f"cannot parse target: {target}")
        return "", host, int(port), ""
    if ":" in target:
        try:
            ipaddress.ip_address(target)
            return "", target, 80, ""
        except ValueError:
            raise ValueError(f"cannot parse target: {target}")
    return "", target, 80, ""


def _resolve(host, port):
    """Return (resolved_ip, ip_version) or (None, None)."""
    try:
        ipaddress.ip_address(host)
        return host, ("v6" if ":" in host else "v4")
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError):
        return None, None
    if not infos:
        return None, None
    family, _, _, _, sockaddr = infos[0]
    return sockaddr[0], ("v6" if family == socket.AF_INET6 else "v4")


def _ping(target, count, timeout):
    """Return (ping_dict_or_None, loss_pct_or_None).

    ICMP only. The returned dict has method="icmp".

    macOS quirk (verified 2026-11-10 on this Mac, macOS 26): system
    ping6 is broken -- its getaddrinfo fails for EVERY IPv6 input
    (literal, hostname, even ::1 loopback), so ICMPv6 is untestable
    with the system tools. And macOS blocks non-Apple processes from
    sending ICMP at all (raw sockets need root; SOCK_DGRAM ICMP
    sendto is denied). So this function is only used for IPv4;
    IPv6 latency is measured with TCP connects (_tcp_ping) instead.
    For IPv4, always ping the hostname when we have one (the resolver
    picks an address, here usually IPv4); only ping an IP literal
    when the target itself is one.
    """
    cmd = ["ping", "-c", str(count), "-t", str(timeout), target]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout + count * 2 + 10)
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    text = (out.stdout or "") + (out.stderr or "")
    m = _PING_SUMMARY.search(text)
    loss = float(m.group(3)) if m else None
    received = int(m.group(2)) if m else 0
    ip_m = re.search(r"PING \S+ \(([0-9a-fA-F:.]+)\)", text)
    ping_ip = ip_m.group(1) if ip_m else None
    r = _PING_RTT.search(text)
    if r and received > 0:
        return {
            "method": "icmp",
            "min_ms": round(float(r.group(1)), 1),
            "avg_ms": round(float(r.group(2)), 1),
            "max_ms": round(float(r.group(3)), 1),
            "loss_pct": loss,
            "ip": ping_ip,
        }, loss
    return None, loss


def _tcp_connect(ip, port, timeout):
    """Return TCP connect time in ms, or None."""
    try:
        t0 = time.monotonic()
        with socket.create_connection((ip, port), timeout=timeout):
            pass
        return round((time.monotonic() - t0) * 1000, 1)
    except OSError:
        return None


def _tcp_ping(ip, port, count, timeout):
    """Do `count` TCP connects; return (ping_dict_or_None, loss_pct,
    first_ms_or_None). The ping dict has method="tcp" and the same
    fields as the ICMP one (min/avg/max ms, loss_pct, ip).

    Used for IPv6 targets (system ping6 is broken on this Mac, see
    _ping) and as the fallback when ICMP returns no usable result.
    A TCP connect measures the same network round trip as an ICMP
    echo for latency purposes.
    """
    times = []
    first_ms = None
    for _ in range(count):
        ms = _tcp_connect(ip, port, timeout)
        if ms is not None:
            times.append(ms)
            if first_ms is None:
                first_ms = ms
    if not times:
        return None, 100.0, None
    loss = round((count - len(times)) / count * 100, 1)
    return {
        "method": "tcp",
        "min_ms": round(min(times), 1),
        "avg_ms": round(sum(times) / len(times), 1),
        "max_ms": round(max(times), 1),
        "loss_pct": loss,
        "ip": ip,
    }, loss, first_ms


def _tcp_ping_ports(ip, ports, count, timeout):
    """Try TCP ping on each port in order; return the first usable
    result as (ping_dict, loss_pct, first_ms, used_port), or
    (None, 100.0, None, None) when every port fails."""
    for port in ports:
        ping, loss, first_ms = _tcp_ping(ip, port, count, timeout)
        if ping is not None:
            return ping, loss, first_ms, port
    return None, 100.0, None, None


def _http_ttfb(url, timeout):
    """Return (ttfb_ms_or_None, http_status_or_None).

    -k on purpose: the probe measures network reachability/latency,
    not certificate trust, so a VPS with a self-signed cert still
    yields a TTFB. Any non-000 status counts as a response.
    """
    cmd = ["curl", "-o", "/dev/null", "-s", "-k",
           "-w", "code=%{http_code} ttfb=%{time_starttransfer}",
           "--max-time", str(timeout), "--connect-timeout", str(timeout), url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout + 10)
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    m = _CURL_TTFB.search(out.stdout or "")
    if not m:
        return None, None
    code = int(m.group(1))
    if code == 0:
        return None, None
    return round(float(m.group(2)) * 1000, 1), code


def _download(url, timeout):
    """Download url to /dev/null; return stats dict or None on failure.

    Download gets a longer cap than the per-request timeout
    (max(timeout, 60)s): a slow VPS still yields a valid average
    speed sample from the bytes actually transferred. `complete`
    is False when --max-time cut the transfer short.
    """
    dl_timeout = max(int(timeout), 60)
    cmd = ["curl", "-o", "/dev/null", "-s", "-k",
           "-w", "code=%{http_code} size=%{size_download} "
                 "time=%{time_total} speed=%{speed_download}",
           "--max-time", str(dl_timeout),
           "--connect-timeout", str(timeout), url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=dl_timeout + 10)
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = _CURL_DL.search(out.stdout or "")
    if not m:
        return None
    code = int(m.group(1))
    size = int(m.group(2))
    total = float(m.group(3))
    speed = float(m.group(4))
    if code == 0 or size == 0:
        return None
    return {
        "mbps": round(speed * 8 / 1e6, 2),
        "bytes_actual": size,
        "seconds": round(total, 2),
        "complete": out.returncode == 0,
        "http_status": code,
    }


def _write(out_dir, result):
    try:
        (Path(out_dir) / "probe.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def run_vps_probe(src, out_dir, params, progress_cb):
    # src is None for this task type (no file input); ignored.
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = str(params.get("target") or "").strip()
    ping_count = max(1, min(int(params.get("ping_count") or 4), 20))
    timeout = max(1, min(int(params.get("timeout") or 10), 120))
    download_bytes = max(0, int(params.get("download_bytes") or 10000000))
    download_url = (params.get("download_url") or "").strip() or None

    result = {
        "target": target,
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "reachable": False,
        "ping": None,
        "packet_loss_pct": None,
        "tcp_connect_ms": None,
        "http_ttfb_ms": None,
        "http_status": None,
        "download_mbps": None,
        "download_bytes_requested": download_bytes,
        "download_bytes_actual": None,
        "download_seconds": None,
        "download_complete": None,
        "ip_version": None,
        "resolved_ip": None,
        "error": None,
    }
    if not target:
        result["error"] = "target is required"
        _write(out_dir, result)
        progress_cb(1.0, "done")
        return result

    progress_cb(0.05, "parsing target")
    try:
        scheme, host, port, path = _parse_target(target)
    except ValueError as e:
        result["error"] = str(e)
        _write(out_dir, result)
        progress_cb(1.0, "done")
        return result

    progress_cb(0.15, "resolving")
    resolved_ip, ip_version = _resolve(host, port)
    result["resolved_ip"] = resolved_ip
    result["ip_version"] = ip_version
    failures = []
    if resolved_ip is None:
        failures.append("DNS resolution failed")
    else:
        progress_cb(0.3, "ping")
        # Port list for TCP latency: URL targets use their own port;
        # bare IP/host targets try 80, then 443 (a bare IP may only
        # serve HTTPS).
        if scheme in ("http", "https"):
            tcp_ports = [port]
        else:
            tcp_ports = [80, 443] if port == 80 else [port]
        if ip_version == "v6":
            # IPv6 latency is TCP-based: system ping6 is broken on
            # this Mac and no unprivileged process may send ICMP.
            # No ICMP is attempted here.
            ping, loss, first_ms, _used = _tcp_ping_ports(
                resolved_ip, tcp_ports, ping_count, timeout)
            if ping is not None:
                result["ping"] = ping
                result["packet_loss_pct"] = loss
                result["tcp_connect_ms"] = first_ms
            else:
                result["packet_loss_pct"] = loss
                failures.append("TCP latency probe failed")
        else:
            # IPv4: ICMP first (system ping works), TCP fallback.
            # Ping the hostname, not the resolved IP; TCP below uses
            # the resolved IP, so the two may differ in address family.
            ping, loss = _ping(host, ping_count, timeout)
            if ping is not None:
                result["ping"] = ping
                result["packet_loss_pct"] = loss
                progress_cb(0.5, "tcp connect")
                tcp_ms = _tcp_connect(resolved_ip, port, timeout)
                result["tcp_connect_ms"] = tcp_ms
                if tcp_ms is None:
                    failures.append(f"TCP connect to port {port} failed")
            else:
                failures.append("ICMP ping got no reply")
                ping, loss, first_ms, _used = _tcp_ping_ports(
                    resolved_ip, tcp_ports, ping_count, timeout)
                if ping is not None:
                    result["ping"] = ping
                    result["packet_loss_pct"] = loss
                    result["tcp_connect_ms"] = first_ms
                else:
                    result["packet_loss_pct"] = loss
                    failures.append("TCP latency probe failed")

    is_http = scheme in ("http", "https")
    if is_http and resolved_ip is not None:
        progress_cb(0.7, "http ttfb")
        ttfb_ms, status = _http_ttfb(target, timeout)
        result["http_ttfb_ms"] = ttfb_ms
        result["http_status"] = status
        if ttfb_ms is None:
            failures.append("HTTP request failed")

    # Download only when there is a real resource to fetch: an explicit
    # download_url, or a target URL with a path beyond "/". A bare
    # IP/host has no file to download; that test is skipped, not faked.
    dl_url = download_url or (target if (is_http and path and path != "/") else None)
    if dl_url:
        progress_cb(0.85, "download test")
        dl = _download(dl_url, timeout)
        if dl:
            result["download_mbps"] = dl["mbps"]
            result["download_bytes_actual"] = dl["bytes_actual"]
            result["download_seconds"] = dl["seconds"]
            result["download_complete"] = dl["complete"]
            if result["http_status"] is None:
                result["http_status"] = dl["http_status"]
        else:
            failures.append("download test failed")

    result["reachable"] = any([
        result["ping"] is not None,
        result["tcp_connect_ms"] is not None,
        result["http_ttfb_ms"] is not None,
        result["download_mbps"] is not None,
    ])
    if not result["reachable"]:
        result["error"] = "; ".join(failures) if failures else "target unreachable"
    progress_cb(1.0, "done")
    _write(out_dir, result)
    return result
