# sub-convert · 订阅转换

FastAPI on 127.0.0.1:18937. Add a subscription by URL or file upload; it
is speed-tested (REAL download speed) and converted into a new Clash
subscription served at `/s/<token>.yaml` (the token IS the credential).
Top 3 nodes by download speed go into a "精选" group; node names carry a
speed suffix accurate to 0.1 MB (e.g. `香港-优化2 4.9MB`). An auxiliary
subscription can be attached as relay source; the fastest 3 relay chains
go into a "精选中继" group. Original proxy-groups / rules / dns are
preserved verbatim — only node names are renamed (speed suffix, with
references inside groups updated).

## Speed test

Delay pre-check (5s) → Google → YouTube → Cloudflare 10MB download in
one round, per node. Speed = measured download speed, not delay. Slow
nodes abort early (under 50 KB/s sustained 5s → "slow", ~5s cost, not
45s). Circuit breaker: dead delay pre-check skips Google/YouTube/
download; dead relay/exit short-circuits its chains. No suffix and no
backfill for unmeasured nodes.

## Parallelism (A/B-verified 2026-11-13)

- **10 workers by default** (configurable 1–20 in the UI; 1 = sequential
  fallback). A/B test on proxy-test (30 stable VPS nodes): sequential
  676s vs 10-worker 74.4s (9× faster); per-node speed ratios median
  1.01, 15/16 within 0.80–2.99, scatter bidirectional — no systematic
  distortion. The ~2-3 MB/s per-node speed is a per-flow limit (high
  RTT, TCP window/loss), NOT a shared aggregate cap, so parallel
  download tests do not distort speeds.
- **One mihomo instance per worker**, each on its own mixed-port /
  controller port: **17910–17919 / 19110–19119** (sub-convert's own
  range; proxy-test uses 17900–17909 / 19100–19109).
- **Each worker gets its own -d directory** (`run/worker-<id>/` with its
  own config.yaml, log, cache.db).
- **Controller-ownership check at startup**: after /version answers,
  the controller's /proxies must show OUR TEST group containing our
  proxy name — fail fast on a port conflict instead of silently testing
  through a stray mihomo (a stray on 17893 poisoned proxy-test's first
  parallel run, 2026-11-13).
- **Dead-worker guard**: a worker whose mihomo died requeues its node
  once for a healthy worker and stops; leftover queued nodes are
  recorded as failed, never dropped.

## Hourly auto-update

Cron `0 * * * * hourly.py` (fcntl lock shared with manual tests).
Re-tests every saved subscription with the default 10 workers and
regenerates the YAML; the token URL is stable. Logs workers + elapsed
per subscription to `cron.log`.

## Files

- `app.py` — FastAPI app, `/api/*` + `/s/<token>.yaml` + static UI
- `runner.py` — parallel test runner (mihomo + curl)
- `convert.py` — YAML builder (preserves source structure)
- `parser.py` — subscription parser (links / Clash YAML / base64)
- `store.py` — subscription storage + fcntl test lock
- `hourly.py` — hourly cron entry point
- `static/index.html` — management UI (live progress, elapsed time,
  worker count, per-node results)

Restart: `bash /Users/zin/sub-convert/restart.sh`
