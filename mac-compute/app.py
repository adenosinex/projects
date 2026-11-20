"""mac-compute: FastAPI service, SQLite queue, single-node Mac compute.

Clients only upload / poll / download. All compute runs here.
Access is protected by a username + password (see auth_store.py:
salted PBKDF2 hash in auth.json, mode 600, shared with the router
status dashboard). First visit shows a setup form; after that,
browser users log in once on the homepage (session cookie) and
API clients send HTTP Basic Auth or header X-Access-Code carrying
the password. Per-project token links (see TOKEN_SCOPES) are also
valid API credentials, scoped to that project's job types only:
a token is accepted as X-Access-Code, Authorization: Bearer,
?token= / ?access_code=, or a form field access_code / token.
The console password still grants full access (all projects + admin).
"""
import base64
import hashlib
import hmac
import re
import secrets
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

import jobs as jobstore
import stats as sysstats
import auth_store
from config import (
    APPLE_CERT_FILE, APPLE_KEY_FILE,
    AUTH_COOKIE, BASE, CERT_FILE, CP_CERT_FILE, CP_KEY_FILE, DATA,
    DATA_DIR_MAX_GB, DELETE_INPUT_AFTER_DONE_TYPES, DISK_FREE_MIN_GB,
    HEAVY_TYPES, HISTORY_MAX_AGE_DAYS, HISTORY_MAX_JOBS,
    HISTORY_PRUNE_INTERVAL_SECONDS,
    HOST, INPUT_DIR, KEY_FILE,
    MAX_RETRIES, OUTPUT_DIR, PORT,
    VIDEO_OUTPUT_RETENTION_DAYS, VIDEO_OUTPUT_RETENTION_TYPES,
    WORK_DIR,
)
from dispatch import JOB_TYPES, NO_INPUT_TYPES, run_job

app = FastAPI(title="mac-compute")
jobstore.init_db()
sysstats.init_stats_table()


# --- Auth: username + password (auth_store.py) ---
# Credentials live in auth.json (salted PBKDF2 hash, mode 600), shared
# with the router status dashboard. The user creates them on first
# visit; there is no password any more. Browser login issues a
# random session token (in-memory, 30 days); API clients send either
# HTTP Basic Auth (username:password) or header X-Access-Code with
# the password (keeps existing scripts working: just update the value).
_SESSIONS = {}  # token -> expiry epoch; both uvicorn servers share it
SESSION_TTL = 30 * 24 * 3600


def _new_session():
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = time.time() + SESSION_TTL
    return token


def _session_ok(token):
    if not token:
        return False
    exp = _SESSIONS.get(token)
    if not exp:
        return False
    if exp < time.time():
        _SESSIONS.pop(token, None)
        return False
    return True


def _set_session(resp):
    resp.set_cookie(AUTH_COOKIE, _new_session(), httponly=True,
                    secure=True, samesite="lax", path="/",
                    max_age=SESSION_TTL)
    return resp


def _is_authed(request: Request):
    # 1. Session cookie (random token issued at login).
    if _session_ok(request.cookies.get(AUTH_COOKIE, "")):
        return True
    # 2. HTTP Basic Auth: username:password.
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
        except Exception:
            user, pw = "", ""
        if user and pw and auth_store.verify_credentials(user, pw):
            return True
    # 3. X-Access-Code header now carries the new password.
    code = request.headers.get("X-Access-Code", "")
    if code and auth_store.verify_password(code):
        return True
    return False


def _unauth_error():
    if not auth_store.credentials_exist():
        return HTTPException(
            401,
            "not set up yet: open the homepage in a browser to "
            "create a username and password")
    return HTTPException(
        401,
        "login required: HTTP Basic Auth (username:password) or "
        "header X-Access-Code with the password (or a project token)")


def _require_auth(request: Request):
    """Console-only auth (session cookie, Basic Auth, or the console
    password via any channel). A project token gets 403 here: it is a
    valid credential, but not an admin credential. Job endpoints do
    not use this; they resolve the credential inline (see
    _resolve_credential) and scope it to the token's project."""
    kind, _scope = _resolve_credential(request)
    if kind == "console":
        return
    if kind == "scope":
        raise HTTPException(
            403,
            "this endpoint requires the console password; "
            "project tokens cannot use it")
    raise _unauth_error()


# --- Per-project token links (subscription links, full API credentials) ---
# Each project gets its OWN token and its OWN task page — not one
# generic token for the whole service. A token is a credential
# (random 32-hex, file mode 600) for THAT project only: the holder can
# submit that project's jobs, check status/progress, and download
# results, without the console username/password. The token is
# accepted as X-Access-Code, Authorization: Bearer, ?token= /
# ?access_code=, or a form field access_code / token (see
# _resolve_credential). The console password still grants full access
# (all projects + admin); admin endpoints (token-links, regenerate,
# stats, github, prune, delete/cancel/retry) stay console-only.
# Wrong scope or wrong/missing token -> 404 (do not reveal that the
# path exists), same pattern as ping-test's /ping/<token>/.
# Public link base: the cp.zin7.de nginx proxies everything to this
# app and has no auth_request gate, so the token link is
# https://cp.zin7.de/t/<scope>/<token>/ and needs no nginx change;
# the token check in this app is the gate.
TOKEN_SCOPES = {
    "asr": {
        "label": "语音转文字 (ASR)",
        "desc": "音频 / 视频转文字",
        "types": ("asr", "asr_batch"),
    },
    "video_transcode": {
        "label": "视频转码",
        "desc": "视频转码 / 压缩",
        "types": ("video_transcode",),
    },
    "ocr": {
        "label": "OCR 图片转文字",
        "desc": "图片 / PDF 转文字",
        "types": ("ocr",),
    },
}
_TOKEN_FILE_FMT = "token_%s"  # under BASE, mode 600
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_TOKEN_BASE_URL = "https://cp.zin7.de"


def _scope_token_path(scope):
    return BASE / (_TOKEN_FILE_FMT % scope)


def _load_scope_token(scope):
    """Current token for a scope; auto-generate on first use (mode 600)."""
    p = _scope_token_path(scope)
    try:
        tok = p.read_text().strip()
        if _HEX32.match(tok):
            return tok
    except OSError:
        pass
    tok = secrets.token_hex(16)
    p.write_text(tok)
    os.chmod(p, 0o600)
    return tok


def _check_scope_token(scope, token):
    """Wrong scope or wrong/missing token -> 404. Re-reads the file on
    every check, so a regeneration takes effect immediately (no restart)."""
    if scope not in TOKEN_SCOPES:
        raise HTTPException(404, "not found")
    if not token or not hmac.compare_digest(token, _load_scope_token(scope)):
        raise HTTPException(404, "not found")


def _match_scope_token(value):
    """If value is a project token, return its scope; else None.
    Re-reads the token files on every call (regeneration immediate)."""
    if not value or not _HEX32.match(value):
        return None
    for scope in TOKEN_SCOPES:
        if hmac.compare_digest(value, _load_scope_token(scope)):
            return scope
    return None


def _resolve_credential(request, form_access_code="", form_token=""):
    """Resolve who is calling. Returns ("console", None) for full access
    (session cookie, Basic Auth, or the console password via any
    channel), ("scope", scope) for a project token, or (None, None).
    A project token is accepted as X-Access-Code, Authorization:
    Bearer, ?token= / ?access_code=, or a form field access_code /
    token (the endpoint passes its parsed form fields in). The console
    password is accepted the same ways and always wins over a token
    when both are present. Token files are re-read on every check, so
    a regenerated token takes effect immediately (no restart)."""
    # 1. Session cookie -> console.
    if _session_ok(request.cookies.get(AUTH_COOKIE, "")):
        return ("console", None)
    # 2. Basic Auth -> console.
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
        except Exception:
            user, pw = "", ""
        if user and pw and auth_store.verify_credentials(user, pw):
            return ("console", None)
    # 3. X-Access-Code header: console password -> console; token -> scope.
    code = request.headers.get("X-Access-Code", "")
    if code:
        if auth_store.verify_password(code):
            return ("console", None)
        scope = _match_scope_token(code)
        if scope:
            return ("scope", scope)
    # 4. Authorization: Bearer <token> (or Bearer <console password>).
    if auth.startswith("Bearer "):
        b = auth[7:].strip()
        if b:
            scope = _match_scope_token(b)
            if scope:
                return ("scope", scope)
            if auth_store.verify_password(b):
                return ("console", None)
    # 5. Query params token / access_code.
    for key in ("token", "access_code"):
        v = request.query_params.get(key, "")
        if v:
            if auth_store.verify_password(v):
                return ("console", None)
            scope = _match_scope_token(v)
            if scope:
                return ("scope", scope)
    # 6. Form fields access_code / token (passed in by the endpoint).
    for v in (form_access_code, form_token):
        if v:
            if auth_store.verify_password(v):
                return ("console", None)
            scope = _match_scope_token(v)
            if scope:
                return ("scope", scope)
    return (None, None)


def _scope_token_url(scope):
    return f"{_TOKEN_BASE_URL}/t/{scope}/{_load_scope_token(scope)}/"


def _public_job(j):
    """Job dict for the token API: status/progress/instructions only.
    The internal `result` (output paths) is stripped."""
    d = dict(j)
    d.pop("result", None)
    return d


_SCOPE_INSTRUCTIONS = {
    "asr": """<p>这是 <b>语音转文字 (ASR)</b> 项目的订阅链接：<b>这个链接就是凭证</b>，拿着它就能提交任务、查进度、下载结果，不用登录。只能用这个项目的类型（asr / asr_batch），不能碰别的项目，也不能管后台。</p>
<h2>怎么提交任务（用这个链接的 token）</h2>
<pre>curl -H "X-Access-Code: __TOKEN__" -F "file=@录音.m4a" -F "type=asr" https://cp.zin7.de/api/jobs
curl -H "X-Access-Code: __TOKEN__" -F "file=@a.m4a" -F "file=@b.m4a" -F "type=asr_batch" https://cp.zin7.de/api/jobs
curl -H "X-Access-Code: __TOKEN__" -F "file=@录音.m4a" -F "type=asr" -F 'params={"model":"accurate"}' https://cp.zin7.de/api/jobs</pre>
<p>token 还可以这样传：<code>Authorization: Bearer __TOKEN__</code>、网址后面加 <code>?token=__TOKEN__</code>、或者表单里加 <code>-F "access_code=__TOKEN__"</code>。默认 Qwen3-ASR 0.6B，45 秒分片；<code>{"model":"accurate"}</code> 用 1.7B 更准。asr_batch 一次传多个文件，串行处理，结果一个 zip：每个文件一个子目录 + INDEX.csv + batch.txt（全部文本合集）。</p>
<h2>怎么查进度 / 下结果</h2>
<p>这个页面每 3 秒自动刷新，任务列表里“下载”那一列可以直接点。也可以用这个链接的 API（不用登录）：<code>GET /t/__SCOPE__/__TOKEN__/api/jobs</code> 看最近任务，<code>GET /t/__SCOPE__/__TOKEN__/api/jobs/&lt;任务ID&gt;</code> 看单个任务。下载：<code>curl https://cp.zin7.de/t/__SCOPE__/__TOKEN__/api/jobs/&lt;任务ID&gt;/download -o 结果.zip</code>，或者主接口 <code>curl -H "X-Access-Code: __TOKEN__" https://cp.zin7.de/api/jobs/&lt;任务ID&gt;/download -o 结果.zip</code>。</p>
<p>重任务一次只跑一个；失败自动重试 2 次，第 3 次失败进死信。</p>""",
    "video_transcode": """<p>这是 <b>视频转码</b> 项目的订阅链接：<b>这个链接就是凭证</b>，拿着它就能提交任务、查进度、下载结果，不用登录。只能用这个项目的类型（video_transcode），不能碰别的项目，也不能管后台。</p>
<h2>怎么提交任务（用这个链接的 token）</h2>
<pre>curl -H "X-Access-Code: __TOKEN__" -F "file=@视频.mp4" -F "type=video_transcode" https://cp.zin7.de/api/jobs</pre>
<p>token 还可以这样传：<code>Authorization: Bearer __TOKEN__</code>、网址后面加 <code>?token=__TOKEN__</code>、或者表单里加 <code>-F "access_code=__TOKEN__"</code>。默认 1080p，源视频更小就按原分辨率，绝不放大；HEVC 编码。可调参数（params JSON）：<code>{"scale":"720p"}</code>（1080p / 720p / 480p / original）、<code>{"vcodec":"h264"}</code>（hevc / h264）、<code>{"fps":30}</code>。</p>
<h2>怎么查进度 / 下结果</h2>
<p>这个页面每 3 秒自动刷新，任务列表里“下载”那一列可以直接点。也可以用这个链接的 API（不用登录）：<code>GET /t/__SCOPE__/__TOKEN__/api/jobs</code>，单个任务 <code>GET /t/__SCOPE__/__TOKEN__/api/jobs/&lt;任务ID&gt;</code>。下载：<code>curl https://cp.zin7.de/t/__SCOPE__/__TOKEN__/api/jobs/&lt;任务ID&gt;/download -o 结果.zip</code>，或者主接口 <code>curl -H "X-Access-Code: __TOKEN__" https://cp.zin7.de/api/jobs/&lt;任务ID&gt;/download -o 结果.zip</code>。视频成品只保留 3 天，记得及时下载。</p>""",
    "ocr": """<p>这是 <b>OCR 图片转文字</b> 项目的订阅链接：<b>这个链接就是凭证</b>，拿着它就能提交任务、查进度、下载结果，不用登录。只能用这个项目的类型（ocr），不能碰别的项目，也不能管后台。</p>
<h2>怎么提交任务（用这个链接的 token）</h2>
<pre>curl -H "X-Access-Code: __TOKEN__" -F "file=@图片.png" -F "type=ocr" https://cp.zin7.de/api/jobs</pre>
<p>token 还可以这样传：<code>Authorization: Bearer __TOKEN__</code>、网址后面加 <code>?token=__TOKEN__</code>、或者表单里加 <code>-F "access_code=__TOKEN__"</code>。RapidOCR，中英文。可调参数（params JSON）：<code>{"mode":"fast"}</code>（fast=RapidOCR；vl=PaddleOCR-VL；long=长文档）、<code>{"lang":"ch_en"}</code>、<code>{"format":"txt"}</code>（txt / json）。</p>
<h2>怎么查进度 / 下结果</h2>
<p>这个页面每 3 秒自动刷新，任务列表里“下载”那一列可以直接点。也可以用这个链接的 API（不用登录）：<code>GET /t/__SCOPE__/__TOKEN__/api/jobs</code>，单个任务 <code>GET /t/__SCOPE__/__TOKEN__/api/jobs/&lt;任务ID&gt;</code>。下载：<code>curl https://cp.zin7.de/t/__SCOPE__/__TOKEN__/api/jobs/&lt;任务ID&gt;/download -o 结果.zip</code>，或者主接口 <code>curl -H "X-Access-Code: __TOKEN__" https://cp.zin7.de/api/jobs/&lt;任务ID&gt;/download -o 结果.zip</code>。</p>""",
}

TOKEN_PAGE_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mac-compute · __LABEL__</title><style>body{font-family:sans-serif;max-width:900px;margin:2em auto;padding:0 1em}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px;text-align:left}
progress{width:120px}code{background:#f0f0f0;padding:1px 4px}pre{background:#f6f6f6;padding:10px;overflow:auto}
.note{color:#888;font-size:14px}</style></head><body>
<h1>mac-compute · __LABEL__</h1>
<p>__DESC__ · 订阅链接就是凭证：拿着这个链接就能提交这个项目的任务、查进度、下载结果，不用登录；不能碰别的项目，也不能管后台。</p>
__INSTR__
<h2>任务 <span id="updated" style="font-size:13px;color:#888;font-weight:normal"></span></h2>
<p><button onclick="load()">刷新</button> <span class="note">每 3 秒自动刷新，只显示这个项目的任务。已完成的任务点“下载”拿结果。</span></p>
<table><tr><th>ID</th><th>类型</th><th>文件</th><th>状态</th><th>进度</th><th>信息</th><th>创建时间</th><th>开始</th><th>完成</th><th>下载</th></tr><tbody id="tb"></tbody></table>
<script>
const API='/t/__SCOPE__/__TOKEN__/api/jobs';
function fmt(ts){if(!ts)return '';let d=new Date(ts*1000);return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')+':'+String(d.getSeconds()).padStart(2,'0');}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
async function load(){let r;try{r=await fetch(API);}catch(e){document.getElementById('tb').innerHTML='<tr><td colspan="10">读取失败：网络错误</td></tr>';return;}
if(!r.ok){document.getElementById('tb').innerHTML='<tr><td colspan="10">读取失败：'+r.status+'</td></tr>';return;}
let j=await r.json();
document.getElementById('tb').innerHTML=j.length?j.map(x=>`<tr><td>${esc(x.id)}</td><td>${esc(x.type)}</td><td>${esc(x.filename)}</td><td>${esc(x.status)}</td><td><progress value="${x.progress}" max="1"></progress> ${Math.round(x.progress*100)}%</td><td>${esc(x.message||'')}${esc(x.error?' '+x.error:'')}</td><td>${fmt(x.created_at)}</td><td>${fmt(x.started_at)}</td><td>${fmt(x.finished_at)}</td><td>${x.status==='done'?`<a href="${API}/${encodeURIComponent(x.id)}/download">下载</a>`:''}</td></tr>`).join(''):'<tr><td colspan="10">暂无任务</td></tr>';
document.getElementById('updated').textContent='（更新于 '+new Date().toLocaleTimeString()+'）';}
setInterval(load,3000);load();
</script></body></html>"""


@app.get("/t/{scope}/{token}/", response_class=HTMLResponse)
def token_page(scope: str, token: str):
    _check_scope_token(scope, token)
    info = TOKEN_SCOPES[scope]
    html = TOKEN_PAGE_HTML.replace("__INSTR__", _SCOPE_INSTRUCTIONS[scope])
    html = (html.replace("__SCOPE__", scope).replace("__TOKEN__", token)
            .replace("__LABEL__", info["label"]).replace("__DESC__", info["desc"]))
    return html


@app.get("/t/{scope}/{token}/api/jobs")
def token_list_jobs(scope: str, token: str):
    _check_scope_token(scope, token)
    return [_public_job(j) for j in
            jobstore.list_jobs_by_types(TOKEN_SCOPES[scope]["types"], limit=50)]


@app.get("/t/{scope}/{token}/api/jobs/{job_id}")
def token_get_job(scope: str, token: str, job_id: str):
    _check_scope_token(scope, token)
    j = jobstore.get_job(job_id)
    if not j or j["type"] not in TOKEN_SCOPES[scope]["types"]:
        raise HTTPException(404, "job not found")
    return _public_job(j)


def _job_result_response(job_id):
    """Build the FileResponse for a done job's result zip (the zip is
    created on first download). The caller must already have checked
    the job is done and its output is present. FileResponse supports
    Range requests, so downloads are resumable."""
    out_dir = OUTPUT_DIR / job_id
    zip_path = out_dir / "result.zip"
    if not zip_path.exists():
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(out_dir.rglob("*")):
                if p.is_file() and p.name != "result.zip":
                    z.write(p, p.relative_to(out_dir))
    return FileResponse(zip_path, filename=f"{job_id}.zip")


@app.get("/t/{scope}/{token}/api/jobs/{job_id}/download")
def token_download_job(scope: str, token: str, job_id: str):
    """Download a done job's result with the project token (no login).
    Same zip as the main download endpoint; scoped to the token's
    job types (a job of another project -> 404)."""
    _check_scope_token(scope, token)
    j = jobstore.get_job(job_id)
    if not j or j["type"] not in TOKEN_SCOPES[scope]["types"]:
        raise HTTPException(404, "result not ready")
    if j["status"] != "done":
        raise HTTPException(404, "result not ready")
    if j.get("output_deleted"):
        raise HTTPException(410, "输出文件已被清理（保留期过后自动删除，或磁盘紧张时被清理），请重新提交任务")
    return _job_result_response(job_id)


@app.get("/api/token-links")
def token_links(request: Request):
    """Login-gated: the three per-project token links for the owner to
    copy. The token value only ever appears here, behind the login."""
    _require_auth(request)
    return {"links": [
        {"scope": scope, "label": info["label"], "desc": info["desc"],
         "types": list(info["types"]), "url": _scope_token_url(scope)}
        for scope, info in TOKEN_SCOPES.items()]}


@app.post("/api/token-links/{scope}/regenerate")
def token_regenerate(scope: str, request: Request):
    """Login-gated: regenerate one scope's token. The old token stops
    working immediately (the check re-reads the file every time)."""
    _require_auth(request)
    if scope not in TOKEN_SCOPES:
        raise HTTPException(404, "unknown scope")
    tok = secrets.token_hex(16)
    p = _scope_token_path(scope)
    p.write_text(tok)
    os.chmod(p, 0o600)
    return {"ok": True, "scope": scope, "url": _scope_token_url(scope)}


SETUP_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>mac-compute · 首次设置</title><style>body{font-family:sans-serif;max-width:480px;margin:4em auto;padding:0 1em}
label{display:block;margin:12px 0 4px}input{width:100%;padding:8px;box-sizing:border-box}
button{margin-top:16px;padding:8px 18px}</style></head><body>
<h1>mac-compute · 首次设置</h1>
<p>第一次使用，先创建一个用户名和密码。同一个用户名和密码也能登录路由器状态面板。</p>
<p style="color:#c00"><!--msg--></p>
<form method="post" action="/setup">
<label>用户名</label>
<input name="username" placeholder="用户名" autofocus required>
<label>密码（至少 8 位，建议 12 位以上）</label>
<input type="password" name="password" required>
<label>再输一次密码</label>
<input type="password" name="password_confirm" required>
<button>创建并进入</button></form>
<p style="color:#888">密码只以加盐哈希形式保存在这台 Mac 上（auth.json），不要把密码发给别人。</p>
</body></html>"""

LOGIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>mac-compute · 登录</title><style>body{font-family:sans-serif;max-width:480px;margin:4em auto;padding:0 1em}
label{display:block;margin:12px 0 4px}input{width:100%;padding:8px;box-sizing:border-box}
button{margin-top:16px;padding:8px 18px}</style></head><body>
<h1>mac-compute</h1>
<p>Mac mini M4 上的算力服务，请输入用户名和密码。</p>
<p style="color:#c00"><!--msg--></p>
<form method="post" action="/login">
<label>用户名</label>
<input name="username" placeholder="用户名" autofocus required>
<label>密码</label>
<input type="password" name="password" required>
<button>进入</button></form>
<p style="color:#888">用户名或密码不对会回到这里。不要把密码发给别人。</p>
</body></html>"""

CONSOLE_HTML = """
<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>mac-compute</title><style>body{font-family:sans-serif;max-width:900px;margin:2em auto;padding:0 1em}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px;text-align:left}
progress{width:120px}code{background:#f0f0f0;padding:1px 4px}pre{background:#f6f6f6;padding:10px;overflow:auto}
details{border:1px solid #ddd;border-radius:8px;padding:10px 14px;margin:12px 0}
summary{font-size:17px;font-weight:700;cursor:pointer}
summary .sub{font-size:13px;color:#888;font-weight:400}
.tok{word-break:break-all}
h2{margin-top:1.5em}</style></head><body>
<h1>mac-compute · Mac mini M4</h1>
<p>所有算力都在 Mac 上，客户端只上传 / 看进度 / 下载结果。<a href="/logout">退出登录</a> · <a href="/github">GitHub 上传</a></p>
<p style="color:#888">点开一个项目，看它的订阅链接、调用方法和任务记录。每个项目有自己的订阅链接（token），拿着链接不用登录就能提交这个项目的任务、查进度、下载结果。</p>

<details id="sysBox">
<summary>系统占用 <span id="now" style="font-size:13px;color:#888;font-weight:normal"></span></summary>
<p>每分钟采一次，保留 30 天。灰色竖条是任务运行的时间段。<span id="ranges">
<button onclick="setMode('live')">实时</button>
<button onclick="setMode('hist',0.25)">15分钟</button>
<button onclick="setMode('hist',1)">1小时</button>
<button onclick="setMode('hist',6)">6小时</button>
<button onclick="setMode('hist',24)">24小时</button>
<button onclick="setMode('hist',168)">7天</button>
<button onclick="setMode('hist',720)">30天</button></span></p>
<div>CPU % <canvas id="c_cpu" width="860" height="120" style="width:100%;height:120px;border:1px solid #ddd"></canvas></div>
<div>内存 % <canvas id="c_mem" width="860" height="120" style="width:100%;height:120px;border:1px solid #ddd"></canvas></div>
<div>磁盘 % <canvas id="c_disk" width="860" height="120" style="width:100%;height:120px;border:1px solid #ddd"></canvas></div>
</details>

<h2>项目</h2>

<details class="proj" data-scope="asr" data-types="asr,asr_batch">
<summary>语音转文字 (ASR) <span class="sub">音频 / 视频转文字</span></summary>
<p><b>订阅链接</b><br><code class="tok" data-tok>正在读取……</code> <button data-copy>复制</button> <button data-regen>重新生成</button></p>
<p><b>调用</b></p>
<pre data-curl>正在读取订阅链接……</pre>
<p>默认：Qwen3-ASR 0.6B，45 秒分片；想更准传 <code>{"model":"accurate"}</code>（1.7B）。一次传多个文件用 asr_batch，串行处理，结果一个 zip。</p>
<form data-submit>
<input type="file" name="file" multiple required>
<select name="type"><option value="asr">asr 单个音频</option><option value="asr_batch">asr_batch 多个音频一次转完</option></select>
<input name="params" placeholder='{"model":"accurate"}' size="20">
<button>提交任务</button></form>
<p data-msg></p>
<table><tr><th>ID</th><th>类型</th><th>文件</th><th>状态</th><th>进度</th><th>信息</th><th>操作</th></tr><tbody data-tb></tbody></table>
</details>

<details class="proj" data-scope="video_transcode" data-types="video_transcode">
<summary>视频转码 <span class="sub">视频转码 / 压缩</span></summary>
<p><b>订阅链接</b><br><code class="tok" data-tok>正在读取……</code> <button data-copy>复制</button> <button data-regen>重新生成</button></p>
<p><b>调用</b></p>
<pre data-curl>正在读取订阅链接……</pre>
<p>默认：1080p，源视频更小就按原分辨率，绝不放大；HEVC。成品只保留 3 天，记得及时下载。</p>
<form data-submit>
<input type="file" name="file" required>
<input type="hidden" name="type" value="video_transcode">
<input name="params" placeholder='{"max_height":720}' size="20">
<button>提交任务</button></form>
<p data-msg></p>
<table><tr><th>ID</th><th>类型</th><th>文件</th><th>状态</th><th>进度</th><th>信息</th><th>操作</th></tr><tbody data-tb></tbody></table>
</details>

<details class="proj" data-scope="ocr" data-types="ocr">
<summary>OCR 图片转文字 <span class="sub">图片 / PDF 转文字</span></summary>
<p><b>订阅链接</b><br><code class="tok" data-tok>正在读取……</code> <button data-copy>复制</button> <button data-regen>重新生成</button></p>
<p><b>调用</b></p>
<pre data-curl>正在读取订阅链接……</pre>
<p>默认：RapidOCR，中英文。</p>
<form data-submit>
<input type="file" name="file" required>
<input type="hidden" name="type" value="ocr">
<input name="params" placeholder='{}' size="20">
<button>提交任务</button></form>
<p data-msg></p>
<table><tr><th>ID</th><th>类型</th><th>文件</th><th>状态</th><th>进度</th><th>信息</th><th>操作</th></tr><tbody data-tb></tbody></table>
</details>

<details class="proj" data-scope="" data-types="video_thumbs,image_select,image_batch,face_cluster,data_csv,novel_index,vps_probe">
<summary>其他 <span class="sub">没有订阅链接，只能登录后在这里提交</span></summary>
<p>这几个类型没有订阅链接，只能登录后在这里提交。</p>
<p><b>调用</b>（用控制台密码）</p>
<pre>curl -u 用户名:密码 -F "file=@截图.zip" -F "type=image_select" -F 'params={"top_n":10}' https://cp.zin7.de/api/jobs
curl -u 用户名:密码 -F "file=@视频.mp4" -F "type=video_thumbs" https://cp.zin7.de/api/jobs
curl -u 用户名:密码 -F 'params={"target":"https://..."}' -F "type=vps_probe" https://cp.zin7.de/api/jobs</pre>
<p>类型：video_thumbs 从视频抽帧选优（每秒 1 帧，留 Top 10）；image_select 多张截图选优（传 zip，留 Top 10）；image_batch 图片批量压缩（JPG 85，长边 1920）；data_csv CSV 清洗去重；vps_probe 测 VPS 直连（不用传文件）；face_cluster / novel_index 按默认跑。</p>
<form data-submit>
<input type="file" name="file" multiple>
<select name="type">
<option value="video_thumbs">video_thumbs 视频抽帧选优</option>
<option value="image_select">image_select 多张截图选优</option>
<option value="image_batch">image_batch 图片批量压缩</option>
<option value="data_csv">data_csv CSV 清洗去重</option>
<option value="face_cluster">face_cluster 人脸聚类</option>
<option value="novel_index">novel_index 小说索引</option>
<option value="vps_probe">vps_probe 测 VPS 直连（不用传文件）</option>
</select>
<input name="params" placeholder='{"top_n":10}' size="20">
<button>提交任务</button></form>
<p data-msg></p>
<table><tr><th>ID</th><th>类型</th><th>文件</th><th>状态</th><th>进度</th><th>信息</th><th>操作</th></tr><tbody data-tb></tbody></table>
</details>

<h2>历史管理</h2>
<p><button onclick="pruneHist()">清理旧历史</button> <button onclick="clearAllHist()" style="color:#c00">清除全部历史</button> <span id="pruneMsg"></span></p>
<p style="color:#888">历史最多保留 1000 条 / 90 天，超出的每小时自动清理一次；“清理旧历史”是立即执行一次。删除任务会同时删掉它的输入和输出文件，删了就找不回来了。进行中或排队中的任务不能删。视频成品（video_transcode）完成 3 天后自动删除，只留任务记录；磁盘紧张时（data/ 超过 30 GB 或剩余空间不足 20 GB）优先清理视频成品，仍不足时按大小清理其他已完成任务的输出文件。“清除全部历史”会删掉所有已完成/失败/已取消的任务（进行中或排队中的除外），需要点两次确认。</p>

<script>
function rowHtml(x){
  return `<tr><td>${x.id}</td><td>${x.type}</td><td>${x.filename}</td><td>${x.status}</td><td><progress value="${x.progress}" max="1"></progress> ${Math.round(x.progress*100)}%</td><td>${x.message||''} ${x.error||''}</td><td>${x.status==='done'?(x.output_deleted?'输出已清理':`<a href="/api/jobs/${x.id}/download">下载</a>`):''} ${['failed','dead'].includes(x.status)?`<button onclick="retry('${x.id}')">重试</button>`:''} ${['queued','running'].includes(x.status)?`<button onclick="cancel('${x.id}')">取消</button>`:''} ${['done','failed','dead','cancelled'].includes(x.status)?`<button onclick="delJob('${x.id}')">删除</button>`:''}</td></tr>`;
}
async function load(){
  for (let sec of document.querySelectorAll('.proj')){
    let types = sec.dataset.types;
    let r = await fetch('/api/jobs?type='+encodeURIComponent(types));
    if (r.status===401){ location.href='/'; return; }
    if (!r.ok) continue;
    let j = await r.json();
    sec.querySelector('[data-tb]').innerHTML = j.map(rowHtml).join('');
  }
}
async function retry(id){await fetch(`/api/jobs/${id}/retry`,{method:'POST'});load();}
async function cancel(id){await fetch(`/api/jobs/${id}/cancel`,{method:'POST'});load();}
async function delJob(id){if(!confirm('删除这个任务？它的输入和输出文件也会一起删掉，删了就找不回来了。'))return;let r=await fetch('/api/jobs/'+id,{method:'DELETE'});let j=await r.json();if(!r.ok){alert('删除失败：'+(j.detail||JSON.stringify(j)));return;}load();}
async function pruneHist(){if(!confirm('按保留规则清理旧历史（最多 1000 条 / 90 天；视频成品只保留 3 天）？超出的任务和它们的文件会被删掉。'))return;let r=await fetch('/api/jobs/prune',{method:'POST'});let j=await r.json();document.getElementById('pruneMsg').textContent=r.ok?('已清理 '+j.deleted_jobs+' 个任务，释放 '+(j.freed_bytes/1048576).toFixed(1)+' MB'+(j.video_outputs_removed?('，视频成品 '+j.video_outputs_removed+' 个'):'' )+(j.guard_triggered?('，磁盘保护清理 '+j.guard_removed_jobs+' 个'):'')):('清理失败：'+(j.detail||JSON.stringify(j)));load();}
async function clearAllHist(){if(!confirm('删除全部历史？所有已完成、失败、已取消的任务记录和它们的输入输出文件都会被删掉，删了就找不回来了。进行中或排队中的任务不会被删。'))return;if(!confirm('再确认一次：真的要清空全部历史吗？'))return;let r=await fetch('/api/jobs/clear-all',{method:'POST'});let j=await r.json();document.getElementById('pruneMsg').textContent=r.ok?('已清除 '+j.deleted_jobs+' 个任务，释放 '+(j.freed_bytes/1048576).toFixed(1)+' MB'+(j.active_jobs?('，还有 '+j.active_jobs+' 个进行中/排队中的任务未删除'):'')):('清除失败：'+(j.detail||JSON.stringify(j)));load();}
async function loadTokenLinks(){
  let r=await fetch('/api/token-links');if(r.status===401){location.href='/';return;}
  let j=await r.json();
  let byScope={}; j.links.forEach(l=>byScope[l.scope]=l);
  for (let sec of document.querySelectorAll('.proj[data-scope]')){
    let sc=sec.dataset.scope; if(!sc) continue;
    let l=byScope[sc]; if(!l) continue;
    sec.querySelector('[data-tok]').textContent=l.url;
    let tok=l.url.split('/').filter(Boolean).pop();
    let ex = sc==='asr' ? 'curl -H "X-Access-Code: '+tok+'" -F "file=@录音.m4a" -F "type=asr" https://cp.zin7.de/api/jobs'
      : sc==='video_transcode' ? 'curl -H "X-Access-Code: '+tok+'" -F "file=@视频.mp4" -F "type=video_transcode" https://cp.zin7.de/api/jobs'
      : 'curl -H "X-Access-Code: '+tok+'" -F "file=@图片.png" -F "type=ocr" https://cp.zin7.de/api/jobs';
    sec.querySelector('[data-curl]').textContent=ex;
  }
}
document.querySelectorAll('.proj[data-scope]').forEach(sec=>{
  let sc=sec.dataset.scope; if(!sc) return;
  sec.querySelector('[data-copy]').onclick=async e=>{
    let url=sec.querySelector('[data-tok]').textContent;
    try{await navigator.clipboard.writeText(url);e.target.textContent='已复制';setTimeout(()=>e.target.textContent='复制',1500);}catch(err){prompt('复制这个链接：',url);}
  };
  sec.querySelector('[data-regen]').onclick=async e=>{
    if(!confirm('重新生成这个项目的订阅链接？旧链接会立刻失效。'))return;
    let r=await fetch('/api/token-links/'+sc+'/regenerate',{method:'POST'});
    let j=await r.json();if(!r.ok){alert('重新生成失败：'+(j.detail||JSON.stringify(j)));return;}
    loadTokenLinks();
  };
});
document.querySelectorAll('form[data-submit]').forEach(f=>{
  f.onsubmit=async e=>{
    e.preventDefault();
    let sec=f.closest('.proj');
    let msg=sec.querySelector('[data-msg]');
    let r=await fetch('/api/jobs',{method:'POST',body:new FormData(f)});
    let j=await r.json();
    msg.textContent=r.ok?('已提交 '+j.id):('失败 '+JSON.stringify(j));
    load();
  };
});
loadTokenLinks();
setInterval(load,3000);load();
let curHours=24, mode='hist', liveTimer=null;
function setMode(m,h){
  mode=m;
  if(m==='hist'){curHours=h;if(liveTimer){clearInterval(liveTimer);liveTimer=null;}loadStats();}
  else{if(liveTimer)clearInterval(liveTimer);loadLive();liveTimer=setInterval(loadLive,5000);}
}
async function loadLive(){
  let r=await fetch('/api/stats/live');if(!r.ok)return;
  let d=await r.json(),pts=d.points,t1=Date.now()/1000,t0=t1-900;
  if(d.latest){let L=d.latest;document.getElementById('now').textContent='现在：CPU '+L.cpu+'% · 内存 '+L.mem+'%（'+L.mem_used_gb+'/'+L.mem_total_gb+' GB）· 磁盘 '+L.disk+'%（'+L.disk_used_gb+'/'+L.disk_total_gb+' GB）';}
  drawChart('c_cpu',pts,'cpu',[],t0,t1);
  drawChart('c_mem',pts,'mem',[],t0,t1);
  drawChart('c_disk',pts,'disk',[],t0,t1);
}
function drawChart(id,pts,key,jobs,t0,t1){
  let cv=document.getElementById(id),ctx=cv.getContext('2d'),W=cv.width,H=cv.height;
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='rgba(0,0,0,0.07)';
  (jobs||[]).forEach(j=>{let x0=(Math.max(j.started_at,t0)-t0)/(t1-t0)*W,x1=((j.finished_at||t1)-t0)/(t1-t0)*W;if(x1>0&&x0<W)ctx.fillRect(Math.max(x0,0),0,Math.min(x1,W)-Math.max(x0,0),H);});
  ctx.strokeStyle='#eee';ctx.fillStyle='#888';ctx.font='10px sans-serif';
  [0,50,100].forEach(v=>{let y=H-4-(v/100)*(H-14);ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke();ctx.fillText(v+'%',2,y-2);});
  if(!pts.length){ctx.fillText('暂无数据，等一分钟采样',10,H/2);return;}
  ctx.strokeStyle='#2563eb';ctx.lineWidth=1.5;ctx.beginPath();
  pts.forEach((p,i)=>{let x=(p.ts-t0)/(t1-t0)*W,y=H-4-(p[key]/100)*(H-14);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
  ctx.stroke();
  let fmt=t=>{let d=new Date(t*1000);return String(d.getMonth()+1)+'/'+d.getDate()+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');};
  ctx.fillText(fmt(t0),4,H-2);let w=ctx.measureText(fmt(t1)).width;ctx.fillText(fmt(t1),W-w-2,H-2);
}
async function loadStats(h){
  curHours=h||curHours;
  let r=await fetch('/api/stats?hours='+curHours);if(!r.ok)return;
  let d=await r.json(),pts=d.points,t1=Date.now()/1000,t0=t1-curHours*3600;
  if(d.latest){let L=d.latest;document.getElementById('now').textContent='现在：CPU '+L.cpu+'% · 内存 '+L.mem+'%（'+L.mem_used_gb+'/'+L.mem_total_gb+' GB）· 磁盘 '+L.disk+'%（'+L.disk_used_gb+'/'+L.disk_total_gb+' GB）';}
  drawChart('c_cpu',pts,'cpu',d.jobs,t0,t1);
  drawChart('c_mem',pts,'mem',d.jobs,t0,t1);
  drawChart('c_disk',pts,'disk',d.jobs,t0,t1);
}
loadStats(24);setInterval(()=>{if(mode==='hist')loadStats();},60000);
</script></body></html>
"""


GITHUB_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>mac-compute · GitHub 上传</title><style>body{font-family:sans-serif;max-width:640px;margin:2em auto;padding:0 1em}
label{display:block;margin:12px 0 4px}input,select{width:100%;padding:8px;box-sizing:border-box}
button{margin-top:16px;padding:8px 18px}code{background:#f0f0f0;padding:1px 4px}
.note{color:#555;font-size:14px}</style></head><body>
<h1>GitHub 上传</h1>
<p><a href="/">← 回控制台</a></p>
<p id="status">正在读取当前配置……</p>
<form id="f">
<label>GitHub 账号</label>
<input name="account" placeholder="cccaius 或 adenosinex" required>
<label>仓库名</label>
<input name="repo" value="projects" required>
<label>Token</label>
<input name="token" type="password" autocomplete="off" placeholder="ghp_ 开头或 github_pat_ 开头">
<label>分支</label>
<input name="branch" value="main" required>
<label>公开 / 私有</label>
<select name="visibility"><option value="public" selected>公开</option><option value="private">私有</option></select>
<button>保存</button></form>
<p id="msg"></p>
<p><button id="test" type="button">验证 Token（检查属于哪个账号）</button> <span id="testmsg"></span></p>
<h2>Token 去哪拿</h2>
<ol>
<li>在 GitHub 上点头像 → Settings → Developer settings → Personal access tokens → <b>Fine-grained tokens</b> → Generate new token。</li>
<li>选 <b>Only select repositories</b>，勾上要用的仓库（如果仓库还没建，先选 All repositories，建好后再改回来；或者建好仓库再来填）。</li>
<li>Permissions 里把 <b>Contents</b> 设为 <b>Read and write</b>，生成后复制下来。</li>
<li>也可以用经典 token（Tokens (classic) → Generate new token (classic)，勾 <code>repo</code>），更简单，但权限是整个账号的。</li>
</ol>
<p class="note">Token 只保存在这台 Mac 的一个文件里（<code>data/github.json</code>，只有本机能读），不会出现在聊天记录里，也不会进代码仓库。保存后在聊天里告诉 Muse，Muse 会用它创建仓库、完成首次推送，并开启每小时自动推送。以后想换账号或换 token，回来改了保存就行；想停用就把 token 在 GitHub 上删掉（Revoke）。</p>
<script>
async function loadStatus(){
  let r=await fetch('/api/github/config');if(r.status===401){location.href='/';return;}
  let j=await r.json();
  document.getElementById('status').textContent=j.configured?
    ('已配置：'+j.account+' / '+j.repo+'（'+j.branch+'，'+(j.visibility==='public'?'公开':'私有')+'）· 保存于 '+j.updated_at):
    '未配置：还没有保存过账号和 token。';
  if(j.configured){
    document.querySelector('[name=account]').value=j.account;
    document.querySelector('[name=repo]').value=j.repo;
    document.querySelector('[name=branch]').value=j.branch;
    document.querySelector('[name=visibility]').value=j.visibility;
    document.querySelector('[name=token]').placeholder='留空保持原来的 token';
  }
}
document.getElementById('f').onsubmit=async e=>{e.preventDefault();
  let fd=new FormData(e.target),body={};fd.forEach((v,k)=>body[k]=v);
  let r=await fetch('/api/github/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let j=await r.json();
  document.getElementById('msg').textContent=r.ok?('已保存：'+j.account+' / '+j.repo+'。请在聊天里告诉 Muse 执行推送。'):('保存失败：'+(j.detail||JSON.stringify(j)));
  loadStatus();};
document.getElementById('test').onclick=async ()=>{
  let r=await fetch('/api/github/test',{method:'POST'});
  let j=await r.json();
  document.getElementById('testmsg').textContent=r.ok?('Token 有效，属于账号 '+j.login+'。'+(j.warning?' '+j.warning:'')):('验证失败：'+(j.detail||JSON.stringify(j)));};
loadStatus();
</script></body></html>"""


GITHUB_CONFIG = DATA / "github.json"


def _load_github_config():
    """GitHub upload config (data/github.json, mode 600). The token is
    never returned by any endpoint and never enters the repo."""
    if not GITHUB_CONFIG.exists():
        return None
    try:
        return json.loads(GITHUB_CONFIG.read_text())
    except Exception:
        return None


def _save_github_config(cfg):
    DATA.mkdir(parents=True, exist_ok=True)
    GITHUB_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    os.chmod(GITHUB_CONFIG, 0o600)


@app.get("/", response_class=HTMLResponse)
def console(request: Request):
    if not auth_store.credentials_exist():
        return SETUP_HTML
    if not _is_authed(request):
        return LOGIN_HTML
    return CONSOLE_HTML


@app.post("/setup")
def setup(username: str = Form(...), password: str = Form(...),
          password_confirm: str = Form(...)):
    if auth_store.credentials_exist():
        return RedirectResponse("/", status_code=303)
    username = (username or "").strip()
    if not username:
        return HTMLResponse(SETUP_HTML.replace("<!--msg-->", "用户名不能为空。"), status_code=400)
    if password != password_confirm:
        return HTMLResponse(SETUP_HTML.replace("<!--msg-->", "两次输入的密码不一致。"), status_code=400)
    if len(password) < 8:
        return HTMLResponse(SETUP_HTML.replace("<!--msg-->", "密码至少 8 位，建议 12 位以上。"), status_code=400)
    try:
        auth_store.create_credentials(username, password)
    except ValueError as e:
        return HTMLResponse(SETUP_HTML.replace("<!--msg-->", str(e)), status_code=400)
    except RuntimeError:
        return RedirectResponse("/", status_code=303)
    return _set_session(RedirectResponse("/", status_code=303))


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    if auth_store.verify_credentials(username, password):
        return _set_session(RedirectResponse("/", status_code=303))
    return HTMLResponse(LOGIN_HTML.replace("<!--msg-->", "用户名或密码不对，请重试。"), status_code=401)


@app.get("/logout")
def logout(request: Request):
    _SESSIONS.pop(request.cookies.get(AUTH_COOKIE, ""), None)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(AUTH_COOKIE, path="/")
    return resp


@app.get("/health")
def health():
    return {"status": "ok", "types": JOB_TYPES}


@app.get("/api/stats")
def get_stats(request: Request, hours: float = 24):
    """History of CPU/mem/disk %, one point per minute.
    hours: 0.25, 1, 6, 24, 168, 720. jobs: tasks that ran in the range."""
    _require_auth(request)
    hours = min(max(hours, 0.25), 24 * 30)
    now = time.time()
    since = now - hours * 3600
    points = sysstats.query_stats(since)
    return {
        "hours": hours,
        "points": points,
        "jobs": sysstats.job_intervals(since, now),
        "latest": points[-1] if points else None,
    }


@app.get("/api/stats/live")
def get_stats_live(request: Request):
    """Real-time: last 15 min at 5-second resolution (memory only)."""
    _require_auth(request)
    points = sysstats.get_fast(900)
    return {"points": points, "latest": points[-1] if points else None}


# --- History deletion + retention (history must not grow forever) ---
_HEX16 = re.compile(r"^[0-9a-f]{16}$")  # job ids are uuid4().hex[:16]


def _rm_file(p):
    """Unlink one file; returns (1, size) or (0, 0). Never raises."""
    try:
        if p.is_file():
            size = p.stat().st_size
            p.unlink()
            return 1, size
    except OSError:
        pass
    return 0, 0


def _rm_tree(d):
    """Remove one directory tree; returns (files_removed, bytes_freed)."""
    try:
        if d.is_dir():
            n, b = 0, 0
            for q in d.rglob("*"):
                if q.is_file():
                    try:
                        b += q.stat().st_size
                    except OSError:
                        pass
                    n += 1
            shutil.rmtree(d)
            return n, b
    except OSError:
        pass
    return 0, 0


def _delete_job_files(job_id):
    """Remove a job's input files, output dir, and work files. Returns
    (files_removed, bytes_freed). Only touches paths directly under the
    service's own data dirs, and only for a well-formed job id, so a
    weird id can never escape onto the rest of the disk. Missing files
    are fine (another thread may have removed them already)."""
    if not _HEX16.match(job_id or ""):
        return 0, 0
    removed, freed = 0, 0
    for p in INPUT_DIR.glob(f"{job_id}_*"):
        if p.parent == INPUT_DIR:
            n, b = _rm_file(p)
            removed += n
            freed += b
    out_dir = OUTPUT_DIR / job_id
    if out_dir.parent == OUTPUT_DIR:
        n, b = _rm_tree(out_dir)
        removed += n
        freed += b
    for p in WORK_DIR.glob(f"{job_id}*"):
        if p.parent == WORK_DIR:
            if p.is_file():
                n, b = _rm_file(p)
            else:
                n, b = _rm_tree(p)
            removed += n
            freed += b
    return removed, freed


def _cleanup_orphan_files():
    """Remove input files / output dirs whose job id has no DB row (left
    behind by an earlier DB state). A file can only be an orphan: rows
    are created before files are written, and a live job always has a
    row. Returns (files_removed, bytes_freed, orphan_ids)."""
    ids = jobstore.all_job_ids()
    removed, freed = 0, 0
    orphans = set()
    for p in INPUT_DIR.iterdir():
        job_id = p.name.split("_", 1)[0]
        if p.is_file() and _HEX16.match(job_id) and job_id not in ids:
            n, b = _rm_file(p)
            removed += n
            freed += b
            if n:
                orphans.add(job_id)
    for d in OUTPUT_DIR.iterdir():
        if d.is_dir() and _HEX16.match(d.name) and d.name not in ids:
            n, b = _rm_tree(d)
            removed += n
            freed += b
            if n or not d.exists():
                orphans.add(d.name)
    for p in WORK_DIR.iterdir():
        job_id = p.name.split("_", 1)[0]
        if _HEX16.match(job_id) and job_id not in ids:
            if p.is_file():
                n, b = _rm_file(p)
            else:
                n, b = _rm_tree(p)
            removed += n
            freed += b
            if n:
                orphans.add(job_id)
    return removed, freed, sorted(orphans)


def _output_dir_size(job_id):
    """Total bytes of a job's output directory (0 if missing)."""
    out_dir = OUTPUT_DIR / job_id
    if not out_dir.is_dir():
        return 0
    total = 0
    for q in out_dir.rglob("*"):
        if q.is_file():
            try:
                total += q.stat().st_size
            except OSError:
                pass
    return total


def _remove_job_output(job_id, reason):
    """Delete one finished job's OUTPUT files only (input files and
    the DB row are untouched). Marks the row output_deleted=1 so the
    UI shows it as removed instead of a broken download link.
    Returns (files_removed, bytes_freed). Never raises. Only touches
    the job's own output directory."""
    if not _HEX16.match(job_id or ""):
        return 0, 0
    n, b = _rm_tree(OUTPUT_DIR / job_id)
    if n:
        try:
            jobstore.update_job(job_id, output_deleted=1)
        except Exception:
            pass
        print(f"[cleanup] removed output of job {job_id}: {n} files, "
              f"{b / 1048576:.1f} MB ({reason})", flush=True)
    return n, b


def _prune_video_outputs():
    """Short retention for video outputs: a finished video_transcode
    job's output files are deleted VIDEO_OUTPUT_RETENTION_DAYS after
    the job finishes. The job record stays in history; only the files
    go. Returns (jobs_removed, files_removed, bytes_freed)."""
    cutoff = time.time() - VIDEO_OUTPUT_RETENTION_DAYS * 86400
    jobs = files = freed = 0
    for j in jobstore.list_done_with_output(VIDEO_OUTPUT_RETENTION_TYPES):
        done_at = j.get("finished_at") or j.get("created_at") or 0
        if done_at and done_at < cutoff:
            n, b = _remove_job_output(
                j["id"], f"video output older than {VIDEO_OUTPUT_RETENTION_DAYS}d")
            if n:
                jobs += 1
                files += n
                freed += b
    return jobs, files, freed


def _disk_usage():
    """(data_dir_GB, disk_free_GB): size of the service's data dir and
    free space on its disk."""
    total = 0
    for dp, _, fs in os.walk(DATA):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    try:
        free = shutil.disk_usage(DATA).free
    except OSError:
        free = 0
    return total / 1073741824, free / 1073741824


def _enforce_disk_guard():
    """Disk guard: if data/ is over DATA_DIR_MAX_GB or the disk's free
    space is under DISK_FREE_MIN_GB, delete finished-job output files
    until both are back under the limits — video outputs first
    (oldest finished first), then other finished jobs' outputs
    (largest first). Job records are never deleted; queued/running
    jobs and failed jobs' inputs are never touched. Every deletion is
    logged with the job id and bytes freed. Returns a result dict."""
    data_gb, free_gb = _disk_usage()
    res = {"triggered": False, "video_removed": 0, "other_removed": 0,
           "guard_files": 0, "freed_bytes": 0,
           "data_gb": round(data_gb, 2), "disk_free_gb": round(free_gb, 2)}
    if data_gb <= DATA_DIR_MAX_GB and free_gb >= DISK_FREE_MIN_GB:
        return res
    res["triggered"] = True
    print(f"[guard] data/ {data_gb:.1f} GB (max {DATA_DIR_MAX_GB}), "
          f"disk free {free_gb:.1f} GB (min {DISK_FREE_MIN_GB}) — pruning outputs",
          flush=True)
    # Freeing X GB shrinks data/ by X and grows disk free by X (same
    # volume), so freeing max(over_data, over_free) fixes both.
    need_gb = max(0.0, data_gb - DATA_DIR_MAX_GB,
                  DISK_FREE_MIN_GB - free_gb)
    freed = 0
    # Stage 1: video outputs, oldest finished first.
    for j in jobstore.list_done_with_output(VIDEO_OUTPUT_RETENTION_TYPES):
        if freed / 1073741824 >= need_gb:
            break
        n, b = _remove_job_output(j["id"], "disk guard: video output")
        if n:
            res["video_removed"] += 1
            res["guard_files"] += n
            freed += b
    # Stage 2: other finished jobs' outputs, largest first.
    if freed / 1073741824 < need_gb:
        others = [j for j in jobstore.list_done_with_output()
                  if j["type"] not in VIDEO_OUTPUT_RETENTION_TYPES]
        others.sort(key=lambda j: -_output_dir_size(j["id"]))
        for j in others:
            if freed / 1073741824 >= need_gb:
                break
            n, b = _remove_job_output(j["id"], "disk guard: other finished output")
            if n:
                res["other_removed"] += 1
                res["guard_files"] += n
                freed += b
    res["freed_bytes"] = freed
    data_gb2, free_gb2 = _disk_usage()
    res["data_gb"] = round(data_gb2, 2)
    res["disk_free_gb"] = round(free_gb2, 2)
    print(f"[guard] freed {freed / 1048576:.1f} MB; data/ now {data_gb2:.1f} GB, "
          f"disk free {free_gb2:.1f} GB", flush=True)
    return res


def _run_prune(max_jobs=HISTORY_MAX_JOBS, max_age_days=HISTORY_MAX_AGE_DAYS):
    """One retention pass: delete terminal jobs beyond the limits, then
    their files, then orphaned files; then the short video-output
    retention; then the disk guard. Returns a result dict. Never
    deletes queued/running jobs (enforced in jobstore.prune_history),
    and never deletes job records except the ones beyond the history
    limits — the video sweep and the guard only remove output FILES."""
    deleted_ids = jobstore.prune_history(max_jobs, max_age_days)
    files, freed = 0, 0
    for job_id in deleted_ids:
        n, b = _delete_job_files(job_id)
        files += n
        freed += b
    on, ob, orphans = _cleanup_orphan_files()
    vjobs, vfiles, vfreed = _prune_video_outputs()
    guard = _enforce_disk_guard()
    return {
        "deleted_jobs": len(deleted_ids),
        "deleted_files": files + on + vfiles + guard["guard_files"],
        "freed_bytes": freed + ob + vfreed + guard["freed_bytes"],
        "orphan_jobs": orphans,
        "job_ids": deleted_ids,
        "video_outputs_removed": vjobs,
        "video_output_freed_bytes": vfreed,
        "guard_triggered": guard["triggered"],
        "guard_removed_jobs": guard["video_removed"] + guard["other_removed"],
        "guard_freed_bytes": guard["freed_bytes"],
        "data_gb": guard["data_gb"],
        "disk_free_gb": guard["disk_free_gb"],
    }


def _prune_loop():
    """Hourly retention pass (same rules as the on-demand prune)."""
    while True:
        time.sleep(HISTORY_PRUNE_INTERVAL_SECONDS)
        try:
            res = _run_prune()
            if res["deleted_jobs"] or res["deleted_files"]:
                print(f"[prune] deleted {res['deleted_jobs']} jobs, "
                      f"{res['deleted_files']} files, "
                      f"freed {res['freed_bytes'] / 1048576:.1f} MB", flush=True)
        except Exception as e:
            print(f"[prune] error: {e}", flush=True)


@app.post("/api/jobs")
async def create_job(request: Request, type: str = Form(...),
                     params: str = Form("{}"),
                     file: list[UploadFile] = File(default=[]),
                     access_code: str = Form(""),
                     token: str = Form("")):
    # A project token is a valid credential here: X-Access-Code,
    # Bearer, ?token= / ?access_code=, or a form field access_code /
    # token. Console password grants all types; a scope token only
    # its own project's types (wrong type -> 403).
    kind, scope = _resolve_credential(request, access_code, token)
    if kind is None:
        raise _unauth_error()
    if type not in JOB_TYPES:
        raise HTTPException(400, f"unknown type {type}, use one of {JOB_TYPES}")
    if kind == "scope" and type not in TOKEN_SCOPES[scope]["types"]:
        raise HTTPException(
            403,
            f"project token '{scope}' cannot submit type '{type}' "
            f"(allowed: {', '.join(TOKEN_SCOPES[scope]['types'])})")
    try:
        p = json.loads(params or "{}")
        if not isinstance(p, dict):
            raise ValueError
    except Exception:
        raise HTTPException(400, "params must be a JSON object")
    # The form field is always named "file", whether one file or many:
    # curl -F "file=@a" sends one part, the browser with multiple sends
    # several parts with the same name; FastAPI gives us a list either way.
    uploads = [uf for uf in (file or []) if uf.filename]
    # Most types need an input file; NO_INPUT_TYPES (e.g. vps_probe)
    # take everything from params and need no upload.
    if not uploads and type not in NO_INPUT_TYPES:
        raise HTTPException(400, "no file uploaded")
    if not uploads:
        display = str(p.get("target") or type)[:120]
    elif type == "asr_batch":
        display = f"{len(uploads)} files"
    else:
        if len(uploads) > 1:
            raise HTTPException(
                400, f"{type} takes one file; use asr_batch for multiple files")
        display = uploads[0].filename or "upload"
    job_id = jobstore.create_job(type, display, p)
    for i, uf in enumerate(uploads):
        dest = INPUT_DIR / f"{job_id}_{i:02d}_{Path(uf.filename or 'upload').name}"
        with open(dest, "wb") as out:
            shutil.copyfileobj(uf.file, out)
    return {"id": job_id, "type": type, "status": "queued",
            "files": len(uploads)}


@app.get("/api/jobs")
def list_jobs(request: Request, type: str = ""):
    kind, scope = _resolve_credential(request)
    if kind is None:
        raise _unauth_error()
    if kind == "scope":
        # A project token only ever sees its own project's jobs.
        return [_public_job(j) for j in jobstore.list_jobs_by_types(
            TOKEN_SCOPES[scope]["types"], limit=50)]
    if type:
        # Console: optionally filter to one project's types, e.g.
        # ?type=asr,asr_batch. Unknown types are ignored; if none
        # are valid the result is empty.
        types = tuple(t for t in (x.strip() for x in type.split(","))
                      if t in JOB_TYPES)
        if types:
            return jobstore.list_jobs_by_types(types, limit=50)
        return []
    return jobstore.list_jobs()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    kind, scope = _resolve_credential(request)
    if kind is None:
        raise _unauth_error()
    j = jobstore.get_job(job_id)
    if kind == "scope":
        if not j or j["type"] not in TOKEN_SCOPES[scope]["types"]:
            raise HTTPException(404, "job not found")
        return _public_job(j)
    if not j:
        raise HTTPException(404, "job not found")
    return j


@app.delete("/api/jobs/{job_id}")
def delete_job_endpoint(job_id: str, request: Request):
    """Delete one finished job: its DB row AND its input files and
    output directory, so disk space is actually freed. Queued/running
    jobs cannot be deleted (409); cancel them first."""
    _require_auth(request)
    try:
        j = jobstore.delete_job(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if not j:
        raise HTTPException(404, "job not found")
    files, freed = _delete_job_files(job_id)
    return {"id": job_id, "deleted_files": files, "freed_bytes": freed}


@app.post("/api/jobs/prune")
async def prune_jobs(request: Request):
    """Run one retention pass now (same rules as the hourly background
    prune). Optional JSON body overrides the limits for this one call,
    e.g. {"max_jobs": 10000, "max_age_days": 90}; no body uses the
    config defaults. Never deletes queued/running jobs. Also removes
    orphaned files whose job id has no DB row, applies the short
    video-output retention (finished transcode outputs older than
    VIDEO_OUTPUT_RETENTION_DAYS are removed, records kept), and runs
    the disk guard (video outputs first, then other finished outputs,
    only when data/ or disk free is over the limit)."""
    _require_auth(request)
    max_jobs, max_age_days = HISTORY_MAX_JOBS, HISTORY_MAX_AGE_DAYS
    try:
        body = await request.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        if "max_jobs" in body:
            max_jobs = body["max_jobs"]
        if "max_age_days" in body:
            max_age_days = body["max_age_days"]
    if (not isinstance(max_jobs, int) or isinstance(max_jobs, bool)
            or not isinstance(max_age_days, int) or isinstance(max_age_days, bool)
            or max_jobs < 0 or max_age_days < 0):
        raise HTTPException(400, "max_jobs and max_age_days must be integers >= 0")
    return _run_prune(max_jobs, max_age_days)


@app.post("/api/jobs/clear-all")
async def clear_all_jobs(request: Request):
    """Delete ALL terminal history at once (console-only). Same
    retention machinery as /api/jobs/prune, but with max_jobs=0 so
    every finished/failed/cancelled job is removed, together with its
    input and output files. Queued/running jobs are never touched
    (enforced in jobstore.prune_history); the response reports how
    many are still active. Also cleans orphaned files, applies the
    video-output retention and the disk guard, like prune."""
    _require_auth(request)
    res = _run_prune(0, None)
    res["active_jobs"] = jobstore.count_jobs()
    return res


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, request: Request):
    kind, scope = _resolve_credential(request)
    if kind is None:
        raise _unauth_error()
    j = jobstore.get_job(job_id)
    if kind == "scope" and (not j or j["type"] not in TOKEN_SCOPES[scope]["types"]):
        raise HTTPException(404, "result not ready")
    if not j or j["status"] != "done":
        raise HTTPException(404, "result not ready")
    if j.get("output_deleted"):
        raise HTTPException(410, "输出文件已被清理（保留期过后自动删除，或磁盘紧张时被清理），请重新提交任务")
    return _job_result_response(job_id)


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str, request: Request):
    _require_auth(request)
    j = jobstore.get_job(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    if j["status"] in ("queued", "running"):
        jobstore.update_job(job_id, status="cancelled", finished_at=time.time())
    return {"id": job_id, "status": jobstore.get_job(job_id)["status"]}


@app.post("/api/jobs/{job_id}/retry")
def retry(job_id: str, request: Request):
    _require_auth(request)
    j = jobstore.get_job(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    if j["status"] in ("failed", "dead", "cancelled"):
        jobstore.update_job(job_id, status="queued", retries=0, error="", progress=0, message="retried")
    return {"id": job_id, "status": jobstore.get_job(job_id)["status"]}


@app.get("/github", response_class=HTMLResponse)
def github_page(request: Request):
    if not auth_store.credentials_exist():
        return RedirectResponse("/", status_code=303)
    if not _is_authed(request):
        return LOGIN_HTML
    return GITHUB_HTML


@app.get("/api/github/config")
def github_config_get(request: Request):
    _require_auth(request)
    cfg = _load_github_config()
    if not cfg:
        return {"configured": False}
    return {"configured": True, "account": cfg.get("account", ""),
            "repo": cfg.get("repo", ""), "branch": cfg.get("branch", "main"),
            "visibility": cfg.get("visibility", "public"),
            "updated_at": cfg.get("updated_at", "")}


@app.post("/api/github/config")
async def github_config_post(request: Request):
    _require_auth(request)
    body = await request.json()
    account = (body.get("account") or "").strip()
    repo = (body.get("repo") or "").strip()
    token = (body.get("token") or "").strip()
    branch = (body.get("branch") or "main").strip() or "main"
    visibility = body.get("visibility") or "public"
    if visibility not in ("public", "private"):
        raise HTTPException(400, "visibility must be public or private")
    if not account or not repo:
        raise HTTPException(400, "account and repo are required")
    old = _load_github_config()
    if not token:
        # Empty token keeps the previously saved one.
        if old and old.get("token"):
            token = old["token"]
        else:
            raise HTTPException(400, "token is required")
    if not (token.startswith("ghp_") or token.startswith("github_pat_")) or len(token) < 20:
        raise HTTPException(400, "token should start with ghp_ or github_pat_")
    cfg = {"account": account, "repo": repo, "token": token,
           "branch": branch, "visibility": visibility,
           "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())}
    _save_github_config(cfg)
    return {"ok": True, "account": account, "repo": repo,
            "branch": branch, "visibility": visibility}


@app.post("/api/github/test")
def github_test(request: Request):
    """Check the saved token against api.github.com: which account it
    belongs to. Read-only; does not change anything."""
    _require_auth(request)
    cfg = _load_github_config()
    if not cfg or not cfg.get("token"):
        raise HTTPException(400, "no token saved yet")
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={"Authorization": "Bearer " + cfg["token"],
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "mac-compute"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise HTTPException(400, "GitHub returned %s (token 无效或没有权限)" % e.code)
    except Exception as e:
        raise HTTPException(400, "连接 GitHub 失败：%s" % e)
    login = data.get("login", "")
    out = {"ok": True, "login": login}
    if cfg.get("account") and login.lower() != cfg["account"].lower():
        out["warning"] = "注意：token 属于 %s，和填的账号 %s 不一致" % (login, cfg["account"])
    return out


def _run_one_job(job):
    """Run a single job in its own thread. One failure never blocks
    the queue: retry twice, then dead-letter."""
    job_id = job["id"]
    inputs = sorted(INPUT_DIR.glob(f"{job_id}_*"))
    if not inputs and job["type"] not in NO_INPUT_TYPES:
        jobstore.update_job(job_id, status="failed", error="input file missing", finished_at=time.time())
        return
    # Batch jobs get the whole input list; single-file jobs get one path;
    # no-input types get src=None (the worker reads params only).
    src = inputs if job["type"] == "asr_batch" else (inputs[0] if inputs else None)
    out_dir = OUTPUT_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    def cb(progress, message):
        cur = jobstore.get_job(job_id)
        if cur and cur["status"] == "cancelled":
            raise RuntimeError("cancelled by user")
        jobstore.update_job(job_id, progress=float(progress), message=message)

    try:
        result = run_job(job["type"], src, out_dir, job["params"], cb)
        jobstore.update_job(job_id, status="done", progress=1.0,
                            message="done", result=result, finished_at=time.time())
        if job["type"] in DELETE_INPUT_AFTER_DONE_TYPES:
            # Video compression: the uploaded video is only a working
            # copy (the user has the original); delete it promptly so
            # finished transcodes stop piling up on disk. The compressed
            # output stays until history pruning removes the job. This
            # must never fail the job, so every error is swallowed.
            try:
                for p in inputs:
                    try:
                        p.unlink()
                    except OSError:
                        pass
            except Exception:
                pass
        # Aggressive disk guard: a burst of big compressions must not
        # fill the disk before the hourly prune runs. Cheap check
        # (walks data/ once); only deletes when over the limits, and
        # never fails the job.
        try:
            _enforce_disk_guard()
        except Exception:
            pass
    except Exception as e:
        if str(e) == "cancelled by user":
            jobstore.update_job(job_id, status="cancelled", finished_at=time.time())
            return
        retries = job["retries"] + 1
        if retries <= MAX_RETRIES:
            jobstore.update_job(job_id, status="queued", retries=retries,
                                error=str(e)[:500], message=f"retry {retries}/{MAX_RETRIES}")
        else:
            jobstore.update_job(job_id, status="dead", retries=retries,
                                error=str(e)[:1000], finished_at=time.time())


def _worker_loop():
    """Dispatcher: pick queued jobs and run each in its own thread.
    Concurrency is gated by next_queued() on the DB status: heavy
    serial (except ASR, up to ASR_MAX_CONCURRENT), light up to 4."""
    while True:
        job = jobstore.next_queued(HEAVY_TYPES)
        if not job:
            time.sleep(1)
            continue
        job_id = job["id"]
        jobstore.update_job(job_id, status="running", started_at=time.time(), message="running")
        threading.Thread(target=_run_one_job, args=(job,), daemon=True).start()


if __name__ == "__main__":
    # Jobs left 'running' by a previous process would block the heavy
    # slot forever; re-queue them (or dead-letter if retries exhausted).
    jobstore.reset_stale_running()
    # One retention pass at startup (also removes orphaned files left
    # behind by earlier DB states), then hourly in the background.
    try:
        res = _run_prune()
        print(f"[prune] startup: deleted {res['deleted_jobs']} jobs, "
              f"{res['deleted_files']} files, "
              f"freed {res['freed_bytes'] / 1048576:.1f} MB", flush=True)
    except Exception as e:
        print(f"[prune] startup error: {e}", flush=True)
    threading.Thread(target=_worker_loop, daemon=True).start()
    threading.Thread(target=_prune_loop, daemon=True).start()
    threading.Thread(target=sysstats.collector_loop, daemon=True).start()
    threading.Thread(target=sysstats.fast_sample_loop, daemon=True).start()
    # Prefer the cp.zin7.de cert; fall back to apple.su7.dpdns.org, then Tailscale.
    cert_file, key_file = None, None
    for cf, kf in ((CP_CERT_FILE, CP_KEY_FILE), (APPLE_CERT_FILE, APPLE_KEY_FILE), (CERT_FILE, KEY_FILE)):
        if cf.exists() and kf.exists():
            cert_file, key_file = cf, kf
            break
    if cert_file:
        print(f"HTTPS enabled, cert={cert_file}", flush=True)
        ssl_kwargs = {"ssl_certfile": str(cert_file), "ssl_keyfile": str(key_file)}
    else:
        print("WARNING: no TLS cert found, serving plain HTTP", flush=True)
        ssl_kwargs = {}
    # macOS: binding "::" listens on IPv6 only (no IPv4-mapped sockets),
    # so run two uvicorn servers in ONE process (one IPv4, one IPv6).
    # They share the same app and the same worker thread, so jobs are
    # never processed twice.
    import asyncio

    async def _serve():
        s4 = uvicorn.Server(uvicorn.Config(
            app, host="0.0.0.0", port=PORT, log_level="info", **ssl_kwargs))
        s6 = uvicorn.Server(uvicorn.Config(
            app, host="::", port=PORT, log_level="info", **ssl_kwargs))
        await asyncio.gather(s4.serve(), s6.serve())

    asyncio.run(_serve())
