#!/opt/homebrew/bin/python3
"""Router data collector for Redmi RM2100 (Padavan) -> ~/router-status/data.json.

Polls the router over SSH every 10s using the Mac's `ssh router` alias
(key auth, ControlMaster multiplexed). ONE SSH call per poll runs a
combined script that reads /proc directly (no per-command processes).

Collects:
  - per-interface RX/TX bytes via `ip -s link` (Mbps computed from deltas)
  - WAN IPv4/gateway/DNS via `ip addr` + `ip route` + nvram wan_dns*_x
    (nvram wan_ipaddr/wan_gateway are stale 0.0.0.0 -- never use them)
  - connection tracking count/max via /proc/sys/net/netfilter/
  - device list via /proc/net/arp (flags: 0x2 = live, 0x0 = stale)
    + /tmp/dnsmasq.leases; eth3 ARP entries are WAN-side UPSTREAM, not clients
  - WiFi association + RSSI via `iwpriv ra0/rai0 show stainfo`, whose
    output goes to the kernel log: isolate with `dmesg -c` before each call
    and read the fresh dump from `dmesg` (deterministic parsing)
  - system stats via /proc/loadavg, /proc/meminfo, /proc/uptime.
    Old kernel has no MemAvailable: available ~= MemFree+Buffers+Cached.
  - per-client DNS queries/blocked from AdGuard Home on the Mac
    (http://127.0.0.1:3003, Basic auth from adguard.auth mode 600):
    /control/stats every poll (24h totals), /control/querylog once a
    minute (blocked per client within the last 2000 log entries).
  - display names from aliases.json (MAC exact, MAC prefix, IP).
  - last-seen timestamps for offline devices in /tmp (never Flash).

Writes the latest snapshot atomically to data.json next to this script.
On router/SSH errors the last good data is kept, the error is logged,
and polling continues.

Stdlib only. Run under LaunchAgent org.zin.router-status-collector.
"""
import base64
import datetime
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "data.json")
ALIASES_FILE = os.path.join(HERE, "aliases.json")
ADGUARD_AUTH_FILE = os.path.join(HERE, "adguard.auth")
LASTSEEN_FILE = "/tmp/router-status-lastseen.json"
ADGUARD_BASE = "http://127.0.0.1:3003"
POLL_INTERVAL = 10
HISTORY_LEN = 60
SSH_TIMEOUT = 15
ADGUARD_QUERYLOG_EVERY = 6  # polls (~once a minute)
ADGUARD_QUERYLOG_LIMIT = 2000
LASTSEEN_KEEP_DAYS = 7

# Interfaces we care about (Padavan on RM2100). Anything else found by
# `ip -s link` is still reported, but these get friendly labels and are
# shown first, in this fixed order (unknown interfaces rank last).
LABELS = {
    "eth3": "WAN",
    "br0": "LAN 桥",
    "eth2": "有线",
    "ra0": "WiFi 2.4G",
    "rai0": "WiFi 5G",
}
ORDER = ["eth3", "br0", "eth2", "ra0", "rai0"]
HISTORY_IFACES = ORDER
LAN_MEMBERS = ["eth2", "ra0", "rai0"]  # children of the br0 bridge

# One SSH call per poll. Markers are unique full lines; the stainfo dumps
# go to the kernel log, so each `iwpriv ... show stainfo` is preceded by
# `dmesg -c` and followed by a plain `dmesg` that holds only that dump.
CMD_COMBINED = """ip -s link
echo ---ARP---
cat /proc/net/arp
echo ---LEASES---
cat /tmp/dnsmasq.leases
echo ---SYS---
cat /proc/loadavg
echo ---MEM---
cat /proc/meminfo
echo ---UPTIME---
cat /proc/uptime
echo ---WANADDR---
ip addr show eth3
echo ---ROUTE---
ip route show
echo ---CONN---
cat /proc/sys/net/netfilter/nf_conntrack_count
cat /proc/sys/net/netfilter/nf_conntrack_max
echo ---WANDNS---
/usr/sbin/nvram get wan_dns1_x
/usr/sbin/nvram get wan_dns2_x
/usr/sbin/nvram get wan_dns3_x
echo ---DMESG-RA0---
dmesg -c >/dev/null 2>&1; iwpriv ra0 show stainfo; dmesg
echo ---DMESG-RAI0---
dmesg -c >/dev/null 2>&1; iwpriv rai0 show stainfo; dmesg"""


def log(msg):
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)


def run_router(cmd):
    """Run one command on the router via the `ssh router` alias.

    BatchMode avoids any interactive password prompt hanging the poll.
    Returns stdout (str). Raises on SSH failure / timeout / non-zero exit.
    """
    proc = subprocess.run(
        ["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         "router", cmd],
        capture_output=True, text=True, timeout=SSH_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ssh exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
    return proc.stdout


def split_sections(text, markers):
    """Split `cmd1; echo MARKER; cmd2` output into {marker: text}."""
    sections = {}
    current = None
    buf = []
    for line in text.splitlines():
        if line.strip() in markers:
            if current is not None:
                sections[current] = "\n".join(buf)
            current = line.strip()
            buf = []
        else:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf)
    return sections


def parse_ip_link(text):
    """Parse `ip -s link` -> {iface: {rx_bytes, tx_bytes, up}}."""
    ifaces = {}
    current = None
    expect = None  # 'rx' or 'tx': next line holds the counters
    for line in text.splitlines():
        m = re.match(r"^\d+:\s+([^:@\s]+)[@:]", line)
        if m:
            current = m.group(1)
            up = ",UP," in line or line.rstrip().endswith(",UP>")
            ifaces[current] = {"rx_bytes": 0, "tx_bytes": 0, "up": bool(up)}
            expect = None
            continue
        if current is None:
            continue
        s = line.strip()
        if s.startswith("RX:"):
            expect = "rx"
            continue
        if s.startswith("TX:"):
            expect = "tx"
            continue
        if expect in ("rx", "tx"):
            parts = s.split()
            if parts and parts[0].isdigit():
                ifaces[current][f"{expect}_bytes"] = int(parts[0])
            expect = None
    return ifaces


def parse_stainfo(dmesg_text):
    """Parse one isolated stainfo dump (after `dmesg -c`) -> {MAC: rssi}.

    Rows start with a MAC; the RSSI field is the first token that looks
    like `-54/-44/0` (slash-separated ints). Plain ints (MODE/AID/...) are
    skipped because the pattern requires at least one '/'.
    """
    stations = {}
    mac_re = re.compile(r"^([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\s+(.*)$")
    rssi_re = re.compile(r"^(-?\d+)(?:/-?\d+)+$")
    for line in dmesg_text.splitlines():
        m = mac_re.match(line.strip())
        if not m:
            continue
        mac = m.group(1).upper()
        rssi = None
        for tok in m.group(2).split():
            rm = rssi_re.match(tok)
            if rm:
                rssi = int(rm.group(1))
                break
        stations[mac] = rssi
    return stations


def parse_arp(text):
    """Parse /proc/net/arp -> {mac: {ip, ips, iface, flags}}.

    Flags 0x2 (ATF_COM) = complete/live entry; 0x0 = stale/incomplete.
    One MAC can hold several IPs (same device, several leases).
    """
    by_mac = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[0] == "IP":
            continue
        ip, flags, mac, iface = parts[0], parts[2], parts[3].upper(), parts[5]
        if mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
            continue
        if not re.match(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$", mac):
            continue
        try:
            flags_int = int(flags, 16)
        except ValueError:
            flags_int = 0
        entry = by_mac.setdefault(mac, {"ips": [], "iface": iface,
                                        "flags": flags_int})
        if ip not in entry["ips"]:
            entry["ips"].append(ip)
        entry["iface"] = iface
        # keep the strongest flags seen (live beats stale)
        entry["flags"] = max(entry["flags"], flags_int)
    return by_mac


def parse_leases(text):
    """Parse /tmp/dnsmasq.leases -> {mac: {ip, hostname}} (last wins)."""
    leases = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        _expiry, mac, ip, hostname = parts[0], parts[1].upper(), parts[2], parts[3]
        if not re.match(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$", mac):
            continue
        leases[mac] = {"ip": ip,
                       "hostname": hostname if hostname != "*" else ""}
    return leases


def is_random_mac(mac):
    """Locally-administered (random/private) MAC: 2nd hex digit of the
    first octet is 2, 6, A or E (multicast bit clear, local bit set)."""
    try:
        second = mac[1].upper()
    except IndexError:
        return False
    return second in ("2", "6", "A", "E")


def ip_key(ip):
    try:
        return tuple(int(p) for p in ip.split("."))
    except ValueError:
        return (999, 999, 999, 999)


def format_last_seen(epoch):
    """'今天 HH:MM' / '昨天 HH:MM' / 'MM-DD HH:MM' (local time)."""
    if not epoch:
        return None
    dt = datetime.datetime.fromtimestamp(epoch)
    now = datetime.datetime.now()
    hm = dt.strftime("%H:%M")
    if dt.date() == now.date():
        return f"今天 {hm}"
    if dt.date() == (now - datetime.timedelta(days=1)).date():
        return f"昨天 {hm}"
    return dt.strftime("%m-%d %H:%M")


def load_aliases():
    """aliases.json: {"by_mac": {...}, "by_mac_prefix": {...}, "by_ip": {...}}.
    Returns (dict, mtime-or-None). Missing file -> empty dicts."""
    empty = {"by_mac": {}, "by_mac_prefix": {}, "by_ip": {}}
    try:
        with open(ALIASES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k in empty:
            empty[k] = {str(kk).upper(): vv for kk, vv in
                        (data.get(k) or {}).items()}
        return empty, os.path.getmtime(ALIASES_FILE)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return empty, None


def resolve_name(mac, ip, hostname, aliases):
    """Alias (MAC exact, then MAC prefix, then IP) wins over hostname."""
    if mac in aliases["by_mac"]:
        return aliases["by_mac"][mac], True
    for prefix, name in aliases["by_mac_prefix"].items():
        if mac.startswith(prefix):
            return name, True
    if ip in aliases["by_ip"]:
        return aliases["by_ip"][ip], True
    return (hostname or "(no name)"), False


def parse_system(loadavg_text, meminfo_text, uptime_text):
    load = [None, None, None]
    try:
        vals = loadavg_text.split()
        load = [float(vals[0]), float(vals[1]), float(vals[2])]
    except (IndexError, ValueError):
        pass
    mem = {}
    for line in meminfo_text.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)\s+kB", line)
        if m:
            mem[m.group(1)] = int(m.group(2))
    total = mem.get("MemTotal", 0)
    free = mem.get("MemFree", 0)
    buffers = mem.get("Buffers", 0)
    cached = mem.get("Cached", 0)
    # Old kernel has no MemAvailable: available ~= Free + Buffers + Cached.
    avail = free + buffers + cached
    used = max(total - avail, 0) if total else 0
    used_pct = round(used / total * 100, 1) if total else None
    uptime_seconds = None
    try:
        uptime_seconds = float(uptime_text.split()[0])
    except (IndexError, ValueError):
        pass
    return {
        "load1": load[0], "load5": load[1], "load15": load[2],
        "mem_total_kb": total or None,
        "mem_free_kb": free or None,
        "mem_buffers_kb": buffers or None,
        "mem_cached_kb": cached or None,
        "mem_available_kb": avail or None,
        "mem_used_kb": used or None,
        "mem_used_pct": used_pct,
        "uptime_seconds": uptime_seconds,
        "uptime": format_uptime(uptime_seconds),
    }


def format_uptime(seconds):
    if seconds is None:
        return None
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, _ = divmod(s, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def parse_wan(addr_text, route_text, dns_lines):
    """WAN (eth3) addressing. Returns dict; missing values stay None.

    NOTE: udhcpc writes no lease file on Padavan, so the IPv4 DHCP lease
    expiry is not observable here (lease_seconds stays None). IPv6
    valid_lft comes from `ip addr`."""
    wan = {"ip": None, "prefix": None, "gateway": None, "dns": [],
           "ipv6": None, "ipv6_valid_lft_sec": None, "proto": "dhcp",
           "lease_seconds": None, "double_nat": None}
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", addr_text)
    if m:
        wan["ip"] = m.group(1)
        wan["prefix"] = int(m.group(2))
    # global (non-link-local) IPv6 + its remaining lifetime
    for m in re.finditer(
            r"inet6 ([0-9a-fA-F:]+)/\d+ scope global(?: dynamic)?\s*\n"
            r"\s*valid_lft (\d+)sec", addr_text):
        wan["ipv6"] = m.group(1)
        wan["ipv6_valid_lft_sec"] = int(m.group(2))
        break
    m = re.search(r"default via (\d+\.\d+\.\d+\.\d+) dev eth3", route_text)
    if m:
        wan["gateway"] = m.group(1)
    for line in dns_lines:
        v = line.strip()
        if v and re.match(r"^\d+\.\d+\.\d+\.\d+$", v):
            wan["dns"].append(v)
    # Double NAT: WAN address is RFC1918 (behind the ONT / upstream router).
    if wan["ip"]:
        first = int(wan["ip"].split(".")[0])
        second = int(wan["ip"].split(".")[1])
        wan["double_nat"] = (
            first == 10
            or (first == 172 and 16 <= second <= 31)
            or (first == 192 and second == 168))
    return wan


def parse_conntrack(text):
    vals = [line.strip() for line in text.splitlines() if line.strip()]
    count = max_count = None
    try:
        if vals:
            count = int(vals[0])
        if len(vals) > 1:
            max_count = int(vals[1])
    except ValueError:
        pass
    pct = round(count / max_count * 100, 1) if count is not None and max_count else None
    return {"count": count, "max": max_count, "pct": pct}


# ---------------- AdGuard Home (runs on this Mac) ----------------

def read_adguard_password():
    try:
        with open(ADGUARD_AUTH_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def adguard_get(password, path, timeout=5):
    req = urllib.request.Request(ADGUARD_BASE + path)
    token = base64.b64encode(f"admin:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def adguard_stats(password):
    """-> (top_clients {ip: queries}, total_queries, total_blocked)."""
    d = adguard_get(password, "/control/stats")
    top = {}
    for item in d.get("top_clients") or []:
        if isinstance(item, dict):
            top.update(item)
    return top, d.get("num_dns_queries"), d.get("num_blocked_filtering")


def adguard_blocked(password, limit=ADGUARD_QUERYLOG_LIMIT):
    """Aggregate the query log -> {ip: blocked_count} for entries whose
    reason is FilteredBlackList (ad-block filtering)."""
    d = adguard_get(password, f"/control/querylog?limit={limit}", timeout=15)
    blocked = {}
    for row in d.get("data") or []:
        if row.get("reason") == "FilteredBlackList":
            client = row.get("client")
            if client:
                blocked[client] = blocked.get(client, 0) + 1
    return blocked


# ---------------- last-seen tracking (Mac /tmp, never Flash) ----------------

def load_lastseen():
    try:
        with open(LASTSEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: int(v) for k, v in data.items()
                    if isinstance(v, (int, float))}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        pass
    return {}


def save_lastseen(state):
    tmp = LASTSEEN_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, LASTSEEN_FILE)
    except OSError as e:
        log(f"lastseen write failed: {e}")


# ---------------- one poll ----------------

def collect(prev, aliases, lastseen, adguard_password, adguard_state, poll_no):
    """One poll. Returns (snapshot_dict, new_prev). Raises on SSH failure."""
    now = time.time()
    out = run_router(CMD_COMBINED)
    sections = split_sections(out, {
        "---ARP---", "---LEASES---", "---SYS---", "---MEM---", "---UPTIME---",
        "---WANADDR---", "---ROUTE---", "---CONN---", "---WANDNS---",
        "---DMESG-RA0---", "---DMESG-RAI0---",
    })
    ifaces_raw = parse_ip_link(out.split("---ARP---")[0])
    arp = parse_arp(sections.get("---ARP---", ""))
    leases = parse_leases(sections.get("---LEASES---", ""))
    system = parse_system(sections.get("---SYS---", ""),
                          sections.get("---MEM---", ""),
                          sections.get("---UPTIME---", ""))
    wan_addr = parse_wan(sections.get("---WANADDR---", ""),
                         sections.get("---ROUTE---", ""),
                         sections.get("---WANDNS---", "").splitlines())
    conntrack = parse_conntrack(sections.get("---CONN---", ""))
    sta_24 = parse_stainfo(sections.get("---DMESG-RA0---", ""))
    sta_5 = parse_stainfo(sections.get("---DMESG-RAI0---", ""))

    # ---- AdGuard (best effort; never breaks the poll) ----
    if adguard_password:
        try:
            top, total_q, total_b = adguard_stats(adguard_password)
            adguard_state["top_clients"] = top
            adguard_state["total_queries"] = total_q
            adguard_state["total_blocked"] = total_b
            adguard_state["ok"] = True
            adguard_state["error"] = None
            adguard_state["updated_at_epoch"] = int(now)
            if poll_no % ADGUARD_QUERYLOG_EVERY == 0:
                adguard_state["blocked"] = adguard_blocked(adguard_password)
                adguard_state["blocked_window"] = (
                    f"最近 {ADGUARD_QUERYLOG_LIMIT} 条日志")
        except Exception as e:  # keep last good numbers, mark stale
            adguard_state["ok"] = False
            adguard_state["error"] = str(e)[:200]
    top_clients = adguard_state.get("top_clients") or {}
    blocked_map = adguard_state.get("blocked") or {}

    # ---- devices: split upstream (eth3 / 192.168.1.x) from LAN clients ----
    devices = []
    upstream = []
    all_macs = set(arp) | set(leases)
    lastseen_dirty = False
    cutoff = now - LASTSEEN_KEEP_DAYS * 86400
    for mac in list(lastseen):
        if lastseen[mac] < cutoff:
            del lastseen[mac]
            lastseen_dirty = True
    for mac in sorted(all_macs):
        arp_entry = arp.get(mac)
        lease = leases.get(mac)
        ips = sorted(set((arp_entry or {}).get("ips", []) +
                         ([lease["ip"]] if lease else [])),
                     key=ip_key)
        if not ips:
            continue
        iface = (arp_entry or {}).get("iface", "")
        flags = (arp_entry or {}).get("flags", 0)
        online = bool(arp_entry) and bool(flags & 0x2)
        hostname = (lease or {}).get("hostname", "")
        # Primary IP: prefer a live ARP address, else the lease address.
        live_ips = [ip for ip in (arp_entry or {}).get("ips", [])
                    if flags & 0x2]
        primary = live_ips[0] if live_ips else ips[0]
        is_upstream = (iface == "eth3" or
                       primary.startswith("192.168.1."))
        name, is_alias = resolve_name(mac, primary, hostname, aliases)
        if is_upstream:
            upstream.append({
                "ip": primary, "ips": ips, "mac": mac,
                "hostname": hostname, "name": name,
                "interface": iface or "eth3",
                "role": "网关（ONT）" if primary == wan_addr.get("gateway")
                        else "上游设备",
            })
            continue
        if online:
            lastseen[mac] = int(now)
            lastseen_dirty = True
            last_seen_epoch = int(now)
        else:
            last_seen_epoch = lastseen.get(mac)
        if mac in sta_24:
            link, rssi = "2.4G", sta_24[mac]
        elif mac in sta_5:
            link, rssi = "5G", sta_5[mac]
        elif iface in ("br0", "eth2"):
            # Seen on the bridge but not on either radio: wired from this
            # router's view (includes clients behind a downstream AP).
            link, rssi = "有线", None
        else:
            # Lease-only, not in ARP and not associated: link unknown.
            link, rssi = "未知", None
        devices.append({
            "ip": primary, "ips": ips, "mac": mac,
            "hostname": hostname, "name": name, "alias": is_alias,
            "interface": iface, "link": link, "rssi": rssi,
            "online": online,
            "last_seen_epoch": last_seen_epoch,
            "last_seen": format_last_seen(last_seen_epoch) if not online else None,
            "random_mac": is_random_mac(mac),
            "dns_queries": top_clients.get(primary),
            "dns_blocked": blocked_map.get(primary),
        })
    devices.sort(key=lambda d: ip_key(d["ip"]))
    upstream.sort(key=lambda d: ip_key(d["ip"]))
    if lastseen_dirty:
        save_lastseen(lastseen)

    # ---- interfaces in fixed order; unknown ones last (rank 99) ----
    interfaces = {}
    new_prev = {}
    for name, raw in ifaces_raw.items():
        rx_mbps = 0.0
        tx_mbps = 0.0
        if prev and name in prev:
            old_rx, old_tx, old_t = prev[name]
            dt = now - old_t
            if dt > 0:
                drx = raw["rx_bytes"] - old_rx
                dtx = raw["tx_bytes"] - old_tx
                # Counters reset (router reboot) -> no valid delta this round.
                rx_mbps = round(max(drx, 0) * 8 / dt / 1e6, 3) if drx >= 0 else 0.0
                tx_mbps = round(max(dtx, 0) * 8 / dt / 1e6, 3) if dtx >= 0 else 0.0
        interfaces[name] = {
            "label": LABELS.get(name, name),
            "rx_bytes": raw["rx_bytes"],
            "tx_bytes": raw["tx_bytes"],
            "rx_mbps": rx_mbps,
            "tx_mbps": tx_mbps,
            "up": raw["up"],
        }
        new_prev[name] = (raw["rx_bytes"], raw["tx_bytes"], now)

    def rank(n):
        return ORDER.index(n) if n in ORDER else 99

    ordered = dict(sorted(interfaces.items(),
                          key=lambda kv: (rank(kv[0]), kv[0])))

    wan = ordered.get("eth3", {})
    snapshot = {
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "updated_at_epoch": int(now),
        "ok": True,
        "error": None,
        "router_reachable": True,
        "interfaces": ordered,
        "wan": {
            "up": wan.get("up", False),
            "rx_mbps": wan.get("rx_mbps", 0.0),
            "tx_mbps": wan.get("tx_mbps", 0.0),
            "ip": wan_addr.get("ip"),
            "prefix": wan_addr.get("prefix"),
            "gateway": wan_addr.get("gateway"),
            "dns": wan_addr.get("dns") or [],
            "ipv6": wan_addr.get("ipv6"),
            "ipv6_valid_lft_sec": wan_addr.get("ipv6_valid_lft_sec"),
            "proto": wan_addr.get("proto"),
            "lease_seconds": wan_addr.get("lease_seconds"),
            "double_nat": wan_addr.get("double_nat"),
        },
        "conntrack": conntrack,
        "devices": devices,
        "upstream": upstream,
        "counts": {
            "clients": len(devices),
            "online": sum(1 for d in devices if d["online"]),
            "offline": sum(1 for d in devices if not d["online"]),
            "upstream": len(upstream),
        },
        "adguard": {
            "ok": bool(adguard_state.get("ok")),
            "error": adguard_state.get("error"),
            "updated_at_epoch": adguard_state.get("updated_at_epoch"),
            "total_queries_24h": adguard_state.get("total_queries"),
            "total_blocked_24h": adguard_state.get("total_blocked"),
            "blocked_window": adguard_state.get("blocked_window"),
        },
        "system": system,
    }
    return snapshot, new_prev


def write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main():
    log("collector starting (poll every %ds) -> %s" % (POLL_INTERVAL, DATA_FILE))
    snapshot = None
    prev = None
    history = []
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            old = json.load(f)
        history = old.get("history", [])[-HISTORY_LEN:]
        log("resumed history: %d samples" % len(history))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    aliases, aliases_mtime = load_aliases()
    log("aliases loaded: %d mac, %d prefix, %d ip" % (
        len(aliases["by_mac"]), len(aliases["by_mac_prefix"]),
        len(aliases["by_ip"])))
    adguard_password = read_adguard_password()
    if adguard_password:
        log("AdGuard credentials file present")
    else:
        log("WARNING: no AdGuard credentials file (%s) -- DNS stats off"
            % ADGUARD_AUTH_FILE)
    adguard_state = {"top_clients": {}, "blocked": {}, "ok": False,
                     "error": "not fetched yet", "total_queries": None,
                     "total_blocked": None, "updated_at_epoch": None,
                     "blocked_window": None}
    lastseen = load_lastseen()
    log("lastseen loaded: %d macs" % len(lastseen))
    poll_no = 0
    while True:
        start = time.time()
        try:
            # Reload aliases.json when it changes (no restart needed).
            try:
                mtime = os.path.getmtime(ALIASES_FILE)
            except OSError:
                mtime = None
            if mtime != aliases_mtime:
                aliases, aliases_mtime = load_aliases()
                log("aliases reloaded")
            snapshot, prev = collect(prev, aliases, lastseen,
                                    adguard_password, adguard_state, poll_no)
            poll_no += 1
            history.append({
                "t": snapshot["updated_at_epoch"],
                "rx_mbps": {n: v["rx_mbps"] for n, v in snapshot["interfaces"].items()
                            if n in HISTORY_IFACES},
                "tx_mbps": {n: v["tx_mbps"] for n, v in snapshot["interfaces"].items()
                            if n in HISTORY_IFACES},
            })
            history = history[-HISTORY_LEN:]
            snapshot["history"] = history
            write_atomic(DATA_FILE, snapshot)
        except Exception as e:  # keep last good data, keep polling
            log(f"poll failed: {e}")
            if snapshot is not None:
                snapshot["ok"] = False
                snapshot["router_reachable"] = False
                snapshot["error"] = str(e)[:300]
                snapshot["last_error_at"] = datetime.datetime.now().isoformat(timespec="seconds")
                snapshot["history"] = history
                try:
                    write_atomic(DATA_FILE, snapshot)
                except OSError as we:
                    log(f"write failed: {we}")
        elapsed = time.time() - start
        time.sleep(max(0.1, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
