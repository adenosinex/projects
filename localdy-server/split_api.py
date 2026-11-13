# -*- coding: utf-8 -*-
"""
split_api.py -- Localdy 服务端/客户端拆分后新增的服务端逻辑。

与原版 app.py 的关系：
    * 原版路由尽量不动；本模块只做新增（/health、/api/*、/download/*、/login）
      和少量覆盖（见 register_split_routes 末尾的 view_functions 替换）。
    * 鉴权沿用 mac-compute 同款模式：访问码只存 SHA-256，不存明文；
      浏览器用 HttpOnly Cookie，API 可用 X-Access-Code 请求头。
    * 客户端 <-> 服务端用另一枚独立 token（LOCALDY_CLIENT_TOKEN），
      只放行 /api/index-diff、/api/file-jobs*、/api/client/status、
      /api/hash-paths、/api/file-moves。

设计依据：workspace/your_files/localdy-server-client-design.md §4/§5/§7/§12/§13。

客户端定死的约定（2026-11-07 客户端实测后确定，设计稿里没这么细）：
  1. index-diff 按 hash 做 UPSERT：added 里 hash 已存在 = 路径更新，
     不建重复记录；换目录时客户端清空本地索引、全量重报 added，
     服务端按 root 清理残留（full_scan 标志，或 roots 列表缩减）。
  2. 文件任务先展开成具体文件再下发：{type, items:[{hash, path?, tags?}]}；
     客户端没有数据库，{type:"apply-tags", tags:[...]} 这类意图不能直发。
  3. 媒体票据 5 分钟内可重复使用（绑定单个 hash），不是一次性——
     票据的签发/校验在客户端实现，服务端只负责在前端按 hash 取票。
  4. 客户端探活时 /api/client/status 返回 401 也算“找到服务端”
     （路由存在只是要鉴权）——本模块未配访问码时返回 200，配了返回 401，
     两种都表示路由存在。
  5. 服务端默认端口 18930（设计稿 §13.1 的 :8080 是旧数）。
  6. 视频扩展名以 core.py 的 12 个为准（服务端不扫盘，只沿用同一份表）。

注意：本文件不读视频文件、不解析 NAS 路径。job payload 里的路径
（如 \\\\UGREEN-1E55\\...）对服务端永远是不透明字符串，只原样存取，
由客户端解析执行。
"""

import hashlib
import hmac
import ipaddress
import json
import ntpath
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime

from flask import jsonify, redirect, request, send_file
from sqlalchemy import func

from core import (
    BASE_DIR,
    DATA_DIR,
    FileHash,
    PlaySession,
    Session,
    Video,
    extract_tags,
    list_pending_note_tags,
    load_paths,
    normalize_text,
)

# ---------------------------------------------------------------------
# 0. 配置与鉴权
# ---------------------------------------------------------------------

def _sha(code: str) -> str:
    return hashlib.sha256(f"localdy:{code}".encode("utf-8")).hexdigest()


_CODE = os.environ.get("LOCALDY_CODE") or ""
ACCESS_HASH = _sha(_CODE) if _CODE else None
CLIENT_TOKEN = os.environ.get("LOCALDY_CLIENT_TOKEN") or None

AUTH_COOKIE = "localdy_auth"
CLIENT_DIR = os.path.join(BASE_DIR, "client")
CLIENT_STATUS_FILE = os.path.join(DATA_DIR, "client_status.json")
CLIENT_TOKEN_FILE = os.path.join(DATA_DIR, "client_token.json")  # 配对后保存的客户端 token（600 权限）
JOBS_DB_FILE = os.path.join(DATA_DIR, "file_jobs.db")

JOB_TYPES = {"apply-tags", "collect", "archive-1star", "copy-5star", "dedup", "rename"}
JOB_STALE_SECONDS = 30 * 60          # 认领后 30 分钟无结果视为过期，回 pending
CLIENT_ONLINE_SECONDS = 90           # 90 秒内有心跳视为在线


def auth_configured() -> bool:
    return ACCESS_HASH is not None


def _browser_authed() -> bool:
    if not ACCESS_HASH:
        return True
    cookie = request.cookies.get(AUTH_COOKIE, "")
    if cookie and hmac.compare_digest(cookie, ACCESS_HASH):
        return True
    header = request.headers.get("X-Access-Code", "")
    if header and hmac.compare_digest(_sha(header), ACCESS_HASH):
        return True
    return False


def _stored_client_token() -> str:
    """配对后保存的客户端 token（文件不存在返回空串）。"""
    try:
        with open(CLIENT_TOKEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        tok = (data or {}).get("token") or ""
        return tok if isinstance(tok, str) else ""
    except (OSError, ValueError):
        return ""


def _save_client_token(token: str) -> None:
    with open(CLIENT_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"token": token}, f)
    os.chmod(CLIENT_TOKEN_FILE, 0o600)


def _client_token_ok() -> bool:
    """客户端 token：环境变量或配对文件任一配置后，必须比对通过；
    两者都未配置时放行（启动时已警告）——配对前客户端需要先能连通，
    配对完成后即开始校验，客户端的心跳/待推送会重试。"""
    expected = CLIENT_TOKEN or _stored_client_token()
    if not expected:
        return True
    tok = request.headers.get("X-Client-Token", "")
    return bool(tok) and hmac.compare_digest(tok, expected)


_CLIENT_ENDPOINTS = ("/api/index-diff", "/api/file-jobs", "/api/client/status",
                     "/api/hash-paths", "/api/file-moves")


def _is_client_endpoint(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _CLIENT_ENDPOINTS)


def _auth_gate():
    """before_request 鉴权门。

    放行：/health、/login、/logout、/download/*。
    客户端 token：只放行 /api/index-diff、/api/file-jobs*、/api/client/status、
    /api/hash-paths、/api/file-moves。
    其余：未配访问码时放行（启动警告）；否则要求浏览器鉴权。
    """
    path = request.path
    if path in ("/health", "/login", "/logout") or path.startswith("/download/"):
        return None
    if _is_client_endpoint(path) and _client_token_ok():
        return None
    if not ACCESS_HASH:
        return None
    if _browser_authed():
        return None
    if path.startswith("/api/") or path.startswith("/static/"):
        return jsonify({"success": False, "error": "unauthorized"}), 401
    return redirect("/login")


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Localdy · 登录</title>
<style>body{font-family:system-ui,sans-serif;background:#e3e6eb;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:#fff;padding:28px 32px;border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.12)}
h1{font-size:20px;margin:0 0 8px}.err{color:#c00;font-size:14px}
input{font-size:16px;padding:8px 10px;border:1px solid #ccc;border-radius:6px;width:220px}
button{font-size:16px;padding:8px 18px;border:0;border-radius:6px;background:#222;color:#fff;cursor:pointer}
p{color:#666;font-size:13px}</style></head>
<body><div class="card"><h1>Localdy · 本地视频</h1>
{error}
<form method="post" action="/login">
<input type="password" name="code" placeholder="访问码" autofocus>
<button type="submit">进入</button></form>
<p>访问码由服务端管理员设置（环境变量 LOCALDY_CODE）。</p>
<hr style="border:0;border-top:1px solid #eee;margin:18px 0 12px">
<p style="color:#333;font-size:14px">还没装客户端？先下载（无需登录）：</p>
<p><a href="/download/localdy_client.py" download>下载客户端 localdy_client.py</a><br>
<a href="/download/localdy_client.bat" download>下载启动器 localdy_client.bat</a></p>
<p>下载后双击 .bat 启动即可（全局模式，无需输入路径；加新文件夹才用命令行 --root &lt;路径&gt;）。</p>
</div></body></html>"""


# ---------------------------------------------------------------------
# 1. 客户端状态（心跳）
# ---------------------------------------------------------------------

_status_lock = threading.Lock()
_status_cache = None


def _load_status():
    global _status_cache
    with _status_lock:
        if _status_cache is not None:
            return _status_cache
        try:
            with open(CLIENT_STATUS_FILE, "r", encoding="utf-8") as f:
                _status_cache = json.load(f)
        except (OSError, ValueError):
            _status_cache = {}
        return _status_cache


def _save_status(data):
    global _status_cache
    with _status_lock:
        _status_cache = data
        tmp = CLIENT_STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, CLIENT_STATUS_FILE)


def record_heartbeat(payload):
    now = time.time()
    prev = _load_status()
    roots = list(payload.get("roots") or [])
    # known_roots：该客户端历史上报过的所有根目录（用于 index-diff 的
    # stale-root 清理）；心跳只做并集，不删除。
    known_roots = sorted(set(prev.get("known_roots") or []) | set(roots))
    data = {
        "client_id": payload.get("client_id", ""),
        "port": int(payload.get("port") or 18931),
        "version": payload.get("version", ""),
        "lan_ips": list(payload.get("lan_ips") or []),
        "tailscale_ip": payload.get("tailscale_ip") or "",
        "last_scan": payload.get("last_scan") or "",
        "roots": roots,
        "known_roots": known_roots,
        "seen_at": now,
        "seen_at_str": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_status(data)
    return data


def get_client_status():
    data = dict(_load_status())
    seen = data.get("seen_at") or 0
    data["online"] = bool(seen) and (time.time() - seen) < CLIENT_ONLINE_SECONDS
    return data


def client_reported_roots():
    """客户端上报的视频根目录（供 /video-paths 只读展示）。未上报过则
    回退到随库复制的 video_paths.json 内容。"""
    st = _load_status()
    roots = st.get("roots") or []
    if roots:
        return roots
    return load_paths()


def _same_slash24(a, b):
    try:
        return a.rsplit(".", 1)[0] == b.rsplit(".", 1)[0]
    except (AttributeError, IndexError):
        return False


def _is_private_ip(ip):
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def select_client_base(remote_addr):
    """按访问者所在子网挑客户端地址（设计 §13）。

    同机（127.0.0.1）-> 127.0.0.1；同子网 -> 该子网的客户端局域网 IP；
    局域网内其他地址 -> 第一个上报的局域网 IP；其余（公网访问）->
    127.0.0.1（浏览器与客户端同机的主场景，见 §12.6）。
    """
    st = _load_status()
    port = int(st.get("port") or 18931)
    lan_ips = st.get("lan_ips") or []
    if remote_addr in ("127.0.0.1", "::1"):
        return f"http://127.0.0.1:{port}"
    for ip in lan_ips:
        if _same_slash24(remote_addr, ip):
            return f"http://{ip}:{port}"
    if lan_ips and _is_private_ip(remote_addr):
        return f"http://{lan_ips[0]}:{port}"
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------
# 2. 文件任务队列（file_jobs.db，独立小库，不动原 schema）
# ---------------------------------------------------------------------

def _jobs_conn():
    conn = sqlite3.connect(JOBS_DB_FILE, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS file_jobs ("
        "  id TEXT PRIMARY KEY, client_id TEXT, type TEXT, payload TEXT,"
        "  status TEXT, created_at REAL, updated_at REAL,"
        "  claimed_by TEXT, claimed_at REAL, result TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_file_jobs_status "
        "ON file_jobs(status, created_at)"
    )
    conn.commit()
    return conn


def create_file_job(job_type, payload, client_id=""):
    job_id = uuid.uuid4().hex[:16]
    now = time.time()
    conn = _jobs_conn()
    try:
        conn.execute(
            "INSERT INTO file_jobs "
            "(id, client_id, type, payload, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (job_id, client_id, job_type, json.dumps(payload, ensure_ascii=False),
             now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return job_id


def _row_to_job(row):
    keys = ("id", "client_id", "type", "payload", "status", "created_at",
            "updated_at", "claimed_by", "claimed_at", "result")
    d = dict(zip(keys, row))
    for k in ("payload", "result"):
        try:
            d[k] = json.loads(d[k]) if d[k] else None
        except ValueError:
            pass
    # 客户端约定（设计稿 §5.3）：下发的 job 顶层就要带 items，客户端
    # JobStore.create 直接读顶层 type/items/dest/mode/tags。payload 里
    # 已展开的 items 提升到顶层，payload 本身保留不动。
    p = d.get("payload") or {}
    if isinstance(p, dict):
        d.setdefault("items", p.get("items") or [])
        for k in ("dest", "mode", "tags"):
            if k in p:
                d.setdefault(k, p[k])
    return d


def get_file_job(job_id):
    conn = _jobs_conn()
    try:
        row = conn.execute(
            "SELECT * FROM file_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return _row_to_job(row) if row else None
    finally:
        conn.close()


def list_file_jobs(status=None, limit=50):
    conn = _jobs_conn()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM file_jobs WHERE status = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM file_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_job(r) for r in rows]
    finally:
        conn.close()


def poll_pending_jobs(client_id):
    """客户端取活：过期认领回 pending；取走 pending 并标记 running。"""
    now = time.time()
    conn = _jobs_conn()
    try:
        conn.execute(
            "UPDATE file_jobs SET status='pending', claimed_by=NULL, "
            "claimed_at=NULL, updated_at=? "
            "WHERE status='running' AND claimed_at IS NOT NULL "
            "AND claimed_at < ?",
            (now, now - JOB_STALE_SECONDS),
        )
        if client_id:
            rows = conn.execute(
                "SELECT * FROM file_jobs WHERE status='pending' "
                "AND (client_id='' OR client_id=?) ORDER BY created_at ASC",
                (client_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM file_jobs WHERE status='pending' "
                "ORDER BY created_at ASC"
            ).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            conn.executemany(
                "UPDATE file_jobs SET status='running', claimed_by=?, "
                "claimed_at=?, updated_at=? WHERE id=?",
                [(client_id, now, now, i) for i in ids],
            )
        conn.commit()
        # 返回认领后的状态（不再是取走前的 pending）
        claimed = []
        for r in rows:
            d = _row_to_job(r)
            d["status"] = "running"
            d["claimed_by"] = client_id
            claimed.append(d)
        return claimed
    finally:
        conn.close()


def apply_renames(renamed):
    """把客户端回报的改名应用到 videos + file_hashes（按旧路径匹配）。

    file_hashes 以 filename 为主键，改名即改主键；play_records /
    video_scores / video_notes 按 hash 关联，不用动。
    返回实际更新条数。
    """
    if not renamed:
        return 0
    applied = 0
    s = Session()
    ps = PlaySession()
    try:
        for item in renamed:
            old = (item or {}).get("old") or ""
            new = (item or {}).get("new") or ""
            if not old or not new or old == new:
                continue
            old_name = _win_basename(old)
            new_name = _win_basename(new)
            # 主库：按旧完整路径匹配
            n = s.query(Video).filter(Video.detail == old).update(
                {"detail": new, "filename": new_name,
                 "filename_norm": normalize_text(new_name)},
                synchronize_session=False,
            )
            if n:
                applied += n
            # file_hashes：按旧文件名匹配（主键改名；目标已存在则跳过）
            if old_name != new_name:
                clash = ps.query(FileHash).filter(
                    FileHash.filename == new_name).first()
                if not clash:
                    ps.query(FileHash).filter(
                        FileHash.filename == old_name).update(
                        {"filename": new_name}, synchronize_session=False)
        s.commit()
        ps.commit()
    except Exception as e:
        s.rollback()
        ps.rollback()
        print(f"[file-jobs] 应用改名失败: {e}")
        raise
    finally:
        s.close()
        ps.close()
    return applied


def set_job_result(job_id, result):
    job = get_file_job(job_id)
    if not job:
        return None
    # 客户端回报包了一层：{ok, status, result: {ok, status, done, total,
    # results:[{hash, ok, old, new}]}}——先拆开再读。
    inner = (result or {}).get("result") or {}
    ok = bool((result or {}).get("ok")) and bool(inner.get("ok", True))
    # 移动类任务（rename / apply-tags / archive-1star / dedup）把 old->new
    # 写回 videos.detail。collect / copy-5star 是复制，原文件还在原地，
    # 绝不能动库路径。
    renamed = inner.get("renamed") or (result or {}).get("renamed") or []
    if not renamed and job.get("type") in ("rename", "apply-tags",
                                           "archive-1star", "dedup"):
        renamed = [r for r in inner.get("results") or []
                   if isinstance(r, dict) and r.get("ok")
                   and r.get("old") and r.get("new")]
    applied = 0
    if ok and renamed:
        applied = apply_renames(renamed)
    conn = _jobs_conn()
    try:
        conn.execute(
            "UPDATE file_jobs SET status=?, result=?, updated_at=? WHERE id=?",
            ("done" if ok else "error",
             json.dumps(result, ensure_ascii=False), time.time(), job_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"job_id": job_id, "status": "done" if ok else "error",
            "renames_applied": applied}


# ---------------------------------------------------------------------
# 3. 索引差异（POST /api/index-diff）
# ---------------------------------------------------------------------

def _win_basename(p):
    """Windows 风格取文件名：客户端上报的是 Windows 路径（反斜杠），
    服务端跑在 Linux 上，os.path.basename 认不出反斜杠，必须用 ntpath。"""
    return ntpath.basename(p or "")


def _norm_sep(p):
    return (p or "").replace("\\", "/")


def _under_root(detail, root):
    d = _norm_sep(detail)
    r = _norm_sep(root).rstrip("/")
    return d == r or d.startswith(r + "/")


def _delete_video_paths(s, ps, paths):
    """删除指定 detail 的视频记录 + 对应 file_hashes。
    play_records / video_scores / video_notes 保留（与 _delete_missing 一致）。
    返回 (deleted_videos, cleaned_hashes)。"""
    if not paths:
        return 0, 0
    rows = s.query(Video.filename).filter(Video.detail.in_(paths)).all()
    names = [r[0] for r in rows if r[0]]
    n_hash = 0
    if names:
        n_hash = ps.query(FileHash).filter(
            FileHash.filename.in_(names)).delete(synchronize_session=False)
    n_vid = s.query(Video).filter(
        Video.detail.in_(paths)).delete(synchronize_session=False)
    return n_vid, n_hash


def _upsert_file_hash(ps, filename, file_hash, size, mtime):
    ps.query(FileHash).filter(FileHash.filename == filename).delete(
        synchronize_session=False)
    ps.add(FileHash(
        filename=filename, file_hash=file_hash,
        file_size=int(size or 0), mtime=float(mtime or 0),
        updated_at=datetime.now(),
    ))


def _upsert_by_hash(s, ps, path, filename, size, mtime, file_hash):
    """按 hash 做 UPSERT（客户端定死的约定 1）。

    返回 ("inserted" | "moved" | "updated", video_id_or_None)。
    - 同 detail 已存在 -> 原地更新大小/文件名/hash。
    - 同 hash 已存在（不同 detail）-> 文件被移动/改名：更新旧记录的
      detail/filename/file_size，并把 file_hashes 的主键文件名改过来。
    - 都不存在 -> 新增（tags/score 默认值与原版 _add_new_files 一致）。
    """
    name = filename or _win_basename(path)
    row = s.query(Video).filter(Video.detail == path).first()
    if row:
        row.file_size = int(size or 0)
        if row.filename != name:
            row.filename = name
            row.filename_norm = normalize_text(name)
        _upsert_file_hash(ps, name, file_hash, size, mtime)
        return "updated", row.id
    fh = ps.query(FileHash).filter(FileHash.file_hash == file_hash).first()
    if fh and fh.filename:
        old_name = fh.filename
        old_row = s.query(Video).filter(Video.filename == old_name).first()
        if old_row is not None:
            old_row.detail = path
            old_row.filename = name
            old_row.filename_norm = normalize_text(name)
            old_row.file_size = int(size or 0)
        else:
            # hash 在、视频记录不在（库不一致）：按新增补一条
            tags = extract_tags(name)
            old_row = Video(
                filename=name, filename_norm=normalize_text(name),
                tags=",".join(tags) if isinstance(tags, list) else tags,
                score=0, detail=path, file_size=int(size or 0))
            s.add(old_row)
        if old_name != name:
            # file_hashes 主键是 filename：目标名已存在先删（后写为准）
            ps.query(FileHash).filter(FileHash.filename == name).delete(
                synchronize_session=False)
            fh.filename = name
        fh.file_size = int(size or 0)
        fh.mtime = float(mtime or 0)
        fh.updated_at = datetime.now()
        return "moved", (old_row.id if old_row is not None else None)
    tags = extract_tags(name)
    s.add(Video(
        filename=name,
        filename_norm=normalize_text(name),
        tags=",".join(tags) if isinstance(tags, list) else tags,
        score=0,
        detail=path,
        file_size=int(size or 0),
    ))
    _upsert_file_hash(ps, name, file_hash, size, mtime)
    return "inserted", None


def apply_index_diff(data):
    """按设计 §5.2 + 客户端约定 1 应用差异。

    added/changed 按 hash 做 UPSERT（见 _upsert_by_hash）；
    removed 按路径删（历史记录保留）。
    根目录清理（约定 1 后半句）：
      * full_scan/rescan=true + root：把 root 下不在本次 added/changed
        路径集合里的记录删掉（客户端清空本地索引后全量重报的场景）。
        没有 full_scan 标志时绝不做这步——增量推送不能删库。
      * roots=[...]：把该客户端曾经上报、现已不在列表里的根目录下的
        记录删掉（换目录场景）。
    两个库各包在一个事务里（两个 SQLite 文件无法共享一个事务，
    输入先做完整校验再写，见 README 偏差说明）。
    """
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    added = data.get("added") or []
    removed = data.get("removed") or []
    changed = data.get("changed") or []
    if not all(isinstance(x, list) for x in (added, removed, changed)):
        raise ValueError("added/removed/changed 必须是数组")

    for item in added:
        if not isinstance(item, dict) or not item.get("path") or not item.get("hash"):
            raise ValueError("added 每一项必须包含 path 和 hash")
    for item in changed:
        if not isinstance(item, dict) or not item.get("path") or not item.get("hash"):
            raise ValueError("changed 每一项必须包含 path 和 hash")
    if not all(isinstance(p, str) and p for p in removed):
        raise ValueError("removed 每一项必须是非空路径字符串")

    n_added = n_removed = n_changed = n_moved = n_cleaned = 0
    s = Session()
    ps = PlaySession()
    try:
        # --- removed：按路径删 ---
        n_v, _ = _delete_video_paths(s, ps, list(removed))
        n_removed = n_v

        # --- added / changed：按 hash UPSERT ---
        seen_paths = set()
        for item in added:
            seen_paths.add(_norm_sep(item["path"]))
            status, _ = _upsert_by_hash(
                s, ps, item["path"], item.get("filename"),
                item.get("size"), item.get("mtime"), item["hash"])
            if status == "inserted":
                n_added += 1
            elif status == "moved":
                n_moved += 1
            else:
                n_changed += 1
        for item in changed:
            seen_paths.add(_norm_sep(item["path"]))
            status, _ = _upsert_by_hash(
                s, ps, item["path"], item.get("filename"),
                item.get("size"), item.get("mtime"), item["hash"])
            if status == "moved":
                n_moved += 1
            else:
                n_changed += 1

        # --- 根目录清理：full_scan 全量重报 ---
        root = (data.get("root") or "").strip()
        full = bool(data.get("full_scan") or data.get("rescan"))
        if root and full:
            stale = [r[0] for r in s.query(Video.detail).all()
                     if r[0] and _under_root(r[0], root)
                     and _norm_sep(r[0]) not in seen_paths]
            n_v, _ = _delete_video_paths(s, ps, stale)
            n_cleaned += n_v

        # --- 根目录清理：roots 列表缩减（换目录）---
        roots = data.get("roots")
        if isinstance(roots, list):
            current = {str(r) for r in roots if r}
            known = set(_load_status().get("known_roots") or []) | current
            stale_roots = [r for r in known if r not in current]
            if stale_roots:
                stale = [r[0] for r in s.query(Video.detail).all()
                         if r[0] and any(_under_root(r[0], sr)
                                        for sr in stale_roots)]
                n_v, _ = _delete_video_paths(s, ps, stale)
                n_cleaned += n_v
            st = _load_status()
            st["known_roots"] = sorted(known)
            st["roots"] = sorted(current)
            _save_status(st)

        s.commit()
        ps.commit()
    except Exception:
        s.rollback()
        ps.rollback()
        raise
    finally:
        s.close()
        ps.close()

    return {"added": n_added, "removed": n_removed, "changed": n_changed,
            "moved": n_moved, "cleaned": n_cleaned}


# ---------------------------------------------------------------------
# 3b. 任务展开：意图 -> 具体文件（客户端约定 2）
# ---------------------------------------------------------------------

def _hash_map_for_filenames(names):
    ps = PlaySession()
    try:
        rows = ps.query(FileHash).filter(
            FileHash.filename.in_(names)).all()
        return {r.filename: r.file_hash for r in rows}
    finally:
        ps.close()


def _videos_to_items(videos):
    """Video 行 -> [{hash, path}]；无 hash 的跳过并计数。"""
    names = [v.filename for v in videos if v.filename]
    hmap = _hash_map_for_filenames(names) if names else {}
    items, missing = [], 0
    for v in videos:
        h = hmap.get(v.filename)
        if not h:
            missing += 1
            continue
        items.append({"hash": h, "path": v.detail or ""})
    return items, missing


def expand_job_items(job_type, payload):
    """把意图展开成具体文件 [{hash, path, ...}]（客户端约定 2）。

    客户端没有数据库，无法把 tag/关键词解析成文件，所以所有需要
    查库的解析都在服务端做完再下发。返回 (items, meta)。
    """
    payload = payload or {}
    s = Session()
    try:
        if job_type == "apply-tags":
            wanted = payload.get("tags")  # None -> 全部待处理标签
            pending = list_pending_note_tags()
            by_video = {}
            for tag, entries in pending.items():
                if wanted is not None and tag not in wanted:
                    continue
                for e in entries:
                    key = e.get("video_id") or e.get("filename")
                    slot = by_video.setdefault(key, {
                        "video_id": e.get("video_id"),
                        "filename": e.get("filename"),
                        "file_hash": e.get("file_hash"), "tags": []})
                    if tag not in slot["tags"]:
                        slot["tags"].append(tag)
            items, missing = [], 0
            for slot in by_video.values():
                v = None
                if slot["video_id"] is not None:
                    v = s.query(Video).filter(
                        Video.id == slot["video_id"]).first()
                path = v.detail if v else ""
                h = slot.get("file_hash")
                if not h and v is not None:
                    h = _hash_map_for_filenames([v.filename]).get(v.filename)
                if not h:
                    missing += 1
                    continue
                items.append({"hash": h, "path": path, "tags": slot["tags"]})
            return items, {"files": len(items), "missing_no_hash": missing,
                           "tags": wanted}
        if job_type == "collect":
            key = (payload.get("key") or "").strip()
            if not key:
                raise ValueError("collect 需要 key")
            key_norm = normalize_text(key)
            rows = s.query(Video).filter(
                Video.filename_norm.contains(key_norm, autoescape=True)).all()
            items, missing = _videos_to_items(rows)
            return items, {"key": key, "matched": len(rows),
                           "files": len(items), "missing_no_hash": missing}
        if job_type == "archive-1star":
            rows = s.query(Video).filter(Video.score == 1).all()
            items, missing = _videos_to_items(rows)
            return items, {"files": len(items), "missing_no_hash": missing}
        if job_type == "copy-5star":
            rows = s.query(Video).filter(Video.score == 5).all()
            items, missing = _videos_to_items(rows)
            return items, {"files": len(items), "missing_no_hash": missing}
        if job_type == "dedup":
            ps = PlaySession()
            try:
                hashes = [r[0] for r in ps.query(FileHash.file_hash)
                          .group_by(FileHash.file_hash)
                          .having(func.count(FileHash.file_hash) > 1).all()]
                dup_rows = ps.query(FileHash).filter(
                    FileHash.file_hash.in_(hashes)).all() if hashes else []
            finally:
                ps.close()
            items = []
            seen_hash = set()
            for fh in sorted(dup_rows,
                             key=lambda r: (r.file_hash, r.filename)):
                keep = fh.file_hash not in seen_hash
                seen_hash.add(fh.file_hash)
                v = s.query(Video).filter(
                    Video.filename == fh.filename).first()
                items.append({"hash": fh.file_hash,
                              "path": v.detail if v else "",
                              "keep": keep})
            return items, {"groups": len(hashes), "files": len(items)}
        if job_type == "rename":
            pairs = payload.get("renames") or payload.get("items") or []
            if isinstance(payload.get("old"), str) and payload.get("new"):
                pairs = [{"old": payload["old"], "new": payload["new"]}]
            items = []
            for p in pairs:
                old = (p or {}).get("old") or ""
                new = (p or {}).get("new") or (p or {}).get("new_path") or ""
                if not old:
                    continue
                v = s.query(Video).filter(Video.detail == old).first()
                h = (p or {}).get("hash")
                if not h and v is not None:
                    h = _hash_map_for_filenames([v.filename]).get(v.filename)
                if not h:
                    continue
                items.append({"hash": h, "path": old, "new_path": new,
                              # 客户端约定：改名执行器读 item["new_name"]（纯文件名，
                              # 不含路径）；new_path 保留供服务端回写库路径用。
                              "new_name": _win_basename(new)})
            return items, {"files": len(items)}
        raise ValueError(f"未知任务类型: {job_type}")
    finally:
        s.close()


def create_expanded_job(job_type, payload, items=None, client_id=""):
    """建任务：先展开成具体文件再入库下发（客户端约定 2）。

    存入的 payload = 原参数 + items:[{hash, path, ...}]；
    客户端轮询到后直接按 items 执行，不用再查库。
    """
    if job_type not in JOB_TYPES:
        raise ValueError(f"type 必须是 {sorted(JOB_TYPES)} 之一")
    if items is None:
        items, _meta = expand_job_items(job_type, payload)
    full_payload = dict(payload or {})
    full_payload["items"] = items
    return create_file_job(job_type, full_payload, client_id)


# ---------------------------------------------------------------------
# 4. 首页注入
# ---------------------------------------------------------------------

def render_index():
    """读 index.html，注入 window.LOCALDY 配置后返回。

    client 地址按访问者子网选择（见 select_client_base）；api 为本服务
    的 origin；download 指向客户端下载地址。
    """
    from flask import Response
    index_path = os.path.join(BASE_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        html = f.read()
    cfg = {
        "api": request.url_root.rstrip("/"),
        "client": select_client_base(request.remote_addr or ""),
        "download": "/download/localdy_client.py",
        "split": True,
    }
    inject = ('<script>window.LOCALDY = %s;</script>'
              % json.dumps(cfg, ensure_ascii=False))
    if "</head>" in html:
        html = html.replace("</head>", inject + "\n</head>", 1)
    else:
        html = inject + "\n" + html
    return Response(html, mimetype="text/html")


# ---------------------------------------------------------------------
# 5. 路由注册
# ---------------------------------------------------------------------

def _video_count():
    s = Session()
    try:
        return s.query(Video).count()
    finally:
        s.close()


def register_split_routes(app):
    app.before_request(_auth_gate)

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({
            "ok": True,
            "service": "localdy-server",
            "mode": "split",
            "auth": auth_configured(),
            "videos": _video_count(),
            "client": get_client_status(),
        })

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if _browser_authed() and ACCESS_HASH:
                return redirect("/")
            return LOGIN_PAGE.replace("{error}", "")
        code = (request.form.get("code") or "").strip()
        if ACCESS_HASH and code and hmac.compare_digest(_sha(code), ACCESS_HASH):
            resp = redirect("/")
            resp.set_cookie(AUTH_COOKIE, ACCESS_HASH, max_age=30 * 86400,
                            httponly=True, samesite="Lax")
            return resp, 303
        return LOGIN_PAGE.replace(
            "{error}", '<p class="err">访问码不正确，请重试。</p>'), 401

    @app.route("/logout", methods=["GET"])
    def logout():
        resp = redirect("/login")
        resp.delete_cookie(AUTH_COOKIE)
        return resp

    @app.route("/download/<name>", methods=["GET"])
    def download_client(name):
        allowed = {"localdy_client.py", "localdy_client.bat"}
        if name not in allowed:
            return jsonify({"success": False, "error": "not found"}), 404
        path = os.path.join(CLIENT_DIR, name)
        if not os.path.isfile(path):
            return jsonify({"success": False,
                            "error": "客户端文件尚未提供"}), 404
        return send_file(path, as_attachment=True)

    @app.route("/api/index-diff", methods=["POST"])
    def index_diff():
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"success": False, "error": "请求体必须是 JSON"}), 400
        try:
            counts = apply_index_diff(data)
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        except Exception as e:
            app.logger.exception("index-diff failed")
            return jsonify({"success": False, "error": str(e)}), 500
        if data.get("root") or data.get("roots"):
            st = _load_status()
            roots = st.get("roots") or []
            known = set(st.get("known_roots") or [])
            for r in ([data["root"]] if data.get("root") else []) + list(data.get("roots") or []):
                if r and r not in roots:
                    roots.append(r)
                if r:
                    known.add(r)
            st["roots"] = roots
            st["known_roots"] = sorted(known)
            _save_status(st)
        return jsonify({"success": True, "client_id": data.get("client_id"),
                        **counts})

    @app.route("/api/hash-paths", methods=["GET"])
    def hash_paths():
        """全局模式：返回全部视频的 hash -> 路径表，供客户端同步本地索引。

        客户端启动时拉一次（之后每 30 分钟一次，票据缺失时即时一次），
        本地不扫盘也能播放。只读，不写库。"""
        s = Session()
        try:
            videos = s.query(Video).all()
            items, missing = _videos_to_items(videos)
        finally:
            s.close()
        m = {}
        for it in items:
            if it.get("hash") and it.get("path"):
                m[it["hash"]] = it["path"]
        return jsonify({"ok": True, "count": len(m),
                        "missing_no_hash": missing, "map": m})

    @app.route("/api/file-moves", methods=["POST"])
    def file_moves():
        """会话任务的结果上报：只按文件改库，不整批索引。

        kind=move（rename / apply-tags / archive-1star / dedup）：
        库里有 old 路径 -> 改 detail（复用 apply_renames）；库里没有 -> 忽略。
        kind=copy（collect / copy-5star）：只有 new 路径在文库根目录
        （video_paths.json）下、且 hash 不在库里时才入库；已在库里的是
        重复副本，保留原记录。"""
        data = request.get_json(silent=True) or {}
        moves = data.get("moves") or []
        lib_roots = load_paths()
        renamed, to_ingest = [], []
        for m in moves or []:
            if not isinstance(m, dict):
                continue
            old, new = (m.get("old") or ""), (m.get("new") or "")
            h = m.get("hash") or ""
            if not old or not new or old == new:
                continue
            if m.get("kind") == "copy":
                if h and any(_under_root(new, r) for r in lib_roots):
                    to_ingest.append(m)
            else:
                renamed.append({"old": old, "new": new})
        applied = apply_renames(renamed) if renamed else 0
        ingested = 0
        if to_ingest:
            s = Session()
            ps = PlaySession()
            try:
                for m in to_ingest:
                    h = m["hash"]
                    exists = ps.query(FileHash).filter(
                        FileHash.file_hash == h).first()
                    if exists:
                        continue
                    _upsert_by_hash(s, ps, m["new"], None,
                                    m.get("size"), m.get("mtime"), h)
                    ingested += 1
                s.commit()
                ps.commit()
            except Exception:
                s.rollback()
                ps.rollback()
                raise
            finally:
                s.close()
                ps.close()
        return jsonify({"success": True, "applied": applied,
                        "ingested": ingested})

    @app.route("/api/file-jobs", methods=["POST"])
    def create_job():
        data = request.get_json(silent=True) or {}
        job_type = data.get("type")
        if job_type not in JOB_TYPES:
            return jsonify({"success": False,
                            "error": f"type 必须是 {sorted(JOB_TYPES)} 之一"}), 400
        try:
            # 客户端约定 2：意图先展开成具体文件再入库下发。
            job_id = create_expanded_job(
                job_type, data.get("payload") or data,
                client_id=data.get("client_id") or "")
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        job = get_file_job(job_id)
        payload = (job or {}).get("payload") or {}
        n_files = len(payload.get("items") or [])
        return jsonify({"success": True, "job_id": job_id,
                        "type": job_type, "status": "pending",
                        "files": n_files})

    @app.route("/api/file-jobs", methods=["GET"])
    def poll_jobs():
        # 客户端取活（带 client token）或浏览器查看（访问码）
        if request.args.get("status") == "pending":
            jobs = poll_pending_jobs(request.args.get("client_id") or "")
            return jsonify({"jobs": jobs})
        return jsonify({"jobs": list_file_jobs(
            status=request.args.get("status"),
            limit=int(request.args.get("limit", 50)))})

    @app.route("/api/file-jobs/<job_id>", methods=["GET"])
    def get_job(job_id):
        job = get_file_job(job_id)
        if not job:
            return jsonify({"success": False, "error": "任务不存在"}), 404
        return jsonify(job)

    @app.route("/api/file-jobs/<job_id>/result", methods=["POST"])
    def job_result(job_id):
        result = request.get_json(silent=True)
        if result is None:
            return jsonify({"success": False, "error": "请求体必须是 JSON"}), 400
        try:
            out = set_job_result(job_id, result)
        except Exception as e:
            app.logger.exception("job result failed")
            return jsonify({"success": False, "error": str(e)}), 500
        if out is None:
            return jsonify({"success": False, "error": "任务不存在"}), 404
        return jsonify({"success": True, **out})

    @app.route("/api/client/status", methods=["GET", "POST"])
    def client_status():
        if request.method == "POST":
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({"success": False,
                                "error": "请求体必须是 JSON"}), 400
            return jsonify({"success": True, **record_heartbeat(data)})
        return jsonify(get_client_status())

    @app.route("/api/pair-client", methods=["GET", "POST"])
    def pair_client():
        """客户端配对：浏览器已通过访问码鉴权（_auth_gate）。
        POST {token}：保存客户端首次启动时生成的 token，之后客户端
        推索引/取任务时服务端用它校验。GET：只返回是否已配对，绝不返回 token。"""
        if request.method == "GET":
            paired = bool(CLIENT_TOKEN or _stored_client_token())
            return jsonify({"success": True, "paired": paired})
        data = request.get_json(silent=True) or {}
        token = (data.get("token") or "").strip()
        if len(token) < 16:
            return jsonify({"success": False, "error": "配对码太短"}), 400
        _save_client_token(token)
        return jsonify({"success": True, "paired": True,
                        "message": "已配对，服务端将用该 token 校验客户端"})

    # ---- 覆盖原版的改文件路由：服务端不再直接碰文件 ----
    # /videos/collect 的原实现约 230 行文件操作代码在 app.py 内，
    # 直接改写会动大段原文；改为替换视图函数（Flask 按 endpoint 查表，
    # 替换后原函数不再被调用）。其余覆盖点见 app.py 内的直接改写。
    def collect_as_job():
        data = request.get_json(silent=True) or {}
        key = (data.get("key") or "").strip()
        if not key:
            return jsonify({"success": False, "error": "key 不能为空"}), 400
        # 客户端约定 2：关键词先在服务端解析成具体文件再下发。
        job_id = create_expanded_job("collect", {
            "key": key,
            "max_total_gb": data.get("max_total_gb"),
            "dest_root": data.get("dest_root") or "",
        }, client_id=data.get("client_id") or "")
        job = get_file_job(job_id)
        payload = (job or {}).get("payload") or {}
        return jsonify({"success": True, "queued": True, "job_id": job_id,
                        "type": "collect", "key": key,
                        "files": len(payload.get("items") or []),
                        "message": "已记为文件任务，等客户端执行（服务端不直接操作文件）"})

    app.view_functions["collect_links"] = collect_as_job


def log_startup_warnings():
    if not ACCESS_HASH:
        print("[WARN] 未设置 LOCALDY_CODE：服务端无访问码鉴权，"
              "仅限可信局域网使用！")
    else:
        print("[auth] 访问码鉴权已启用（LOCALDY_CODE，SHA-256 比对）")
    if not CLIENT_TOKEN:
        if _stored_client_token():
            print("[auth] 客户端 token 校验已启用（配对文件）")
        else:
            print("[WARN] 未设置 LOCALDY_CLIENT_TOKEN 且未配对：客户端接口暂不校验 token，"
                  "请在网页上输入客户端控制台显示的配对码完成配对。")
    else:
        print("[auth] 客户端 token 校验已启用")
    print(f"[split] 客户端下载：/download/localdy_client.py "
          f"（文件在 {CLIENT_DIR}）")
