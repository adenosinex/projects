#!/usr/bin/env python3
"""Upgrade mac-compute auth: 4-digit access code -> username + password.

Run ON the Mac, in ~/mac-compute/:
    python3 patch_auth.py

What it does (all-or-nothing; aborts before writing if any REQUIRED
pattern is missing, so the live files are never left half-patched):
  - Backs up app.py / config.py to *.bak-<timestamp>.
  - config.py: removes ACCESS_CODE / ACCESS_CODE_HASH and the old-code
    comments; imports auth_store (credentials in auth.json, mode 600).
  - app.py: replaces the access-code check with auth_store
    (username+password). Web: setup form on first visit, login form
    after that, session cookie = random token (HttpOnly, Secure,
    SameSite=Lax, 30 days). API: HTTP Basic Auth OR X-Access-Code
    header carrying the new password. /health stays public.
  - config.example.py / README.md: same wording updates (warn-only).
  - Verifies: py_compile passes; no ACCESS_CODE / old-code reference
    remains in app.py or config.py; auth_store.py exists;
    auth.json is NOT created by this script.
  - Never touches jobs.db, data/, certs/, or any other endpoint.

Idempotent: if the files are already patched, it reports that and
exits 0 (after re-running the checks).
"""
import datetime
import os
import py_compile
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(BASE, "app.py")
CFG = os.path.join(BASE, "config.py")
CFG_EX = os.path.join(BASE, "config.example.py")
README = os.path.join(BASE, "README.md")
AUTH_STORE = os.path.join(BASE, "auth_store.py")

FAILURES = []
WARNINGS = []


def fail(msg):
    FAILURES.append(msg)
    print("FAIL: " + msg, file=sys.stderr)


def warn(msg):
    WARNINGS.append(msg)
    print("WARN: " + msg)


def replace_once(text, old, new, name):
    """Exact replacement, must occur exactly once. Returns new text or
    None (records failure)."""
    n = text.count(old)
    if n == 0:
        fail(f"{name}: pattern not found")
        return None
    if n > 1:
        fail(f"{name}: pattern found {n} times, expected 1")
        return None
    return text.replace(old, new, 1)


def replace_regex(text, pattern, new, name, required=True):
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        if required:
            fail(f"{name}: pattern not found")
            return None
        warn(f"{name}: pattern not found, skipped")
        return text
    return text[:m.start()] + new + text[m.end():]


# ---------------------------------------------------------------- app.py
NEW_AUTH_BLOCK = '''# --- Auth: username + password (auth_store.py) ---
# Credentials live in auth.json (salted PBKDF2 hash, mode 600), shared
# with the router status dashboard. The user creates them on first
# visit; there is no access code any more. Browser login issues a
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


def _require_auth(request: Request):
    if not _is_authed(request):
        if not auth_store.credentials_exist():
            raise HTTPException(
                401,
                "not set up yet: open the homepage in a browser to "
                "create a username and password")
        raise HTTPException(
            401,
            "login required: HTTP Basic Auth (username:password) or "
            "header X-Access-Code with the password")'''

SETUP_HTML = '''SETUP_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
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
</body></html>"""'''

NEW_DOCSTRING = '''Access is protected by a username + password (see auth_store.py:
salted PBKDF2 hash in auth.json, mode 600, shared with the router
status dashboard). First visit shows a setup form; after that,
browser users log in once on the homepage (session cookie) and
API clients send HTTP Basic Auth or header X-Access-Code carrying
the password.'''

NEW_CONSOLE = '''@app.get("/", response_class=HTMLResponse)
def console(request: Request):
    if not auth_store.credentials_exist():
        return SETUP_HTML
    if not _is_authed(request):
        return LOGIN_HTML
    return CONSOLE_HTML'''

NEW_LOGIN = '''@app.post("/setup")
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
    return HTMLResponse(LOGIN_HTML.replace("<!--msg-->", "用户名或密码不对，请重试。"), status_code=401)'''

NEW_LOGOUT = '''@app.get("/logout")
def logout(request: Request):
    _SESSIONS.pop(request.cookies.get(AUTH_COOKIE, ""), None)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(AUTH_COOKIE, path="/")
    return resp'''

NEW_GITHUB_PAGE = '''@app.get("/github", response_class=HTMLResponse)
def github_page(request: Request):
    if not auth_store.credentials_exist():
        return RedirectResponse("/", status_code=303)
    if not _is_authed(request):
        return LOGIN_HTML
    return GITHUB_HTML'''

NEW_CURL = '''<pre>curl -u 用户名:密码 -F "file=@视频.mp4" -F "type=video_transcode" https://cp.zin7.de:5850/api/jobs
curl -H "X-Access-Code: 密码" -F "file=@截图.zip" -F "type=image_select" -F 'params={"top_n":10}' https://cp.zin7.de:5850/api/jobs
curl -u 用户名:密码 -F "file=@a.m4a" -F "file=@b.m4a" -F "file=@c.m4a" -F "type=asr_batch" https://cp.zin7.de:5850/api/jobs
curl -u 用户名:密码 https://cp.zin7.de:5850/api/jobs/&lt;任务ID&gt;/download -o 结果.zip</pre>
<p>登录方式：浏览器在首页输用户名和密码；命令行用 <code>-u 用户名:密码</code>（HTTP Basic Auth），或者继续用 <code>-H "X-Access-Code: 密码"</code>（header 里放的是新密码，不是旧访问码）。第一次使用先在浏览器里打开首页创建用户名和密码。</p>'''

NEW_ADDR = '''<p>访问地址：<code>https://cp.zin7.de:5850</code>（IPv6），Tailscale 内也可以用 <code>https://zindemac-mini.tail35e420.ts.net:5850</code>（打开会报证书名字不符，因为证书是给 cp.zin7.de 发的）。</p>'''


def patch_app(text):
    # Idempotency: already patched?
    if "auth_store" in text and "ACCESS_CODE_HASH" not in text \
            and "_code_ok" not in text:
        print("app.py already looks patched; verifying only.")
        return text, True

    # 1. Module docstring (exact, then regex fallback).
    old_doc = """Access is protected by an access code (see config.py: only its
SHA-256 is stored). Browser users log in once on the homepage;
API clients send header X-Access-Code with each request."""
    if old_doc in text:
        text = text.replace(old_doc, NEW_DOCSTRING, 1)
    else:
        text = replace_regex(
            text, r"Access is protected by.*?request\.",
            NEW_DOCSTRING, "app.py docstring")
        if text is None:
            return None, False

    # 2. Imports: base64 + secrets; auth_store; drop ACCESS_CODE names.
    if "import base64" not in text:
        if "import hashlib\n" in text:
            text = text.replace("import hashlib\n",
                                "import base64\nimport hashlib\n", 1)
        elif "import json\n" in text:
            text = text.replace("import json\n",
                                "import base64\nimport json\n", 1)
        else:
            fail("app.py imports: no anchor for base64")
            return None, False
    if "import secrets" not in text:
        if "import hmac\n" in text:
            text = text.replace("import hmac\n",
                                "import hmac\nimport secrets\n", 1)
        elif "import json\n" in text:
            text = text.replace("import json\n",
                                "import json\nimport secrets\n", 1)
        else:
            fail("app.py imports: no anchor for secrets")
            return None, False
    if "import auth_store" not in text:
        for anchor in ("import stats as sysstats\n",
                       "import jobs as jobstore\n",
                       "from dispatch import"):
            if anchor in text:
                text = text.replace(anchor, anchor + "import auth_store\n", 1)
                break
        else:
            fail("app.py imports: no anchor for auth_store")
            return None, False
    # Drop the old names from the `from config import (...)` block.
    before = text
    text = re.sub(r"\bACCESS_CODE_HASH,\s*", "", text)
    text = re.sub(r"\bACCESS_CODE,\s*", "", text)
    if text == before:
        fail("app.py imports: ACCESS_CODE names not found in config import")
        return None, False

    # 3. Auth functions: _auth_token .. _require_auth -> new block.
    text = replace_regex(
        text, r"def _auth_token\(code\):.*?raise HTTPException\(401, \"access code required: header X-Access-Code\"\)",
        NEW_AUTH_BLOCK, "app.py auth functions")
    if text is None:
        return None, False

    # 4. LOGIN_HTML -> SETUP_HTML + LOGIN_HTML.
    text = replace_regex(
        text, r"LOGIN_HTML = \"\"\".*?\"\"\"",
        SETUP_HTML, "app.py LOGIN_HTML")
    if text is None:
        return None, False

    # 5. Console gating.
    old_console = '''@app.get("/", response_class=HTMLResponse)
def console(request: Request):
    if not _is_authed(request):
        return LOGIN_HTML
    return CONSOLE_HTML'''
    text = replace_once(text, old_console, NEW_CONSOLE, "app.py console()")
    if text is None:
        return None, False

    # 6. /login -> /setup + /login.
    old_login = '''@app.post("/login")
def login(code: str = Form(...)):
    if not _code_ok(code):
        return HTMLResponse(LOGIN_HTML.replace(
            "访问码不对会回到这里。",
            "访问码不对，请重试。"), status_code=401)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(AUTH_COOKIE, ACCESS_CODE_HASH, httponly=True,
                    samesite="lax", path="/", max_age=30 * 24 * 3600)
    return resp'''
    if old_login in text:
        text = text.replace(old_login, NEW_LOGIN, 1)
    else:
        text = replace_regex(
            text, r"@app\.post\(\"/login\"\).*?return resp\n",
            NEW_LOGIN + "\n", "app.py /login")
        if text is None:
            return None, False

    # 7. /logout: drop the session token too.
    old_logout = '''@app.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(AUTH_COOKIE, path="/")
    return resp'''
    if old_logout in text:
        text = text.replace(old_logout, NEW_LOGOUT, 1)
    else:
        text = replace_regex(
            text, r"@app\.get\(\"/logout\"\).*?return resp\n",
            NEW_LOGOUT + "\n", "app.py /logout", required=False)

    # 8. /github gating.
    old_gh = '''@app.get("/github", response_class=HTMLResponse)
def github_page(request: Request):
    if not _is_authed(request):
        return LOGIN_HTML
    return GITHUB_HTML'''
    if old_gh in text:
        text = text.replace(old_gh, NEW_GITHUB_PAGE, 1)
    else:
        warn("app.py /github gating pattern not found, skipped")

    # 9. Homepage curl examples (warn-only) + address line (warn-only).
    new_text = replace_regex(
        text, r"<pre>curl -H \"X-Access-Code:.*?</pre>",
        NEW_CURL, "app.py curl block", required=False)
    if new_text is not None:
        text = new_text
    new_text = replace_regex(
        text, r"<pre>curl.*?</pre>",
        NEW_CURL, "app.py curl block (fallback)", required=False)
    if new_text is not None and new_text != text:
        text = new_text
    new_text = replace_regex(
        text, r"<p>访问地址：.*?</p>",
        NEW_ADDR, "app.py address line", required=False)
    if new_text is not None:
        text = new_text

    # 10. Residual old-code wording in user-facing strings.
    text = text.replace("访问码", "密码")
    text = re.sub(r"(?<!X-)access code", "password", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!X-)Access code", "Password", text)
    return text, False


# ---------------------------------------------------------------- config.py
def patch_config(text):
    if "ACCESS_CODE_HASH" not in text and "ACCESS_CODE" not in text \
            and "auth_store" in text:
        print("config.py already looks patched; verifying only.")
        return text, True
    # Remove the old-code comment lines.
    lines = text.split("\n")
    kept = []
    for ln in lines:
        if re.match(
                r"^#.*(Access code|ACCESS_CODE|MAC_COMPUTE_CODE|"
                r"sha256\(b'mac-compute|To change the code|"
                r"never the code itself|Env MAC_COMPUTE_CODE)", ln):
            continue
        if re.match(r"^ACCESS_CODE(_HASH)?\s*=", ln):
            continue
        kept.append(ln)
    text = "\n".join(kept)
    # Collapse 3+ blank lines left behind.
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Insert the new note + auth_store import.
    note = ('# Auth: username + password, stored as a salted PBKDF2 hash in\n'
            '# auth.json (mode 600) by auth_store.py. The same file is shared\n'
            '# with the router status dashboard. First visit shows a setup\n'
            '# form; there is no password in this file and no access code.\n')
    if 'AUTH_COOKIE = "mc_auth"' in text:
        text = text.replace('AUTH_COOKIE = "mc_auth"',
                            note + 'AUTH_COOKIE = "mc_auth"', 1)
    else:
        fail("config.py: AUTH_COOKIE line not found")
        return None, False
    if "import auth_store" not in text:
        if "import os\nfrom pathlib import Path" in text:
            text = text.replace(
                "import os\nfrom pathlib import Path",
                "import os\nfrom pathlib import Path\n\n"
                "import auth_store  # noqa: F401  (username+password auth; "
                "credentials in auth.json)", 1)
        elif "import os\n" in text:
            text = text.replace(
                "import os\n",
                "import os\nimport auth_store  # noqa: F401  "
                "(username+password auth; credentials in auth.json)\n", 1)
        else:
            fail("config.py: no anchor for auth_store import")
            return None, False
    return text, False


def patch_config_example(text):
    lines = text.split("\n")
    kept = []
    for ln in lines:
        if re.match(
                r"^#.*(Access code|ACCESS_CODE|MAC_COMPUTE_CODE|"
                r"sha256\(b'mac-compute|To change the code|"
                r"never the code itself|Env MAC_COMPUTE_CODE)", ln):
            continue
        if re.match(r"^ACCESS_CODE(_HASH)?\s*=", ln):
            continue
        kept.append(ln)
    text = "\n".join(kept)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if 'AUTH_COOKIE = "mc_auth"' in text:
        text = text.replace(
            'AUTH_COOKIE = "mc_auth"',
            '# Auth: username + password via auth_store.py (auth.json).\n'
            'AUTH_COOKIE = "mc_auth"', 1)
    return text


def patch_readme(text):
    orig = text
    text = text.replace(
        "## Access code\n\n"
        "All pages and API calls need the access code (set 2026-11-07).\n"
        "Browser: open the homepage, enter the code once, it stays logged\n"
        "in for 30 days (HttpOnly cookie). API: send header\n"
        "`X-Access-Code: <code>` with every request. The code itself is\n"
        "never stored in plain text in the project — only its SHA-256\n"
        "(`config.py: ACCESS_CODE_HASH`); to change it, compute a new hash\n"
        "with `python3 -c \"import hashlib; print(hashlib.sha256(b'mac-compute:NEWCODE').hexdigest())\"`,\n"
        "replace the hash, restart. `/health` is public (no code needed).",
        "## Login (username + password)\n\n"
        "All pages and API calls need the username + password.\n"
        "First visit to the homepage shows a setup form (username +\n"
        "password, at least 8 characters, 12+ suggested); the same login\n"
        "also works for the router status dashboard. Browser: log in once,\n"
        "it stays logged in for 30 days (HttpOnly, Secure session cookie\n"
        "holding a random token, never the password). API: send HTTP Basic\n"
        "Auth (`-u username:password`) or header `X-Access-Code: <password>`\n"
        "with every request. The password is never stored in plain text —\n"
        "only its salted PBKDF2 hash in `auth.json` (mode 600, written by\n"
        "`auth_store.py`). `/health` is public (no login needed).")
    text = text.replace("同样需要访问码", "同样需要登录")
    text = text.replace(
        "curl -H \"X-Access-Code: <code>\" -F \"file=@a.m4a\" -F \"type=asr\" https://apple.su7.dpdns.org:5850/api/jobs",
        "curl -u username:password -F \"file=@a.m4a\" -F \"type=asr\" https://cp.zin7.de:5850/api/jobs")
    text = text.replace(
        "curl -H \"X-Access-Code: <code>\" -F \"file=@v.mp4\" -F \"type=video_transcode\" https://apple.su7.dpdns.org:5850/api/jobs",
        "curl -u username:password -F \"file=@v.mp4\" -F \"type=video_transcode\" https://cp.zin7.de:5850/api/jobs")
    text = text.replace(
        "curl -H \"X-Access-Code: <code>\" -F \"file=@shots.zip\" -F \"type=image_select\" -F 'params={\"top_n\":10}' https://apple.su7.dpdns.org:5850/api/jobs",
        "curl -u username:password -F \"file=@shots.zip\" -F \"type=image_select\" -F 'params={\"top_n\":10}' https://cp.zin7.de:5850/api/jobs")
    text = text.replace("API (same access code):", "API (same login):")
    text = text.replace(
        "No login is configured on mac-compute yet; the apple domain is\n"
        "reachable from the whole IPv6 internet, so add an access code\n"
        "before sharing the URL widely.",
        "Login (username + password, set on first visit) is required; do\n"
        "not share the URL widely before it is set up.")
    if text == orig:
        warn("README.md: no known patterns matched, skipped")
    return text


def main():
    if not os.path.exists(AUTH_STORE):
        fail(f"auth_store.py missing in {BASE} — copy it there first")
        print("ABORTED: nothing written.", file=sys.stderr)
        return 1
    app_text = open(APP, encoding="utf-8").read()
    cfg_text = open(CFG, encoding="utf-8").read()

    new_app, app_done = patch_app(app_text)
    new_cfg, cfg_done = patch_config(cfg_text)
    if new_app is None or new_cfg is None or FAILURES:
        print("ABORTED: nothing written.", file=sys.stderr)
        return 1

    # Final checks on the patched text BEFORE writing anything.
    for name, t in (("app.py", new_app), ("config.py", new_cfg)):
        if "ACCESS_CODE" in t:
            fail(f"{name}: ACCESS_CODE still present after patch")
        if "_code_ok" in t or "_auth_token" in t:
            fail(f"{name}: old auth helper still present")
    if "访问码" in new_app or "访问码" in new_cfg:
        fail("old-code wording (访问码) still present")
    if "auth_store" not in new_app or "auth_store" not in new_cfg:
        fail("auth_store reference missing after patch")
    if FAILURES:
        print("ABORTED: nothing written.", file=sys.stderr)
        return 1

    # Optional files (warn-only).
    new_ex, new_readme = None, None
    if os.path.exists(CFG_EX):
        new_ex = patch_config_example(open(CFG_EX, encoding="utf-8").read())
        if "ACCESS_CODE" in new_ex:
            warn("config.example.py: ACCESS_CODE still present, leaving as-is")
            new_ex = None
    if os.path.exists(README):
        new_readme = patch_readme(open(README, encoding="utf-8").read())

    # Compile-check the new sources before touching disk.
    for label, src in (("app.py", new_app), ("config.py", new_cfg)):
        try:
            compile(src, label, "exec")
        except SyntaxError as e:
            fail(f"{label}: syntax error after patch: {e}")
    if FAILURES:
        print("ABORTED: nothing written.", file=sys.stderr)
        return 1

    # Write with backups.
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    for path, new_text in ((APP, new_app), (CFG, new_cfg)):
        bak = f"{path}.bak-{ts}"
        with open(path, encoding="utf-8") as f:
            old = f.read()
        with open(bak, "w", encoding="utf-8") as f:
            f.write(old)
        if old != new_text:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_text)
            print(f"patched {os.path.basename(path)} (backup {os.path.basename(bak)})")
        else:
            print(f"{os.path.basename(path)} already patched, unchanged")
    if new_ex is not None:
        with open(CFG_EX, "w", encoding="utf-8") as f:
            f.write(new_ex)
        print("patched config.example.py")
    if new_readme is not None:
        with open(README, "w", encoding="utf-8") as f:
            f.write(new_readme)
        print("patched README.md")

    for path in (APP, CFG):
        py_compile.compile(path, doraise=True)
    print("py_compile OK for app.py and config.py")
    if os.path.exists(os.path.join(BASE, "auth.json")):
        warn("auth.json already exists — setup form will NOT show; "
             "the user must log in with the existing credentials")
    else:
        print("auth.json does not exist (good): first visit will show setup")
    if WARNINGS:
        print(f"{len(WARNINGS)} warning(s), see above.")
    print("PATCH OK — now restart: bash restart.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
