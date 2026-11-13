"""Subscription parser for proxy-test.

Handles:
  - base64-encoded subscription bodies (whole-body base64 of links or YAML)
  - plain link lists: vmess://, vless://, trojan://, ss://, hysteria2://,
    anytls://, tuic://
  - Clash YAML (block-style and flow-style proxies), incl. plain YAML lists
    of proxy dicts.

Output: a list of node dicts already in mihomo proxy format
({name, type, server, port, ...type-specific fields...}).

Credentials (passwords, uuids) are kept server-side only; use
sanitize_for_api() before returning nodes to any client.
"""
import base64
import binascii
import json
import re
import urllib.parse
import urllib.request

import yaml

LINK_SCHEMES = ("vmess://", "vless://", "trojan://", "ss://",
                "hysteria2://", "hysteria://", "anytls://", "tuic://")

INFO_NAME_PATTERNS = ("剩余流量", "套餐到期", "过滤掉", "流量", "到期", "套餐",
                      "有效期", "余额")
GROUP_TYPES = {"select", "url-test", "fallback", "load-balance", "relay"}


def is_info_node(name):
    if not name:
        return True
    return any(p in name for p in INFO_NAME_PATTERNS)


def is_group_node(node):
    t = str(node.get("type", "")).lower()
    if t in GROUP_TYPES:
        return True
    name = str(node.get("name", ""))
    if "chain" in name.lower():
        return True
    # groups carry a `proxies` list but no `server`
    if "proxies" in node and "server" not in node:
        return True
    return False


def region_key(name):
    """Extract a region key from a node name, e.g. '香港-优化' -> '香港',
    '美国LA-优化-GPT' -> '美国LA', '新加坡🇸🇬1解锁' -> '新加坡'."""
    if not name:
        return "未知"
    # strip emoji / symbols
    s = "".join(ch for ch in name
                if not (0x1F000 <= ord(ch) <= 0x1FAFF or
                        0x2600 <= ord(ch) <= 0x27BF or
                        0xFE00 <= ord(ch) <= 0xFE0F or
                        ord(ch) == 0x200D))
    m = re.match(r"([\u4e00-\u9fff]+[A-Za-z]*)", s)
    if m:
        return m.group(1)
    # English names: take part before first '-'
    return s.split("-")[0].strip() or s.strip() or "未知"


# ---------------------------------------------------------------- links

def _b64decode_padded(s):
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def _parse_vmess(link):
    raw = _b64decode_padded(link[len("vmess://"):])
    d = json.loads(raw.decode("utf-8", "replace"))
    node = {
        "name": d.get("ps", "vmess"),
        "type": "vmess",
        "server": d.get("add", ""),
        "port": int(d.get("port", 0) or 0),
        "uuid": d.get("id", ""),
        "alterId": int(d.get("aid", 0) or 0),
        "cipher": "auto",
    }
    net = (d.get("net") or "tcp").lower()
    node["network"] = net
    if str(d.get("tls", "")).lower() in ("tls", "true", "1"):
        node["tls"] = True
        if d.get("sni"):
            node["servername"] = d["sni"]
    if net == "ws":
        node["ws-opts"] = {"path": d.get("path", "/") or "/"}
        if d.get("host"):
            node["ws-opts"]["headers"] = {"Host": d["host"]}
    elif net == "grpc":
        node["grpc-opts"] = {"grpc-service-name": d.get("path", "") or ""}
    return node


def _parse_url_link(link):
    """vless/trojan/ss/hysteria2/anytls/tuic URL-style links."""
    scheme, rest = link.split("://", 1)
    scheme = scheme.lower()
    name = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
        name = urllib.parse.unquote(frag)
    if "?" in rest:
        main, query = rest.split("?", 1)
        params = dict(urllib.parse.parse_qsl(query))
    else:
        main, params = rest, {}
    if "@" in main:
        userinfo, hostport = main.rsplit("@", 1)
    else:
        userinfo, hostport = "", main
    if ":" in hostport:
        host, port = hostport.rsplit(":", 1)
        try:
            port = int(port)
        except ValueError:
            port = 0
    else:
        host, port = hostport, 0
    host = host.strip("[]")

    if scheme == "vless":
        node = {"name": name or "vless", "type": "vless", "server": host,
                "port": port, "uuid": urllib.parse.unquote(userinfo),
                "udp": True}
        if params.get("flow"):
            node["flow"] = params["flow"]
        if params.get("security", "").lower() == "tls":
            node["tls"] = True
            if params.get("sni"):
                node["servername"] = params["sni"]
            if params.get("fp"):
                node["client-fingerprint"] = params["fp"]
        if params.get("security", "").lower() == "reality":
            node["tls"] = True
            if params.get("sni"):
                node["servername"] = params["sni"]
            if params.get("fp"):
                node["client-fingerprint"] = params["fp"]
            node["reality-opts"] = {
                "public-key": params.get("pb", ""),
                "short-id": params.get("sid", ""),
            }
        return node
    if scheme == "trojan":
        node = {"name": name or "trojan", "type": "trojan", "server": host,
                "port": port, "password": urllib.parse.unquote(userinfo),
                "skip-cert-verify": True, "udp": True}
        if params.get("sni"):
            node["sni"] = params["sni"]
        return node
    if scheme in ("hysteria2", "hysteria"):
        node = {"name": name or "hysteria2", "type": "hysteria2",
                "server": host, "port": port,
                "password": urllib.parse.unquote(userinfo),
                "skip-cert-verify": True}
        if params.get("sni"):
            node["sni"] = params["sni"]
        return node
    if scheme == "anytls":
        node = {"name": name or "anytls", "type": "anytls", "server": host,
                "port": port, "password": urllib.parse.unquote(userinfo),
                "skip-cert-verify": True, "udp": True}
        if params.get("sni"):
            node["sni"] = params["sni"]
        return node
    if scheme == "tuic":
        uuid = pw = ""
        if ":" in userinfo:
            uuid, pw = userinfo.split(":", 1)
        else:
            uuid = userinfo
        node = {"name": name or "tuic", "type": "tuic", "server": host,
                "port": port, "uuid": urllib.parse.unquote(uuid),
                "password": urllib.parse.unquote(pw),
                "skip-cert-verify": True}
        if params.get("sni"):
            node["sni"] = params["sni"]
        return node
    if scheme == "ss":
        # ss://BASE64(method:password@host:port)  or
        # ss://BASE64(method:password)@host:port
        try:
            if "@" in userinfo:
                decoded = _b64decode_padded(userinfo).decode("utf-8", "replace")
                # decoded is method:password@host:port  OR method:password
                if "@" in decoded:
                    creds, hp = decoded.rsplit("@", 1)
                    if ":" in hp:
                        host, port = hp.rsplit(":", 1)
                        port = int(port)
                else:
                    creds = decoded
                method, password = creds.split(":", 1)
            else:
                decoded = _b64decode_padded(userinfo).decode("utf-8", "replace")
                method, password = decoded.split(":", 1)
        except Exception:
            method, password = "aes-256-gcm", ""
        return {"name": name or "ss", "type": "ss", "server": host,
                "port": port, "cipher": method, "password": password}
    raise ValueError("unsupported scheme: " + scheme)


def parse_link_line(line):
    line = line.strip()
    if line.startswith("vmess://"):
        return _parse_vmess(line)
    for scheme in ("vless://", "trojan://", "ss://", "hysteria2://",
                   "hysteria://", "anytls://", "tuic://"):
        if line.startswith(scheme):
            return _parse_url_link(line)
    raise ValueError("not a proxy link")


# ---------------------------------------------------------------- YAML

def _yaml_to_nodes(doc):
    """Extract proxy dicts from a parsed YAML doc."""
    nodes = []
    if isinstance(doc, dict):
        proxies = doc.get("proxies") or []
        if isinstance(proxies, list):
            nodes.extend(proxies)
    elif isinstance(doc, list):
        nodes.extend(doc)
    out = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        if "name" not in n or "type" not in n:
            continue
        out.append(dict(n))
    return out


def _maybe_b64(text):
    """If the whole body is base64, decode it; else return as-is."""
    t = text.strip()
    if not t:
        return t
    # base64 bodies are single-line-ish with no spaces and valid alphabet
    compact = re.sub(r"\s+", "", t)
    if len(compact) < 20:
        return t
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        return t
    try:
        decoded = base64.b64decode(compact + "=" * (-len(compact) % 4))
        s = decoded.decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return t
    # only accept if decoded looks like links or YAML
    if ("://" in s) or ("proxies:" in s) or ("- name:" in s) or ("type:" in s):
        return s
    return t


def parse_subscription_text(text):
    """Parse raw subscription text into a list of mihomo-format node dicts.
    Raises ValueError if nothing usable is found."""
    text = _maybe_b64(text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # link list?
    link_lines = [ln for ln in lines
                  if any(ln.startswith(s) for s in LINK_SCHEMES)]
    if link_lines:
        nodes = []
        for ln in link_lines:
            try:
                nodes.append(parse_link_line(ln))
            except Exception:
                continue
        if nodes:
            return nodes

    # YAML?
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError("could not parse subscription: %s" % e)
    nodes = _yaml_to_nodes(doc)
    if nodes:
        return nodes
    raise ValueError("no proxy nodes found in subscription")


def fetch_url(url, timeout=30):
    req = urllib.request.Request(
        url, headers={"User-Agent": "ClashMeta/1.0",
                      "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------- helpers

CRED_KEYS = ("password", "uuid", "token", "private-key", "public-key",
             "client-key")


def sanitize_for_api(node):
    """Copy of a node safe for API responses: no credentials."""
    out = {}
    for k, v in node.items():
        if k.lower() in CRED_KEYS:
            out[k] = "***"
        elif k == "reality-opts" and isinstance(v, dict):
            out[k] = {kk: ("***" if kk in ("public-key", "private-key")
                           else vv) for kk, vv in v.items()}
        else:
            out[k] = v
    return out


def filter_real_nodes(nodes):
    """Drop info nodes and group/chain nodes; keep real proxies."""
    out = []
    for n in nodes:
        if is_group_node(n):
            continue
        if is_info_node(str(n.get("name", ""))):
            continue
        if not n.get("server") or not n.get("port"):
            continue
        out.append(n)
    return out


def pick_relays(nodes):
    """One relay node per region (first in subscription order)."""
    seen = {}
    order = []
    for n in filter_real_nodes(nodes):
        key = region_key(str(n.get("name", "")))
        if key not in seen:
            seen[key] = n
            order.append(key)
    return [(key, seen[key]) for key in order]


def group_exits(nodes):
    """Group exit nodes by server; returns [(server, [nodes...])]."""
    groups = {}
    order = []
    for n in filter_real_nodes(nodes):
        srv = str(n.get("server", ""))
        if srv not in groups:
            groups[srv] = []
            order.append(srv)
        groups[srv].append(n)
    return [(srv, groups[srv]) for srv in order]


def summarize(nodes):
    """Summary for the subscription list: regions and servers/protocols."""
    real = filter_real_nodes(nodes)
    regions = []
    for key, _ in pick_relays(nodes):
        regions.append(key)
    servers = {}
    for srv, ns in group_exits(nodes):
        servers[srv] = sorted({str(n.get("type", "")) for n in ns})
    return {
        "node_count": len(real),
        "regions": regions,
        "servers": servers,
    }
