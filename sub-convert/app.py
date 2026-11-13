"""sub-convert — subscription converter with real speed test and 精选 / 精选中继.

- Add a subscription by URL or file upload; auto-parses (vmess/vless/
  trojan/ss/hy2/anytls/tuic links, Clash YAML, base64).
- Speed test: REAL download speed (not delay-only). Reuses proxy-test's
  runner approach (delay pre-check, Google, YouTube, Cloudflare 10MB
  download in one round), running in PARALLEL (10 mihomo workers,
  ports 17910-17919 / 19110-19119, one worker = one mihomo instance,
  each with its own -d directory and a controller-ownership check at
  startup) with a slow-speed abort (curl --speed-limit 50KB/s
  --speed-time 5s). Workers configurable 1-20 (1 = sequential
  fallback), default 10 — A/B-verified on proxy-test 2026-11-13: 10
  workers were 9x faster than sequential with no systematic speed
  distortion. See runner.py. Failed nodes get no speed, never backfilled.
- After each test the 3 fastest nodes by DOWNLOAD speed go into a "精选"
  url-test group; node names in the converted YAML carry a speed suffix
  (" 2.4MB", one decimal). Nodes with no measured speed get no suffix.
- Auxiliary subscription: attach an aux subscription to a main one; the
  aux nodes act as relays. Relay paths (aux relay -> main exit) are
  tested with the same real speed test; the fastest 3 go into a
  "精选中继" url-test group in the main subscription's converted YAML.
- Converted Clash YAML (all nodes + 精选 + 精选中继 + 全部节点) is served
  at /s/<32-hex-token>.yaml — the token IS the credential, unguessable,
  and is never written to logs.
- Hourly re-test is done by cron calling hourly.py (same test code, same
  cross-process lock, so manual and cron runs never overlap).

Binds 127.0.0.1:18937 only. The management UI is gated by cpl.zin7.de
login (nginx auth_request); /s/<token>.yaml is served WITHOUT login so
FlClash can fetch it directly.
"""
import logging
import os
import threading
import traceback

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import convert as converter
import featured_watch as _fw
import parser as subparser
import runner
import store

BASE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(BASE, "run")
LOG_PATH = os.path.join(BASE, "app.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()])
log = logging.getLogger("sub-convert")

store.init()
os.makedirs(RUN_DIR, exist_ok=True)
app = FastAPI(title="sub-convert")

_current = {"sub_id": None, "started_at": None}
_current_lock = threading.Lock()

# Live progress of the currently running test (manual or cron). Updated
# by the runner's progress callback; read by GET /api/test/progress.
_progress = {"phase": "", "current": "", "done": 0, "total": 0,
             "results": [], "started_at": None, "finished_at": None,
             "elapsed_s": None, "status": "idle", "sub_id": None,
             "workers": None}
_progress_lock = threading.Lock()


def _reset_progress(sub_id, workers=10):
    with _progress_lock:
        _progress.update({"phase": "starting", "current": "", "done": 0,
                          "total": 0, "results": [],
                          "started_at": store.now_iso(),
                          "finished_at": None, "elapsed_s": None,
                          "status": "running", "sub_id": sub_id,
                          "workers": workers})


def _progress_cb(phase, name, done, total, result=None):
    with _progress_lock:
        _progress["phase"] = phase
        _progress["current"] = name
        _progress["done"] = done
        _progress["total"] = total
        if result is not None:
            _progress["results"].append(_compact_result(result))


def _compact_result(r):
    """Per-node result reduced to what the UI shows: name, kind/role,
    status, delay ms, Google/YouTube seconds, download speed."""
    def _t(check):
        c = (r or {}).get(check) or {}
        t = c.get("time_s")
        return t if isinstance(t, (int, float)) else None

    def _sp(check):
        c = (r or {}).get(check) or {}
        sp = c.get("speed_bps")
        return sp if isinstance(sp, (int, float)) and sp > 0 else None

    d = (r or {}).get("delay") or {}
    dt = d.get("time_s")
    return {
        "name": r.get("name", ""),
        "kind": r.get("kind", ""),
        "role": r.get("role", ""),
        "relay": r.get("relay", ""),
        "exit": r.get("exit", ""),
        "status": r.get("status", ""),
        "delay_ms": int(dt * 1000) if isinstance(dt, (int, float))
        and dt > 0 else None,
        "google_s": _t("google"),
        "youtube_s": _t("youtube"),
        "download_bps": _sp("download"),
        "download_mb": (round(_sp("download") / 1_000_000, 1)
                        if _sp("download") else None),
    }


def _finish_progress(status, elapsed_s=None):
    with _progress_lock:
        _progress["status"] = status
        _progress["finished_at"] = store.now_iso()
        _progress["elapsed_s"] = elapsed_s


def _public_meta(meta):
    """Meta safe for API responses. The token is shown to the logged-in
    owner in the UI (they need it for FlClash); it is never logged."""
    aux_id = meta.get("aux_sub_id") or ""
    aux_meta = store.get_sub_meta(aux_id) if aux_id else None
    return {
        "id": meta["id"],
        "name": meta.get("name", ""),
        "source": meta.get("source", ""),
        "node_count": meta.get("node_count", 0),
        "regions": meta.get("regions", []),
        "servers": meta.get("servers", {}),
        "token": meta.get("token", ""),
        "created_at": meta.get("created_at", ""),
        "last_test_at": meta.get("last_test_at"),
        "last_test": meta.get("last_test"),
        "last_results": meta.get("last_results", []),
        "featured": meta.get("featured", []),
        "featured_relay": meta.get("featured_relay", []),
        "aux_sub_id": aux_id,
        "aux_name": aux_meta.get("name", "") if aux_meta else "",
        "convert_ready": os.path.exists(store.converted_path(meta["id"])),
    }


def _speed_of(result):
    d = (result or {}).get("download") or {}
    sp = d.get("speed_bps")
    return sp if isinstance(sp, (int, float)) and sp > 0 else 0


def run_test_for_sub(sub_id, workers=10):
    """Run the full test flow for one subscription and regenerate its
    YAML: direct speed test (parallel), top-3 精选 by download speed,
    and if an auxiliary subscription is attached, relay-chain speed
    test and top-3 精选中继. Raises RuntimeError on failure. Caller
    must hold the test lock. Returns the summary dict (now including
    elapsed_s, workers, and the compact per-node results)."""
    import time as _time
    t0 = _time.time()
    meta = store.get_sub_meta(sub_id)
    if meta is None:
        raise RuntimeError("subscription not found: " + sub_id)
    nodes = store.get_sub_nodes(sub_id)
    if nodes is None:
        raise RuntimeError("subscription not found: " + sub_id)
    real = subparser.filter_real_nodes(nodes)
    if not real:
        raise RuntimeError("no usable nodes in subscription")
    aux_all = []
    aux_nodes = []
    aux_id = meta.get("aux_sub_id") or ""
    if aux_id:
        aux_all = store.get_sub_nodes(aux_id) or []
        if aux_all:
            aux_nodes = subparser.filter_real_nodes(aux_all)
    log.info("test sub %s: %d main nodes, %d aux nodes, %d workers",
             sub_id, len(real), len(aux_nodes), workers)
    direct, chains, chain_proxies = runner.run_convert_flow(
        RUN_DIR, real, aux_nodes, progress_cb=_progress_cb,
        max_workers=workers)
    # Preserve the source subscription's original structure: load the
    # raw text saved at add-time and parse its YAML doc (None for bare
    # link lists — then the converter falls back to the minimal output).
    source_doc = None
    raw = store.get_sub_raw(sub_id)
    if raw:
        try:
            source_doc = subparser.extract_yaml_doc(raw)
        except Exception:
            source_doc = None
    # Template cleansing: every subscription except the VPS template
    # itself is rebuilt from the VPS template's rules/groups/DNS; only
    # its own nodes are kept. If the template cannot be loaded, the
    # converter falls back to the sub's own doc — never break an
    # existing subscription.
    template_doc = None
    if sub_id != converter.TEMPLATE_SUB_ID:
        try:
            traw = store.get_sub_raw(converter.TEMPLATE_SUB_ID)
            if traw:
                template_doc = subparser.extract_yaml_doc(traw)
        except Exception:
            template_doc = None
    # Build the YAML from the FULL node lists (including info nodes, so
    # original group references never dangle); only tested nodes carry a
    # speed suffix.
    text, featured_rows, relay_rows = converter.build_yaml(
        nodes, aux_all, direct, chains, chain_proxies,
        source_doc=source_doc, template_doc=template_doc, sub_id=sub_id)
    with open(store.converted_path(sub_id), "w", encoding="utf-8") as f:
        f.write(text)
    ok = sum(1 for r in direct if r.get("status") in ("ok", "slow"))
    slow = sum(1 for r in direct if r.get("status") == "slow")
    failed = len(direct) - ok
    chain_ok = sum(1 for r in chains if r.get("status") in ("ok", "slow"))
    elapsed_s = round(_time.time() - t0, 1)
    all_results = [_compact_result(r)
                   for r in (direct + chains) if r is not None]
    summary = {"total": len(direct), "ok": ok, "slow": slow,
               "failed": failed,
               "chains_total": len(chains), "chains_ok": chain_ok,
               "best_speed_bps": max((_speed_of(r) for r in direct),
                                     default=0),
               "elapsed_s": elapsed_s, "workers": workers}
    store.update_sub_meta(
        sub_id, last_test_at=store.now_iso(), last_test=summary,
        featured=featured_rows, featured_relay=relay_rows,
        last_results=all_results)
    return {"summary": summary, "featured": featured_rows,
            "featured_relay": relay_rows, "results": all_results,
            "elapsed_s": elapsed_s}


def _run_in_background(sub_id, workers=10):
    import time as _time
    t0 = _time.time()
    with _current_lock:
        _current["sub_id"] = sub_id
        _current["started_at"] = store.now_iso()
    _reset_progress(sub_id, workers=workers)
    try:
        with store.TestLock():
            log.info("test started for sub %s (%d workers)", sub_id, workers)
            res = run_test_for_sub(sub_id, workers=workers)
            elapsed = round(_time.time() - t0, 1)
            _finish_progress("done", elapsed)
            log.info("test done for sub %s: %s elapsed=%.1fs workers=%d",
                     sub_id, res["summary"], elapsed, workers)
    except BlockingIOError:
        _finish_progress("skipped", round(_time.time() - t0, 1))
        log.info("test for sub %s skipped: another test is running", sub_id)
    except Exception as e:
        _finish_progress("error", round(_time.time() - t0, 1))
        log.error("test for sub %s failed: %s\n%s", sub_id, e,
                  traceback.format_exc())
    finally:
        with _current_lock:
            _current["sub_id"] = None
            _current["started_at"] = None


@app.get("/api/health")
def health():
    with _current_lock:
        cur = dict(_current)
    return {"ok": True, "running_test": cur["sub_id"],
            "cron_running": store.lock_held() and cur["sub_id"] is None}


@app.get("/s/{token}.yaml", response_class=PlainTextResponse)
def serve_converted(token: str):
    meta = store.find_sub_by_token(token)
    if meta is None:
        raise HTTPException(404, "not found")
    path = store.converted_path(meta["id"])
    if not os.path.exists(path):
        raise HTTPException(
            404, "converted subscription not ready yet — test pending")
    with open(path, encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------ subscriptions

@app.post("/api/subscriptions")
async def add_subscription(request: Request):
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
        "token": store.new_token(),
        "created_at": store.now_iso(),
        "last_test_at": None,
        "last_test": None,
        "featured": [],
        "featured_relay": [],
        "aux_sub_id": "",
    }
    store.save_sub(meta, nodes)
    # Keep the raw subscription text so the converter can preserve the
    # source's original proxy-groups / rules / dns verbatim.
    store.save_sub_raw(meta["id"], raw)
    log.info("subscription added: %s (%d nodes)", meta["id"],
             meta["node_count"])
    # initial speed test in the background so the converted URL works soon
    threading.Thread(target=_run_in_background, args=(meta["id"],),
                     daemon=True).start()
    return _public_meta(meta)


@app.get("/api/subscriptions")
def list_subscriptions():
    return [_public_meta(m) for m in store.list_subs()]


@app.delete("/api/subscriptions/{sub_id}")
def delete_subscription(sub_id: str):
    store.delete_sub(sub_id)
    return {"ok": True}


class AuxBody(BaseModel):
    aux_sub_id: str = ""


@app.post("/api/subscriptions/{sub_id}/aux")
def set_aux_subscription(sub_id: str, body: AuxBody):
    """Attach (or clear, with empty aux_sub_id) an auxiliary
    subscription that provides relay nodes for this main subscription."""
    meta = store.get_sub_meta(sub_id)
    if meta is None:
        raise HTTPException(404, "subscription not found")
    aux_id = (body.aux_sub_id or "").strip()
    if aux_id:
        if aux_id == sub_id:
            raise HTTPException(400, "aux subscription cannot be itself")
        if store.get_sub_meta(aux_id) is None:
            raise HTTPException(404, "aux subscription not found")
    store.update_sub_meta(sub_id, aux_sub_id=aux_id)
    return _public_meta(store.get_sub_meta(sub_id))


# ------------------------------------------------------------ tests

class TestBody(BaseModel):
    sub_id: str = ""
    workers: int = 10  # parallel mihomo workers (1-20, 1 = sequential)


@app.post("/api/test")
def start_test(body: TestBody):
    sub_id = body.sub_id
    if not sub_id:
        raise HTTPException(400, "sub_id required")
    if store.get_sub_meta(sub_id) is None:
        raise HTTPException(404, "subscription not found")
    with _current_lock:
        if _current["sub_id"] is not None:
            raise HTTPException(409, "a test is already running")
    if store.lock_held():
        raise HTTPException(409, "a test is already running")
    workers = max(1, min(20, int(body.workers or 10)))
    threading.Thread(target=_run_in_background, args=(sub_id, workers),
                     daemon=True).start()
    return {"ok": True, "sub_id": sub_id, "status": "running",
            "workers": workers}


@app.get("/api/test/status")
def test_status():
    with _current_lock:
        cur = dict(_current)
    return {"running_sub_id": cur["sub_id"],
            "started_at": cur["started_at"],
            "cron_running": store.lock_held() and cur["sub_id"] is None}


@app.get("/api/test/progress")
def test_progress():
    """Live progress of the running test (manual or cron): phase,
    current proxy, done/total, per-proxy results as they arrive, and
    elapsed time once finished. The frontend polls this during a run."""
    with _progress_lock:
        p = dict(_progress)
        p["results"] = list(p["results"])
    with _current_lock:
        p["running_sub_id"] = _current["sub_id"]
    return p


# ------------------------------------------------------------ featured watch

def _featured_status_for(sub_id):
    """One subscription's 精选 maintenance status (for the
    精选维护 page). Reads the watchdog state + the sub meta."""
    meta = store.get_sub_meta(sub_id)
    if meta is None:
        return None
    cfg = _fw.load_config()
    st = _fw.load_state()
    sst = _fw.sub_state(st, sub_id)
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
            >= _fw.BAD_STREAK,
        })
    return {
        "id": sub_id,
        "name": meta.get("name", ""),
        "enabled": sub_id in cfg.get("enabled", []),
        "featured": rows,
        "last_probe_at": sst.get("last_probe_at"),
        "last_publish_at": sst.get("last_publish_at"),
        "last_action": sst.get("last_action"),
        "last_test_at": meta.get("last_test_at"),
        "last_swap": meta.get("featured_watch"),
        "interval_s": _fw.PROBE_INTERVAL_S,
        "bad_threshold_mb": _fw.BAD_MBPS,
        "bad_streak": _fw.BAD_STREAK,
    }


@app.get("/api/featured/status")
def featured_status(sub_id: str = ""):
    if sub_id:
        s = _featured_status_for(sub_id)
        if s is None:
            raise HTTPException(404, "subscription not found")
        return s
    return {"subs": [_featured_status_for(m["id"])
                     for m in store.list_subs()]}


class FeaturedEnableBody(BaseModel):
    sub_id: str = ""
    enabled: bool = False


@app.post("/api/featured/enable")
def featured_enable(body: FeaturedEnableBody):
    """Opt a subscription into / out of featured-node monitoring.
    Takes effect within one watchdog interval (5 min)."""
    sub_id = (body.sub_id or "").strip()
    if not sub_id or store.get_sub_meta(sub_id) is None:
        raise HTTPException(404, "subscription not found")
    cfg = _fw.load_config()
    enabled = [x for x in cfg.get("enabled", []) if x != sub_id]
    if body.enabled:
        enabled.append(sub_id)
    _fw.save_config(enabled)
    log.info("featured watch %s for sub %s",
             "enabled" if body.enabled else "disabled", sub_id)
    return {"ok": True, "sub_id": sub_id, "enabled": body.enabled,
            "enabled_list": enabled}


class FeaturedProbeBody(BaseModel):
    sub_id: str = ""


@app.post("/api/featured/probe")
def featured_probe(body: FeaturedProbeBody):
    """Run one watchdog probe round for a subscription immediately
    (same code path as the scheduled LaunchAgent round: speed-check
    the 3 精选 nodes; if all 3 are bad, replace + publish). Does NOT
    trigger a full re-test. Defers (409) while a full test runs."""
    sub_id = (body.sub_id or "").strip()
    if not sub_id or store.get_sub_meta(sub_id) is None:
        raise HTTPException(404, "subscription not found")
    cfg = _fw.load_config()
    if sub_id not in cfg.get("enabled", []):
        raise HTTPException(
            400, "monitoring is disabled for this subscription; "
                 "enable it first")
    with _current_lock:
        if _current["sub_id"] is not None:
            raise HTTPException(409, "a test is already running")
    if store.lock_held():
        raise HTTPException(409, "a full test is running; try again later")
    try:
        with _fw.WatchLock():
            result = _fw.run_round(sub_id)
    except BlockingIOError:
        raise HTTPException(409, "a watchdog round is already running")
    return result


app.mount("/", StaticFiles(directory=os.path.join(BASE, "static"),
                           html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=18937,
                log_level="info")
