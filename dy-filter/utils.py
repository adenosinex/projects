# -*- coding: utf-8 -*-
"""
utils.py — 工具与数据层（不依赖 Flask，可独立复用）
=========================================================================
115 云端预筛 + 本地 SQLite 状态库 + Aria2 下载队列版本。

核心职责：
  1. 数据层：SQLAlchemy 模型 / 引擎 / 会话（兼容历史字段并扩展 115 云端字段）。
  2. 云端配置层：管理 115 Cookie / 115-cookies.txt、INBOX_CID、TARGET_CID、
     LOCAL_SAVE_DIR、Aria2 RPC 等配置。
  3. 115 客户端与防风控缓存层（P115Manager）：
     - 自动加载 Cookie / 扫码登录刷新 Cookie
     - 支持 CID 或路径自动解析（如 /待筛选目录、/精选集中营）
     - 播放直链 / m3u8 地址 TTL 缓存（内存 + SQLite 双重缓存，防频繁请求风控）
     - 云端相对目录树按需创建（保持原目录相对路径结构）
  4. 兼容层：保留标签提取、过滤器、黑名单、重命名等原有服务。
=========================================================================
"""

import os
import re
import time
import json
import uuid
import shutil
import hashlib
import sqlite3
import datetime
import subprocess
import threading
from functools import wraps
from pathlib import Path

from sqlalchemy import (
    create_engine, Column, Integer, String, BigInteger, Float, DateTime,
    func, or_, event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

try:
    import jieba
except ImportError:
    jieba = None

try:
    from p115client import P115Client
except ImportError:
    P115Client = None


# ================= 路径与常量配置 =================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.abspath(os.environ.get('VIDEO_DATA_DIR', os.path.join(CURRENT_DIR, 'db')))
DB_FILE = os.path.join(DB_DIR, 'videos.db')
DB_PATH = f"sqlite:///{DB_FILE}"
PATHS_FILE = os.path.join(DB_DIR, 'video_paths.json')
CLOUD_CONFIG_FILE = os.path.join(DB_DIR, 'cloud_config.json')
DEFAULT_COOKIES_FILE = os.path.join(CURRENT_DIR, '115-cookies.txt')
BLACKLIST_FILE = os.path.join(DB_DIR, 'blacklist.json')
INDEX_FILE = os.path.join(CURRENT_DIR, 'index.html')
THUMBNAIL_DIR = os.path.join(DB_DIR, 'thumbnails')

VALID_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.ts', '.wmv', '.m4v'}
VALID_EXTS_TUPLE = tuple(VALID_EXTS)

MODE_FAST = 'fast'
MODE_FULL = 'full'
_VALID_MODES = {MODE_FAST, MODE_FULL}

# 云端状态枚举
STATUS_PENDING = 'pending'
STATUS_STARRED = 'starred'
STATUS_REJECTED = 'rejected'
STATUS_DOWNLOADED = 'downloaded'
STATUS_DISLIKED = 'disliked'
VALID_STATUSES = {STATUS_PENDING, STATUS_STARRED, STATUS_REJECTED, STATUS_DOWNLOADED, STATUS_DISLIKED}

library_operation_lock = threading.RLock()


def serialized_library_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with library_operation_lock:
            return function(*args, **kwargs)
    return wrapped


# ================= 数据模型层 (Models) =================
Base = declarative_base()


class Video(Base):
    __tablename__ = 'videos'
    id = Column(Integer, primary_key=True)
    filename = Column(String)
    detail = Column(String)                 # 云端展示路径或相对路径，如 文件夹A/video.mp4
    tags = Column(String)
    score = Column(Integer, default=0)      # 0=待筛, 1=淘汰(rejected), 5=精选(starred/downloaded)
    file_size = Column(BigInteger, default=0)
    cp = Column(Integer, default=0)         # 1=已下载落盘
    preview_count = Column(BigInteger, default=0, server_default='0', nullable=False)
    last_preview_at = Column(DateTime)
    updatetime = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)

    # --- 115 云端扩展字段 ---
    file_id = Column(String, default='', index=True)
    pickcode = Column(String, default='', index=True)
    sha1 = Column(String, default='', index=True)
    rel_path = Column(String, default='')            # 相对输入根目录的路径，保持原目录结构
    parent_cid = Column(String, default='')
    inbox_cid = Column(String, default='', index=True)  # 同步来源的 115 待筛选目录根 CID（喜欢/待筛计数按它绑定目录）
    duration = Column(Integer, default=0)
    thumb = Column(String, default='')
    status = Column(String, default=STATUS_PENDING, index=True)
    stream_url = Column(String, default='')
    url_expire_at = Column(Float, default=0.0)
    cloud_moved = Column(Integer, default=0)         # 1=已移动到 115 精选目录
    cloud_deleted = Column(Integer, default=0)       # 1=已从 115 删除（进回收站）
    source_type = Column(String, default='cloud')    # 'cloud' | 'local_history'


class DislikedVideo(Base):
    """不喜欢数据集：按 115 原生 SHA1 记录不喜欢的视频（主要去广告）。

    SHA1 是内容哈希，同一视频改名后仍能命中；同步时自动跳过。
    数据只存在本地 SQLite，用户可导出后自行维护、再导入。
    """
    __tablename__ = 'disliked_videos'
    sha1 = Column(String, primary_key=True)              # 40 位大写 hex
    name = Column(String, default='')                    # 原始文件名（若已知）
    source = Column(String, default='manual')            # 'manual' | 'import' | 'sync'
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=False)


class FileCache(Base):
    """通用文件身份缓存（加速模块，非替代）。

    键：相对路径 rel_path（/ 分隔，相对同步根目录；本地文件则相对本地根目录）。
    一个相对路径一份身份：同一份文件可能来自 115、来自本地，或两者都有。

    设计约束（2026-11-17，用户定）：
    1. 加速而非替代：可选的快路径。命中且可用的条目复用已知哈希，跳过重复
       读文件算哈希；未命中、缓存为空、或任何存疑时一律回落传统路径（照常
       算哈希），传统模式的行为与输出完全不变。缓存缺失时不改变任何结果。
    2. 跨来源兼容：不绑定 115。file_id / pickcode / inbox_cid 均可为空；
       origin 记录条目来源（'115' | 'local' | 'both'），本地-only 文件同样
       可建条目、同样可查（为将来纯本地筛选预留）。
    3. 两种哈希分开存，绝不互相替代：115 全文件 SHA1（sha1_full）与本地
       头部 SHA1（前 1MB，sha1_head）是不同值。本地行沿用头部哈希做去重 /
       不喜欢匹配（与传统模式一致），云端行沿用全文件哈希；缓存只负责把
       “已经算过 / 已经拿到”的哈希值按相对路径存起来复用。清单导出时头部
       哈希优先，只有全哈希的条目导出全哈希并标明 sha1_kind='full'，供从
       115 下载到本地的文件复用——其 Video.sha1 本就是全哈希，与 115 库内
       行完全一致，去重 / 不喜欢匹配反而更准（2026-11-17 补：移出大文件后
       下载到本地再索引，正是缓存要加速的流程）。
    4. 内部标记 indexed：1 表示该文件已在平台内完成索引（小文件无需再下载
       到本地、也无需再本地重索引），由任务中心“标记小文件已索引”动作设置；
       它只是工作流记录，不改变筛选队列、分数或状态。
    """
    __tablename__ = 'file_cache'
    rel_path = Column(String, primary_key=True)          # 相对路径（/ 分隔）
    size = Column(BigInteger, default=0)                 # 文件字节数（命中时校验，不一致即未命中）
    sha1_full = Column(String, default='')               # 全文件 SHA1（115 原生，大写 40 位 hex）
    sha1_head = Column(String, default='')               # 头部 SHA1（前 1MB，大写 40 位 hex）
    origin = Column(String, default='')                  # '115' | 'local' | 'both' | ''
    file_id = Column(String, default='')                 # 115 file_id（本地-only 时为空）
    pickcode = Column(String, default='')                # 115 pick_code（本地-only 时为空）
    inbox_cid = Column(String, default='')               # 115 待筛选根 CID（本地-only 时为空）
    indexed = Column(Integer, default=0)                 # 1=已在平台内完成索引（无需下载/重索引）
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)


class VideoPreviewEvent(Base):
    __tablename__ = 'video_preview_events'
    event_id = Column(String(64), primary_key=True)
    video_id = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=False)


class VideoRandomOrder(Base):
    __tablename__ = 'video_random_order'
    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, index=True)
    order_index = Column(Integer, index=True)


class RenameLog(Base):
    __tablename__ = 'rename_logs'
    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, index=True)
    original_path = Column(String)
    target_path = Column(String)
    status = Column(String, default='pending')
    error_msg = Column(String, default='')
    create_time = Column(DateTime, default=datetime.datetime.now)


class BlacklistRule(Base):
    __tablename__ = 'blacklist_rules'
    id = Column(Integer, primary_key=True)
    word = Column(String, unique=True, nullable=False)


class TagPartition(Base):
    __tablename__ = 'tag_partitions'
    id = Column(Integer, primary_key=True)
    partition_id = Column(String, unique=True)
    order_index = Column(Integer, default=0)


class CustomTag(Base):
    __tablename__ = 'custom_tags'
    id = Column(Integer, primary_key=True)
    partition_id = Column(String, index=True)
    name = Column(String, nullable=False)
    order_index = Column(Integer, default=0)


# ================= 标签提取引擎 =================
class TagFilter:
    _AUTO_TAG = re.compile(r'#a_[A-Za-z0-9_\u4e00-\u9fa5]+')
    _MANUAL_TAG = re.compile(r'#[A-Za-z\u4e00-\u9fa5]+(?![_0-9A-Za-z\u4e00-\u9fa5])')

    @classmethod
    def is_untagged_or_auto(cls, filename: str) -> bool:
        if not filename:
            return True
        tmp = cls._AUTO_TAG.sub('', filename)
        return not bool(cls._MANUAL_TAG.search(tmp))


_AUTO_RE       = re.compile(r'#(a_[A-Za-z0-9_\u4e00-\u9fa5]+)')
_STRIP_AUTO_RE = re.compile(r'#a_[A-Za-z0-9_\u4e00-\u9fa5]+')
_MANUAL_RE     = re.compile(r'#([A-Za-z\u4e00-\u9fa5]+)(?![_0-9A-Za-z\u4e00-\u9fa5])')


def extract_tags(filename):
    if not filename or '#' not in filename:
        return []
    auto = [m.group(1) for m in _AUTO_RE.finditer(filename)]
    if not auto:
        return _MANUAL_RE.findall(filename)
    tmp = _STRIP_AUTO_RE.sub('', filename)
    return auto + _MANUAL_RE.findall(tmp)


# ================= 引擎与会话 =================
engine = create_engine(DB_PATH, echo=False)


@event.listens_for(Engine, "connect")
def _sqlite_engine_connect(dbapi_connection, connection_record):
    def _is_untagged(filename):
        return 1 if TagFilter.is_untagged_or_auto(filename) else 0
    try:
        dbapi_connection.create_function("is_untagged", 1, _is_untagged)
    except Exception:
        pass

    cur = dbapi_connection.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-65536")
        cur.execute("PRAGMA mmap_size=268435456")
        cur.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    finally:
        cur.close()


Session = sessionmaker(bind=engine)

_db_ready = False
_db_init_lock = threading.Lock()


def init_db():
    global _db_ready
    if _db_ready:
        return
    with _db_init_lock:
        if _db_ready:
            return
        os.makedirs(DB_DIR, exist_ok=True)
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            columns = {row[1] for row in conn.exec_driver_sql('PRAGMA table_info(videos)')}
            migrations = [
                ('preview_count', 'BIGINT NOT NULL DEFAULT 0'),
                ('last_preview_at', 'DATETIME'),
                ('file_id', "TEXT DEFAULT ''"),
                ('pickcode', "TEXT DEFAULT ''"),
                ('sha1', "TEXT DEFAULT ''"),
                ('rel_path', "TEXT DEFAULT ''"),
                ('parent_cid', "TEXT DEFAULT ''"),
                ('inbox_cid', "TEXT DEFAULT ''"),
                ('duration', 'INTEGER DEFAULT 0'),
                ('thumb', "TEXT DEFAULT ''"),
                ('status', "TEXT DEFAULT 'pending'"),
                ('stream_url', "TEXT DEFAULT ''"),
                ('url_expire_at', 'REAL DEFAULT 0.0'),
                ('cloud_moved', 'INTEGER DEFAULT 0'),
                ('cloud_deleted', 'INTEGER DEFAULT 0'),
                ('source_type', "TEXT DEFAULT 'cloud'"),
            ]
            for col_name, col_def in migrations:
                if col_name not in columns:
                    conn.exec_driver_sql(f'ALTER TABLE videos ADD COLUMN {col_name} {col_def}')

            # 一次性回填：inbox_cid 列是 2026-11-15 新增的，此前库内全部视频都来自
            # 唯一的旧待筛选目录 3548436729486640425（2026-11-13 起的唯一 inbox），
            # 统一补上，之后新同步的视频由 sync_cloud_videos 写入各自的 root_cid。
            conn.exec_driver_sql(
                "UPDATE videos SET inbox_cid = '3548436729486640425' "
                "WHERE (inbox_cid IS NULL OR inbox_cid = '') AND source_type != 'local_history'"
            )

            # 将旧库中没有 status 的记录按 score 同步初始 status
            conn.exec_driver_sql(
                "UPDATE videos SET status = CASE "
                "WHEN score = 5 AND cp = 1 THEN 'downloaded' "
                "WHEN score = 5 THEN 'starred' "
                "WHEN score = 1 THEN 'rejected' "
                "ELSE 'pending' END "
                "WHERE status IS NULL OR status = ''"
            )

            for idx_name, col in [
                ('detail', 'detail'),
                ('score', 'score'),
                ('size', 'file_size'),
                ('preview_count', 'preview_count'),
                ('file_id', 'file_id'),
                ('pickcode', 'pickcode'),
                ('sha1', 'sha1'),
                ('inbox_cid', 'inbox_cid'),
                ('status', 'status'),
            ]:
                conn.exec_driver_sql(f'CREATE INDEX IF NOT EXISTS ix_videos_{idx_name} ON videos ({col})')
        _db_ready = True


def score_to_status(score: int, cp: int = 0) -> str:
    if score == 5:
        return STATUS_DOWNLOADED if cp else STATUS_STARRED
    if score == 1:
        return STATUS_REJECTED
    return STATUS_PENDING


def status_to_score(status: str) -> int:
    if status in (STATUS_STARRED, STATUS_DOWNLOADED):
        return 5
    if status == STATUS_REJECTED:
        return 1
    return 0


_SHA1_RE = re.compile(r'^[0-9a-fA-F]{40}$')


def normalize_sha1(value) -> str:
    """SHA1 归一化为大写 40 位 hex；无效则返回 ''。"""
    s = str(value or '').strip().upper()
    return s if _SHA1_RE.match(s) else ''


def is_disliked(sha1) -> bool:
    """该 SHA1 是否在不喜欢数据集中（纯本地查询，零 115 调用）。"""
    s = normalize_sha1(sha1)
    if not s:
        return False
    init_db()
    with Session() as session:
        return session.query(DislikedVideo).filter(DislikedVideo.sha1 == s).first() is not None


def add_disliked(sha1, name: str = '', source: str = 'manual') -> bool:
    """记入一条不喜欢数据；已存在返回 False，新加入返回 True。无效 SHA1 返回 False。"""
    s = normalize_sha1(sha1)
    if not s:
        return False
    init_db()
    with Session() as session:
        if session.get(DislikedVideo, s) is not None:
            return False
        session.add(DislikedVideo(
            sha1=s,
            name=(name or '').strip()[:240],
            source=source or 'manual',
        ))
        session.commit()
        return True


def list_disliked(limit: int = 200, offset: int = 0):
    """返回 (total, items)；items 按创建时间倒序。"""
    init_db()
    with Session() as session:
        total = session.query(DislikedVideo).count()
        rows = (
            session.query(DislikedVideo)
            .order_by(DislikedVideo.created_at.desc(), DislikedVideo.sha1)
            .limit(max(1, int(limit)))
            .offset(max(0, int(offset)))
            .all()
        )
        items = [{
            'sha1': r.sha1,
            'name': r.name or '',
            'source': r.source or '',
            'created_at': r.created_at.isoformat() if r.created_at else None,
        } for r in rows]
        return total, items


def remove_disliked(sha1) -> bool:
    """删除一条不喜欢数据；不存在返回 False。"""
    s = normalize_sha1(sha1)
    if not s:
        return False
    init_db()
    with Session() as session:
        row = session.get(DislikedVideo, s)
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True


def count_disliked() -> int:
    init_db()
    with Session() as session:
        return session.query(DislikedVideo).count()


# ================= 文件身份缓存（加速模块，非替代） =================
# 约束见 FileCache 模型 docstring：命中复用已知哈希、跳过重复读文件算哈希；
# 未命中 / 存疑时回落传统路径，传统模式行为与输出不变；不绑定 115，本地-only
# 文件同样可建条目；两种哈希分开存，绝不互相替代。

_CACHE_ORIGINS = ('115', 'local', 'both')


def normalize_cache_rel_path(rel) -> str:
    """缓存键规范化：/ 分隔、去首尾空白与重复斜杠；绝对路径、.. 段、空值返回 ''。"""
    if not isinstance(rel, str):
        return ''
    rel = rel.replace('\\', '/').strip()
    if not rel or rel.startswith('/') or '\x00' in rel:
        return ''
    parts = [p for p in rel.split('/') if p not in ('', '.')]
    if not parts or '..' in parts:
        return ''
    return '/'.join(parts)


def _merge_origin(existing: str, new: str) -> str:
    existing = (existing or '').strip()
    new = (new or '').strip()
    if not new:
        return existing
    if not existing:
        return new
    if existing == new:
        return existing
    if existing in _CACHE_ORIGINS and new in _CACHE_ORIGINS:
        return 'both'
    return existing


def cache_lookup(rel_path, size=None):
    """按相对路径查缓存（容错查找，永不抛异常）。

    返回 dict（rel_path / size / sha1_full / sha1_head / origin / file_id /
    pickcode / inbox_cid / indexed）或 None。大小不一致即未命中（返回 None），
    绝不报错；调用方按未命中走传统路径。
    """
    rel = normalize_cache_rel_path(rel_path)
    if not rel:
        return None
    try:
        init_db()
        with Session() as session:
            row = session.get(FileCache, rel)
            if row is None:
                return None
            if size is not None:
                try:
                    if int(size) != int(row.size or 0) and int(row.size or 0) > 0:
                        return None
                except (TypeError, ValueError):
                    return None
            return {
                'rel_path': row.rel_path,
                'size': int(row.size or 0),
                'sha1_full': normalize_sha1(row.sha1_full or ''),
                'sha1_head': normalize_sha1(row.sha1_head or ''),
                'origin': row.origin or '',
                'file_id': row.file_id or '',
                'pickcode': row.pickcode or '',
                'inbox_cid': row.inbox_cid or '',
                'indexed': 1 if row.indexed else 0,
            }
    except Exception:
        return None


def cache_upsert(rel_path, size=None, sha1_full=None, sha1_head=None,
                 origin=None, file_id=None, pickcode=None, inbox_cid=None,
                 indexed=None):
    """新建或更新一条缓存条目（容错，失败返回 False，不抛异常）。

    只用非空值覆盖已有字段：115 同步只写 sha1_full / file_id / pickcode /
    inbox_cid，本地同步只写 sha1_head，互不覆盖对方已知的哈希。origin 按
    _merge_origin 合并（'115' + 'local' -> 'both'）。indexed 仅当明确传入
    0/1 时才改。
    """
    rel = normalize_cache_rel_path(rel_path)
    if not rel:
        return False
    try:
        init_db()
        with Session() as session:
            row = session.get(FileCache, rel)
            if row is None:
                row = FileCache(rel_path=rel)
                session.add(row)
            if size is not None:
                try:
                    row.size = int(size)
                except (TypeError, ValueError):
                    pass
            full = normalize_sha1(sha1_full) if sha1_full is not None else ''
            if full:
                row.sha1_full = full
            head = normalize_sha1(sha1_head) if sha1_head is not None else ''
            if head:
                row.sha1_head = head
            if origin:
                row.origin = _merge_origin(row.origin or '', origin)
            if file_id:
                row.file_id = str(file_id)
            if pickcode:
                row.pickcode = str(pickcode)
            if inbox_cid:
                row.inbox_cid = str(inbox_cid)
            if indexed is not None:
                row.indexed = 1 if indexed else 0
            row.updated_at = datetime.datetime.now()
            session.commit()
            return True
    except Exception:
        return False


def cache_upsert_many(entries):
    """批量写入缓存条目（单会话单提交，供 115 同步等大批量场景用）。

    entries: list of dict，键同 cache_upsert。语义同 cache_upsert（只用非空值
    覆盖，origin 合并）。返回写入的条数。失败不抛异常。
    """
    items = []
    for e in (entries or []):
        if not isinstance(e, dict):
            continue
        rel = normalize_cache_rel_path(e.get('rel_path'))
        if not rel:
            continue
        items.append({
            'rel_path': rel,
            'size': e.get('size'),
            'sha1_full': normalize_sha1(e.get('sha1_full')) if e.get('sha1_full') is not None else '',
            'sha1_head': normalize_sha1(e.get('sha1_head')) if e.get('sha1_head') is not None else '',
            'origin': (e.get('origin') or '').strip(),
            'file_id': str(e.get('file_id') or ''),
            'pickcode': str(e.get('pickcode') or ''),
            'inbox_cid': str(e.get('inbox_cid') or ''),
            'indexed': e.get('indexed'),
        })
    if not items:
        return 0
    try:
        init_db()
        with Session() as session:
            rels = [it['rel_path'] for it in items]
            existing = {r.rel_path: r for r in
                        session.query(FileCache).filter(FileCache.rel_path.in_(rels)).all()}
            now = datetime.datetime.now()
            count = 0
            for it in items:
                row = existing.get(it['rel_path'])
                if row is None:
                    row = FileCache(rel_path=it['rel_path'])
                    session.add(row)
                    existing[it['rel_path']] = row
                if it['size'] is not None:
                    try:
                        row.size = int(it['size'])
                    except (TypeError, ValueError):
                        pass
                if it['sha1_full']:
                    row.sha1_full = it['sha1_full']
                if it['sha1_head']:
                    row.sha1_head = it['sha1_head']
                if it['origin']:
                    row.origin = _merge_origin(row.origin or '', it['origin'])
                if it['file_id']:
                    row.file_id = it['file_id']
                if it['pickcode']:
                    row.pickcode = it['pickcode']
                if it['inbox_cid']:
                    row.inbox_cid = it['inbox_cid']
                if it['indexed'] is not None:
                    row.indexed = 1 if it['indexed'] else 0
                row.updated_at = now
                count += 1
            session.commit()
            return count
    except Exception:
        return 0


def cache_rename_many(pairs) -> int:
    """批量改名缓存键（按大小整理移动文件时调用，保持缓存与 Video.rel_path 一致）。

    pairs: [(old_rel, new_rel), ...]。目标键已存在或旧键不存在时跳过该条；
    返回实际改名的条数。失败不抛异常。
    """
    norm = []
    for old_rel, new_rel in (pairs or []):
        old = normalize_cache_rel_path(old_rel)
        new = normalize_cache_rel_path(new_rel)
        if old and new and old != new:
            norm.append((old, new))
    if not norm:
        return 0
    try:
        init_db()
        with Session() as session:
            olds = [o for o, _ in norm]
            news = [n for _, n in norm]
            existing_old = {r.rel_path: r for r in
                            session.query(FileCache).filter(FileCache.rel_path.in_(olds)).all()}
            existing_new = {r.rel_path for r in
                            session.query(FileCache.rel_path).filter(FileCache.rel_path.in_(news)).all()}
            count = 0
            for old, new in norm:
                row = existing_old.get(old)
                if row is None or new in existing_new:
                    continue
                session.delete(row)
                session.flush()
                session.add(FileCache(
                    rel_path=new,
                    size=row.size,
                    sha1_full=row.sha1_full or '',
                    sha1_head=row.sha1_head or '',
                    origin=row.origin or '',
                    file_id=row.file_id or '',
                    pickcode=row.pickcode or '',
                    inbox_cid=row.inbox_cid or '',
                    indexed=row.indexed or 0,
                ))
                existing_new.add(new)
                count += 1
            session.commit()
            return count
    except Exception:
        return 0


def cache_rename_rel_path(old_rel, new_rel) -> bool:
    """缓存键改名（按大小整理移动文件时调用，保持缓存与 Video.rel_path 一致）。

    目标键已存在时不覆盖（返回 False）；旧键不存在返回 False。失败不抛异常。
    """
    old = normalize_cache_rel_path(old_rel)
    new = normalize_cache_rel_path(new_rel)
    if not old or not new or old == new:
        return False
    try:
        init_db()
        with Session() as session:
            row = session.get(FileCache, old)
            if row is None:
                return False
            if session.get(FileCache, new) is not None:
                return False
            session.delete(row)
            session.flush()
            session.add(FileCache(
                rel_path=new,
                size=row.size,
                sha1_full=row.sha1_full or '',
                sha1_head=row.sha1_head or '',
                origin=row.origin or '',
                file_id=row.file_id or '',
                pickcode=row.pickcode or '',
                inbox_cid=row.inbox_cid or '',
                indexed=row.indexed or 0,
            ))
            session.commit()
            return True
    except Exception:
        return False


def cache_mark_indexed(rel_paths) -> int:
    """把给定相对路径的缓存条目标记为已索引（indexed=1）。

    只标记已存在的条目（不新建）；返回实际标记的条数。失败不抛异常。
    """
    rels = [normalize_cache_rel_path(r) for r in (rel_paths or [])]
    rels = [r for r in rels if r]
    if not rels:
        return 0
    try:
        init_db()
        with Session() as session:
            rows = session.query(FileCache).filter(FileCache.rel_path.in_(rels)).all()
            for row in rows:
                row.indexed = 1
                row.updated_at = datetime.datetime.now()
            session.commit()
            return len(rows)
    except Exception:
        return 0


def cache_stats() -> dict:
    """缓存统计（供任务中心 / API 展示）：总数、已索引数、含全哈希数、含头部哈希数、按来源计数。"""
    try:
        init_db()
        with Session() as session:
            total = session.query(FileCache).count()
            indexed = session.query(FileCache).filter(FileCache.indexed == 1).count()
            with_full = session.query(FileCache).filter(FileCache.sha1_full != '').count()
            with_head = session.query(FileCache).filter(FileCache.sha1_head != '').count()
            by_origin = {}
            for origin, cnt in session.query(FileCache.origin, func.count(FileCache.rel_path)).group_by(FileCache.origin).all():
                by_origin[origin or ''] = int(cnt)
            return {
                'total': int(total), 'indexed': int(indexed),
                'with_full_sha1': int(with_full), 'with_head_sha1': int(with_head),
                'by_origin': by_origin,
            }
    except Exception:
        return {'total': 0, 'indexed': 0, 'with_full_sha1': 0, 'with_head_sha1': 0, 'by_origin': {}}


def cache_export_entries():
    """导出客户端清单用条目：含头部哈希或全文件哈希的条目。

    客户端用它跳过读文件算哈希：清单里的 sha1 即要复用的哈希值，sha1_kind
    标明其形式（'head'=头部哈希，'full'=115 全文件哈希）。头部哈希优先
    （与客户端现算的值同形式）；只有全哈希的条目（典型：115 同步建的条目，
    对应用户从 115 下载到本地的大文件）导出全哈希——下载下来的 115 文件
    其 Video.sha1 本就是全哈希，与 115 库内行完全一致，去重 / 不喜欢匹配
    反而更准。返回 [{rel_path, size, sha1, sha1_kind, origin}]（按 rel_path
    排序）。两种哈希都没有的条目不导出。
    """
    try:
        init_db()
        with Session() as session:
            rows = (session.query(FileCache)
                    .filter(or_(FileCache.sha1_head != '',
                                FileCache.sha1_full != ''))
                    .order_by(FileCache.rel_path).all())
            out = []
            for r in rows:
                head = normalize_sha1(r.sha1_head or '')
                if head:
                    sha1, kind = head, 'head'
                else:
                    sha1, kind = normalize_sha1(r.sha1_full or ''), 'full'
                if not sha1:
                    continue
                out.append({
                    'rel_path': r.rel_path,
                    'size': int(r.size or 0),
                    'sha1': sha1,
                    'sha1_kind': kind,
                    'origin': r.origin or '',
                })
            return out
    except Exception:
        return []


def cache_lookup_many(rel_paths):
    """批量按相对路径查缓存（单会话单查询，永不抛异常）。

    返回 {rel_path: dict}（dict 字段同 cache_lookup；查不到的相对路径不在
    结果里）。供本地同步写回时批量分类哈希形式用，避免逐条查库。不做
    大小校验（调用方自行按需校验）。
    """
    rels = []
    for rp in (rel_paths or []):
        r = normalize_cache_rel_path(rp)
        if r and r not in rels:
            rels.append(r)
    if not rels:
        return {}
    try:
        init_db()
        with Session() as session:
            rows = session.query(FileCache).filter(
                FileCache.rel_path.in_(rels)).all()
            return {r.rel_path: {
                'rel_path': r.rel_path,
                'size': int(r.size or 0),
                'sha1_full': normalize_sha1(r.sha1_full or ''),
                'sha1_head': normalize_sha1(r.sha1_head or ''),
                'origin': r.origin or '',
                'file_id': r.file_id or '',
                'pickcode': r.pickcode or '',
                'inbox_cid': r.inbox_cid or '',
                'indexed': 1 if r.indexed else 0,
            } for r in rows}
    except Exception:
        return {}


def classify_received_sha1(cached_entry, sha1):
    """把客户端发来的 sha1 按缓存现状分类，返回应写入的（列名，值）。

    cached_entry: cache_lookup / cache_lookup_many 返回的 dict 或 None。
    sha1: 客户端发来的哈希（归一化前也可，内部统一大写）。
    规则：等于已存头部哈希→头部哈希；等于已存全哈希→全哈希；都不等（或
    无缓存条目）→客户端现算的头部哈希。永不把全哈希写进 sha1_head。
    无效哈希返回 None。
    """
    s = normalize_sha1(sha1)
    if not s:
        return None
    if cached_entry:
        if s == (cached_entry.get('sha1_head') or ''):
            return ('sha1_head', s)
        if s == (cached_entry.get('sha1_full') or ''):
            return ('sha1_full', s)
    return ('sha1_head', s)


# ================= 云端账户与目录配置 =================
_cloud_config_lock = threading.RLock()


def default_cloud_config() -> dict:
    return {
        'cookie': os.environ.get('P115_COOKIE', '').strip(),
        'cookie_file': os.environ.get('P115_COOKIE_FILE', DEFAULT_COOKIES_FILE),
        'inbox_cid': os.environ.get('P115_INBOX_CID', '').strip(),       # 支持目录 CID 或路径如 /待筛选目录
        'target_cid': os.environ.get('P115_TARGET_CID', '/精选集中营').strip(),
        'local_save_dir': os.environ.get('LOCAL_SAVE_DIR', os.path.join(CURRENT_DIR, 'downloads')),
        'aria2_rpc': os.environ.get('ARIA2_RPC', 'http://127.0.0.1:6800/jsonrpc').strip(),
        'aria2_secret': os.environ.get('ARIA2_SECRET', '').strip(),
        'preserve_relative_path': True,
        'delete_rejected_on_commit': True,
        'prefer_m3u8': True,
        'stream_ttl_seconds': 3600,
        'request_interval_seconds': 1.2,
        'poll_interval_seconds': 0,  # >0 时后台定时轮询同步
    }


def load_cloud_config() -> dict:
    with _cloud_config_lock:
        cfg = default_cloud_config()
        if os.path.exists(CLOUD_CONFIG_FILE):
            try:
                with open(CLOUD_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    cfg.update(saved)
            except (OSError, ValueError):
                pass
        return cfg


# 浏览器 Cookie JSON 快捷导入（2026-11-16）
# 浏览器 Cookie 导出插件（EditThisCookie / Cookie-Editor 等）导出的 JSON
# 是一个对象数组，每项形如：
#   {"domain": ".115.com", "name": "UID", "value": "...", ...}
# 本函数把它转成 dy-filter 内部用的 Cookie 头字符串
# （"UID=...; CID=...; SEID=...; ..."），供 save_cloud_config 保存。
# 去重规则：同名只留一个，优先 .115.com（全站通用）；acw_tc 优先
# webapi.115.com（主接口域名）。必须包含 UID / CID / SEID 核心三件套，
# 否则直接报错，列出缺失项。
REQUIRED_COOKIE_NAMES = ('UID', 'CID', 'SEID')


def parse_browser_cookie_json(text: str) -> dict:
    """Parse browser-exported cookie JSON -> {'cookie': header, 'names': [...]}.

    Accepts a JSON array of cookie objects, or a JSON object wrapping one
    (e.g. {"cookies": [...]}). Raises ValueError with a clear Chinese
    message when the input is not usable.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError('请先粘贴浏览器导出的 Cookie JSON（数组格式）')
    try:
        data = json.loads(text.strip())
    except ValueError:
        raise ValueError('不是有效的 JSON：请粘贴浏览器 Cookie 导出插件复制的完整 JSON 数组')
    if isinstance(data, dict):
        # 有些导出工具包一层，如 {"cookies": [...]}
        for key in ('cookies', 'data', 'items'):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise ValueError('JSON 里没有找到 Cookie 数组：请确认复制的是浏览器导出的完整 JSON')
    if not isinstance(data, list) or not data:
        raise ValueError('JSON 里没有找到 Cookie 数组：请确认复制的是浏览器导出的完整 JSON')
    # 按域名优先级去重：同名只留一个。
    # .115.com 最通用；acw_tc 是分域名防刷 token，优先 webapi.115.com。
    best = {}
    order = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        value = item.get('value')
        if not name or value is None:
            continue
        value = str(value)
        if '=' in name or ';' in name or ' ' in name:
            continue
        domain = str(item.get('domain') or '').strip().lower()
        cur = best.get(name)
        if cur is None:
            best[name] = (domain, value)
            order.append(name)
            continue
        cur_domain, _ = cur
        if name == 'acw_tc':
            if domain == 'webapi.115.com' and cur_domain != 'webapi.115.com':
                best[name] = (domain, value)
        elif domain == '.115.com' and cur_domain != '.115.com':
            best[name] = (domain, value)
    if not best:
        raise ValueError('JSON 里没有解析到任何 Cookie：请确认复制的是 115.com 的 Cookie 导出')
    missing = [n for n in REQUIRED_COOKIE_NAMES if n not in best]
    if missing:
        raise ValueError('缺少关键 Cookie（%s）：请确认已登录 115.com 后再导出，导出时不要只选部分 Cookie' % '、'.join(missing))
    header = '; '.join('%s=%s' % (name, best[name][1]) for name in order)
    return {'cookie': header, 'names': order, 'count': len(order)}


def save_cloud_config(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError('云端配置必须为 JSON 对象')
    with _cloud_config_lock:
        cfg = load_cloud_config()
        for key in ('cookie', 'cookie_file', 'inbox_cid', 'target_cid', 'local_save_dir', 'aria2_rpc', 'aria2_secret'):
            if key in data:
                val = data[key]
                if not isinstance(val, str) or '\x00' in val:
                    raise ValueError(f'{key} 格式无效')
                cfg[key] = val.strip()
                if key == 'cookie' and cfg[key]:
                    # 必须是浏览器 Cookie 头格式：name=value 对，用 ';' 分隔。
                    # 粘贴页面文字（没有任何 name=value）会在 p115client 里报
                    # 'dictionary update sequence element #0 has length 1'，
                    # 在保存时就直接拒绝，给出明确提示。
                    segs = [s.strip() for s in cfg[key].split(';') if s.strip()]
                    ok = any(
                        '=' in s
                        and s.split('=', 1)[0].strip()
                        and ' ' not in s.split('=', 1)[0]
                        for s in segs
                    )
                    if not ok:
                        raise ValueError('115 Cookie 格式不对：请粘贴浏览器里复制的 Cookie（形如 UID=...; CID=...; ...），不要粘贴页面文字')
        for key in ('preserve_relative_path', 'delete_rejected_on_commit', 'prefer_m3u8'):
            if key in data:
                if not isinstance(data[key], bool):
                    raise ValueError(f'{key} 必须为布尔值')
                cfg[key] = data[key]
        if 'stream_ttl_seconds' in data:
            val = int(data['stream_ttl_seconds'])
            if not 60 <= val <= 14400:
                raise ValueError('stream_ttl_seconds 必须在 60–14400 秒之间')
            cfg['stream_ttl_seconds'] = val
        if 'request_interval_seconds' in data:
            val = float(data['request_interval_seconds'])
            if not 1.0 <= val <= 10.0:
                raise ValueError('request_interval_seconds 必须在 1.0–10.0 秒之间')
            cfg['request_interval_seconds'] = val
        if 'poll_interval_seconds' in data:
            val = int(data['poll_interval_seconds'])
            if not 0 <= val <= 86400:
                raise ValueError('poll_interval_seconds 必须在 0–86400 秒之间')
            cfg['poll_interval_seconds'] = val

        # 若用户直接填写了 cookie 字符串，同步写入 cookie_file 方便 p115client 自动持久化刷新
        if cfg.get('cookie') and cfg.get('cookie_file'):
            try:
                cookie_path = Path(cfg['cookie_file'])
                cookie_path.parent.mkdir(parents=True, exist_ok=True)
                cookie_path.write_text(cfg['cookie'], encoding='utf-8')
            except OSError:
                pass

        os.makedirs(DB_DIR, exist_ok=True)
        tmp = CLOUD_CONFIG_FILE + '.' + uuid.uuid4().hex + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CLOUD_CONFIG_FILE)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

        # 重置客户端缓存以便新 Cookie 立即生效
        p115_manager.reset_client()
        return cfg


def mask_cookie(cookie: str) -> str:
    if not cookie:
        return ''
    if len(cookie) <= 16:
        return '***'
    return cookie[:8] + '...' + cookie[-6:]


# ================= 115 客户端与防风控缓存管理器 =================
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _parse_m3u8_best_url(text: str) -> str:
    """从 m3u8 主播放列表文本中取码率最高的 variant URL。

    p115client 的 fs_video_m3u8 返回原始 m3u8 文本（bytes），形如：
        #EXTM3U
        #EXT-X-STREAM-INF:BANDWIDTH=1800000,RESOLUTION=1280x720
        https://...（绝对地址）
    取 BANDWIDTH 最大的那条；若无 variant 行，则取第一个 http 开头的行。
    """
    best_url = ""
    best_bw = -1
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            bw = int(m.group(1)) if m else 0
            if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                if bw >= best_bw:
                    best_bw = bw
                    best_url = lines[i + 1]
    if not best_url:
        for line in lines:
            if not line.startswith("#") and line.startswith("http"):
                best_url = line
                break
    return best_url


class P115Manager:
    """封装 p115client 登录态、目录解析、播放链接 TTL 缓存与相对目录树创建。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._client = None
        self._stream_cache = {}       # pickcode -> (url, stream_type, expire_at)
        self._dir_cache = {}          # (root_cid_str, rel_dir_posix) -> cid_str

    def reset_client(self):
        with self._lock:
            self._client = None
            self._dir_cache.clear()

    def get_client(self):
        with self._lock:
            if self._client is not None:
                return self._client
            if P115Client is None:
                raise RuntimeError('未安装 p115client，请先执行 pip install -r requirements.txt')
            cfg = load_cloud_config()
            cookie_str = (cfg.get('cookie') or '').strip()
            cookie_file = (cfg.get('cookie_file') or DEFAULT_COOKIES_FILE).strip()

            if cookie_str:
                if cookie_file and not os.path.exists(cookie_file):
                    try:
                        Path(cookie_file).parent.mkdir(parents=True, exist_ok=True)
                        Path(cookie_file).write_text(cookie_str, encoding='utf-8')
                    except OSError:
                        pass
                self._client = P115Client(cookie_str)
            elif cookie_file and os.path.exists(cookie_file):
                self._client = P115Client(Path(cookie_file))
            else:
                raise ValueError('尚未配置 115 Cookie 或 115-cookies.txt 文件，请先在「云端配置」中输入 Cookie')
            return self._client

    def check_auth_status(self) -> dict:
        cfg = load_cloud_config()
        has_cookie = bool(cfg.get('cookie')) or (
            bool(cfg.get('cookie_file')) and os.path.exists(cfg['cookie_file'])
        )
        return {
            'configured': has_cookie,
            'p115client_installed': P115Client is not None,
            'inbox_cid': cfg.get('inbox_cid', ''),
            'target_cid': cfg.get('target_cid', ''),
            'local_save_dir': cfg.get('local_save_dir', ''),
        }

    def _aps_list(self, client, cid, show_dir: int, limit: int) -> list[dict]:
        """用 aps 接口（/natsort/files.php）分页拉取一页目录条目。
        aps 不走被风控的 webapi /files，响应也更快。"""
        offset = 0
        items = []
        while True:
            resp = client.fs_files_aps({
                'cid': int(cid) if str(cid).isdigit() else cid,
                'limit': limit,
                'offset': offset,
                'show_dir': show_dir,
            })
            if not isinstance(resp, dict):
                break
            if resp.get('state') is False:
                raise RuntimeError(resp.get('error') or resp.get('message') or '115 接口返回错误')
            batch = resp.get('data') or []
            items.extend(batch)
            if len(batch) < limit:
                break
            offset += len(batch)
            time.sleep(0.25)
        return items

    def list_dir_children(self, cid: str | int, show_dir: int = 1, limit: int = 1000) -> list[dict]:
        """分页拉取指定 115 目录下的全部条目。
        优先用 aps 接口（/natsort/files.php）：webapi /files 自 2026-11-15 起被
        115 风控（HTTP 405），且程序频繁访问会触发封控、连网页都打不开。
        aps 的 show_dir=0 返回文件完整信息（含 fid/sha/s），show_dir=1 返回
        文件+目录的简要信息；目录 = show_dir=1 里不在 show_dir=0 文件集合
        中的条目（按 cid 区分，cid 是每条目唯一 id）。
        每次列目录前按 request_interval_seconds 休眠，避免触发 115 风控。
        aps 失败时回退到 webapi fs_files（若风控解除仍可用）。"""
        cfg = load_cloud_config()
        interval = float(cfg.get('request_interval_seconds') or 1.2)
        # 列目录是同步里最频繁的 115 调用：先休眠，把两次调用隔开，避免触发封控
        time.sleep(interval)
        try:
            files = self._aps_list(self.get_client(), cid, show_dir=0, limit=limit)
            if show_dir == 0:
                return files
            mixed = self._aps_list(self.get_client(), cid, show_dir=1, limit=limit)
            file_cids = {str(f.get('cid')) for f in files}
            # 目录 = 简要列表里不在文件集合中的条目（按 cid 区分）。
            # 兜底：名字带常见文件后缀的（.mp4/.rar/.png 等）一定是文件不是目录，
            # 即使 cid 对不上也不当目录递归，避免把文件误当目录去列目录。
            import re as _re
            _ext = _re.compile(r'\.[A-Za-z0-9]{2,5}$')
            dirs = [it for it in mixed
                    if str(it.get('cid')) not in file_cids
                    and not _ext.search((it.get('n') or '').strip())]
            return files + dirs
        except Exception as aps_err:
            # aps 不可用时回退 webapi（风控解除后仍能工作）；若 webapi 也 405，抛原始错误
            try:
                client = self.get_client()
                offset = 0
                items = []
                while True:
                    resp = client.fs_files({
                        'cid': int(cid) if str(cid).isdigit() else cid,
                        'limit': limit,
                        'offset': offset,
                        'show_dir': show_dir,
                    })
                    if not isinstance(resp, dict):
                        break
                    if resp.get('state') is False:
                        raise RuntimeError(resp.get('error') or resp.get('message') or '115 接口返回错误')
                    batch = resp.get('data') or []
                    items.extend(batch)
                    if len(batch) < limit:
                        break
                    offset += len(batch)
                    time.sleep(0.25)
                return items
            except Exception:
                raise aps_err

    @staticmethod
    def _is_dir_item(item: dict) -> bool:
        # 115 文件接口中，目录通常没有 fid（只有 cid），或存在 fc == '0' / 没有 sha
        return 'fid' not in item or not item.get('fid')

    def resolve_cid(self, cid_or_path: str, create_if_missing: bool = False) -> str:
        """
        将用户输入解析为 115 目录 CID：
        - 若输入纯数字（如 '123456789'），直接作为 CID 返回
        - 若输入路径（如 '/待筛选目录/2026' 或 '精选集中营'），从根目录逐级查找（可选自动创建）
        """
        raw = str(cid_or_path or '').strip()
        if not raw:
            raise ValueError('目录 CID 或路径不能为空')
        if raw.isdigit():
            return raw

        parts = [p for p in raw.replace('\\', '/').split('/') if p]
        if not parts:
            return '0'

        client = self.get_client()
        cur_cid = '0'
        for part in parts:
            cache_key = (cur_cid, part)
            with self._lock:
                if cache_key in self._dir_cache:
                    cur_cid = self._dir_cache[cache_key]
                    continue

            children = self.list_dir_children(cur_cid, show_dir=1, limit=1000)
            matched = None
            for item in children:
                if self._is_dir_item(item) and item.get('n') == part:
                    matched = str(item.get('cid') or '')
                    break

            if not matched:
                if not create_if_missing:
                    raise ValueError(f'115 云端未找到目录：{raw}（缺失子目录 {part}）')
                mk_resp = client.fs_mkdir({'pid': int(cur_cid), 'cname': part})
                if isinstance(mk_resp, dict) and mk_resp.get('state') is False:
                    raise RuntimeError(f'创建 115 目录 {part} 失败：{mk_resp.get("error") or mk_resp}')
                matched = str(
                    (mk_resp.get('cid') if isinstance(mk_resp, dict) else None)
                    or (mk_resp.get('data', {}).get('cid') if isinstance(mk_resp, dict) else None)
                    or ''
                )
                if not matched:
                    # 兜底重新列出父目录查找
                    for item in self.list_dir_children(cur_cid, show_dir=1, limit=1000):
                        if self._is_dir_item(item) and item.get('n') == part:
                            matched = str(item.get('cid') or '')
                            break
                if not matched:
                    raise RuntimeError(f'创建 115 目录 {part} 后无法获取 CID')

            with self._lock:
                self._dir_cache[cache_key] = matched
            cur_cid = matched
        return cur_cid

    def ensure_cloud_rel_dir(self, root_cid: str, rel_dir: str) -> str:
        """
        在 root_cid 下按相对目录路径（如 '子目录A/子目录B'）逐级确保目录存在，
        返回最末级目录的 CID，用于保持原目录相对路径结构。
        """
        rel_dir = (rel_dir or '').replace('\\', '/').strip('/')
        if not rel_dir or rel_dir == '.':
            return str(root_cid)

        cur_cid = str(root_cid)
        for part in [p for p in rel_dir.split('/') if p and p != '.']:
            cache_key = (cur_cid, part)
            with self._lock:
                if cache_key in self._dir_cache:
                    cur_cid = self._dir_cache[cache_key]
                    continue

            children = self.list_dir_children(cur_cid, show_dir=1, limit=1000)
            matched = None
            for item in children:
                if self._is_dir_item(item) and item.get('n') == part:
                    matched = str(item.get('cid') or '')
                    break

            if not matched:
                client = self.get_client()
                mk_resp = client.fs_mkdir({'pid': int(cur_cid), 'cname': part})
                if isinstance(mk_resp, dict) and mk_resp.get('state') is False:
                    raise RuntimeError(f'创建云端相对目录 {part} 失败：{mk_resp.get("error") or mk_resp}')
                matched = str(
                    (mk_resp.get('cid') if isinstance(mk_resp, dict) else None)
                    or (mk_resp.get('data', {}).get('cid') if isinstance(mk_resp, dict) else None)
                    or ''
                )
                if not matched:
                    for item in self.list_dir_children(cur_cid, show_dir=1, limit=1000):
                        if self._is_dir_item(item) and item.get('n') == part:
                            matched = str(item.get('cid') or '')
                            break
                if not matched:
                    raise RuntimeError(f'无法获取新目录 {part} 的 CID')

            with self._lock:
                self._dir_cache[cache_key] = matched
            cur_cid = matched
        return cur_cid

    def get_stream_url(self, pickcode: str, force_refresh: bool = False) -> tuple[str, str, float]:
        """
        懒加载获取 115 播放链接（带内存 + SQLite TTL 缓存）。
        返回: (url, stream_type ['hls'|'direct'], expire_at)
        """
        if not pickcode:
            raise ValueError('缺少 pickcode')
        now = time.time()
        cfg = load_cloud_config()
        ttl = int(cfg.get('stream_ttl_seconds', 3600))

        # 1. 内存缓存命中
        if not force_refresh:
            with self._lock:
                cached = self._stream_cache.get(pickcode)
                if cached and cached[2] > now + 60:
                    return cached

            # 2. SQLite 缓存命中
            init_db()
            with Session() as session:
                row = session.query(Video).filter(Video.pickcode == pickcode).first()
                if row and row.stream_url and (row.url_expire_at or 0) > now + 60:
                    stype = 'hls' if '.m3u8' in row.stream_url.lower() else 'direct'
                    result = (row.stream_url, stype, float(row.url_expire_at))
                    with self._lock:
                        self._stream_cache[pickcode] = result
                    return result

        # 3. 缓存未命中 -> 请求 115 接口
        client = self.get_client()
        url = ''
        stype = 'hls'

        if cfg.get('prefer_m3u8', True):
            try:
                resp = client.fs_video_m3u8(pickcode)
                if isinstance(resp, (bytes, bytearray)):
                    # p115client 0.0.9.7.2 返回原始 m3u8 文本（bytes）
                    url = _parse_m3u8_best_url(resp.decode('utf-8', 'replace'))
                elif isinstance(resp, str):
                    url = _parse_m3u8_best_url(resp)
                elif isinstance(resp, dict) and resp.get('data'):
                    data_list = resp['data']
                    if isinstance(data_list, list) and data_list:
                        url = data_list[0].get('url', '')
                elif isinstance(resp, list) and resp:
                    url = resp[0].get('url', '') if isinstance(resp[0], dict) else str(resp[0])
            except Exception:
                url = ''

        if not url:
            # 兜底：获取原画直链
            direct_info = self.get_download_url(pickcode)
            url = direct_info['url']
            stype = 'direct'

        if not url:
            raise RuntimeError(f'无法获取视频 {pickcode} 的云端播放地址')

        expire_at = now + ttl
        with self._lock:
            self._stream_cache[pickcode] = (url, stype, expire_at)

        init_db()
        with Session() as session:
            session.query(Video).filter(Video.pickcode == pickcode).update(
                {Video.stream_url: url, Video.url_expire_at: expire_at},
                synchronize_session=False,
            )
            session.commit()

        return url, stype, expire_at

    def get_download_url(self, pickcode: str) -> dict:
        """获取原画下载直链与请求头（供 Aria2 使用）。"""
        client = self.get_client()
        info = client.download_url(pickcode, app='web', user_agent=_BROWSER_UA)
        if isinstance(info, dict):
            return {
                'url': info.get('url', ''),
                'headers': info.get('headers', {}) or {},
            }
        # 兼容 P115URL (str 子类，带 .headers 属性)
        return {
            'url': str(info),
            'headers': getattr(info, 'headers', {}) or {},
        }


p115_manager = P115Manager()


# ================= 索引任务进度管理 =================
_index_lock = threading.Lock()
_active_indexes = set()
_tasks = {}
_tasks_lock = threading.Lock()


def _new_task(abs_path, mode=MODE_FAST):
    tid = uuid.uuid4().hex
    with _tasks_lock:
        if len(_tasks) >= 200:
            completed = [key for key, value in _tasks.items() if value.get('done')]
            for key in completed[:max(1, len(_tasks) - 199)]:
                _tasks.pop(key, None)
        _tasks[tid] = {
            'task_id': tid,
            'path': abs_path,
            'mode': mode,
            'total': 0,
            'scanned': 0,
            'added': 0,
            'removed': 0,
            'dedup_rejected': 0,
            'done': False,
            'error': None,
        }
    return tid


def _update_task(tid, **kw):
    with _tasks_lock:
        if tid in _tasks:
            _tasks[tid].update(kw)


def load_paths():
    if os.path.exists(PATHS_FILE):
        with open(PATHS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def save_paths(paths):
    os.makedirs(DB_DIR, exist_ok=True)
    temporary = PATHS_FILE + '.' + uuid.uuid4().hex + '.tmp'
    try:
        with open(temporary, 'w', encoding='utf-8') as f:
            json.dump(paths, f, ensure_ascii=False, indent=2)
        os.replace(temporary, PATHS_FILE)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _open_raw_conn():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _delete_video_ids(session, ids, task_id=None):
    if not ids:
        return 0
    total = len(ids)
    for i in range(0, total, 500):
        chunk = ids[i:i + 500]
        session.query(VideoRandomOrder).filter(VideoRandomOrder.video_id.in_(chunk)).delete(synchronize_session=False)
        session.query(RenameLog).filter(RenameLog.video_id.in_(chunk)).delete(synchronize_session=False)
        session.query(Video).filter(Video.id.in_(chunk)).delete(synchronize_session=False)
        session.commit()
    return total


@serialized_library_operation
def delete_videos_by_path_prefix(session, path):
    abs_p = os.path.abspath(path)
    prefix = abs_p.rstrip(os.sep) + os.sep
    q = session.query(Video.id).filter(
        or_(Video.detail == abs_p, Video.detail.startswith(prefix, autoescape=True))
    )
    ids = [vid for (vid,) in q.all()]
    _delete_video_ids(session, ids)
    return len(ids)


# ================= 查询过滤器 =================
def apply_size_filter(query, size_param):
    if ':' in size_param:
        operator, value = size_param.split(':', 1)
        if operator not in ('lte', 'gte', 'eq'):
            raise ValueError('文件大小比较方式无效')
        size_bytes = float(value) * 1024 * 1024
        if size_bytes < 0:
            raise ValueError('文件大小不能为负数')
        query = query.filter(Video.file_size > 0)
        if operator == 'lte':
            query = query.filter(Video.file_size <= size_bytes)
        elif operator == 'gte':
            query = query.filter(Video.file_size >= size_bytes)
        elif operator == 'eq':
            margin = 0.5 * 1024 * 1024
            query = query.filter(Video.file_size.between(size_bytes - margin, size_bytes + margin))
    else:
        query = query.filter(Video.file_size > 0, Video.file_size <= float(size_param) * 1024 * 1024)
    return query


def apply_video_filters(query, request_args, session=None):
    """根据前端查询参数构造 SQLAlchemy 查询，返回 (query, use_random)。"""
    # 默认仅展示云端可预览的视频（排除纯本地历史 SHA1 占位记录）
    query = query.filter(or_(Video.source_type != 'local_history', Video.source_type.is_(None)))

    # 视频来源：all（默认）/ cloud（115 云端）/ local（本地来源，Windows 客户端）
    source_param = (request_args.get('source') or 'all').strip()
    if source_param == 'cloud':
        query = query.filter(Video.source_type == 'cloud')
    elif source_param == 'local':
        query = query.filter(Video.source_type == 'local')
    elif source_param not in ('all', ''):
        raise ValueError('来源筛选无效')

    status_param = (request_args.get('status') or '').strip()
    score = request_args.get('score')
    search = request_args.get('search')
    exclude = request_args.get('exclude')
    tags = request_args.get('tags')
    size = request_args.get('size')

    untag_raw = str(request_args.get('untagged', '0')).lower()
    if untag_raw in ('1', 'true'):
        untagged = '1'
    elif untag_raw in ('2', 'false'):
        untagged = '2'
    else:
        untagged = '0'

    use_random = request_args.get('sort_by') == 'random'
    if search and re.search(r'\brandom\b', search, re.I):
        use_random = True
        search = re.sub(r'\brandom\b', '', search, flags=re.I).strip()

    if status_param and status_param in VALID_STATUSES:
        query = query.filter(Video.status == status_param)
    elif score == 'unrated':
        query = query.filter(or_(Video.score == 0, Video.score.is_(None)))
    elif score == 'all':
        pass
    elif score and str(score) != '0':
        score_value = int(score)
        if score_value not in range(1, 6):
            raise ValueError('评分筛选必须为 1–5')
        query = query.filter(Video.score == score_value)
    else:
        query = query.filter(or_(Video.score != 1, Video.score.is_(None)))

    if tags:
        for tag in tags.split(','):
            if tag.strip():
                query = query.filter(Video.tags.contains(tag.strip(), autoescape=True))

    if search:
        for kw in search.strip().split():
            query = query.filter(or_(
                Video.detail.contains(kw, autoescape=True),
                Video.filename.contains(kw, autoescape=True),
                Video.rel_path.contains(kw, autoescape=True),
            ))

    if exclude:
        for kw in exclude.strip().split():
            query = query.filter(~Video.detail.contains(kw, autoescape=True))

    directory = request_args.get('directory', '').strip()
    if directory:
        norm = directory.replace('\\', '/').strip('/')
        query = query.filter(or_(
            Video.rel_path.startswith(norm + '/', autoescape=True),
            Video.detail.startswith(directory, autoescape=True),
        ))

    if size and str(size) != '0':
        query = apply_size_filter(query, str(size))

    previewed = request_args.get('previewed', '')
    if previewed == '0':
        query = query.filter(func.coalesce(Video.preview_count, 0) == 0)
    elif previewed == '1':
        query = query.filter(Video.preview_count > 0)
    elif previewed:
        raise ValueError('预览状态筛选无效')

    if untagged == '1':
        query = query.filter(func.is_untagged(Video.filename) == 1)
    elif untagged == '2':
        query = query.filter(func.is_untagged(Video.filename) == 0)

    return query, use_random


# ================= 领域服务层 (兼容保留) =================
class BlacklistService:
    @staticmethod
    def get_all(session):
        records = session.query(BlacklistRule).all()
        return [r.word for r in records if r.word.strip()]

    @staticmethod
    def sync_list(session, words):
        session.query(BlacklistRule).delete()
        for w in set(words):
            if w.strip():
                session.add(BlacklistRule(word=w.strip()))
        session.commit()


class TagService:
    @staticmethod
    def get_all_groups(session):
        partitions = session.query(TagPartition).order_by(TagPartition.order_index).all()
        result = []
        for p in partitions:
            tags = session.query(CustomTag).filter(CustomTag.partition_id == p.partition_id) \
                .order_by(CustomTag.order_index).all()
            result.append({"id": p.partition_id, "tags": [t.name for t in tags]})
        return result

    @staticmethod
    def sync_groups(session, groups_data):
        session.query(TagPartition).delete()
        session.query(CustomTag).delete()
        for p_idx, group in enumerate(groups_data):
            p_id = group.get('id', f'g_{p_idx}')
            session.add(TagPartition(partition_id=p_id, order_index=p_idx))
            for t_idx, t_name in enumerate(group.get('tags', [])):
                session.add(CustomTag(partition_id=p_id, name=t_name, order_index=t_idx))
        session.commit()


class RenameService:
    @staticmethod
    def extract_tags(filename):
        return extract_tags(filename)

    @staticmethod
    def clean_filename_base(base_name, blacklist_words):
        protected_tags = []

        def protect_tag(match):
            protected_tags.append(match.group(0))
            return f" __TAG_{len(protected_tags) - 1}__ "

        temp_base = re.sub(r'#\S+', protect_tag, base_name)
        needs_clean = False
        for bw in blacklist_words:
            if bw.strip() and re.search(re.escape(bw), temp_base, re.IGNORECASE):
                temp_base = re.sub(re.escape(bw), '', temp_base, flags=re.IGNORECASE)
                needs_clean = True

        temp_base = re.sub(r'\[\s*\]|\(\s*\)|【\s*】', '', temp_base)
        temp_base = re.sub(r'\.{2,}', '.', temp_base)
        if re.search(r'#+', temp_base):
            temp_base = re.sub(r'#+', '', temp_base)
            needs_clean = True
        for i, tag in enumerate(protected_tags):
            temp_base = temp_base.replace(f"__TAG_{i}__", tag)
        temp_base = re.sub(r'\s{2,}', ' ', temp_base).strip(' .-_')
        return temp_base, needs_clean

    @staticmethod
    @serialized_library_operation
    def process_renames(session, mode='queue_only'):
        return 0, 0


def is_meaningful_filename(filename):
    if not filename:
        return False
    stem = os.path.splitext(filename)[0]
    return len(stem.strip()) >= 2


def ensure_thumbnail(video_id, video_path):
    return False, '云端视频使用 115 原生封面 thumb'


def build_archive_plan(*args, **kwargs):
    return []


def execute_archive_plan(*args, **kwargs):
    return 0


def scan_directory_for_videos(*args, **kwargs):
    return 0, 0


def build_db_map_for_prefix(*args, **kwargs):
    return {}


def clean_invalid_videos(*args, **kwargs):
    return 0


def run_index_task(task_id, abs_path, mode=MODE_FAST):
    _update_task(task_id, done=True, error='请使用云端同步接口 /dy/api/cloud/sync')


# ================= 云端引导启动（兼容 dy_laucher.py） =================
def launch_main(script='app.py'):
    import sys
    script_path = os.path.join(CURRENT_DIR, script)
    if not os.path.exists(script_path):
        print(f"[!] 致命错误：找不到主服务脚本 {script_path}")
        return 1
    print("[+] 正在启动 115 云端筛选工作台服务...")
    try:
        subprocess.run([sys.executable, script_path], cwd=CURRENT_DIR)
        return 0
    except KeyboardInterrupt:
        return 0


def download_and_launch():
    raise SystemExit(launch_main())


def main_menu():
    import sys
    script = os.path.join(CURRENT_DIR, 'app.py')
    port = int(os.environ.get('VIDEO_TAG_PORT', '81'))
    print(f"115 云端筛选工作台已就绪，请访问: http://127.0.0.1:{port}/dy/")
    try:
        subprocess.run([sys.executable, script], cwd=CURRENT_DIR)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main_menu()
