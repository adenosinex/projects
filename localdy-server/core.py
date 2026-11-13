# -*- coding: utf-8 -*-
"""
core.py —— 数据与工具层（单体文件）

本文件合并了原来的：
    models.py      -> 数据模型 / Session / 建表 / 迁移
    data_init.py   -> 目录扫描 + 批量入库
    tools.py       -> 文件工具 / 手工维护脚本
    move_files.py  -> 维护任务入口
    temp.py        -> filename_norm 回填迁移

职责边界：
    core.py  只做「数据 + 文件 + 工具」，不 import Flask，可单独使用
    app.py   只做「HTTP 路由」，只从 core 取函数

播放记录（独立数据库 play_records.db，与主库 videos.db 完全隔离）：
    - PlayRecord  一次观看
    - FileHash    filename ↔ hash 的当前快照，索引时维护
    - VideoScore  评分镜像（记录具体分值 1~5）
    - VideoNote   用户备注
    文件名改名/移动后，靠 hash 仍能保持已看状态 / 评分 / 备注。

索引流程（重构版）：
    1. 扫描磁盘 → disk_paths
    2. 拉取主库已有路径 → db_paths
    3. 集合差：
         disk - db → 新增（算 hash，入库，写 file_hashes）
         db - disk → 删除（主库删；play_records/video_scores/video_notes 保留）
         disk ∩ db → 跳过（size 变化的重算 hash 并刷新）
    4. 每阶段打印「开始时间 / 耗时」，最后汇总

直接运行本文件时，会执行一次「扫描目录并入库」的 CLI 入口：
    python core.py "D:\\videos"
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    create_engine,
    event,
    or_,
    text,
)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:          # 没装 tqdm 也能正常跑
    HAS_TQDM = False


# =====================================================================
# 0. 路径与常量
# =====================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.environ.get("LOCALDY_DATA_DIR", BASE_DIR))
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "videos.db")
DB_PATH = "sqlite:///" + DB_FILE.replace("\\", "/")
PATHS_FILE = os.path.join(DATA_DIR, "video_paths.json")

# 播放记录独立数据库
PLAY_DB_FILE = os.path.join(DATA_DIR, "play_records.db")
PLAY_DB_PATH = "sqlite:///" + PLAY_DB_FILE.replace("\\", "/")

HEAD_HASH_SIZE = 1024 * 1024     # 只读文件前 1MB 算 SHA-256

VIDEO_EXTS = (".mp4", ".avi", ".mkv", ".mov", ".flv", ".webm", ".m4v",
              ".wmv", ".ts", ".rmvb", ".mpg", ".mpeg")

# 手工维护脚本用到的网络共享路径（集中在此，方便统一修改）
# 【拆分版注意】以下 NAS 路径只在放视频的 Windows 客户端上有意义。
# 服务端（LOCALDY_SERVER_MODE=1）永远不解析、不访问它们：
# del_1score / copy_5score 在服务端模式下直接拒绝执行；任务队列
# 只把这些路径当不透明字符串传给客户端。
SCORE1_ARCHIVE_DIRS = [r"\\UGREEN-1E55\xin_Y\view", r"\\Synology\home\del"]
SCORE1_DEST_DIR = r"\\Synology\home\sync od\dy-fastnas\del"
SCORE5_DEST_DIR = r"\\UGREEN-1E55\xin_Y\0.保护\精华-短视频"
SCORE5_BIG_FILE_DIRNAME = "大文件信息"
SCORE5_BIG_FILE_LIMIT = 500 * 1024 * 1024      # 超过 500MB 只写信息报告，不复制

# 【拆分版】服务端模式：由 start.sh 设置 LOCALDY_SERVER_MODE=1。
# 服务端模式下：(1) hash 一律从 file_hashes 查，不读文件；
# (2) del_1score / copy_5score 拒绝执行；(3) 跳过耗时的评分回填。
SERVER_MODE = os.environ.get("LOCALDY_SERVER_MODE", "0") == "1"

ENABLE_MAINTENANCE_ON_INDEX = False


# =====================================================================
# 1. 数据模型（主库 videos.db）
# =====================================================================
Base = declarative_base()

engine = create_engine(DB_PATH, echo=False)
Session = sessionmaker(bind=engine)


# 【拆分版】WAL 模式：服务端是唯一写库方，但读接口与索引写入并发时
# 仍可能互相阻塞；WAL 让读不被写锁挡住（设计 §10 风险 4）。
# 双进程部署（HTTP + HTTPS 各一个进程）同时启动时，另一进程可能正占着库：
# 必须先设 busy_timeout 再重试，否则 journal_mode=WAL 会立刻报
# database is locked 导致启动失败（2026-11-07 Mac 部署实测）。
def _enable_wal(dbapi_conn, _conn_record):
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA busy_timeout=30000")
        for _ in range(10):
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                break
            except OperationalError:
                time.sleep(0.5)
        else:
            # 重试耗尽也不致命：WAL 模式存在库文件头里，已由另一进程开启，
            # 本进程沿用现有模式即可。
            pass
        cur.execute("PRAGMA synchronous=NORMAL")
    finally:
        cur.close()


event.listens_for(engine, "connect")(_enable_wal)


def normalize_text(s: str) -> str:
    """规范化文本：去掉所有空白（含全角空格）、统一小写。用于模糊搜索匹配。"""
    if not s:
        return ""
    return "".join(ch for ch in s if not ch.isspace()).lower()


class Video(Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True)
    filename = Column(String)
    filename_norm = Column(String, index=True)   # 规范化文件名，供搜索/排序
    detail = Column(String)                      # 完整文件路径
    tags = Column(String)
    score = Column(Integer)
    file_size = Column(BigInteger, default=0)    # 字节


@event.listens_for(Video, "before_insert")
@event.listens_for(Video, "before_update")
def _fill_filename_norm(mapper, connection, target):
    """ORM 单条写入时自动补 filename_norm。

    注意：bulk_insert_mappings 不会触发 ORM 事件，批量入库处需显式赋值。
    """
    target.filename_norm = normalize_text(target.filename)


# =====================================================================
# 2. 建表与迁移
# =====================================================================
def init_db():
    """建表（已存在则跳过）+ 执行迁移。"""
    Base.metadata.create_all(engine)
    migrate_database()


def migrate_database():
    """幂等迁移：缺字段就补字段，并回填数据。新增字段只需往列表里加一项。"""
    migrations = [
        {
            "field": "file_size",
            "sql": "ALTER TABLE videos ADD COLUMN file_size BIGINT DEFAULT 0",
            "update_function": update_existing_file_sizes,
            "description": "文件大小字段",
        },
        {
            "field": "filename_norm",
            "sql": "ALTER TABLE videos ADD COLUMN filename_norm VARCHAR",
            "index_sql": "CREATE INDEX IF NOT EXISTS ix_videos_filename_norm "
                         "ON videos(filename_norm)",
            "update_function": update_existing_filename_norm,
            "description": "规范化文件名字段",
        },
    ]

    try:
        with engine.connect() as conn:
            result = conn.execute(text("PRAGMA table_info(videos)"))
            existing_columns = [row[1] for row in result.fetchall()]

            for migration in migrations:
                field_name = migration["field"]
                if field_name in existing_columns:
                    print(f"字段 {field_name} 已存在，跳过迁移")
                    continue

                print(f"添加 {migration['description']} ({field_name}) 到数据库...")
                try:
                    conn.execute(text(migration["sql"]))
                    conn.commit()
                    print(f"{field_name} 字段添加成功")

                    if migration.get("index_sql"):
                        conn.execute(text(migration["index_sql"]))
                        conn.commit()

                    if migration.get("update_function"):
                        print(f"开始回填现有记录的 {field_name} 数据...")
                        migration["update_function"]()
                except Exception as e:
                    print(f"添加字段 {field_name} 时出错: {e}")
                    conn.rollback()

            # 老库补索引
            try:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_videos_filename_norm "
                    "ON videos(filename_norm)"
                ))
                conn.commit()
            except Exception:
                pass

    except OperationalError as e:
        print(f"数据库迁移时出错: {e}")


def update_existing_file_sizes():
    """为 file_size 为空/0 的存量记录补文件大小（一次性迁移，兼容用）。"""
    session = Session()
    try:
        targets = session.query(Video).filter(
            (Video.file_size.is_(None)) | (Video.file_size == 0)
        ).all()

        if not targets:
            print("所有记录已有文件大小信息")
            return

        updated = 0
        for video in targets:
            try:
                if video.detail:
                    try:
                        size = os.path.getsize(video.detail)
                    except OSError:
                        size = 0
                    video.file_size = size
                    if size:
                        updated += 1
                else:
                    video.file_size = 0
            except Exception as e:
                print(f"处理视频 {video.filename} 时出错: {e}")
                video.file_size = 0

        session.commit()
        print(f"文件大小更新完成，共更新 {updated} 个视频记录")
    except Exception as e:
        session.rollback()
        print(f"更新文件大小时出错: {e}")
    finally:
        session.close()


def update_existing_filename_norm():
    """为存量记录回填 filename_norm（原 temp.py 的逻辑）。"""
    session = Session()
    try:
        rows = session.query(Video).all()
        for v in rows:
            v.filename_norm = normalize_text(v.filename)
        session.commit()
        print(f"filename_norm 回填完成，共 {len(rows)} 行")
    except Exception as e:
        session.rollback()
        print(f"回填 filename_norm 失败: {e}")
    finally:
        session.close()


# =====================================================================
# 3. 标签与单条增删
# =====================================================================
def extract_tags(filename: str) -> list[str]:
    """从文件名提取标签。第一个空格前作为主标签；#xxx 作为附加标签。"""
    if not filename:
        return []
    tags = [filename.split(" ")[0]]
    tags += re.findall(r"#([\u4e00-\u9fa5\w]+)", filename)
    return tags


def delete_video(video_id: int) -> bool:
    """按 id 删除一条视频记录。
    同时清理 play 库的 file_hashes（主键为 filename）。
    play_records / video_scores / video_notes 保留（按 hash 追踪）。
    """
    session = Session()
    filename = None
    try:
        video = session.query(Video).filter(Video.id == video_id).first()
        if not video:
            return False
        filename = video.filename
        session.delete(video)
        session.commit()
    finally:
        session.close()

    try:
        ps = PlaySession()
        try:
            if filename:
                ps.query(FileHash).filter(
                    FileHash.filename == filename
                ).delete(synchronize_session=False)
                ps.commit()
        finally:
            ps.close()
    except Exception as e:
        print(f"[play-db] 清理 file_hashes 失败: {e}")

    try:
        delete_video_score_by_video_id(video_id)
        delete_note_by_video_id(video_id)
    except Exception as e:
        print(f"[play-db] 清理关联记录失败: {e}")
    return True


def del_videos_path(path: str) -> int:
    """删除 detail 中包含 path 的所有记录，返回删除条数。
    同步清理 file_hashes。
    """
    session = Session()
    deleted = 0
    names = []
    try:
        rows = session.query(Video.filename).filter(
            Video.detail.like(f"%{path}%")
        ).all()
        names = [r[0] for r in rows if r[0]]

        deleted = session.query(Video).filter(
            Video.detail.like(f"%{path}%")
        ).delete(synchronize_session=False)
        session.commit()
        print(f"已删除路径 {path} 下的 {deleted} 条视频记录。")
    except Exception as e:
        session.rollback()
        print(f"删除操作失败: {e}")
        return 0
    finally:
        session.close()

    if names:
        try:
            ps = PlaySession()
            try:
                ps.query(FileHash).filter(
                    FileHash.filename.in_(names)
                ).delete(synchronize_session=False)
                ps.commit()
            finally:
                ps.close()
        except Exception as e:
            print(f"[play-db] 清理 file_hashes 失败: {e}")
    return deleted


# =====================================================================
# 4. 批量入库（兼容接口，单批写入）
# =====================================================================
def add_video_records_batch(video_list, batch_size=None):
    """批量写入，按 detail 去重，已存在则跳过。返回 (新增数, 跳过数)。
    保留兼容；索引主流程已改走 _add_new_files。
    """
    if not video_list:
        print("视频列表为空，无需添加。")
        return 0, 0

    session = Session()
    try:
        details_to_add = [item["detail"] for item in video_list]
        existing = session.query(Video.detail).filter(
            Video.detail.in_(details_to_add)
        ).all()
        existing_set = {row.detail for row in existing}

        new_rows = []
        skipped = 0
        for item in video_list:
            if item["detail"] in existing_set:
                skipped += 1
                continue
            tags = item["tags"]
            new_rows.append({
                "filename": item["filename"],
                "filename_norm": normalize_text(item["filename"]),
                "tags": ",".join(tags) if isinstance(tags, list) else tags,
                "score": item["score"],
                "detail": item["detail"],
                "file_size": item.get("file_size", 0),
            })

        if new_rows:
            session.bulk_insert_mappings(Video, new_rows)
            session.commit()
            print(f"成功批量添加 {len(new_rows)} 个新视频。")
        else:
            print("没有新视频需要添加。")

        return len(new_rows), skipped
    finally:
        session.close()


def add_video_record(filename, tags, score, detail):
    """单条写入，同名文件已存在则跳过。返回 1 表示跳过，0 表示写入。"""
    session = Session()
    try:
        if session.query(Video).filter(Video.filename == filename).count() > 0:
            return 1
        video = Video(
            filename=filename,
            tags=",".join(tags) if isinstance(tags, list) else tags,
            score=score,
            detail=detail,
        )
        session.add(video)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        return 0
    finally:
        session.close()


# =====================================================================
# 5. 路径配置读写（video_paths.json）
# =====================================================================
def load_paths() -> list[str]:
    if os.path.exists(PATHS_FILE):
        try:
            with open(PATHS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            print(f"读取 {PATHS_FILE} 失败: {e}")
    return []


def save_paths(paths: list[str]):
    with open(PATHS_FILE, "w", encoding="utf-8") as f:
        json.dump(paths, f, ensure_ascii=False)


# =====================================================================
# 6. 扫描与格式化
# =====================================================================
def format_size(num_bytes) -> str:
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def format_duration(seconds) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def fast_scan(root_folder: str) -> list[tuple[str, str, int]]:
    """单次递归遍历，返回 [(完整路径, 文件名, 文件大小), ...]。"""
    results = []
    stack = [root_folder]
    dir_errors = 0

    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    name = entry.name
                    if name.startswith("."):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if not name.lower().endswith(VIDEO_EXTS):
                        continue
                    try:
                        size = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        size = 0
                    results.append((entry.path, name, size))
        except OSError as e:
            dir_errors += 1
            print(f"  [skip] 无法访问目录 {d}: {e}")

    if dir_errors:
        print(f"  [warn] 共 {dir_errors} 个目录无法访问")
    return results


# =====================================================================
# 7. 索引核心（集合比对驱动）
# =====================================================================
def _hash_file(path):
    """读取文件前 1MB 计算 SHA-256，返回 (hash, size, mtime)；失败返回 (None, 0, 0)。"""
    try:
        st = os.stat(path)
        with open(path, "rb") as f:
            h = hashlib.sha256(f.read(HEAD_HASH_SIZE)).hexdigest()
        return h, st.st_size, st.st_mtime
    except Exception as e:
        print(f"[hash] 失败 {path}: {e}")
        return None, 0, 0


def _add_new_files(to_add, disk_map, batch_size=500, progress_cb=None):
    """新增文件：写主库 + 写 file_hashes（需要算 hash）。返回新增条数。

    进度条按「文件」粒度刷新；batch_size 只控制提交频率。
    """
    paths = sorted(to_add)
    total = len(paths)
    if not total:
        return 0

    added = 0
    s = Session()
    ps = PlaySession()

    # 待提交缓冲
    video_rows: list[dict] = []
    hash_rows:  list[dict] = []

    def _flush():
        """把当前缓冲落库；失败则回滚并清空缓冲。"""
        nonlocal added
        try:
            if video_rows:
                s.bulk_insert_mappings(Video, video_rows)
                s.commit()
                added += len(video_rows)

            if hash_rows:
                names = [r["filename"] for r in hash_rows]
                ps.query(FileHash).filter(
                    FileHash.filename.in_(names)
                ).delete(synchronize_session=False)
                ps.bulk_insert_mappings(FileHash, hash_rows)
                ps.commit()
        except Exception as e:
            s.rollback()
            ps.rollback()
            print(f"\n[add] 提交失败: {e}")
        finally:
            video_rows.clear()
            hash_rows.clear()

    iterator = tqdm(
        paths,
        total=total,
        desc="  新增入库",
        unit="file",
        ncols=100,
        file=sys.stdout,
        disable=not HAS_TQDM,
    )

    try:
        for done, p in enumerate(iterator, 1):
            name, size = disk_map[p]
            tags = extract_tags(name)

            video_rows.append({
                "filename":      name,
                "filename_norm": normalize_text(name),
                "tags":          ",".join(tags) if isinstance(tags, list) else tags,
                "score":         0,
                "detail":        p,
                "file_size":     size,
            })

            h, sz, mt = _hash_file(p)
            if h:
                hash_rows.append({
                    "filename":   name,
                    "file_hash":  h,
                    "file_size":  sz,
                    "mtime":      mt,
                    "updated_at": datetime.now(),
                })

            # 攒够一批就提交（回滚粒度仍是批，避免单个坏文件毁掉整批）
            if len(video_rows) >= batch_size:
                _flush()

            if HAS_TQDM:
                iterator.set_postfix_str(f"+{added}")
            if progress_cb:
                progress_cb(done)

        # 收尾提交
        _flush()
    finally:
        s.close()
        ps.close()

    return added

def _delete_missing(to_delete, batch_size=500, progress_cb=None):
    """磁盘已无、主库尚存的记录：
        - 主库 videos 表删除
        - play 库 file_hashes 清理（filename 主键已失效）
        - play_records / video_scores / video_notes 保留（按 hash 追踪历史）
    返回删除条数。
    """
    paths = list(to_delete)
    if not paths:
        return 0

    deleted = 0
    s = Session()
    ps = PlaySession()
    try:
        for i in range(0, len(paths), batch_size):
            chunk = paths[i:i + batch_size]
            try:
                # 1) 先取出将删记录的 filename
                rows = s.query(Video.filename).filter(
                    Video.detail.in_(chunk)
                ).all()
                names = [r[0] for r in rows if r[0]]

                # 2) 清理 file_hashes
                if names:
                    ps.query(FileHash).filter(
                        FileHash.filename.in_(names)
                    ).delete(synchronize_session=False)
                    ps.commit()

                # 3) 主库删除
                n = s.query(Video).filter(
                    Video.detail.in_(chunk)
                ).delete(synchronize_session=False)
                s.commit()
                deleted += n
                if progress_cb:
                    progress_cb(min(i + batch_size, len(paths)))
            except Exception as e:
                s.rollback()
                ps.rollback()
                print(f"[del] 第 {i // batch_size + 1} 批失败: {e}")
    finally:
        s.close()
        ps.close()
    return deleted


def _update_changed(to_update, disk_map, batch_size=500, progress_cb=None):
    """同路径文件但大小/内容变化的记录：
        - 重算 hash
        - 更新主库 file_size
        - upsert file_hashes
    返回更新条数。
    """
    paths = list(to_update)
    if not paths:
        return 0

    updated = 0
    s = Session()
    ps = PlaySession()
    try:
        for i in range(0, len(paths), batch_size):
            chunk = paths[i:i + batch_size]
            try:
                for p in chunk:
                    name, size = disk_map[p]
                    h, sz, mt = _hash_file(p)
                    if not h:
                        continue

                    s.query(Video).filter(Video.detail == p).update(
                        {"file_size": size},
                        synchronize_session=False,
                    )

                    # upsert file_hashes
                    ps.query(FileHash).filter(
                        FileHash.filename == name
                    ).delete(synchronize_session=False)
                    ps.add(FileHash(
                        filename=name, file_hash=h,
                        file_size=sz, mtime=mt,
                        updated_at=datetime.now(),
                    ))
                    updated += 1
                s.commit()
                ps.commit()
                if progress_cb:
                    progress_cb(min(i + batch_size, len(paths)))
            except Exception as e:
                s.rollback()
                ps.rollback()
                print(f"[upd] 第 {i // batch_size + 1} 批失败: {e}")
    finally:
        s.close()
        ps.close()
    return updated


def init_data_from_folder(root_folder: str, batch_size: int = 500, progress_cb=None):
    """索引主入口（重构版）。

    流程：
        阶段1 扫描磁盘
        阶段2 拉取主库现有路径
        阶段3 集合比对（新增 / 删除 / 变更 / 跳过）
        阶段4 处理新增（算 hash + 入库）
        阶段5 处理删除（主库删；play 库仅清 file_hashes）
        阶段6 处理变更（重算 hash 并刷新）
        汇总 打印总耗时
    """
    overall_start = time.time()
    overall_start_dt = datetime.now()

    # 规范化根目录，避免前缀误匹配（如 D:\video 匹配到 D:\video2\...）
    root_abs = os.path.abspath(root_folder)
    root_prefix = root_abs if root_abs.endswith(os.sep) else root_abs + os.sep

    print("=" * 72)
    print(f"[{overall_start_dt:%Y-%m-%d %H:%M:%S}] 开始索引：{root_abs}")
    print(f"  批大小 : {batch_size} 条/批")
    print("=" * 72)

    # 建表
    init_db()
    init_play_db()

    # ---------- 阶段1：扫描磁盘 ----------
    print(f"\n[阶段1] {datetime.now():%H:%M:%S} 扫描磁盘...")
    t1 = time.time()
    files = fast_scan(root_abs)
    disk_map = {p: (n, s) for p, n, s in files}
    disk_paths = set(disk_map.keys())
    total_size = sum(s for _, s in disk_map.values())
    t1_cost = time.time() - t1
    print(f"[阶段1] 完成 : {len(disk_paths)} 个视频, "
          f"总大小 {format_size(total_size)}, 耗时 {format_duration(t1_cost)}")

    # ---------- 阶段2：拉取主库现有路径 ----------
    print(f"\n[阶段2] {datetime.now():%H:%M:%S} 拉取主库现有路径...")
    t2 = time.time()
    s = Session()
    try:
        rows = s.query(Video.id, Video.detail, Video.file_size).all()
    finally:
        s.close()

    db_map = {}
    for r in rows:
        if r.detail and r.detail.startswith(root_prefix):
            db_map[r.detail] = (r.id, r.file_size or 0)
    db_paths = set(db_map.keys())
    t2_cost = time.time() - t2
    print(f"[阶段2] 完成 : 主库在本目录下 {len(db_paths)} 条, "
          f"耗时 {format_duration(t2_cost)}")

    # ---------- 阶段3：集合比对 ----------
    print(f"\n[阶段3] {datetime.now():%H:%M:%S} 差异分析...")
    t3 = time.time()
    to_add = disk_paths - db_paths
    to_delete = db_paths - disk_paths
    common = disk_paths & db_paths
    to_update = {p for p in common if db_map[p][1] != disk_map[p][1]}
    total_work = len(disk_paths) + len(to_delete)
    skipped = len(common) - len(to_update)
    def progress(done, message):
        if progress_cb:
            progress_cb(done, total_work, message)
    progress(skipped, f"扫描完成：{len(disk_paths)} 个视频，跳过 {skipped} 个未变化文件")
    t3_cost = time.time() - t3

    print(f"    磁盘文件 : {len(disk_paths)}")
    print(f"    主库记录 : {len(db_paths)}")
    print(f"    新增     : {len(to_add)}")
    print(f"    删除     : {len(to_delete)}")
    print(f"    变更     : {len(to_update)}")
    print(f"    跳过     : {len(common) - len(to_update)}")
    print(f"[阶段3] 耗时 : {format_duration(t3_cost)}")

    if not disk_paths and not db_paths:
        print("\n目录为空且主库无记录，退出。")
        return

    # ---------- 阶段4：新增 ----------
    added = 0
    t4_cost = 0.0
    if to_add:
        print(f"\n[阶段4] {datetime.now():%H:%M:%S} 处理新增 "
              f"{len(to_add)} 个文件（含 hash 计算）...")
        t4 = time.time()
        added = _add_new_files(to_add, disk_map, batch_size,
                               lambda done: progress(skipped + done, f"新增视频 {done}/{len(to_add)}"))
        t4_cost = time.time() - t4
        print(f"[阶段4] 完成 : 新增 {added}, 耗时 {format_duration(t4_cost)}")
    else:
        print(f"\n[阶段4] 无新增文件")

    # ---------- 阶段5：删除 ----------
    deleted = 0
    t5_cost = 0.0
    if to_delete:
        print(f"\n[阶段5] {datetime.now():%H:%M:%S} 处理删除 "
              f"{len(to_delete)} 条（保留播放/评分/备注记录）...")
        t5 = time.time()
        deleted = _delete_missing(to_delete, batch_size,
                                  lambda done: progress(skipped + len(to_add) + done, f"清理失效记录 {done}/{len(to_delete)}"))
        t5_cost = time.time() - t5
        print(f"[阶段5] 完成 : 删除 {deleted}, 耗时 {format_duration(t5_cost)}")
    else:
        print(f"\n[阶段5] 无删除")

    # ---------- 阶段6：变更 ----------
    updated = 0
    t6_cost = 0.0
    if to_update:
        print(f"\n[阶段6] {datetime.now():%H:%M:%S} 处理变更 "
              f"{len(to_update)} 条（重算 hash）...")
        t6 = time.time()
        updated = _update_changed(to_update, disk_map, batch_size,
                                  lambda done: progress(skipped + len(to_add) + len(to_delete) + done, f"更新视频 {done}/{len(to_update)}"))
        t6_cost = time.time() - t6
        print(f"[阶段6] 完成 : 更新 {updated}, 耗时 {format_duration(t6_cost)}")
    else:
        print(f"\n[阶段6] 无变更")

    # ---------- 汇总 ----------
    progress(total_work, f"索引完成：新增 {added}，更新 {updated}，清理 {deleted}")
    total_cost = time.time() - overall_start
    print()
    print("=" * 72)
    print("索引完成")
    print(f"  开始时间 : {overall_start_dt:%Y-%m-%d %H:%M:%S}")
    print(f"  结束时间 : {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  总耗时   : {format_duration(total_cost)}")
    print(f"  分阶段   : 扫描 {format_duration(t1_cost)} | "
          f"读库 {format_duration(t2_cost)} | "
          f"比对 {format_duration(t3_cost)} | "
          f"新增 {format_duration(t4_cost)} | "
          f"删除 {format_duration(t5_cost)} | "
          f"变更 {format_duration(t6_cost)}")
    print(f"  扫描文件 : {len(disk_paths)} 个")
    print(f"  新增     : {added}")
    print(f"  删除     : {deleted}")
    print(f"  更新     : {updated}")
    print(f"  跳过     : {len(common) - len(to_update)}")
    print(f"  总大小   : {format_size(total_size)}")
    if total_cost > 0:
        print(f"  速率     : {len(disk_paths) / total_cost:.1f} file/s")
    print("=" * 72)


# =====================================================================
# 8. 文件工具
# =====================================================================
def create_1kb_file(filename: str):
    """生成一个 1KB 占位文件。"""
    with open(filename, "wb") as f:
        f.write(b"\0" * 1024)


def create_file_info_report(src: str, dst: str):
    """把源文件的信息写成 <dst>.txt（用于超大文件只留档不搬运）。"""
    if not os.path.isfile(src):
        print(f"❌ 错误：文件 '{src}' 不存在或不是文件。")
        return

    st = os.stat(src)
    size = st.st_size
    with open(str(dst) + ".txt", "w", encoding="utf-8") as f:
        f.write("=" * 50 + "\n")
        f.write("📄 文件信息报告\n")
        f.write("=" * 50 + "\n")
        f.write(f"原始文件名: {os.path.basename(src)}\n")
        f.write(f"完整路径: {src}\n")
        f.write(f"文件大小: {size} 字节\n")
        f.write(f"         : {size / 1024:.2f} KB\n")
        f.write(f"         : {size / (1024 * 1024):.2f} MB\n")
        f.write(f"创建时间: {datetime.fromtimestamp(st.st_ctime):%Y-%m-%d %H:%M:%S}\n")
        f.write(f"修改时间: {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M:%S}\n")
        f.write(f"所在目录: {os.path.dirname(src) or '当前目录'}\n")
        f.write("=" * 50 + "\n")
        f.write("✅ 此文件由 Python 自动生成。\n")


# =====================================================================
# 9. 手工维护脚本（默认不自动执行，自行调用）
# =====================================================================
def del_1score(dirs=None):
    """把 1 分视频移动到归档目录，并从数据库删除对应记录。

    【拆分版】服务端模式下拒绝执行：移动文件只能在客户端做，
    服务端改走 /api/file-jobs 的 archive-1star 任务。
    """
    if SERVER_MODE:
        raise RuntimeError(
            "del_1score 已在服务端禁用：文件移动只能由客户端执行，"
            "请通过 /api/file-jobs 提交 archive-1star 任务")
    dirs = dirs or SCORE1_ARCHIVE_DIRS
    cnt = 0
    dst_name = "del " + time.strftime("%Y-%m-%d", time.localtime())

    session = Session()
    try:
        files = session.query(Video).filter(Video.score == 1).all()
        dst_root = Path(SCORE1_DEST_DIR)
        pairs = [[f.detail, dst_root.joinpath(Path(f.detail).name)] for f in files]

        for src, dest in pairs:
            for d in dirs:
                if os.path.splitdrive(src)[0] == os.path.splitdrive(d)[0]:
                    dest = Path(d).joinpath(dst_name, Path(dest).name)
                    break

            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)

            src_path = Path(src)
            if not src_path.exists():
                print(f"源文件不存在: {src_path.name}")
                continue

            cnt += 1
            try:
                os.renames(src_path, dest)
                print(f"文件已移动到 {dest}")
            except Exception:
                if os.path.getsize(src) > 200 * 1024 * 1024:
                    os.remove(src)
                    print(f"文件 {src} 大于 200MB，跳过复制，直接删除。")
                else:
                    shutil.copy2(src, dest)
                    os.remove(src)
                    print(f"文件已复制到 {dest}，源文件 {src_path.name} 已删除")

        session.query(Video).filter(Video.score == 1).delete(synchronize_session=False)
        session.commit()
        print("已删除分数为 1 的视频记录。")
    except Exception as e:
        session.rollback()
        print(f"删除操作失败: {e}")
    finally:
        session.close()

    print(f"共处理分数为 1 的视频文件：{cnt}")


def copy_5score():
    """把 5 分视频复制到精华目录；超过 500MB 的只写信息报告到「大文件信息」。

    【拆分版】服务端模式下拒绝执行：复制文件只能在客户端做，
    服务端改走 /api/file-jobs 的 copy-5star 任务。
    """
    if SERVER_MODE:
        raise RuntimeError(
            "copy_5score 已在服务端禁用：文件复制只能由客户端执行，"
            "请通过 /api/file-jobs 提交 copy-5star 任务")
    cnt = 0
    session = Session()
    try:
        files = session.query(Video).filter(Video.score == 5).all()
        dst = Path(SCORE5_DEST_DIR)
        dst_big = dst.joinpath(SCORE5_BIG_FILE_DIRNAME)

        for f in files:
            src = f.detail
            dest = dst.joinpath(Path(src).name)

            if not os.path.exists(src) or os.path.exists(dest):
                continue

            cnt += 1
            if os.path.getsize(src) > SCORE5_BIG_FILE_LIMIT:
                print(f"文件 {src} 超过 500MB，只记录信息、跳过复制。")
                if not dst_big.exists():
                    dst_big.mkdir(parents=True, exist_ok=True)
                create_file_info_report(src, dst_big.joinpath(Path(src).name))
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

        print("已复制分数为 5 的视频记录。")
    except Exception as e:
        print(f"复制操作失败: {e}")
    finally:
        session.close()

    print(f"共处理分数为 5 的视频文件：{cnt}")


def run_save_star():
    """「索引」后触发的维护任务入口。默认空操作。"""
    if not ENABLE_MAINTENANCE_ON_INDEX:
        return
    del_1score(SCORE1_ARCHIVE_DIRS)
    copy_5score()


# =====================================================================
# 10. 播放记录（独立数据库 play_records.db）
# =====================================================================
play_engine = create_engine(
    PLAY_DB_PATH,
    connect_args={"check_same_thread": False},
    future=True,
)
PlaySession = sessionmaker(bind=play_engine, future=True)
PlayBase = declarative_base()

# 【拆分版】播放库同样开 WAL（与主库同理）。
event.listens_for(play_engine, "connect")(_enable_wal)


# ---------------------------------------------------------------------
# 【拆分版】服务端 hash 解析：文件在客户端，服务端永远不读文件。
# ---------------------------------------------------------------------
def _hash_from_db(filename):
    """从 file_hashes 查已存的 (hash, size)。查不到返回 (None, 0)。"""
    ps = PlaySession()
    try:
        row = ps.query(FileHash).filter(FileHash.filename == filename).first()
        if row:
            return row.file_hash, row.file_size or 0
        return None, 0
    finally:
        ps.close()


def _resolve_file_hash(filename, file_path):
    """统一入口：服务端模式查库；单机模式读文件（原版行为）。

    返回 (hash, size)；拿不到返回 (None, 0)，调用方按“无 hash”处理。
    """
    if SERVER_MODE:
        return _hash_from_db(filename)
    if not file_path or not os.path.isfile(file_path):
        return None, 0
    h = _head_hash(file_path)
    if not h:
        return None, 0
    try:
        return h, os.path.getsize(file_path)
    except OSError:
        return h, 0


class PlayRecord(PlayBase):
    """一次观看记录。file_hash 是主关联键；filename / video_id 仅作快照。"""
    __tablename__ = "play_records"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    file_hash   = Column(String(64), index=True)
    filename    = Column(String)
    video_id    = Column(Integer)
    file_size   = Column(BigInteger)
    accessed_at = Column(DateTime, default=datetime.now, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "file_hash": self.file_hash,
            "filename": self.filename,
            "video_id": self.video_id,
            "file_size": self.file_size,
            "accessed_at": self.accessed_at.strftime("%Y-%m-%d %H:%M:%S")
                          if self.accessed_at else None,
        }


class FileHash(PlayBase):
    """filename ↔ hash 的当前快照。索引时维护，供查询时翻译。"""
    __tablename__ = "file_hashes"

    filename   = Column(String, primary_key=True)
    file_hash  = Column(String(64), index=True)
    file_size  = Column(BigInteger)
    mtime      = Column(Float)
    updated_at = Column(DateTime, default=datetime.now)


class VideoScore(PlayBase):
    """每个 hash 一条评分记录，记录具体得分（1~5）。
    名字改了靠 hash 追踪。"""
    __tablename__ = "video_scores"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    file_hash  = Column(String(64), unique=True, index=True)
    filename   = Column(String)
    video_id   = Column(Integer)
    file_size  = Column(BigInteger)
    score      = Column(Integer, default=0, index=True)   # 具体得分
    marked_at  = Column(DateTime, default=datetime.now, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "file_hash": self.file_hash,
            "filename": self.filename,
            "video_id": self.video_id,
            "file_size": self.file_size,
            "score": self.score,
            "marked_at": self.marked_at.strftime("%Y-%m-%d %H:%M:%S")
                         if self.marked_at else None,
        }


class VideoNote(PlayBase):
    """用户对某个视频的备注。file_hash 关联，改名/移动也保留。"""
    __tablename__ = "video_notes"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    file_hash  = Column(String(64), unique=True, index=True)
    filename   = Column(String)
    video_id   = Column(Integer)
    note       = Column(String)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "file_hash": self.file_hash,
            "filename": self.filename,
            "video_id": self.video_id,
            "note": self.note,
            "updated_at": self.updated_at.strftime("%Y-%m-%d %H:%M:%S")
                          if self.updated_at else None,
        }


def init_play_db():
    """创建播放记录库与表（幂等）。并把主库现有评分的视频镜像过来。"""
    PlayBase.metadata.create_all(play_engine)
    print(f"[play-db] 数据库文件: {PLAY_DB_FILE}")
    try:
        _migrate_five_star_table()
    except Exception as e:
        print(f"[play-db] five_star 迁移失败: {e}")
    if SERVER_MODE:
        # 【拆分版】回填要逐文件读前 1MB 算 hash，服务端没有文件；
        # 评分镜像由客户端索引时通过 file_hashes 间接维护，此处跳过。
        print("[play-db] 服务端模式：跳过评分记录回填（文件在客户端）")
        return
    try:
        _backfill_video_scores()
    except Exception as e:
        print(f"[play-db] 评分记录回填失败: {e}")


def _migrate_five_star_table():
    """旧表 five_star → video_scores 一次性迁移（幂等）。

    仅当旧表存在且新表为空时执行；迁移后旧表数据全部标记为 score=5。
    """
    with play_engine.connect() as conn:
        # 旧表存在？
        row = conn.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='five_star'"
        )).fetchone()
        if not row:
            return

        # 新表有数据就不覆盖
        row = conn.execute(text("SELECT COUNT(*) FROM video_scores")).fetchone()
        if row and row[0] > 0:
            return

        conn.execute(text(
            "INSERT INTO video_scores "
            "(file_hash, filename, video_id, file_size, score, marked_at) "
            "SELECT file_hash, filename, video_id, file_size, 5, marked_at "
            "FROM five_star"
        ))
        conn.commit()
        print("[play-db] 已迁移 five_star → video_scores")


def _head_hash(file_path: str):
    """文件前 1MB 的 SHA-256。返回 None 表示读取失败。"""
    try:
        with open(file_path, "rb") as f:
            return hashlib.sha256(f.read(HEAD_HASH_SIZE)).hexdigest()
    except Exception as e:
        print(f"[play-db] hash 失败 {file_path}: {e}")
        return None


def _backfill_video_scores():
    """把主库中所有有评分的 Video 镜像到 video_scores（只处理缺失的）。"""
    s = Session()
    try:
        videos = s.query(
            Video.id, Video.filename, Video.detail, Video.score
        ).filter(
            Video.score.isnot(None),
            Video.score != 0,
        ).all()
    finally:
        s.close()

    if not videos:
        return

    ps = PlaySession()
    try:
        existing_vids = {r[0] for r in ps.query(VideoScore.video_id).all()}
    finally:
        ps.close()

    to_process = [(vid, fn, fp, sc) for vid, fn, fp, sc in videos
                  if vid not in existing_vids]
    if not to_process:
        return

    added = 0
    ps = PlaySession()
    try:
        for vid, fn, fp, sc in to_process:
            if not fp:
                continue
            h, sz, _ = _hash_file(fp)
            if not h:
                continue
            try:
                ps.add(VideoScore(
                    file_hash=h, filename=fn, video_id=vid,
                    file_size=sz, score=sc, marked_at=datetime.now(),
                ))
                ps.commit()
                added += 1
            except IntegrityError:
                # 已有同 hash 记录
                ps.rollback()
            except Exception:
                ps.rollback()
    finally:
        ps.close()

    if added:
        print(f"[play-db] 回填 {added} 条评分记录")


def sync_file_hashes():
    """
    【手工/兼容】全量对齐 file_hashes 与主库。

    索引主流程已不再调用该函数（性能考虑）。
    保留给外部脚本或故障修复使用。
    """
    s = Session()
    try:
        videos = s.query(Video.filename, Video.detail).all()
    finally:
        s.close()

    ps = PlaySession()
    try:
        existing = {r.filename: r for r in ps.query(FileHash).all()}
        seen = set()

        for filename, path in videos:
            if not path:
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            seen.add(filename)

            row = existing.get(filename)
            if (row and row.file_size == st.st_size
                    and abs((row.mtime or 0) - st.st_mtime) < 1):
                continue

            h = _head_hash(path)
            if not h:
                continue

            if row:
                row.file_hash  = h
                row.file_size  = st.st_size
                row.mtime      = st.st_mtime
                row.updated_at = datetime.now()
            else:
                ps.add(FileHash(
                    filename=filename, file_hash=h,
                    file_size=st.st_size, mtime=st.st_mtime,
                    updated_at=datetime.now(),
                ))

        for row in ps.query(FileHash).all():
            if row.filename not in seen:
                ps.delete(row)

        # 刷新 video_scores / video_notes 的 filename 快照
        hash_to_name = {r.file_hash: r.filename
                        for r in ps.query(FileHash).all() if r.file_hash}
        for vs in ps.query(VideoScore).all():
            current = hash_to_name.get(vs.file_hash)
            if current and current != vs.filename:
                vs.filename = current
        for nt in ps.query(VideoNote).all():
            current = hash_to_name.get(nt.file_hash)
            if current and current != nt.filename:
                nt.filename = current

        ps.commit()
        print(f"[play-db] 已同步 {len(seen)} 个文件的 hash")
    except Exception as e:
        ps.rollback()
        print(f"[play-db] 同步失败: {e}")
    finally:
        ps.close()


def record_play(video_id, filename, file_path):
    """写一条观看记录。失败返回 None（不影响播放）。

    【拆分版】服务端模式下 hash/size 从 file_hashes 查，不读文件。
    """
    h, size = _resolve_file_hash(filename, file_path)
    if not h:
        return None

    s = PlaySession()
    try:
        rec = PlayRecord(
            file_hash=h,
            filename=filename,
            video_id=video_id,
            file_size=size,
            accessed_at=datetime.now(),
        )
        s.add(rec)
        snapshot = s.query(FileHash).filter(FileHash.filename == filename).first()
        if snapshot:
            snapshot.file_hash = h
            snapshot.file_size = size
            snapshot.updated_at = datetime.now()
        else:
            mtime = 0
            if not SERVER_MODE:
                try:
                    mtime = os.path.getmtime(file_path)
                except OSError:
                    mtime = 0
            s.add(FileHash(
                filename=filename, file_hash=h, file_size=size,
                mtime=mtime, updated_at=datetime.now(),
            ))
        s.commit()
        return rec.id
    except Exception as e:
        print(f"[play-db] 写记录失败: {e}")
        return None
    finally:
        s.close()


def apply_watched_filter(query, watched):
    """
    观看状态过滤：yes / no / stale（最近 30 天未看，含从未看）。
    """
    if watched not in ("yes", "no", "stale"):
        return query

    ps = PlaySession()
    try:
        if watched == "stale":
            cutoff = datetime.now() - timedelta(days=30)
            rows = (ps.query(PlayRecord.file_hash)
                      .filter(PlayRecord.accessed_at >= cutoff)
                      .distinct().all())
        else:
            rows = ps.query(PlayRecord.file_hash).distinct().all()
        hashes = [r[0] for r in rows if r[0]]

        if not hashes:
            return query.filter(False) if watched == "yes" else query

        names = set()
        for i in range(0, len(hashes), 500):
            chunk = hashes[i:i + 500]
            for r in ps.query(FileHash.filename).filter(
                FileHash.file_hash.in_(chunk)
            ).all():
                if r[0]:
                    names.add(r[0])
    finally:
        ps.close()

    if not names:
        return query.filter(False) if watched == "yes" else query

    names = list(names)
    if len(names) <= 500:
        cond = Video.filename.in_(names)
    else:
        cond = or_(*[Video.filename.in_(names[i:i + 500])
                     for i in range(0, len(names), 500)])

    return query.filter(cond if watched == "yes" else ~cond)


# ---------- 评分镜像（记录具体分值 1~5） ----------
def sync_video_score(video_id, filename, file_path, new_score):
    """评分变更时同步 video_scores：
        new_score 非 0 → upsert（带上具体分值）
        new_score 为 0/None → 删除
    """
    ps = PlaySession()
    try:
        # 撤回也应适用于已经离线、移动或删除的文件。
        if not new_score:
            h, _ = _resolve_file_hash(filename, file_path)
            condition = VideoScore.video_id == video_id
            if h:
                condition = or_(condition, VideoScore.file_hash == h)
            ps.query(VideoScore).filter(condition).delete(synchronize_session=False)
            ps.commit()
            return
        h, size = _resolve_file_hash(filename, file_path)
        if not h:
            return
        if new_score:
            row = ps.query(VideoScore).filter(VideoScore.file_hash == h).first()
            if row:
                row.filename  = filename
                row.video_id  = video_id
                row.file_size = size
                row.score     = new_score
                row.marked_at = datetime.now()
            else:
                ps.add(VideoScore(
                    file_hash=h, filename=filename, video_id=video_id,
                    file_size=size, score=new_score, marked_at=datetime.now(),
                ))
            ps.commit()
        else:
            n = ps.query(VideoScore).filter(VideoScore.file_hash == h).delete()
            ps.commit()
            if n:
                print(f"[play-db] 移除评分记录 hash={h[:8]}…")
    except Exception as e:
        ps.rollback()
        print(f"[play-db] 同步评分记录失败: {e}")
    finally:
        ps.close()


def list_video_scores(page=1, page_size=100, keyword=None, score=None):
    """分页查询评分记录。

    score=None → 返回所有有评分的记录
    score=1/2/3/4/5 → 只返回指定分值
    """
    ps = PlaySession()
    try:
        q = ps.query(VideoScore)
        if keyword:
            q = q.filter(VideoScore.filename.like(f"%{keyword}%"))
        if score is not None:
            q = q.filter(VideoScore.score == score)
        total = q.count()
        rows = (q.order_by(VideoScore.marked_at.desc())
                 .offset((page - 1) * page_size)
                 .limit(page_size).all())
        return total, [r.to_dict() for r in rows]
    finally:
        ps.close()


def count_video_scores_by_score() -> dict:
    """返回 {score: count} 的统计，方便前端展示各分档数量。"""
    ps = PlaySession()
    try:
        rows = (ps.query(VideoScore.score, func.count(VideoScore.id))
                  .group_by(VideoScore.score).all())
        return {int(s): int(c) for s, c in rows if s is not None}
    finally:
        ps.close()


def delete_video_score_by_video_id(video_id):
    """视频被删除时清理其评分镜像。"""
    ps = PlaySession()
    try:
        n = ps.query(VideoScore).filter(VideoScore.video_id == video_id).delete()
        ps.commit()
        return n
    except Exception:
        ps.rollback()
        return 0
    finally:
        ps.close()


# ---------- 备注 ----------
def get_note(video_id, filename, file_path):
    """读取某个视频的备注。没有则返回 None。

    【拆分版】服务端模式下 hash 从 file_hashes 查，不读文件。
    """
    h, _ = _resolve_file_hash(filename, file_path)
    if not h:
        return None

    ps = PlaySession()
    try:
        row = ps.query(VideoNote).filter(VideoNote.file_hash == h).first()
        return row.to_dict() if row else None
    finally:
        ps.close()


def save_note(video_id, filename, file_path, note):
    """写入/更新备注。空字符串视为清空。返回 dict 或 None。

    【拆分版】服务端模式下 hash 从 file_hashes 查，不读文件。
    """
    h, _ = _resolve_file_hash(filename, file_path)
    if not h:
        return None

    note = (note or "").strip()
    ps = PlaySession()
    try:
        row = ps.query(VideoNote).filter(VideoNote.file_hash == h).first()

        if not note:
            if row:
                ps.delete(row)
                ps.commit()
            return None

        if row:
            row.note       = note
            row.filename   = filename
            row.video_id   = video_id
            row.updated_at = datetime.now()
        else:
            row = VideoNote(
                file_hash=h, filename=filename, video_id=video_id,
                note=note, updated_at=datetime.now(),
            )
            ps.add(row)
        ps.commit()
        return row.to_dict()
    except Exception as e:
        ps.rollback()
        print(f"[play-db] 保存备注失败: {e}")
        return None
    finally:
        ps.close()


def delete_note_by_video_id(video_id):
    """视频被删除时清理其备注。"""
    ps = PlaySession()
    try:
        n = ps.query(VideoNote).filter(VideoNote.video_id == video_id).delete()
        ps.commit()
        return n
    except Exception:
        ps.rollback()
        return 0
    finally:
        ps.close()

# =====================================================================
# 10.5 备注标签 → 文件名（批量处理）
# =====================================================================
_NOTE_TAG_RE = re.compile(r"#([\u4e00-\u9fa5\w]+)")


def extract_note_tags(note: str) -> list[str]:
    """从备注文本里提取所有 #tag。"""
    if not note:
        return []
    return _NOTE_TAG_RE.findall(note)


def strip_note_tags(note: str, tags_to_remove) -> str:
    """从备注中移除指定 tag（其它内容保留），返回清理后的文本。"""
    if not note:
        return ""
    remove_set = set(tags_to_remove or [])
    if not remove_set:
        return note

    def repl(m):
        return "" if m.group(1) in remove_set else m.group(0)

    cleaned = _NOTE_TAG_RE.sub(repl, note)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _add_tags_to_filename(filename: str, tags) -> str:
    """把 tag 追加到文件名末尾（扩展名之前）。已存在的 tag 自动跳过。
    返回新文件名；未变化时返回原值。
    """
    if not filename or not tags:
        return filename

    stem, ext = os.path.splitext(filename)
    existing = set(re.findall(r"#([\u4e00-\u9fa5\w]+)", filename))

    to_add = []
    for t in tags:
        if t and t not in existing and t not in to_add:
            to_add.append(t)
    if not to_add:
        return filename

    suffix = "".join(f" #{t}" for t in to_add)
    return f"{stem}{suffix}{ext}"


def _find_file_by_hash(target_hash, filename_hint=None, extra_dirs=None):
    """通过 head-hash 在磁盘上定位文件。

    用途：
        数据库里 v.detail 已失效（文件被手动移动/改名），
        但 play 库的 FileHash 仍记录着 hash 时，找回文件真实路径。

    搜索顺序：
        1) extra_dirs（一般为原目录，命中概率最高）
        2) load_paths() 配置的视频根目录

    优化：
        - 优先尝试同名文件（filename_hint）
        - 先用 file_size 粗筛，只有大小一致才计算 hash（IO 最低）

    返回：找到的完整路径；未找到返回 None。
    """
    if not target_hash:
        return None

    # 目标大小：优先取 play 库的 FileHash.file_size
    target_size = None
    try:
        ps = PlaySession()
        try:
            fh = ps.query(FileHash).filter(
                FileHash.file_hash == target_hash
            ).first()
            if fh:
                target_size = fh.file_size
        finally:
            ps.close()
    except Exception:
        pass

    # 搜索根目录
    roots = []
    for d in (extra_dirs or []):
        if d and os.path.isdir(d) and d not in roots:
            roots.append(d)
    try:
        for p in load_paths():
            if p and os.path.isdir(p) and p not in roots:
                roots.append(p)
    except Exception:
        pass

    if not roots:
        return None

    def _matches(full):
        try:
            if target_size:
                if os.path.getsize(full) != target_size:
                    return False
        except OSError:
            return False
        h, _, _ = _hash_file(full)
        return h == target_hash

    # 第一遍：按同名优先（最可能命中，省 IO）
    if filename_hint:
        lower = filename_hint.lower()
        for root in roots:
            for dirpath, _, filenames in os.walk(root):
                for fn in filenames:
                    if fn.lower() != lower:
                        continue
                    full = os.path.join(dirpath, fn)
                    if _matches(full):
                        return full

    # 第二遍：全目录扫（扩展名 + 大小粗筛后算 hash）
    for root in roots:
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                if not fn.lower().endswith(VIDEO_EXTS):
                    continue
                full = os.path.join(dirpath, fn)
                if _matches(full):
                    return full
    return None


def list_pending_note_tags() -> dict:
    """扫描所有备注，返回 {tag: [ {note_id, video_id, filename, file_hash, note}, ... ]}。"""
    ps = PlaySession()
    try:
        notes = ps.query(VideoNote).all()
        result: dict[str, list] = {}
        for n in notes:
            for tag in extract_note_tags(n.note or ""):
                result.setdefault(tag, []).append({
                    "note_id": n.id,
                    "video_id": n.video_id,
                    "filename": n.filename,
                    "file_hash": n.file_hash,
                    "note": n.note,
                })
        return result
    finally:
        ps.close()


def _clear_tags_from_notes(ps, note_ids, tags, report):
    """把指定备注里的某些 tag 清掉；清空则删记录。"""
    for nid in note_ids:
        n = ps.query(VideoNote).filter(VideoNote.id == nid).first()
        if not n:
            continue
        new_note = strip_note_tags(n.note or "", tags)
        if new_note:
            if new_note != (n.note or ""):
                n.note = new_note
                n.updated_at = datetime.now()
                report["notes_updated"] += 1
        else:
            ps.delete(n)
            report["notes_cleared"] += 1


def _process_one_note_group(video_id, file_hash, note_ids, tags, report):
    """处理一个视频的所有待应用 tag：改名 → 更新 DB → 清备注。

    若主库路径失效（文件被手动移动/改名），会尝试用 file_hash 在磁盘上找回：
        - 找回成功：刷新主库 filename/detail，按找回的文件名继续加 tag 并改名
        - 找回失败：跳过，reason 标注 hash 未匹配
    """
    s = Session()
    ps = PlaySession()
    try:
        # ---------- 定位视频 ----------
        v = None
        if video_id:
            v = s.query(Video).filter(Video.id == video_id).first()

        if v is None and file_hash:
            fh = ps.query(FileHash).filter(FileHash.file_hash == file_hash).first()
            if fh:
                v = s.query(Video).filter(Video.filename == fh.filename).first()

        if v is None:
            report["skipped"].append({
                "reason": "视频记录不存在",
                "video_id": video_id,
                "tags": sorted(tags),
            })
            return

        old_name   = v.filename or ""
        old_detail = v.detail or ""

        # ---------- 源文件不存在 → 尝试通过 hash 找回 ----------
        if not old_detail or not os.path.isfile(old_detail):
            recovered = None
            if file_hash:
                recovered = _find_file_by_hash(
                    file_hash,
                    filename_hint=old_name,
                    extra_dirs=[os.path.dirname(old_detail)] if old_detail else None,
                )

            if recovered:
                # 磁盘上文件被移动/改名了，主库路径已失效。
                # 用找回的真实路径刷新主库，后续按新名称继续加 tag。
                old_detail = recovered
                old_name   = os.path.basename(recovered)
                v.filename      = old_name
                v.filename_norm = normalize_text(old_name)
                v.detail        = old_detail
                report.setdefault("recovered", []).append({
                    "video_id": v.id,
                    "new_path": old_detail,
                    "hash":     file_hash,
                })
            else:
                report["skipped"].append({
                    "reason": "源文件不存在，且 hash 未匹配到磁盘文件",
                    "video_id": v.id,
                    "filename": old_name,
                    "path":     old_detail,
                    "tags":     sorted(tags),
                })
                return

        new_name = _add_tags_to_filename(old_name, sorted(tags))

        # 文件名已经包含所有 tag → 只清备注
        if new_name == old_name:
            _clear_tags_from_notes(ps, note_ids, tags, report)
            s.commit()          # 提交可能发生的路径修复
            ps.commit()
            report["skipped"].append({
                "reason": "文件名已含全部 tag",
                "video_id": v.id,
                "filename": old_name,
                "tags":     sorted(tags),
            })
            return

        old_dir    = os.path.dirname(old_detail)
        new_detail = os.path.join(old_dir, new_name)

        # ---------- 目标文件已存在 ----------
        if os.path.exists(new_detail):
            s.commit()          # 提交可能发生的路径修复
            ps.commit()
            report["skipped"].append({
                "reason": "目标文件已存在",
                "video_id": v.id,
                "filename": old_name,
                "target":   new_detail,
                "tags":     sorted(tags),
            })
            return

        # ---------- 执行改名 ----------
        try:
            os.rename(old_detail, new_detail)
        except OSError as e:
            report["skipped"].append({
                "reason": f"文件重命名失败: {e}",
                "video_id": v.id,
                "filename": old_name,
                "tags":     sorted(tags),
            })
            return

        # ---------- 更新主库 ----------
        v.filename      = new_name
        v.filename_norm = normalize_text(new_name)
        v.detail        = new_detail

        # ---------- 更新 play 库快照（用 hash 定位，避免旧文件名歧义） ----------
        if file_hash:
            fh = ps.query(FileHash).filter(
                FileHash.file_hash == file_hash
            ).first()
            if fh:
                fh.filename   = new_name
                fh.updated_at = datetime.now()

        for vs in ps.query(VideoScore).filter(VideoScore.video_id == v.id).all():
            vs.filename = new_name

        for nt in ps.query(VideoNote).filter(VideoNote.video_id == v.id).all():
            nt.filename   = new_name
            nt.updated_at = datetime.now()

        # ---------- 清备注中的 tag ----------
        _clear_tags_from_notes(ps, note_ids, tags, report)

        s.commit()
        ps.commit()

        report["renamed"].append({
            "video_id": v.id,
            "old":      old_detail,
            "new":      new_detail,
            "tags":     sorted(tags),
        })
    except Exception:
        s.rollback()
        ps.rollback()
        raise
    finally:
        s.close()
        ps.close()


def apply_note_tags(tags=None, dry_run=False) -> dict:
    """把备注中的 #tag 应用到实际文件名 + 数据库；清掉备注里这些 tag。

    参数：
        tags:    list[str] | None，只处理这些 tag；None 表示全部。
        dry_run: True → 只返回计划，不执行。

    返回：
        {
          "renamed": [{video_id, old, new, tags}],
          "skipped": [{reason, video_id, filename, tags, ...}],
          "notes_updated": int,
          "notes_cleared": int,
          "errors": [{...}],
          "recovered": [{video_id, new_path, hash}],  # 路径靠 hash 找回的
          "planned": int,          # dry_run 模式下额外返回
        }
    """
    tag_filter = set(tags) if tags else None

    # ---------- 1. 收集 tag → 按视频分组 ----------
    ps = PlaySession()
    try:
        notes = ps.query(VideoNote).all()
    finally:
        ps.close()

    groups: dict = {}
    for n in notes:
        note_tags = [t for t in extract_note_tags(n.note or "")
                     if tag_filter is None or t in tag_filter]
        if not note_tags:
            continue
        key = f"v:{n.video_id}" if n.video_id else f"h:{n.file_hash}"
        g = groups.setdefault(key, {
            "video_id":  n.video_id,
            "file_hash": n.file_hash,
            "note_ids":  [],
            "tags":      set(),
        })
        g["note_ids"].append(n.id)
        g["tags"].update(note_tags)

    report = {
        "renamed": [],
        "skipped": [],
        "notes_updated": 0,
        "notes_cleared": 0,
        "errors": [],
        "recovered": [],
    }

    if not groups:
        return report

    # ---------- 2. dry_run 只生成计划 ----------
    if dry_run:
        s = Session()
        ps = PlaySession()
        try:
            for key, g in groups.items():
                v = None
                if g["video_id"]:
                    v = s.query(Video).filter(Video.id == g["video_id"]).first()
                if v is None and g["file_hash"]:
                    fh = ps.query(FileHash).filter(
                        FileHash.file_hash == g["file_hash"]
                    ).first()
                    if fh:
                        v = s.query(Video).filter(Video.filename == fh.filename).first()

                if not v:
                    report["skipped"].append({
                        "reason": "视频记录不存在",
                        "video_id": g["video_id"],
                        "tags": sorted(g["tags"]),
                    })
                    continue

                new_name = _add_tags_to_filename(v.filename or "", sorted(g["tags"]))
                if new_name == (v.filename or ""):
                    report["skipped"].append({
                        "reason": "文件名已含全部 tag",
                        "video_id": v.id,
                        "filename": v.filename,
                        "tags": sorted(g["tags"]),
                    })
                    continue

                new_detail = os.path.join(
                    os.path.dirname(v.detail or ""), new_name
                )
                report["renamed"].append({
                    "video_id": v.id,
                    "old":      v.detail,
                    "new":      new_detail,
                    "tags":     sorted(g["tags"]),
                })
        finally:
            s.close()
            ps.close()

        report["planned"] = len(report["renamed"])
        return report

    # ---------- 3. 真执行 ----------
    for key, g in groups.items():
        try:
            _process_one_note_group(
                g["video_id"], g["file_hash"],
                g["note_ids"], g["tags"], report,
            )
        except Exception as e:
            report["errors"].append({
                "key":   key,
                "error": repr(e),
            })
    return report
