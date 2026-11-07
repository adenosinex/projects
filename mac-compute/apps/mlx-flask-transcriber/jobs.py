"""SQLite-backed job queue and cooperative cancellation.

Use exactly ONE Python server process for the dispatcher. Flask may use threads
for HTTP responses, but do not run several WSGI worker processes against the
same MLX device unless you add a cross-process worker architecture.
"""
import json
from contextlib import contextmanager
import sqlite3
import threading
import time
from pathlib import Path

from asr import profile_settings

ACTIVE = {'preparing', 'running', 'formatting', 'cancelling'}
TERMINAL = {'done', 'error', 'cancelled'}


class JobCancelled(Exception):
    """Raised at a safe cancellation point."""


class JobStore:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / 'jobs.sqlite3'
        self.wakeup = threading.Event()
        self._initialize()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA busy_timeout=15000')
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self):
        with self._connect() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                extension TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                info_json TEXT NOT NULL
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS jobs_order ON jobs(status, created_at)')

    @staticmethod
    def _snapshot(row):
        if row is None:
            return None
        info = json.loads(row['info_json'])
        info.update(
            id=row['id'], filename=row['filename'], extension=row['extension'],
            status=row['status'], created_at=row['created_at'],
            updated_at=row['updated_at'],
            cancel_requested=bool(row['cancel_requested'])
        )
        if row['status'] not in TERMINAL:
            info['elapsed'] = round(max(0, time.time() - row['created_at']), 1)
        return info

    def add(self, job_id, filename, extension, file_bytes, model_profile=None, *,
            retry_of=None, model=None):
        now = time.time()
        info = {
            'percent': 0, 'processed': 0, 'duration': 0,
            'elapsed': 0, 'preprocess_elapsed': 0,
            'transcribe_elapsed': 0, 'format_elapsed': 0,
            'cue_count': 0, 'char_count': 0, 'speed_x': 0,
            'paragraph_count': 0, 'file_bytes': file_bytes,
            'token_limit_chunk_count': 0, 'timestamp_mode': 'audio_block',
            'message': '已提交，等待后台处理',
            'format_mode': 'Qwen3-ASR 原文 · 音频块时间轴',
        }
        info.update(profile_settings(model_profile))
        if model is not None:
            info['model'] = model
        if retry_of is not None:
            info['retry_of'] = retry_of
        with self._connect() as conn:
            conn.execute(
                'INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (job_id, filename, extension, 'queued', now, now, 0,
                 json.dumps(info, ensure_ascii=False))
            )
        self.wakeup.set()

    def get(self, job_id):
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            job = self._snapshot(row)
            if job and job['status'] == 'queued':
                job['queue_position'] = conn.execute(
                    'SELECT COUNT(*) FROM jobs WHERE status=? AND created_at<=?',
                    ('queued', row['created_at'])
                ).fetchone()[0]
            return job

    def list(self, limit=100):
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?',
                (limit,)
            ).fetchall()
            jobs = [self._snapshot(row) for row in rows]
            position = 0
            for job in sorted(jobs, key=lambda j: j['created_at']):
                if job['status'] == 'queued':
                    position += 1
                    job['queue_position'] = position
            return jobs

    def latest_failed(self):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='error' "
                'ORDER BY created_at DESC, rowid DESC LIMIT 1'
            ).fetchone()
            return self._snapshot(row)

    def summary(self):
        with self._connect() as conn:
            counts = dict(conn.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status'))
            latest = conn.execute(
                "SELECT id FROM jobs WHERE status='error' "
                'ORDER BY created_at DESC, rowid DESC LIMIT 1'
            ).fetchone()
        return {
            'total': sum(counts.values()),
            'clearable_count': sum(counts.get(status, 0) for status in TERMINAL),
            'latest_failed_id': latest['id'] if latest else None,
        }

    def update(self, job_id, **fields):
        now = time.time()
        status = fields.pop('status', None)
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or row['status'] in TERMINAL:
                return
            info = json.loads(row['info_json'])
            info.update(fields)
            final_status = status or row['status']
            if row['cancel_requested'] and final_status not in TERMINAL:
                final_status = 'cancelling'
            # Cancellation wins a race against final completion.
            if row['cancel_requested'] and final_status == 'done':
                final_status = 'cancelled'
                info['message'] = '已取消（处理到安全检查点）'
            conn.execute(
                'UPDATE jobs SET status=?,updated_at=?,info_json=? WHERE id=?',
                (final_status, now, json.dumps(info, ensure_ascii=False), job_id)
            )

    def cancelled(self, job_id):
        with self._connect() as conn:
            row = conn.execute(
                'SELECT cancel_requested,status FROM jobs WHERE id=?', (job_id,)
            ).fetchone()
            return row is None or bool(row['cancel_requested']) or row['status'] == 'cancelled'

    def cancel(self, job_id):
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                return 'missing'
            if row['status'] in TERMINAL:
                return 'terminal'
            info = json.loads(row['info_json'])
            if row['status'] == 'queued':
                status = 'cancelled'
                info['message'] = '已取消排队任务'
                info['elapsed'] = round(time.time() - row['created_at'], 1)
            else:
                status = 'cancelling'
                info['message'] = '取消请求已接收；正在停止预处理或等待当前推理片段结束'
            conn.execute(
                'UPDATE jobs SET status=?,cancel_requested=1,updated_at=?,info_json=? WHERE id=?',
                (status, time.time(), json.dumps(info, ensure_ascii=False), job_id)
            )
        self.wakeup.set()
        return status

    def claim_next(self):
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='queued' AND cancel_requested=0 "
                'ORDER BY created_at ASC LIMIT 1'
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE jobs SET status='preparing',updated_at=? WHERE id=?",
                (time.time(), row['id'])
            )
            return row['id'], row['extension']

    def recover(self):
        """On restart, requeue interrupted jobs from their original uploads."""
        recovered = 0
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('preparing','running','formatting','cancelling')"
            ).fetchall()
            for row in rows:
                info = json.loads(row['info_json'])
                if row['cancel_requested']:
                    state, msg = 'cancelled', '服务重启前已请求取消'
                elif not (self.data_dir / row['id'] / ('upload' + row['extension'])).is_file():
                    state, msg = 'error', '重启恢复失败：找不到上传的源文件'
                else:
                    state, msg = 'queued', '服务器已恢复任务，将从头重新识别'
                    info['percent'] = 0
                    info['processed'] = 0
                    recovered += 1
                info['message'] = msg
                if state in TERMINAL:
                    info['elapsed'] = round(time.time() - row['created_at'], 1)
                conn.execute(
                    'UPDATE jobs SET status=?,updated_at=?,info_json=? WHERE id=?',
                    (state, time.time(), json.dumps(info, ensure_ascii=False), row['id'])
                )
        if recovered:
            self.wakeup.set()
        return recovered

    def delete(self, job_id):
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                return 'missing'
            if row['status'] not in TERMINAL:
                return 'active'
            conn.execute('DELETE FROM jobs WHERE id=?', (job_id,))
        return 'deleted'

    def delete_many(self, job_ids=None):
        """Delete only terminal jobs, atomically; None means all history."""
        if job_ids is not None and not job_ids:
            return []
        statuses = sorted(TERMINAL)
        where = 'status IN (' + ','.join('?' for _ in statuses) + ')'
        params = statuses
        if job_ids is not None:
            where += ' AND id IN (' + ','.join('?' for _ in job_ids) + ')'
            params = statuses + list(job_ids)
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute('SELECT id FROM jobs WHERE ' + where, params).fetchall()
            conn.execute('DELETE FROM jobs WHERE ' + where, params)
        return [row['id'] for row in rows]


class JobHandle:
    def __init__(self, store, job_id):
        self.store = store
        self.id = job_id
        self.folder = store.data_dir / job_id

    def update(self, **fields):
        self.store.update(self.id, **fields)

    def snapshot(self):
        return self.store.get(self.id)

    def check_cancel(self):
        if self.store.cancelled(self.id):
            raise JobCancelled('任务已取消')


class Dispatcher:
    """One background thread and one MLX job at a time."""
    def __init__(self, store, run_worker):
        self.store = store
        self.run_worker = run_worker
        self.thread = None

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.store.recover()
        self.thread = threading.Thread(target=self._loop, daemon=True, name='mlx-worker')
        self.thread.start()

    def _loop(self):
        while True:
            next_job = self.store.claim_next()
            if next_job is None:
                self.store.wakeup.wait(timeout=1)
                self.store.wakeup.clear()
                continue
            job_id, extension = next_job
            handle = JobHandle(self.store, job_id)
            source = handle.folder / ('upload' + extension)
            try:
                self.run_worker(handle, source)
            except JobCancelled:
                handle.update(status='cancelled', message='任务已取消')
            except Exception as exc:
                handle.update(status='error', message=f'工作线程异常：{exc}')
            finally:
                # Worker must explicitly reach done, error, or cancelled.
                current = self.store.get(job_id)
                if current and current['status'] not in TERMINAL:
                    handle.update(status='error', message='工作线程意外退出')
