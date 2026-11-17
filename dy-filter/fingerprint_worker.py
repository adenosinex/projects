# -*- coding: utf-8 -*-
"""
fingerprint_worker.py — 云端 SHA1 预筛与本地历史库 SHA1 导入器
=========================================================================
替代原本的“本地抽帧/文件头哈希”子进程：
  1. 递归扫描 115 云端 /待筛选目录 (INBOX_CID)，提取 file_id、pickcode、sha1、
     体积、时长、封面 thumb，并计算相对于 INBOX_CID 的相对路径 rel_path。
  2. 利用 115 原生 sha1 进行秒级去重：若 sha1 已存在于历史库（含本地历史
     精选库与本次已入库记录），自动将重复项标记为 rejected (score=1)，
     零流量消耗。
  3. 提供本地历史精选目录 SHA1 扫描导入函数，方便平滑迁移历史数据。
  4. 可选后台定时轮询线程（由 cloud_config.json 的 poll_interval_seconds 控制）。
=========================================================================
"""

import os
import time
import hashlib
import datetime
import threading

from utils import (
    VALID_EXTS_TUPLE,
    STATUS_PENDING, STATUS_REJECTED, STATUS_DOWNLOADED,
    init_db, Session, Video, DislikedVideo,
    load_cloud_config, p115_manager, extract_tags,
    _update_task, serialized_library_operation,
    cache_upsert_many,
)


def _is_video_item(item: dict) -> bool:
    if item.get('iv') == 1:
        return True
    name = (item.get('n') or '').lower()
    return name.endswith(VALID_EXTS_TUPLE)


@serialized_library_operation
def sync_cloud_videos(inbox_cid: str | None = None, task_id: str | None = None, recursive: bool = True, on_progress=None) -> dict:
    """
    拉取 115 待筛选目录下的所有视频并按原生 SHA1 自动去重入库。
    返回统计字典: {total_scanned, added_pending, dedup_rejected, skipped_existing, inbox_cid}
    on_progress: 可选回调，每扫完一个目录调用一次
        on_progress(scanned, added, dedup_rejected, skipped_existing, current)
        用于向任务中心实时回写进度（已扫描数 + 当前目录），避免全程无进度。
    """
    init_db()
    cfg = load_cloud_config()
    raw_inbox = (inbox_cid or cfg.get('inbox_cid') or '').strip()
    if not raw_inbox:
        raise ValueError('尚未配置 115 待筛选目录（INBOX_CID 或目录路径）')

    root_cid = p115_manager.resolve_cid(raw_inbox, create_if_missing=False)

    # 提前取总数：115 网页版“目录属性”用的就是 fs_info（proapi
    # /open/folder/get_info），它会递归统计整棵目录树的文件数/目录数/总大小。
    # 首次查询服务端现算约 10 秒，短时间内再查有缓存、不到 1 秒。只调一次，
    # 失败也不阻塞同步（总数未知时回退到只显示已扫描数）。
    # 注意 count 是全部文件数（含少量非视频），视频总数略小于它，总数作
    # 为进度条分母是合理的上界。
    total_files = None
    if on_progress is not None:
        try:
            _info = p115_manager.get_client().fs_info(int(root_cid))
            if _info.get('state') and isinstance(_info.get('count'), int):
                total_files = _info['count']
        except Exception:
            total_files = None
        try:
            on_progress(
                scanned=0, added=0, dedup_rejected=0, skipped_existing=0,
                current='正在统计目录…', dirs_done=0, dirs_queued=0,
                total=total_files,
            )
        except Exception:
            pass

    # 加载库内已存在的 file_id 与 sha1（含历史本地导入和之前已处理记录）
    with Session() as session:
        existing_fids = {
            fid for (fid,) in session.query(Video.file_id).filter(Video.file_id != '').all()
        }
        seen_sha1 = {
            s.upper() for (s,) in session.query(Video.sha1).filter(Video.sha1 != '').all() if s
        }
        # 不喜欢数据集（本地）：同步时直接跳过这些 SHA1，不入库
        disliked_sha1 = {
            s for (s,) in session.query(DislikedVideo.sha1).all() if s
        }

    stack = [(str(root_cid), '')]
    scanned = 0
    added_pending = 0
    dedup_rejected = 0
    skipped_existing = 0
    dislike_skipped = 0
    dirs_done = 0
    new_records = []
    cache_entries = []  # 首次 115 缓存：本轮扫描到的全部视频身份（相对路径键）

    while stack:
        cur_cid, rel_prefix = stack.pop()
        dirs_done += 1
        items = p115_manager.list_dir_children(cur_cid, show_dir=1 if recursive else 0, limit=1000)
        for item in items:
            if p115_manager._is_dir_item(item):
                if recursive:
                    sub_cid = str(item.get('cid') or '')
                    sub_name = (item.get('n') or '').strip()
                    if sub_cid and sub_name:
                        next_prefix = f"{rel_prefix}/{sub_name}" if rel_prefix else sub_name
                        stack.append((sub_cid, next_prefix))
                continue

            if not _is_video_item(item):
                continue

            scanned += 1
            fid = str(item.get('fid') or item.get('id') or '')
            if not fid:
                continue
            # 首次 115 缓存：收集本条目的身份（相对路径键 + 全文件 SHA1），
            # 不论是否已入库 / 是否被不喜欢跳过——缓存记录的是文件夹内容，
            # 与去重、入库逻辑无关，只收集，不改变任何已有行为。
            _c_name = (item.get('n') or f'{fid}.mp4').strip()
            _c_rel = f"{rel_prefix}/{_c_name}" if rel_prefix else _c_name
            try:
                _c_size = int(item.get('s') or 0)
            except (TypeError, ValueError):
                _c_size = 0
            cache_entries.append({
                'rel_path': _c_rel, 'size': _c_size,
                'sha1_full': (item.get('sha') or '').strip().upper(),
                'origin': '115', 'file_id': fid,
                'pickcode': (item.get('pc') or '').strip(),
                'inbox_cid': str(root_cid),
            })
            if fid in existing_fids:
                skipped_existing += 1
                continue

            name = (item.get('n') or f'{fid}.mp4').strip()
            rel_path = f"{rel_prefix}/{name}" if rel_prefix else name
            sha1 = (item.get('sha') or '').strip().upper()
            pickcode = (item.get('pc') or '').strip()
            size = int(item.get('s') or 0)
            duration = int(item.get('play_long') or 0)
            thumb = (item.get('u') or '').strip()
            tags = ','.join(extract_tags(name))

            if sha1 and sha1 in disliked_sha1:
                # 不喜欢数据集：直接跳过，不入库（不计入去重）
                dislike_skipped += 1
                continue

            is_dup = bool(sha1 and sha1 in seen_sha1)
            if sha1:
                seen_sha1.add(sha1)
            existing_fids.add(fid)

            if is_dup:
                status = STATUS_REJECTED
                score = 1
                dedup_rejected += 1
            else:
                status = STATUS_PENDING
                score = 0
                added_pending += 1

            new_records.append(Video(
                filename=name,
                detail=rel_path,
                rel_path=rel_path,
                parent_cid=str(cur_cid),
                inbox_cid=str(root_cid),
                tags=tags,
                score=score,
                file_size=size,
                cp=0,
                file_id=fid,
                pickcode=pickcode,
                sha1=sha1,
                duration=duration,
                thumb=thumb,
                status=status,
                cloud_moved=0,
                cloud_deleted=0,
                source_type='cloud',
                updatetime=datetime.datetime.now(),
            ))

        if task_id:
            _update_task(
                task_id,
                scanned=scanned,
                added=added_pending,
                dedup_rejected=dedup_rejected,
                dislike_skipped=dislike_skipped,
            )

        # 实时进度回写：每扫完一个目录就回调一次，调用方据此更新任务中心
        # （已扫描视频数 + 当前目录），否则同步全程只显示“正在准备或扫描…”。
        # 同时回写目录进度（已扫目录数 / 待扫目录数），让用户知道整棵目录树
        # 还剩多少；视频总数无法提前得知（要全扫一遍才知道），目录进度是
        # 不用多花一次扫描就能给出的最实在的剩余量。
        if on_progress is not None:
            try:
                on_progress(
                    scanned=scanned,
                    added=added_pending,
                    dedup_rejected=dedup_rejected,
                    skipped_existing=skipped_existing,
                    current=rel_prefix or '根目录',
                    dirs_done=dirs_done,
                    dirs_queued=len(stack),
                    total=total_files,
                )
            except Exception:
                pass

    if new_records:
        with Session() as session:
            for i in range(0, len(new_records), 500):
                session.bulk_save_objects(new_records[i:i + 500])
                session.commit()

    # 首次 115 缓存：把本轮扫描到的全部视频身份写入 file_cache（相对路径键，
    # 全文件 SHA1）。只新增 / 补全，不覆盖本地已知的头部哈希，不改变去重与入库；
    # 失败不影响同步结果（cache_upsert_many 内部容错）。
    cache_written = 0
    if cache_entries:
        for i in range(0, len(cache_entries), 500):
            cache_written += cache_upsert_many(cache_entries[i:i + 500])

    result = {
        'inbox_cid': str(root_cid),
        'total_scanned': scanned,
        'added_pending': added_pending,
        'dedup_rejected': dedup_rejected,
        'skipped_existing': skipped_existing,
        'dislike_skipped': dislike_skipped,
    }
    if task_id:
        _update_task(
            task_id,
            total=scanned,
            scanned=scanned,
            added=added_pending,
            dedup_rejected=dedup_rejected,
            dislike_skipped=dislike_skipped,
            done=True,
        )
    return result


def run_cloud_sync_task(task_id: str, inbox_cid: str | None = None):
    try:
        sync_cloud_videos(inbox_cid=inbox_cid, task_id=task_id, recursive=True)
    except Exception as exc:
        _update_task(task_id, done=True, error=str(exc))


def _sha1_file(path: str, chunk_size: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest().upper()


@serialized_library_operation
def import_local_history_sha1(local_dir: str) -> dict:
    """
    扫描本地已下载的历史精选视频目录，计算 SHA1 写入 SQLite (source_type='local_history')，
    使后续 115 云端同步时能直接秒级去重。
    """
    init_db()
    abs_dir = os.path.abspath(local_dir)
    if not os.path.isdir(abs_dir):
        raise ValueError(f'本地历史目录不存在：{abs_dir}')

    with Session() as session:
        seen_sha1 = {
            s.upper() for (s,) in session.query(Video.sha1).filter(Video.sha1 != '').all() if s
        }

    scanned = 0
    imported = 0
    skipped = 0
    batch = []
    cache_batch = []  # 文件身份缓存：本地历史文件的全文件 SHA1（origin='local'）

    for root, _, files in os.walk(abs_dir):
        for fname in files:
            if not fname.lower().endswith(VALID_EXTS_TUPLE):
                continue
            fpath = os.path.join(root, fname)
            scanned += 1
            try:
                sha1 = _sha1_file(fpath)
                size = os.path.getsize(fpath)
            except OSError:
                continue
            if sha1 in seen_sha1:
                skipped += 1
                continue
            seen_sha1.add(sha1)
            rel_path = os.path.relpath(fpath, abs_dir).replace('\\', '/')
            batch.append(Video(
                filename=fname,
                detail=fpath,
                rel_path=rel_path,
                tags='',
                score=5,
                file_size=size,
                cp=1,
                file_id='',
                pickcode='',
                sha1=sha1,
                status=STATUS_DOWNLOADED,
                cloud_moved=1,
                source_type='local_history',
                updatetime=datetime.datetime.now(),
            ))
            cache_batch.append({
                'rel_path': rel_path, 'size': size,
                'sha1_full': sha1, 'origin': 'local',
            })
            imported += 1
            if len(batch) >= 200:
                with Session() as session:
                    session.bulk_save_objects(batch)
                    session.commit()
                batch.clear()
                if cache_batch:
                    cache_upsert_many(cache_batch)
                    cache_batch.clear()

    if batch:
        with Session() as session:
            session.bulk_save_objects(batch)
            session.commit()
    if cache_batch:
        cache_upsert_many(cache_batch)

    return {'scanned': scanned, 'imported': imported, 'skipped': skipped}


class CloudPoller:
    """可选后台定时轮询线程：按 poll_interval_seconds 自动同步 115 待筛选目录。"""

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name='p115-cloud-poller', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _loop(self):
        while not self._stop_event.is_set():
            cfg = load_cloud_config()
            interval = int(cfg.get('poll_interval_seconds') or 0)
            if interval > 0 and cfg.get('inbox_cid'):
                try:
                    sync_cloud_videos()
                except Exception:
                    pass
                self._stop_event.wait(interval)
            else:
                self._stop_event.wait(10)


cloud_poller = CloudPoller()
