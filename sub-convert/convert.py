"""Build the converted Clash YAML for a subscription.

Contents: all parsed main nodes (renamed with a measured download-speed
suffix, e.g. "香港-优化 2.4MB"), a "精选" url-test group with the 3
fastest main nodes by DOWNLOAD speed, a "精选中继" url-test group with
the 3 fastest relay chains by download speed (when an auxiliary
subscription is attached), and an "全部节点" select group.

TEMPLATE CLEANSING (2026-11-13): the VPS subscription is the template.
Every OTHER subscription keeps ONLY its own nodes (parsed, renamed
with the measured speed suffix); the template's rules, dns,
rule-providers, geox-url, mixed-port, allow-lan, mode and log-level
are applied on top. The template sub itself keeps its own doc (it IS
the template), and if the template doc cannot be loaded the converter
falls back to the old behaviour (preserve the sub's own doc / minimal
output) — never crash an existing subscription.

The target's ORIGINAL proxy-groups are kept (node names renamed to
the speed-suffixed names, exactly as the preserve logic already
does). For a link-list source (no groups) the minimal output's
全部节点 select group is the "original" group. The template's
protocol groups (选-AnyTLS etc.), 选-chain and 选-goog are NOT
copied — the rules never reference them.

The template's rules reference exactly 6 group names (AI, 流媒体,
Telegram, 社交, 开发, 节点选择, plus DIRECT/REJECT), which the
target subscriptions do NOT have. So those 6 groups are ADDED when
missing:
- Auto: a url-test over ALL of the target's real nodes, using the
  template's url-test url/interval/tolerance. If the target's
  original groups already contain a url-test group, THAT group's name
  is used as the Auto instead of creating a new one (its members are
  set to all real nodes and its settings to the template's).
- 节点选择 (select): members = [Auto] + [the target's other original
  groups] + [精选] + [精选中继 if chains exist]. For a link-list
  source: members = [Auto, 全部节点, 精选].
- AI, 流媒体, Telegram, 社交, 开发 (select): members = [节点选择].

Speed suffix: download speed in MB/s, one decimal (" 2.4MB"). Nodes
with no measured speed get NO suffix and are never backfilled.
Selection for 精选 / 精选中继 is by download speed_bps, highest first.

Node names are deduplicated first (first occurrence keeps its name,
later duplicates get #2, #3...), then the speed suffix is appended, so
group references always use the renamed names. A chain proxy's
dialer-proxy points at its relay node's RENAMED name in the YAML.
"""
import copy

import yaml

GEN204 = "https://www.gstatic.com/generate_204"

# The VPS subscription whose rules/DNS cleanse every other
# subscription. The template sub itself is exempt (its own doc IS the
# template).
TEMPLATE_SUB_ID = "419387b4d5dc"

# Groups referenced by the template's rules (plus DIRECT/REJECT).
# They are added when the target does not already have them.
RULE_GROUPS = ["AI", "流媒体", "Telegram", "社交", "开发"]


def speed_suffix(speed_bps):
    """' 2.4MB' for a measured download speed, '' when unmeasured."""
    if isinstance(speed_bps, (int, float)) and speed_bps > 0:
        return " %.1fMB" % (speed_bps / 1_000_000)
    return ""


def download_speed(result):
    """Download speed_bps from a per-proxy result dict, or 0."""
    d = (result or {}).get("download") or {}
    sp = d.get("speed_bps")
    return sp if isinstance(sp, (int, float)) and sp > 0 else 0


def delay_ms(result):
    """Delay in ms from a per-proxy result dict, or None."""
    d = (result or {}).get("delay") or {}
    t = d.get("time_s")
    if isinstance(t, (int, float)) and t > 0:
        return int(t * 1000)
    return None


def dedupe_names(nodes):
    seen = {}
    out = []
    for n in nodes:
        p = dict(n)
        name = str(p.get("name", "node"))
        if name in seen:
            seen[name] += 1
            p["name"] = "%s#%d" % (name, seen[name])
        else:
            seen[name] = 1
        out.append(p)
    return out


def pick_top_by_speed(results, count=3):
    """Highest download speed first; only nodes with a measured speed."""
    ok = [r for r in (results or []) if download_speed(r) > 0]
    ok.sort(key=download_speed, reverse=True)
    return ok[:count]


def _rename_dialer_proxy(proxy, main_yaml_name, aux_yaml_name):
    """Return a copy of a proxy with its dialer-proxy renamed to the
    renamed target name (main map first, then aux map, else unchanged).
    A chain proxy whose dialer-proxy still points at the pre-suffix
    name dangles and makes mihomo refuse the whole file."""
    q = dict(proxy)
    dp = q.get("dialer-proxy")
    if isinstance(dp, str):
        q["dialer-proxy"] = main_yaml_name.get(
            dp, aux_yaml_name.get(dp, dp))
    return q


def _rename_source_proxies(source_doc, main_yaml_name):
    """Rename the source doc's own proxies (name + dialer-proxy) to
    the speed-suffixed names. Replicates the dedupe counting so each
    occurrence maps to the right renamed name; a proxy we do not know
    (not in the parsed list) is kept verbatim so no group reference
    ever dangles. Returns the renamed proxy list."""
    seen = {}
    renamed = []
    for p in (source_doc.get("proxies") or []):
        orig = str(p.get("name", ""))
        if orig in seen:
            seen[orig] += 1
            deduped = "%s#%d" % (orig, seen[orig])
        else:
            seen[orig] = 1
            deduped = orig
        q = dict(p)
        if deduped in main_yaml_name:
            q["name"] = main_yaml_name[deduped]
        # A source chain node (dialer-proxy) must point at the
        # RENAMED target: the target node got a speed suffix too, so
        # the original name no longer exists in the YAML and
        # mihomo/FlClash refuses to load it ("dialer-proxy not
        # found").
        dp = q.get("dialer-proxy")
        if isinstance(dp, str) and dp in main_yaml_name:
            q["dialer-proxy"] = main_yaml_name[dp]
        renamed.append(q)
    return renamed


def _rename_group_members(groups, main_yaml_name):
    """Rename node references inside groups to the suffixed names.
    Names not in the map (other group names, DIRECT, REJECT, ...) are
    left untouched. Returns a deep-copied group list."""
    out = []
    for g in (groups or []):
        if not isinstance(g, dict):
            continue
        q = copy.deepcopy(g)
        members = q.get("proxies") or []
        q["proxies"] = [main_yaml_name.get(m, m) for m in members]
        out.append(q)
    return out


def _apply_template(template_doc, source_doc, main_deduped,
                    main_yaml_name, aux_yaml_name, main_renamed,
                    relay_proxies, chain_yaml_proxies, extra_groups,
                    tested_set):
    """Rebuild a subscription's YAML from the VPS template doc.

    The target's own nodes are kept (renamed with speed suffix) and
    its ORIGINAL proxy-groups are kept (node names renamed). The
    template's rules, dns, rule-providers, geox-url, mixed-port,
    allow-lan, mode and log-level replace the target's. The 6 groups
    the template's rules reference (Auto / 节点选择 / AI / 流媒体 /
    Telegram / 社交 / 开发) are added when missing.
    Returns the YAML text.
    """
    # --- the target's original groups + proxies ----------------------
    if (isinstance(source_doc, dict)
            and isinstance(source_doc.get("proxy-groups"), list)):
        doc_proxies = _rename_source_proxies(source_doc, main_yaml_name)
        orig_groups = _rename_group_members(
            source_doc.get("proxy-groups"), main_yaml_name)
    else:
        # Link-list source (no groups): the minimal 全部节点 group is
        # the "original" group.
        doc_proxies = [
            _rename_dialer_proxy(p, main_yaml_name, aux_yaml_name)
            for p in main_renamed
        ]
        orig_groups = [{"name": "全部节点", "type": "select",
                        "proxies": [str(n.get("name", ""))
                                    for n in main_renamed]}]
    doc_proxies = doc_proxies + list(relay_proxies) + list(chain_yaml_proxies)

    # --- Auto: url-test over all real nodes ---------------------------
    # All real (tested) node names, in subscription order.
    auto_members = [main_yaml_name.get(str(n.get("name", "")), "")
                    for n in main_deduped
                    if str(n.get("name", "")) in tested_set]
    # Template's url-test settings (from its Auto group, else defaults).
    t_auto = {}
    for g in (template_doc.get("proxy-groups") or []):
        if isinstance(g, dict) and g.get("name") == "Auto":
            t_auto = g
            break
    auto_url = t_auto.get("url") or GEN204
    auto_interval = t_auto.get("interval") or 300
    auto_tolerance = t_auto.get("tolerance") or 50
    # Reuse an existing url-test group as the Auto; else create "Auto".
    auto_name = None
    for g in orig_groups:
        if g.get("type") == "url-test":
            auto_name = g.get("name")
            g["proxies"] = list(auto_members)
            g["url"] = auto_url
            g["interval"] = auto_interval
            g["tolerance"] = auto_tolerance
            break
    auto_is_original = auto_name is not None
    if not auto_is_original:
        auto_name = "Auto"

    # --- 节点选择 ------------------------------------------------------
    # Members: [Auto] + other original groups + 精选 / 精选中继.
    other_orig = [g.get("name") for g in orig_groups
                  if g.get("name") not in (auto_name, "节点选择")]
    sel_members = [auto_name] + other_orig
    sel_members += [g["name"] for g in extra_groups]
    sel_group = {"name": "节点选择", "type": "select",
                 "proxies": sel_members}

    # --- assemble the group list --------------------------------------
    # Order: 节点选择 first, then Auto (if new), then the original
    # groups in their original order, then the purpose groups, then
    # 精选 / 精选中继.
    groups = [sel_group]
    if not auto_is_original:
        groups.append({"name": "Auto", "type": "url-test",
                       "url": auto_url, "interval": auto_interval,
                       "tolerance": auto_tolerance,
                       "proxies": list(auto_members)})
    groups.extend([g for g in orig_groups if g.get("name") != "节点选择"])
    existing = {g.get("name") for g in groups}
    for pname in RULE_GROUPS:
        if pname not in existing:
            groups.append({"name": pname, "type": "select",
                           "proxies": ["节点选择"]})
            existing.add(pname)
    groups.extend(extra_groups)

    # --- assemble: template's top-level keys verbatim -----------------
    # Keep the template's key order; only proxies / proxy-groups are
    # replaced with the target's rebuilt ones.
    doc = {}
    for k, v in template_doc.items():
        if k == "proxies":
            doc[k] = doc_proxies
        elif k == "proxy-groups":
            doc[k] = groups
        else:
            doc[k] = copy.deepcopy(v)
    doc.setdefault("proxies", doc_proxies)
    doc.setdefault("proxy-groups", groups)

    # Safety net: every group the rules reference must exist. The 6
    # rule groups are added above, so this should never trigger; if
    # it does, add the missing group with [节点选择] members.
    group_names = {g.get("name") for g in groups}
    for rule in (doc.get("rules") or []):
        if not isinstance(rule, str):
            continue
        parts = rule.split(",")
        if len(parts) >= 3:
            policy = parts[2]
        elif len(parts) >= 2:
            policy = parts[1]
        else:
            continue
        if (policy and policy not in ("DIRECT", "REJECT")
                and policy not in group_names):
            groups.append({"name": policy, "type": "select",
                           "proxies": ["节点选择"]})
            group_names.add(policy)

    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def build_yaml(main_nodes, aux_nodes, direct_results, chain_results,
               chain_proxies, source_doc=None, template_doc=None,
               sub_id=None, featured_override=None):
    """Returns (yaml_text, featured_rows, relay_featured_rows).

    main_nodes / aux_nodes: parsed node dicts (aux may be []). Pass the
    FULL parsed lists (including info nodes) — only the node NAMES are
    renamed; untested nodes keep their original name (no suffix).
    direct_results: Phase-1 per-proxy results, each with name/role.
    chain_results: Phase-2 per-proxy results (kind=chain).
    chain_proxies: chain proxy dicts as built for the test (name
        "<relay>-><exit>", dialer-proxy "<relay>").
    source_doc: the source subscription's parsed YAML dict (or None).
        When it has proxy-groups, the output keeps the source's whole
        structure (groups, rules, dns, ...) verbatim, renames node
        references to the suffixed names, and appends 精选 / 精选中继.
    template_doc: the VPS template's parsed YAML dict (or None). For
        every subscription except the template sub itself, the output
        is rebuilt from the template (only the sub's own nodes and its
        original groups are kept; rules/dns/etc. come from the
        template). If the template cannot be loaded, falls back to the
        source_doc behaviour below.
    sub_id: the subscription being built, to exempt the template sub
        itself from template cleansing.
    featured_override: optional list of (deduped) test names to use as
        the 精选, in order, instead of the top 3 by download speed.
        Used by the featured-node watchdog (featured_watch.py) when
        all published 精选 nodes are bad: it rebuilds the YAML with
        replacement nodes while keeping everything else (groups,
        rules, dns, 精选中继) exactly as a normal rebuild would.
        Names not found in the direct results are skipped.
    """
    aux_nodes = aux_nodes or []
    chain_results = [r for r in (chain_results or []) if r is not None]
    chain_proxies = chain_proxies or []
    direct_results = [r for r in (direct_results or []) if r is not None]

    # --- speeds by test proxy name ------------------------------------
    main_speed = {}
    aux_speed = {}
    for r in (direct_results or []):
        sp = download_speed(r)
        if r.get("role") == "main":
            main_speed[r["name"]] = sp
        elif r.get("role") == "aux":
            aux_speed[r["name"]] = sp

    # --- rename main + aux nodes with speed suffix --------------------
    main_deduped = dedupe_names(main_nodes)
    main_renamed = []
    main_yaml_name = {}   # test name -> YAML name
    for n in main_deduped:
        base = str(n.get("name", ""))
        p = dict(n)
        p["name"] = base + speed_suffix(main_speed.get(base))
        main_renamed.append(p)
        main_yaml_name[base] = p["name"]
    aux_renamed = []
    aux_yaml_name = {}    # test name -> YAML name
    for n in dedupe_names(aux_nodes):
        base = str(n.get("name", ""))
        p = dict(n)
        p["name"] = base + speed_suffix(aux_speed.get(base))
        aux_renamed.append(p)
        aux_yaml_name[base] = p["name"]
    # A dialer-proxy inside an aux node must point at the RENAMED
    # target (same rule as for main nodes below).
    for p in aux_renamed:
        dp = p.get("dialer-proxy")
        if isinstance(dp, str) and dp in aux_yaml_name:
            p["dialer-proxy"] = aux_yaml_name[dp]

    # --- 精选: top 3 main by download speed ---------------------------
    # featured_override (watchdog): use the given test names, in
    # order, instead of the top 3 by speed. Names not found in the
    # direct results are skipped.
    if featured_override is not None:
        _by_name = {r["name"]: r for r in (direct_results or [])
                    if r.get("role") == "main"}
        featured = [_by_name[nm] for nm in featured_override
                    if nm in _by_name]
    else:
        featured = pick_top_by_speed(
            [r for r in (direct_results or []) if r.get("role") == "main"],
            3)
    featured_names = [main_yaml_name.get(r["name"], r["name"])
                      for r in featured]
    featured_rows = [{
        "name": main_yaml_name.get(r["name"], r["name"]),
        "delay_ms": delay_ms(r),
        "speed_bps": download_speed(r),
        "speed_mb": round(download_speed(r) / 1_000_000, 1),
    } for r in featured]

    # --- 精选中继: top 3 chains by download speed ----------------------
    relay_featured = pick_top_by_speed(chain_results, 3)
    chain_yaml_proxies = []
    chain_yaml_names = []
    relay_featured_rows = []
    needed_relays = set()   # relay YAML names used by featured chains
    chain_by_name = {p["name"]: p for p in chain_proxies}
    for r in relay_featured:
        p = chain_by_name.get(r["name"])
        if p is None:
            continue
        cp = dict(p)
        cp["name"] = p["name"] + speed_suffix(download_speed(r))
        # The relay is an aux node; fall back to the main map so a
        # chain whose relay is a main node still resolves.
        relay_yname = aux_yaml_name.get(
            p.get("dialer-proxy", ""),
            main_yaml_name.get(p.get("dialer-proxy", ""),
                               p.get("dialer-proxy", "")))
        cp["dialer-proxy"] = relay_yname
        chain_yaml_proxies.append(cp)
        chain_yaml_names.append(cp["name"])
        needed_relays.add(relay_yname)
        relay_featured_rows.append({
            "name": cp["name"],
            "relay": r.get("relay", ""),
            "exit": r.get("exit", ""),
            "delay_ms": delay_ms(r),
            "speed_bps": download_speed(r),
            "speed_mb": round(download_speed(r) / 1_000_000, 1),
        })

    # relay nodes actually used by the featured chains
    relay_proxies = [p for p in aux_renamed
                     if p["name"] in needed_relays]

    # --- assemble -------------------------------------------------------
    # The 精选 / 精选中继 groups to append (same in both paths).
    extra_groups = []
    if featured_names:
        extra_groups.append({"name": "精选", "type": "url-test",
                             "url": GEN204, "interval": 300, "tolerance": 50,
                             "proxies": featured_names})
    if chain_yaml_names:
        extra_groups.append({"name": "精选中继", "type": "url-test",
                             "url": GEN204, "interval": 300, "tolerance": 50,
                             "proxies": chain_yaml_names})

    # Template cleansing: every subscription except the template sub
    # itself is rebuilt from the VPS template (only its own nodes and
    # its original groups are kept; rules/dns/etc. come from the
    # template). If the template doc is missing/unusable, fall through
    # to the old behaviour below.
    if (sub_id != TEMPLATE_SUB_ID
            and isinstance(template_doc, dict)
            and isinstance(template_doc.get("proxy-groups"), list)):
        tested_set = {r["name"] for r in direct_results
                      if r.get("role") == "main"}
        text = _apply_template(
            template_doc, source_doc, main_deduped, main_yaml_name,
            aux_yaml_name, main_renamed, relay_proxies,
            chain_yaml_proxies, extra_groups, tested_set)
        return text, featured_rows, relay_featured_rows

    if (isinstance(source_doc, dict)
            and isinstance(source_doc.get("proxy-groups"), list)):
        # Preserve the source subscription's whole structure verbatim:
        # only node names change (speed suffix), references in the
        # original groups are renamed to match, then 精选 / 精选中继 are
        # appended. All other sections (rules, dns, ...) stay as-is.
        doc = copy.deepcopy(source_doc)
        renamed_proxies = _rename_source_proxies(doc, main_yaml_name)
        doc["proxies"] = (renamed_proxies + relay_proxies
                          + chain_yaml_proxies)
        # Rename member references inside the ORIGINAL groups only.
        # Names not in the map (other group names, DIRECT, REJECT, ...)
        # are left untouched.
        for g in doc["proxy-groups"]:
            members = g.get("proxies") or []
            g["proxies"] = [main_yaml_name.get(m, m) for m in members]
        doc["proxy-groups"] = list(doc["proxy-groups"]) + extra_groups
        text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
        return text, featured_rows, relay_featured_rows

    # Fallback (bare link list / no groups in source): minimal output.
    doc_proxies = list(main_renamed) + relay_proxies + chain_yaml_proxies
    groups = list(extra_groups)
    groups.append({"name": "全部节点", "type": "select",
                   "proxies": [str(n.get("name", "")) for n in main_renamed]})

    doc = {"proxies": doc_proxies, "proxy-groups": groups}
    text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    return text, featured_rows, relay_featured_rows
