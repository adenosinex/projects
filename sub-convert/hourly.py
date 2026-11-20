#!/usr/bin/env python3
"""Hourly auto-update for sub-convert (run from cron).

Re-tests every saved subscription (REAL download speed test, parallel,
with slow-speed abort), re-selects the top 3 featured nodes by download
speed and the top 3 featured relay chains (when an auxiliary
subscription is attached), and regenerates the converted YAML. The
converted URL (token) stays the same; only its contents change.

Skips cleanly if a manual test is already running (shared fcntl lock).
Called as: /Users/zin/sub-convert/venv/bin/python /Users/zin/sub-convert/hourly.py
Cron: 0 * * * * /Users/zin/sub-convert/venv/bin/python /Users/zin/sub-convert/hourly.py >> /Users/zin/sub-convert/cron.log 2>&1
"""
import logging
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402
from app import run_test_for_sub  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(os.path.join(BASE, "cron.log")),
              logging.StreamHandler()])
log = logging.getLogger("sub-convert-hourly")


# Auto-test policy (2026-11-18, user): only the self-hosted 'vps' sub
# (debi/shoc) is tested on schedule. Third-party airports (vyy, 渔云,
# nc, planb, 多出口聚合) are MANUAL-ONLY -- test them from the web UI.
AUTO_TEST_SUB_IDS = {"419387b4d5dc"}  # vps


def main():
    # Daytime only (2026-11-18, user): speed tests run 08:00-23:00 only.
    import datetime as _dt
    _h = _dt.datetime.now().hour
    if _h < 8 or _h >= 23:
        log.info("hourly: outside 08:00-23:00 (hour=%d), skip", _h)
        return 0
    store.init()
    subs = [m for m in store.list_subs() if m["id"] in AUTO_TEST_SUB_IDS]
    if not subs:
        log.info("hourly: no subscriptions, nothing to do")
        return 0
    try:
        with store.TestLock():
            for meta in subs:
                try:
                    import time as _time
                    t0 = _time.time()
                    res = run_test_for_sub(meta["id"])
                    el = res.get("elapsed_s") or round(_time.time() - t0, 1)
                    wk = (res.get("summary") or {}).get("workers") or 10
                    log.info("hourly: sub %s done in %.1fs workers=%d: %s featured=%s relay=%s",
                             meta["id"], el, wk, res["summary"],
                             [f["name"] for f in res["featured"]],
                             [f["name"] for f in res["featured_relay"]])
                except Exception as e:
                    log.error("hourly: sub %s failed: %s\n%s", meta["id"],
                              e, traceback.format_exc())
    except BlockingIOError:
        log.info("hourly: skipped, a test is already running")
        return 0
    log.info("hourly: all done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
