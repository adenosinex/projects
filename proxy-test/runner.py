"""Test runner for proxy-test.

Reuses the proven approach from /Users/zin/sub-test/:
- one mihomo instance per worker (mixed-port 17900+i, controller
  19100+i)
- select group TEST, switch via controller API
- curl through the mixed port:
    delay    GET http://www.gstatic.com/generate_204      (5s, pre-check)
    google   GET https://www.google.com/search?q=hello -L  (20s)
    youtube  GET https://www.youtube.com/                  (30s)
    download GET https://speed.cloudflare.com/__down?bytes=10000000 (45s)

Parallelism (single mode): up to MAX_WORKERS mihomo instances run at
once, each on its own mixed-port / controller port (base 17900/19100 +
worker id), fed from a shared queue. Each worker switches its own
mihomo to the proxy it is testing. max_workers=1 is the sequential
fallback (one mihomo, one node at a time, exactly the old behaviour).

Ports: 17900-17909 / 19100-19109 are proxy-test's own range. They do
NOT overlap sub-convert's 17891-17895 / 19091-19095 or the old
sequential 17890 / 19090. Overlapping ranges are dangerous: if another
process holds a worker's ports, that worker's mihomo fails to bind but
the readiness check can be fooled by the other process answering
/version — the worker then fails every node it touches (see below).

Robustness (learned from a real failure, 2026-11-13):
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

Slow-speed abort: the download curl uses --speed-limit + --speed-time.
If the speed stays below SLOW_LIMIT_BPS for SLOW_TIME_S seconds (or no
data arrives), curl aborts early and the node is marked "slow" with the
observed speed — it does not wait out the full 45s timeout. A slow node
costs ~5s, not 45s. (Parallel mode only; the sequential fallback keeps
the original no-abort download.)

Circuit breaker (automatic, no config):
  - per-proxy pre-check: delay test first; on failure mark dead and SKIP
    google/youtube/download for that proxy.
  - per-relay short-circuit: relay direct dead -> skip ALL its chains.
  - per-exit short-circuit: exit direct dead -> skip ALL chains using it.
  - per-chain short-circuit: chain delay dead -> skip its google/youtube/
    download.
  Skipped checks are recorded as status "skipped" with a reason, not as
  failures.
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
BASE_MIXED_PORT = 17900
BASE_CTRL_PORT = 19100
MIXED_PORT = BASE_MIXED_PORT  # worker 0 (sequential / relay mode)
CTRL_PORT = BASE_CTRL_PORT
CTRL = "http://127.0.0.1:%d" % CTRL_PORT
PROXY_URL = "http://127.0.0.1:%d" % MIXED_PORT
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
GEN204 = "http://www.gstatic.com/generate_204"
GOOGLE = "https://www.google.com/search?q=hello&hl=en"
YOUTUBE = "https://www.youtube.com/"
CFDL = "https://speed.cloudflare.com/__down?bytes=10000000"

DELAY_TIMEOUT = 5
GOOGLE_TIMEOUT = 20
YOUTUBE_TIMEOUT = 30
DL_TIMEOUT = 45

# Parallelism + slow abort.
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


def build_config(proxies, run_dir, worker_id=0):
    """proxies: list of mihomo proxy dicts (each with unique 'name').
    Names are deduplicated defensively (dialer-proxy refs updated too).
    Writes config.yaml into this worker's own directory."""
    seen = {}
    for p in proxies:
        n = p["name"]
        if n in seen:
            seen[n] += 1
            p["name"] = "%s#%d" % (n, seen[n])
        else:
            seen[n] = 1
    # Note: the first occurrence keeps the original name, so dialer-proxy
    # references to it stay valid; duplicates get a suffix.
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


def start_mihomo(run_dir, worker_id=0, expect_name=None):
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


def switch_proxy(name, ctrl_port=CTRL_PORT):
    data = json.dumps({"name": name}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d/proxies/TEST" % ctrl_port, data=data,
        headers={"Content-Type": "application/json"}, method="PUT")
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


# ---------------------------------------------------------------- curl

def _curl(url, timeout, follow=False, ua=False, proxy_url=PROXY_URL,
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


def delay_test(proxy_url=PROXY_URL):
    r = _curl(GEN204, DELAY_TIMEOUT, proxy_url=proxy_url)
    alive = (r["code"] == 204)
    r["alive"] = alive
    return r


def full_test(proxy_url=PROXY_URL, slow_abort=False):
    """Google + YouTube + download. Caller must have passed delay first.
    With slow_abort, the download aborts early if it stays too slow."""
    g = _curl(GOOGLE, GOOGLE_TIMEOUT, follow=True, ua=True,
              proxy_url=proxy_url)
    y = _curl(YOUTUBE, YOUTUBE_TIMEOUT, ua=True, proxy_url=proxy_url)
    if slow_abort:
        d = _curl(CFDL, DL_TIMEOUT, proxy_url=proxy_url,
                  slow_limit=SLOW_LIMIT_BPS, slow_time=SLOW_TIME_S)
    else:
        d = _curl(CFDL, DL_TIMEOUT, proxy_url=proxy_url)
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


# ---------------------------------------------------------------- run

def _run_one(name, skip_reason=None, proxy_url=PROXY_URL,
             ctrl_port=CTRL_PORT, slow_abort=False):
    """Run the full per-proxy test with circuit breaker.
    Returns a result dict. If skip_reason is given, everything is skipped."""
    if skip_reason:
        return {"name": name, "status": "skipped",
                "delay": skipped(skip_reason),
                "google": skipped(skip_reason),
                "youtube": skipped(skip_reason),
                "download": skipped(skip_reason)}
    switch_proxy(name, ctrl_port)
    time.sleep(0.5)
    d = delay_test(proxy_url)
    if not d["alive"]:
        reason = "circuit-breaker: delay pre-check failed (code %s)" % d["code"]
        return {"name": name, "status": "failed",
                "delay": d,
                "google": skipped(reason),
                "youtube": skipped(reason),
                "download": skipped(reason)}
    rest = full_test(proxy_url, slow_abort=slow_abort)
    dl = rest["download"]
    if dl["status"] == "slow":
        st = "slow"
    else:
        ok = any(rest[k]["code"] for k in ("google", "youtube", "download"))
        st = "ok" if ok else "failed"
    return {"name": name, "status": st, "delay": d, **rest}


def run_single_mode(run_dir, nodes, progress_cb, max_workers=MAX_WORKERS):
    """Test every node direct. Returns list of results in node order.
    max_workers=1 is the sequential fallback (one mihomo, one node at a
    time, no slow-speed abort — the original behaviour). Otherwise
    max_workers mihomo instances run in parallel from a shared queue."""
    proxies = _dedupe_names([dict(n) for n in nodes])
    meta = {p["name"]: p for p in proxies}
    total = len(proxies)
    expect = proxies[0]["name"] if proxies else None
    if max_workers <= 1:
        build_config([dict(p) for p in proxies], run_dir, 0)
        proc, logf = start_mihomo(run_dir, 0, expect_name=expect)
        results = []
        try:
            for i, p in enumerate(proxies):
                progress_cb("single", p["name"], i, total)
                r = _run_one(p["name"])
                r["kind"] = "single"
                r["protocol"] = str(p.get("type", ""))
                r["server"] = str(p.get("server", ""))
                results.append(r)
        finally:
            stop_mihomo(proc, logf)
        return results

    # ---- parallel path
    os.makedirs(run_dir, exist_ok=True)
    # Kill strays from a previous crashed run ONCE, before starting any
    # workers. (Do NOT do this per-worker: the pkill pattern matches the
    # run dir, so it would kill the sibling workers just started.)
    _kill_stray(run_dir)
    for wid in range(max_workers):
        build_config([dict(p) for p in proxies], run_dir, wid)
    procs = []
    try:
        for wid in range(max_workers):
            procs.append(start_mihomo(run_dir, wid, expect_name=expect))
        q = queue.Queue()
        for p in proxies:
            q.put(p["name"])
        results = {}
        retries = {}
        lock = threading.Lock()
        done = {"n": 0}

        def worker(wid):
            mixed, ctrl = _ports(wid)
            proxy_url = "http://127.0.0.1:%d" % mixed
            while True:
                try:
                    name = q.get_nowait()
                except queue.Empty:
                    return
                try:
                    r = _run_one(name, proxy_url=proxy_url,
                                 ctrl_port=ctrl, slow_abort=True)
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
                p = meta.get(name, {})
                r["kind"] = "single"
                r["protocol"] = str(p.get("type", ""))
                r["server"] = str(p.get("server", ""))
                with lock:
                    results[name] = r
                    done["n"] += 1
                    progress_cb("single", name, done["n"], total)

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
                done["n"] += 1
    finally:
        for proc, logf in procs:
            stop_mihomo(proc, logf)
    out = []
    for p in proxies:
        r = results.get(p["name"])
        if r is None:
            r = _failed_stub(p["name"], "missing result")
        out.append(r)
    return out


def run_relay_mode(run_dir, relays, exits, progress_cb):
    """relays: [(region, node)], exits: [(server, [nodes])].
    Phase A: relay direct. Phase B: exit direct. Phase C: chains.
    Circuit breaker skips dead relays / dead exits / dead chains.
    (Sequential, one mihomo — unchanged.)"""
    # build proxy list: relay directs, exit directs, chains
    proxies = []
    relay_names = {}
    for region, node in relays:
        p = dict(node)
        p["name"] = "relay:%s" % region
        relay_names[region] = p["name"]
        proxies.append(p)
    exit_names = {}  # (server, type) -> proxy name
    for server, ns in exits:
        for n in ns:
            p = dict(n)
            p["name"] = "exit:%s:%s" % (server, n.get("type", ""))
            exit_names[(server, str(n.get("type", "")))] = p["name"]
            proxies.append(p)
    # chains: relay x exit
    chain_names = {}
    for region, _ in relays:
        for server, ns in exits:
            for n in ns:
                p = dict(n)
                cname = "chain:%s->%s:%s" % (region, server, n.get("type", ""))
                p["name"] = cname
                p["dialer-proxy"] = relay_names[region]
                chain_names[(region, server, str(n.get("type", "")))] = cname
                proxies.append(p)

    build_config(proxies, run_dir)
    proc, logf = start_mihomo(
        run_dir, 0, expect_name=proxies[0]["name"] if proxies else None)
    results = []
    try:
        # Phase A: relay direct
        relay_alive = {}
        total_a = len(relays)
        for i, (region, _) in enumerate(relays):
            progress_cb("relay-direct", region, i, total_a)
            r = _run_one(relay_names[region])
            r["kind"] = "relay-direct"
            r["region"] = region
            relay_alive[region] = (r["status"] == "ok")
            results.append(r)
        # Phase B: exit direct
        exit_alive = {}
        flat_exits = [(server, n) for server, ns in exits for n in ns]
        total_b = len(flat_exits)
        for i, (server, n) in enumerate(flat_exits):
            key = (server, str(n.get("type", "")))
            progress_cb("exit-direct", "%s %s" % (server, n.get("type", "")),
                        i, total_b)
            r = _run_one(exit_names[key])
            r["kind"] = "exit-direct"
            r["server"] = server
            r["protocol"] = str(n.get("type", ""))
            exit_alive[key] = (r["status"] == "ok")
            results.append(r)
        # Phase C: chains
        pairs = [(region, server, n) for region, _ in relays
                 for server, ns in exits for n in ns]
        total_c = len(pairs)
        for i, (region, server, n) in enumerate(pairs):
            key = (server, str(n.get("type", "")))
            cname = chain_names[(region, server, str(n.get("type", "")))]
            progress_cb("chain", "%s -> %s %s" % (region, server,
                                                  n.get("type", "")),
                        i, total_c)
            if not relay_alive.get(region):
                r = _run_one(cname, skip_reason=(
                    "circuit-breaker: relay %s direct failed" % region))
            elif not exit_alive.get(key):
                r = _run_one(cname, skip_reason=(
                    "circuit-breaker: exit %s %s direct failed"
                    % (server, n.get("type", ""))))
            else:
                r = _run_one(cname)
            r["kind"] = "chain"
            r["region"] = region
            r["server"] = server
            r["protocol"] = str(n.get("type", ""))
            results.append(r)
    finally:
        stop_mihomo(proc, logf)
    return results
