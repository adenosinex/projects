"""ping-test — IP/host latency tester from home (Chongqing).

ACCESS: token link, NOT login. A random 32-hex token lives in
/Users/zin/ping-test/token (mode 600); the token IS the credential.
Public URL: https://cpl.zin7.de/ping/<token>/ — no login required.
Nginx /ping/ is PUBLIC (same pattern as sub-convert's /s/): it forwards
the full path to this app, and the app validates the token from the URL.
Wrong or missing token -> 404. The token link is also shown on the
login-gated nav page (https://cpl.zin7.de/), which reads the token file
at render time, so it always shows the current link.

No password, no admin UI in this version. The token is a single stable
token; if it ever needs rotating, that is done directly on the Mac.

- Input: IPs / hostnames, one per line (or comma-separated). Hostnames are
  resolved to IP first; the resolved IP is shown.
- Two modes: ICMP ping (default, `ping -c N -t T`, ping6 for IPv6) and
  TCP-connect latency (connect to host:port, N attempts, avg connect time)
  for hosts that block ICMP.
- MULTI-THREADED: targets are tested in parallel with a ThreadPoolExecutor
  (default 10 workers, 1-20 in the UI). Each ping is its own subprocess —
  no shared state, safe to parallelize. Live progress (done/total) and
  per-target results stream in as they arrive.
- Results: resolved IP, packets sent/received, loss %, min/avg/max ms,
  status (ok / unreachable / timeout / dns_fail). History of the last 20
  runs is kept in data/history.json.
- Rate limits (token link is public, so abuse must stay bounded):
  max 1 concurrent test, max 50 targets per test.
- CLI: `POST /ping/<token>/api/test-sync` (JSON body, blocking, returns the
  finished run) and `GET /ping/<token>/api/ping?target=A&target=B&...`
  (curl-friendly, same finished-run JSON). Both share the validation,
  clamps, 50-target max and 1-concurrent-test rule with the async endpoint.
  The web page has a "命令行调用" section with copy-paste curl examples
  (token filled in at render time).

Binds 127.0.0.1:18938 only. /api/health is local-only (no token, used by
restart.sh); everything else lives under /ping/<token>/.
"""
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
HISTORY_PATH = os.path.join(DATA, "history.json")
LOG_PATH = os.path.join(BASE, "app.log")
TOKEN_PATH = os.path.join(BASE, "token")
INDEX_PATH = os.path.join(BASE, "static", "index.html")
os.makedirs(DATA, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()])
log = logging.getLogger("ping-test")


def _load_token():
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH) as f:
            tok = f.read().strip()
        if len(tok) == 32 and all(c in "0123456789abcdef" for c in tok):
            return tok
    tok = secrets.token_hex(16)
    with open(TOKEN_PATH, "w") as f:
        f.write(tok)
    os.chmod(TOKEN_PATH, 0o600)
    return tok


TOKEN = _load_token()

app = FastAPI(title="ping-test")

LOSS_RE = re.compile(
    r"(\d+) packets transmitted, (\d+) packets received, ([\d.]+)% packet loss")
RTT_RE = re.compile(
    r"round-trip min/avg/max/stddev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms")

MAX_HISTORY = 20
MAX_TARGETS = 50
_runs = {}          # run_id -> run dict (in-memory, live progress)
_runs_lock = threading.Lock()


def _check_token(token: str):
    # Wrong or missing token -> 404 (do not reveal that the path exists).
    if not token or token != TOKEN:
        raise HTTPException(status_code=404, detail="not found")


class TestRequest(BaseModel):
    targets: list = []
    count: int = 4
    timeout: int = 5
    workers: int = 10
    mode: str = "ping"          # "ping" | "tcp"
    tcp_port: int = 443
    tcp_attempts: int = 3


def _current_token():
    """Re-read the token file (cheap); fall back to the startup value."""
    try:
        with open(TOKEN_PATH) as f:
            t = f.read().strip()
        if len(t) == 32 and all(c in "0123456789abcdef" for c in t):
            return t
    except Exception:
        pass
    return TOKEN


def _prepare_run(req):
    """Shared validation for ALL test starts (async, sync, GET).

    Parses/dedupes targets, applies the same clamps as the web UI,
    enforces max 50 targets and the 1-concurrent-test rule, and creates
    the run record. Returns (run, None) on success or
    (None, JSONResponse) on error.
    """
    targets = []
    for raw in req.targets:
        for part in str(raw).split(","):
            t = part.strip()
            if t and t not in targets:
                targets.append(t)
    if not targets:
        return None, JSONResponse({"error": "no targets"}, status_code=400)
    if len(targets) > MAX_TARGETS:
        return None, JSONResponse(
            {"error": "too many targets (max %d)" % MAX_TARGETS},
            status_code=400)
    req.count = max(1, min(20, int(req.count or 4)))
    req.timeout = max(1, min(30, int(req.timeout or 5)))
    req.workers = max(1, min(20, int(req.workers or 10)))
    req.tcp_port = max(1, min(65535, int(req.tcp_port or 443)))
    req.tcp_attempts = max(1, min(10, int(req.tcp_attempts or 3)))
    if req.mode not in ("ping", "tcp"):
        req.mode = "ping"
    with _runs_lock:
        running = next((r for r in _runs.values()
                        if r["status"] == "running"), None)
        if running:
            return None, JSONResponse(
                {"error": "a test is already running",
                 "run_id": running["run_id"]}, status_code=409)
        run_id = uuid.uuid4().hex[:12]
        run = {"run_id": run_id, "status": "running",
               "targets": targets, "mode": req.mode,
               "workers": req.workers, "count": req.count,
               "timeout": req.timeout, "tcp_port": req.tcp_port,
               "tcp_attempts": req.tcp_attempts,
               "done": 0, "total": len(targets), "results": [],
               "started_at": time.time(), "finished_at": None,
               "elapsed_s": None, "summary": None,
               "lock": threading.Lock()}
        _runs[run_id] = run
    return run, None


def _run_public(run):
    """Public JSON shape of a run (same as GET /api/test/{run_id})."""
    with run["lock"]:
        results = list(run["results"])
        done = run["done"]
    return {"run_id": run["run_id"], "status": run["status"],
            "mode": run["mode"], "workers": run["workers"],
            "done": done, "total": run["total"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "elapsed_s": run["elapsed_s"]
            if run["elapsed_s"] is not None
            else round(time.time() - run["started_at"], 1),
            "results": results, "summary": run["summary"]}


def resolve(target):
    """Resolve a target to (ip, family 'v4'|'v6') or (None, error)."""
    t = (target or "").strip()
    if not t:
        return None, "empty"
    try:
        ip = ipaddress.ip_address(t)
        return str(ip), ("v6" if ip.version == 6 else "v4")
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(t, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return None, "dns_fail"
    except Exception:
        return None, "dns_fail"
    for fam, _tp, _pr, _cn, sockaddr in infos:
        if fam == socket.AF_INET:
            return sockaddr[0], "v4"
    for fam, _tp, _pr, _cn, sockaddr in infos:
        if fam == socket.AF_INET6:
            return sockaddr[0], "v6"
    return None, "dns_fail"


def _empty_result(target, ip, status, detail=""):
    return {"target": target, "ip": ip, "status": status, "detail": detail,
            "sent": None, "received": None, "loss_pct": None,
            "min_ms": None, "avg_ms": None, "max_ms": None}


def run_ping(target, ip, family, count, timeout):
    cmd = [("ping6" if family == "v6" else "ping"),
           "-c", str(count), "-t", str(timeout), ip]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=count + timeout + 10)
        out = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return _empty_result(target, ip, "timeout",
                             "ping subprocess timed out")
    m = LOSS_RE.search(out)
    if not m:
        return _empty_result(target, ip, "unreachable",
                             "no ping reply (unparsed output)")
    sent, received, loss = int(m.group(1)), int(m.group(2)), float(m.group(3))
    r = RTT_RE.search(out)
    if r:
        mn, avg, mx = float(r.group(1)), float(r.group(2)), float(r.group(3))
    else:
        mn = avg = mx = None
    status = "ok" if received > 0 else "unreachable"
    return {"target": target, "ip": ip, "status": status, "detail": "",
            "sent": sent, "received": received, "loss_pct": loss,
            "min_ms": mn, "avg_ms": avg, "max_ms": mx}


def run_tcp(target, ip, family, port, attempts, timeout):
    times = []
    err = ""
    for _ in range(attempts):
        t0 = time.time()
        try:
            s = socket.create_connection((ip, port), timeout=timeout)
            s.close()
            times.append((time.time() - t0) * 1000.0)
        except Exception as e:
            err = str(e) or e.__class__.__name__
    if not times:
        return _empty_result(target, ip, "unreachable",
                             "TCP connect failed: %s" % err)
    return {"target": target, "ip": ip, "status": "ok", "detail": "",
            "sent": attempts, "received": len(times), "loss_pct": None,
            "min_ms": round(min(times), 2), "avg_ms": round(sum(times) / len(times), 2),
            "max_ms": round(max(times), 2)}


def _test_one(target, req):
    ip, fam = resolve(target)
    if ip is None:
        return _empty_result(target, None, "dns_fail",
                             "cannot resolve hostname")
    if req.mode == "tcp":
        return run_tcp(target, ip, fam, req.tcp_port, req.tcp_attempts,
                       req.timeout)
    return run_ping(target, ip, fam, req.count, req.timeout)


def _load_history():
    try:
        with open(HISTORY_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save_history(hist):
    try:
        with open(HISTORY_PATH, "w") as f:
            json.dump(hist[-MAX_HISTORY:], f, ensure_ascii=False, indent=1)
    except Exception as e:
        log.warning("history save failed: %s", e)


def _run_in_background(run_id, req):
    run = _runs[run_id]
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=req.workers) as ex:
            futs = {ex.submit(_test_one, t, req): t for t in run["targets"]}
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                except Exception as e:
                    r = _empty_result(futs[fut], None, "error", str(e))
                with run["lock"]:
                    run["results"].append(r)
                    run["done"] += 1
    finally:
        run["status"] = "done"
        run["finished_at"] = time.time()
        run["elapsed_s"] = round(run["finished_at"] - run["started_at"], 1)
        ok = [r for r in run["results"] if r["status"] == "ok"]
        avgs = [r["avg_ms"] for r in ok if r["avg_ms"] is not None]
        summary = {"run_id": run_id, "started_at": run["started_at"],
                   "elapsed_s": run["elapsed_s"], "mode": req.mode,
                   "targets": len(run["targets"]), "ok": len(ok),
                   "failed": len(run["results"]) - len(ok),
                   "best_avg_ms": min(avgs) if avgs else None,
                   "worst_avg_ms": max(avgs) if avgs else None}
        run["summary"] = summary
        hist = _load_history()
        hist.append(summary)
        _save_history(hist)
        log.info("run %s done: %d targets, %d ok, %.1fs",
                 run_id, len(run["targets"]), len(ok), run["elapsed_s"])


@app.get("/api/health")
def health():
    with _runs_lock:
        running = next((r for r in _runs.values()
                        if r["status"] == "running"), None)
    return {"ok": True,
            "running_test": running["run_id"] if running else None}


@app.get("/ping/{token}/", response_class=HTMLResponse)
def index(token: str):
    _check_token(token)
    with open(INDEX_PATH) as f:
        html = f.read()
    # Fill the current token into the curl examples ("命令行调用" section).
    return html.replace("__TOKEN__", _current_token())


@app.post("/ping/{token}/api/test")
def start_test(token: str, req: TestRequest):
    _check_token(token)
    run, err = _prepare_run(req)
    if err is not None:
        return err
    threading.Thread(target=_run_in_background, args=(run["run_id"], req),
                     daemon=True).start()
    return {"run_id": run["run_id"], "total": run["total"]}


@app.post("/ping/{token}/api/test-sync")
def test_sync(token: str, req: TestRequest):
    """Synchronous test: same request/validation as /api/test, but runs
    inline (blocking) and returns the COMPLETE finished run object.
    The 1-concurrent-test lock covers sync runs too (the run is claimed
    in _prepare_run before the inline execution starts)."""
    _check_token(token)
    run, err = _prepare_run(req)
    if err is not None:
        return err
    _run_in_background(run["run_id"], req)
    return _run_public(run)


@app.get("/ping/{token}/api/ping")
def ping_get(token: str,
             target: list = Query(default=[]),
             targets: str = "",
             mode: str = "ping", count: int = 4, timeout: int = 5,
             workers: int = 10, tcp_port: int = 443, tcp_attempts: int = 3):
    """curl-friendly synchronous test: repeatable `target` query params
    (or one comma-separated `targets` param); all other params optional
    with the same defaults/clamps as the web UI. Returns the same
    finished-run JSON as /api/test-sync."""
    _check_token(token)
    all_targets = list(target)
    if targets:
        all_targets.append(targets)
    req = TestRequest(targets=all_targets, mode=mode, count=count,
                      timeout=timeout, workers=workers,
                      tcp_port=tcp_port, tcp_attempts=tcp_attempts)
    run, err = _prepare_run(req)
    if err is not None:
        return err
    _run_in_background(run["run_id"], req)
    return _run_public(run)


@app.get("/ping/{token}/api/test/{run_id}")
def get_test(token: str, run_id: str):
    _check_token(token)
    run = _runs.get(run_id)
    if not run:
        return JSONResponse({"error": "unknown run_id"}, status_code=404)
    return _run_public(run)


@app.get("/ping/{token}/api/history")
def history(token: str):
    _check_token(token)
    return _load_history()[-MAX_HISTORY:]
