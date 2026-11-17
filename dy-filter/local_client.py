# -*- coding: utf-8 -*-
"""
local_client.py — dy-filter「本地来源」Windows 客户端（仅标准库，无需安装依赖）
================================================================================
在 Windows 电脑上运行，扫描本机视频目录，生成一个订阅链接；
dy-filter（Mac 服务端）通过该链接同步视频列表，浏览器在 dy-filter 页面里
筛选、打标；结算时 Mac 服务端调回本客户端移动文件。

用法（Windows）：
    python local_client.py --root "D:\\Videos" [--root "E:\\更多视频"] [--port 18950]
    python local_client.py --root "D:\\Videos" --cache dy-cache-manifest.json
        用服务端导出的缓存清单跳过读文件算哈希：清单里有的文件直接复用
        清单里的哈希，不读文件现算；清单里没有的照常现算头部哈希。不传
        --cache 时行为与原来完全一致。清单在 dy-filter 任务中心「本地来源」
        卡片下载。清单条目带 sha1_kind：'head' 是头部哈希，'full' 是 115
        全文件哈希（从 115 下载到本地的大文件用这个，其 Video.sha1 本就是
        全哈希，与 115 库内行一致）；旧版清单没有 sha1_kind 时按 'head' 处理。
    python local_client.py --hash "D:\\Videos\\某个广告.mp4"   只算一个文件的头部哈希并打印

首次运行会在脚本同目录生成 local_client_token.txt（随机 token，只存本机），
并打印订阅链接，例如：
    http://192.168.31.10:18950/sub?token=xxxxxxxx
把这个链接粘贴到 dy-filter 任务中心「本地来源」卡片，点「同步本地」。

头部哈希（去重 / 不喜欢数据集用）：
    sha1 = SHA1(文件前 1,048,576 字节 = 1MB)；不足 1MB 的文件取全部。
    只读文件头部，多 GB 视频也很快；服务端把它存进 Video.sha1 字段，
    与 115 的全文件 SHA1 共用同一字段、同一格式（底层兼容）：
    同一内容改名/换目录仍能命中去重；不喜欢数据集里已有的头部哈希
    会在同步时自动跳过。取广告视频哈希：用上面 --hash 模式打印出来，
    写进 .txt（每行一个）后在 dy-filter「不喜欢数据」里点「后台导入」。

接口（全部需要 token；token 可放 ?token= 查询串或 Authorization: Bearer）：
    GET  /sub?token=...            订阅：{version, host, roots, videos:[{id,name,size,mtime,rel_path,url}]}
    GET  /file/<id>?token=...      视频直链：支持 HTTP Range（206），可拖进度条
    GET  /health?token=...         健康检查：{ok, videos, roots}
    POST /api/settle?token=...     结算：{moves:[{id, dest}], deletes:[id]}
                                   moves: 精选 -> <根目录>/<dest>/<相对路径>
                                   deletes: 淘汰 -> <根目录>/_dy_回收站/<相对路径>
                                   只移动文件，永不永久删除；目标已存在时自动加 (1)(2)… 后缀。

约定：
  - 视频扩展名：.mp4 .mkv .mov .avi .flv .wmv .webm .m4v .ts .rmvb
  - 文件 id = sha1(根目录绝对路径 + '|' + 相对路径)，同一台电脑上稳定；
    文件改名/移动后 id 会变（按下次同步为准）。
  - 扫描时跳过 _dy_精选 与 _dy_回收站 目录（已结算的文件不再重新出现）。
  - 只监听本机端口，token 是唯一凭证：不要把订阅链接发给别人。
"""
import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse, parse_qs

VERSION = 3
DEFAULT_PORT = 18950
HEADER_HASH_BYTES = 1048576  # 头部哈希固定取前 1MB；不足 1MB 的文件取全部（与服务端约定，永不改动）
VIDEO_EXTS = {'.mp4', '.mkv', '.mov', '.avi', '.flv', '.wmv', '.webm', '.m4v', '.ts', '.rmvb'}
SKIP_DIR_NAMES = {'_dy_精选', '_dy_回收站'}
RECYCLE_FOLDER = '_dy_回收站'
TOKEN_FILE = 'local_client_token.txt'
CHUNK = 65536

# 扩展名 -> Content-Type（mimetypes 在 Windows 上可能缺失 mkv/ts，显式补齐）
CONTENT_TYPES = {
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mkv': 'video/x-matroska',
    '.mov': 'video/quicktime', '.avi': 'video/x-msvideo', '.flv': 'video/x-flv',
    '.wmv': 'video/x-ms-wmv', '.webm': 'video/webm', '.ts': 'video/mp2t',
    '.rmvb': 'application/vnd.rn-realmedia',
}


def log(*args):
    print('[local-client]', *args, flush=True)


def detect_lan_ip():
    """取本机访问外网时用的局域网 IPv4（只建 UDP socket，不发流量）。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(('8.8.8.8', 80))
            ip = sock.getsockname()[0]
        finally:
            sock.close()
        if ip and not ip.startswith('127.'):
            return ip
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return '127.0.0.1'


def load_or_create_token(script_dir):
    path = os.path.join(script_dir, TOKEN_FILE)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            token = f.read().strip()
        if len(token) >= 16:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        # 0600：仅本机用户可读（Windows 上基本无效果，但无害）
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(token + '\n')
    except OSError:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(token + '\n')
    return token


def file_id_for(root_abs, rel_path):
    """稳定的文件 id：根目录绝对路径 + 相对路径（/ 分隔）的 sha1。"""
    key = os.path.normcase(root_abs) + '|' + rel_path.replace(os.sep, '/')
    return hashlib.sha1(key.encode('utf-8')).hexdigest()


def header_sha1(path):
    """文件头部哈希：SHA1(前 1MB)；不足 1MB 时取整个文件。

    只读文件头部，多 GB 视频也很快。返回小写 40 位 hex；读失败返回 ''。
    服务端收到后统一转大写存入 Video.sha1（与不喜欢数据集的格式一致）。
    """
    try:
        h = hashlib.sha1()
        with open(path, 'rb') as f:
            h.update(f.read(HEADER_HASH_BYTES))
        return h.hexdigest()
    except OSError:
        return ''


def load_cache_manifest(path):
    """加载服务端导出的缓存清单（dy-cache-manifest.json）。

    返回 {rel_path: (size, sha1, sha1_kind)}，sha1_kind 为 'head'（头部
    哈希）或 'full'（115 全文件哈希）；旧版清单没有 sha1_kind 时按 'head'
    处理；sha1_kind 非法时该条目跳过（回落现算）。清单无效 / 不存在 /
    格式不对时返回空 dict（回落传统路径：全部现算头部哈希），只记日志，
    不报错。清单里的 sha1 即要复用的哈希值（无论哪种形式），大小不一致
    时视为未命中。
    """
    if not path:
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        log('缓存清单无法读取（%s），本次全部现算头部哈希：' % exc, path)
        return {}
    entries = data.get('entries') if isinstance(data, dict) else None
    if not isinstance(entries, list):
        log('缓存清单格式无效，本次全部现算头部哈希：', path)
        return {}
    out = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        rel = (e.get('rel_path') or '').replace('\\', '/').strip()
        if not rel or rel.startswith('/') or '..' in rel.split('/'):
            continue
        sha1 = (e.get('sha1') or '').strip().lower()
        if len(sha1) != 40 or not all(c in '0123456789abcdef' for c in sha1):
            continue
        kind = (e.get('sha1_kind') or 'head').strip().lower()
        if kind not in ('head', 'full'):
            continue
        try:
            size = int(e.get('size') or 0)
        except (TypeError, ValueError):
            continue
        if size < 0:
            continue
        out[rel] = (size, sha1, kind)
    return out


def scan_roots(roots, cache=None):
    """扫描所有根目录，返回 (videos, index)。

    videos: [{id, name, size, mtime, rel_path, root_index, sha1}]
    index: {id: absolute_path}

    cache（可选）：服务端导出的缓存清单 {rel_path: (size, sha1, sha1_kind)}。
    命中（相对路径一致且大小一致）时直接复用清单里的哈希（无论头部哈希
    还是 115 全文件哈希），不读文件算哈希；未命中时照常现算头部哈希。
    清单缺失 / 为空时行为与原来完全一致。返回的 videos 每项带 '_cache_hit'
    （True=复用缓存，False=现算）和 '_cache_kind'（命中时为 'head'/'full'，
    未命中时为 ''），供统计用。
    """
    cache = cache or {}
    videos = []
    index = {}
    cache_hits = 0
    for root_index, root in enumerate(roots):
        root_abs = os.path.abspath(root)
        if not os.path.isdir(root_abs):
            log('根目录不存在，跳过：', root_abs)
            continue
        for dirpath, dirnames, filenames in os.walk(root_abs, followlinks=False):
            # 跳过已结算输出目录（不让已移动的文件重新出现为待筛）
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIR_NAMES and not d.startswith('.')]
            for name in filenames:
                ext = os.path.splitext(name)[1].lower()
                if ext not in VIDEO_EXTS:
                    continue
                abs_path = os.path.join(dirpath, name)
                try:
                    st = os.stat(abs_path)
                except OSError:
                    continue
                rel = os.path.relpath(abs_path, root_abs).replace(os.sep, '/')
                fid = file_id_for(root_abs, rel)
                hit = cache.get(rel)
                if hit is not None and hit[0] == int(st.st_size):
                    sha1 = hit[1]
                    cache_hits += 1
                    from_cache = True
                    cache_kind = hit[2] if len(hit) > 2 else 'head'
                else:
                    sha1 = header_sha1(abs_path)
                    from_cache = False
                    cache_kind = ''
                videos.append({
                    'id': fid,
                    'name': name,
                    'size': int(st.st_size),
                    'mtime': int(st.st_mtime),
                    'rel_path': rel,
                    'root_index': root_index,
                    'sha1': sha1,
                    '_cache_hit': from_cache,
                    '_cache_kind': cache_kind,
                })
                index[fid] = abs_path
    videos.sort(key=lambda v: (v['root_index'], v['rel_path']))
    return videos, index, cache_hits


def validate_dest(dest):
    """结算目标文件夹名：单个文件夹名，不含路径分隔符，不超过 50 字，不能是回收站。"""
    if not isinstance(dest, str):
        return None
    dest = dest.strip()
    if not dest or len(dest) > 50:
        return None
    if '/' in dest or '\\' in dest or dest in ('.', '..', RECYCLE_FOLDER):
        return None
    if '\x00' in dest:
        return None
    return dest


def unique_target(path):
    """目标已存在时加 (1)(2)… 后缀，永不覆盖已有文件。"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while True:
        candidate = '%s (%d)%s' % (base, n, ext)
        if not os.path.exists(candidate):
            return candidate
        n += 1


class ClientState:
    def __init__(self, roots, token, host_ip, port, cache=None):
        self.roots = [os.path.abspath(r) for r in roots]
        self.token = token
        self.host_ip = host_ip
        self.port = port
        self.cache = cache or {}  # 服务端导出的缓存清单 {rel_path: (size, sha1, sha1_kind)}
        self.cache_hits = 0       # 本轮扫描复用缓存的条数
        self.lock = threading.RLock()
        self.videos = []
        self.index = {}

    def rescan(self):
        with self.lock:
            self.videos, self.index, self.cache_hits = scan_roots(self.roots, self.cache)
            return self.videos

    def base_url(self):
        return 'http://%s:%d' % (self.host_ip, self.port)

    def sub_payload(self):
        videos = self.rescan()
        items = []
        for v in videos:
            items.append({
                'id': v['id'],
                'name': v['name'],
                'size': v['size'],
                'mtime': v['mtime'],
                'rel_path': v['rel_path'],
                'sha1': v.get('sha1') or '',
                'url': '%s/file/%s?token=%s' % (self.base_url(), v['id'], quote(self.token, safe='')),
            })
        return {
            'version': VERSION,
            'host': self.host_ip,
            'port': self.port,
            'roots': self.roots,
            'videos': items,
        }

    def settle(self, moves, deletes):
        """执行结算移动。moves: [{id, dest}]，deletes: [id]。

        返回 {results: [{id, ok, error?, new_rel_path?}]}。
        每项独立成败；成功后丢弃旧索引（下次 /sub 重新扫描）。
        """
        results = []
        changed = False

        def _move_one(fid, dest_folder):
            with self.lock:
                abs_path = self.index.get(fid)
            if not abs_path or not os.path.isfile(abs_path):
                with self.lock:
                    self.rescan()
                    abs_path = self.index.get(fid)
                if not abs_path or not os.path.isfile(abs_path):
                    return False, '文件不存在（可能已移动或删除）', None
            with self.lock:
                video = next((v for v in self.videos if v['id'] == fid), None)
            if video is None:
                return False, '文件不在索引中', None
            root_abs = self.roots[video['root_index']]
            rel = video['rel_path']
            target_dir = os.path.join(root_abs, dest_folder, os.path.dirname(rel))
            os.makedirs(target_dir, exist_ok=True)
            target = unique_target(os.path.join(target_dir, os.path.basename(rel)))
            try:
                shutil.move(abs_path, target)
            except OSError as exc:
                return False, '移动失败：%s' % exc, None
            new_rel = os.path.relpath(target, root_abs).replace(os.sep, '/')
            return True, None, new_rel

        for item in moves or []:
            fid = item.get('id') if isinstance(item, dict) else None
            dest = validate_dest(item.get('dest')) if isinstance(item, dict) else None
            if not fid:
                results.append({'id': None, 'ok': False, 'error': '缺少文件 id'})
                continue
            if not dest:
                results.append({'id': fid, 'ok': False, 'error': '目标文件夹名无效'})
                continue
            ok, error, new_rel = _move_one(fid, dest)
            results.append({'id': fid, 'ok': ok, **({'error': error} if error else {}),
                            **({'new_rel_path': new_rel} if new_rel else {})})
            changed = changed or ok
        for fid in deletes or []:
            ok, error, new_rel = _move_one(fid, RECYCLE_FOLDER)
            results.append({'id': fid, 'ok': ok, **({'error': error} if error else {}),
                            **({'new_rel_path': new_rel} if new_rel else {})})
            changed = changed or ok
        if changed:
            with self.lock:
                self.videos, self.index = [], {}
        return {'results': results}


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            sys.stderr.write('[local-client] %s - %s\n' % (self.address_string(), fmt % args))

        # ---- 通用 ----
        def _cors(self):
            # 浏览器从 dy-filter 页面（可能跨域、跨协议）直连订阅时需要 CORS；
            # 实际拉取仍需 token，CORS 只放行浏览器读取响应，不放行未授权访问。
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, Range, Authorization')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Access-Control-Expose-Headers', 'Content-Length, Content-Range, Accept-Ranges')

        def _send_json(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self._cors()
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def _auth_ok(self, query):
            token = ''
            if 'token' in query:
                token = query['token'][0]
            elif (self.headers.get('Authorization') or '').startswith('Bearer '):
                token = self.headers.get('Authorization')[7:].strip()
            return bool(token) and hmac.compare_digest(token, state.token)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self._cors()
            self.end_headers()

        def do_GET(self):
            self._handle(head_only=False)

        def do_HEAD(self):
            self._handle(head_only=True)

        def _handle(self, head_only=False):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            path = parsed.path
            if not self._auth_ok(query):
                self._send_json(401, {'ok': False, 'error': '缺少或错误的 token'})
                return
            if path == '/health':
                with state.lock:
                    count = len(state.index)
                self._send_json(200, {'ok': True, 'version': VERSION,
                                      'videos': count, 'roots': state.roots})
                return
            if path == '/sub':
                try:
                    payload = state.sub_payload()
                except OSError as exc:
                    self._send_json(500, {'ok': False, 'error': '扫描失败：%s' % exc})
                    return
                self._send_json(200, payload)
                return
            m = re.fullmatch(r'/file/([0-9a-f]{40})', path)
            if m:
                self._serve_file(m.group(1), head_only)
                return
            self._send_json(404, {'ok': False, 'error': '未知接口'})

        def _serve_file(self, fid, head_only):
            with state.lock:
                abs_path = state.index.get(fid)
            if not abs_path or not os.path.isfile(abs_path):
                state.rescan()
                with state.lock:
                    abs_path = state.index.get(fid)
                if not abs_path or not os.path.isfile(abs_path):
                    self._send_json(404, {'ok': False, 'error': '文件不存在'})
                    return
            try:
                size = os.path.getsize(abs_path)
            except OSError:
                self._send_json(404, {'ok': False, 'error': '文件不存在'})
                return
            ext = os.path.splitext(abs_path)[1].lower()
            ctype = CONTENT_TYPES.get(ext) or mimetypes.guess_type(abs_path)[0] or 'application/octet-stream'
            range_header = (self.headers.get('Range') or '').strip()
            start, end = 0, size - 1
            status = 200
            if range_header:
                m = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header)
                if m and (m.group(1) or m.group(2)):
                    if m.group(1) and m.group(2):
                        start, end = int(m.group(1)), int(m.group(2))
                    elif m.group(1):
                        start = int(m.group(1))
                    else:
                        start = max(0, size - int(m.group(2)))
                    if start >= size or end < start:
                        self.send_response(416)
                        self.send_header('Content-Range', 'bytes */%d' % size)
                        self.send_header('Content-Length', '0')
                        self._cors()
                        self.end_headers()
                        return
                    end = min(end, size - 1)
                    status = 206
            length = end - start + 1
            try:
                f = open(abs_path, 'rb')
            except OSError:
                self._send_json(404, {'ok': False, 'error': '文件无法打开'})
                return
            try:
                f.seek(start)
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(length))
                self.send_header('Accept-Ranges', 'bytes')
                if status == 206:
                    self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
                self._cors()
                self.end_headers()
                if head_only:
                    return
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    remaining -= len(chunk)
            finally:
                f.close()

        def do_POST(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != '/api/settle':
                self._send_json(404, {'ok': False, 'error': '未知接口'})
                return
            if not self._auth_ok(query):
                self._send_json(401, {'ok': False, 'error': '缺少或错误的 token'})
                return
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 1024 * 1024:
                self._send_json(400, {'ok': False, 'error': '请求体无效'})
                return
            try:
                body = json.loads(self.rfile.read(length).decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                self._send_json(400, {'ok': False, 'error': '请求体不是有效 JSON'})
                return
            if not isinstance(body, dict):
                self._send_json(400, {'ok': False, 'error': '请求体必须是 JSON 对象'})
                return
            try:
                result = state.settle(body.get('moves'), body.get('deletes'))
            except OSError as exc:
                self._send_json(500, {'ok': False, 'error': '结算失败：%s' % exc})
                return
            result['ok'] = True
            self._send_json(200, result)

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description='dy-filter 本地来源客户端：扫描本机视频目录并生成订阅链接')
    parser.add_argument('--root', action='append', default=None,
                        help='视频根目录，可重复指定，例如 --root "D:\\Videos"')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help='监听端口（默认 18950）')
    parser.add_argument('--host', default='0.0.0.0', help='监听地址（默认 0.0.0.0，仅局域网）')
    parser.add_argument('--hash', metavar='FILE', default=None,
                        help='只计算一个文件的头部 SHA1 并打印，不启动服务（用于收集广告视频哈希，导入不喜欢数据集）')
    parser.add_argument('--cache', metavar='MANIFEST', default=None,
                        help='服务端导出的缓存清单 dy-cache-manifest.json（可选）：清单里有的文件直接复用清单里的哈希（115 文件用全文件哈希、本地文件用头部哈希），不读文件现算；没有的照常现算头部哈希。不传时行为与原来完全一致')
    args = parser.parse_args(argv)

    if args.hash:
        path = args.hash
        if not os.path.isfile(path):
            print('文件不存在：' + path, file=sys.stderr)
            return 2
        size = os.path.getsize(path)
        sha1 = header_sha1(path)
        if not sha1:
            print('读取失败：' + path, file=sys.stderr)
            return 2
        print('文件：%s' % os.path.abspath(path))
        print('大小：%d 字节（头部哈希取前 %d 字节）' % (size, min(size, HEADER_HASH_BYTES)))
        print('头部 SHA1：%s' % sha1.upper())
        return 0
    if not args.root:
        parser.error('请至少指定一个 --root，或用 --hash 计算单个文件的头部哈希')

    script_dir = os.path.dirname(os.path.abspath(__file__))
    token = load_or_create_token(script_dir)
    host_ip = detect_lan_ip()
    cache = load_cache_manifest(args.cache) if args.cache else {}
    state = ClientState(args.root, token, host_ip, args.port, cache=cache)
    videos = state.rescan()
    sub_url = '%s/sub?token=%s' % (state.base_url(), quote(token, safe=''))

    print('=' * 72)
    print('dy-filter 本地来源客户端已启动')
    print('  根目录：' + '、'.join(state.roots))
    print('  视频数：%d 个' % len(videos))
    if args.cache:
        print('  缓存清单：%s（%d 条）' % (args.cache, len(cache)))
        print('  缓存复用：%d 个文件直接用清单哈希，未现算；其余 %d 个现算头部哈希'
              % (state.cache_hits, len(videos) - state.cache_hits))
    print('  订阅链接（粘贴到 dy-filter 任务中心「本地来源」卡片）：')
    print('  ' + sub_url)
    print('  token 保存在：' + os.path.join(script_dir, TOKEN_FILE))
    print('  按 Ctrl+C 停止。')
    print('=' * 72, flush=True)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
