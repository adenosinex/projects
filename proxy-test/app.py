"""proxy-test — subscription speed-test web app (FastAPI).

Modes:
  single: one subscription, test all its nodes direct.
  relay:  relay subscription + exit subscription; relay nodes direct first,
          exit nodes direct, then relay->exit chains (all protocols, all
          regions, automatic circuit breaker).

Binds 127.0.0.1:18935 only; no auth in v1.
Credentials are never returned in API responses (see parser.sanitize_for_api)
and never written to the frontend-visible log.
"""
import logging
import os
import threading
import traceback

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import parser as subparser
import runner
import store

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE, "app.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()])
log = logging.getLogger("proxy-test")

store.init()
app = FastAPI(title="proxy-test")

_run_lock = threading.Lock()
_progress_lock = threading.Lock()
_current = {"id": None}


def _safe_sub_meta(meta):
    return {k: meta[k] for k in
            ("id", "name", "source", "node_count", "regions", "servers",
             "created_at") if k in meta}


@app.get("/api/health")
def health():
    return {"ok": True, "running_test": _current["id"]}


# ------------------------------------------------------------ subscriptions

@app.post("/api/subscriptions")
async def add_subscription(request: Request):
    """Add by URL (JSON {url, name}) or by uploaded file (multipart)."""
    ct = request.headers.get("content-type", "")
    try:
        if ct.startswith("multipart/form-data"):
            form = await request.form()
            file = form.get("file")
            if file is None:
                raise HTTPException(400, "no file uploaded")
            raw = (await file.read()).decode("utf-8", "replace")
            src = "file:" + (file.filename or "upload")
            disp_name = form.get("name") or (file.filename or "upload")
        else:
            data = await request.json()
            url = (data or {}).get("url", "")
            if not url:
                raise HTTPException(400, "provide a url or upload a file")
            raw = subparser.fetch_url(url)
            src = "url:" + url.split("?")[0]
            disp_name = data.get("name") or url.split("?")[0]
        nodes = subparser.parse_subscription_text(raw)
    except HTTPException:
        raise
    except Exception as e:
        log.warning("subscription parse failed: %s", e)
        raise HTTPException(400, "parse failed: %s" % e)
    if not nodes:
        raise HTTPException(400, "no proxy nodes found")
    summary = subparser.summarize(nodes)
    meta = {
        "id": store.new_id(),
        "name": disp_name,
        "source": src,
        "node_count": summary["node_count"],
        "regions": summary["regions"],
        "servers": summary["servers"],
        "created_at": store.now_iso(),
    }
    store.save_sub(meta, nodes)
    log.info("subscription added: %s (%d nodes)", meta["id"],
             meta["node_count"])
    return _safe_sub_meta(meta)


@app.get("/api/subscriptions")
def list_subscriptions():
    return [_safe_sub_meta(m) for m in store.list_subs()]


@app.get("/api/subscriptions/{sub_id}")
def get_subscription(sub_id: str):
    nodes = store.get_sub_nodes(sub_id)
    if nodes is None:
        raise HTTPException(404, "subscription not found")
    meta = next((m for m in store.list_subs() if m["id"] == sub_id), None)
    return {"meta": _safe_sub_meta(meta) if meta else {"id": sub_id},
            "nodes": [subparser.sanitize_for_api(n) for n in nodes]}


@app.delete("/api/subscriptions/{sub_id}")
def delete_subscription(sub_id: str):
    store.delete_sub(sub_id)
    return {"ok": True}


# ------------------------------------------------------------ tests

class StartTestBody(BaseModel):
    mode: str  # "single" | "relay"
    sub_id: str = ""
    relay_sub_id: str = ""
    exit_sub_id: str = ""
    workers: int = 10  # single mode: parallel mihomo workers (1 = sequential)


def _progress(run_id, phase, name, i, total):
    # Parallel workers call this concurrently; serialize the
    # read-modify-write so progress never goes backwards.
    with _progress_lock:
        run = store.get_run(run_id)
        if run is None:
            return
        run["progress"] = {"phase": phase, "name": name,
                           "done": i, "total": total}
        store.save_run(run)


def _run_in_background(run_id, mode, sub_id, relay_sub_id, exit_sub_id,
                       workers=10):
    run = store.get_run(run_id)
    run_dir = os.path.join(BASE, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    import time as _time
    t0 = _time.time()
    try:
        if mode == "single":
            nodes = store.get_sub_nodes(sub_id)
            if nodes is None:
                raise RuntimeError("subscription not found: " + sub_id)
            real = subparser.filter_real_nodes(nodes)
            if not real:
                raise RuntimeError("no usable nodes in subscription")
            run["progress"] = {"phase": "single", "name": "",
                               "done": 0, "total": len(real)}
            store.save_run(run)
            results = runner.run_single_mode(
                run_dir, real,
                lambda ph, nm, i, t: _progress(run_id, ph, nm, i, t),
                max_workers=workers)
        else:
            rnodes = store.get_sub_nodes(relay_sub_id)
            enodes = store.get_sub_nodes(exit_sub_id)
            if rnodes is None or enodes is None:
                raise RuntimeError("relay or exit subscription not found")
            relays = subparser.pick_relays(rnodes)
            exits = subparser.group_exits(enodes)
            if not relays:
                raise RuntimeError("no usable relay nodes")
            if not exits:
                raise RuntimeError("no usable exit nodes")
            run["progress"] = {"phase": "relay-direct", "name": "",
                               "done": 0,
                               "total": len(relays) + sum(len(ns) for _, ns in exits)}
            store.save_run(run)
            results = runner.run_relay_mode(
                run_dir, relays, exits,
                lambda ph, nm, i, t: _progress(run_id, ph, nm, i, t))
        run["results"] = results
        run["status"] = "done"
        run["summary"] = _summarize(results)
    except Exception as e:
        log.error("test %s failed: %s\n%s", run_id, e,
                  traceback.format_exc())
        run["status"] = "error"
        run["error"] = str(e)
    finally:
        run["elapsed_s"] = round(_time.time() - t0, 1)
        run["progress"] = None
        store.save_run(run)
        _current["id"] = None


def _summarize(results):
    def is_ok(r):
        return r.get("status") == "ok"
    total = len(results)
    ok = sum(1 for r in results if is_ok(r))
    skipped = sum(1 for r in results if r.get("status") == "skipped")
    slow = sum(1 for r in results if r.get("status") == "slow")
    failed = total - ok - skipped - slow
    best = None
    for r in results:
        d = r.get("download") or {}
        sp = d.get("speed_bps") or 0
        if sp and (best is None or sp > best[1]):
            best = (r.get("name"), sp)
    return {"total": total, "ok": ok, "skipped": skipped, "slow": slow,
            "failed": failed,
            "best": {"name": best[0], "speed_bps": best[1]} if best else None}


@app.post("/api/test/start")
def start_test(body: StartTestBody):
    mode = body.mode
    if mode not in ("single", "relay"):
        raise HTTPException(400, "mode must be single or relay")
    if mode == "single" and not body.sub_id:
        raise HTTPException(400, "sub_id required for single mode")
    if mode == "relay" and not (body.relay_sub_id and body.exit_sub_id):
        raise HTTPException(400,
                            "relay_sub_id and exit_sub_id required")
    if _current["id"] is not None:
        raise HTTPException(409, "a test is already running: %s"
                            % _current["id"])
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(409, "a test is already running")
    run_id = store.new_id()
    workers = max(1, min(20, int(body.workers or 10)))
    run = {"id": run_id, "mode": mode,
           "sub_id": body.sub_id,
           "relay_sub_id": body.relay_sub_id,
           "exit_sub_id": body.exit_sub_id,
           "workers": workers if mode == "single" else None,
           "status": "running", "created_at": store.now_iso(),
           "progress": {"phase": "starting", "name": "", "done": 0,
                        "total": 0},
           "results": [], "summary": None, "error": None,
           "elapsed_s": None}
    store.save_run(run)
    _current["id"] = run_id

    def _bg():
        try:
            _run_in_background(run_id, mode, body.sub_id,
                               body.relay_sub_id, body.exit_sub_id,
                               workers=workers)
        finally:
            _run_lock.release()

    threading.Thread(target=_bg, daemon=True).start()
    log.info("test started: %s mode=%s", run_id, mode)
    return {"test_id": run_id, "status": "running"}


@app.get("/api/test/{run_id}")
def get_test(run_id: str):
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "test not found")
    return run


@app.get("/api/tests")
def list_tests():
    return store.list_runs()


app.mount("/", StaticFiles(directory=os.path.join(BASE, "static"),
                           html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=18935,
                log_level="info")
