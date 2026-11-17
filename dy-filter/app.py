# -*- coding: utf-8 -*-
"""
app.py — 核心路由层（Flask）
=========================================================================
115 云端在线预筛 + 本地 SQLite 状态库 + hls.js 流媒体预览 + Aria2 批量落盘。

对外前缀:
    /dy/    主入口（前端 index.html 使用）
    /dyfn/  兼容别名

新增/核心云端接口:
    GET  /dy/api/list                   查询本地 SQLite 待筛/指定状态记录 (<10ms)
    GET  /dy/api/play/<pickcode>        懒加载获取 115 m3u8/直链（带 TTL 缓存）
    POST /dy/api/mark                   快捷键打标（仅更新 SQLite，不阻塞 115）
    GET/POST /dy/api/cloud/config       115 Cookie、目录 CID/路径、Aria2 与本地目录配置
    POST /dy/api/cloud/sync             同步 115 待筛选目录并执行 SHA1 秒级去重
    POST /dy/api/cloud/commit           触发「结算归档并下载 (3%)」后台任务
    POST /dy/api/cloud/import_local_sha1 扫描本地历史精选目录导入 SHA1 去重库
    GET  /dy/api/cloud/dirs             列出 115 指定目录下的子文件夹（辅助查 CID）

启动:  python app.py          （默认 http://127.0.0.1:81）
=========================================================================
"""

import os
import re
import json
import random
import shutil
import subprocess
import datetime
import threading
from urllib.parse import urlparse, quote, urljoin

import requests

from flask import (
    Flask, Blueprint, request, jsonify, Response, stream_with_context,
    send_file, abort, redirect, current_app,
)
from sqlalchemy import func, or_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from werkzeug.exceptions import HTTPException

from utils import (
    INDEX_FILE, DB_FILE, THUMBNAIL_DIR, CURRENT_DIR,
    STATUS_PENDING, STATUS_STARRED, STATUS_REJECTED, STATUS_DOWNLOADED, STATUS_DISLIKED, VALID_STATUSES,
    init_db, Session, Video, VideoRandomOrder, RenameLog, VideoPreviewEvent, DislikedVideo,
    load_paths, save_paths,
    load_cloud_config, save_cloud_config, mask_cookie, p115_manager,
    parse_browser_cookie_json,
    apply_video_filters, score_to_status, status_to_score,
    BlacklistService, TagService, RenameService,
    _new_task, _tasks, _tasks_lock, _update_task,
    normalize_sha1, is_disliked, add_disliked, list_disliked, remove_disliked, count_disliked,
    library_operation_lock, serialized_library_operation,
    cache_lookup, cache_lookup_many, classify_received_sha1,
    cache_upsert, cache_upsert_many, cache_stats, cache_export_entries,
)
from local_source import (
    load_local_source, save_local_source, validate_subscription_payload,
    fetch_subscription, parse_sub_url,
)
from fingerprint_worker import (
    sync_cloud_videos, run_cloud_sync_task, import_local_history_sha1, cloud_poller,
)
from star_tasks import (
    task_service, get_config as get_star_config,
    save_config as save_star_config, ACTIONS as STAR_ACTIONS,
    is_size_folder_path, normalize_move_params,
)


_random_lock = threading.Lock()
_path_config_lock = threading.RLock()

# 服务端中间帧：ffmpeg 提取并发上限与超时（m3u8 中间位置截图）
_midframe_semaphore = threading.Semaphore(2)
_MIDFRAME_TIMEOUT = 30
_FFMPEG_BIN = shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'
_FFPROBE_BIN = shutil.which('ffprobe') or '/opt/homebrew/bin/ffprobe'

dy_bp = Blueprint('dy', __name__)


@dy_bp.before_request
def _setup():
    init_db()
    if request.method == 'POST' and request.is_json:
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError('请求内容必须为 JSON 对象')


# ================= 云端核心 API（高速打标与懒加载取链） =================
@dy_bp.route('/api/list', methods=['GET'])
def api_cloud_list():
    """直接查询本地 SQLite 状态库，默认返回 status='pending' 的待筛视频。"""
    status = (request.args.get('status') or STATUS_PENDING).strip()
    limit = max(1, min(1000, int(request.args.get('limit', 200))))
    offset = max(0, int(request.args.get('offset', 0)))
    source = (request.args.get('source') or 'all').strip()

    with Session() as session:
        q = session.query(Video).filter(or_(Video.source_type != 'local_history', Video.source_type.is_(None)))
        if source == 'cloud':
            q = q.filter(Video.source_type == 'cloud')
        elif source == 'local':
            q = q.filter(Video.source_type == 'local')
        elif source not in ('all', ''):
            raise ValueError('来源筛选无效')
        if status != 'all':
            if status not in VALID_STATUSES:
                raise ValueError('status 参数无效')
            q = q.filter(Video.status == status)
        total = q.count()
        rows = q.order_by(Video.id.asc()).offset(offset).limit(limit).all()

    items = [{
        'id': v.id,
        'file_id': v.file_id or '',
        'pickcode': v.pickcode or '',
        'sha1': v.sha1 or '',
        'name': v.filename,
        'filename': v.filename,
        'rel_path': v.rel_path or v.detail or v.filename,
        'size': v.file_size or 0,
        'duration': v.duration or 0,
        'thumb': v.thumb or '',
        'status': v.status or STATUS_PENDING,
        'score': v.score or 0,
        'source': v.source_type or 'cloud',
        'tags': v.tags or '',
        'preview_count': v.preview_count or 0,
    } for v in rows]
    return jsonify({'success': True, 'total': total, 'items': items})


@dy_bp.route('/api/play/<pickcode>', methods=['GET'])
def api_cloud_play(pickcode: str):
    """懒加载获取 115 云端 m3u8 转码流（或直链），内置 TTL 缓存防风控。"""
    force_refresh = request.args.get('refresh') == '1'
    url, stream_type, expire_at = p115_manager.get_stream_url(pickcode.strip(), force_refresh=force_refresh)
    if stream_type == 'hls':
        # 浏览器直连 115 会 403 + CORS 拦截，必须走同源代理。
        # 前端主播放器调的是这个接口（不是 /videos/<id>/stream），
        # 所以这里也要返回代理地址 /dy/videos/<id>/play。
        with Session() as session:
            row = session.query(Video).filter(Video.pickcode == pickcode.strip()).first()
            if row:
                url = f'/dy/videos/{row.id}/play'
    return jsonify({
        'success': True,
        'pickcode': pickcode,
        'url': url,
        'type': stream_type,
        'expire_at': expire_at,
    })


@dy_bp.route('/api/mark', methods=['POST'])
def api_cloud_mark():
    """接收前端快捷键打标，仅更新 SQLite 状态，不阻塞等待 115 网络请求。"""
    data = request.get_json() or {}
    status = data.get('status')
    score = data.get('score')

    if status is not None:
        if status not in VALID_STATUSES:
            raise ValueError('status 必须为 pending / starred / rejected / downloaded')
        target_status = status
        target_score = status_to_score(status)
    elif score is not None:
        if isinstance(score, bool) or not isinstance(score, int) or score not in range(6):
            raise ValueError('score 必须为 0–5 的整数')
        target_score = score
        target_status = score_to_status(score)
    else:
        raise ValueError('请提供 status 或 score')

    with Session() as session:
        video = None
        if data.get('id') is not None:
            video = session.get(Video, int(data['id']))
        elif data.get('file_id'):
            video = session.query(Video).filter(Video.file_id == str(data['file_id'])).first()
        elif data.get('pickcode'):
            video = session.query(Video).filter(Video.pickcode == str(data['pickcode'])).first()

        if not video:
            return jsonify({'success': False, 'msg': '视频不存在'}), 404

        video.status = target_status
        video.score = target_score
        video.updatetime = datetime.datetime.now()
        session.commit()
    return jsonify({'success': True, 'status': target_status, 'score': target_score})


# ================= 不喜欢数据集（本地去广告） =================
# 纯本地 SQLite 操作，零 115 API 调用。SHA1 是 115 原生内容哈希，
# 同一视频改名后仍能命中；同步时自动跳过已在数据集中的视频。
# 临时上传文件只解析不落盘；数据集可导出为 dislike-sha1.txt 自行维护。


def _decode_import_text(raw_bytes):
    """按 BOM / 内容特征识别导入文件的编码（PowerShell 默认 UTF-16LE 需支持）。

    检测顺序：UTF-16 BOM（FF FE / FE FF）→ utf-16；UTF-8 BOM（EF BB BF）→ utf-8-sig；
    无 BOM 时先按 UTF-8 解码，若结果含 NUL（UTF-16 被误读的特征）则改按 UTF-16LE 重解；
    最后剔除残留的 NUL 字符，保证 SHA1 仍能被正则命中。
    """
    if raw_bytes.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = raw_bytes.decode('utf-16', 'replace')
    elif raw_bytes.startswith(b'\xef\xbb\xbf'):
        text = raw_bytes.decode('utf-8-sig', 'replace')
    else:
        text = raw_bytes.decode('utf-8', 'replace')
        if '\x00' in text:
            text = raw_bytes.decode('utf-16-le', 'replace')
    return text.replace('\x00', '')


def _run_dislike_import(task_id, raw_bytes, filename):
    """后台解析临时文件并合并进不喜欢数据集。文件不落盘，解析后即丢弃。"""
    try:
        total_lines = 0
        added = 0
        skipped_invalid = 0
        already_present = 0
        text = _decode_import_text(raw_bytes)
        entries = []  # list of (sha1, name)
        if filename.lower().endswith('.json'):
            data = json.loads(text)
            if not isinstance(data, list):
                raise ValueError('JSON 文件顶层应为数组')
            for item in data:
                total_lines += 1
                if isinstance(item, str):
                    sha1 = normalize_sha1(item)
                    name = ''
                elif isinstance(item, dict):
                    sha1 = normalize_sha1(item.get('sha1') or '')
                    name = str(item.get('name') or '').strip()
                else:
                    sha1 = ''
                    name = ''
                if not sha1:
                    skipped_invalid += 1
                    continue
                entries.append((sha1, name))
        else:
            # .txt：每行一个 SHA1，可选跟空白/逗号 + 文件名；空行与 # 注释跳过。
            # PowerShell Format-Table 导出（Hash/Path 表头 + ---- 分隔行）也支持：
            # 表头与分隔行直接跳过，不计入无效行。
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                first = line.split(None, 1)[0]
                if first.lower() in ('hash', 'sha1') or set(first) == {'-'}:
                    continue
                total_lines += 1
                m = re.match(r'([0-9a-fA-F]{40})\b', line)
                if not m:
                    skipped_invalid += 1
                    continue
                sha1 = m.group(1).upper()
                name = line[m.end():].strip().lstrip(',').strip()
                entries.append((sha1, name))
        for sha1, name in entries:
            if add_disliked(sha1, name=name, source='import'):
                added += 1
            else:
                already_present += 1
        _update_task(task_id, done=True, total_lines=total_lines, added=added,
                     skipped_invalid=skipped_invalid, already_present=already_present)
    except Exception as exc:
        _update_task(task_id, done=True, error=str(exc))


@dy_bp.route('/api/dislike', methods=['POST'])
def api_dislike():
    """把一个视频记入不喜欢数据集（主要用于去广告），并把该视频状态置为 disliked。"""
    data = request.get_json() or {}
    sha1 = normalize_sha1(data.get('sha1') or '')
    name = (data.get('name') or '').strip()
    video_id = None
    with Session() as session:
        video = None
        if data.get('id') is not None:
            video = session.get(Video, int(data['id']))
        elif data.get('file_id'):
            video = session.query(Video).filter(Video.file_id == str(data['file_id'])).first()
        elif data.get('pickcode'):
            video = session.query(Video).filter(Video.pickcode == str(data['pickcode'])).first()
        if video is not None:
            video_id = video.id
            if not sha1:
                sha1 = normalize_sha1(video.sha1 or '')
            if not name:
                name = (video.filename or '').strip()
    if not sha1:
        return jsonify({'success': False, 'msg': '该视频没有 SHA1，无法记入不喜欢数据集'}), 400
    added = add_disliked(sha1, name=name, source='manual')
    if video_id is not None:
        with Session() as session:
            v = session.get(Video, video_id)
            if v is not None:
                v.status = STATUS_DISLIKED
                v.score = 0
                v.updatetime = datetime.datetime.now()
                session.commit()
    return jsonify({'success': True, 'sha1': sha1, 'status': STATUS_DISLIKED, 'added': added})


@dy_bp.route('/api/dislike/import', methods=['POST'])
def api_dislike_import():
    """上传临时文件（.txt/.json），后台解析并合并进不喜欢数据集。文件不落盘。"""
    f = request.files.get('file')
    if f is None or not f.filename:
        return jsonify({'success': False, 'msg': '请上传文件'}), 400
    raw = f.read()
    if not raw:
        return jsonify({'success': False, 'msg': '文件为空'}), 400
    if len(raw) > 10 * 1024 * 1024:
        return jsonify({'success': False, 'msg': '文件过大（上限 10MB）'}), 400
    tid = _new_task('dislike-import', mode='dislike_import')
    _update_task(tid, total_lines=0, added=0, skipped_invalid=0, already_present=0)
    threading.Thread(target=_run_dislike_import, args=(tid, raw, f.filename), daemon=True).start()
    return jsonify({'success': True, 'task_id': tid}), 202


@dy_bp.route('/api/dislike/import/progress', methods=['GET'])
def api_dislike_import_progress():
    tid = (request.args.get('task_id') or '').strip()
    with _tasks_lock:
        task = dict(_tasks.get(tid)) if tid in _tasks else None
    if task is None:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify({
        'done': bool(task.get('done')),
        'total_lines': task.get('total_lines', 0),
        'added': task.get('added', 0),
        'skipped_invalid': task.get('skipped_invalid', 0),
        'already_present': task.get('already_present', 0),
        'error': task.get('error'),
        'task_id': tid,
    })


@dy_bp.route('/api/dislike/export', methods=['GET'])
def api_dislike_export():
    """导出不喜欢数据集为文本文件（每行 SHA1，可选 Tab + 名称），用户可在本地维护后重新导入。"""
    total, items = list_disliked(limit=100000, offset=0)
    lines = []
    for it in items:
        lines.append(f"{it['sha1']}\t{it['name']}" if it['name'] else it['sha1'])
    body = '\n'.join(lines)
    if body:
        body += '\n'
    return Response(
        body,
        mimetype='text/plain; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename="dislike-sha1.txt"'},
    )


@dy_bp.route('/api/dislike/list', methods=['GET'])
def api_dislike_list():
    try:
        limit = max(1, min(1000, int(request.args.get('limit', 200))))
    except ValueError:
        limit = 200
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        offset = 0
    total, items = list_disliked(limit=limit, offset=offset)
    return jsonify({'success': True, 'total': total, 'items': items})


@dy_bp.route('/api/dislike/remove', methods=['POST'])
def api_dislike_remove():
    data = request.get_json() or {}
    sha1 = normalize_sha1(data.get('sha1') or '')
    if not sha1:
        return jsonify({'success': False, 'msg': '请提供 40 位 SHA1'}), 400
    if not remove_disliked(sha1):
        return jsonify({'success': False, 'msg': '不在不喜欢数据集中'}), 404
    return jsonify({'success': True, 'sha1': sha1})


# ================= 云端账户、目录与结算接口 =================
@dy_bp.route('/api/cloud/config', methods=['GET', 'POST'])
def api_cloud_config():
    if request.method == 'GET':
        cfg = load_cloud_config()
        has_cookie_file = bool(cfg.get('cookie_file')) and os.path.exists(cfg['cookie_file'])
        safe = dict(cfg)
        safe['has_cookie'] = bool(cfg.get('cookie')) or has_cookie_file
        safe['cookie_masked'] = mask_cookie(cfg.get('cookie', ''))
        safe['cookie'] = ''
        safe['aria2_secret_configured'] = bool(cfg.get('aria2_secret'))
        safe['aria2_secret'] = ''
        return jsonify({'success': True, 'config': safe, 'auth': p115_manager.check_auth_status()})

    data = request.get_json() or {}
    # 若前端未修改 cookie/secret 而传空字符串，保留原值（除非显式 clear_cookie=True）
    current = load_cloud_config()
    if 'cookie' in data and not data['cookie'] and not data.get('clear_cookie'):
        data['cookie'] = current.get('cookie', '')
    if 'aria2_secret' in data and not data['aria2_secret'] and not data.get('clear_secret'):
        data['aria2_secret'] = current.get('aria2_secret', '')

    saved = save_cloud_config(data)
    safe = dict(saved)
    safe['has_cookie'] = bool(saved.get('cookie')) or (bool(saved.get('cookie_file')) and os.path.exists(saved['cookie_file']))
    safe['cookie_masked'] = mask_cookie(saved.get('cookie', ''))
    safe['cookie'] = ''
    safe['aria2_secret'] = ''
    return jsonify({'success': True, 'config': safe, 'msg': '云端配置已保存'})


@dy_bp.route('/api/cloud/cookie/import', methods=['POST'])
def api_cloud_cookie_import():
    """快捷导入浏览器导出的 115 Cookie JSON（EditThisCookie / Cookie-Editor 等）。

    前端把浏览器插件复制的 JSON 数组原文 POST 过来，后端解析成 Cookie 头
    字符串后走 save_cloud_config 保存（同时写 cloud_config.json 与
    115-cookies.txt，并重置 p115 客户端使新 Cookie 立即生效）。
    返回只含 Cookie 名与数量、掩码，绝不回传完整 Cookie 值。
    """
    data = request.get_json() or {}
    raw = data.get('cookies_json', data.get('cookies', data.get('text', '')))
    if isinstance(raw, (list, dict)):
        raw = json.dumps(raw, ensure_ascii=False)
    if not isinstance(raw, str):
        return jsonify({'success': False, 'error': '请粘贴浏览器导出的 Cookie JSON'}), 400
    try:
        parsed = parse_browser_cookie_json(raw)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    saved = save_cloud_config({'cookie': parsed['cookie']})
    return jsonify({
        'success': True,
        'msg': 'Cookie 已导入（%d 个）' % parsed['count'],
        'names': parsed['names'],
        'count': parsed['count'],
        'cookie_masked': mask_cookie(saved.get('cookie', '')),
        'has_cookie': True,
    })


@dy_bp.route('/api/cloud/dirs', methods=['GET'])
def api_cloud_dirs():
    """列出 115 指定目录下的子文件夹，方便用户查看目录 CID。"""
    cid = (request.args.get('cid') or '0').strip()
    if not cid.isdigit():
        cid = p115_manager.resolve_cid(cid, create_if_missing=False)
    items = p115_manager.list_dir_children(cid, show_dir=1, limit=500)
    dirs = [
        {'cid': str(it.get('cid')), 'name': it.get('n', '')}
        for it in items
        if p115_manager._is_dir_item(it)
    ]
    return jsonify({'success': True, 'cid': str(cid), 'dirs': dirs})


@dy_bp.route('/api/cloud/sync', methods=['POST'])
def api_cloud_sync():
    """触发同步 115 待筛选目录并执行 SHA1 秒级去重。"""
    data = request.get_json(silent=True) or {}
    inbox_cid = (data.get('inbox_cid') or '').strip() or None
    async_mode = bool(data.get('async', False))

    if async_mode:
        tid = _new_task(inbox_cid or load_cloud_config().get('inbox_cid', ''), mode='cloud_sync')
        threading.Thread(target=run_cloud_sync_task, args=(tid, inbox_cid), daemon=True).start()
        return jsonify({'success': True, 'async': True, 'task_id': tid}), 202

    result = sync_cloud_videos(inbox_cid=inbox_cid)
    return jsonify({'success': True, **result})


@dy_bp.route('/api/cloud/commit', methods=['POST'])
def api_cloud_commit():
    """触发「结算归档并下载 (3%)」后台任务。"""
    data = request.get_json(silent=True) or {}
    dry_run = bool(data.get('dry_run', False))
    action = data.get('action', 'commit_archive')
    job = task_service.submit(action, dry_run=dry_run)
    return jsonify({'success': True, 'task': job}), 202


# ================= 本地来源（Windows 客户端） =================
# 不调任何 115 接口。订阅链接里的 token 只存服务端 db/local_source.json（0600），
# 永不返回给前端；播放走 /videos/<id>/local-play 同源代理。
def _send_local_client(filename, download_name):
    path = os.path.join(CURRENT_DIR, filename)
    if not os.path.exists(path):
        abort(404, '本地客户端脚本尚未部署')
    response = send_file(path, mimetype='text/plain; charset=utf-8', max_age=0)
    response.headers['Content-Disposition'] = 'attachment; filename="%s"' % download_name
    return response


@dy_bp.route('/download/local-client.py', methods=['GET'])
def download_local_client_py():
    """下载本地来源客户端脚本（Python 标准库 only，Windows/Mac/Linux 通用）。"""
    return _send_local_client('local_client.py', 'local_client.py')


@dy_bp.route('/download/local-client.bat', methods=['GET'])
def download_local_client_bat():
    """下载本地来源客户端 Windows 启动脚本（双击运行）。"""
    return _send_local_client('local_client.bat', 'local_client.bat')


@dy_bp.route('/api/local/status', methods=['GET'])
def api_local_status():
    """本地来源连接状态（不含 token）。"""
    src = load_local_source()
    if not src or not src.get('base_url'):
        return jsonify({'success': True, 'configured': False})
    with Session() as session:
        count = session.query(Video).filter(Video.source_type == 'local').count()
    return jsonify({
        'success': True,
        'configured': True,
        'host': src.get('host', ''),
        'base_url': src.get('base_url', ''),
        'roots': src.get('roots', []),
        'updated_at': src.get('updated_at', ''),
        'video_count': count,
    })


@dy_bp.route('/api/local/sync', methods=['POST'])
def api_local_sync():
    """同步本地来源订阅。两种输入（二选一）：

    1. {sub_url}：服务端直连客户端拉取订阅（局域网内，无浏览器混合内容限制）。
    2. {payload, base_url, token}：前端已从浏览器直连客户端取到订阅，
       把 payload 转交给服务端写入（HTTPS 页面直连失败时自动改走方式 1）。

    按 (source_type='local', file_id) 去重写入；已有记录保留打标与结算状态，
    只更新元数据；本次订阅里没有的未结算记录视为文件已移除/改名，删除。

    头部哈希（Video.sha1 = SHA1 前 1MB，与 115 全文件 SHA1 同字段同格式）：
    新视频先查不喜欢数据集（在其中的自动跳过，计 disliked），再按 sha1 做
    内容去重（同一内容换名/换目录只留第一条，计 duplicates）；无哈希的视频
    （旧版客户端）跳过这两步，不报错。已有记录只更新元数据与哈希，不重新
    判定去重/不喜欢（D 键已把该行标为 disliked 的，行本身保留）。
    """
    data = request.get_json() or {}
    payload = None
    base_url = None
    token = None
    if data.get('sub_url'):
        base_url, token = parse_sub_url(str(data['sub_url']))
        payload = fetch_subscription(str(data['sub_url']))
    elif data.get('payload') is not None:
        payload = data['payload']
        base_url = (data.get('base_url') or '').strip()
        token = (data.get('token') or '').strip()
        if not base_url or not token:
            raise ValueError('缺少客户端地址或 token')
        parsed = urlparse(base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('客户端地址无效')
        base_url = '%s://%s' % (parsed.scheme, parsed.netloc)
    else:
        raise ValueError('请提供订阅链接 sub_url 或客户端订阅 payload')
    videos = validate_subscription_payload(payload)
    roots = payload.get('roots') or []
    host = payload.get('host') or ''

    with Session() as session:
        existing = {v.file_id: v for v in session.query(Video).filter(Video.source_type == 'local').all()}
        disliked_sha1s = {r.sha1 for r in session.query(DislikedVideo.sha1).all() if r.sha1}
        # 内容去重用：已有本地行的 sha1 计数（更新行改哈希时维护，避免误判）
        sha1_counts = {}
        for v in existing.values():
            if v.sha1:
                sha1_counts[v.sha1] = sha1_counts.get(v.sha1, 0) + 1
        seen = set()
        seen_sha1s = set(sha1_counts)
        added = 0
        updated = 0
        duplicates = 0
        disliked_skipped = 0
        cache_filled = 0
        cache_writes = []  # 本轮观测到的哈希，同步结束时批量写回文件身份缓存
        # 预取本轮所有 rel_path 的缓存条目（一次查询），写回时用它分类哈希
        # 形式（头部哈希 / 全文件哈希），避免逐条查库。
        cache_by_rel = cache_lookup_many([item.get('rel_path') or '' for item in videos])
        for item in videos:
            sha1 = item.get('sha1') or ''
            # 文件身份缓存（加速，非替代）：客户端没给哈希时（旧版客户端 /
            # 清单里没有该文件），若缓存里有该相对路径的哈希且大小一致，
            # 直接复用，不重新读文件算哈希。头部哈希优先（与客户端现算同
            # 形式）；只有全哈希时用全哈希（从 115 下载到本地的文件，其
            # Video.sha1 本就是全哈希）。客户端给了哈希时沿用客户端的值，
            # 传统行为不变；缓存只补缺，不覆盖。
            if not sha1:
                hit = cache_lookup(item.get('rel_path') or '', size=item.get('size'))
                if hit:
                    filled = hit.get('sha1_head') or hit.get('sha1_full') or ''
                    if filled:
                        sha1 = filled
                        cache_filled += 1
            seen.add(item['id'])
            row = existing.get(item['id'])
            if row is None:
                # 新视频：先查不喜欢数据集（广告拦截），再查内容重复
                if sha1 and sha1 in disliked_sha1s:
                    disliked_skipped += 1
                    continue
                if sha1 and sha1 in seen_sha1s:
                    duplicates += 1
                    continue
                session.add(Video(
                    filename=item['name'],
                    detail=item['rel_path'],
                    rel_path=item['rel_path'],
                    file_size=item['size'],
                    file_id=item['id'],
                    pickcode='',
                    sha1=sha1,
                    inbox_cid='',
                    duration=0,
                    thumb='',
                    status=STATUS_PENDING,
                    score=0,
                    source_type='local',
                    cloud_moved=0,
                    cloud_deleted=0,
                ))
                if sha1:
                    seen_sha1s.add(sha1)
                    sha1_counts[sha1] = sha1_counts.get(sha1, 0) + 1
                    # 按缓存现状分类：全哈希绝不写进 sha1_head（见
                    # classify_received_sha1）。
                    _col_val = classify_received_sha1(
                        cache_by_rel.get(item['rel_path']), sha1)
                    if _col_val:
                        _w = {'rel_path': item['rel_path'], 'size': item['size'],
                              'origin': 'local'}
                        _w[_col_val[0]] = _col_val[1]
                        cache_writes.append(_w)
                added += 1
            else:
                row.filename = item['name']
                row.rel_path = item['rel_path']
                row.detail = item['rel_path']
                row.file_size = item['size']
                if sha1 and row.sha1 != sha1:
                    old = row.sha1 or ''
                    if old and old in sha1_counts:
                        sha1_counts[old] -= 1
                        if sha1_counts[old] <= 0:
                            seen_sha1s.discard(old)
                            del sha1_counts[old]
                    row.sha1 = sha1
                    seen_sha1s.add(sha1)
                    sha1_counts[sha1] = sha1_counts.get(sha1, 0) + 1
                if sha1:
                    # 按缓存现状分类：全哈希绝不写进 sha1_head（见
                    # classify_received_sha1）。
                    _col_val = classify_received_sha1(
                        cache_by_rel.get(item['rel_path']), sha1)
                    if _col_val:
                        _w = {'rel_path': item['rel_path'], 'size': item['size'],
                              'origin': 'local'}
                        _w[_col_val[0]] = _col_val[1]
                        cache_writes.append(_w)
                row.updatetime = datetime.datetime.now()
                updated += 1
        removed = 0
        for fid, row in existing.items():
            if fid not in seen and not row.cloud_moved and not row.cloud_deleted:
                session.delete(row)
                removed += 1
        session.commit()
    # 本轮观测到的哈希批量写回文件身份缓存（供下次清单导出复用）。
    # 写回时已按缓存现状分类，全哈希只进 sha1_full，绝不进 sha1_head。
    # 缓存写失败不影响同步结果（cache_upsert_many 内部容错）。
    if cache_writes:
        cache_upsert_many(cache_writes)
    save_local_source(base_url, token, roots, host)
    return jsonify({
        'success': True,
        'added': added,
        'updated': updated,
        'removed': removed,
        'duplicates': duplicates,
        'disliked': disliked_skipped,
        'cache_filled': cache_filled,
        'total': len(videos),
        'roots': roots,
        'host': host,
    })


@dy_bp.route('/api/cache/stats', methods=['GET'])
def api_cache_stats():
    """文件身份缓存统计（总数、已索引数、含全哈希/头部哈希数、按来源计数）。"""
    return jsonify({'success': True, **cache_stats()})


@dy_bp.route('/api/cache/export', methods=['GET'])
def api_cache_export():
    """导出客户端缓存清单（dy-cache-manifest.json）。

    含头部哈希或全文件哈希的条目：客户端用它跳过读文件算哈希（清单里的
    sha1 即要复用的哈希值，sha1_kind 标明形式：'head'=头部哈希，'full'=
    115 全文件哈希）。用法：下载后传给 local_client.py 的 --cache 参数。
    清单为空时返回空 entries，不报错。
    """
    entries = cache_export_entries()
    body = json.dumps({'version': 1, 'entries': entries}, ensure_ascii=False)
    return Response(
        body,
        status=200,
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment; filename="dy-cache-manifest.json"'},
    )


@dy_bp.route('/api/cloud/import_local_sha1', methods=['POST'])
def api_import_local_sha1():
    """扫描本地历史精选目录并导入 SHA1 至 SQLite，用于云端去重。"""
    data = request.get_json() or {}
    local_dir = (data.get('local_dir') or '').strip()
    if not local_dir:
        raise ValueError('请提供本地历史视频目录路径')
    result = import_local_history_sha1(local_dir)
    return jsonify({'success': True, **result})


# ================= 兼容原有前端路由 =================
@dy_bp.route('/video-paths', methods=['GET', 'POST'])
def handle_video_paths():
    if request.method == 'GET':
        cfg = load_cloud_config()
        paths = load_paths()
        if cfg.get('inbox_cid') and cfg['inbox_cid'] not in paths:
            paths = [cfg['inbox_cid']] + paths
        return jsonify({'paths': paths})

    path = ((request.get_json() or {}).get('path') or '').strip()
    if not path:
        return jsonify({'error': '目录 CID 或路径不能为空'}), 400
    save_cloud_config({'inbox_cid': path})
    with _path_config_lock:
        paths = load_paths()
        if path not in paths:
            paths.append(path)
            save_paths(paths)
    return jsonify({'success': True})


@dy_bp.route('/video-paths/index', methods=['POST'])
def index_video_path():
    path = ((request.get_json() or {}).get('path') or '').strip()
    if request.args.get('del') == '1':
        with _path_config_lock:
            paths = load_paths()
            if path in paths:
                paths.remove(path)
                save_paths(paths)
        return jsonify({'success': True, 'msg': '已移除目录记录'})

    if not path:
        return jsonify({'error': '目录不能为空'}), 400
    save_cloud_config({'inbox_cid': path})
    tid = _new_task(path, mode='cloud_sync')
    threading.Thread(target=run_cloud_sync_task, args=(tid, path), daemon=True).start()
    return jsonify({'success': True, 'task_id': tid, 'async': True, 'mode': 'cloud_sync'})


@dy_bp.route('/video-paths/index/progress', methods=['GET'])
def index_video_path_progress():
    task_id = request.args.get('task_id', '').strip()
    if not task_id:
        return jsonify({'error': '缺少 task_id'}), 400
    with _tasks_lock:
        t = _tasks.get(task_id)
        if not t:
            return jsonify({'error': 'task 不存在'}), 404
        return jsonify(dict(t))


@dy_bp.route('/video-paths/updatefile')
def index_video_path_update():
    result = sync_cloud_videos()
    return jsonify({
        'success': True,
        'added': result['added_pending'],
        'dedup_rejected': result['dedup_rejected'],
        'removed': 0,
        'mode': 'cloud_sync',
    })


@dy_bp.route('/config', methods=['GET'])
def get_config():
    return jsonify({
        'status': 'ok',
        'mode': '115_cloud',
        'thumbnails': True,
        'auth': p115_manager.check_auth_status(),
    })


# ================= 视频列表 / 流媒体 / 缩略图 =================
@dy_bp.route('/videos', methods=['GET'])
def get_videos():
    session = Session()
    try:
        query = session.query(Video)
        page = max(1, int(request.args.get('page', 1)))
        page_size = max(1, min(200, int(request.args.get('page_size', 24))))
        start_offset = max(0, int(request.args.get('offset', (page - 1) * page_size)))
        latest = int(request.args.get('latest', 0))
        stream = request.args.get('stream', 'false').lower() == 'true'
        sort_by = request.args.get('sort_by', 'path')
        sort_order = request.args.get('sort_order', 'auto')
        if sort_order not in ('auto', 'asc', 'desc'):
            raise ValueError('排序顺序必须为 auto、asc 或 desc')

        query, use_random = apply_video_filters(query, request.args, session)
        total = query.count() if request.args.get('include_total') == '1' else None

        if use_random:
            with _random_lock:
                missing_ids = [vid for (vid,) in session.query(Video.id).filter(
                    ~Video.id.in_(session.query(VideoRandomOrder.video_id))).all()]
                if missing_ids:
                    random.shuffle(missing_ids)
                    random_offset = (session.query(func.max(VideoRandomOrder.order_index)).scalar() or 0) + 1
                    session.bulk_insert_mappings(VideoRandomOrder, [
                        {'video_id': vid, 'order_index': random_offset + i} for i, vid in enumerate(missing_ids)])
                    session.commit()

            random_subq = session.query(VideoRandomOrder) \
                .order_by(VideoRandomOrder.order_index) \
                .with_entities(
                    VideoRandomOrder.video_id.label('video_id'),
                    VideoRandomOrder.order_index.label('order_index'),
                ).subquery()

            ordered = query.outerjoin(
                random_subq, Video.id == random_subq.c.video_id
            ).order_by(func.coalesce(random_subq.c.order_index, 1), Video.id)
            videos = ordered.offset(start_offset).limit(page_size).all()
        else:
            sort_fields = {
                'filename': (Video.filename, 'asc'),
                'path': (Video.detail, 'asc'),
                'oldest': (Video.id, 'asc'),
                'score': (Video.score, 'desc'),
                'size': (Video.file_size, 'desc'),
                'least_viewed': (func.coalesce(Video.preview_count, 0), 'asc'),
                'most_viewed': (func.coalesce(Video.preview_count, 0), 'desc'),
            }
            column, natural_order = sort_fields.get(sort_by, (Video.id, 'desc'))
            direction = natural_order if sort_order == 'auto' else sort_order
            query = query.order_by(column.asc() if direction == 'asc' else column.desc())
            if sort_by in sort_fields and sort_by != 'oldest':
                tie_ascending = sort_by in ('filename', 'path') if sort_order == 'auto' else direction == 'asc'
                query = query.order_by(Video.id.asc() if tie_ascending else Video.id.desc())

            if page_size:
                videos = query.offset(start_offset).limit(page_size).all()
            elif latest:
                videos = query.limit(int(latest)).all()
            else:
                videos = query.all()
    finally:
        session.close()

    items = [{
        'id': v.id,
        'file_id': v.file_id or '',
        'pickcode': v.pickcode or '',
        'sha1': v.sha1 or '',
        'filename': v.filename,
        'tags': v.tags or '',
        'score': v.score or 0,
        'status': v.status or STATUS_PENDING,
        'source': v.source_type or 'cloud',
        'detail': v.rel_path or v.detail or v.filename,
        'rel_path': v.rel_path or v.detail or v.filename,
        'file_size': v.file_size or 0,
        'duration': v.duration or 0,
        'thumb': v.thumb or '',
        'preview_count': v.preview_count or 0,
        'last_preview_at': v.last_preview_at.isoformat() if v.last_preview_at else None,
        'updated_at': v.updatetime.isoformat() if v.updatetime else None,
    } for v in videos]

    if stream:
        return Response(json.dumps(items, ensure_ascii=False), mimetype='application/json')
    if total is not None:
        return jsonify({'items': items, 'total': total, 'page': page, 'page_size': page_size, 'offset': start_offset})
    return jsonify(items)


@dy_bp.route('/videos/update_score', methods=['POST'])
def update_score():
    data = request.get_json() or {}
    score = data.get('score')
    if isinstance(score, bool) or not isinstance(score, int) or score not in range(6):
        raise ValueError('评分必须为 0–5 的整数')
    with Session() as session:
        video = session.get(Video, data.get('id'))
        if not video:
            return jsonify({'success': False, 'msg': '视频不存在'}), 404
        # commit 会 expire 全部属性、with 退出后 session 关闭，
        # 之后再读 video.status 会报 DetachedInstanceError，
        # 所以先把值存到局部变量再返回（/api/mark 已是这种写法）。
        target_status = score_to_status(score, cp=video.cp or 0)
        video.score = score
        video.status = target_status
        video.updatetime = datetime.datetime.now()
        session.commit()
    return jsonify({'success': True, 'status': target_status})


@dy_bp.route('/videos/<int:video_id>/preview', methods=['POST'])
def record_video_preview(video_id):
    event_id = (request.get_json() or {}).get('event_id', '')
    if not isinstance(event_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', event_id):
        raise ValueError('预览记录标识无效')
    now = datetime.datetime.now()
    with Session() as session:
        video = session.get(Video, video_id)
        if not video:
            abort(404, '视频不存在')
        inserted = session.execute(sqlite_insert(VideoPreviewEvent).values(
            event_id=event_id, video_id=video_id, created_at=now).on_conflict_do_nothing(
                index_elements=['event_id']))
        if inserted.rowcount:
            session.query(Video).filter(Video.id == video_id).update({
                Video.preview_count: func.coalesce(Video.preview_count, 0) + 1,
                Video.last_preview_at: now,
                Video.updatetime: Video.updatetime,
            }, synchronize_session=False)
        elif session.get(VideoPreviewEvent, event_id).video_id != video_id:
            abort(409, '此预览标识已用于其他视频')
        session.commit()
        session.refresh(video)
        return jsonify({
            'success': True,
            'video_id': video_id,
            'preview_count': video.preview_count,
            'last_preview_at': video.last_preview_at.isoformat() if video.last_preview_at else None,
        })


# 115 把 m3u8/分片 URL 绑定到生成它时用的 User-Agent（se=u,ua）。
# fs_video_m3u8 经由 p115client 发起，默认 UA 就是 'Mozilla/5.0'，
# 所以代理抓取必须用完全相同的 UA；用完整 Chrome UA 会被 115 返回 403。
_PROXY_UA = "Mozilla/5.0"
_M3U8_CT = "application/vnd.apple.mpegurl"


def _proxy_url(video_id, target):
    """Build the same-origin proxy URL for a 115 target URL."""
    return f"/dy/videos/{video_id}/px?u={quote(target, safe='')}"


def _rewrite_m3u8(text, video_id, base_url=None):
    """Rewrite every URL in an m3u8 playlist to same-origin proxy URLs.

    Bare URL lines are resolved against base_url (urljoin) then proxied.
    A URI="..." inside a tag line (e.g. #EXT-X-KEY) is rewritten the same way.
    Non-URL lines are left untouched.
    """
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith("#"):
            m = re.search(r'URI="([^"]+)"', s)
            if m:
                target = urljoin(base_url, m.group(1)) if base_url else m.group(1)
                s = s[:m.start(1)] + _proxy_url(video_id, target) + s[m.end(1):]
            out.append(s)
            continue
        target = urljoin(base_url, s) if base_url else s
        out.append(_proxy_url(video_id, target))
    return "\n".join(out) + "\n"


def _fetch_upstream(url, timeout):
    """Fetch a 115 URL server-side (same IP/session as the link request)."""
    headers = {"User-Agent": _PROXY_UA}
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    return requests.get(url, headers=headers, timeout=timeout, stream=True)


@dy_bp.route('/videos/<int:video_id>/play', methods=['GET'])
def play_video_proxy(video_id):
    """Same-origin m3u8 entry point for browser playback.

    Fetches the master playlist server-side, rewrites its URLs to the
    /px fetcher, and serves the rewritten playlist. The browser (hls.js)
    then only ever talks to this server.
    """
    with Session() as session:
        video = session.get(Video, video_id)
    if not video or not video.pickcode:
        abort(404, '云端视频不存在或缺少 pickcode')
    url, stype, expire_at = p115_manager.get_stream_url(video.pickcode)
    if stype != 'hls':
        # No master playlist (fell back to a direct mp4 link). Direct links
        # are bound to a different UA and are out of scope here; keep the
        # previous behavior (redirect to the raw URL).
        return redirect(url, code=302)
    try:
        resp = _fetch_upstream(url, timeout=15)
    except Exception:
        abort(502, '无法从 115 获取播放列表')
    if resp.status_code != 200:
        abort(502, '无法从 115 获取播放列表')
    text = resp.content.decode('utf-8', 'replace')
    rewritten = _rewrite_m3u8(text, video_id, base_url=url)
    return Response(rewritten, content_type=_M3U8_CT,
                    headers={'Cache-Control': 'no-cache'})


@dy_bp.route('/videos/<int:video_id>/px', methods=['GET'])
def proxy_fetch(video_id):
    """Generic same-origin fetcher for 115 playlist/segment URLs.

    video_id only namespaces the URL; it is not validated against the
    target. Only http(s) targets are allowed. m3u8 bodies are rewritten
    to proxy URLs (relative URLs resolved against the fetched URL first);
    everything else is streamed through with the upstream Content-Type.
    """
    target = (request.args.get('u') or '').strip()
    if not target:
        abort(400, '缺少目标地址')
    parsed = urlparse(target)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        abort(400, '目标地址无效')
    try:
        resp = _fetch_upstream(target, timeout=30)
    except Exception:
        abort(502, '上游请求失败')
    if resp.status_code not in (200, 206):
        abort(502, '上游返回错误')
    ctype = (resp.headers.get('Content-Type') or '').lower()
    is_m3u8 = (parsed.path.lower().endswith('.m3u8')
               or 'mpegurl' in ctype or 'x-mpegurl' in ctype)
    if is_m3u8:
        text = resp.content.decode('utf-8', 'replace')
        rewritten = _rewrite_m3u8(text, video_id, base_url=target)
        return Response(rewritten, content_type=_M3U8_CT,
                        headers={'Cache-Control': 'no-cache'})
    out_headers = {}
    if resp.headers.get('Content-Length'):
        out_headers['Content-Length'] = resp.headers['Content-Length']
    if resp.status_code == 206 and resp.headers.get('Content-Range'):
        out_headers['Content-Range'] = resp.headers['Content-Range']
    return Response(
        stream_with_context(resp.iter_content(chunk_size=65536)),
        status=resp.status_code,
        content_type=resp.headers.get('Content-Type') or 'application/octet-stream',
        headers=out_headers,
    )


@dy_bp.route('/videos/<int:video_id>/stream', methods=['GET'])
def stream_video_file(video_id):
    """兼容旧路由：重定向到 115 云端流地址，或 ?format=json 返回 m3u8 信息。"""
    with Session() as session:
        video = session.get(Video, video_id)
    if not video or not video.pickcode:
        return abort(404, '云端视频不存在或缺少 pickcode')
    url, stype, expire_at = p115_manager.get_stream_url(video.pickcode)
    if stype == 'hls':
        # Browser cannot fetch 115 URLs directly (403 + CORS); serve
        # through the same-origin proxy instead.
        play_url = f'/dy/videos/{video_id}/play'
        if request.args.get('format') == 'json':
            return jsonify({'success': True, 'url': play_url, 'type': stype, 'expire_at': expire_at})
        return redirect(play_url, code=302)
    if request.args.get('format') == 'json':
        return jsonify({'success': True, 'url': url, 'type': stype, 'expire_at': expire_at})
    return redirect(url, code=302)


@dy_bp.route('/videos/<int:video_id>/local-play', methods=['GET', 'HEAD'])
def local_play_proxy(video_id):
    """本地来源播放代理：把浏览器对局域网客户端的直链请求经 Mac 服务端中转。

    浏览器在 HTTPS 页面（cpl.zin7.de）里不能直接拉 http:// 局域网地址
    （混合内容拦截），所以播放走同源代理：服务端按 Range 把客户端的
    /file/<id> 流式转发回来，token 只留在服务端。普通 <video> 直播，
    不走 m3u8/HLS，不调 115 接口。
    """
    with Session() as session:
        video = session.get(Video, video_id)
    if not video or video.source_type != 'local' or not video.file_id:
        abort(404, '本地视频不存在')
    src = load_local_source()
    if not src or not src.get('base_url') or not src.get('token'):
        abort(502, '本地客户端未连接，请先同步本地来源')
    target = '%s/file/%s?token=%s' % (
        src['base_url'].rstrip('/'), quote(video.file_id, safe=''), quote(src['token'], safe=''))
    headers = {}
    if request.headers.get('Range'):
        headers['Range'] = request.headers['Range']
    try:
        resp = requests.request(request.method, target, headers=headers, timeout=10, stream=True)
    except requests.RequestException:
        abort(502, '无法连接本地客户端（请确认电脑上的 local_client.py 正在运行）')
    if resp.status_code not in (200, 206):
        abort(502, '本地客户端返回错误')
    out_headers = {}
    for key in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
        if resp.headers.get(key):
            out_headers[key] = resp.headers[key]

    def _gen():
        try:
            yield from resp.iter_content(chunk_size=65536)
        finally:
            resp.close()

    return Response(stream_with_context(_gen()), status=resp.status_code, headers=out_headers)


@dy_bp.route('/videos/<int:video_id>/thumbnail', methods=['GET'])
def get_video_thumbnail(video_id):
    """优先重定向到 115 云端缩略图 thumb。"""
    with Session() as session:
        video = session.get(Video, video_id)
    if not video or not video.thumb:
        abort(404, '暂无云端缩略图')
    return redirect(video.thumb, code=302)


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


def _local_midframe(video_id, duration):
    """本地来源中间帧：输入改为本机 local-play 代理 URL；ffmpeg 命令、缓存路径、
    信号量与超时与云端完全一致，只换输入与取时长方式（库里没有时长就用 ffprobe
    读流，ffprobe 失败按 10 秒取中间帧；ffprobe 读到的时长写回数据库）。
    本地没有 115 原生缩略图可回落，失败直接 404/503/504。"""
    src = load_local_source()
    if not src or not src.get('base_url') or not src.get('token'):
        abort(502, '本地客户端未连接，请先同步本地来源')
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
    out_path = os.path.join(THUMBNAIL_DIR, f'midframe_{video_id}.jpg')
    os.makedirs(THUMBNAIL_DIR, exist_ok=True)
    # 临时文件保留 .jpg 后缀（ffmpeg 靠扩展名推断输出格式），线程 id 保证唯一；
    # 另用 -f image2 显式指定图片输出格式，双保险。
    tmp_path = out_path + f'.{threading.get_ident()}.jpg'
    # -ss 在 -i 之前：快速定位到中间位置；只取一帧；-q:v 3 较高质量。
    # local-play 是本机代理直链，不需要 115 的 UA 绑定，但命令与云端保持一致。
    cmd = [_FFMPEG_BIN, '-hide_banner', '-loglevel', 'error',
           '-user_agent', 'Mozilla/5.0',
           '-ss', str(mid), '-i', url,
           '-frames:v', '1', '-q:v', '3', '-f', 'image2', '-y', tmp_path]
    if not _midframe_semaphore.acquire(timeout=35):
        abort(503, '中间帧提取繁忙，请稍后重试')
    try:
        try:
            result = subprocess.run(cmd, timeout=_MIDFRAME_TIMEOUT,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
        except subprocess.TimeoutExpired:
            abort(504, '中间帧提取超时')
        if result.returncode != 0:
            current_app.logger.warning(
                'midframe ffmpeg failed video=%s rc=%s err=%s',
                video_id, result.returncode,
                result.stderr.decode('utf-8', 'replace')[-500:])
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            os.replace(tmp_path, out_path)
            return send_file(out_path, mimetype='image/jpeg', max_age=86400)
    finally:
        _midframe_semaphore.release()
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    abort(404, '本地中间帧提取失败（请确认电脑上的 local_client.py 正在运行）')


@dy_bp.route('/videos/<int:video_id>/midframe', methods=['GET'])
def get_video_midframe(video_id):
    """服务端中间帧：用 ffmpeg 从 m3u8 中间位置截一帧作封面。
    成功结果缓存为 db/thumbnails/midframe_<id>.jpg；任何失败都回落到 115 原生 thumb。"""
    out_path = os.path.join(THUMBNAIL_DIR, f'midframe_{video_id}.jpg')
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return send_file(out_path, mimetype='image/jpeg', max_age=86400)
    with Session() as session:
        video = session.get(Video, video_id)
        if not video:
            abort(404, '视频不存在')
        source_type = video.source_type or ''
        duration = video.duration or 0
        pickcode = video.pickcode or ''
        thumb = video.thumb or ''
    if source_type == 'local':
        return _local_midframe(video_id, duration)
    if not duration or not pickcode:
        if thumb:
            return redirect(thumb, code=302)
        abort(404, '暂无中间帧与云端缩略图')
    try:
        url, _stype, _expire = p115_manager.get_stream_url(pickcode)
    except Exception:
        if thumb:
            return redirect(thumb, code=302)
        abort(404, '无法获取云端播放地址')
    if not url:
        if thumb:
            return redirect(thumb, code=302)
        abort(404, '无法获取云端播放地址')
    mid = max(0, int(duration) // 2)
    os.makedirs(THUMBNAIL_DIR, exist_ok=True)
    # 临时文件保留 .jpg 后缀（ffmpeg 靠扩展名推断输出格式），线程 id 保证唯一；
    # 另用 -f image2 显式指定图片输出格式，双保险。
    tmp_path = out_path + f'.{threading.get_ident()}.jpg'
    # -ss 在 -i 之前：快速定位到中间位置；只取一帧；-q:v 3 较高质量
    # 115 把 m3u8 链接绑定到生成它时用的 UA（p115client 默认恰为 Mozilla/5.0），
    # ffmpeg 默认 UA 会被 403；这里显式用同一个 UA。
    cmd = [_FFMPEG_BIN, '-hide_banner', '-loglevel', 'error',
           '-user_agent', 'Mozilla/5.0',
           '-ss', str(mid), '-i', url,
           '-frames:v', '1', '-q:v', '3', '-f', 'image2', '-y', tmp_path]
    if not _midframe_semaphore.acquire(timeout=35):
        if thumb:
            return redirect(thumb, code=302)
        abort(503, '中间帧提取繁忙，请稍后重试')
    try:
        try:
            result = subprocess.run(cmd, timeout=_MIDFRAME_TIMEOUT,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
        except subprocess.TimeoutExpired:
            if thumb:
                return redirect(thumb, code=302)
            abort(504, '中间帧提取超时')
        if result.returncode != 0:
            current_app.logger.warning(
                'midframe ffmpeg failed video=%s rc=%s err=%s',
                video_id, result.returncode,
                result.stderr.decode('utf-8', 'replace')[-500:])
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            os.replace(tmp_path, out_path)
            return send_file(out_path, mimetype='image/jpeg', max_age=86400)
    finally:
        _midframe_semaphore.release()
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    if thumb:
        return redirect(thumb, code=302)
    abort(404, '中间帧提取失败且无云端缩略图')


@dy_bp.route('/videos/count', methods=['GET'])
def get_videos_count():
    with Session() as session:
        query, _ = apply_video_filters(session.query(Video), request.args, session)
        total = query.count()
    return jsonify({'total': total})


@dy_bp.route('/videos/batch_score', methods=['POST'])
def batch_score():
    data = request.get_json() or {}
    ids, score = data.get('ids'), data.get('score')
    if not isinstance(ids, list) or not ids or len(ids) > 500 or any(type(i) is not int for i in ids):
        raise ValueError('请选择 1–500 个视频')
    if type(score) is not int or score not in range(6):
        raise ValueError('评分必须为 0–5 的整数')
    new_status = score_to_status(score)
    with Session() as session:
        count = session.query(Video).filter(Video.id.in_(ids)).update(
            {Video.score: score, Video.status: new_status, Video.updatetime: datetime.datetime.now()},
            synchronize_session=False,
        )
        session.commit()
    return jsonify({'success': True, 'count': count, 'status': new_status})


@dy_bp.route('/videos/shuffle', methods=['POST'])
def shuffle_videos():
    with _random_lock, Session() as session:
        ids = [vid for (vid,) in session.query(Video.id).all()]
        random.shuffle(ids)
        session.query(VideoRandomOrder).delete()
        session.bulk_insert_mappings(VideoRandomOrder, [
            {'video_id': vid, 'order_index': i} for i, vid in enumerate(ids)])
        session.commit()
    return jsonify({'success': True})


# ================= 首页 =================
@dy_bp.route('/')
def index():
    if not os.path.exists(INDEX_FILE):
        return f"找不到首页文件：{INDEX_FILE}", 404
    response = send_file(INDEX_FILE, conditional=True, max_age=0)
    response.headers['Cache-Control'] = 'no-cache'
    return response


# ================= 标签体系（保留兼容） =================
@dy_bp.route('/sys_tags/blacklist', methods=['GET', 'POST'])
def api_blacklist():
    with Session() as session:
        if request.method == 'GET':
            return jsonify({'blacklist': BlacklistService.get_all(session)})
        words = (request.get_json() or {}).get('blacklist', [])
        if not isinstance(words, list) or any(not isinstance(w, str) for w in words):
            raise ValueError('清理词必须为字符串列表')
        BlacklistService.sync_list(session, list(dict.fromkeys(w.strip() for w in words if w.strip())))
    return jsonify({'success': True, 'msg': '黑名单更新成功'})


@dy_bp.route('/sys_tags/tag_groups', methods=['GET', 'POST'])
def api_tag_groups():
    with Session() as session:
        if request.method == 'GET':
            return jsonify({'success': True, 'groups': TagService.get_all_groups(session)})
        groups = (request.get_json() or {}).get('groups', [])
        TagService.sync_groups(session, groups)
    return jsonify({'success': True, 'msg': 'Tag分区已更新'})


# ================= 统计与归档任务 =================
@dy_bp.route('/library/stats')
def library_stats():
    # inbox_cid 可选：传了就只统计该 115 目录的视频（喜欢/待筛按目录绑定），
    # 不传则全局。local_history 不属于任何目录，不参与过滤。
    # source 可选：all（默认）/ cloud / local；local 行的 inbox_cid 为空，
    # 选 local 时忽略 inbox_cid 过滤。
    inbox_cid = (request.args.get('inbox_cid') or '').strip()
    source = (request.args.get('source') or 'all').strip()
    with Session() as session:
        base_q = session.query(Video).filter(or_(Video.source_type != 'local_history', Video.source_type.is_(None)))
        if source == 'cloud':
            base_q = base_q.filter(Video.source_type == 'cloud')
        elif source == 'local':
            base_q = base_q.filter(Video.source_type == 'local')
        elif source not in ('all', ''):
            raise ValueError('来源筛选无效')
        if inbox_cid and source != 'local':
            base_q = base_q.filter(Video.inbox_cid == inbox_cid)
        total = base_q.count()
        pending = base_q.filter(Video.status == STATUS_PENDING).count()
        starred = base_q.filter(Video.status == STATUS_STARRED).count()
        downloaded = base_q.filter(Video.status == STATUS_DOWNLOADED).count()
        rejected = base_q.filter(Video.status == STATUS_REJECTED).count()
        history_sha1 = session.query(Video).filter(Video.source_type == 'local_history').count()

    result = {
        'total': total,
        'pending': pending,
        'favorites': starred + downloaded,
        'starred': starred,
        'downloaded': downloaded,
        'excluded': rejected,
        'unrated': pending,
        'history_sha1': history_sha1,
    }
    if request.args.get('include_previews') == '1':
        with Session() as session:
            result['preview_total'] = session.query(func.coalesce(func.sum(Video.preview_count), 0)).scalar()
            result['previewed_videos'] = session.query(Video.id).filter(Video.preview_count > 0).count()
    return jsonify(result)


# 大小分桶：按大小分桶的视频体积分布（MB），供按大小整理时选阈值用。
# 只读本地 SQLite，不调 115 接口。分桶边缘固定为 100/300/500/1024/2048/4096MB；
# 统计口径与 move_small_videos / move_large_videos 一致（当前待筛选目录、未移动、未删除、未进大小文件夹）。
_SIZE_EDGES_MB = (100, 300, 500, 1024, 2048, 4096)


@dy_bp.route('/library/size-distribution')
def library_size_distribution():
    cfg = get_star_config()
    inbox_cid = (request.args.get('inbox_cid') or '').strip() or str(cfg.get('inbox_cid') or '').strip()
    if not inbox_cid:
        return jsonify({'success': False, 'error': '请先在云端配置中填写 115 待筛选目录 (INBOX_CID)'}), 400
    try:
        threshold_mb = int(float(request.args.get('max_size_mb'))) if request.args.get('max_size_mb') else None
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '阈值必须是数字（单位 MB）'}), 400
    defaults = normalize_move_params({'max_size_mb': threshold_mb} if threshold_mb else None, cfg)
    threshold_mb = threshold_mb if threshold_mb is not None else defaults['max_size_mb']
    threshold_mb = max(10, min(10240, threshold_mb))
    try:
        min_mb = int(float(request.args.get('min_size_mb'))) if request.args.get('min_size_mb') else None
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '阈值必须是数字（单位 MB）'}), 400
    defaults_large = normalize_move_params({'min_size_mb': min_mb} if min_mb else None, cfg, direction='large')
    min_mb = min_mb if min_mb is not None else defaults_large['min_size_mb']
    min_mb = max(10, min(10240, min_mb))

    with Session() as session:
        rows = session.query(Video.file_size, Video.rel_path).filter(
            Video.source_type == 'cloud',
            Video.inbox_cid == inbox_cid,
            Video.file_size > 0,
            Video.file_id != '',
            Video.cloud_moved == 0,
            Video.cloud_deleted == 0,
        ).all()
    sizes = [int(sz) for sz, rel in rows if not is_size_folder_path(rel)]
    total_bytes = sum(sizes)

    def _fmt(mb):
        return ('%dGB' % (mb // 1024)) if mb >= 1024 and mb % 1024 == 0 else ('%dMB' % mb)

    buckets = []
    edges = [0] + list(_SIZE_EDGES_MB)
    for i, lo in enumerate(edges):
        hi = _SIZE_EDGES_MB[i] if i < len(_SIZE_EDGES_MB) else None
        in_bucket = [s for s in sizes if s >= lo * 1048576 and (hi is None or s < hi * 1048576)]
        label = ('<%s' % _fmt(hi)) if lo == 0 else (('%s-%s' % (_fmt(lo), _fmt(hi))) if hi else ('>%s' % _fmt(lo)))
        buckets.append({'label': label, 'min_mb': lo, 'max_mb': hi,
                        'count': len(in_bucket), 'bytes': sum(in_bucket)})
    cumulative = []
    for edge in _SIZE_EDGES_MB:
        under = [s for s in sizes if s < edge * 1048576]
        cumulative.append({'max_mb': edge, 'label': '≤' + _fmt(edge),
                           'count': len(under), 'bytes': sum(under)})
    under_threshold = [s for s in sizes if s < threshold_mb * 1048576]
    over_threshold = [s for s in sizes if s >= min_mb * 1048576]
    return jsonify({
        'success': True,
        'inbox_cid': inbox_cid,
        'total': len(sizes),
        'total_bytes': total_bytes,
        'buckets': buckets,
        'cumulative': cumulative,
        'preview': {'max_mb': threshold_mb, 'label': '≤' + _fmt(threshold_mb),
                    'count': len(under_threshold), 'bytes': sum(under_threshold)},
        'preview_large': {'min_mb': min_mb, 'label': '≥' + _fmt(min_mb),
                          'count': len(over_threshold), 'bytes': sum(over_threshold)},
        'defaults': defaults,
        'defaults_large': defaults_large,
    })


@dy_bp.route('/tasks')
@dy_bp.route('/tasks/')
def star_task_page():
    prefix = '/dyfn' if request.blueprint == 'dyfn' else '/dy'
    return redirect(prefix + '/#tasks')


@dy_bp.route('/star/config', methods=['GET', 'POST'])
def star_configuration():
    if request.method == 'POST':
        data = request.get_json() or {}
        current = load_cloud_config()
        if 'cookie' in data and not data['cookie'] and not data.get('clear_cookie'):
            data['cookie'] = current.get('cookie', '')
        if 'aria2_secret' in data and not data['aria2_secret'] and not data.get('clear_secret'):
            data['aria2_secret'] = current.get('aria2_secret', '')
        config = save_star_config(data)
    else:
        config = get_star_config()

    safe = dict(config)
    safe['has_cookie'] = bool(config.get('cookie')) or (bool(config.get('cookie_file')) and os.path.exists(config['cookie_file']))
    safe['cookie_masked'] = mask_cookie(config.get('cookie', ''))
    safe['cookie'] = ''
    safe['aria2_secret_configured'] = bool(config.get('aria2_secret'))
    safe['aria2_secret'] = ''
    return jsonify({'success': True, 'config': safe, 'db_path': DB_FILE, 'actions': STAR_ACTIONS})


@dy_bp.route('/star/tasks', methods=['GET', 'POST'])
def star_task_collection():
    if request.method == 'POST':
        data = request.get_json() or {}
        job = task_service.submit(data.get('action'), bool(data.get('dry_run', False)),
                                  params=data.get('params'))
        return jsonify({'success': True, 'task': job}), 202
    return jsonify({'tasks': task_service.list()})


@dy_bp.route('/star/tasks/<task_id>')
def star_task_detail(task_id):
    job = task_service.get(task_id)
    if not job:
        abort(404, '任务不存在')
    return jsonify({'task': job})


@dy_bp.route('/star/tasks/<task_id>/cancel', methods=['POST'])
def star_cancel_task(task_id):
    job = task_service.cancel(task_id)
    if not job:
        abort(404, '任务不存在')
    return jsonify({'success': True, 'task': job})


@dy_bp.route('/star/tasks/<task_id>/log')
def star_task_log(task_id):
    job = task_service.get(task_id)
    if not job:
        abort(404, '任务不存在')
    lines = [
        job['title'],
        '状态：' + job['status'],
        '创建：' + job['created_at'],
        '配置：' + json.dumps(job['config'], ensure_ascii=False),
        '统计：' + json.dumps(job['stats'], ensure_ascii=False),
    ]
    lines.extend(row['time'] + ' ' + row['message'] for row in job['logs'])
    if job.get('error'):
        lines.append('错误：' + job['error'])
    return Response(
        '\n'.join(lines),
        content_type='text/plain; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=dy-cloud-' + job['id'][:8] + '.txt'},
    )


def create_app():
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
    app.json.ensure_ascii = False

    frontend_origins = {
        value.strip().rstrip('/')
        for value in os.environ.get('VIDEO_FRONTEND_ORIGINS', '').split(',')
        if value.strip()
    }

    def allowed_frontend(origin):
        if origin == 'null':
            return True
        parsed = urlparse(origin or '')
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            return False
        return (
            parsed.netloc == request.host
            or parsed.hostname in ('localhost', '127.0.0.1', '::1')
            or origin.rstrip('/') in frontend_origins
        )

    @app.before_request
    def protect_local_mutations():
        origin = request.headers.get('Origin')
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH') and origin and not allowed_frontend(origin):
            abort(403, '不允许跨站修改视频库')

    @app.after_request
    def frontend_cors(response):
        origin = request.headers.get('Origin')
        if origin and allowed_frontend(origin):
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Range'
            response.headers['Access-Control-Expose-Headers'] = 'Content-Disposition, Content-Length, Content-Range, Accept-Ranges'
            response.headers['Access-Control-Max-Age'] = '600'
            response.vary.add('Origin')
        return response

    @app.errorhandler(ValueError)
    @app.errorhandler(TypeError)
    def invalid_input(error):
        return jsonify({'success': False, 'error': str(error), 'msg': str(error)}), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify({'success': False, 'error': error.description, 'msg': error.description}), error.code

    @app.errorhandler(Exception)
    def unexpected_error(error):
        app.logger.exception('请求处理失败')
        return jsonify({'success': False, 'error': str(error), 'msg': str(error)}), 500

    app.register_blueprint(dy_bp, url_prefix='/dy')
    try:
        app.register_blueprint(dy_bp, url_prefix='/dyfn', name='dyfn')
    except TypeError:
        pass

    @app.route('/')
    def root_redirect():
        return redirect('/dy/')

    return app


app = create_app()


if __name__ == "__main__":
    init_db()
    cloud_poller.start()
    PORT = int(os.environ.get('VIDEO_TAG_PORT', 81))
    print(f"[*] 115 云端视频工作台已启动，请访问: http://127.0.0.1:{PORT}/dy/")
    app.run(
        debug=False,
        host=os.environ.get('VIDEO_TAG_HOST', '127.0.0.1'),
        port=PORT,
        use_reloader=False,
        threaded=True,
    )
