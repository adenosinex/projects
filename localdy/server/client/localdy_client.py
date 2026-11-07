#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
localdy_client.py -- Localdy 单文件客户端（仅 Python 标准库）

把放视频的 Windows 机器变成“数据面”：扫盘算 hash、推索引给服务端、
直接给浏览器送视频流（HTTP Range）、执行改文件任务（改名/收集/归档）。

运行方式：
    双击 localdy_client.bat，或：  python localdy_client.py
    默认是全局模式：直接启动即可播放服务端已索引的全部视频，
    无需输入路径（服务端已有 hash->路径表，见 /api/hash-paths）。
    加新文件夹用命令行启动一个临时会话（只看这个文件夹，单次处理，
    不影响全局模式）：
        python localdy_client.py --root "D:\\Videos"

两种模式（2026-11-07 起，v0.3.0 定死）：
    * 全局模式（默认）：启动时从服务端同步全部 hash->路径，
      之后每 30 分钟同步一次；票据遇到未知 hash 时也会即时同步一次。
      本地不扫盘、不问路径。全局模式的视频库实际横跨多个路径
      （服务端 video_paths.json 里有 5 个 NAS 根目录），所以单路径
      提示本来就是错的。
    * 新路径模式（--root <路径>）：临时独立会话。只扫这一个文件夹，
      索引只放内存，不写盘、不上报、不进全局索引；适合单次处理文件
      （看完移动/打标签），下次换个新路径再开一次。会话里改文件后，
      移动结果会按文件上报给服务端（POST /api/file-moves）：库里有
      的文件改路径，复制进文库根目录的新文件才入库；会话文件夹本身
      永远不会被整批索引。
    * 会话记录（v0.4.0）：会话数据单独存在客户端本地的
      localdy_sessions.json 里（按文件夹分条），关掉客户端不丢失；
      下次再 --root 同一个路径，处理进度（哪些已打标签/收藏/归档）
      直接恢复。记录不上传服务端，用户自己管理（删文件或会话页里
      “删除会话记录”即忘掉）。

协议与设计见 workspace/your_files/localdy-server-client-design.md §3 / §5 / §12 / §13。

与设计稿的两处有意偏离（原因写在代码注释和交付报告里）：
    1. 媒体票据不是“用一次就焚毁”，而是 5 分钟有效期内可重复使用
       （绑定单个 hash）。浏览器一次播放会发多次 Range 请求，
       严格单次票据会在 seek/预加载时断流。
    2. 服务端下发的任务必须带具体文件（items:[{hash, path?, ...}]），
       因为客户端没有数据库，无法把 §5.3 示例里的 tags 解析成文件；
       tag 到文件的展开必须在服务端做。
    3. del_1score 原版“移动失败且文件 >200MB 时直接删除源文件”的
       逻辑没有照搬：失败只报错，绝不删源文件。
"""

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.4.0"
DEFAULT_PORT = 18931
DEFAULT_BIND = "0.0.0.0"
TICKET_TTL = 300                    # 媒体票据有效期（秒）
HEARTBEAT_INTERVAL = 30             # 心跳间隔（秒）
POLL_INTERVAL = 5                   # 取任务间隔（秒）
HEAD_HASH_SIZE = 1024 * 1024        # 只读文件前 1MB 算 SHA-256（与 core.py 一致）
CHUNK = 1024 * 1024                 # 视频流分块大小
BIG_FILE_LIMIT = 500 * 1024 * 1024  # 5 星复制：超过此大小只写信息报告（与 core.py 一致）

# 与 core.py VIDEO_EXTS 完全一致（任务简述里只列了其中 8 个）
VIDEO_EXTS = (".mp4", ".avi", ".mkv", ".mov", ".flv", ".webm", ".m4v",
              ".wmv", ".ts", ".rmvb", ".mpg", ".mpeg")

DEFAULT_SERVER_URLS = [
    "http://192.168.31.197:18930",
    "https://apple.su7.dpdns.org:18931",
    "http://100.87.118.70:18930",
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "localdy_client.json")
STATE_FILE = os.path.join(BASE_DIR, "localdy_client_state.json")
PENDING_FILE = os.path.join(BASE_DIR, "localdy_client_pending.json")
# 会话记录：按文件夹存，一个 JSON 文件，全在客户端本地，用户自己管理。
# 删掉这个文件（或会话页里的“删除会话记录”）就等于忘掉所有会话。
SESSIONS_FILE = os.path.join(BASE_DIR, "localdy_sessions.json")

_TAG_RE = re.compile(r"#([\u4e00-\u9fa5\w]+)")
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)\s*$")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def head_hash(path):
    """读文件前 1MB 算 SHA-256，返回 (hash, size, mtime)；失败返回 (None, 0, 0)。"""
    try:
        st = os.stat(path)
        with open(path, "rb") as f:
            h = hashlib.sha256(f.read(HEAD_HASH_SIZE)).hexdigest()
        return h, st.st_size, st.st_mtime
    except Exception as e:
        log("[hash] 失败 %s: %s" % (path, e))
        return None, 0, 0


def lan_ips():
    """本机局域网 IP 列表（不含 127.0.0.1）。"""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # 不发包，只取本地出口地址
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
        finally:
            s.close()
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def origin_of(url):
    """把 http://host:port/path 归约成 origin（scheme://host:port）。"""
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme and p.netloc:
            return "%s://%s" % (p.scheme.lower(), p.netloc.lower())
    except Exception:
        pass
    return None


def http_json(url, method="GET", payload=None, headers=None, timeout=10):
    """发一个 JSON 请求，返回 (status, body_dict_or_None, error_str_or_None)。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8") or "null"), None
            except Exception:
                return resp.status, None, None
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "null")
        except Exception:
            body = None
        return e.code, body, "HTTP %d" % e.code
    except Exception as e:
        return None, None, str(e)


# ---------------------------------------------------------------------------
# 配置（localdy_client.json，和脚本放一起）
# ---------------------------------------------------------------------------
class Config:
    def __init__(self):
        self.root = None          # 最近一次扫描的根目录（兼容旧配置）
        self.roots = []           # 本地管理的根目录列表（可多个；全局模式可为空）
        self.server_urls = list(DEFAULT_SERVER_URLS)
        self.server_url = None        # 上次连通的服务端，记住，下次优先试它
        self.extra_origins = []       # 额外放行的 Origin（如局域网页面换了端口）
        self.port = DEFAULT_PORT
        self.bind = DEFAULT_BIND
        self.token = None
        self.client_id = None
        # 任务默认目标目录（留空则任务必须自带 dest；也可以直接在
        # localdy_client.json 里填上，之后 collect/archive/copy-5star
        # 任务不带 dest 时就用这里的值）
        self.collect_dest = None
        self.archive_dest = None
        self.star5_dest = None

    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                for k in ("root", "roots", "server_urls", "server_url", "extra_origins",
                          "port", "bind", "token", "client_id",
                          "collect_dest", "archive_dest", "star5_dest"):
                    if k in d and d[k] is not None:
                        setattr(self, k, d[k])
                # 旧版只有单个 root：迁入 roots 列表
                if not d.get("roots") and d.get("root"):
                    self.roots = [d["root"]]
                if self.roots and not self.root:
                    self.root = self.roots[0]
            except Exception as e:
                log("[config] 配置文件损坏，将用默认值重建：%s" % e)

    def save(self):
        d = {
            "root": self.root,
            "roots": self.roots,
            "server_urls": self.server_urls,
            "server_url": self.server_url,
            "extra_origins": self.extra_origins,
            "port": self.port,
            "bind": self.bind,
            "token": self.token,
            "client_id": self.client_id,
            "collect_dest": self.collect_dest,
            "archive_dest": self.archive_dest,
            "star5_dest": self.star5_dest,
        }
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)

    def allowed_origins(self):
        origins = set()
        for u in (self.server_urls or []) + ([self.server_url] if self.server_url else []):
            o = origin_of(u)
            if o:
                origins.add(o)
        for o in self.extra_origins or []:
            if o:
                origins.add(o)
        return origins


CONFIG = Config()


# ---------------------------------------------------------------------------
# 扫描状态（localdy_client_state.json）+ 待推送差异（localdy_client_pending.json）
# ---------------------------------------------------------------------------
class ScanState:
    def __init__(self):
        self.lock = threading.Lock()
        self.files = {}      # path -> {"size": int, "mtime": float, "hash": str, "src": "server"|"scan"}
        self.last_scan = None
        self.root = None     # 最近一次扫描的根目录（兼容旧状态）
        self.roots = []      # 本地管理的根目录列表

    def load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self.files = d.get("files", {})
                self.last_scan = d.get("last_scan")
                self.root = d.get("root")
                self.roots = d.get("roots") or ([d["root"]] if d.get("root") else [])
                # 旧版条目没有 src：一律视为本地扫描所得
                for info in self.files.values():
                    if isinstance(info, dict):
                        info.setdefault("src", "scan")
            except Exception as e:
                log("[state] 状态文件损坏，从空开始：%s" % e)

    def save(self):
        with self.lock:
            d = {"root": self.root, "roots": self.roots,
                 "last_scan": self.last_scan, "files": self.files}
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, STATE_FILE)

    def find_path_by_hash(self, target_hash):
        with self.lock:
            for path, info in self.files.items():
                if info.get("hash") == target_hash:
                    return path
        return None


STATE = ScanState()


# ---------------------------------------------------------------------------
# 新路径会话（--root <路径>）：只看一个文件夹，临时独立
# ---------------------------------------------------------------------------
class SessionState:
    """新路径会话：只扫 --root 指定的一个文件夹。

    会话数据（索引 + 处理进度）单独存在客户端本地的
    localdy_sessions.json 里（按文件夹分条），不进全局索引、
    不上报服务端；下次再 --root 同一个路径直接恢复。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.root = None
        self.scanned_at = None
        self.files = {}      # path -> {"size","mtime","hash","status","action_at"}
        self.done = {}       # old_path -> {"hash","filename","action","new_path","at"}

    def find_path_by_hash(self, target_hash):
        with self.lock:
            for path, info in self.files.items():
                if info.get("hash") == target_hash:
                    return path
        return None

    def update_path(self, old_path, new_path):
        with self.lock:
            info = self.files.pop(old_path, None)
            if info is not None:
                self.files[new_path] = info

    def record(self):
        """当前会话的持久化记录（调用方负责在锁外写盘）。"""
        with self.lock:
            return {
                "root": self.root,
                "scanned_at": self.scanned_at,
                "files": dict(self.files),
                "done": dict(self.done),
            }

    def clear(self):
        with self.lock:
            self.root = None
            self.scanned_at = None
            self.files = {}
            self.done = {}


SESSION = SessionState()


def _session_key(root):
    """会话记录的键：归一化后的文件夹路径。"""
    return os.path.normcase(os.path.abspath(root))


def load_sessions():
    """读全部会话记录 {root_key: record}；文件不存在或损坏返回 {}。"""
    if not os.path.exists(SESSIONS_FILE):
        return {}
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception as e:
        log("[session] 会话记录读失败（忽略）：%s" % e)
        return {}


def save_sessions():
    """把当前会话写回 localdy_sessions.json（只改自己这一条，
    其他文件夹的记录原样保留）。"""
    if not SESSION.root:
        return
    sessions = load_sessions()
    sessions[_session_key(SESSION.root)] = SESSION.record()
    tmp = SESSIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SESSIONS_FILE)


def clear_session_record(root=None):
    """删掉一个文件夹的会话记录（默认当前会话），内存里的会话也清掉。"""
    sessions = load_sessions()
    key = _session_key(root or SESSION.root or "")
    if key in sessions:
        del sessions[key]
        tmp = SESSIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SESSIONS_FILE)
    if root is None or _session_key(root) == _session_key(SESSION.root or ""):
        SESSION.clear()


def find_path_anywhere(target_hash):
    """找文件：先看会话（新路径），再看全局索引。全局播放不受会话影响，
    会话文件也能播。"""
    path = SESSION.find_path_by_hash(target_hash)
    if path:
        return path
    return STATE.find_path_by_hash(target_hash)


def _move_index_entry(old, new):
    """改名/移动后更新索引：全局条目（在 STATE 里才改，写盘）+
    会话条目（只改内存）。"""
    moved_global = False
    with SCAN_LOCK:
        with STATE.lock:
            if old in STATE.files:
                STATE.files[new] = STATE.files.pop(old)
                moved_global = True
    if moved_global:
        STATE.save()
    SESSION.update_path(old, new)


def load_pending():
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, list):
                return d
        except Exception:
            pass
    return []


def save_pending(items):
    tmp = PENDING_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
    os.replace(tmp, PENDING_FILE)


# ---------------------------------------------------------------------------
# 媒体票据（内存，5 分钟有效期，绑定单个 hash）
# ---------------------------------------------------------------------------
class Tickets:
    def __init__(self):
        self.lock = threading.Lock()
        self.tickets = {}    # ticket -> {"hash": str, "exp": float, "uses": int}

    def issue(self, file_hash):
        ticket = secrets.token_urlsafe(24)
        with self.lock:
            self.tickets[ticket] = {"hash": file_hash,
                                    "exp": time.time() + TICKET_TTL, "uses": 0}
        return ticket

    def check(self, ticket, file_hash):
        """校验通过返回 True；过期/不存在/不属于该 hash 返回 False。
        同一票据在有效期内可重复使用（浏览器一次播放发多次 Range 请求，
        严格单次票据会在 seek/预加载时断流——见文件头注释）。"""
        with self.lock:
            self._cleanup_locked()
            t = self.tickets.get(ticket)
            if not t:
                return False
            if t["hash"] != file_hash or t["exp"] < time.time():
                return False
            t["uses"] += 1
            return True

    def _cleanup_locked(self):
        now = time.time()
        dead = [k for k, v in self.tickets.items() if v["exp"] < now]
        for k in dead:
            del self.tickets[k]


TICKETS = Tickets()


# ---------------------------------------------------------------------------
# 扫描：fast_scan + 集合比对（逻辑照搬 core.py）
# ---------------------------------------------------------------------------
def fast_scan(root_folder):
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
            log("[scan] 无法访问目录 %s: %s" % (d, e))
    if dir_errors:
        log("[scan] 共 %d 个目录无法访问" % dir_errors)
    return results


SCAN_LOCK = threading.Lock()


def scan_session(root):
    """新路径会话：只扫这一个文件夹。

    先读本地会话记录（localdy_sessions.json）：同一个文件夹上次的处理
    进度（status / 已处理）直接恢复，大小没变的文件不重新算 hash；
    新文件 / 大小变了的文件重新算 hash，状态回到“新”。
    磁盘为准：记录里有、磁盘上没了、又不是已处理的文件，直接丢掉。
    扫完把记录写回本地文件。不上报、不进全局索引。
    返回 (文件数, 新增 hash 数)。"""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise RuntimeError("视频目录不存在：%s" % root)
    prev_record = load_sessions().get(_session_key(root)) or {}
    prev_files = prev_record.get("files") or {}
    prev_done = prev_record.get("done") or {}
    disk = fast_scan(root)
    new_files = {}
    n_new_hash = 0
    for path, name, size in disk:
        prev = prev_files.get(path)
        if prev is not None and prev.get("size") == size:
            new_files[path] = prev
            continue
        h, sz, mt = head_hash(path)
        if h is None:
            continue
        new_files[path] = {"size": sz, "mtime": mt, "hash": h, "status": "new"}
        if prev is None or prev.get("hash") != h:
            n_new_hash += 1
    with SESSION.lock:
        SESSION.root = root
        SESSION.scanned_at = now_iso()
        SESSION.files = new_files
        SESSION.done = dict(prev_done)
    save_sessions()
    n_restored = sum(1 for v in new_files.values() if v.get("status") not in (None, "new"))
    log("[session] 会话文件夹 %s：%d 个文件（新 hash %d，恢复进度 %d，已处理 %d）"
        % (root, len(new_files), n_new_hash, n_restored, len(prev_done)))
    return len(new_files), n_new_hash


def update_session_from_job(job, result=None):
    """会话任务完成后的进度记录：打标签/改名留在清单里标状态；
    归档/去重移出清单、记进“已处理”；收藏/5星复制留在清单里标状态。
    只改本地会话记录，不动全局索引。"""
    result = result if result is not None else (job.get("result") or {})
    jtype = job.get("type")
    status_map = {"rename": "tagged", "apply-tags": "tagged",
                  "collect": "collected", "copy-5star": "copied"}
    changed = False
    with SESSION.lock:
        for r in result.get("results") or []:
            if not isinstance(r, dict) or not r.get("ok"):
                continue
            old, new = r.get("old"), r.get("new")
            if not old or not new or old == new:
                continue
            if jtype in ("archive-1star", "dedup"):
                info = SESSION.files.pop(old, None)
                if info is None:
                    # _move_index_entry 已在执行时把条目移到新路径，
                    # 这里按新路径找回来记进“已处理”。
                    info = SESSION.files.pop(new, None)
                if info is not None:
                    SESSION.done[old] = {
                        "hash": r.get("hash"),
                        "filename": os.path.basename(old),
                        "action": jtype, "new_path": new, "at": now_iso()}
                    changed = True
            elif jtype in status_map:
                info = SESSION.files.get(old)
                if info is not None:
                    info["status"] = status_map[jtype]
                    info["action_at"] = now_iso()
                    changed = True
                elif jtype in ("rename", "apply-tags"):
                    # 改名时 _move_index_entry 已把条目移到新路径，
                    # 这里按新路径找回来标状态。
                    info = SESSION.files.get(new)
                    if info is not None:
                        info["status"] = status_map[jtype]
                        info["action_at"] = now_iso()
                        changed = True
    if changed:
        save_sessions()


def push_diff(payload):
    """把一批差异 POST 给服务端 /api/index-diff。返回 (ok, 说明)。"""
    url = (CONFIG.server_url or "").rstrip("/") + "/api/index-diff"
    status, _body, err = http_json(url, method="POST", payload=payload,
                                   headers={"X-Client-Token": CONFIG.token or ""},
                                   timeout=30)
    if status == 200:
        return True, "ok"
    if status is not None:
        return False, "HTTP %d" % status
    return False, err or "unreachable"


# ---------------------------------------------------------------------------
# 全局模式：从服务端同步全部 hash -> 路径（GET /api/hash-paths）
# ---------------------------------------------------------------------------
_LAST_SYNC = {"at": 0.0}
_LAST_LAZY_SYNC = {"at": 0.0}
SYNC_MIN_INTERVAL = 60  # 秒：定期同步的节流
LAZY_SYNC_MIN_INTERVAL = 5  # 秒：票据缺失时即时同步的节流（短一点，
# 因为“服务端刚索引了新视频”是常见情况；5 秒内重复缺失不再拉全表）


def _norm_sep_lower(p):
    return (p or "").replace("\\", "/").rstrip("/").lower()


def _under_root(path, root):
    """Windows 感知：path 是否在 root 之下（大小写、斜杠都归一化）。"""
    npath, nroot = _norm_sep_lower(path), _norm_sep_lower(root)
    return npath == nroot or npath.startswith(nroot + "/")


def sync_global_map(force=False):
    """全局模式：拉取服务端全部 hash->path，合并进本地索引。

    合并规则：本地扫描过的条目（src=scan）更新鲜，hash 冲突时保留本地
    路径；其余按服务端表为准（src=server）；服务端已没有的 server 条目
    删掉；本地扫描条目即使服务端没有也保留（可能是刚扫完还没推成功）。
    返回 (ok, 说明)。"""
    if not CONFIG.server_url:
        return False, "no-server"
    now = time.time()
    if not force and now - _LAST_SYNC["at"] < SYNC_MIN_INTERVAL:
        return True, "throttled"
    url = CONFIG.server_url.rstrip("/") + "/api/hash-paths"
    status, body, err = http_json(url, method="GET",
                                  headers={"X-Client-Token": CONFIG.token or ""},
                                  timeout=60)
    if status != 200:
        return False, err or ("HTTP %s" % status)
    server_map = (body or {}).get("map") or {}
    with SCAN_LOCK:
        with STATE.lock:
            old = dict(STATE.files)
        scanned_by_hash = {}
        for p, info in old.items():
            if info.get("src") == "scan" and info.get("hash"):
                scanned_by_hash[info["hash"]] = p
        new_files = {}
        n_server = n_kept = 0
        for h, p in server_map.items():
            if not h or not p:
                continue
            if h in scanned_by_hash:
                lp = scanned_by_hash[h]
                new_files[lp] = old[lp]
                n_kept += 1
                continue
            new_files[p] = {"size": 0, "mtime": 0, "hash": h, "src": "server"}
            n_server += 1
        for p, info in old.items():
            if info.get("src") == "scan" and p not in new_files:
                new_files[p] = info
        STATE.files = new_files
        STATE.save()
    _LAST_SYNC["at"] = time.time()
    log("[sync] 全局索引已同步：服务端 %d 个，本地扫描保留 %d 个，共 %d 个"
        % (n_server, n_kept, len(new_files)))
    return True, "ok"


# ---------------------------------------------------------------------------
# 改文件任务：执行器（rename / apply-tags / collect / archive-1star /
# copy-5star / dedup）。每个文件独立 try/except，一个出错不卡住其余。
# ---------------------------------------------------------------------------
def add_tags_to_filename(filename, tags):
    """把 tag 追加到文件名末尾（扩展名之前），和 core.py _add_tags_to_filename 一致。"""
    if not filename or not tags:
        return filename
    stem, ext = os.path.splitext(filename)
    existing = set(_TAG_RE.findall(filename))
    to_add = []
    for t in tags:
        if t and t not in existing and t not in to_add:
            to_add.append(t)
    if not to_add:
        return filename
    return "%s%s%s" % (stem, "".join(" #%s" % t for t in to_add), ext)


def resolve_item_path(item):
    """按 path（若存在）或 hash（先查会话、再查全局索引）定位文件；找不到返回 (None, 原因)。"""
    path = item.get("path")
    if path and os.path.isfile(path):
        return path, None
    h = item.get("hash")
    if h:
        path = find_path_anywhere(h)
        if path and os.path.isfile(path):
            return path, None
        if path:
            return None, "索引里有记录但文件不在磁盘上：%s" % path
        return None, "hash 不在索引里：%s" % h
    return None, "任务项既没有 path 也没有 hash"


def _unique_path(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = "%s_%d%s" % (stem, i, ext)
        if not os.path.exists(cand):
            return cand
        i += 1


def _collect_dedup_index(day_dir):
    idx_path = os.path.join(day_dir, ".collect-copies.json")
    try:
        with open(idx_path, "r", encoding="utf-8") as f:
            return json.load(f), idx_path
    except Exception:
        return {}, idx_path


def execute_item(jtype, item, job):
    """执行单个文件操作，返回 {hash, ok, ...}。"""
    h = item.get("hash")
    if jtype in ("rename", "apply-tags"):
        path, err = resolve_item_path(item)
        if not path:
            return {"hash": h, "ok": False, "error": err}
        if jtype == "rename":
            new_name = (item.get("new_name") or "").strip()
            if not new_name or os.path.dirname(new_name) or "/" in new_name \
                    or "\\" in new_name:
                return {"hash": h, "ok": False,
                        "error": "new_name 必须是不含路径的纯文件名：%r" % new_name}
        else:
            new_name = add_tags_to_filename(os.path.basename(path),
                                            item.get("tags") or job.get("tags") or [])
            if new_name == os.path.basename(path):
                return {"hash": h, "ok": True, "old": path, "new": path,
                        "note": "tag 已存在，无需改名"}
        new_path = os.path.join(os.path.dirname(path), new_name)
        if os.path.exists(new_path):
            return {"hash": h, "ok": False,
                    "error": "目标已存在，拒绝覆盖：%s" % new_path}
        os.rename(path, new_path)
        _move_index_entry(path, new_path)
        return {"hash": h, "ok": True, "old": path, "new": new_path}

    if jtype == "collect":
        path, err = resolve_item_path(item)
        if not path:
            return {"hash": h, "ok": False, "error": err}
        dest_root = job.get("dest") or CONFIG.collect_dest
        if not dest_root:
            return {"hash": h, "ok": False,
                    "error": "collect 需要 dest（目标目录）"}
        day_dir = os.path.join(dest_root, time.strftime("%Y-%m-%d"))
        os.makedirs(day_dir, exist_ok=True)
        idx, idx_path = _collect_dedup_index(day_dir)
        if h and h in idx:
            return {"hash": h, "ok": True, "new": idx[h], "note": "已收集过，跳过"}
        dest = os.path.join(day_dir, os.path.basename(path))
        if os.path.exists(dest):
            dest = _unique_path(dest)
        mode = (job.get("mode") or "hardlink").lower()
        made = None
        if mode == "hardlink":
            try:
                os.link(path, dest)
                made = "hardlink"
            except OSError as e:
                # 跨盘/跨文件系统硬链接失败时退回复制（与原版 collect 一致）
                shutil.copy2(path, dest)
                made = "copy(hardlink-failed: %s)" % e
        else:
            shutil.copy2(path, dest)
            made = "copy"
        if h:
            idx[h] = dest
            with open(idx_path, "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False)
        return {"hash": h, "ok": True, "old": path, "new": dest, "mode": made}

    if jtype == "archive-1star":
        path, err = resolve_item_path(item)
        if not path:
            return {"hash": h, "ok": False, "error": err}
        dest_root = job.get("dest") or CONFIG.archive_dest
        if not dest_root:
            return {"hash": h, "ok": False, "error": "archive-1star 需要 dest"}
        dest = os.path.join(dest_root, os.path.basename(path))
        if os.path.exists(dest):
            dest = _unique_path(dest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            os.renames(path, dest)
        except OSError:
            # 移动失败时退回“复制+删除源文件”；任何一步失败都只报错，
            # 绝不像 core.py del_1score 那样把大文件直接删掉。
            shutil.copy2(path, dest)
            os.remove(path)
        _move_index_entry(path, dest)
        return {"hash": h, "ok": True, "old": path, "new": dest}

    if jtype == "copy-5star":
        path, err = resolve_item_path(item)
        if not path:
            return {"hash": h, "ok": False, "error": err}
        dest_root = job.get("dest") or CONFIG.star5_dest
        if not dest_root:
            return {"hash": h, "ok": False, "error": "copy-5star 需要 dest"}
        size = os.path.getsize(path)
        if size > BIG_FILE_LIMIT:
            info_dir = os.path.join(dest_root, "大文件信息")
            os.makedirs(info_dir, exist_ok=True)
            stem, _ext = os.path.splitext(os.path.basename(path))
            info_path = os.path.join(info_dir, stem + ".txt")
            with open(info_path, "w", encoding="utf-8") as f:
                f.write("文件：%s\n大小：%d 字节\n超过 500MB，只写信息报告，不复制。\n"
                        % (path, size))
            return {"hash": h, "ok": True, "old": path, "new": info_path,
                    "note": "超过 500MB，只写了信息报告"}
        dest = os.path.join(dest_root, os.path.basename(path))
        if os.path.exists(dest):
            dest = _unique_path(dest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(path, dest)
        return {"hash": h, "ok": True, "old": path, "new": dest}

    if jtype == "dedup":
        path, err = resolve_item_path(item)
        if not path:
            return {"hash": h, "ok": False, "error": err}
        dest_root = job.get("dest")
        if not dest_root:
            return {"hash": h, "ok": False, "error": "dedup 需要 dest"}
        dup_dir = os.path.join(dest_root, "_duplicates")
        os.makedirs(dup_dir, exist_ok=True)
        dest = os.path.join(dup_dir, os.path.basename(path))
        if os.path.exists(dest):
            dest = _unique_path(dest)
        os.renames(path, dest)
        _move_index_entry(path, dest)
        return {"hash": h, "ok": True, "old": path, "new": dest}

    return {"hash": h, "ok": False, "error": "未知任务类型：%s" % jtype}


def execute_job(job):
    """执行一个任务的全部 items，返回 result。绝不抛异常。"""
    jtype = job.get("type")
    items = job.get("items") or []
    if jtype not in ("rename", "apply-tags", "collect",
                     "archive-1star", "copy-5star", "dedup"):
        return {"ok": False, "error": "未知任务类型：%s" % jtype,
                "results": []}
    if not items:
        return {"ok": False, "error": "任务没有 items", "results": []}
    results = []
    ok_count = 0
    for item in items:
        try:
            r = execute_item(jtype, item, job)
        except Exception as e:
            r = {"hash": item.get("hash"), "ok": False,
                 "error": "%s: %s" % (type(e).__name__, e)}
        results.append(r)
        if r.get("ok"):
            ok_count += 1
    if ok_count == len(results):
        status = "done"
    elif ok_count == 0:
        status = "failed"
    else:
        status = "partial"
    return {"ok": ok_count > 0, "status": status,
            "done": ok_count, "total": len(results), "results": results}


class JobStore:
    """任务队列：串行执行，一个任务出错绝不卡住队列。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}               # id -> job dict
        self.queue = queue.Queue()

    def create(self, payload, source="api"):
        job_id = payload.get("id") or secrets.token_hex(6)
        job = {
            "id": job_id,
            "type": payload.get("type"),
            "items": payload.get("items") or [],
            "dest": payload.get("dest"),
            "mode": payload.get("mode"),
            "tags": payload.get("tags"),
            "status": "queued",
            # 来源：server（服务端下发，执行完回传 /api/file-jobs/<id>/result）、
            # session（会话页直接下发，执行完只把移动结果报 /api/file-moves）。
            "source": payload.get("source") or source,
            "server_job_id": payload.get("server_job_id"),
            "created_at": now_iso(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        # 把服务端下发的原始字段也留一份，供执行器取 dest/mode/tags
        job["_payload"] = payload
        with self.lock:
            self.jobs[job_id] = job
        self.queue.put(job_id)
        return job

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def list_recent(self, limit=50):
        with self.lock:
            jobs = sorted(self.jobs.values(),
                          key=lambda j: j["created_at"], reverse=True)[:limit]
        return [{k: v for k, v in j.items() if k != "_payload"} for j in jobs]

    def worker(self):
        while True:
            job_id = self.queue.get()
            job = self.get(job_id)
            if not job:
                continue
            try:
                with self.lock:
                    job["status"] = "running"
                    job["started_at"] = now_iso()
                log("[job] 开始 %s (%s)，%d 个文件"
                    % (job_id, job["type"], len(job["items"])))
                payload = dict(job["_payload"])
                payload.setdefault("dest", job.get("dest"))
                payload.setdefault("mode", job.get("mode"))
                payload.setdefault("tags", job.get("tags"))
                result = execute_job(payload)
                if job["source"] == "session":
                    # 先记会话进度，再把任务标成 done，
                    # 避免页面一刷新就看到旧状态。
                    update_session_from_job(job, result)
                with self.lock:
                    job["result"] = result
                    job["status"] = result.get("status", "failed")
                    job["finished_at"] = now_iso()
                log("[job] 完成 %s：%s，%d/%d 成功"
                    % (job_id, job["status"], result.get("done", 0),
                       result.get("total", 0)))
                if job["source"] == "server" and CONFIG.server_url:
                    self._report_to_server(job)
                elif job["source"] == "session" and CONFIG.server_url:
                    self._report_session_moves(job)
            except Exception as e:
                with self.lock:
                    job["status"] = "failed"
                    job["error"] = "%s: %s" % (type(e).__name__, e)
                    job["finished_at"] = now_iso()
                log("[job] 异常 %s：%s" % (job_id, e))

    def _report_to_server(self, job):
        sid = job.get("server_job_id") or job["id"]
        url = (CONFIG.server_url or "").rstrip("/") + \
            "/api/file-jobs/%s/result" % sid
        status, _b, err = http_json(
            url, method="POST",
            payload={"ok": job["result"].get("ok", False)
                     if job.get("result") else False,
                     "status": job["status"], "result": job.get("result")},
            headers={"X-Client-Token": CONFIG.token or ""}, timeout=15)
        if status == 200:
            log("[job] 已回传服务端：%s" % job["id"])
        else:
            log("[job] 回传服务端失败 %s：%s" % (job["id"], err or status))

    def _report_session_moves(self, job):
        """会话任务：只把移动结果按文件上报 /api/file-moves。
        服务端据此改库路径（库里有的文件）或入库（复制进文库根目录的新文件）；
        会话文件夹本身永远不会被整批索引。"""
        result = job.get("result") or {}
        moves = []
        kind = "move" if job.get("type") in (
            "rename", "apply-tags", "archive-1star", "dedup") else "copy"
        for r in result.get("results") or []:
            if not isinstance(r, dict) or not r.get("ok"):
                continue
            old, new = r.get("old"), r.get("new")
            if not old or not new or old == new:
                continue
            item = {"hash": r.get("hash"), "old": old, "new": new, "kind": kind}
            # 复制类任务需要文件大小（入库时用）；移动类不需要
            if kind == "copy":
                try:
                    item["size"] = os.path.getsize(new)
                except OSError:
                    item["size"] = 0
                item["mtime"] = time.time()
            moves.append(item)
        if not moves:
            return
        url = (CONFIG.server_url or "").rstrip("/") + "/api/file-moves"
        status, body, err = http_json(
            url, method="POST", payload={"moves": moves},
            headers={"X-Client-Token": CONFIG.token or ""}, timeout=15)
        if status == 200:
            log("[job] 会话移动已上报服务端：%s（改路径 %s，入库 %s）"
                % (job["id"], (body or {}).get("applied", "?"),
                   (body or {}).get("ingested", "?")))
        else:
            log("[job] 会话移动上报失败 %s：%s" % (job["id"], err or status))


JOBS = JobStore()


# ---------------------------------------------------------------------------
# 服务端发现 + 心跳 + 取任务（后台线程）
# ---------------------------------------------------------------------------
SERVER_STATE = {"reached": False, "last_error": None}


def probe_server(url):
    """GET {url}/api/client/status；返回 HTTP 状态码，None 表示连不上。
    200 或 401 都算“找到了服务端”（401 说明路由存在，只是要鉴权）。"""
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/client/status",
                                     method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def discover_server(candidates):
    seen = set()
    for url in candidates:
        url = (url or "").strip().rstrip("/")
        if not url or url in seen:
            continue
        seen.add(url)
        st = probe_server(url)
        if st in (200, 401):
            return url, st
    return None, None


def flush_pending_diffs():
    pending = load_pending()
    if not pending or not CONFIG.server_url:
        return
    remaining = []
    for payload in pending:
        ok, detail = push_diff(payload)
        if ok:
            log("[push] 补推上次未成功的差异成功（%s，新增 %d）"
                % (payload.get("scanned_at"), len(payload.get("added", []))))
        else:
            remaining.append(payload)
    if len(remaining) != len(pending):
        save_pending(remaining)


def heartbeat_once():
    if not CONFIG.server_url:
        url, st = discover_server(CONFIG.server_urls)
        if url:
            CONFIG.server_url = url
            CONFIG.save()
            SERVER_STATE["reached"] = True
            log("[server] 已连上服务端：%s（HTTP %d）" % (url, st))
        else:
            if SERVER_STATE["reached"]:
                log("[server] 服务端失联，继续本地运行，稍后重试")
            SERVER_STATE["reached"] = False
            return False
    flush_pending_diffs()
    url = CONFIG.server_url.rstrip("/") + "/api/client/status"
    payload = {
        "client_id": CONFIG.client_id,
        "port": CONFIG.port,
        "lan_ips": lan_ips(),
        "version": VERSION,
        "last_scan": STATE.last_scan,
        "root": SESSION.root or "",
        # v0.3.0 起没有“本地管理的根目录”了：新路径会话是临时的，
        # 不上报；服务端回退到 video_paths.json 展示文库根目录。
        "roots": [],
        "files_indexed": len(STATE.files),
    }
    status, _b, err = http_json(url, method="POST", payload=payload,
                                headers={"X-Client-Token": CONFIG.token or ""},
                                timeout=10)
    if status == 200:
        if not SERVER_STATE["reached"]:
            log("[server] 心跳恢复：%s" % CONFIG.server_url)
        SERVER_STATE["reached"] = True
        return True
    SERVER_STATE["reached"] = False
    SERVER_STATE["last_error"] = err or ("HTTP %s" % status)
    log("[server] 心跳失败：%s" % SERVER_STATE["last_error"])
    return False


def heartbeat_loop():
    time.sleep(1)
    n = 0
    while True:
        try:
            heartbeat_once()
        except Exception as e:
            log("[server] 心跳异常：%s" % e)
        n += 1
        if n % 60 == 0:  # 约 30 分钟：刷新一次全局索引
            try:
                sync_global_map()
            except Exception as e:
                log("[sync] 定期同步异常：%s" % e)
        time.sleep(HEARTBEAT_INTERVAL)


_POLL_LOGGED = {"ok": True}


def poll_once():
    if not CONFIG.server_url:
        return
    url = (CONFIG.server_url.rstrip("/") + "/api/file-jobs?client_id=%s&status=pending"
           % urllib.parse.quote(CONFIG.client_id or ""))
    status, body, err = http_json(url, method="GET",
                                  headers={"X-Client-Token": CONFIG.token or ""},
                                  timeout=10)
    if status != 200:
        if _POLL_LOGGED["ok"]:
            log("[poll] 取任务失败：%s" % (err or ("HTTP %s" % status)))
            _POLL_LOGGED["ok"] = False
        return
    if not _POLL_LOGGED["ok"]:
        log("[poll] 取任务恢复正常")
        _POLL_LOGGED["ok"] = True
    if isinstance(body, dict):
        jobs = body.get("jobs") or []
    elif isinstance(body, list):
        jobs = body
    else:
        jobs = []
    for sj in jobs:
        if not isinstance(sj, dict) or not sj.get("type"):
            continue
        payload = dict(sj)
        payload["server_job_id"] = sj.get("id")
        payload.setdefault("id", "srv-%s" % (sj.get("id") or secrets.token_hex(4)))
        if JOBS.get(payload["id"]):
            continue  # 已取过，不重复执行
        log("[poll] 取到服务端任务 %s（%s）" % (payload["id"], payload["type"]))
        JOBS.create(payload, source="server")


def poll_loop():
    time.sleep(2)
    while True:
        try:
            poll_once()
        except Exception as e:
            log("[poll] 异常：%s" % e)
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
SESSION_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Localdy · 新路径会话</title>
<style>body{font-family:system-ui,sans-serif;background:#e3e6eb;margin:0;padding:16px}
h1{font-size:18px;margin:0 0 4px}.sub{color:#666;font-size:13px;margin:0 0 12px}
video{width:100%;max-width:900px;background:#000;border-radius:8px}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden}
td,th{padding:6px 8px;border-bottom:1px solid #eee;font-size:13px;text-align:left}
tr.sel{background:#fdf0f3}button{font-size:13px;padding:4px 10px;margin:1px;border:0;border-radius:5px;background:#222;color:#fff;cursor:pointer}
button.ghost{background:#888}.row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
input{font-size:13px;padding:5px 8px;border:1px solid #ccc;border-radius:5px}
label{font-size:13px;color:#555}#msg{color:#c7355b;font-size:13px;min-height:18px}</style></head>
<body>
<h1>新路径会话</h1>
<p class="sub" id="root"></p>
<div id="auth" style="display:none">配对码：<input id="token" type="password" placeholder="客户端控制台里显示的配对码">
<button onclick="saveToken()">保存</button>（每台设备的浏览器输一次，存在本页地址下）</div>
<div class="row">
<label>收藏目录 <input id="destCollect" size="30" placeholder="\\\\NAS\\...\\view"></label>
<label>归档目录 <input id="destArchive" size="30" placeholder="\\\\NAS\\...\\del"></label>
<label>5星目录 <input id="destStar5" size="30" placeholder="\\\\NAS\\...\\精华"></label>
</div>
<video id="player" controls preload="metadata"></video>
<p id="now"></p>
<p id="msg"></p>
<table><thead><tr><th>文件名</th><th>大小</th><th>状态</th><th>操作</th></tr></thead>
<tbody id="list"></tbody></table>
<h2 style="font-size:15px;margin:16px 0 4px">已处理（移出本文件夹的）</h2>
<table><thead><tr><th>文件名</th><th>动作</th><th>新位置</th></tr></thead>
<tbody id="done"></tbody></table>
<p style="font-size:12px;color:#888" id="store"></p>
<p><button class="ghost" onclick="clearSession()">删除会话记录</button>
<span style="font-size:12px;color:#888">删掉后下次再打开这个文件夹就是全新会话（文件本身不动）。</span></p>
<script>
let token = localStorage.getItem("localdy_token") || "";
let files = [];
function headers(){ return {"X-Client-Token": token, "Content-Type": "application/json"}; }
function saveToken(){ token = document.getElementById("token").value.trim();
  if(token){ localStorage.setItem("localdy_token", token);
    document.getElementById("auth").style.display = "none"; load(); } }
function fmt(n){ if(n > 1048576) return (n/1048576).toFixed(1)+" MB";
  if(n > 1024) return (n/1024).toFixed(0)+" KB"; return n+" B"; }
async function load(){
  const r = await fetch("/session.json", {headers: headers()});
  if(r.status === 401){ document.getElementById("auth").style.display = "block"; return; }
  const d = await r.json();
  document.getElementById("root").textContent =
    "只看这个文件夹（临时会话，不进全局索引）：" + (d.root || "未启动会话");
  const dests = d.dests || {};
  for(const [id, key] of [["destCollect","collect"],["destArchive","archive"],["destStar5","star5"]]){
    const el = document.getElementById(id);
    if(!el.value) el.value = localStorage.getItem("localdy_dest_"+key) || dests[key] || "";
    el.onchange = () => localStorage.setItem("localdy_dest_"+key, el.value);
  }
  files = d.files || [];
  const STATUS = {new: "新", tagged: "已打标签", collected: "已收藏", copied: "已复制5星"};
  const tb = document.getElementById("list"); tb.innerHTML = "";
  files.forEach((f, i) => {
    const tr = document.createElement("tr"); tr.id = "row-" + i;
    tr.innerHTML = "<td></td><td>"+fmt(f.size)+"</td><td>"+(STATUS[f.status] || f.status || "新")+"</td><td>"
      + "<button onclick='play("+i+")'>播放</button> "
      + "<button onclick='act("+i+",\"apply-tags\")'>打标签</button> "
      + "<button onclick='act("+i+",\"collect\")'>收藏</button> "
      + "<button onclick='act("+i+",\"archive-1star\")'>归档1星</button> "
      + "<button onclick='act("+i+",\"copy-5star\")'>5星复制</button></td>";
    tr.firstChild.textContent = f.filename;
    tb.appendChild(tr);
  });
  const done = d.done || [];
  const dtb = document.getElementById("done"); dtb.innerHTML = "";
  const ACT = {"archive-1star": "归档1星", "dedup": "去重"};
  done.forEach(v => {
    const tr = document.createElement("tr");
    tr.innerHTML = "<td></td><td>"+(ACT[v.action] || v.action || "")+"</td><td></td>";
    tr.children[0].textContent = v.filename || "";
    tr.children[2].textContent = v.new_path || "";
    dtb.appendChild(tr);
  });
  document.getElementById("store").textContent =
    "会话记录存在客户端本地的 " + (d.session_file || "localdy_sessions.json")
    + " 里（按文件夹分条），关掉客户端不丢失；下次再打开同一个文件夹直接恢复。";
  document.getElementById("msg").textContent =
    "共 " + files.length + " 个文件，其中已处理 " + done.length + " 个";
}
async function play(i){
  const f = files[i];
  document.querySelectorAll("tr.sel").forEach(e => e.classList.remove("sel"));
  document.getElementById("row-"+i).classList.add("sel");
  const r = await fetch("/media-tickets", {method: "POST", headers: headers(),
    body: JSON.stringify({hashes: [f.hash]})});
  const d = await r.json();
  const t = d.tickets && d.tickets[f.hash];
  if(!t){ document.getElementById("msg").textContent = "拿不到票据：" + JSON.stringify(d); return; }
  const v = document.getElementById("player");
  v.src = "/media/" + f.hash + "?ticket=" + t;
  document.getElementById("now").textContent = "正在播放：" + f.filename;
}
async function act(i, type){
  const f = files[i];
  let payload = {type: type, source: "session",
    items: [{hash: f.hash, path: f.path}]};
  if(type === "apply-tags"){
    const tags = prompt("输入标签（用 # 开头，多个用空格隔开）：", "#");
    if(!tags) return;
    // 后端约定：tags 不带 #，函数自己会加 " #tag"
    payload.tags = tags.split(/\s+/).map(x => x.replace(/^#+/, "")).filter(x => x);
  }
  if(type === "collect" || type === "copy-5star" || type === "archive-1star"){
    const key = type === "collect" ? "destCollect" : type === "copy-5star" ? "destStar5" : "destArchive";
    const dest = document.getElementById(key).value.trim();
    if(!dest){ document.getElementById("msg").textContent = "请先填目标目录"; return; }
    payload.dest = dest;
  }
  const r = await fetch("/jobs", {method: "POST", headers: headers(),
    body: JSON.stringify(payload)});
  const d = await r.json();
  if(!d.job_id){ document.getElementById("msg").textContent = "任务创建失败：" + JSON.stringify(d); return; }
  document.getElementById("msg").textContent = "任务执行中…";
  for(let k = 0; k < 60; k++){
    await new Promise(res => setTimeout(res, 1000));
    const jr = await fetch("/jobs/" + d.job_id, {headers: headers()});
    const jd = await jr.json();
    if(jd.status === "done" || jd.status === "failed" || jd.status === "partial"){
      const res = (jd.result && jd.result.results && jd.result.results[0]) || {};
      document.getElementById("msg").textContent =
        "完成：" + jd.status + "，" + (res.ok ? "新位置：" + (res.new || "") : "失败：" + (res.error || ""));
      load(); return;
    }
  }
  document.getElementById("msg").textContent = "任务还在执行，稍后刷新查看";
}
async function clearSession(){
  if(!confirm("确定要忘掉这个文件夹的会话记录吗？文件本身不会动。")) return;
  const r = await fetch("/session/clear", {method: "POST", headers: headers()});
  const d = await r.json();
  if(d.ok){ document.getElementById("msg").textContent = "已删除会话记录"; load(); }
  else { document.getElementById("msg").textContent = "删除失败：" + JSON.stringify(d); }
}
load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocaldyClient/" + VERSION

    # -- 响应工具 ------------------------------------------------------------
    def log_message(self, fmt, *args):
        # 常规访问日志不刷屏，只记错误行（状态码 >= 400 时由调用方显式记）
        pass

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_html(self, status, html):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _cors_headers(self):
        origin = self.headers.get("Origin")
        if origin and origin in CONFIG.allowed_origins():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _check_origin(self):
        """API 接口：Origin 头如果存在，必须是配置里的服务端来源。"""
        origin = self.headers.get("Origin")
        if origin and origin not in CONFIG.allowed_origins():
            self._send_json(403, {"error": "origin not allowed: %s" % origin})
            return False
        return True

    def _require_auth(self):
        token = self.headers.get("X-Client-Token", "")
        if not token or not hmac.compare_digest(token, CONFIG.token or ""):
            self._send_json(401, {"error": "unauthorized"})
            return False
        return True

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _read_json(self):
        raw = self._read_body()
        if not raw:
            return {}, None
        try:
            d = json.loads(raw.decode("utf-8"))
            return (d if isinstance(d, dict) else {"_list": d}), None
        except Exception as e:
            return None, "invalid json: %s" % e

    # -- 路由 ----------------------------------------------------------------
    def do_OPTIONS(self):
        origin = self.headers.get("Origin")
        self.send_response(204)
        if origin and origin in CONFIG.allowed_origins():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "X-Client-Token, Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if path == "/health":
            n_server = sum(1 for v in STATE.files.values() if v.get("src") == "server")
            self._send_json(200, {
                "ok": True,
                "version": VERSION,
                "mode": "session" if SESSION.root else "global",
                "client_id": CONFIG.client_id,
                "root": SESSION.root or CONFIG.root,
                "roots": [SESSION.root] if SESSION.root else [],
                "session_root": SESSION.root,
                "session_files": len(SESSION.files),
                "session_done": len(SESSION.done),
                "files_indexed": len(STATE.files),
                "files_server": n_server,
                "files_scanned": len(STATE.files) - n_server,
                "last_scan": STATE.last_scan,
                "server_url": CONFIG.server_url,
                "server_reached": SERVER_STATE["reached"],
            })
            return
        if path == "/session.json":
            # 会话文件清单（需配对码；页面外壳 /session 不鉴权）
            if not self._require_auth():
                return
            with SESSION.lock:
                files = [{"hash": info.get("hash"), "path": p,
                          "filename": os.path.basename(p),
                          "size": info.get("size", 0),
                          "status": info.get("status") or "new",
                          "action_at": info.get("action_at")}
                         for p, info in sorted(SESSION.files.items())]
                done = [{"path": p, "filename": v.get("filename"),
                         "action": v.get("action"), "new_path": v.get("new_path"),
                         "at": v.get("at")}
                        for p, v in sorted(SESSION.done.items())]
            self._send_json(200, {
                "ok": True, "root": SESSION.root, "files": files, "done": done,
                "session_file": os.path.basename(SESSIONS_FILE),
                "dests": {"collect": CONFIG.collect_dest,
                          "archive": CONFIG.archive_dest,
                          "star5": CONFIG.star5_dest},
            })
            return
        if path == "/session":
            self._send_html(200, SESSION_PAGE)
            return
        if path == "/":
            n_server = sum(1 for v in STATE.files.values() if v.get("src") == "server")
            mode_line = ("新路径会话：%s（临时独立）" % SESSION.root) if SESSION.root \
                else "全局（服务端索引，无需输入路径）"
            html = ("<!doctype html><meta charset=utf-8><title>Localdy 客户端</title>"
                    "<h1>Localdy 客户端 v%s</h1>"
                    "<p>模式：%s</p>"
                    "<p>会话文件：%d 个</p>"
                    "<p>全局索引：%d 个文件（服务端同步 %d，本地扫描 %d）</p>"
                    "<p>服务端：%s（%s）</p>"
                    "<p>配对码请查看客户端控制台窗口（每次启动都会显示）。</p>"
                    "<p>新路径会话页：<a href=\"/session\">/session</a></p>"
                    % (VERSION, mode_line, len(SESSION.files),
                       len(STATE.files), n_server, len(STATE.files) - n_server,
                       CONFIG.server_url or "未连接",
                       "已连接" if SERVER_STATE["reached"] else "未连接"))
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path.startswith("/media/"):
            self._handle_media(path[len("/media/"):], qs)
            return
        if path.startswith("/jobs/"):
            job = JOBS.get(path[len("/jobs/"):])
            if not job:
                self._send_json(404, {"error": "job not found"})
                return
            self._send_json(200, {k: v for k, v in job.items()
                                  if k != "_payload"})
            return
        if path == "/jobs":
            self._send_json(200, {"jobs": JOBS.list_recent()})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/scan":
            if not self._check_origin() or not self._require_auth():
                self._read_body()  # 保持连接状态干净
                return
            self._read_body()
            try:
                # 只刷新当前会话文件夹；全局模式没有本地扫描目录。
                if not SESSION.root:
                    raise RuntimeError(
                        "全局模式无本地扫描目录；"
                        "新路径会话用命令行 --root <路径> 启动")
                total, _n = scan_session(SESSION.root)
                self._send_json(200, {
                    "ok": True,
                    "root": SESSION.root,
                    "total": total,
                    "session_files": len(SESSION.files),
                })
            except Exception as e:
                self._send_json(500, {"error": "%s: %s" % (type(e).__name__, e)})
            return
        if path == "/media-tickets":
            if not self._check_origin() or not self._require_auth():
                self._read_body()
                return
            body, err = self._read_json()
            if err:
                self._send_json(400, {"error": err})
                return
            hashes = body.get("hashes") or []
            out, missing = {}, []
            for h in hashes:
                if find_path_anywhere(h):
                    out[h] = TICKETS.issue(h)
                else:
                    missing.append(h)
            if missing:
                # 懒同步：服务端可能刚索引了新视频（或本机刚 --root 上报完
                # 还没同步），即时拉一次再试；5 秒内重复缺失不再拉全表。
                now = time.time()
                if now - _LAST_LAZY_SYNC["at"] >= LAZY_SYNC_MIN_INTERVAL:
                    _LAST_LAZY_SYNC["at"] = now
                    ok, _detail = sync_global_map(force=True)
                    if ok:
                        still = []
                        for h in missing:
                            if find_path_anywhere(h):
                                out[h] = TICKETS.issue(h)
                            else:
                                still.append(h)
                        missing = still
            self._send_json(200, {"tickets": out, "missing": missing,
                                  "ttl_seconds": TICKET_TTL})
            return
        if path == "/jobs":
            if not self._check_origin() or not self._require_auth():
                self._read_body()
                return
            body, err = self._read_json()
            if err:
                self._send_json(400, {"error": err})
                return
            if not body.get("type"):
                self._send_json(400, {"error": "缺少 type"})
                return
            job = JOBS.create(body, source="api")
            self._send_json(202, {"job_id": job["id"], "status": "queued"})
            return
        if path == "/session/clear":
            # 忘掉当前会话的记录（本地文件里删掉这一条，内存里的会话也清掉）
            if not self._check_origin() or not self._require_auth():
                self._read_body()
                return
            self._read_body()
            root = SESSION.root
            clear_session_record()
            self._send_json(200, {"ok": True, "cleared": root})
            return
        self._read_body()
        self._send_json(404, {"error": "not found"})

    # -- 视频流 --------------------------------------------------------------
    def _handle_media(self, file_hash, qs):
        ticket = (qs.get("ticket") or [None])[0]
        if not ticket or not TICKETS.check(ticket, file_hash):
            self._send_json(401, {"error": "invalid or expired ticket"})
            return
        path = find_path_anywhere(file_hash)
        if not path or not os.path.isfile(path):
            self._send_json(404, {"error": "file not found（服务端记录的路径在本地不存在）"})
            return
        try:
            size = os.path.getsize(path)
        except OSError as e:
            self._send_json(404, {"error": str(e)})
            return
        ctype = mimetypes.guess_type(path)[0]
        if not ctype:
            ctype = "video/mp4" if os.path.splitext(path)[1].lower() in VIDEO_EXTS \
                else "application/octet-stream"
        range_hdr = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if range_hdr:
            m = _RANGE_RE.match(range_hdr)
            if not m:
                self._send_json(400, {"error": "bad Range header"})
                return
            s, e = m.group(1), m.group(2)
            try:
                if s == "" and e != "":
                    # bytes=-N：最后 N 字节
                    start = max(0, size - int(e))
                elif s != "":
                    start = int(s)
                    end = int(e) if e != "" else size - 1
                else:
                    raise ValueError
            except ValueError:
                self._send_json(400, {"error": "bad Range header"})
                return
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self._cors_headers()
                self.end_headers()
                return
            end = min(end, size - 1)
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(length))
        self._cors_headers()
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    data = f.read(min(CHUNK, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 浏览器 seek/关闭是正常情况

    def do_HEAD(self):
        # 视频探测常用 HEAD：走同一条媒体路径（票据同样校验）
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/media/"):
            qs = urllib.parse.parse_qs(parsed.query)
            self._handle_media(parsed.path[len("/media/"):], qs)
            return
        self._send_json(404, {"error": "not found"})


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)  # 控制台/重定向都实时出日志
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Localdy 单文件客户端 v%s" % VERSION)
    ap.add_argument("--root", help="启动一个新路径会话（只看这个文件夹，临时独立，不影响全局模式）")
    ap.add_argument("--server", help="服务端地址（会优先尝试并记住）")
    ap.add_argument("--port", type=int, help="监听端口（默认 %d）" % DEFAULT_PORT)
    ap.add_argument("--bind", help="监听地址（默认 0.0.0.0；只本机用填 127.0.0.1）")
    args = ap.parse_args()

    CONFIG.load()
    STATE.load()

    # 命令行参数覆盖配置，并保存（下次双击沿用）。
    # 注意：--root 不写配置——新路径会话是临时的，下次换个新路径。
    session_root = None
    if args.root:
        rp = os.path.abspath(args.root)
        if not os.path.isdir(rp):
            print("命令行指定的视频目录不存在：%s" % rp)
            sys.exit(1)
        session_root = rp
    changed = False
    if args.port:
        CONFIG.port = args.port
        changed = True
    if args.bind:
        CONFIG.bind = args.bind
        changed = True
    if not CONFIG.token:
        CONFIG.token = secrets.token_urlsafe(32)
        changed = True
    if not CONFIG.client_id:
        host = re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname() or "client")
        CONFIG.client_id = "%s-%s" % (host.lower()[:24], secrets.token_hex(3))
        changed = True
    if changed:
        CONFIG.save()

    # 全局模式：不再问路径。旧版单 root 配置已在 load() 里迁入 roots，
    # 但 v0.3.0 起不再用“管理的根目录”这个概念（见新路径会话）。

    # 新路径会话：先扫文件夹（只放内存），再连服务端。扫盘不需要服务端。

    # 服务端发现：命令行 --server > 上次记住的 > 默认列表
    candidates = []
    if args.server:
        candidates.append(args.server)
    if CONFIG.server_url:
        candidates.append(CONFIG.server_url)
    candidates.extend(CONFIG.server_urls or [])
    server_url, st = discover_server(candidates)
    if server_url:
        CONFIG.server_url = server_url
        CONFIG.save()
        SERVER_STATE["reached"] = True
        # 全局模式：启动时同步一次服务端索引（失败也不阻塞启动，
        # 先用本地缓存，心跳/票据缺失时还会再同步）
        ok, detail = sync_global_map(force=True)
        if not ok:
            log("[sync] 全局索引同步失败（%s），先用本地缓存的 %d 个条目"
                % (detail, len(STATE.files)))
    else:
        log("[server] 未找到服务端（将继续本地运行，心跳线程会重试）：%s"
            % ", ".join(candidates))

    # 新路径会话：扫文件夹（只放内存，不上报、不写盘）
    if session_root:
        scan_session(session_root)

    print()
    print("=" * 60)
    print("  Localdy 客户端 v%s" % VERSION)
    print("-" * 60)
    print("  监听地址 : %s:%d%s" % (CONFIG.bind, CONFIG.port,
          "（仅本机）" if CONFIG.bind in ("127.0.0.1", "localhost") else "（局域网可访问）"))
    if SESSION.root:
        print("  模式     : 新路径会话（仅看 %s，临时独立）" % SESSION.root)
        print("  会话文件 : %d 个（只放内存，不进全局索引）" % len(SESSION.files))
        print("  会话记录 : localdy_sessions.json（已处理 %d 个，存在客户端本地）"
              % len(SESSION.done))
    else:
        print("  模式     : 全局（服务端索引，无需输入路径）")
    n_server = sum(1 for v in STATE.files.values() if v.get("src") == "server")
    print("  全局索引 : %d 个文件（服务端同步 %d，本地扫描 %d）"
          % (len(STATE.files), n_server, len(STATE.files) - n_server))
    print("  服务端   : %s" % (CONFIG.server_url or "未连接（本地模式）"))
    print("  客户端ID : %s" % CONFIG.client_id)
    print("  配对码   : %s" % CONFIG.token)
    print("           ↑ 在网页上输入一次即可配对（每台设备的浏览器各输一次）")
    print("=" * 60)
    print("  会话页：/session　　播放：GET /media/<hash>?ticket=xxx")
    print("  加新文件夹：python localdy_client.py --root <路径>")
    print("  停止：按 Ctrl+C")
    print()

    server = ThreadingHTTPServer((CONFIG.bind, CONFIG.port), Handler)
    server.daemon_threads = True
    threading.Thread(target=JOBS.worker, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n客户端已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
