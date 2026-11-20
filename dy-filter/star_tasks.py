# -*- coding: utf-8 -*-
"""
star_tasks.py — 云端归档结算、批量清理与 Aria2 自动下载队列服务
=========================================================================
核心流程（点击「结算归档并下载 (3%)」或调用 /dy/api/cloud/commit 时后台异步执行）：
  1. 云端集中：将 status='starred' (score=5) 的精选视频按原目录相对路径
     (rel_path)，在 TARGET_CID 下新建一个日期文件夹（如 2026-11-18）存放，
     再在该文件夹内按相对路径创建对应子目录并调用 client.fs_move 移动集中；
     不直接全部放到 TARGET_CID 根目录。
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
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import requests
from sqlalchemy import or_

from utils import (
    DB_DIR, DB_FILE, THUMBNAIL_DIR,
    STATUS_STARRED, STATUS_REJECTED, STATUS_DOWNLOADED,
    Session, Video,
    load_cloud_config, save_cloud_config, p115_manager,
    library_operation_lock,
    cache_mark_indexed, cache_rename_rel_path, cache_rename_many, cache_upsert_many,
)
from fingerprint_worker import sync_cloud_videos, import_local_history_sha1
from local_source import (
    load_local_source, call_local_settle,
    LOCAL_RECYCLE_FOLDER, LOCAL_SETTLE_FOLDER_DEFAULT,
)


ACTIONS = {
    'commit_archive': '结算归档并下载 (3%)',
    'cloud_move_only': '仅云端集中移动精选视频 (保持相对路径)',
    'cloud_delete_rejected': '仅云端批量清理淘汰视频 (进回收站)',
    'aria2_download': '仅推送精选视频到 Aria2 下载 (保持相对路径)',
    'sync_cloud': '同步 115 待筛选目录并 SHA1 去重',
    'move_small_videos': '按大小整理：移动 <1GB 视频到子文件夹（保持相对路径）',
    'move_large_videos': '按大小整理：移动 ≥1GB 视频到子文件夹（保持相对路径）',
    'mark_small_indexed': '标记小文件已索引：小文件无需下载到本地、无需本地重索引（内部标记）',
    'prefetch_thumbnails': '后台预取封面：低频逐个截取中间帧存本地（跳过已有封面）',
    'settle_local': '结算本地：精选移到电脑精选文件夹，淘汰移到回收站（不删除）',
}

TERMINAL = {'completed', 'cancelled', 'failed', 'interrupted'}
HISTORY_DIR = Path(DB_DIR) / 'star_tasks'
_config_lock = threading.RLock()

# 按大小整理：已归入“小于/大于 XMB/GB”子文件夹的视频不再参与，避免换阈值/文件夹重跑时嵌套移动；两个方向互相跳过已归入对方文件夹的视频。
_SIZE_FOLDER_RE = re.compile(r'^(?:小于|大于)\d+(?:\.\d+)?(?:MB|GB)$')
MOVE_SIZE_MB_MIN = 10
MOVE_SIZE_MB_MAX = 10240


def is_size_folder_path(rel_path):
    """rel_path 的首段是否为按大小整理生成的子文件夹（如 小于1GB / 大于1GB）。"""
    first = (rel_path or '').replace('\\', '/').strip('/').split('/')[0]
    return bool(_SIZE_FOLDER_RE.match(first))


def default_size_folder(size_mb, direction='small'):
    """按阈值自动生成目标文件夹名：small 1024MB -> 小于1GB，large 1024MB -> 大于1GB。"""
    prefix = '大于' if direction == 'large' else '小于'
    if size_mb >= 1024 and size_mb % 1024 == 0:
        return '%s%dGB' % (prefix, size_mb // 1024)
    return '%s%dMB' % (prefix, size_mb)


def normalize_move_params(params, cfg, direction='small'):
    """校验并归一化按大小整理任务的单次参数；不写回云端配置。
    direction='small'：移动 < max_size_mb 的视频，默认文件夹 小于X；
    direction='large'：移动 >= min_size_mb 的视频，默认文件夹 大于X。"""
    params = params or {}
    if direction == 'large':
        key = 'min_size_mb'
        raw_mb = params.get('min_size_mb')
        if raw_mb is None:
            raw = cfg.get('large_move_min_size')
            size_mb = int(raw) // 1048576 if raw else 1024
        else:
            try:
                size_mb = int(float(raw_mb))
            except (TypeError, ValueError):
                raise ValueError('阈值必须是数字（单位 MB）')
    else:
        key = 'max_size_mb'
        raw_mb = params.get('max_size_mb')
        if raw_mb is None:
            raw = cfg.get('small_move_max_size')
            size_mb = int(raw) // 1048576 if raw else 1024
        else:
            try:
                size_mb = int(float(raw_mb))
            except (TypeError, ValueError):
                raise ValueError('阈值必须是数字（单位 MB）')
    size_mb = max(MOVE_SIZE_MB_MIN, min(MOVE_SIZE_MB_MAX, size_mb))
    folder = params.get('folder')
    folder = folder.strip() if isinstance(folder, str) else ''
    if not folder:
        folder = default_size_folder(size_mb, direction)
    if '/' in folder or '\\' in folder or len(folder) > 50:
        raise ValueError('目标文件夹名不能包含 / 或 \\，且不超过 50 个字符')
    return {key: size_mb, 'folder': folder}


# ---- 本地来源结算 ----
def normalize_settle_local_params(params):
    """校验并归一化本地结算的单次参数；不写回云端配置。

    folder：精选目标文件夹名（默认 _dy_精选），单个文件夹名，不含 / 或 \\，
    不超过 50 字，不能是回收站 _dy_回收站。淘汰固定进 _dy_回收站。"""
    params = params or {}
    folder = params.get('folder')
    folder = folder.strip() if isinstance(folder, str) else ''
    if not folder:
        folder = LOCAL_SETTLE_FOLDER_DEFAULT
    if '/' in folder or '\\' in folder or len(folder) > 50 or folder == LOCAL_RECYCLE_FOLDER:
        raise ValueError('目标文件夹名不能包含 / 或 \\，不超过 50 个字符，也不能是 _dy_回收站')
    return {'folder': folder}


# ---- 后台预取封面 ----
# 频次安全：用户实测短时间约 200 个封面触发 115 账号异常，前端限流器按 150/10 分钟
# 暂停。预取默认每 10 秒 1 张（60/10 分钟），上限每 5 秒 1 张（120/10 分钟），
# 始终低于限流阈值；只用 m3u8 截帧，不取下载直链。
PREFETCH_INTERVAL_MIN = 5
PREFETCH_INTERVAL_MAX = 60
PREFETCH_INTERVAL_DEFAULT = 10
PREFETCH_MAX_MIN = 1
PREFETCH_MAX_MAX = 500
PREFETCH_MAX_DEFAULT = 100
_FFMPEG_BIN = shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'
_FFPROBE_BIN = shutil.which('ffprobe') or '/opt/homebrew/bin/ffprobe'
_MIDFRAME_TIMEOUT = 30
_AUTH_ERROR_HINTS = ('911', '请重新登录', '登录', '验证码', '频繁', '限流', '风控', '账号异常')


def normalize_prefetch_params(params):
    """校验并归一化后台预取封面的单次参数；不写回云端配置。"""
    params = params or {}
    try:
        interval = float(params.get('interval_seconds', PREFETCH_INTERVAL_DEFAULT))
    except (TypeError, ValueError):
        raise ValueError('间隔必须是数字（单位秒）')
    interval = max(PREFETCH_INTERVAL_MIN, min(PREFETCH_INTERVAL_MAX, interval))
    try:
        max_items = int(float(params.get('max_items', PREFETCH_MAX_DEFAULT)))
    except (TypeError, ValueError):
        raise ValueError('数量必须是数字')
    max_items = max(PREFETCH_MAX_MIN, min(PREFETCH_MAX_MAX, max_items))
    return {'interval_seconds': interval, 'max_items': max_items}


def _midframe_cached(video_id):
    path = os.path.join(THUMBNAIL_DIR, f'midframe_{video_id}.jpg')
    return os.path.exists(path) and os.path.getsize(path) > 0


def _probe_duration(url):
    """用 ffprobe 读流的时长（秒，float）；读不到返回 None。"""
    cmd = [_FFPROBE_BIN, '-v', 'error',
           '-show_entries', 'format=duration',
           '-of', 'default=noprint_wrappers=1:nokey=1', url]
    try:
        result = subprocess.run(cmd, timeout=15,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        value = float(result.stdout.decode('utf-8', 'replace').strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _extract_midframe_from_url(video_id, url, mid):
    """截帧核心：ffmpeg 从给定 url 的 mid 秒处取一帧存缓存；命令、缓存路径、
    超时与 app.py 的 midframe 接口一致。调用方须先查缓存。
    返回 (ok, auth_error, message)；auth_error 仅云端可能为 True。"""
    out_path = os.path.join(THUMBNAIL_DIR, f'midframe_{video_id}.jpg')
    os.makedirs(THUMBNAIL_DIR, exist_ok=True)
    tmp_path = out_path + f'.{threading.get_ident()}.jpg'
    cmd = [_FFMPEG_BIN, '-hide_banner', '-loglevel', 'error',
           '-user_agent', 'Mozilla/5.0',
           '-ss', str(mid), '-i', url,
           '-frames:v', '1', '-q:v', '3', '-f', 'image2', '-y', tmp_path]
    try:
        result = subprocess.run(cmd, timeout=_MIDFRAME_TIMEOUT,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
    except subprocess.TimeoutExpired:
        return False, False, 'ffmpeg 超时（30s）'
    except Exception as error:
        return False, False, 'ffmpeg 执行失败: ' + str(error)[:120]
    try:
        if result.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            os.replace(tmp_path, out_path)
            return True, False, ''
        err = result.stderr.decode('utf-8', 'replace')[-200:] if result.stderr else ''
        if any(h in err for h in _AUTH_ERROR_HINTS):
            return False, True, 'ffmpeg 被拒（疑似账号异常）: ' + err[:120]
        return False, False, f'ffmpeg 失败 rc={result.returncode} ' + err[:120]
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _extract_midframe(video_id, pickcode, duration):
    """云端中间帧：m3u8 -> ffmpeg 中间位置取一帧 -> db/thumbnails/midframe_<id>.jpg，
    逻辑与 app.py 的 midframe 接口一致。
    返回 (ok, auth_error, message)：auth_error 为 True 表示疑似 115 账号异常，
    调用方应立即停止任务。"""
    if _midframe_cached(video_id):
        return True, False, '已有缓存'
    try:
        url, _stype, _expire = p115_manager.get_stream_url(pickcode)
    except Exception as error:
        message = str(error)
        if any(h in message for h in _AUTH_ERROR_HINTS):
            return False, True, '获取播放地址失败（疑似账号异常/需重新登录）: ' + message[:120]
        return False, False, '获取播放地址失败: ' + message[:120]
    if not url:
        return False, False, '播放地址为空'
    mid = max(0, int(duration or 0) // 2)
    return _extract_midframe_from_url(video_id, url, mid)


def _extract_local_midframe(video_id, duration):
    """本地中间帧：local-play 代理 URL -> ffmpeg 中间位置取一帧；
    命令/缓存/超时与云端完全一致，只换输入与取时长方式。
    返回 (ok, auth_error, message)；auth_error 恒为 False（不走 115）。
    时长缺失时用 ffprobe 读流，失败按 10 秒；ffprobe 读到的时长写回数据库。"""
    if _midframe_cached(video_id):
        return True, False, '已有缓存'
    port = int(os.environ.get('VIDEO_TAG_PORT', 81))
    url = f'http://127.0.0.1:{port}/dy/videos/{video_id}/local-play'
    if not duration or duration <= 0:
        probed = _probe_duration(url)
        if probed and probed > 0:
            duration = probed
            with Session() as session:
                session.query(Video).filter(Video.id == video_id).update(
                    {Video.duration: int(probed)}, synchronize_session=False)
                session.commit()
    mid = max(0, int(duration or 10) // 2)
    return _extract_midframe_from_url(video_id, url, mid)


def _local_client_reachable():
    """本地客户端是否可达（GET /health，带 token，5 秒超时）。
    返回 (reachable, message)：不可达时 message 说明原因。"""
    src = load_local_source()
    if not src or not src.get('base_url') or not src.get('token'):
        return False, '尚未同步本地来源'
    try:
        resp = requests.get(src['base_url'].rstrip('/') + '/health',
                            params={'token': src['token']}, timeout=5)
    except requests.RequestException as exc:
        return False, '无法连接本地客户端（%s）' % exc
    if resp.status_code == 401:
        return False, '本地客户端拒绝访问：token 无效，请重新同步本地来源'
    if resp.status_code != 200:
        return False, '本地客户端返回错误（HTTP %d）' % resp.status_code
    return True, ''


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

    def submit(self, action: str, dry_run: bool = False, params=None):
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
        move_params = None
        prefetch_params = None
        settle_params = None
        if action == 'move_small_videos':
            if not config.get('inbox_cid'):
                raise ValueError('请先在云端配置中填写 115 待筛选目录 (INBOX_CID)')
            move_params = normalize_move_params(params, config)
        elif action == 'mark_small_indexed':
            if not config.get('inbox_cid'):
                raise ValueError('请先在云端配置中填写 115 待筛选目录 (INBOX_CID)')
            move_params = normalize_move_params(params, config)
        elif action == 'move_large_videos':
            if not config.get('inbox_cid'):
                raise ValueError('请先在云端配置中填写 115 待筛选目录 (INBOX_CID)')
            move_params = normalize_move_params(params, config, direction='large')
        elif action == 'prefetch_thumbnails':
            # inbox_cid 只约束云端部分：没有 inbox_cid 但库里有本地视频时，允许只预取本地封面。
            if not config.get('inbox_cid') and not _has_local_videos():
                raise ValueError('请先在云端配置中填写 115 待筛选目录 (INBOX_CID)，或先同步本地来源')
            prefetch_params = normalize_prefetch_params(params)
        elif action == 'settle_local':
            settle_params = normalize_settle_local_params(params)

        with self.lock:
            if sum(j['status'] not in TERMINAL for j in self.jobs.values()) >= 10:
                raise ValueError('任务队列已满，请等待正在运行的任务完成')
            # 同步去重：已有同步在跑（或排队）时不再新建，直接返回那个任务。
            # 否则重复点“同步云端”会排起多次全量扫描（每次 261 个目录），
            # 三倍调用 115 接口，加重账号风控压力；前端也会切到新排队的空任务，
            # 看起来像“一直没进度”。
            if action in ('sync_cloud', 'move_small_videos', 'move_large_videos', 'mark_small_indexed', 'prefetch_thumbnails', 'settle_local'):
                for j in sorted(self.jobs.values(), key=lambda x: x['created_at'], reverse=True):
                    if j['action'] == action and j['status'] in ('running', 'queued'):
                        return copy.deepcopy(j)
            job_id = uuid.uuid4().hex
            safe_cfg = {k: ('***' if k in ('cookie', 'aria2_secret') and v else v) for k, v in config.items()}
            job = {
                'id': job_id,
                'action': action,
                'title': ACTIONS[action],
                'dry_run': dry_run,
                'params': move_params if move_params is not None else (
                    prefetch_params if prefetch_params is not None else settle_params),
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
            'move_small_videos': _run_move_small_videos,
            'move_large_videos': _run_move_large_videos,
            'mark_small_indexed': _run_mark_small_indexed,
            'prefetch_thumbnails': _run_prefetch_thumbnails,
            'settle_local': _run_settle_local,
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

    # 每次移动新建一个日期文件夹存放，不直接全部放到 /精选集中营 根目录
    batch_folder = dt.datetime.now().strftime('%Y-%m-%d')

    if ctx.dry_run:
        ctx.add(move_candidates=len(starred))
        ctx.log(f'[预览] 将移动 {len(starred)} 个精选视频到 {cfg["target_cid"]}/{batch_folder}（保持相对路径: {preserve}）')
        return len(starred)

    target_root_cid = p115_manager.resolve_cid(cfg['target_cid'], create_if_missing=True)
    batch_root_cid = p115_manager.ensure_cloud_rel_dir(target_root_cid, batch_folder)
    ctx.log(f'本次移动新建文件夹：{cfg["target_cid"]}/{batch_folder}（CID={batch_root_cid}）')
    ctx.update(dest_folder=f'{cfg["target_cid"]}/{batch_folder}', dest_folder_cid=str(batch_root_cid))
    client = p115_manager.get_client()

    # 按相对子目录分组，保持原目录相对路径结构
    groups: dict[str, list[Video]] = {}
    for v in starred:
        rdir = _rel_dir_of(v, preserve)
        groups.setdefault(rdir, []).append(v)

    moved_total = 0
    for rdir, videos in groups.items():
        ctx.check()
        dest_cid = p115_manager.ensure_cloud_rel_dir(batch_root_cid, rdir) if rdir else batch_root_cid
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
            ctx.log(f'已云端集中 {len(chunk_fids)} 个视频 -> {batch_folder}/{rdir or "根目录"} (CID={dest_cid})')
            for v in videos[i:i + 500]:
                ctx.advance(v.rel_path or v.filename)
            time.sleep(0.3)

    return moved_total


def _run_move_small_videos(ctx: TaskContext) -> int:
    """按大小整理：把待筛选目录里小于阈值的视频移动到 <inbox>/<目标文件夹>/ 子文件夹，保持相对路径。

    只做 fs_move（改父目录指针），不取直链、不下载。fs_move 是元数据操作，
    不生成下载链接，所以不触发 911 那类直链风控；但仍按 1s/批限速、顺序执行，
    避免短时间大量调用被判定为异常。已移动过的（rel_path 已在目标文件夹下，
    或已在任意“小于XMB/GB”子文件夹下）和已精选移动/已删除的跳过，可重复运行。
    阈值与文件夹来自任务单次参数（默认 1024MB / 小于1GB），不改云端配置。
    """
    cfg = get_config()
    inbox_cid = str(cfg.get('inbox_cid') or '').strip()
    if not inbox_cid:
        raise ValueError('请先在云端配置中填写 115 待筛选目录 (INBOX_CID)')
    params = normalize_move_params(ctx.job.get('params'), cfg)
    max_size = params['max_size_mb'] * 1048576
    folder_name = params['folder']
    prefix = folder_name + '/'

    with Session() as session:
        q = session.query(Video).filter(
            Video.source_type == 'cloud',
            Video.inbox_cid == inbox_cid,
            Video.file_size > 0,
            Video.file_size < max_size,
            Video.file_id != '',
            Video.cloud_moved == 0,
            Video.cloud_deleted == 0,
        ).all()
        candidates = [v for v in q
                      if not (v.rel_path or '').replace('\\', '/').strip('/').startswith(prefix)
                      and not is_size_folder_path(v.rel_path)]

    ctx.update(total=len(candidates), processed=0)
    if not candidates:
        ctx.log(f'没有需要移动的小于 {params["max_size_mb"]}MB 视频。')
        return 0

    total_bytes = sum(int(v.file_size or 0) for v in candidates)
    ctx.log(f'待移动 {len(candidates)} 个视频（<{max_size / 1073741824:.2f}GB），合计约 {total_bytes / 1073741824:.2f} GB -> {folder_name}/（保持相对路径）')
    if ctx.dry_run:
        ctx.add(move_candidates=len(candidates))
        return len(candidates)

    small_root_cid = p115_manager.ensure_cloud_rel_dir(inbox_cid, folder_name)
    client = p115_manager.get_client()

    groups: dict[str, list[Video]] = {}
    for v in candidates:
        rdir = _rel_dir_of(v, True)
        groups.setdefault(rdir, []).append(v)

    moved_total = 0
    for rdir, videos in groups.items():
        ctx.check()
        dest_cid = p115_manager.ensure_cloud_rel_dir(small_root_cid, rdir) if rdir else small_root_cid
        for i in range(0, len(videos), 200):
            ctx.check()
            chunk = videos[i:i + 200]
            chunk_fids = [v.file_id for v in chunk]
            resp = client.fs_move(chunk_fids, pid=int(dest_cid))
            if isinstance(resp, dict) and resp.get('state') is False:
                raise RuntimeError(f'云端移动失败：{resp.get("error") or resp}')
            rename_pairs = []
            with Session() as session:
                for v in chunk:
                    orig_rel = (v.rel_path or v.detail or v.filename).replace('\\', '/').strip('/')
                    new_rel = (prefix + orig_rel).strip('/')
                    session.query(Video).filter(Video.id == v.id).update(
                        {Video.parent_cid: str(dest_cid), Video.rel_path: new_rel},
                        synchronize_session=False,
                    )
                    rename_pairs.append((orig_rel, new_rel))
                session.commit()
            # 文件身份缓存键跟随改名（保持缓存与 Video.rel_path 一致）；失败不影响移动。
            cache_rename_many(rename_pairs)
            moved_total += len(chunk_fids)
            ctx.add(small_moved=len(chunk_fids))
            ctx.log(f'已移动 {len(chunk_fids)} 个视频 -> {folder_name}/{rdir or "根目录"} (CID={dest_cid})')
            for v in chunk:
                ctx.advance(v.rel_path or v.filename)
            time.sleep(1.0)

    return moved_total


def _run_move_large_videos(ctx: TaskContext) -> int:
    """按大小整理（反向）：把待筛选目录里大于等于阈值的视频移动到 <inbox>/<目标文件夹>/ 子文件夹，保持相对路径。

    与 move_small_videos 同一套逻辑，只改方向：>= 阈值进 大于X 文件夹。
    只做 fs_move（改父目录指针），不取直链、不下载；按 1s/批限速、顺序执行。
    已在任意大小文件夹下（小于/大于）的和已精选移动/已删除的跳过，可重复运行。
    阈值与文件夹来自任务单次参数（默认 1024MB / 大于1GB），不改云端配置。
    """
    cfg = get_config()
    inbox_cid = str(cfg.get('inbox_cid') or '').strip()
    if not inbox_cid:
        raise ValueError('请先在云端配置中填写 115 待筛选目录 (INBOX_CID)')
    params = normalize_move_params(ctx.job.get('params'), cfg, direction='large')
    min_size = params['min_size_mb'] * 1048576
    folder_name = params['folder']
    prefix = folder_name + '/'

    with Session() as session:
        q = session.query(Video).filter(
            Video.source_type == 'cloud',
            Video.inbox_cid == inbox_cid,
            Video.file_size > 0,
            Video.file_size >= min_size,
            Video.file_id != '',
            Video.cloud_moved == 0,
            Video.cloud_deleted == 0,
        ).all()
        candidates = [v for v in q
                      if not (v.rel_path or '').replace('\\', '/').strip('/').startswith(prefix)
                      and not is_size_folder_path(v.rel_path)]

    ctx.update(total=len(candidates), processed=0)
    if not candidates:
        ctx.log(f'没有需要移动的大于等于 {params["min_size_mb"]}MB 视频。')
        return 0

    total_bytes = sum(int(v.file_size or 0) for v in candidates)
    ctx.log(f'待移动 {len(candidates)} 个视频（≥{min_size / 1073741824:.2f}GB），合计约 {total_bytes / 1073741824:.2f} GB -> {folder_name}/（保持相对路径）')
    if ctx.dry_run:
        ctx.add(move_candidates=len(candidates))
        return len(candidates)

    large_root_cid = p115_manager.ensure_cloud_rel_dir(inbox_cid, folder_name)
    client = p115_manager.get_client()

    groups: dict[str, list[Video]] = {}
    for v in candidates:
        rdir = _rel_dir_of(v, True)
        groups.setdefault(rdir, []).append(v)

    moved_total = 0
    for rdir, videos in groups.items():
        ctx.check()
        dest_cid = p115_manager.ensure_cloud_rel_dir(large_root_cid, rdir) if rdir else large_root_cid
        for i in range(0, len(videos), 200):
            ctx.check()
            chunk = videos[i:i + 200]
            chunk_fids = [v.file_id for v in chunk]
            resp = client.fs_move(chunk_fids, pid=int(dest_cid))
            if isinstance(resp, dict) and resp.get('state') is False:
                raise RuntimeError(f'云端移动失败：{resp.get("error") or resp}')
            rename_pairs = []
            with Session() as session:
                for v in chunk:
                    orig_rel = (v.rel_path or v.detail or v.filename).replace('\\', '/').strip('/')
                    new_rel = (prefix + orig_rel).strip('/')
                    session.query(Video).filter(Video.id == v.id).update(
                        {Video.parent_cid: str(dest_cid), Video.rel_path: new_rel},
                        synchronize_session=False,
                    )
                    rename_pairs.append((orig_rel, new_rel))
                session.commit()
            # 文件身份缓存键跟随改名（保持缓存与 Video.rel_path 一致）；失败不影响移动。
            cache_rename_many(rename_pairs)
            moved_total += len(chunk_fids)
            ctx.add(large_moved=len(chunk_fids))
            ctx.log(f'已移动 {len(chunk_fids)} 个视频 -> {folder_name}/{rdir or "根目录"} (CID={dest_cid})')
            for v in chunk:
                ctx.advance(v.rel_path or v.filename)
            time.sleep(1.0)

    # 移动成功后把本次实际用的目标文件夹记进任务结果，供前端完成后自动切过去。
    # 只在成功完成时记（失败/取消会抛异常走不到这里；预览提前 return，也不记）；
    # 前端只在 completed 且有该字段时才切，绝不在失败时切。
    ctx.update(dest_folder=folder_name, dest_folder_cid=str(large_root_cid))
    return moved_total


def _run_mark_small_indexed(ctx: TaskContext) -> int:
    """标记小文件已索引（内部标记，替代“下载小文件到本地 + 本地重索引”）。

    工作流：索引 115 文件夹 -> 移出大文件 -> 本轮把剩余的小文件标记为已索引。
    小文件已在平台内（云端 Video 行 + 文件身份缓存），筛选直接走 115/m3u8，
    无需下载到本地、也无需本地重索引。标记只是工作流记录，不改变筛选队列、
    分数或状态；已标记过的重复运行不再重复计数。
    阈值与 move_small_videos 共用同一套参数（默认 <1024MB）。
    """
    cfg = get_config()
    inbox_cid = str(cfg.get('inbox_cid') or '').strip()
    if not inbox_cid:
        raise ValueError('请先在云端配置中填写 115 待筛选目录 (INBOX_CID)')
    params = normalize_move_params(ctx.job.get('params'), cfg)
    max_size = params['max_size_mb'] * 1048576

    with Session() as session:
        q = session.query(Video).filter(
            Video.source_type == 'cloud',
            Video.inbox_cid == inbox_cid,
            Video.file_size > 0,
            Video.file_size < max_size,
            Video.file_id != '',
            Video.cloud_moved == 0,
            Video.cloud_deleted == 0,
        ).all()
        candidates = list(q)

    ctx.update(total=len(candidates), processed=0)
    if not candidates:
        ctx.log(f'没有小于 {params["max_size_mb"]}MB 的视频需要标记。')
        return 0

    if ctx.dry_run:
        ctx.add(mark_candidates=len(candidates))
        return len(candidates)

    # 先补全缓存条目（用 Video 行的身份：相对路径 + 全文件 SHA1），再标记。
    # 这样即使是缓存建成之前同步的老行，也能被标记；不改变 Video 行本身。
    entries = [{
        'rel_path': (v.rel_path or v.detail or v.filename or '').replace('\\', '/').strip('/'),
        'size': int(v.file_size or 0),
        'sha1_full': (v.sha1 or '').strip().upper(),
        'origin': '115',
        'file_id': v.file_id or '',
        'pickcode': v.pickcode or '',
        'inbox_cid': inbox_cid,
    } for v in candidates]
    entries = [e for e in entries if e['rel_path']]
    if entries:
        cache_upsert_many(entries)
    marked = cache_mark_indexed([e['rel_path'] for e in entries])
    ctx.add(marked_indexed=marked)
    ctx.log(f'已标记 {marked} 个小于 {params["max_size_mb"]}MB 的视频为已索引（无需下载到本地）。')
    for v in candidates:
        ctx.advance(v.rel_path or v.filename)
    return marked


def _has_local_videos():
    """库里是否有未结算的本地来源视频（供 prefetch 提交校验用）。"""
    try:
        with Session() as session:
            return session.query(Video.id).filter(
                Video.source_type == 'local',
                Video.file_id != '',
                Video.cloud_moved == 0,
                Video.cloud_deleted == 0,
            ).first() is not None
    except Exception:
        return False


def _run_prefetch_thumbnails(ctx: TaskContext) -> int:
    """后台预取封面：低频逐个截取中间帧存本地，播放时直接读本地文件，
    不用再集中取播放链接。只取还没有本地封面的视频（待筛优先），
    已有封面的跳过，可重复运行接着做剩下的。
    云端：每个视频只调一次 m3u8（get_stream_url）+ 一次 ffmpeg 截帧，不取下载直链；
    间隔默认 10 秒（每 10 分钟 60 次，远低于约 200 次触发 911 的实测阈值），
    可随时取消；疑似账号异常（911/需重新登录）或连续 3 次失败立即停止。
    本地：用 local-play 代理 URL + ffprobe 取时长截帧，不走 115；
    客户端不可达时在第一个本地视频前直接失败，不逐个烧超时。"""
    cfg = get_config()
    inbox_cid = str(cfg.get('inbox_cid') or '').strip()
    params = normalize_prefetch_params(ctx.job.get('params'))
    interval = float(params['interval_seconds'])
    max_items = int(params['max_items'])

    # 待处理：(video_id, filename, source, pickcode, duration)；云端在前、本地在后，各按待筛优先。
    # 云端仍需 inbox_cid；本地不需要（时长缺失时 ffprobe 现读）。
    candidates = []
    if inbox_cid:
        with Session() as session:
            rows = (session.query(Video.id, Video.filename, Video.pickcode, Video.duration)
                    .filter(Video.source_type == 'cloud',
                            Video.inbox_cid == inbox_cid,
                            Video.file_id != '',
                            Video.pickcode != '',
                            Video.duration > 0,
                            Video.cloud_moved == 0,
                            Video.cloud_deleted == 0)
                    .order_by(Video.score.asc(), Video.id.asc())
                    .all())
        candidates += [(r.id, r.filename, 'cloud', r.pickcode, r.duration) for r in rows
                       if not _midframe_cached(r.id)]
    with Session() as session:
        local_rows = (session.query(Video.id, Video.filename, Video.duration)
                      .filter(Video.source_type == 'local',
                              Video.file_id != '',
                              Video.cloud_moved == 0,
                              Video.cloud_deleted == 0)
                      .order_by(Video.score.asc(), Video.id.asc())
                      .all())
    candidates += [(r.id, r.filename, 'local', '', r.duration) for r in local_rows
                   if not _midframe_cached(r.id)]
    if not candidates and not inbox_cid:
        raise ValueError('没有可预取的封面：请先在云端配置中填写 115 待筛选目录，或先同步本地来源')
    to_process = candidates[:max_items]
    n_cloud = sum(1 for c in to_process if c[2] == 'cloud')
    n_local = len(to_process) - n_cloud
    ctx.update(total=len(to_process), processed=0)
    ctx.log(f'待预取 {len(to_process)} 个视频（云端 {n_cloud} 个，本地 {n_local} 个；共 {len(candidates)} 个无本地封面），'
            f'每 {interval:g}s 一个，预计约 {len(to_process) * interval / 60:.1f} 分钟；'
            '云端只用 m3u8 截帧，不取下载直链；本地用电脑直链截帧')
    if ctx.dry_run:
        ctx.add(move_candidates=len(to_process))
        return len(to_process)

    consecutive_failures = 0
    local_checked = False
    for index, (video_id, filename, source, pickcode, duration) in enumerate(to_process):
        ctx.check()
        if _midframe_cached(video_id):
            ctx.add(skipped=1)
            continue
        if source == 'local':
            if not local_checked:
                local_checked = True
                reachable, why = _local_client_reachable()
                if not reachable:
                    raise RuntimeError('预取中断：本地客户端不可达（' + why + '）。请确认电脑上的 local_client.py 正在运行、与 Mac 在同一局域网，再重试。已完成的封面保留。')
            ok, auth_error, message = _extract_local_midframe(video_id, duration)
        else:
            ok, auth_error, message = _extract_midframe(video_id, pickcode, duration)
        if ok:
            ctx.add(prefetched=1)
            consecutive_failures = 0
        else:
            ctx.add(failed=1)
            ctx.log(f'预取失败 {filename or video_id}：{message}')
            if auth_error:
                raise RuntimeError('预取中断：' + message + '。请先在 115 手机 App 验证账号、重新导入 Cookie 后再跑。')
            consecutive_failures += 1
            if consecutive_failures >= 3:
                if source == 'local':
                    raise RuntimeError('连续 3 个本地预取失败，已停止（请检查本地客户端是否在运行、视频文件是否可读）。已完成的封面保留。')
                raise RuntimeError('连续 3 个预取失败，已停止（请检查 115 账号状态与网络）。已完成的封面保留。')
        ctx.advance(filename or str(video_id))
        if index < len(to_process) - 1:
            ctx.cancel.wait(timeout=max(3.0, interval + random.uniform(-1, 1)))
            ctx.check()
    ctx.log(f"预取结束：已生成 {ctx.job['stats'].get('prefetched', 0)} 张，"
            f"失败 {ctx.job['stats'].get('failed', 0)} 个，"
            f"跳过 {ctx.job['stats'].get('skipped', 0)} 个")
    return len(to_process)


def _run_settle_local(ctx: TaskContext) -> int:
    """结算本地来源：调 Windows 客户端 POST /api/settle 移动文件。

    精选（starred / score 5）-> <根目录>/<folder>/（保持相对路径）；
    淘汰（rejected / score 1）-> <根目录>/_dy_回收站/（保持相对路径）。
    只移动文件，永不永久删除；不调 115 接口，不走 Aria2。
    客户端不可达时直接失败（不静默完成）；逐项写库，失败的项可重试，
    已完成的项不再重复处理。
    """
    src = load_local_source()
    if not src or not src.get('base_url') or not src.get('token'):
        raise ValueError('尚未同步本地来源：请先在任务中心「本地来源」卡片粘贴订阅链接并同步。')
    params = normalize_settle_local_params(ctx.job.get('params'))
    folder = params['folder']
    base_url = src['base_url']
    token = src['token']

    with Session() as session:
        starred = session.query(Video).filter(
            Video.source_type == 'local',
            or_(Video.status == STATUS_STARRED, Video.score == 5),
            Video.cloud_moved == 0,
            Video.file_id != '',
        ).all()
        rejected = session.query(Video).filter(
            Video.source_type == 'local',
            or_(Video.status == STATUS_REJECTED, Video.score == 1),
            Video.cloud_deleted == 0,
            Video.file_id != '',
        ).all()
        # 结算时按 file_id 映射回数据库行；v 对象在 session 关闭后即脱离，
        # 只保留 (id, 种类, 文件名) 供逐项更新。
        rows = {}
        for v in starred:
            rows[v.file_id] = (v.id, 'starred', v.filename or v.file_id)
        for v in rejected:
            rows[v.file_id] = (v.id, 'rejected', v.filename or v.file_id)

    ctx.update(total=len(rows), processed=0)
    if not rows:
        ctx.log('没有需要结算的本地视频（无已标精选或已淘汰的本地视频）。')
        return 0
    ctx.log(f'待结算：精选 {len(starred)} 个 -> {folder}/，淘汰 {len(rejected)} 个 -> {LOCAL_RECYCLE_FOLDER}/（仅移动文件，不删除）')
    if ctx.dry_run:
        ctx.add(settle_candidates=len(rows))
        return len(rows)

    moves = [{'id': fid, 'dest': folder} for fid, (_, kind, _) in rows.items() if kind == 'starred']
    deletes = [fid for fid, (_, kind, _) in rows.items() if kind == 'rejected']
    result = call_local_settle(base_url, token, moves, deletes)
    returned = {r.get('id'): r for r in result.get('results') or [] if isinstance(r, dict)}

    failed = []
    moved_n = 0
    recycled_n = 0
    for fid, (vid, kind, filename) in rows.items():
        r = returned.get(fid)
        if not r or not r.get('ok'):
            failed.append((filename, (r or {}).get('error') or '客户端未返回该文件的结果'))
            ctx.add(local_failed=1)
            continue
        new_rel = (r.get('new_rel_path') or '').replace('\\', '/').strip('/')
        if not new_rel:
            failed.append((filename, '客户端未返回移动后的路径'))
            ctx.add(local_failed=1)
            continue
        with Session() as session:
            if kind == 'starred':
                session.query(Video).filter(Video.id == vid).update(
                    {Video.cloud_moved: 1, Video.status: STATUS_DOWNLOADED, Video.cp: 1,
                     Video.rel_path: new_rel, Video.detail: new_rel},
                    synchronize_session=False,
                )
                moved_n += 1
                ctx.add(local_moved=1)
            else:
                session.query(Video).filter(Video.id == vid).update(
                    {Video.cloud_deleted: 1, Video.rel_path: new_rel, Video.detail: new_rel},
                    synchronize_session=False,
                )
                recycled_n += 1
                ctx.add(local_recycled=1)
            session.commit()
        ctx.advance(filename)
    if failed:
        for filename, err in failed[:10]:
            ctx.log(f'结算失败 {filename}：{err}')
        raise RuntimeError(
            f'本地结算部分失败：{len(failed)} 个未完成（已完成的 {moved_n + recycled_n} 个已写入库中记录），'
            '请检查电脑上的客户端是否仍在运行、文件是否被其他程序占用，然后重试。')
    ctx.log(f'本地结算完成：精选 {moved_n} 个 -> {folder}/，淘汰 {recycled_n} 个 -> {LOCAL_RECYCLE_FOLDER}/')
    return moved_n + recycled_n


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
    def _progress(scanned, added, dedup_rejected, skipped_existing, current, dirs_done=None, dirs_queued=None, total=None):
        # 同步中实时回写进度：前端每 2 秒轮询即能看到已扫描数与当前目录，
        # 不再是全程“正在准备或扫描…”。（此前 sync_cloud_videos 未传 task_id，
        # 且其 _update_task 写的是 utils._tasks，与任务中心读的 TaskService.jobs
        # 不是同一份存储，所以进度始终为 0。）
        # 目录进度：视频总数无法提前得知（要全扫一遍才知道，预扫等于双倍调用），
        # 但目录数可以实时给——已扫 dirs_done，待扫 dirs_queued（栈里待处理的
        # 子目录），两者相加就是当前已知的目录总量，随发现子目录而增长。
        # 总数 total：来自 115 目录属性接口 fs_info 的递归文件数（整棵树），
        # 同步开始时调一次即设定，进度条据此显示百分比；失败时为 None，
        # 保持只显示已扫描数。
        values = {'processed': scanned, 'current': current or '正在扫描…'}
        if dirs_done is not None:
            values['dirs_done'] = dirs_done
        if dirs_queued is not None:
            values['dirs_queued'] = dirs_queued
        if total is not None:
            values['total'] = total
        ctx.update(**values)

    result = sync_cloud_videos(on_progress=_progress)
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
