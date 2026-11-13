# proxy-test

Standalone subscription speed-test app on the Mac mini.

- URL: http://127.0.0.1:18935/ (binds localhost only; reach it via Tailscale
  `http://100.87.118.70:18935/` only if a forward is added — by default use
  SSH port-forward or the Mac itself)
- Start / restart: `bash /Users/zin/proxy-test/restart.sh`
- Logs: `/Users/zin/proxy-test/app.log`
- Data: `/Users/zin/proxy-test/data/` (subscriptions + test runs)
- Test runs: `/Users/zin/proxy-test/runs/<id>/` (mihomo config + log)

## Modes
1. **Single subscription** — test every node in one subscription direct
   (delay / Google / YouTube / download).
2. **Relay test** — relay subscription + exit subscription. Relays direct
   first, exits direct, then relay→exit chains. All protocols in the exit
   subscription are tested; one relay node per region.

## Circuit breaker (automatic)
- Delay pre-check (5s) first; dead proxy → skip Google/YouTube/download.
- Dead relay → skip all its chains. Dead exit+protocol → skip all its chains.
- Dead chain delay → skip its Google/YouTube/download.
- Skipped checks are recorded as "skipped (circuit-breaker: …)", not failures.

## Parallel testing (single mode, 2026-11-13)
- Default **10 workers** (configurable 1–20 in the UI; **1 = sequential**,
  the original behaviour). Each worker runs its OWN mihomo instance on
  mixed-port 17900+i / controller 19100+i, fed from a shared queue.
- **Slow-speed abort**: a download staying under 50 KB/s for 5 s is
  aborted early and recorded as "slow" with the observed speed (~5 s
  cost, not the full 45 s timeout). A download slower than ~222 KB/s
  also hits the 45 s max-time and is recorded "slow" with its speed.
- Run records `workers` and `elapsed_s`; the UI shows live progress
  (phase, current proxy, done/total, worker count) and elapsed time.

## A/B verification (2026-11-13): does parallel testing distort speeds?
Hypothesis (user): home LAN is gigabit and each node is a separate exit,
so the home link is NOT the bottleneck; ~2–3 MB/s is a per-flow limit
(high RTT, TCP window/loss), not a shared aggregate cap.
- **vyy (airport, 27 nodes)**: sequential 404 s vs parallel-10 101 s.
  10 nodes with real speeds in both runs; ratios (parallel/sequential)
  0.11–5.49, median 0.53, mean 1.56. Ratios scatter in BOTH directions
  (parallel faster for 3 nodes) — node speeds vary hugely run to run,
  so vyy alone cannot isolate a parallelism effect.
- **own proxies / vps (stable VPS nodes, 30 nodes)**: sequential 676 s
  vs parallel-10 74 s (9× faster). 16 nodes with real speeds in both;
  ratios 0.01–2.99, **median 1.01**, mean 1.27; 15/16 within 0.80–2.99.
  The single outlier (shoc v4 node, 7.07 MB/s sequential → 0.09 MB/s
  parallel) crawled the full 45 s at ~98 KB/s while sibling nodes on the
  SAME server measured 0.91–1.03 concurrently — a transient node-path
  stall, not a shared-pipe effect. Top-3 differs only because of that
  one node; excluding it, the top sets agree within 10%.
- **Conclusion: hypothesis HOLDS — no systematic distortion.** Median
  ratio 1.01, bidirectional scatter, no downward bias. Parallel-10 is
  the deployed default for single mode.
- Caveats: Cloudflare 429s on the 10 MB download endpoint appear in
  both modes (SS nodes 429'd in the parallel vps run); a 429 is not a
  node failure and is excluded from speed comparisons.

## Robustness notes (real failure, 2026-11-13)
- A stray mihomo (`-d /tmp/mihomo-speed-test`, leftover from manual
  testing) held 17893/19093. The first parallel attempt used
  17890+i/19090+i, so worker 3's mihomo failed to bind — but the
  readiness check was fooled because the stray answered /version, and
  the dead worker drained the queue with instant 404 failures (18
  nodes). Fixed: (1) ports moved to proxy-test's own range
  17900–17909 / 19100–19109, which does NOT overlap sub-convert's
  17891–17895 / 19091–19095; (2) startup verifies the controller serves
  OUR config (GET /proxies, TEST group must contain our proxy name)
  and fails fast on a port conflict; (3) each worker gets its own -d
  directory (own config/log/cache.db); (4) a dead worker requeues its
  node once and stops instead of draining the queue. The stray was
  killed. **sub-convert's runner has the same readiness-check flaw**
  (its 17891–17895 range also overlapped the stray) — not fixed here.
- Relay mode is still sequential (one mihomo); only single mode is
  parallel.

## Notes
- One test at a time; test mihomo instances use mixed-ports 17900–17909
  + controllers 19100–19109 and are stopped after each run. Never
  touches ~/sub-node/ (live proxy).
- Subscription credentials are stored server-side only and never returned
  by the API (names/types/servers only).
- No auth in v1; binds 127.0.0.1 only.
