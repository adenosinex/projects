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
)


def _is_video_item(item: dict) -> bool:
    if item.get('iv') == 1:
        return True
    name = (item.get('n') or '').lower()
    return name.endswith(VALID_EXTS_TUPLE)


@serialized_library_operation
def sync_cloud_videos(inbox_cid: str | None = None, task_id: str | None = None, recursive: bool = True) -> dict:
    """
    拉取 115 待筛选目录下的所有视频并按原生 SHA1 自动去重入库。
    返回统计字典: {total_scanned, added_pending, dedup_rejected, skipped_existing, inbox_cid}
    """
    init_db()
    cfg = load_cloud_config()
    raw_inbox = (inbox_cid or cfg.get('inbox_cid') or '').strip()
    if not raw_inbox:
        raise ValueError('尚未配置 115 待筛选目录（INBOX_CID 或目录路径）')

    root_cid = p115_manager.resolve_cid(raw_inbox, create_if_missing=False)

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
    new_records = []

    while stack:
        cur_cid, rel_prefix = stack.pop()
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

    if new_records:
        with Session() as session:
            for i in range(0, len(new_records), 500):
                session.bulk_save_objects(new_records[i:i + 500])
                session.commit()

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
            imported += 1
            if len(batch) >= 200:
                with Session() as session:
                    session.bulk_save_objects(batch)
                    session.commit()
                batch.clear()

    if batch:
        with Session() as session:
            session.bulk_save_objects(batch)
            session.commit()

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
