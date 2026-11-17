# -*- coding: utf-8 -*-
"""
local_source.py — dy-filter「本地来源」服务端支持
=========================================================================
本地来源 = 用户 Windows 电脑上跑的 local_client.py（见同目录 local_client.py）。
Mac 服务端只保存客户端的地址 + token（db/local_source.json，0600），
token 永不返回给前端；播放与订阅拉取都由服务端中转（浏览器不能从 HTTPS 页面
直接访问局域网 http:// 客户端——混合内容拦截）。

本模块只做本地来源的事，不碰任何 115 接口。
"""
import json
import os
import threading
from urllib.parse import urlparse, parse_qs

import requests

from utils import DB_DIR

LOCAL_SOURCE_FILE = os.path.join(DB_DIR, 'local_source.json')
LOCAL_RECYCLE_FOLDER = '_dy_回收站'
LOCAL_SETTLE_FOLDER_DEFAULT = '_dy_精选'
LOCAL_SUB_MAX_VIDEOS = 20000
# 头部哈希（本地来源）：SHA1(文件前 1,048,576 字节 = 1MB)；不足 1MB 的文件取全部。
# 由客户端在扫描时计算，随订阅发来；服务端存入 Video.sha1 字段（大写 40 位 hex），
# 与 115 的全文件 SHA1 共用同一字段、同一格式，所以已有的不喜欢数据集
# 与去重逻辑对两个来源完全一样。注意：本地 sha1 是头部部分哈希，不是全文件
# 哈希——它与云端全文件哈希在 160 位空间里随机碰撞的概率可忽略。
# 旧版客户端（无 sha1 字段）的订阅仍可同步：该视频无哈希，只跳过去重与
# 不喜欢匹配，不报错。
_HEX40 = set('0123456789abcdefABCDEF')
_local_source_lock = threading.RLock()


def load_local_source():
    """读本地来源配置；未配置返回 {}。"""
    with _local_source_lock:
        try:
            with open(LOCAL_SOURCE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data


def save_local_source(base_url, token, roots, host):
    """保存本地来源配置（原子写，0600）。token 只存服务端，不返回前端。"""
    import datetime
    value = {
        'base_url': base_url,
        'token': token,
        'roots': list(roots or []),
        'host': host or '',
        'updated_at': datetime.datetime.now().isoformat(timespec='seconds'),
    }
    with _local_source_lock:
        os.makedirs(DB_DIR, exist_ok=True)
        tmp = LOCAL_SOURCE_FILE + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(value, f, ensure_ascii=False, indent=2)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.replace(tmp, LOCAL_SOURCE_FILE)
        try:
            os.chmod(LOCAL_SOURCE_FILE, 0o600)
        except OSError:
            pass
    return value


def parse_sub_url(sub_url):
    """解析订阅链接 -> (base_url, token)。base_url 只保留 scheme://netloc。"""
    if not isinstance(sub_url, str) or not sub_url.strip():
        raise ValueError('订阅链接为空')
    parsed = urlparse(sub_url.strip())
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('订阅链接无效：应为 http(s)://<局域网 IP>:<端口>/sub?token=…')
    token = (parse_qs(parsed.query).get('token') or [''])[0].strip()
    if len(token) < 16:
        raise ValueError('订阅链接缺少有效 token')
    base_url = '%s://%s' % (parsed.scheme, parsed.netloc)
    return base_url, token


def sanitize_rel_path(rel):
    """校验客户端上报的相对路径：不为空、无绝对路径、无 .. 段、无 NUL。返回 / 分隔的规范路径。"""
    if not isinstance(rel, str):
        return None
    rel = rel.replace('\\', '/').strip()
    if not rel or rel.startswith('/') or '\x00' in rel:
        return None
    parts = [p for p in rel.split('/') if p not in ('', '.')]
    if not parts or '..' in parts:
        return None
    return '/'.join(parts)


def validate_subscription_payload(payload):
    """校验订阅 JSON，返回归一化的视频列表 [{id, name, size, mtime, rel_path, sha1}]。

    sha1 可选：客户端新版发来头部哈希（SHA1 前 1MB，大写 40 位 hex 存入）；
    旧版客户端没有该字段时记 ''，同步时该视频跳过去重与不喜欢匹配，不报错。
    """
    if not isinstance(payload, dict):
        raise ValueError('订阅内容无效：应为 JSON 对象')
    videos = payload.get('videos')
    if not isinstance(videos, list):
        raise ValueError('订阅内容无效：缺少 videos 列表')
    if len(videos) > LOCAL_SUB_MAX_VIDEOS:
        raise ValueError('订阅视频过多（上限 %d 个）' % LOCAL_SUB_MAX_VIDEOS)
    out = []
    seen = set()
    for item in videos:
        if not isinstance(item, dict):
            raise ValueError('订阅内容无效：videos 元素应为对象')
        fid = item.get('id')
        if not isinstance(fid, str) or len(fid) != 40 or not all(c in '0123456789abcdef' for c in fid.lower()):
            raise ValueError('订阅内容无效：视频 id 应为 40 位 hex')
        fid = fid.lower()
        if fid in seen:
            continue
        seen.add(fid)
        name = (item.get('name') or '').strip()
        if not name:
            raise ValueError('订阅内容无效：视频缺少名称')
        try:
            size = int(item.get('size') or 0)
        except (TypeError, ValueError):
            raise ValueError('订阅内容无效：视频大小应为数字')
        if size < 0:
            raise ValueError('订阅内容无效：视频大小不能为负')
        rel = sanitize_rel_path(item.get('rel_path') or '')
        if not rel:
            raise ValueError('订阅内容无效：视频相对路径无效')
        try:
            mtime = int(item.get('mtime') or 0)
        except (TypeError, ValueError):
            mtime = 0
        sha1 = ''
        raw_sha1 = item.get('sha1')
        if isinstance(raw_sha1, str):
            s = raw_sha1.strip().upper()
            if len(s) == 40 and all(c in _HEX40 for c in s):
                sha1 = s
        out.append({'id': fid, 'name': name, 'size': size, 'mtime': mtime, 'rel_path': rel, 'sha1': sha1})
    return out


def fetch_subscription(sub_url, timeout=30):
    """服务端直连客户端拉取订阅 JSON（局域网内，无浏览器混合内容限制）。"""
    try:
        resp = requests.get(sub_url, timeout=timeout)
    except requests.RequestException as exc:
        raise ValueError('无法连接本地客户端（%s）：请确认电脑上的 local_client.py 正在运行，且与 Mac 在同一局域网' % exc)
    if resp.status_code == 401:
        raise ValueError('本地客户端拒绝访问：订阅链接的 token 无效，请用客户端重新打印的链接')
    if resp.status_code != 200:
        raise ValueError('本地客户端返回错误（HTTP %d）' % resp.status_code)
    try:
        payload = resp.json()
    except ValueError:
        raise ValueError('本地客户端返回的不是有效 JSON')
    if not isinstance(payload, dict) or not isinstance(payload.get('videos'), list):
        raise ValueError('本地客户端返回的订阅格式无效')
    return payload


def call_local_settle(base_url, token, moves, deletes, timeout=120):
    """调客户端 POST /api/settle 执行结算移动。返回解析后的 JSON。

    客户端不可达 / 返回错误时抛 RuntimeError（任务应标记失败，不静默完成）。
    """
    from urllib.parse import quote
    url = '%s/api/settle?token=%s' % (base_url.rstrip('/'), quote(token, safe=''))
    try:
        resp = requests.post(url, json={'moves': moves or [], 'deletes': deletes or []}, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError('无法连接本地客户端（%s），请确认电脑上的 local_client.py 正在运行且与 Mac 在同一局域网' % exc)
    if resp.status_code == 401:
        raise RuntimeError('本地客户端拒绝访问：token 无效，请重新同步本地来源')
    if resp.status_code != 200:
        raise RuntimeError('本地客户端返回错误（HTTP %d）' % resp.status_code)
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError('本地客户端返回的不是有效 JSON')
    if not isinstance(data, dict) or not isinstance(data.get('results'), list):
        raise RuntimeError('本地客户端返回的结算结果格式无效')
    return data
