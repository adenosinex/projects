#!/usr/bin/env python3
"""Featured-node watchdog (multi-subscription).

Runs every 5 minutes from a LaunchAgent (StartInterval 300) and
probes each ENABLED subscription's 3 published 精选 nodes' ACTUAL
DOWNLOAD SPEED — not latency (the phone client's url-test already
covers latency; the server-side watchdog's job is speed).

Which subscriptions are monitored is opt-in per subscription and
stored in data/featured_watch_config.json:
    {"enabled": ["419387b4d5dc"]}
Default: only the vps subscription (419387b4d5dc). vyy and 渔云
are OFF until the user enables them on the 精选维护 page.
The watchdog reads the config each run, so enabling/disabling
takes effect within one interval.

Probe (lightweight): one mihomo worker per featured node, on
dedicated ports 17920-17922 / 19120-19122 (no overlap with the full
test's 17910-17919 / 19110-19119 or proxy-test's 17900-17909), a
delay pre-check (5s) then a capped 5MB Cloudflare download (15s).
Speed comes from curl's speed_download (bytes / time). The mihomo
config is built from the live converted YAML's own proxy list, so
dialer-proxy references resolve and the probe tests exactly the
published (speed-suffixed) node names.

Why 5MB, not 2MB: the 2MB probe systematically underestimates
speed over this ~213ms-RTT link — TCP slow-start dominates a
short transfer, so nodes the full 10MB test measures at 5.4-6.8
MB/s came back at only 0.6-0.8 MB/s in the 2MB probe, leaving
almost no margin above the 0.5 MB/s threshold (a healthy 1 MB/s
node would read ~0.5 and be flagged bad). 5MB is still half the
full test and skips Google/YouTube, but the ramp-up is a smaller
fraction, so healthy nodes read well above the threshold and
genuinely slow nodes read below it.

A node is "bad" when its probed speed is below BAD_MBPS (0.5 MB/s)
or the probe fails (delay pre-check failed / connection error /
timeout). A non-200 HTTP code other than 0 (e.g. 429 from the
Cloudflare endpoint) is INCONCLUSIVE — an endpoint rate-limit, not
a node failure — and neither increments nor resets the count. A
node counts as bad only after BAD_STREAK (2) consecutive bad
probes (debounce against single blips).

LAZY PUBLISH: 1-2 bad nodes do NOT rewrite the subscription — the
phone client's url-test auto-switches among the 3 精选 members, so
the user can still use the remaining good node(s). Only when ALL 3
published 精选 nodes are bad (each confirmed by 2 consecutive bad
probes) are they replaced with the best available replacements from
THAT subscription's own last full test (ranked by download speed,
excluding the bad nodes and any node already known bad, preferring
at most 1 node per (server, protocol) group when enough candidates
exist) and the YAML is rebuilt + published immediately via the
existing build_yaml path (with a featured_override). For a
non-template subscription the rebuild goes through the same
template-cleansing path as a normal test, so rules/dns stay
identical to the VPS template. After publishing, the bad counts
reset for the new 精选.

If no viable replacement exists, nothing is published — a warning
is logged and the current 精选 stays in place.

Safety:
- The whole round is skipped (deferred) while a full test holds
  the shared fcntl lock, so the watchdog never races the manual
  test or the hourly cron. The publish re-acquires the lock
  non-blocking and re-reads the meta; if the featured changed
  since the round started (a full test finished in between), the
  publish aborts — the fresh hourly rebuild wins.
- A watchdog-only lock (data/.watch.lock) prevents two rounds
  from overlapping.
- State (monitored 精选 list, consecutive-bad counts, nodes
  confirmed bad) persists per subscription in
  data/featured_watch.json and survives restarts. When the hourly
  test publishes a fresh 精选, the watchdog follows it (counts
  reset for the new list); when a new full test runs, the
  known-bad set is cleared (fresh measurements supersede watchdog
  knowledge). The old flat (vps-only) state file is migrated
  automatically into the per-subscription structure.
- The probe restores runner's port globals after each round, so
  calling run_round() in-process (from the app's manual-probe
  endpoint) cannot shift the full test onto the watchdog's ports.

Test hooks (never set by the LaunchAgent):
- Env FEATURED_WATCH_FORCE_BAD="name1,name2" forces those
  published 精选 names to be treated as bad (for simulating the
  1-bad / 2-bad / all-3-bad cases without touching live data).
- --dry-run probes and decides but does not publish (it still
  updates the state file's probe records; restore the state file
  after a simulation if the counts must not leak into live runs).
- --sub <id> runs a round for one subscription only.

Called as:
  /Users/zin/sub-convert/venv/bin/python /Users/zin/sub-convert/featured_watch.py [--sub <id>] [--dry-run] [--json]
"""
import argparse
import copy
import fcntl
import json
import logging
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml  # noqa: E402

import convert as converter  # noqa: E402
import parser as subparser  # noqa: E402
import runner  # noqa: E402
import store  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
VPS_ID = converter.TEMPLATE_SUB_ID  # "419387b4d5dc" — the template sub

# Probe settings.
BAD_MBPS = 0.5            # below this download speed a node is "bad"
BAD_STREAK = 2            # consecutive bad probes before a node counts bad
PROBE_BYTES = 5_000_000   # capped probe download (5MB, not the full 10MB)
PROBE_TIMEOUT = 15        # seconds for the probe download
CFDL = "https://speed.cloudflare.com/__down?bytes=%d" % PROBE_BYTES
PROBE_INTERVAL_S = 900    # LaunchAgent StartInterval (15 min) -- traffic cap 100GB/mo

# Dedicated mihomo worker ports for the watchdog. No overlap with the
# full test (17910-17919 / 19110-19119) or proxy-test (17900-17909).
WATCH_MIXED_BASE = 17920
WATCH_CTRL_BASE = 19120
WATCH_RUN_DIR = os.path.join(BASE, "run", "watchdog")
WATCH_LOCK_PATH = os.path.join(BASE, "data", ".watch.lock")

STATE_PATH = os.path.join(BASE, "data", "featured_watch.json")
CONFIG_PATH = os.path.join(BASE, "data", "featured_watch_config.json")
DEFAULT_ENABLED = [VPS_ID]

log = logging.getLogger("featured-watch")

_SUFFIX_RE = re.compile(r" \d+\.\dMB$")


def _fmt_speed(speed_bps):
    if isinstance(speed_bps, (int, float)) and speed_bps > 0:
        return "%.1fMB/s" % (speed_bps / 1_000_000)
    return "-"


# ------------------------------------------------------------------ config

def load_config():
    """Enabled-subscription set. Missing/corrupt file -> default
    (vps only). Only ids that are real subscriptions are kept."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        enabled = cfg.get("enabled")
        if isinstance(enabled, list):
            known = {m["id"] for m in store.list_subs()}
            out = [str(x) for x in enabled if str(x) in known]
            if out:
                return {"enabled": out}
    except Exception:
        pass
    return {"enabled": list(DEFAULT_ENABLED)}


def save_config(enabled):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"enabled": list(enabled)}, f, ensure_ascii=False,
                  indent=1)
    os.replace(tmp, CONFIG_PATH)


# ------------------------------------------------------------------ state

def _fresh_sub_state(src=None):
    base = {"monitored": [], "bad_counts": {}, "known_bad": {},
            "last_probe_at": None, "last_probe": {},
            "last_publish_at": None, "last_action": None,
            "last_test_at": None}
    if isinstance(src, dict):
        for k in base:
            if k in src:
                base[k] = src[k]
    return base


def load_state():
    """Per-subscription state: {"version": 2, "subs": {id: {...}}}.
    Migrates the old flat (vps-only) file into subs[VPS_ID]."""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    if not isinstance(st, dict):
        st = {}
    if "subs" not in st and "monitored" in st:
        st = {"version": 2, "subs": {VPS_ID: _fresh_sub_state(st)}}
    st.setdefault("version", 2)
    subs = st.get("subs")
    if not isinstance(subs, dict):
        st["subs"] = {}
    return st


def sub_state(state, sub_id):
    """The state dict for one subscription (created if missing)."""
    subs = state.setdefault("subs", {})
    st = subs.get(sub_id)
    if not isinstance(st, dict):
        st = _fresh_sub_state()
        subs[sub_id] = st
    for k, v in _fresh_sub_state().items():
        st.setdefault(k, v)
    return st


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


# ------------------------------------------------------------------ names

def yaml_name_to_test_name(yaml_name, known_test_names):
    """Strip the ' X.YMB' speed suffix to get the test (pre-suffix)
    name. Falls back to the name as-is if the stripped name is not a
    known test name."""
    base = _SUFFIX_RE.sub("", yaml_name)
    if base in known_test_names:
        return base
    return yaml_name


# ------------------------------------------------------------------ probe

def _probe_one(proxy_url, ctrl_port, name):
    """Lightweight probe for one node: delay pre-check, then a capped
    5MB download. Returns {"speed_bps", "code", "status"} with status
    "good" | "bad" | "inconclusive" | "error".
    - good: HTTP 200 and speed >= BAD_MBPS.
    - bad: delay pre-check failed, HTTP 0 (connection failed), or
      HTTP 200 with speed < BAD_MBPS.
    - inconclusive: non-200 HTTP code other than 0 (e.g. 429 rate
      limit from the endpoint — not a node failure).
    - error: the worker/mihomo itself failed (not the node); the
      caller leaves the bad count unchanged.
    """
    try:
        runner.switch_proxy(ctrl_port, name)
    except Exception as e:
        return {"speed_bps": None, "code": None, "status": "error",
                "error": "switch failed: %s" % e}
    time.sleep(0.3)
    d = runner.delay_test(proxy_url)
    if not d.get("alive"):
        return {"speed_bps": None, "code": d.get("code"),
                "status": "bad", "error": "delay pre-check failed"}
    dl = runner._curl(proxy_url, CFDL, PROBE_TIMEOUT)
    code = dl.get("code") or 0
    sp = dl.get("speed_bps")
    if code == 200:
        if isinstance(sp, (int, float)) and sp >= BAD_MBPS * 1_000_000:
            return {"speed_bps": sp, "code": code, "status": "good"}
        return {"speed_bps": sp, "code": code, "status": "bad"}
    if code == 0:
        return {"speed_bps": sp, "code": code, "status": "bad",
                "error": "download connection failed"}
    return {"speed_bps": sp, "code": code, "status": "inconclusive",
            "error": "HTTP %s from download endpoint" % code}


def probe_featured(yaml_proxies, featured_names):
    """Probe the featured nodes in parallel (one mihomo worker each).
    yaml_proxies: the full proxy list from the live converted YAML
    (so dialer-proxy references resolve). featured_names: the 3 YAML
    (speed-suffixed) names to probe. Returns {name: probe_result}.
    Restores runner's port globals afterwards so an in-process call
    (from the app) cannot shift the full test onto these ports."""
    if not yaml_proxies:
        return {nm: {"speed_bps": None, "code": None, "status": "error",
                     "error": "no proxies in live YAML"}
                for nm in featured_names}
    proxies = runner._dedupe_names(copy.deepcopy(yaml_proxies))
    old_mixed, old_ctrl = runner.BASE_MIXED_PORT, runner.BASE_CTRL_PORT
    runner.BASE_MIXED_PORT = WATCH_MIXED_BASE
    runner.BASE_CTRL_PORT = WATCH_CTRL_BASE
    os.makedirs(WATCH_RUN_DIR, exist_ok=True)
    runner._kill_stray(WATCH_RUN_DIR)
    procs = []
    try:
        for wid in range(len(featured_names)):
            runner.build_config([dict(p) for p in proxies],
                                WATCH_RUN_DIR, wid)
        for wid in range(len(featured_names)):
            procs.append(runner.start_mihomo(
                WATCH_RUN_DIR, wid, expect_name=proxies[0]["name"]))
        results = {}
        lock = threading.Lock()

        def worker(wid, name):
            mixed, ctrl = runner._ports(wid)
            proxy_url = "http://127.0.0.1:%d" % mixed
            try:
                r = _probe_one(proxy_url, ctrl, name)
            except Exception as e:
                r = {"speed_bps": None, "code": None, "status": "error",
                     "error": "worker error: %s" % e}
            with lock:
                results[name] = r

        threads = [threading.Thread(target=worker, args=(wid, nm),
                                    daemon=True)
                   for wid, nm in enumerate(featured_names)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results
    finally:
        for proc, logf in procs:
            runner.stop_mihomo(proc, logf)
        runner.BASE_MIXED_PORT = old_mixed
        runner.BASE_CTRL_PORT = old_ctrl


# -------------------------------------------------------------- replacements

def pick_replacements(last_results, nodes_by_test_name,
                      exclude_test_names, count=3):
    """Best replacements from the last full test. Candidates: direct
    main results with status ok/slow and download speed >= BAD_MBPS,
    excluding the given test names. Ranked by speed desc. Diversity:
    at most 1 per (server, type) group on the first pass; a second
    pass fills any remaining slots (soft preference). Returns a list
    of test names (up to count)."""
    cands = []
    for r in (last_results or []):
        if r.get("kind") != "direct" or r.get("role") != "main":
            continue
        if r.get("status") not in ("ok", "slow"):
            continue
        sp = r.get("download_bps")
        if not isinstance(sp, (int, float)) or sp < BAD_MBPS * 1_000_000:
            continue
        nm = r.get("name", "")
        if not nm or nm in exclude_test_names:
            continue
        cands.append((sp, nm))
    cands.sort(reverse=True)

    def group_key(nm):
        n = nodes_by_test_name.get(nm) or {}
        return (str(n.get("server", "")), str(n.get("type", "")))

    picked = []
    used_groups = set()
    for _sp, nm in cands:
        if len(picked) >= count:
            break
        g = group_key(nm)
        if g in used_groups:
            continue
        picked.append(nm)
        used_groups.add(g)
    if len(picked) < count:
        for _sp, nm in cands:
            if len(picked) >= count:
                break
            if nm not in picked:
                picked.append(nm)
    return picked


# ------------------------------------------------------------------ rebuild

def rebuild_with_featured(meta, sub_id, featured_test_names):
    """Rebuild the subscription's YAML with an overridden 精选 (test
    names). Reconstructs build_yaml's inputs from the stored last
    full test and calls build_yaml with featured_override. For a
    non-template subscription the VPS template doc is passed, so the
    rebuild goes through the same template-cleansing path as a
    normal test. Returns (yaml_text, featured_rows, relay_rows).
    Caller must hold the test lock."""
    nodes = store.get_sub_nodes(sub_id)
    if nodes is None:
        raise RuntimeError("nodes not found for sub " + sub_id)
    aux_id = meta.get("aux_sub_id") or ""
    aux_all = store.get_sub_nodes(aux_id) if aux_id else []
    if aux_all is None:
        aux_all = []
    last = meta.get("last_results") or []
    # Reconstruct build_yaml's result inputs from the compacted
    # last_results (the full test's per-node results).
    direct = []
    chains = []
    for r in last:
        rec = {"name": r.get("name", ""), "status": r.get("status", ""),
               "delay": {"time_s": (r["delay_ms"] / 1000.0
                                    if isinstance(r.get("delay_ms"),
                                                  (int, float)) else None)},
               "download": {"speed_bps": r.get("download_bps")}}
        if r.get("kind") == "chain":
            rec["relay"] = r.get("relay", "")
            rec["exit"] = r.get("exit", "")
            chains.append(rec)
        else:
            rec["role"] = r.get("role", "")
            direct.append(rec)
    # Chain proxy dicts (unsuffixed, as run_convert_flow builds them).
    main_by_name = {n.get("name", ""): n
                    for n in converter.dedupe_names(nodes)}
    chain_proxies = []
    for r in chains:
        exit_node = main_by_name.get(r.get("exit", ""))
        if exit_node is None:
            continue
        p = dict(exit_node)
        p["name"] = "%s->%s" % (r.get("relay", ""), r.get("exit", ""))
        p["dialer-proxy"] = r.get("relay", "")
        chain_proxies.append(p)
    # Source doc (kept for the template sub itself; for other subs it
    # supplies the original groups) + the VPS template doc for
    # non-template subs (same as run_test_for_sub in app.py).
    source_doc = None
    raw = store.get_sub_raw(sub_id)
    if raw:
        try:
            source_doc = subparser.extract_yaml_doc(raw)
        except Exception:
            source_doc = None
    template_doc = None
    if sub_id != converter.TEMPLATE_SUB_ID:
        try:
            traw = store.get_sub_raw(converter.TEMPLATE_SUB_ID)
            if traw:
                template_doc = subparser.extract_yaml_doc(traw)
        except Exception:
            template_doc = None
    text, featured_rows, relay_rows = converter.build_yaml(
        nodes, aux_all, direct, chains, chain_proxies,
        source_doc=source_doc, template_doc=template_doc, sub_id=sub_id,
        featured_override=featured_test_names)
    return text, featured_rows, relay_rows


def _load_yaml_proxies(sub_id):
    """(proxies, 精选 members) from the live converted YAML, or
    (None, None) if it cannot be read."""
    try:
        with open(store.converted_path(sub_id), encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except Exception as e:
        log.error("cannot read live converted YAML: %s", e)
        return None, None
    proxies = (doc or {}).get("proxies") or []
    featured = []
    for g in (doc or {}).get("proxy-groups") or []:
        if isinstance(g, dict) and g.get("name") == "精选":
            featured = list(g.get("proxies") or [])
            break
    return proxies, featured


def publish(round_featured_names, featured_test_names, sub_id):
    """Rebuild + write the YAML with the replacement 精选, under the
    test lock. Re-reads the meta after acquiring the lock and aborts
    if the featured changed since the round started (a full test
    finished in between — the fresh hourly rebuild wins). Returns
    (ok, featured_rows)."""
    try:
        with store.TestLock():
            fresh = store.get_sub_meta(sub_id)
            if fresh is None:
                log.error("publish: subscription %s vanished", sub_id)
                return False, None
            fresh_names = [f.get("name", "")
                           for f in (fresh.get("featured") or [])]
            if set(fresh_names) != set(round_featured_names):
                log.info("publish aborted: featured changed since round "
                         "start (full test rebuilt?) %s -> %s",
                         round_featured_names, fresh_names)
                return False, None
            text, featured_rows, _relay_rows = rebuild_with_featured(
                fresh, sub_id, featured_test_names)
            with open(store.converted_path(sub_id), "w",
                      encoding="utf-8") as f:
                f.write(text)
            store.update_sub_meta(
                sub_id, featured=featured_rows,
                featured_watch={
                    "swapped_at": store.now_iso(),
                    "replacements": [r["name"] for r in featured_rows],
                    "reason": "all 3 featured bad (2 consecutive probes)"})
            return True, featured_rows
    except BlockingIOError:
        log.info("publish deferred: a full test is running")
        return False, None


# -------------------------------------------------------------------- round

class WatchLock:
    """Watchdog-only lock so two rounds never overlap."""

    def __init__(self):
        self._f = None

    def __enter__(self):
        self._f = open(WATCH_LOCK_PATH, "w")
        fcntl.flock(self._f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._f, fcntl.LOCK_UN)
            self._f.close()
        except Exception:
            pass
        self._f = None
        return False


def _featured_result(sub_id, meta, sst, action, new_featured=None):
    """Structured round result (for the API + --json)."""
    rows = []
    for f in (meta.get("featured") or []):
        nm = f.get("name", "")
        probe = (sst.get("last_probe") or {}).get(nm) or {}
        sp = probe.get("speed_bps")
        rows.append({
            "name": nm,
            "full_speed_mb": f.get("speed_mb"),
            "delay_ms": f.get("delay_ms"),
            "probe_speed_bps": sp if isinstance(sp, (int, float)) else None,
            "probe_speed_mb": (round(sp / 1_000_000, 1)
                               if isinstance(sp, (int, float)) else None),
            "probe_status": probe.get("status"),
            "probe_code": probe.get("code"),
            "bad_count": (sst.get("bad_counts") or {}).get(nm, 0),
            "confirmed_bad": (sst.get("bad_counts") or {}).get(nm, 0)
            >= BAD_STREAK,
        })
    return {
        "sub_id": sub_id,
        "action": action,
        "featured": rows,
        "published": action == "published",
        "new_featured": ([r["name"] for r in new_featured]
                         if new_featured else None),
        "last_probe_at": sst.get("last_probe_at"),
        "last_publish_at": sst.get("last_publish_at"),
        "last_action": sst.get("last_action"),
    }


def run_round(sub_id, force_bad=None, dry_run=False):
    """One watchdog round for one subscription. Caller must hold the
    WatchLock. force_bad: set of YAML names to treat as bad (test
    hook only). Returns a result dict (see _featured_result)."""
    if store.lock_held():
        log.info("[%s] defer: a full test is running, skipping "
                 "this round", sub_id)
        return {"sub_id": sub_id, "action": "deferred",
                "featured": [], "published": False,
                "new_featured": None, "last_probe_at": None,
                "last_publish_at": None, "last_action": "deferred"}
    meta = store.get_sub_meta(sub_id)
    if meta is None:
        log.error("[%s] subscription not found", sub_id)
        return {"sub_id": sub_id, "action": "error",
                "featured": [], "published": False,
                "new_featured": None, "last_probe_at": None,
                "last_publish_at": None, "last_action": "error"}
    featured = meta.get("featured") or []
    if not featured:
        log.warning("[%s] no featured nodes in meta; nothing to "
                    "monitor", sub_id)
        return {"sub_id": sub_id, "action": "no-featured",
                "featured": [], "published": False,
                "new_featured": None, "last_probe_at": None,
                "last_publish_at": None, "last_action": "no-featured"}
    featured_names = [f.get("name", "") for f in featured]
    state = load_state()
    sst = sub_state(state, sub_id)
    # A fresh full test supersedes watchdog knowledge.
    if meta.get("last_test_at") != sst.get("last_test_at"):
        sst["known_bad"] = {}
        sst["last_test_at"] = meta.get("last_test_at")
    # Follow a fresh hourly rebuild of the featured list.
    if set(sst.get("monitored") or []) != set(featured_names):
        if sst.get("monitored"):
            log.info("[%s] featured changed (%s -> %s); following the "
                     "new list", sub_id, sst["monitored"], featured_names)
        sst["monitored"] = list(featured_names)
        sst["bad_counts"] = {n: 0 for n in featured_names}
    # Test-name universe (for suffix stripping + replacement lookup).
    nodes = store.get_sub_nodes(sub_id) or []
    real = subparser.filter_real_nodes(nodes)
    nodes_by_test_name = {n.get("name", ""): n
                          for n in converter.dedupe_names(real)}
    known_test_names = set(nodes_by_test_name)
    for r in (meta.get("last_results") or []):
        if r.get("kind") == "direct" and r.get("role") == "main":
            known_test_names.add(r.get("name", ""))
    # Probe the published 精选.
    yaml_proxies, yaml_featured = _load_yaml_proxies(sub_id)
    if yaml_proxies is None:
        log.error("[%s] cannot read the live converted YAML; skipping",
                  sub_id)
        return {"sub_id": sub_id, "action": "error",
                "featured": [], "published": False,
                "new_featured": None, "last_probe_at": None,
                "last_publish_at": None, "last_action": "error"}
    if yaml_featured and set(yaml_featured) != set(featured_names):
        log.warning("[%s] meta featured %s != YAML 精选 group %s; "
                    "using meta", sub_id, featured_names, yaml_featured)
    results = probe_featured(yaml_proxies, featured_names)
    if force_bad:
        for nm in force_bad:
            if nm in results:
                results[nm] = {"speed_bps": 0, "code": 0, "status": "bad",
                               "error": "forced bad (test hook)"}
    sst["last_probe_at"] = store.now_iso()
    sst["last_probe"] = {
        n: {"speed_bps": (results.get(n) or {}).get("speed_bps"),
            "code": (results.get(n) or {}).get("code"),
            "status": (results.get(n) or {}).get("status")}
        for n in featured_names}
    # Update consecutive-bad counts.
    for nm in featured_names:
        r = results.get(nm) or {}
        st = r.get("status")
        if st == "good":
            sst["bad_counts"][nm] = 0
        elif st == "bad":
            sst["bad_counts"][nm] = sst["bad_counts"].get(nm, 0) + 1
            if sst["bad_counts"][nm] >= BAD_STREAK:
                tname = yaml_name_to_test_name(nm, known_test_names)
                sst.setdefault("known_bad", {})[tname] = store.now_iso()
        # inconclusive / error: leave the count unchanged
        extra = (" (%s)" % r["error"]) if r.get("error") else ""
        log.info("[%s] probe %-42s %-12s speed=%-9s code=%s bad_count=%d%s",
                 sub_id, nm, st, _fmt_speed(r.get("speed_bps")),
                 r.get("code"), sst["bad_counts"].get(nm, 0), extra)
    save_state(state)
    bad = [n for n in featured_names
           if sst["bad_counts"].get(n, 0) >= BAD_STREAK]
    if len(bad) < len(featured_names):
        log.info("[%s] %d/%d featured bad (confirmed); lazy: no "
                 "publish, client url-test covers the rest",
                 sub_id, len(bad), len(featured_names))
        sst["last_action"] = "no-publish %d/%d bad" % (
            len(bad), len(featured_names))
        save_state(state)
        return _featured_result(sub_id, meta, sst, "no-publish")
    # All featured bad — pick replacements.
    log.warning("[%s] all %d featured nodes bad (confirmed); picking "
                "replacements", sub_id, len(featured_names))
    exclude = {yaml_name_to_test_name(n, known_test_names) for n in bad}
    exclude |= set(sst.get("known_bad") or {})
    replacements = pick_replacements(meta.get("last_results") or [],
                                     nodes_by_test_name, exclude)
    if not replacements:
        log.warning("[%s] all featured bad but NO viable replacement; "
                    "leaving the YAML in place", sub_id)
        sst["last_action"] = "no-replacement"
        save_state(state)
        return _featured_result(sub_id, meta, sst, "no-replacement")
    log.info("[%s] replacements: %s", sub_id, replacements)
    if dry_run:
        log.info("[%s] dry-run: not publishing", sub_id)
        sst["last_action"] = "dry-run would-publish %s" % replacements
        save_state(state)
        return _featured_result(sub_id, meta, sst, "dry-run")
    ok, new_featured = publish(featured_names, replacements, sub_id)
    if ok:
        sst["monitored"] = [r["name"] for r in new_featured]
        sst["bad_counts"] = {r["name"]: 0 for r in new_featured}
        sst["last_publish_at"] = store.now_iso()
        sst["last_action"] = "published replacements %s" % (
            [r["name"] for r in new_featured])
        save_state(state)
        log.info("[%s] published replacement 精选: %s", sub_id,
                 [r["name"] for r in new_featured])
        fresh_meta = store.get_sub_meta(sub_id) or meta
        return _featured_result(sub_id, fresh_meta, sst, "published",
                                new_featured=new_featured)
    sst["last_action"] = "publish-deferred"
    save_state(state)
    return _featured_result(sub_id, meta, sst, "publish-deferred")


def main(argv=None):
    # Daytime only (2026-11-18, user): probes run 08:00-23:00 only.
    import datetime as _dt
    _h = _dt.datetime.now().hour
    if _h < 8 or _h >= 23:
        print(f"watchdog: outside 08:00-23:00 (hour={_h}), skip")
        return 0
    ap = argparse.ArgumentParser(description="featured-node watchdog")
    ap.add_argument("--sub", default="",
                    help="run a round for one subscription only "
                         "(default: all enabled subscriptions)")
    ap.add_argument("--dry-run", action="store_true",
                    help="probe and decide, but do not write the YAML "
                         "or meta")
    ap.add_argument("--json", action="store_true",
                    help="print the round result(s) as JSON")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    force_bad = set()
    env = os.environ.get("FEATURED_WATCH_FORCE_BAD", "")
    if env:
        force_bad = {s.strip() for s in env.split(",") if s.strip()}
        log.warning("TEST HOOK: forcing bad: %s", sorted(force_bad))
    if args.sub:
        targets = [args.sub]
    else:
        targets = load_config().get("enabled", [])
    results = []
    try:
        with WatchLock():
            for sub_id in targets:
                if store.get_sub_meta(sub_id) is None:
                    log.warning("subscription %s not found; skipping",
                                sub_id)
                    continue
                r = run_round(sub_id, force_bad=force_bad,
                              dry_run=args.dry_run)
                log.info("[%s] round done: %s", sub_id, r["action"])
                results.append(r)
    except BlockingIOError:
        log.info("another watchdog round is running; exiting")
        return 0
    except Exception:
        log.exception("watchdog round failed")
        return 1
    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
