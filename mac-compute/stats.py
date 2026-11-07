"""System stats: CPU / memory / disk sampled once a minute into SQLite.

One row per minute, 30-day retention. No external service, no new
dependency beyond psutil. The homepage draws history charts from this.

Live view: a second sampler runs every 5 seconds into an in-memory
ring buffer (last 30 min, not persisted) for the real-time chart.
"""
import sqlite3
import threading
import time
from collections import deque

import psutil

from config import DB_PATH

_lock = threading.Lock()
INTERVAL = 60            # seconds between stored samples
RETENTION_DAYS = 30
FAST_INTERVAL = 5        # seconds between live samples (memory only)
FAST_KEEP = 360          # 360 x 5s = last 30 minutes

_fast = deque(maxlen=FAST_KEEP)
_fast_lock = threading.Lock()


def init_stats_table():
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS stats (
                ts REAL PRIMARY KEY,
                cpu REAL NOT NULL,
                mem REAL NOT NULL,
                disk REAL NOT NULL,
                mem_used_gb REAL NOT NULL,
                mem_total_gb REAL NOT NULL,
                disk_used_gb REAL NOT NULL,
                disk_total_gb REAL NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stats_ts ON stats(ts)")


def _read_now(cpu_interval=1):
    cpu = psutil.cpu_percent(interval=cpu_interval)
    vm = psutil.virtual_memory()
    du = psutil.disk_usage("/")
    return {
        "ts": time.time(),
        "cpu": round(cpu, 1),
        "mem": round(vm.percent, 1),
        "disk": round(du.percent, 1),
        "mem_used_gb": round(vm.used / 1e9, 2),
        "mem_total_gb": round(vm.total / 1e9, 2),
        "disk_used_gb": round(du.used / 1e9, 2),
        "disk_total_gb": round(du.total / 1e9, 2),
    }


def sample_once():
    """Take one sample and store it. Returns the row dict."""
    row = _read_now()
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO stats VALUES (?,?,?,?,?,?,?,?)",
            (row["ts"], row["cpu"], row["mem"], row["disk"],
             row["mem_used_gb"], row["mem_total_gb"],
             row["disk_used_gb"], row["disk_total_gb"]),
        )
        conn.execute("DELETE FROM stats WHERE ts < ?",
                     (time.time() - RETENTION_DAYS * 86400,))
    return row


def fast_sample_loop():
    """Live sampler: every 5s into the in-memory ring buffer only."""
    psutil.cpu_percent(interval=None)  # prime the counter
    while True:
        try:
            with _fast_lock:
                _fast.append(_read_now(cpu_interval=None))
        except Exception:
            pass
        time.sleep(FAST_INTERVAL)


def get_fast(seconds=900):
    """Live points from the last `seconds` (default 15 min)."""
    cutoff = time.time() - seconds
    with _fast_lock:
        return [p for p in _fast if p["ts"] >= cutoff]


def query_stats(since_ts):
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM stats WHERE ts >= ? ORDER BY ts ASC", (since_ts,)
        ).fetchall()
    return [dict(r) for r in rows]


def job_intervals(since_ts, until_ts):
    """Jobs that ran in [since, until], for shading on the charts."""
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, type, filename, status, started_at, finished_at"
            " FROM jobs WHERE started_at IS NOT NULL"
            " AND started_at <= ? AND COALESCE(finished_at, ?) >= ?"
            " ORDER BY started_at ASC",
            (until_ts, until_ts, since_ts),
        ).fetchall()
    return [dict(r) for r in rows]


def collector_loop():
    """Background sampler: one sample per minute, forever."""
    psutil.cpu_percent(interval=None)  # prime the counter
    while True:
        try:
            sample_once()
        except Exception:
            pass  # a failed sample must never kill the service
        time.sleep(INTERVAL)
