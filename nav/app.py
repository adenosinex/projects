"""Nav page for cpl.zin7.de — lists the user's services, forwards /proxy-test/,
login = same username/password as cp.zin7.de (mac-compute auth.json, PBKDF2)."""
import hashlib
import hmac
import os
import secrets
import sys
import time

sys.path.insert(0, "/Users/zin/mac-compute")
import auth_store  # noqa: E402 — identical PBKDF2 verification as mac-compute

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

SECRET_FILE = "/Users/zin/nav/session_secret"
COOKIE_NAME = "nav_session"
SESSION_DAYS = 30

app = FastAPI()


def _secret():
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE) as f:
            return f.read().strip()
    s = secrets.token_hex(32)
    with open(SECRET_FILE, "w") as f:
        f.write(s)
    os.chmod(SECRET_FILE, 0o600)
    return s


SECRET = _secret()


def make_session(username):
    exp = int(time.time()) + SESSION_DAYS * 86400
    payload = "%s.%d" % (username, exp)
    sig = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return "%s.%s" % (payload, sig)


def check_session(cookie):
    try:
        username, exp, sig = cookie.rsplit(".", 2)
        payload = "%s.%s" % (username, exp)
        expected = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if int(exp) < time.time():
            return None
        return username
    except Exception:
        return None


def current_user(request):
    return check_session(request.cookies.get(COOKIE_NAME, "") or "")


SERVICES = [
    ("mac-compute", "/service/mac-compute", "Mac 上的计算服务：ASR、视频转码、OCR、VPS 探测；详情页有各项目的订阅链接"),
    ("Home Assistant", "https://ha.zin7.de/", "智能家居（华为云，Mac 转发）"),
    ("Status 状态面板", "https://status.mac.zin7.de/", "路由器和设备状态"),
    ("proxy-test 代理测试", "/proxy-test/", "订阅节点测试：直连 / 中继、速度对比"),
    ("sub-convert 订阅转换", "/convert/", "订阅转换：每小时自动测速，延迟最低的 3 个节点进「精选」"),
    ("dy 115 视频筛选", "/dy/", "115 云端视频预筛：在线播放、打标、结算归档"),
    ("DJI 视频压缩", "/dji/", "DJI 视频压缩：HEVC 硬件加速，上传或指定 Mac 上的路径"),
    ("路况监测", "/traffic/", "指定路段/路线拥堵监测：实时状态、今日曲线、历史与热力图"),
    ("知乎提取", "/zhihu/", "知乎文章/回答提取 + YouTube/Bilibili 视频转文字"),
    ("zipview 压缩包图片浏览", "/zipview/", "压缩包里的图片浏览：按文件夹分组，按需取图，不解压"),
    ("抖音视频流", "/douyin/", "上下滑动的短视频流：电脑上的视频，浏览器直接播"),
]

# mac-compute per-project token links (same scopes as mac-compute's
# TOKEN_SCOPES). The nav detail page reads these token files at render
# time, so a regeneration updates the displayed link automatically.
MC_TOKEN_SCOPES = [
    ("asr", "语音转文字 (ASR)", "音频 / 视频转文字"),
    ("video_transcode", "视频转码", "视频转码 / 压缩"),
    ("ocr", "OCR 图片转文字", "图片 / PDF 转文字"),
]


def _mc_token(scope):
    """Current mac-compute token for one project (read at render time)."""
    try:
        with open("/Users/zin/mac-compute/token_%s" % scope) as f:
            tok = f.read().strip()
        if len(tok) == 32 and all(c in "0123456789abcdef" for c in tok):
            return tok
    except Exception:
        pass
    return None


SERVICE_DETAIL_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__NAME__ · 导航</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#222;background:#fafafa}
h1{font-size:22px}h2{font-size:17px}a{color:#0b5fff;text-decoration:none}
.card{background:#fff;border:1px solid #e3e3e3;border-radius:10px;margin:12px 0;padding:14px 16px}
p{margin:6px 0;color:#666;font-size:14px}code{background:#f0f0f0;padding:2px 6px;font-size:13px;word-break:break-all}
button{margin-left:8px;padding:4px 10px;border:1px solid #ccc;border-radius:6px;background:#fff;font-size:13px}
.top{text-align:right;font-size:13px}.top a{color:#888}</style></head><body>
<div class="top"><a href="/">← 导航</a> · <a href="/logout">退出登录</a></div>
<h1>__NAME__</h1>
<p>__DESC__</p>
<div class="card"><a href="__URL__">打开控制台 →</a></div>
__BODY__
<script>
async function copyLink(url,btn){try{await navigator.clipboard.writeText(url);btn.textContent='已复制';setTimeout(()=>btn.textContent='复制',1500);}catch(e){prompt('复制这个链接：',url);}}
</script></body></html>"""

LANDING_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>导航 · zin7.de</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#222;background:#fafafa}
h1{font-size:22px}ul{list-style:none;padding:0}li{background:#fff;border:1px solid #e3e3e3;border-radius:10px;margin:12px 0;padding:14px 16px}
a{color:#0b5fff;text-decoration:none;font-size:17px;font-weight:600}
p{margin:6px 0 0;color:#666;font-size:14px}.top{text-align:right;font-size:13px}
.top a{color:#888;font-weight:400}</style></head><body>
<div class="top"><a href="/logout">退出登录</a></div>
<h1>导航</h1><ul>
""" + "".join(
    '<li><a href="%s">%s</a><p>%s</p></li>' % (url, name, desc)
    for name, url, desc in SERVICES
) + """</ul></body></html>"""

LOGIN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · zin7.de</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;max-width:360px;margin:80px auto;padding:0 16px;color:#222;background:#fafafa}
h1{font-size:20px}form{background:#fff;border:1px solid #e3e3e3;border-radius:10px;padding:20px}
label{display:block;font-size:14px;margin:10px 0 4px}
input{width:100%;box-sizing:border-box;padding:9px;border:1px solid #ccc;border-radius:6px;font-size:15px}
button{margin-top:16px;width:100%;padding:10px;border:0;border-radius:6px;background:#0b5fff;color:#fff;font-size:15px}
.err{color:#c00;font-size:14px}</style></head><body>
<h1>登录</h1>
<p style="color:#666;font-size:14px">和 cp.zin7.de 同一个账号密码。</p>
<form method="post" action="/login">
<label>用户名</label><input name="username" autocomplete="username">
<label>密码</label><input name="password" type="password" autocomplete="current-password">
{err}
<button type="submit">登录</button>
</form></body></html>"""


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/auth-check")
def auth_check(request: Request):
    if current_user(request):
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False}, status_code=401)



def _ping_token():
    """Current ping-test token (read at render time, so a regeneration
    updates the nav link automatically)."""
    try:
        with open("/Users/zin/ping-test/token") as f:
            tok = f.read().strip()
        if len(tok) == 32 and all(c in "0123456789abcdef" for c in tok):
            return tok
    except Exception:
        pass
    return None


def _dji_token():
    """Current dji-compress token (read at render time, so a
    regeneration updates the nav link automatically)."""
    try:
        with open("/Users/zin/dji-compress/token") as f:
            tok = f.read().strip()
        if len(tok) == 32 and all(c in "0123456789abcdef" for c in tok):
            return tok
    except Exception:
        pass
    return None


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", status_code=302)
    html = LANDING_HTML
    tok = _ping_token()
    if tok:
        li = ('<li><a href="https://cpl.zin7.de/ping/%s/">ping-test IP 延迟测试</a>'
              '<p>测 IP / 域名延迟：ping + TCP，10 线程并行；公开 token 链接，打开就能测</p></li>') % tok
        html = html.replace("</ul>", li + "</ul>")
    return html


@app.get("/service/{name}", response_class=HTMLResponse)
def service_detail(name: str, request: Request):
    """Minimal per-service detail view (login-gated). The nav previously
    had no detail page — just the landing list. mac-compute's detail
    page lists its per-project token links (read from the token files
    at render time) so the user can copy a link and hand it to other
    callers; other services show their info and a note that they have
    no token link yet."""
    if not current_user(request):
        return RedirectResponse("/login", status_code=302)
    svc = None
    for n, url, desc in SERVICES:
        if n == name:
            svc = (n, url, desc)
            break
    if not svc:
        return HTMLResponse("没有这个服务。", status_code=404)
    n, url, desc = svc
    console = "https://cp.zin7.de/" if n == "mac-compute" else url
    if n == "mac-compute":
        cards = []
        for scope, label, sdesc in MC_TOKEN_SCOPES:
            tok = _mc_token(scope)
            if tok:
                link = "https://cp.zin7.de/t/%s/%s/" % (scope, tok)
                cards.append(
                    '<div class="card"><b>%s</b><p>%s</p>'
                    '<code>%s</code>'
                    '<button onclick="copyLink(\'%s\',this)">复制</button>'
                    '<p>只读：看这个项目的任务、进度和调用说明，不用登录；不能提交、不能下载。</p></div>'
                    % (label, sdesc, link, link))
            else:
                cards.append(
                    '<div class="card"><b>%s</b><p>%s</p>'
                    '<p>还没有生成这个项目的订阅链接：先打开控制台一次（会自动生成）。</p></div>'
                    % (label, sdesc))
        body = ('<h2>订阅链接（token 链接）</h2>'
                '<p>把链接发给别人，对方打开就能看这个项目的任务和进度，不用登录。链接里的 token 就是凭证，别公开贴出去；想换 token，去控制台的“订阅链接”里点“重新生成”。</p>'
                + "".join(cards))
    elif n == "DJI 视频压缩":
        tok = _dji_token()
        if tok:
            link = "https://cpl.zin7.de/djit/%s/" % tok
            body = ('<h2>订阅链接（token 链接）</h2>'
                    '<p>把链接发给别人，对方打开就能用这个压缩工具，不用登录。链接里的 token 就是凭证，别公开贴出去；想换 token，删掉 Mac 上 /Users/zin/dji-compress/token 后重启服务，会自动生成新的。</p>'
                    '<div class="card"><b>DJI 视频压缩</b><p>上传视频或指定 Mac 上的路径，HEVC 硬件加速压缩</p>'
                    '<code>%s</code>'
                    '<button onclick="copyLink(\'%s\',this)">复制</button></div>'
                    % (link, link))
        else:
            body = '<p>还没有生成这个服务的订阅链接：先打开一次 /dji/（会自动生成）。</p>'
    else:
        body = '<p>这个服务还没有订阅链接。</p>'
    html = (SERVICE_DETAIL_HTML.replace("__NAME__", n)
            .replace("__DESC__", desc).replace("__URL__", console)
            .replace("__BODY__", body))
    return html


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = ""):
    err = '<p class="err">用户名或密码不对，请重试。</p>' if error else ""
    return LOGIN_HTML.replace("{err}", err)


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    if auth_store.verify_credentials(username, password):
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie(
            COOKIE_NAME, make_session(username.strip()),
            max_age=SESSION_DAYS * 86400, httponly=True, secure=True,
            samesite="lax", path="/",
        )
        return resp
    return RedirectResponse("/login?error=1", status_code=302)


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp
