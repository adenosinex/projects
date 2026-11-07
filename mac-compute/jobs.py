"""Job store: SQLite, one row per job. No external DB needed."""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from config import ASR_MAX_CONCURRENT, DB_PATH, MAX_RETRIES

_lock = threading.Lock()


def init_db():
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                filename TEXT NOT NULL,
                params TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued',
                progress REAL NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                result TEXT,
                error TEXT,
                retries INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at)")


def create_job(job_type, filename, params):
    job_id = uuid.uuid4().hex[:16]
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO jobs (id, type, filename, params, status, created_at)"
            " VALUES (?, ?, ?, ?, 'queued', ?)",
            (job_id, job_type, filename, json.dumps(params, ensure_ascii=False), time.time()),
        )
    return job_id


def get_job(job_id):
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["params"] = json.loads(d["params"] or "{}")
    if d.get("result"):
        try:
            d["result"] = json.loads(d["result"])
        except Exception:
            pass
    return d


def list_jobs(limit=50):
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["params"] = json.loads(d["params"] or "{}")
        out.append(d)
    return out


def update_job(job_id, **fields):
    if not fields:
        return
    if "result" in fields and isinstance(fields["result"], (dict, list)):
        fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
    keys = ", ".join(f"{k}=?" for k in fields)
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE jobs SET {keys} WHERE id=?", (*fields.values(), job_id))


def reset_stale_running():
    """On startup, jobs left 'running' by a killed/restarted process
    must not block the queue forever: re-queue them (retries+1), or
    dead-letter them once retries are exhausted."""
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE jobs SET status='dead', error='interrupted by service restart, retries exhausted', finished_at=? "
            "WHERE status='running' AND retries>=?",
            (time.time(), MAX_RETRIES),
        )
        conn.execute(
            "UPDATE jobs SET status='queued', retries=retries+1, progress=0, "
            "error='interrupted by service restart, re-queued', message='re-queued after restart' "
            "WHERE status='running'",
        )


def next_queued(heavy_types):
    """Pick next job: heavy jobs run one at a time, light jobs up to 4.
    A running heavy job blocks other heavy jobs, not light ones."""
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        running = conn.execute(
            "SELECT type FROM jobs WHERE status='running'"
        ).fetchall()
        running_types = [r["type"] for r in running]
        heavy_running = any(t in heavy_types for t in running_types)
        running_asr = sum(1 for t in running_types if t == "asr")
        heavy_running_other = any(
            t in heavy_types and t != "asr" for t in running_types)
        light_running = sum(1 for t in running_types if t not in heavy_types)
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at ASC"
        ).fetchall()
        for row in rows:
            t = row["type"]
            if t == "asr":
                # Experiment: up to ASR_MAX_CONCURRENT ASR jobs at once,
                # but never alongside another heavy (GPU) job.
                if heavy_running_other or running_asr >= ASR_MAX_CONCURRENT:
                    continue
                d = dict(row)
                d["params"] = json.loads(d["params"] or "{}")
                return d
            if t in heavy_types:
                if heavy_running:
                    continue
                d = dict(row)
                d["params"] = json.loads(d["params"] or "{}")
                return d
            if light_running >= 4:
                continue
            d = dict(row)
            d["params"] = json.loads(d["params"] or "{}")
            return d
    return None
