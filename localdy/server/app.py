# -*- coding: utf-8 -*-

"""

app.py —— Web 核心路由（单体文件）


启动：

    python app.py                     # 默认 80 端口，环境变量 PORT 可覆盖

    FLASK_DEBUG=1 python app.py       # 开发模式（关 reloader，仅开调试页）


生产建议：

    pip install waitress

    set PORT=8080 && python app.py    # 自动走 waitress（多线程），不会重载

"""


import json

import mimetypes

import ntpath

import os

import threading

import time

import uuid

from dataclasses import dataclass, field

from typing import Optional


from flask import (

    Flask, Response, abort, jsonify, request, send_file, stream_with_context,

)

from flask_cors import CORS

from sqlalchemy import or_


from core import (
    BASE_DIR,
    Session,
    Video,
    FileHash,
    PlaySession,
    apply_note_tags,

    apply_watched_filter,

    count_video_scores_by_score,

    delete_video,

    del_videos_path,

    extract_tags,

    get_note,

    init_data_from_folder,

    init_db,

    init_play_db,

    list_pending_note_tags,

    list_video_scores,

    load_paths,

    migrate_database,

    normalize_text,

    record_play,

    run_save_star,

    save_note,
    save_paths,
    sync_video_score,
)

# 【拆分版】新增的服务端逻辑（鉴权、/api/*、/download/*、首页注入）
from split_api import (
    client_reported_roots,
    create_expanded_job,
    create_file_job,
    expand_job_items,

    log_startup_warnings,
    register_split_routes,
    render_index,
)


def _hash_for_filename(filename):
    """查 file_hashes 得 hash（服务端不读文件）。查不到返回 None。"""
    if not filename:
        return None
    ps = PlaySession()
    try:
        row = ps.query(FileHash).filter(FileHash.filename == filename).first()
        return row.file_hash if row else None
    finally:
        ps.close()


INDEX_HTML = os.path.join(BASE_DIR, "index.html")


app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"))


# =========================================================

# 1. CORS

# =========================================================

CORS(

    app,

    resources={r"/*": {"origins": "*"}},

    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],

    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],

    expose_headers=["Content-Type"],

    max_age=3600,

    supports_credentials=False,

)


@app.after_request

def ensure_cors_headers(resp):

    origin = request.headers.get("Origin")

    if origin:

        resp.headers["Access-Control-Allow-Origin"] = origin

        resp.headers["Vary"] = "Origin"

    if request.method == "OPTIONS":

        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"

        resp.headers["Access-Control-Allow-Headers"] = (

            "Content-Type, Authorization, X-Requested-With"

        )

        resp.headers["Access-Control-Max-Age"] = "3600"

    return resp


# =========================================================

# =========================================================
# HTTP 请求日志：Flask 和 Waitress 都输出到启动终端
# =========================================================
@app.before_request
def print_request_start():
    request.environ["_app_start_perf"] = time.perf_counter()
    print(f"[HTTP >>] {request.remote_addr or '-'} "
          f"{request.method} {request.full_path.rstrip('?')}", flush=True)


@app.after_request
def print_request_finish(response):
    started = request.environ.get("_app_start_perf", time.perf_counter())
    elapsed_ms = (time.perf_counter() - started) * 1000
    print(f"[HTTP <<] {request.method} {request.path} "
          f"{response.status_code} {elapsed_ms:.1f}ms", flush=True)
    return response


# 2. 查询条件构造

# =========================================================

def apply_size_filter(query, size_param):

    """文件大小过滤。支持 "lte:100" / "gte:500" / "eq:200"，兼容旧的纯数字。"""

    if ":" in size_param:

        operator, value = size_param.split(":", 1)

        size_bytes = int(value) * 1024 * 1024

        if operator == "lte":

            return query.filter(Video.file_size <= size_bytes)

        if operator == "gte":

            return query.filter(Video.file_size >= size_bytes)

        if operator == "eq":

            margin = 10 * 1024 * 1024

            return query.filter(

                Video.file_size.between(size_bytes - margin, size_bytes + margin)

            )

        return query

    return query.filter(Video.file_size <= int(size_param) * 1024 * 1024)


def apply_video_filters(query, request_args):

    """score / tags / search / size / watched"""

    score   = request_args.get("score")

    search  = request_args.get("search")

    tags    = request_args.get("tags")

    size    = request_args.get("size")

    watched = request_args.get("watched")


    if score and int(score) > 0:

        query = query.filter(Video.score == int(score))


    if tags:

        for tag in tags.split(","):

            query = query.filter(Video.tags.like(f"%{tag}%"))


    if search:

        for kw in normalize_text(search).strip().split():

            query = query.filter(

                or_(

                    Video.filename_norm.like(f"%{kw}%"),

                    Video.detail.like(f"%{kw}%"),

                )

            )


    if size:

        query = apply_size_filter(query, size)


    if watched:

        query = apply_watched_filter(query, watched)


    return query


@app.errorhandler(ValueError)

def invalid_parameter(e):

    return jsonify({"success": False, "error": "请求参数格式不正确"}), 400


# =========================================================

# 2.5 后台任务系统（索引进度）

# =========================================================

@dataclass

class TaskInfo:

    task_id: str

    path: str

    status: str = "pending"          # pending|running|done|error

    total: int = 0

    current: int = 0

    message: str = ""

    started_at: float = 0.0

    finished_at: float = 0.0

    error: str = ""

    extra: dict = field(default_factory=dict)


    def to_dict(self) -> dict:

        end = self.finished_at or time.time()

        started = self.started_at or end

        elapsed = max(0.0, end - started)

        percent = (self.current / self.total * 100.0) if self.total else 0.0

        if self.status == "done":

            percent = 100.0

        eta = None

        if self.status == "running" and self.current > 0 and self.total > self.current:

            rate = self.current / elapsed if elapsed > 0 else 0

            if rate > 0:

                eta = round((self.total - self.current) / rate, 1)

        return {

            "task_id": self.task_id,

            "path": self.path,

            "status": self.status,

            "total": self.total,

            "current": self.current,

            "percent": round(percent, 1),

            "message": self.message,

            "error": self.error,

            "elapsed_seconds": round(elapsed, 1),

            "eta_seconds": eta,

            "started_at": self.started_at,

            "finished_at": self.finished_at,

            "extra": self.extra,

        }


_tasks: dict[str, TaskInfo] = {}

_tasks_lock = threading.Lock()


_TASK_TTL = 30 * 60

_TASK_MAX = 200


def _prune_tasks():

    now = time.time()

    with _tasks_lock:

        dead = [

            tid for tid, t in _tasks.items()

            if t.status in ("done", "error")

            and t.finished_at

            and (now - t.finished_at) > _TASK_TTL

        ]

        for tid in dead:

            _tasks.pop(tid, None)


        if len(_tasks) > _TASK_MAX:

            finished = sorted(

                (t for t in _tasks.values() if t.status in ("done", "error")),

                key=lambda x: x.started_at,

            )

            for t in finished[: len(_tasks) - _TASK_MAX]:

                _tasks.pop(t.task_id, None)


def _new_task(path: str) -> TaskInfo:

    t = TaskInfo(task_id=uuid.uuid4().hex, path=path)

    with _tasks_lock:

        _tasks[t.task_id] = t

    _prune_tasks()

    return t


def _find_running_task(path: str) -> Optional[TaskInfo]:

    with _tasks_lock:

        for t in _tasks.values():

            if t.path == path and t.status in ("pending", "running"):

                return t

    return None


def _bump(t: TaskInfo, current: int, total: int = 0, message: str = ""):

    t.current = current

    if total:

        t.total = total

    if message:

        t.message = message


VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",

              ".m4v", ".ts", ".rmvb", ".mpg", ".mpeg")


def _count_videos(folder: str) -> int:

    total = 0

    for _, _, files in os.walk(folder):

        for f in files:

            if f.lower().endswith(VIDEO_EXTS):

                total += 1

    return total


def _call_init_data_from_folder(folder: str, progress_cb):

    init_data_from_folder(folder, progress_cb=progress_cb)


def _run_index_task(t: TaskInfo):

    t.status = "running"

    t.started_at = time.time()


    try:

        if not os.path.isdir(t.path):

            raise FileNotFoundError(f"路径不存在: {t.path}")


        t.message = "正在扫描视频文件..."

        t.extra["phase"] = "scan"


        try:

            run_save_star()

            t.message = "评分记录已同步，开始入库..."

        except Exception as e:

            print(f"[index-task] run_save_star 失败: {e}")


        t.extra["phase"] = "index"


        def progress_cb(done: int, total_inner: int, message: str = ""):

            _bump(t, done, total_inner,

                  message or f"入库 {done}/{total_inner}")


        _call_init_data_from_folder(t.path, progress_cb)


        if t.total:

            t.current = t.total

        t.status = "done"

        t.message = f"索引完成，共处理 {t.total} 项"

        t.extra["phase"] = "done"


    except Exception as e:

        t.status = "error"

        t.error = repr(e)

        t.message = f"失败：{e}"

    finally:

        t.finished_at = time.time()


# =========================================================

# 3. 首页

# =========================================================

@app.route("/")

def index():
    # 【拆分版】注入 window.LOCALDY（api / client / download / split），
    # client 地址按访问者子网选择，见 split_api.render_index。
    return render_index()


# =========================================================

# 4. 路径配置

# =========================================================

@app.route("/video-paths", methods=["GET"])

def get_video_paths():
    # 【拆分版】只读展示：路径归客户端管理，服务端只展示客户端上报的
    # 根目录（心跳上报；未上报过则回退到随库复制的 video_paths.json）。
    return jsonify({"paths": client_reported_roots(), "read_only": True,
                    "managed_by": "client"})


@app.route("/video-paths", methods=["POST"])

def add_video_path():
    # 【拆分版】410：路径配置归客户端管理（全局模式无需输入路径，
    # 加新文件夹用命令行 --root）。服务端只读展示，见 GET /video-paths。
    return jsonify({
        "success": False,
        "error": "路径由客户端管理：全局模式无需输入路径，"
                 "加新文件夹请用命令行 localdy_client.py --root <新路径>",
        "managed_by": "client",
    }), 410


@app.route("/video-paths/index", methods=["POST"])

def index_video_path():
    """【拆分版】410：索引由客户端扫描后推 /api/index-diff，服务端不再扫盘。"""
    return jsonify({
        "success": False,
        "error": "索引由客户端执行：请在客户端上扫描，差异会自动推给服务端",
        "managed_by": "client",
    }), 410


@app.route("/video-paths/index/async", methods=["POST"])

def index_video_path_async():
    """【拆分版】410：索引由客户端扫描后推 /api/index-diff，服务端不再扫盘。"""
    return jsonify({
        "success": False,
        "error": "索引由客户端执行：请在客户端上扫描，差异会自动推给服务端",
        "managed_by": "client",
    }), 410


@app.route("/tasks/<task_id>", methods=["GET"])

def get_task(task_id):

    with _tasks_lock:

        t = _tasks.get(task_id)

    if not t:

        return jsonify({"error": "任务不存在或已过期"}), 404

    return jsonify(t.to_dict())


@app.route("/tasks", methods=["GET"])

def list_tasks():

    with _tasks_lock:

        items = [t.to_dict() for t in _tasks.values()]

    items.sort(key=lambda x: x["started_at"], reverse=True)

    return jsonify({"total": len(items), "items": items})


@app.route("/tasks/<task_id>/stream", methods=["GET"])

def stream_task(task_id):

    with _tasks_lock:

        t = _tasks.get(task_id)

    if not t:

        abort(404)


    def gen():

        last_snapshot = None

        while True:

            payload = t.to_dict()

            snapshot = (

                payload["status"], payload["current"],

                payload["total"], payload["message"],

            )

            if snapshot != last_snapshot:

                last_snapshot = snapshot

                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


            if t.status in ("done", "error"):

                break

            time.sleep(0.5)


    return Response(

        stream_with_context(gen()),

        mimetype="text/event-stream",

        headers={

            "Cache-Control": "no-cache",

            "X-Accel-Buffering": "no",

            "Connection": "keep-alive",

        },

    )


# =========================================================

# 5. 视频读取

# =========================================================

@app.route("/videos", methods=["GET"])

def get_videos():

    session = Session()

    try:

        query = session.query(Video)


        page      = int(request.args.get("page", 1))

        page_size = int(request.args.get("page_size", 5))

        latest    = request.args.get("latest")

        stream    = request.args.get("stream", "false").lower() == "true"

        if page < 1 or page_size < 0 or page_size > 200:

            return jsonify({"error": "页码必须大于 0，每页数量应为 0–200"}), 400


        query = apply_video_filters(query, request.args)

        query = query.order_by(Video.filename_norm.desc())


        if page_size:

            query = query.offset((page - 1) * page_size).limit(page_size)

        else:

            query = query.limit(max(1, min(200, int(latest or 5))))


        rows = query.all()
        # 【拆分版】一次查出 file_hash，供前端拼客户端媒体地址；
        # 客户端按 hash 认文件，不知道视频 id。
        _hash_map = {}
        if rows:
            _names = [v.filename for v in rows if v.filename]
            if _names:
                _ps = PlaySession()
                try:
                    for _fh in _ps.query(FileHash).filter(
                            FileHash.filename.in_(_names)).all():
                        _hash_map[_fh.filename] = _fh.file_hash
                finally:
                    _ps.close()
        videos = [
            {
                "id": v.id,
                "filename": v.filename,
                "tags": v.tags,
                "score": v.score,
                "detail": v.detail,
                "file_size": v.file_size or 0,
                "file_hash": _hash_map.get(v.filename),
            }
            for v in rows
        ]

    finally:

        session.close()


    if not stream:

        return jsonify(videos)


    def generate():

        yield "["

        for i, v in enumerate(videos):

            if i > 0:

                yield ","

            yield json.dumps(v, ensure_ascii=False)

        yield "]"


    return Response(stream_with_context(generate()), mimetype="application/json")


@app.route("/videos/count", methods=["GET"])

def get_videos_count():

    session = Session()

    try:

        query = apply_video_filters(session.query(Video), request.args)


        key = request.args.get("key") or request.args.get("Key")

        if key:

            key_norm = normalize_text(key)

            if key_norm:

                query = query.filter(Video.filename_norm.contains(key_norm, autoescape=True))


        total = query.count()

    finally:

        session.close()


    print(f"Total videos count: {total}, key={key}")

    return jsonify({"total": total, "key": key})


@app.route("/videos/<int:video_id>", methods=["GET"])

def get_video_detail(video_id):

    session = Session()

    try:

        video = session.query(Video).filter(Video.id == video_id).first()

        if not video:

            return jsonify({"error": "视频不存在"}), 404

        return jsonify({
            "id": video.id,
            "filename": video.filename,
            "tags": video.tags,
            "score": video.score,
            "detail": video.detail,
            "file_size": video.file_size or 0,
            "file_hash": _hash_for_filename(video.filename),
        })

    finally:

        session.close()


@app.route("/videos/<int:video_id>/stream", methods=["GET"])

def stream_video_file(video_id):
    # 【拆分版】410：视频流改由客户端提供（/media/<hash>），服务端不再送文件。
    return jsonify({
        "success": False,
        "error": "视频流已由客户端提供，请确认客户端已运行，"
                 "客户端下载：/download/localdy_client.py",
        "client_media": "GET {client}/media/<file_hash>?ticket=...",
    }), 410


# =========================================================

# 5.5 播放记录 / 评分 / 备注

# =========================================================

@app.route("/videos/<int:video_id>/play", methods=["POST"])

def mark_played(video_id):

    session = Session()

    try:

        video = session.query(Video).filter(Video.id == video_id).first()

        if not video:

            return jsonify({"success": False, "error": "视频不存在"}), 404

        filename, file_path = video.filename, video.detail

    finally:

        session.close()


    rec_id = record_play(video_id, filename, file_path)

    return jsonify({"success": rec_id is not None, "record_id": rec_id})


@app.route("/play-records", methods=["GET"])

def list_play_records():

    from core import PlayRecord, PlaySession


    s = PlaySession()

    try:

        q = s.query(PlayRecord)

        fn = request.args.get("filename")

        if fn:

            q = q.filter(PlayRecord.filename.like(f"%{fn}%"))


        page = int(request.args.get("page", 1))

        size = int(request.args.get("page_size", 50))


        total = q.count()

        rows = (q.order_by(PlayRecord.accessed_at.desc())

                 .offset((page - 1) * size).limit(size).all())

        return jsonify({

            "total": total,

            "page": page,

            "page_size": size,

            "items": [r.to_dict() for r in rows],

        })

    finally:

        s.close()


# ---------------- 评分镜像（记录具体分值 1~5） ----------------

@app.route("/video-scores", methods=["GET"])

def get_video_scores():

    """分页查询评分记录。


    Query:

        page, page_size      分页

        keyword              按文件名模糊匹配

        score                指定分值（1~5）；省略 → 全部有评分的记录

    """

    page = int(request.args.get("page", 1))

    size = int(request.args.get("page_size", 100))

    kw   = request.args.get("keyword")

    score_param = request.args.get("score")

    score = int(score_param) if score_param not in (None, "", "0") else None


    total, items = list_video_scores(page, size, kw, score=score)

    return jsonify({

        "total": total,

        "page": page,

        "page_size": size,

        "score": score,

        "items": items,

    })


@app.route("/video-scores/stats", methods=["GET"])

def get_video_score_stats():

    """各分档数量统计，例如 {1: 12, 2: 8, 3: 30, 4: 25, 5: 40}。"""

    stats = count_video_scores_by_score()

    # 补齐 1~5，方便前端直接渲染

    for s in (1, 2, 3, 4, 5):

        stats.setdefault(s, 0)

    total = sum(stats.values())

    return jsonify({"total": total, "by_score": stats})


# ---- 向后兼容：老前端还调 /five-star ----

@app.route("/five-star", methods=["GET"])

def get_five_star_compat():

    """兼容旧接口，固定返回 score=5。"""

    page = int(request.args.get("page", 1))

    size = int(request.args.get("page_size", 100))

    kw   = request.args.get("keyword")

    total, items = list_video_scores(page, size, kw, score=5)

    return jsonify({

        "total": total,

        "page": page,

        "page_size": size,

        "items": items,

    })


@app.route("/videos/<int:video_id>/note", methods=["GET"])

def get_video_note(video_id):

    session = Session()

    try:

        v = session.query(Video).filter(Video.id == video_id).first()

        if not v:

            return jsonify({"success": False, "error": "视频不存在"}), 404

        filename, file_path = v.filename, v.detail

    finally:

        session.close()


    info = get_note(video_id, filename, file_path)

    return jsonify({

        "success": True,

        "note": (info or {}).get("note", ""),

        "info": info,

    })


@app.route("/videos/<int:video_id>/note", methods=["POST", "PUT"])

def save_video_note(video_id):

    data = request.get_json(silent=True) or {}

    note = data.get("note", "")


    session = Session()

    try:

        v = session.query(Video).filter(Video.id == video_id).first()

        if not v:

            return jsonify({"success": False, "error": "视频不存在"}), 404

        filename, file_path = v.filename, v.detail

    finally:

        session.close()


    info = save_note(video_id, filename, file_path, note)

    return jsonify({

        "success": True,

        "cleared": info is None,

        "info": info,

    })


# =========================================================

# 5.6 备注标签 → 文件名

# =========================================================

@app.route("/notes/pending-tags", methods=["GET"])

def get_pending_note_tags():

    """列出所有备注中出现的 #tag，及涉及视频（预览前 20 个）。"""

    data = list_pending_note_tags()

    items = [

        {"tag": tag, "count": len(vids), "videos": vids[:20]}

        for tag, vids in data.items()

    ]

    items.sort(key=lambda x: (-x["count"], x["tag"]))

    return jsonify({"total": len(items), "items": items})


@app.route("/notes/history-tags", methods=["GET"])

def get_history_tags():

    from core import PlaySession, VideoNote, extract_note_tags


    tags = []

    ps = PlaySession()

    session = Session()

    try:

        notes = ps.query(VideoNote).order_by(VideoNote.updated_at.desc()).limit(500).all()

        for note in notes:

            tags.extend(extract_note_tags(note.note or ""))

        for (filename,) in session.query(Video.filename).limit(1000):

            tags.extend(extract_tags(filename or ""))

        return jsonify({"tags": list(dict.fromkeys(tags))[:60]})

    finally:

        ps.close()

        session.close()


@app.route("/notes/apply-tags", methods=["POST"])

def apply_note_tags_endpoint():
    """【拆分版】改名要动文件，改成记一笔 file-job，等客户端执行并回报。

    服务端先把意图展开成具体文件再下发
    （客户端没有数据库，无法把 tag 解析成文件）。
    Body（可选）：{"tags": ["少妇"], "dry_run": false}
    dry_run=true 时只返回计划，不创建任务、不改名。
    返回：{success, queued, job_id}；进度查 GET /api/file-jobs/<job_id>。
    """
    data = request.get_json(silent=True) or {}
    tags = data.get("tags")
    dry_run = bool(data.get("dry_run"))
    if tags is not None and not isinstance(tags, list):
        return jsonify({"error": "tags 必须是数组"}), 400
    items, meta = expand_job_items("apply-tags", {"tags": tags})
    if dry_run:
        return jsonify({"success": True, "dry_run": True,
                        "plan": {"items": items, **meta},
                        "message": "试算完成，未创建任务、未改名。"})
    job_id = create_expanded_job("apply-tags", {"tags": tags}, items)
    return jsonify({
        "success": True,
        "queued": True,
        "job_id": job_id,
        "type": "apply-tags",
        "dry_run": dry_run,
        "files": len(items),
        "message": "已记为文件任务，等客户端执行（服务端不直接改名）。"
                   "进度查 GET /api/file-jobs/<job_id>。",
    })


# =========================================================

# 6. 视频写入

# =========================================================

@app.route("/videos", methods=["POST"])

def add_video():

    data = request.get_json(silent=True) or {}

    filename = data.get("filename")

    score = int(data.get("score", 0))

    detail = data.get("detail", "")

    tags = extract_tags(filename)


    session = Session()

    try:

        video = Video(

            filename=filename,

            score=score,

            detail=detail,

            tags=",".join(tags),

        )

        session.add(video)

        session.commit()

        vid = video.id

    finally:

        session.close()


    return jsonify({

        "id": vid,

        "filename": filename,

        "tags": ",".join(tags),

        "score": score,

        "detail": detail,

    })


@app.route("/videos/update_score", methods=["POST"])

def update_score():

    data = request.get_json(silent=True) or {}

    video_id = data.get("id")

    score = data.get("score")

    if (isinstance(video_id, bool) or not isinstance(video_id, int)

            or isinstance(score, bool) or not isinstance(score, int)

            or not 0 <= score <= 5):

        return jsonify({"success": False, "error": "id 必须为整数，评分必须为 0–5 的整数"}), 400


    session = Session()

    try:

        video = session.query(Video).filter(Video.id == video_id).first()

        if not video:

            return jsonify({"success": False, "msg": "视频不存在"}), 404

        video.score = score

        session.commit()

        filename, file_path = video.filename, video.detail

    finally:

        session.close()


    # 任意分值都同步到 video_scores（0 表示取消评分 → 删除记录）

    try:

        sync_video_score(video_id, filename, file_path, int(score))

    except Exception as e:

        print(f"[video-scores] 同步失败: {e}")


    return jsonify({"success": True, "score": score})


@app.route("/videos/<int:video_id>/delete", methods=["POST", "DELETE"])

def remove_video(video_id):

    return jsonify({"success": delete_video(video_id)})


# =========================================================

# 7. 统一错误处理

# =========================================================

def _wants_json() -> bool:

    return request.path.startswith(("/videos", "/video-paths",
                                    "/play-records", "/five-star",
                                    "/video-scores", "/notes", "/tasks",
                                    "/api", "/download"))


@app.errorhandler(404)

def not_found(e):

    if _wants_json():

        return jsonify({"success": False, "error": "not found", "path": request.path}), 404

    return e, 404


@app.errorhandler(500)

def server_error(e):

    if _wants_json():

        return jsonify({"success": False, "error": "internal server error"}), 500

    return e, 500


# =========================================================

# 8. 初始化与启动

# =========================================================

# =========================================================
# 7.5 收集：预览 + 硬链接/复制 + 实际文件数/大小目录名（Flask 原生路由）
# =========================================================
import math
import os
import stat as statmod
import threading
from datetime import datetime
from pathlib import Path

from flask import jsonify, request


# ==================== 可修改的配置 ====================
# 【拆分版】NAS 目标根目录不再由服务端使用：收集改走 file-job，
# dest_root 由客户端在执行时决定；这里置 None 仅保留预览接口的
# 纯字符串比对（is_under 对 None 返回 False，不访问磁盘）。
COLLECT_DEST_ROOT = None
COLLECT_DEFAULT_MAX_GB = None   # None / 0 = 不限制；如 20 = 默认 20 GiB
COLLECT_REQUIRE_INDEXED_ROOT = False  # False 直接使用数据库结果；True 再按 /video-paths 限制来源
# ========================================================

_GIB = 1024 ** 3
_collect_lock = threading.RLock()


def register_collect_routes(app, Session, Video, normalize_text, load_paths):
    """两个 Flask 路由。预览只查询 SQL，不访问任何视频文件。"""

    def error(message, status=400):
        return jsonify({"success": False, "error": message}), status

    def valid_key(raw):
        if not isinstance(raw, str):
            raise ValueError("关键词必须是字符串")
        key = raw.strip()
        if not key or len(key) > 120:
            raise ValueError("关键词不能为空，且不能超过120个字符")
        if (key in (".", "..") or key.endswith((".", " "))
                or any(ch in key for ch in '\\/:*?"<>|')
                or any(ord(ch) < 32 for ch in key)):
            raise ValueError("关键词不能包含路径分隔符或非法文件名字符")
        reserved = {"CON", "PRN", "AUX", "NUL"}
        reserved.update(f"COM{i}" for i in range(1, 10))
        reserved.update(f"LPT{i}" for i in range(1, 10))
        if key.split(".", 1)[0].upper() in reserved:
            raise ValueError("关键词不能是 Windows 保留文件名")
        return key

    def limit_bytes(raw):
        gb = COLLECT_DEFAULT_MAX_GB if raw is None else raw
        if gb is None:
            return None
        if isinstance(gb, bool):
            raise ValueError("max_total_gb 必须是非负数字")
        try:
            gb = float(gb)
        except (ValueError, TypeError):
            raise ValueError("max_total_gb 必须是非负数字")
        if not math.isfinite(gb) or gb < 0:
            raise ValueError("max_total_gb 必须是非负有限数字")
        return None if gb == 0 else int(gb * _GIB)

    def norm_path(path):
        """纯字符串路径归一化，不使用 realpath，不访问 SMB/NAS。"""
        return os.path.normcase(os.path.abspath(os.path.normpath(path)))

    def is_under(path, folder):
        """只做路径字符串比较；真正的磁盘检查延后到创建硬链接时。"""
        try:
            p, root = norm_path(path), norm_path(folder)
            return os.path.commonpath((p, root)) == root
        except (ValueError, OSError, TypeError):
            return False

    def find_videos(key):
        """仅 SELECT filename/detail/file_size，不调用 stat、exists、realpath。"""
        key_norm = normalize_text(key)
        if not key_norm:
            return []

        roots = []
        if COLLECT_REQUIRE_INDEXED_ROOT:
            roots = [norm_path(p) for p in (load_paths() or []) if p]
            if not roots:
                raise RuntimeError("尚未配置视频索引目录（/video-paths）")

        session = Session()
        try:
            # 同 /videos/count 的字面字符串匹配规则。
            # 直接读取数据库保存的文件名、绝对路径和字节数。
            records = (session.query(Video.filename, Video.detail, Video.file_size)
                       .filter(Video.filename_norm.contains(key_norm, autoescape=True))
                       .order_by(Video.id.asc())
                       .all())
        finally:
            session.close()

        results, seen = [], set()
        for filename, path, size in records:
            if not path:
                continue
            lexical = norm_path(path)
            if is_under(lexical, COLLECT_DEST_ROOT):
                continue
            if COLLECT_REQUIRE_INDEXED_ROOT and not any(is_under(lexical, root) for root in roots):
                continue
            if lexical in seen:
                continue
            seen.add(lexical)
            results.append((filename or ntpath.basename(path), path, int(size or 0)))
        return results

    def is_linked_dir(folder):
        """拒绝将关键词目录识别为符号链接或 Windows junction。"""
        return folder.is_symlink() or (hasattr(folder, "is_junction") and folder.is_junction())

    def is_summary_name(key, folder_name):
        """兼容旧目录 key 和新目录 key_数量个_体积。"""
        if folder_name == key:
            return True
        prefix = f"{key}_"
        if not folder_name.startswith(prefix):
            return False
        suffix = folder_name[len(prefix):]
        import re
        return re.fullmatch(r"[0-9]+个_[0-9]+(?:\.[0-9]{1,2})?(?:B|KB|MB|GB|TB)", suffix) is not None

    def safe_target_dir(key):
        """找到当天该关键词已收集的目录，避免目录名变化后重复收集。"""
        base = Path(COLLECT_DEST_ROOT)
        day_dir = base / datetime.now().strftime("%y%m%d")
        if is_linked_dir(base) or is_linked_dir(day_dir):
            raise OSError(f"目标根目录/日期目录不允许是链接：{day_dir}")
        candidates = []
        if day_dir.is_dir():
            with os.scandir(day_dir) as iterator:
                for item in iterator:
                    if not is_summary_name(key, item.name):
                        continue
                    existing = day_dir / item.name
                    if is_linked_dir(existing):
                        raise OSError(f"已存在的收集目录是链接：{existing}")
                    if item.is_dir(follow_symlinks=False):
                        candidates.append(existing)
        if len(candidates) > 1:
            raise OSError(f"发现多个同一关键词的收集目录，请先手动合并：{', '.join(str(x) for x in candidates)}")
        return candidates[0] if candidates else day_dir / key

    def human_folder_size(size_bytes):
        """目录名使用 1024 进制的紧凑显示值，精确字节数另在 JSON 返回。"""
        if size_bytes < 1024:
            return f"{size_bytes}B"
        for unit, divisor in (("TB", 1024 ** 4), ("GB", 1024 ** 3),
                              ("MB", 1024 ** 2), ("KB", 1024)):
            if size_bytes >= divisor:
                value = f"{size_bytes / divisor:.2f}".rstrip("0").rstrip(".")
                return f"{value}{unit}"
        return "0B"

    def stat_collected_folder(folder):
        """只统计目标目录里实际存在的普通文件，不计候选数、失败数或索引文件。"""
        actual_count = actual_bytes = 0
        with os.scandir(folder) as iterator:
            for item in iterator:
                info = item.stat(follow_symlinks=False)
                if statmod.S_ISREG(info.st_mode):
                    actual_count += 1
                    actual_bytes += info.st_size
        return actual_count, actual_bytes

    def finalize_target_dir(folder, key):
        """收集完毕后改名，重复操作复用同一个目录。"""
        actual_count, actual_bytes = stat_collected_folder(folder)
        new_name = f"{key}_{actual_count}个_{human_folder_size(actual_bytes)}"
        new_folder = folder.parent / new_name
        if folder != new_folder:
            if os.path.lexists(new_folder):
                raise FileExistsError(f"目标汇总目录已存在，不覆盖：{new_folder}")
            folder.rename(new_folder)
        return new_folder, actual_count, actual_bytes

    def pick_target(source, folder):
        """已链接则跳过，文件名冲突时附加序号。"""
        name = Path(source).name
        stem, ext = os.path.splitext(name)
        for n in range(1, 100002):
            candidate = folder / (name if n == 1 else f"{stem}_{n}{ext}")
            if not os.path.lexists(candidate):
                return candidate, False
            try:
                if os.path.samefile(source, candidate):
                    return candidate, True
            except OSError:
                pass
        raise OSError("同名文件冲突数量过多")

    @app.route("/videos/collect/preview", methods=["GET"])
    def collect_preview():
        try:
            key = valid_key(request.args.get("key"))
            entries = find_videos(key)
        except ValueError as exc:
            return error(str(exc), 400)
        except RuntimeError as exc:
            return error(str(exc), 503)
        except Exception:
            app.logger.exception("collect preview failed")
            return error("数据库查询失败，请查看服务端日志", 500)

        # 直接返回数据库存储的大小。不访问磁盘，也不验证文件是否存在。
        files = [{"name": name, "size_bytes": size} for name, _, size in entries]
        return jsonify({
            "success": True,
            "key": key,
            "count": len(files),
            "total_size_bytes": sum(size for _, _, size in entries),
            "default_max_total_gb": COLLECT_DEFAULT_MAX_GB,
            "size_source": "database",
            "files": files,
        })

    @app.route("/videos/collect", methods=["POST"])
    def collect_links():
        """优先硬链接；失败复制。同一磁盘硬链接失败显式报警。"""
        import errno
        import json
        import ntpath
        import shutil
        import tempfile

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return error("请求体必须是 JSON 对象")
        try:
            key = valid_key(body.get("key"))
            ceiling = limit_bytes(body.get("max_total_gb"))
            target_dir = safe_target_dir(key)
        except (ValueError, OSError) as exc:
            return error(str(exc))

        def log_event(message):
            print(f"[COLLECT] {message}", flush=True)

        def same_disk(source_path, source_stat, dest_stat):
            """st_dev 优先；Windows 同盘符/同 SMB share 作为补充判断。"""
            src_dev = getattr(source_stat, "st_dev", None)
            dst_dev = getattr(dest_stat, "st_dev", None)
            if src_dev is not None and dst_dev is not None and src_dev == dst_dev and src_dev != 0:
                return True, "st_dev相同"
            if os.name == "nt":
                src_drive = ntpath.normcase(ntpath.splitdrive(source_path)[0])
                dst_drive = ntpath.normcase(ntpath.splitdrive(str(target_dir))[0])
                if src_drive and src_drive == dst_drive:
                    return True, "同盘符或同SMB共享"
            return False, "不同文件系统或无法确认"

        def copy_exclusive(source_path, dest_path, expected_size):
            """独占创建，拷贝异常立即清理半成品，不覆盖任何已存在文件。"""
            created = False
            try:
                with open(source_path, "rb") as input_file:
                    with open(dest_path, "xb") as output_file:
                        created = True
                        shutil.copyfileobj(input_file, output_file, length=8 * 1024 * 1024)
                actual_size = os.stat(dest_path).st_size
                if actual_size != expected_size:
                    raise OSError(f"复制后大小不一致：原文件 {expected_size} 字节，副本 {actual_size} 字节")
                # 复制元数据失败不影响已完成的文件内容。
                try:
                    shutil.copystat(source_path, dest_path)
                except OSError as metadata_exc:
                    log_event(f"[WARN] 复制成功但无法同步时间属性：{dest_path.name}：{metadata_exc}")
            except Exception:
                if created:
                    try:
                        os.unlink(dest_path)
                    except OSError as cleanup_exc:
                        log_event(f"[ERROR] 清理不完整副本失败：{dest_path}：{cleanup_exc}")
                raise

        # 复制文件不具备硬链接的 inode 标识，使用独立清单防止重复点击复制多份。
        # 清单放在日期目录下，不混入 view/日期/关键词 的视频文件列表。
        def read_copy_index(day_dir):
            index_path = day_dir / ".collect-copies.json"
            try:
                with open(index_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, dict):
                    return index_path, payload
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError) as exc:
                log_event(f"[WARN] 读取复制去重清单失败：{exc}")
            return index_path, {}

        def write_copy_index(index_path, payload):
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=str(index_path.parent),
                    prefix=".collect-copies-", suffix=".tmp", delete=False
                ) as f:
                    temp_path = f.name
                    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
                os.replace(temp_path, index_path)
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)

        with _collect_lock:
            try:
                entries = find_videos(key)
            except RuntimeError as exc:
                return error(str(exc), 503)
            except Exception:
                app.logger.exception("collect search failed")
                return error("数据库查询失败，请查看服务端日志", 500)

            linked, copied, skipped, failed, warnings = [], [], [], [], []
            linked_bytes = copied_bytes = 0
            same_disk_hardlink_failed_count = 0
            if entries:
                try:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    if target_dir.is_symlink() or not target_dir.is_dir():
                        raise OSError("目标目录不可用")
                    dest_stat = os.stat(target_dir)
                except OSError as exc:
                    return error(f"无法创建目标目录：{exc}", 500)
            else:
                dest_stat = None

            copy_index_dirty = False
            copy_index_path = None
            copy_index = {}
            if entries:
                copy_index_path, copy_index = read_copy_index(target_dir.parent)
                if not isinstance(copy_index.get(key), dict):
                    copy_index[key] = {}

            roots = [norm_path(p) for p in (load_paths() or []) if p] if COLLECT_REQUIRE_INDEXED_ROOT else []
            for filename, source, db_size in entries:
                try:
                    if is_under(source, COLLECT_DEST_ROOT):
                        skipped.append({"name": filename, "reason": "源文件位于收集目录"})
                        continue
                    if COLLECT_REQUIRE_INDEXED_ROOT and not any(is_under(source, r) for r in roots):
                        skipped.append({"name": filename, "reason": "源文件不在索引目录内"})
                        continue

                    if os.path.islink(source):
                        skipped.append({"name": filename, "reason": "源文件是符号链接"})
                        continue
                    info = os.stat(source)
                    if not statmod.S_ISREG(info.st_mode):
                        skipped.append({"name": filename, "reason": "不是普通文件"})
                        continue
                    size = info.st_size

                    # 复制回退的历史记录：大小、修改时间一致时跳过重复拷贝。
                    source_id = norm_path(source)
                    record = copy_index[key].get(source_id)
                    if isinstance(record, dict):
                        previous_name = record.get("name")
                        if isinstance(previous_name, str) and previous_name == os.path.basename(previous_name):
                            existing = target_dir / previous_name
                            if (existing.is_file() and not existing.is_symlink()
                                    and os.stat(existing).st_size == size
                                    and record.get("size") == size
                                    and record.get("mtime_ns") == info.st_mtime_ns):
                                skipped.append({"name": filename, "reason": "已经复制收集"})
                                continue

                    candidate, already_linked = pick_target(source, target_dir)
                    if already_linked:
                        skipped.append({"name": filename, "reason": "已经硬链接收集"})
                        continue

                    if ceiling is not None and linked_bytes + copied_bytes + size > ceiling:
                        skipped.append({"name": filename, "reason": "超过总大小限制", "size_bytes": size})
                        continue

                    try:
                        os.link(source, candidate)
                    except FileExistsError:
                        skipped.append({"name": filename, "reason": "目标文件已存在"})
                        continue
                    except OSError as link_exc:
                        # 硬链接失败：优先警告后再复制；同一磁盘失败为异常状况。
                        on_same_disk, proof = same_disk(source, info, dest_stat)
                        if on_same_disk:
                            same_disk_hardlink_failed_count += 1
                            message = (
                                f"同一磁盘/文件系统硬链接失败（{proof}），已尝试复制："
                                f"{filename}；原因：{link_exc}"
                            )
                            warnings.append({"name": filename, "type": "same_disk_hardlink_failed",
                                             "message": message, "hardlink_error": str(link_exc)})
                            log_event(f"[WARNING][SAME-DISK-HARDLINK-FAILED] {message}")
                        else:
                            log_event(f"[FALLBACK] 无法硬链接，改为复制：{filename}；原因：{link_exc}")

                        try:
                            copy_exclusive(source, candidate, size)
                        except FileExistsError:
                            skipped.append({"name": filename, "reason": "目标文件已存在"})
                            continue
                        except OSError as copy_exc:
                            failed.append({"name": filename, "reason": f"硬链接失败：{link_exc}；复制也失败：{copy_exc}"})
                            log_event(f"[ERROR] {filename}：硬链接及复制均失败，{copy_exc}")
                            continue

                        copied_bytes += size
                        copied.append({"name": candidate.name, "size_bytes": size,
                                       "hardlink_error": str(link_exc),
                                       "same_disk_hardlink_failed": on_same_disk})
                        copy_index[key][source_id] = {
                            "name": candidate.name, "size": size, "mtime_ns": info.st_mtime_ns
                        }
                        copy_index_dirty = True
                    else:
                        linked_bytes += size
                        linked.append({"name": candidate.name, "size_bytes": size})
                except FileNotFoundError:
                    skipped.append({"name": filename, "reason": "源文件不存在"})
                except OSError as exc:
                    failed.append({"name": filename, "reason": str(exc)})
                    log_event(f"[ERROR] {filename}：{exc}")

            if copy_index_dirty:
                try:
                    write_copy_index(copy_index_path, copy_index)
                except OSError as exc:
                    message = f"复制已完成，但保存去重清单失败；重复收集可能重复复制：{exc}"
                    warnings.append({"type": "copy_index_write_failed", "message": message})
                    log_event(f"[WARNING] {message}")

            # 汇总目录名 = 关键词_目标目录实际文件数量个_目标目录实际文件大小。
            # 注意：这里统计目录内的全部现有文件，不只是本次新增的文件。
            folder_file_count = 0
            folder_total_size_bytes = 0
            if target_dir.is_dir():
                try:
                    target_dir, folder_file_count, folder_total_size_bytes = finalize_target_dir(target_dir, key)
                    log_event(f"[DIR] {target_dir.name} | 文件数={folder_file_count} "
                              f"总大小={human_folder_size(folder_total_size_bytes)} "
                              f"({folder_total_size_bytes} bytes)")
                except OSError as exc:
                    message = f"文件已收集，但统计或重命名目标目录失败：{exc}"
                    warnings.append({"type": "folder_summary_failed", "message": message})
                    log_event(f"[WARNING] {message}")
                    # 统计失败时不声称目录中有0个文件。
                    folder_file_count = None
                    folder_total_size_bytes = None

        collected_bytes = linked_bytes + copied_bytes
        log_event(f"[DONE] key={key!r} matched={len(entries)} hardlinks={len(linked)} "
                  f"copies={len(copied)} skipped={len(skipped)} failed={len(failed)} "
                  f"same_disk_hardlink_failed={same_disk_hardlink_failed_count} "
                  f"collected_bytes={collected_bytes}")
        return jsonify({
            "success": len(failed) == 0,
            "key": key,
            "target_dir": str(target_dir),
            "target_folder_name": target_dir.name,
            "folder_file_count": folder_file_count,
            "folder_total_size_bytes": folder_total_size_bytes,
            "folder_total_size_human": (human_folder_size(folder_total_size_bytes)
                                        if folder_total_size_bytes is not None else None),
            "matched": len(entries),
            "max_total_gb": COLLECT_DEFAULT_MAX_GB if body.get("max_total_gb") is None else body["max_total_gb"],
            "linked_count": len(linked),
            "linked_bytes": linked_bytes,
            "copied_count": len(copied),
            "copied_bytes": copied_bytes,
            "collected_count": len(linked) + len(copied),
            "collected_bytes": collected_bytes,
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "same_disk_hardlink_failed_count": same_disk_hardlink_failed_count,
            "warning_count": len(warnings),
            "warnings": warnings,
            "linked": linked,
            "copied": copied,
            "skipped": skipped,
            "failed": failed,
        })

register_collect_routes(app, Session, Video, normalize_text, load_paths)


_initialized = False


def initialize_app():

    global _initialized

    if _initialized:

        return

    _initialized = True


    print("[init] 建表 / 迁移...")

    init_db()

    try:

        migrate_database()

    except Exception as e:

        print(f"[init] 迁移跳过: {e}")


    try:
        init_play_db()
    except Exception as e:
        print(f"[init] 播放记录库初始化失败: {e}")

    # 【拆分版】注册新增路由 + 鉴权门，并打印配置警告。
    register_split_routes(app)
    log_startup_warnings()


PORT = int(os.environ.get("PORT", 18930))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Localdy split server")
    parser.add_argument("--port", type=int, default=None,
                        help="监听端口（默认环境变量 PORT，否则 18930）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--cert", default=None, help="HTTPS 证书文件（PEM）")
    parser.add_argument("--key", default=None, help="HTTPS 私钥文件（PEM）")
    args = parser.parse_args()

    port = args.port or PORT
    initialize_app()
    debug_mode = os.environ.get("FLASK_DEBUG") == "1"

    # 【拆分版】--cert/--key 走 Flask 内置服务器 + ssl_context；
    # waitress 不支持 TLS，有证书时不走 waitress。
    if args.cert and args.key:
        print(f"[start] HTTPS 服务启动: https://{args.host}:{port}")
        app.run(host=args.host, port=port, debug=False, use_reloader=False,
                threaded=True, ssl_context=(args.cert, args.key))
    elif debug_mode:
        app.run(host=args.host, port=port, debug=True, use_reloader=False,
                threaded=True)
    else:
        try:
            from waitress import serve
            print(f"[start] waitress 服务启动: http://{args.host}:{port}")
            serve(app, host=args.host, port=port, threads=8,
                  channel_timeout=600, cleanup_interval=30)
        except ImportError:
            print("[start] 未安装 waitress，退回 Flask 内置服务器（仍关闭 reloader）")
            print("        建议：pip install waitress")
            app.run(host=args.host, port=port, debug=False, use_reloader=False,
                    threaded=True)
