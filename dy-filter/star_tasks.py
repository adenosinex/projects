# -*- coding: utf-8 -*-
"""
star_tasks.py — 云端归档结算、批量清理与 Aria2 自动下载队列服务
=========================================================================
核心流程（点击「结算归档并下载 (3%)」或调用 /dy/api/cloud/commit 时后台异步执行）：
  1. 云端集中：将 status='starred' (score=5) 的精选视频按原目录相对路径
     (rel_path) 在 TARGET_CID 下创建对应子目录并调用 client.fs_move 移动集中。
  2. 云端清理：将 status='rejected' (score=1) 的淘汰视频按每批 500 个调用
     client.fs_delete 批量删除（进 115 回收站兜底）。
  3. 本地落盘：获取精选视频原画直链 + 请求头，按原目录相对路径推入本地
     Aria2 JSON-RPC 队列自动下载到 LOCAL_SAVE_DIR。
=========================================================================
"""

import copy
import datetime as dt
import json
import os
import posixpath
import queue
import random
import re
import threading
import time
import uuid
from pathlib import Path

import requests
from sqlalchemy import or_

from utils import (
    DB_DIR, DB_FILE,
    STATUS_STARRED, STATUS_REJECTED, STATUS_DOWNLOADED,
    Session, Video,
    load_cloud_config, save_cloud_config, p115_manager,
    library_operation_lock,
)
from fingerprint_worker import sync_cloud_videos, import_local_history_sha1


ACTIONS = {
    'commit_archive': '结算归档并下载 (3%)',
    'cloud_move_only': '仅云端集中移动精选视频 (保持相对路径)',
    'cloud_delete_rejected': '仅云端批量清理淘汰视频 (进回收站)',
    'aria2_download': '仅推送精选视频到 Aria2 下载 (保持相对路径)',
    'sync_cloud': '同步 115 待筛选目录并 SHA1 去重',
}

TERMINAL = {'completed', 'cancelled', 'failed', 'interrupted'}
HISTORY_DIR = Path(DB_DIR) / 'star_tasks'
_config_lock = threading.RLock()


def _now():
    return dt.datetime.now().isoformat(timespec='seconds')


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def get_config():
    return load_cloud_config()


def save_config(data):
    return save_cloud_config(data)


class TaskCancelled(Exception):
    pass


class TaskContext:
    def __init__(self, service, job, cancel):
        self.service = service
        self.job = job
        self.cancel = cancel
        self.config = job['config']
        self.dry_run = job['dry_run']

    def check(self):
        if self.cancel.is_set():
            raise TaskCancelled()

    def update(self, **values):
        self.service.patch(self.job['id'], **values)

    def log(self, message):
        self.service.patch(self.job['id'], log=message)

    def add(self, **values):
        with self.service.lock:
            for key, value in values.items():
                self.job['stats'][key] = self.job['stats'].get(key, 0) + value
        self.service.persist(self.job['id'])

    def advance(self, current_label):
        self.check()
        with self.service.lock:
            processed = self.job['processed'] + 1
        self.update(processed=processed, current=str(current_label))


class TaskService:
    def __init__(self):
        self.lock = threading.RLock()
        self.disk_lock = threading.Lock()
        self.jobs = {}
        self.cancel_events = {}
        self.last_saved = {}
        self.queue = queue.Queue()
        self.worker = None
        self.loaded = False

    def load(self):
        with self.lock:
            if self.loaded:
                return
            HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            for path in sorted(HISTORY_DIR.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
                if not re.fullmatch(r'[a-f0-9]{32}', path.stem):
                    continue
                try:
                    job = json.loads(path.read_text(encoding='utf-8'))
                    if job.get('id') != path.stem or job.get('action') not in ACTIONS:
                        continue
                    if job.get('status') not in TERMINAL:
                        job.update(
                            status='interrupted',
                            finished_at=_now(),
                            error='服务重启，任务已中断。已完成的操作保留，可重新运行。',
                        )
                        _write_json(path, job)
                    self.jobs[job['id']] = job
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
            self.loaded = True

    def persist(self, job_id, force=False):
        with self.disk_lock:
            with self.lock:
                if not force and time.monotonic() - self.last_saved.get(job_id, 0) < 1:
                    return True
                snapshot = copy.deepcopy(self.jobs[job_id])
                self.last_saved[job_id] = time.monotonic()
            try:
                _write_json(HISTORY_DIR / (job_id + '.json'), snapshot)
            except OSError as error:
                with self.lock:
                    self.jobs[job_id]['storage_error'] = '任务历史保存失败：' + str(error)
                return False
            return True

    def patch(self, job_id, log=None, **values):
        with self.lock:
            job = self.jobs[job_id]
            job.update(values)
            if log:
                job['logs'].append({'time': _now(), 'message': str(log)})
                job['logs'] = job['logs'][-200:]
            job['updated_at'] = _now()
        self.persist(job_id)

    def list(self):
        self.load()
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j['created_at'], reverse=True)
            return [copy.deepcopy({k: v for k, v in job.items() if k not in ('logs', 'config')}) for job in jobs[:50]]

    def get(self, job_id):
        self.load()
        with self.lock:
            return copy.deepcopy(self.jobs.get(job_id))

    def submit(self, action: str, dry_run: bool = False):
        self.load()
        if action not in ACTIONS or type(dry_run) is not bool:
            raise ValueError('任务类型或预览参数无效')
        config = get_config()

        if action in ('commit_archive', 'cloud_move_only') and not config.get('target_cid'):
            raise ValueError('请先在云端配置中填写 115 精选集中目录 (TARGET_CID)')
        if action in ('commit_archive', 'aria2_download') and not config.get('local_save_dir'):
            raise ValueError('请先在云端配置中填写本地下载保存目录 (LOCAL_SAVE_DIR)')
        if action == 'sync_cloud' and not config.get('inbox_cid'):
            raise ValueError('请先在云端配置中填写 115 待筛选目录 (INBOX_CID)')

        with self.lock:
            if sum(j['status'] not in TERMINAL for j in self.jobs.values()) >= 10:
                raise ValueError('任务队列已满，请等待正在运行的任务完成')
            job_id = uuid.uuid4().hex
            safe_cfg = {k: ('***' if k in ('cookie', 'aria2_secret') and v else v) for k, v in config.items()}
            job = {
                'id': job_id,
                'action': action,
                'title': ACTIONS[action],
                'dry_run': dry_run,
                'status': 'queued',
                'created_at': _now(),
                'updated_at': _now(),
                'started_at': None,
                'finished_at': None,
                'total': 0,
                'processed': 0,
                'current': '',
                'stats': {},
                'logs': [],
                'error': None,
                'config': safe_cfg,
            }
            self.jobs[job_id] = job
            self.cancel_events[job_id] = threading.Event()

        if not self.persist(job_id, force=True):
            with self.lock:
                self.jobs.pop(job_id, None)
                self.cancel_events.pop(job_id, None)
            raise ValueError('无法保存任务历史，请检查数据目录写入权限')

        with self.lock:
            self.queue.put(job_id)
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._work, name='dy-cloud-worker', daemon=True)
                self.worker.start()
        return self.get(job_id)

    def cancel(self, job_id):
        self.load()
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return None
            if job['status'] in TERMINAL:
                return copy.deepcopy(job)
            self.cancel_events[job_id].set()
            queued = job['status'] == 'queued'
            job.update(
                status='cancelled' if queued else 'cancelling',
                finished_at=_now() if queued else None,
                updated_at=_now(),
            )
            job['logs'].append({'time': _now(), 'message': '已请求取消；将在当前安全节点停止。'})
            result = copy.deepcopy(job)
        self.persist(job_id, force=True)
        return result

    def _work(self):
        handlers = {
            'commit_archive': _run_commit_archive,
            'cloud_move_only': _run_cloud_move,
            'cloud_delete_rejected': _run_cloud_delete,
            'aria2_download': _run_aria2_download,
            'sync_cloud': _run_sync_cloud,
        }
        while True:
            job_id = self.queue.get()
            context = TaskContext(self, self.jobs[job_id], self.cancel_events[job_id])
            acquired = False
            try:
                context.check()
                self.patch(job_id, status='running', started_at=_now(), log='准备执行云端任务…')
                while not acquired:
                    context.check()
                    acquired = library_operation_lock.acquire(timeout=0.25)
                context.check()
                context.log('开始预览数量（不修改云端与本地文件）' if context.dry_run else '开始执行任务')
                handlers[context.job['action']](context)
                context.check()
                self.patch(job_id, status='completed', finished_at=_now(), current='', log='任务完成')
            except TaskCancelled:
                self.patch(job_id, status='cancelled', finished_at=_now(), log='任务已取消，已完成的操作已保留。')
            except Exception as error:
                self.patch(job_id, status='failed', finished_at=_now(), error=str(error), log='任务失败：' + str(error))
            finally:
                if acquired:
                    library_operation_lock.release()
                try:
                    self.persist(job_id, force=True)
                finally:
                    self.queue.task_done()


# ================= 具体云端与 Aria2 执行逻辑 =================
def _rel_dir_of(video: Video, preserve_relative_path: bool) -> str:
    if not preserve_relative_path:
        return ''
    rel = (video.rel_path or video.detail or '').replace('\\', '/').strip('/')
    if '/' not in rel:
        return ''
    return posixpath.dirname(rel)


def _run_cloud_move(ctx: TaskContext) -> int:
    cfg = get_config()
    preserve = bool(cfg.get('preserve_relative_path', True))

    with Session() as session:
        starred = session.query(Video).filter(
            Video.source_type == 'cloud',
            or_(Video.status == STATUS_STARRED, Video.score == 5),
            Video.cloud_moved == 0,
            Video.file_id != '',
        ).all()

    ctx.update(total=len(starred), processed=0)
    if not starred:
        ctx.log('没有需要云端移动的精选视频。')
        return 0

    if ctx.dry_run:
        ctx.add(move_candidates=len(starred))
        ctx.log(f'[预览] 将移动 {len(starred)} 个精选视频到 {cfg["target_cid"]}（保持相对路径: {preserve}）')
        return len(starred)

    target_root_cid = p115_manager.resolve_cid(cfg['target_cid'], create_if_missing=True)
    client = p115_manager.get_client()

    # 按相对子目录分组，保持原目录相对路径结构
    groups: dict[str, list[Video]] = {}
    for v in starred:
        rdir = _rel_dir_of(v, preserve)
        groups.setdefault(rdir, []).append(v)

    moved_total = 0
    for rdir, videos in groups.items():
        ctx.check()
        dest_cid = p115_manager.ensure_cloud_rel_dir(target_root_cid, rdir) if rdir else target_root_cid
        fids = [v.file_id for v in videos]
        for i in range(0, len(fids), 500):
            ctx.check()
            chunk_fids = fids[i:i + 500]
            chunk_ids = [v.id for v in videos[i:i + 500]]
            resp = client.fs_move(chunk_fids, pid=int(dest_cid))
            if isinstance(resp, dict) and resp.get('state') is False:
                raise RuntimeError(f'云端移动失败：{resp.get("error") or resp}')
            with Session() as session:
                session.query(Video).filter(Video.id.in_(chunk_ids)).update(
                    {Video.cloud_moved: 1, Video.parent_cid: str(dest_cid)},
                    synchronize_session=False,
                )
                session.commit()
            moved_total += len(chunk_fids)
            ctx.add(cloud_moved=len(chunk_fids))
            ctx.log(f'已云端集中 {len(chunk_fids)} 个视频 -> /{rdir or "根目录"} (CID={dest_cid})')
            for v in videos[i:i + 500]:
                ctx.advance(v.rel_path or v.filename)
            time.sleep(0.3)

    return moved_total


def _run_cloud_delete(ctx: TaskContext) -> int:
    cfg = get_config()
    with Session() as session:
        rejected = session.query(Video).filter(
            Video.source_type == 'cloud',
            or_(Video.status == STATUS_REJECTED, Video.score == 1),
            Video.cloud_deleted == 0,
            Video.file_id != '',
        ).all()

    if not rejected:
        ctx.log('没有需要云端清理的淘汰视频。')
        return 0

    if ctx.dry_run:
        ctx.add(delete_candidates=len(rejected))
        ctx.log(f'[预览] 将批量删除 {len(rejected)} 个淘汰视频（进入 115 回收站兜底）')
        return len(rejected)

    client = p115_manager.get_client()
    deleted_total = 0
    for i in range(0, len(rejected), 500):
        ctx.check()
        chunk = rejected[i:i + 500]
        fids = [v.file_id for v in chunk]
        ids = [v.id for v in chunk]
        resp = client.fs_delete(fids)
        if isinstance(resp, dict) and resp.get('state') is False:
            raise RuntimeError(f'云端批量删除失败：{resp.get("error") or resp}')
        with Session() as session:
            session.query(Video).filter(Video.id.in_(ids)).update(
                {Video.cloud_deleted: 1},
                synchronize_session=False,
            )
            session.commit()
        deleted_total += len(fids)
        ctx.add(cloud_deleted=len(fids))
        ctx.log(f'已批量删除 {len(fids)} 个淘汰视频（进 115 回收站）')
        time.sleep(0.4)

    return deleted_total


def _push_aria2(aria2_rpc: str, aria2_secret: str, direct_url: str, headers: dict, save_dir: str, out_name: str, req_id: str):
    header_list = [f'{k}: {v}' for k, v in (headers or {}).items()]
    options = {
        'dir': save_dir,
        'out': out_name,
        'header': header_list,
        'max-connection-per-server': '8',
        'split': '8',
        'continue': 'true',
    }
    params = []
    if aria2_secret:
        params.append(f'token:{aria2_secret}')
    params.append([direct_url])
    params.append(options)

    payload = {
        'jsonrpc': '2.0',
        'id': req_id,
        'method': 'aria2.addUri',
        'params': params,
    }
    resp = requests.post(aria2_rpc, json=payload, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get('error'):
        raise RuntimeError(f'Aria2 RPC 错误：{data["error"]}')
    return data.get('result')


def _run_aria2_download(ctx: TaskContext) -> int:
    cfg = get_config()
    local_root = os.path.abspath(cfg['local_save_dir'])
    aria2_rpc = cfg['aria2_rpc']
    aria2_secret = cfg.get('aria2_secret', '')
    preserve = bool(cfg.get('preserve_relative_path', True))
    interval = float(cfg.get('request_interval_seconds', 1.2))

    with Session() as session:
        to_download = session.query(Video).filter(
            Video.source_type == 'cloud',
            Video.score == 5,
            Video.cp == 0,
            Video.pickcode != '',
        ).all()

    ctx.update(total=len(to_download), processed=0)
    if not to_download:
        ctx.log('没有待推送到 Aria2 下载的精选视频。')
        return 0

    if ctx.dry_run:
        ctx.add(download_candidates=len(to_download))
        ctx.log(f'[预览] 将推送 {len(to_download)} 个精选视频到 Aria2 ({aria2_rpc}) -> {local_root}')
        return len(to_download)

    os.makedirs(local_root, exist_ok=True)
    pushed = 0
    consecutive_failures = 0
    for idx, v in enumerate(to_download):
        ctx.check()
        try:
            url_info = p115_manager.get_download_url(v.pickcode)
            direct_url = url_info['url']
            if not direct_url:
                raise RuntimeError('115 返回空下载直链')

            rel_dir = _rel_dir_of(v, preserve)
            dest_dir = os.path.join(local_root, *rel_dir.split('/')) if rel_dir else local_root
            os.makedirs(dest_dir, exist_ok=True)
            out_name = os.path.basename((v.rel_path or v.filename).replace('\\', '/')) or v.filename

            gid = _push_aria2(aria2_rpc, aria2_secret, direct_url, url_info.get('headers', {}), dest_dir, out_name, v.pickcode)

            with Session() as session:
                session.query(Video).filter(Video.id == v.id).update(
                    {Video.status: STATUS_DOWNLOADED, Video.cp: 1},
                    synchronize_session=False,
                )
                session.commit()
            pushed += 1
            consecutive_failures = 0
            ctx.add(aria2_pushed=1)
            ctx.log(f'已推送 Aria2 (gid={gid})：{os.path.join(rel_dir, out_name) if rel_dir else out_name}')
        except Exception as exc:
            ctx.add(errors=1)
            ctx.log(f'推送失败 [{v.filename}]：{exc}')
            # 风控熔断：直链获取失败疑似限流/403，或连续失败 2 次，立即停止整批，
            # 不再继续调用 downurl，避免触发 115 临时封禁；同一 pickcode 不在同一次运行内重试。
            err_text = str(exc).lower()
            looks_rate_limited = any(k in err_text for k in (
                '403', 'forbidden', '429', 'rate limit', 'too many requests',
                'captcha', '验证码', '频繁', '限流', '风控',
            ))
            consecutive_failures += 1
            if looks_rate_limited or consecutive_failures >= 2:
                ctx.log('风控熔断：直链获取失败疑似 115 限流或连续失败，已停止整批推送，请等待一段时间后再重试，不要立即重新提交。')
                raise RuntimeError(
                    '直链获取失败疑似 115 限流（或连续失败），已熔断停止整批推送，'
                    '请等待一段时间后再重试。'
                )
        finally:
            ctx.advance(v.rel_path or v.filename)

        if idx < len(to_download) - 1:
            # 直链请求间隔：配置值 + 0–0.5s 随机抖动，避免固定节律被识别为自动化
            time.sleep(interval + random.uniform(0, 0.5))

    return pushed


def _run_commit_archive(ctx: TaskContext):
    cfg = get_config()
    ctx.log('阶段 1/3：云端集中移动 3% 精选视频（保持原目录相对路径）...')
    _run_cloud_move(ctx)
    ctx.check()

    if cfg.get('delete_rejected_on_commit', True):
        ctx.log('阶段 2/3：云端批量清理淘汰视频（进 115 回收站兜底）...')
        _run_cloud_delete(ctx)
    else:
        ctx.log('阶段 2/3：已跳过云端删除（配置为保留淘汰视频）。')
    ctx.check()

    ctx.log('阶段 3/3：获取精选直链并推送到本地 Aria2 下载队列...')
    _run_aria2_download(ctx)


def _run_sync_cloud(ctx: TaskContext):
    result = sync_cloud_videos()
    ctx.add(**{
        'scanned': result['total_scanned'],
        'added_pending': result['added_pending'],
        'dedup_rejected': result['dedup_rejected'],
        'skipped_existing': result['skipped_existing'],
    })
    ctx.log(
        f"同步完成：扫描 {result['total_scanned']} 个，新增待筛 {result['added_pending']} 个，"
        f"SHA1 秒级去重拦截 {result['dedup_rejected']} 个，已存在跳过 {result['skipped_existing']} 个。"
    )


task_service = TaskService()
