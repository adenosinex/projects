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
        # output_deleted (2026-11-14): 1 once the job's output files
        # were removed by the video-retention sweep or the disk guard.
        # The row stays in history; only the files are gone. ALTER is
        # a no-op if the column already exists.
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN output_deleted INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass


def list_done_with_output(types=None):
    """Done jobs whose output files have not been removed yet, oldest
    finished first. Used by the video-output retention sweep and the
    disk guard. Returns dicts with id/type/finished_at/created_at."""
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if types:
            ph = ",".join("?" * len(types))
            rows = conn.execute(
                f"SELECT id, type, finished_at, created_at FROM jobs "
                f"WHERE status='done' AND output_deleted=0 AND type IN ({ph}) "
                f"ORDER BY finished_at ASC",
                list(types)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, type, finished_at, created_at FROM jobs "
                "WHERE status='done' AND output_deleted=0 "
                "ORDER BY finished_at ASC").fetchall()
    return [dict(r) for r in rows]


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


def list_jobs_by_types(types, limit=50):
    """Most recent jobs of the given types only (for per-project token
    pages). Filters in SQL so an old job of a rare type (e.g. ocr) is
    not pushed out by the newest 200 rows of a busy type."""
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        ph = ",".join("?" * len(types))
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE type IN ({ph}) "
            f"ORDER BY created_at DESC LIMIT ?",
            (*types, limit)).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["params"] = json.loads(d["params"] or "{}")
        out.append(d)
    return out


def count_jobs():
    with _lock, sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def all_job_ids():
    """All job ids currently in the DB (used to spot orphaned files)."""
    with _lock, sqlite3.connect(DB_PATH) as conn:
        return set(r[0] for r in conn.execute("SELECT id FROM jobs"))


def delete_job(job_id):
    """Delete one job row. Refuses queued/running jobs (they may still
    be writing files). Returns the job dict, or None if not found.
    Raises ValueError if the job is still active. File cleanup is the
    caller's job (see app._delete_job_files)."""
    j = get_job(job_id)
    if not j:
        return None
    if j["status"] in ("queued", "running"):
        raise ValueError(f"job {job_id} is {j['status']}, cannot delete")
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return j


def prune_history(max_jobs, max_age_days):
    """Delete terminal jobs beyond the retention limits. Returns the
    list of deleted job ids. Never touches queued/running jobs, so a
    prune can never delete a job that is still working or waiting.
    max_jobs: keep at most this many most-recent terminal jobs
    (0 = delete all terminal jobs). max_age_days: delete terminal jobs
    older than this (by created_at)."""
    now = time.time()
    with _lock, sqlite3.connect(DB_PATH) as conn:
        ids = []
        if max_age_days is not None and max_age_days >= 0:
            cutoff = now - max_age_days * 86400
            rows = conn.execute(
                "SELECT id FROM jobs WHERE created_at < ?"
                " AND status NOT IN ('queued','running')",
                (cutoff,)).fetchall()
            ids += [r[0] for r in rows]
        if max_jobs is not None and max_jobs >= 0:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status NOT IN ('queued','running')"
                " ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                (max_jobs,)).fetchall()
            ids += [r[0] for r in rows]
        # Dedupe, keep order (oldest first is fine for the caller).
        seen = set()
        deleted = []
        for job_id in ids:
            if job_id not in seen:
                seen.add(job_id)
                deleted.append(job_id)
        for job_id in deleted:
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return deleted


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
