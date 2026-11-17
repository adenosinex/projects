"""Parallel test runner for sub-convert.

Copied from proxy-test's runner.py (same proven approach: one mihomo
instance per run, select group, curl through the mixed port) and adapted:

- PARALLEL: up to MAX_WORKERS mihomo instances run at once, each on its
  own mixed-port / controller port (base 17910/19110 + worker id). A
  shared queue feeds proxy names to the workers; each worker switches
  its own mihomo to the proxy it is testing. This parallelises the
  download phase (the bottleneck) instead of testing one node at a time.
  max_workers=1 is the sequential fallback (one mihomo, one node at a
  time). Default is 10 (A/B-verified on proxy-test 2026-11-13: 10
  workers were 9x faster than sequential with no systematic speed
  distortion — per-node speed ratio median 1.01).
- SLOW-SPEED ABORT: the download curl uses --speed-limit + --speed-time.
  If the speed stays below SLOW_LIMIT_BPS for SLOW_TIME_S seconds (or no
  data arrives), curl aborts early and the node is marked "slow"
  (circuit-breaker) with the observed speed — it does not wait out the
  full 45s timeout. A slow node costs ~5s, not 45s.
- Circuit breaker (same as proxy-test): delay pre-check (5s) first; on
  failure the proxy's Google/YouTube/download are skipped. Dead relay
  skips its chains; dead exit+protocol skips its chains.

Ports: 17910-17919 / 19110-19119 are sub-convert's own range. They do
NOT overlap proxy-test's 17900-17909 / 19100-19109, sub-node's
7890/9090, or mac-compute's 5850. Overlapping ranges are dangerous: if
another process holds a worker's ports, that worker's mihomo fails to
bind but the readiness check can be fooled by the other process
answering /version — the worker then fails every node it touches.

Robustness (learned from a real failure in proxy-test, 2026-11-13):
- Each worker gets its OWN -d directory (run_dir/worker-<id>/ with its
  own config.yaml, log and cache.db). Sharing one -d dir makes all
  workers fight over one cache.db.
- Controller-ownership check: after /version answers, start_mihomo
  fetches /proxies and verifies the TEST group contains our proxy name.
  If the controller belongs to another mihomo (stray process holding
  the port), it fails fast with a clear error instead of silently
  testing through the wrong instance.
- Dead-worker guard: if a worker's test raises (e.g. its mihomo died),
  the node is requeued once for a healthy worker and the dead worker
  stops — it must NOT keep draining the queue with instant failures.
  Leftover queued nodes are recorded as failed, never dropped.

Per-proxy result (same shape as proxy-test):
  {name, kind, status, delay{...}, google{...}, youtube{...},
   download{...}}
where each check is {code, time_s, speed_bps, status} and status is
"ok" | "failed" | "skipped" | "slow". For "slow", speed_bps holds the
observed speed before the abort (may be 0/None).
"""
import json
import os
import queue
import subprocess
import threading
import time
import urllib.request

import yaml

MIHOMO_BIN = "/opt/homebrew/bin/mihomo"
BASE_MIXED_PORT = 17910
BASE_CTRL_PORT = 19110
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
GEN204 = "http://www.gstatic.com/generate_204"
GOOGLE = "https://www.google.com/search?q=hello&hl=en"
YOUTUBE = "https://www.youtube.com/"
# Download-speed probe (user choice, 2026-11-15): GitHub, NOT Cloudflare.
# speed.cloudflare.com/__down was returning HTTP 429 on every hit
# (2026-11-15 15:37, 3/3 rounds), which zeroed the speed of working
# nodes. The git repo zip is a real ~15 MB file served by GitHub's CDN;
# it 302-redirects to codeload.github.com, so full_test must follow it.
DL_URL = "https://github.com/git/git/archive/refs/heads/master.zip"

DELAY_TIMEOUT = 5
GOOGLE_TIMEOUT = 20
YOUTUBE_TIMEOUT = 30
DL_TIMEOUT = 45

# Parallelism + slow abort (user requirements).
MAX_WORKERS = 10
SLOW_LIMIT_BPS = 51200      # 50 KB/s — below this, abort the download
SLOW_TIME_S = 5             # sustained below-limit window before abort


def _ports(worker_id):
    return (BASE_MIXED_PORT + worker_id, BASE_CTRL_PORT + worker_id)


def _worker_dir(run_dir, worker_id):
    d = os.path.join(run_dir, "worker-%d" % worker_id)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- mihomo

def _dedupe_names(proxies):
    """Return copies with unique names (duplicates get a '#N' suffix).
    Input list is not mutated."""
    seen = {}
    out = []
    for p in proxies:
        q = dict(p)
        n = q["name"]
        if n in seen:
            seen[n] += 1
            q["name"] = "%s#%d" % (n, seen[n])
        else:
            seen[n] = 1
        out.append(q)
    return out


def build_config(proxies, run_dir, worker_id):
    """proxies: list of mihomo proxy dicts (unique 'name').
    Writes config.yaml into this worker's own directory."""
    seen = {}
    for p in proxies:
        n = p["name"]
        if n in seen:
            seen[n] += 1
            p["name"] = "%s#%d" % (n, seen[n])
        else:
            seen[n] = 1
    mixed, ctrl = _ports(worker_id)
    cfg = {
        "mixed-port": mixed,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "warning",
        "ipv6": True,
        "external-controller": "127.0.0.1:%d" % ctrl,
        "unified-delay": True,
        "proxies": proxies,
        "proxy-groups": [
            {"name": "TEST", "type": "select",
             "proxies": [p["name"] for p in proxies]},
        ],
        "rules": ["MATCH,TEST"],
    }
    wdir = _worker_dir(run_dir, worker_id)
    path = os.path.join(wdir, "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return path


def _kill_stray(run_dir):
    # pkill -f matches the full command line; "mihomo -d <run_dir>" is a
    # prefix of every worker's "-d <run_dir>/worker-<id>", so one call
    # covers all workers of this run (and only this run).
    try:
        subprocess.run(["pkill", "-f", "mihomo -d " + run_dir],
                       capture_output=True, timeout=10)
    except Exception:
        pass
    time.sleep(0.5)


def _controller_serves(ctrl_port, expect_name):
    """True if the controller on ctrl_port serves OUR config: the TEST
    group exists and contains expect_name. Raises ValueError if the
    controller answers but serves a different config (wrong process
    holding the port). Returns False if it is not up yet / TEST is not
    loaded yet (caller keeps polling)."""
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/proxies" % ctrl_port,
                timeout=2) as r:
            data = json.load(r)
    except Exception:
        return False
    groups = (data or {}).get("proxies") or {}
    test = groups.get("TEST") or {}
    members = test.get("all") or []
    if not members:
        return False  # config not loaded yet (or group missing); retry
    if expect_name is not None and expect_name not in members:
        raise ValueError(
            "controller on port %d serves a different config "
            "(TEST group lacks %r) — port conflict with another mihomo?"
            % (ctrl_port, expect_name))
    return True


def start_mihomo(run_dir, worker_id, expect_name=None):
    mixed, ctrl = _ports(worker_id)
    wdir = _worker_dir(run_dir, worker_id)
    log_path = os.path.join(wdir, "mihomo.log")
    logf = open(log_path, "w")
    proc = subprocess.Popen(
        [MIHOMO_BIN, "-d", wdir, "-f",
         os.path.join(wdir, "config.yaml")],
        stdout=logf, stderr=subprocess.STDOUT)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/version" % ctrl, timeout=2) as r:
                if r.status == 200 and _controller_serves(ctrl, expect_name):
                    return proc, logf
        except ValueError:
            # Wrong controller on this port — fail fast, do not wait.
            stop_mihomo(proc, logf)
            raise
        except Exception:
            pass
        time.sleep(0.5)
    stop_mihomo(proc, logf)
    raise RuntimeError("mihomo worker %d controller did not come up"
                       % worker_id)


def stop_mihomo(proc, logf):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        logf.close()
    except Exception:
        pass


def switch_proxy(ctrl_port, name):
    data = json.dumps({"name": name}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d/proxies/TEST" % ctrl_port, data=data,
        headers={"Content-Type": "application/json"}, method="PUT")
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


# ---------------------------------------------------------------- curl

def _curl(proxy_url, url, timeout, follow=False, ua=False,
          slow_limit=None, slow_time=None):
    """curl through proxy_url. Returns {code, time_s, speed_bps, status,
    curl_exit}. When slow_limit/slow_time are given (download only),
    curl aborts if speed stays below slow_limit for slow_time seconds;
    that comes back as status "slow" with the observed speed_bps."""
    cmd = ["curl", "-s", "-x", proxy_url, "--max-time", str(timeout),
           "-o", "/dev/null",
           "-w", "code=%{http_code} time=%{time_total} speed=%{speed_download}"]
    if follow:
        cmd.append("-L")
    if ua:
        cmd += ["-A", UA]
    if slow_limit is not None and slow_time is not None:
        cmd += ["--speed-limit", str(slow_limit),
                "--speed-time", str(slow_time)]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout + 10)
        out = proc.stdout.strip()
        curl_exit = proc.returncode
    except Exception:
        return {"code": 0, "time_s": None, "speed_bps": None,
                "status": "failed", "curl_exit": None}
    kv = {}
    for part in out.split():
        if "=" in part:
            k, v = part.split("=", 1)
            kv[k] = v
    try:
        code = int(kv.get("code", 0))
    except ValueError:
        code = 0
    try:
        t = float(kv.get("time", 0))
    except ValueError:
        t = None
    try:
        sp = float(kv.get("speed", 0))
    except ValueError:
        sp = None
    # curl exit 28 = timeout / "operation too slow" (speed-limit abort).
    # If the download was aborted for slowness, the speed we observed is
    # real data — report it as "slow", not as a generic failure.
    if slow_limit is not None and curl_exit == 28:
        return {"code": code, "time_s": t, "speed_bps": sp,
                "status": "slow", "curl_exit": curl_exit}
    return {"code": code, "time_s": t, "speed_bps": sp,
            "status": "ok" if code else "failed", "curl_exit": curl_exit}


def delay_test(proxy_url):
    r = _curl(proxy_url, GEN204, DELAY_TIMEOUT)
    r["alive"] = (r["code"] == 204)
    return r


def full_test(proxy_url):
    """Google + YouTube + download. Caller must have passed delay first.
    Download uses the slow-speed abort."""
    g = _curl(proxy_url, GOOGLE, GOOGLE_TIMEOUT, follow=True, ua=True)
    y = _curl(proxy_url, YOUTUBE, YOUTUBE_TIMEOUT, ua=True)
    d = _curl(proxy_url, DL_URL, DL_TIMEOUT, follow=True,
              slow_limit=SLOW_LIMIT_BPS, slow_time=SLOW_TIME_S)
    return {"google": g, "youtube": y, "download": d}


def skipped(reason):
    return {"status": "skipped", "reason": reason,
            "code": None, "time_s": None, "speed_bps": None}


def _failed_stub(name, reason):
    return {"name": name, "status": "failed",
            "delay": {"code": 0, "time_s": None, "speed_bps": None,
                      "status": "failed", "error": reason},
            "google": skipped(reason),
            "youtube": skipped(reason),
            "download": skipped(reason)}


def _run_one(proxy_url, ctrl_port, name, skip_reason=None):
    """Full per-proxy test with circuit breaker."""
    if skip_reason:
        return {"name": name, "status": "skipped",
                "delay": skipped(skip_reason),
                "google": skipped(skip_reason),
                "youtube": skipped(skip_reason),
                "download": skipped(skip_reason)}
    switch_proxy(ctrl_port, name)
    time.sleep(0.3)
    d = delay_test(proxy_url)
    if not d["alive"]:
        reason = "circuit-breaker: delay pre-check failed (code %s)" % d["code"]
        return {"name": name, "status": "failed",
                "delay": d,
                "google": skipped(reason),
                "youtube": skipped(reason),
                "download": skipped(reason)}
    rest = full_test(proxy_url)
    dl = rest["download"]
    # status: ok if anything returned data; "slow" if the download was
    # aborted for low speed (still a real measurement, not a failure).
    if dl["status"] == "slow":
        st = "slow"
    elif any(rest[k]["code"] for k in ("google", "youtube", "download")):
        st = "ok"
    else:
        st = "failed"
    return {"name": name, "status": st, "delay": d, **rest}


# ---------------------------------------------------------------- parallel

def run_parallel(run_dir, proxies, test_names=None, progress_cb=None,
                 max_workers=MAX_WORKERS, phase=""):
    """Test proxies in parallel across max_workers mihomo instances.
    `proxies` is the full list that goes into the mihomo config (so
    dialer-proxy references resolve). `test_names` (optional) restricts
    which proxies are actually tested — the rest are in the config only
    (e.g. relay nodes that a chain's dialer-proxy points at). Returns a
    list of per-proxy result dicts in the same order as `proxies` for
    the tested ones (untested proxies get None). max_workers=1 is the
    sequential fallback (one mihomo, one node at a time).

    progress_cb (optional) is called as progress_cb(phase, name, done,
    total, result) each time a proxy finishes, so the caller can render
    live progress and per-proxy results as they arrive."""
    proxies = _dedupe_names(proxies)
    expect = proxies[0]["name"] if proxies else None
    os.makedirs(run_dir, exist_ok=True)
    # Kill strays from a previous crashed run ONCE, before starting any
    # workers. (Do NOT do this per-worker: the pkill pattern matches the
    # run dir, so it would kill the sibling workers just started.)
    _kill_stray(run_dir)
    # one config + one mihomo per worker (same proxy list, own ports,
    # own -d directory)
    workers = []
    for wid in range(max_workers):
        build_config([dict(p) for p in proxies], run_dir, wid)
    procs = []
    try:
        for wid in range(max_workers):
            procs.append(start_mihomo(run_dir, wid, expect_name=expect))
        q = queue.Queue()
        names_to_test = test_names if test_names is not None else [
            p["name"] for p in proxies]
        for nm in names_to_test:
            q.put(nm)
        results = {}
        retries = {}
        lock = threading.Lock()
        done = {"n": 0}
        total = len(names_to_test)

        def worker(wid):
            mixed, ctrl = _ports(wid)
            proxy_url = "http://127.0.0.1:%d" % mixed
            while True:
                try:
                    name = q.get_nowait()
                except queue.Empty:
                    return
                try:
                    r = _run_one(proxy_url, ctrl, name)
                except Exception as e:
                    # Is this worker's mihomo still alive? If it died,
                    # requeue the node once for a healthy worker and
                    # STOP this worker — a dead worker must not drain
                    # the queue with instant failures.
                    alive = False
                    try:
                        alive = _controller_serves(ctrl, name)
                    except ValueError:
                        alive = False
                    if not alive:
                        with lock:
                            if retries.get(name, 0) < 1:
                                retries[name] = retries.get(name, 0) + 1
                                q.put(name)
                        return
                    r = _failed_stub(name, "worker error: %s" % e)
                with lock:
                    results[name] = r
                    done["n"] += 1
                    if progress_cb:
                        progress_cb(phase, name, done["n"], total, r)

        threads = [threading.Thread(target=worker, args=(wid,), daemon=True)
                   for wid in range(max_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Any node still queued (all workers died before reaching it)
        # is recorded as failed, never silently dropped.
        left = []
        while True:
            try:
                left.append(q.get_nowait())
            except queue.Empty:
                break
        for name in left:
            if name not in results:
                results[name] = _failed_stub(
                    name, "no healthy worker left to test it")
                with lock:
                    done["n"] += 1
    finally:
        for proc, logf in procs:
            stop_mihomo(proc, logf)
    # return results aligned with the input proxy order; untested proxies
    # (config-only, e.g. relays) get None
    return [results.get(p["name"]) for p in proxies]


def _exit_pref(name):
    """Exit-provider preference for relay chains (IP quality first).

    shoc (fraud 0/100) is preferred, debi (30/100) is the fallback,
    anything else last. Matched on the node name (case-insensitive).
    """
    n = str(name or "").lower()
    if "shoc" in n:
        return 0
    if "debi" in n:
        return 1
    return 2


def _pick_exits(main_ok, count=5):
    """Pick the exit nodes for relay chains, shoc first then debi.

    main_ok is already sorted by direct speed (fastest first). Take up
    to 3 shoc exits, then up to 3 debi exits, then the rest, capped at
    `count` total — so shoc is preferred but debi is always in the mix
    as a fallback when available, and the chain test (not this pick)
    is what decides usability.
    """
    shoc = [r for r in main_ok if _exit_pref(r.get("name")) == 0]
    debi = [r for r in main_ok if _exit_pref(r.get("name")) == 1]
    other = [r for r in main_ok if _exit_pref(r.get("name")) == 2]
    return (shoc[:3] + debi[:3] + other)[:count]


def run_convert_flow(run_dir, main_nodes, aux_nodes, progress_cb=None,
                     max_workers=MAX_WORKERS):
    """Two-phase flow for sub-convert.

    Phase 1 — direct: test all main nodes + all aux nodes in parallel.
    Phase 2 — chains: pick the top 5 main exits and top 5 aux relays by
    measured download speed, build relay->exit chains (dialer-proxy) and
    test them in parallel. Dead relays / dead exits are skipped by the
    circuit breaker (their chains never run). Both phases use the same
    max_workers (default 10).

    Returns (direct_results, chain_results, chain_proxies). Each result
    is a per-proxy dict from _run_one, with "kind" set to "direct" or
    "chain" and "role" set to "main" / "aux" for directs. chain_proxies
    are the mihomo proxy dicts built for the chains (name
    "<relay>-><exit>", dialer-proxy "<relay>").
    """
    # dedupe once so test names, results and the YAML builder agree
    def _dedupe(nodes):
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

    main_nodes = _dedupe(main_nodes)
    aux_nodes = _dedupe(aux_nodes or [])
    # ---- Phase 1: direct
    proxies = []
    roles = []
    for n in main_nodes:
        proxies.append(dict(n))
        roles.append("main")
    for n in aux_nodes:
        proxies.append(dict(n))
        roles.append("aux")
    direct = run_parallel(run_dir, proxies, progress_cb=progress_cb,
                          max_workers=max_workers, phase="direct")
    for r, role in zip(direct, roles):
        if r is None:
            continue
        r["kind"] = "direct"
        r["role"] = role

    # ---- Phase 2: chains (only if aux nodes exist)
    chains = []
    chain_proxies = []
    if aux_nodes:
        def speed_of(r):
            d = (r or {}).get("download") or {}
            sp = d.get("speed_bps")
            return sp if isinstance(sp, (int, float)) and sp > 0 else 0

        main_ok = [r for r in direct
                   if r.get("role") == "main" and speed_of(r) > 0]
        aux_ok = [r for r in direct
                  if r.get("role") == "aux" and speed_of(r) > 0]
        main_ok.sort(key=speed_of, reverse=True)
        aux_ok.sort(key=speed_of, reverse=True)
        top_exits = _pick_exits(main_ok, 5)
        top_relays = aux_ok[:5]
        # map result name -> node dict
        by_name = {}
        for n in main_nodes + aux_nodes:
            by_name[n.get("name", "")] = n
        chain_proxies = []
        chain_meta = []
        for rr in top_relays:
            relay_node = by_name.get(rr["name"])
            if relay_node is None:
                continue
            for er in top_exits:
                exit_node = by_name.get(er["name"])
                if exit_node is None:
                    continue
                p = dict(exit_node)
                p["name"] = "%s->%s" % (rr["name"], er["name"])
                p["dialer-proxy"] = rr["name"]
                chain_proxies.append(p)
                chain_meta.append({"relay": rr["name"], "exit": er["name"],
                                   "relay_speed": speed_of(rr),
                                   "exit_speed": speed_of(er)})
        if chain_proxies:
            # the config must also contain the relay nodes themselves
            # (dialer-proxy references them by name), but only the
            # chains are tested in this phase (relays already measured)
            all_proxies = []
            seen = set()
            for n in aux_nodes:
                if n.get("name") not in seen:
                    seen.add(n.get("name"))
                    all_proxies.append(dict(n))
            all_proxies.extend(chain_proxies)
            chain_names = [p["name"] for p in chain_proxies]
            raw = run_parallel(run_dir, all_proxies,
                               test_names=chain_names,
                               progress_cb=progress_cb,
                               max_workers=max_workers, phase="chains")
            chains = [r for r in raw if r is not None]
            for r, m in zip(chains, chain_meta):
                r["kind"] = "chain"
                r["relay"] = m["relay"]
                r["exit"] = m["exit"]
    return direct, chains, chain_proxies
