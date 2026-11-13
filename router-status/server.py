#!/opt/homebrew/bin/python3
"""Router status dashboard web server (stdlib only, no extra deps).

HTTPS on port 18933 (or $ROUTER_STATUS_PORT / argv[1]).
Certs: ~/mac-compute/certs/status.mac.zin7.de.fullchain.crt + .key.
  If the real cert is not present yet, falls back to a self-signed cert at
  ~/router-status/selfsigned.crt/.key if present, else plain HTTP with a
  loud warning (initial testing only -- final state must be the real cert).
Auth: shared ~/mac-compute/auth_store.py (single username + PBKDF2 hash in
  ~/mac-compute/auth.json, mode 600). The user sets credentials on first
  visit via /setup; this server never creates auth.json itself except
  through auth_store.create_credentials() on POST /setup.
Sessions: random token in an HttpOnly (+Secure over HTTPS) SameSite=Lax
  cookie, 30 days, kept in memory only.

Endpoints:
  GET  /          dashboard (auth; no creds -> /setup, else -> /login)
  GET  /api/data  JSON snapshot from data.json (auth)
  GET  /login     login form
  POST /login     verify via auth_store.verify_credentials
  GET  /setup     setup form (only when no credentials exist)
  POST /setup     create via auth_store.create_credentials (min 8 chars)
  GET  /logout    clear cookie
  GET  /health    public {"status":"ok"}
"""
import html
import json
import os
import secrets
import socket
import ssl
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
DATA_FILE = os.path.join(HERE, "data.json")
DEFAULT_PORT = 18933
SESSION_DAYS = 30

# auth_store lives in ~/mac-compute on the Mac. Overridable for testing.
MAC_COMPUTE_DIR = os.environ.get(
    "ROUTER_STATUS_MAC_COMPUTE_DIR", os.path.join(HOME, "mac-compute"))
sys.path.insert(0, MAC_COMPUTE_DIR)
import auth_store  # noqa: E402

CERT_FILE = os.environ.get(
    "ROUTER_STATUS_CERT",
    os.path.join(HOME, "mac-compute", "certs", "status.mac.zin7.de.fullchain.crt"))
KEY_FILE = os.environ.get(
    "ROUTER_STATUS_KEY",
    os.path.join(HOME, "mac-compute", "certs", "status.mac.zin7.de.key"))
FALLBACK_CERT = os.path.join(HERE, "selfsigned.crt")
FALLBACK_KEY = os.path.join(HERE, "selfsigned.key")

SESSIONS = {}  # token -> username
USE_TLS = False


def using_tls():
    return USE_TLS


def cookie_header(token, max_age=None):
    parts = [f"rs_session={token}", "HttpOnly", "SameSite=Lax", "Path=/"]
    if using_tls():
        parts.append("Secure")
    if max_age is None:
        max_age = SESSION_DAYS * 86400
    parts.append(f"Max-Age={max_age}")
    return "; ".join(parts)


def session_user(handler):
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("rs_session="):
            return SESSIONS.get(part[len("rs_session="):])
    return None


PAGE_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
margin:0;background:#111827;color:#e5e7eb}
header{background:#1f2937;padding:14px 20px;display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
h1{font-size:18px;margin:0}h2{font-size:15px;margin:24px 0 8px;color:#9ca3af}
main{max-width:960px;margin:0 auto;padding:0 16px 32px}
.cards{display:flex;gap:12px;flex-wrap:wrap}
.card{background:#1f2937;border-radius:10px;padding:14px 18px;min-width:150px;flex:1}
.card .v{font-size:26px;font-weight:700}.card .l{color:#9ca3af;font-size:13px}
table{width:100%;border-collapse:collapse;background:#1f2937;border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:8px 12px;font-size:14px;border-bottom:1px solid #374151}
th{color:#9ca3af;font-weight:600}
canvas{width:100%;height:110px;background:#1f2937;border-radius:10px}
.meta{color:#9ca3af;font-size:13px}.warn{color:#fbbf24}.err{color:#f87171}
a{color:#60a5fa}input,button{font-size:15px;padding:8px;border-radius:6px;border:1px solid #374151}
input{background:#111827;color:#e5e7eb}button{background:#2563eb;color:#fff;border:none;cursor:pointer}
form label{display:block;margin:10px 0}
.banner{display:none;background:#451a03;color:#fbbf24;padding:10px 14px;border-radius:8px;margin:12px 0;font-size:14px}
.legend{font-size:13px;color:#9ca3af;margin:6px 0 0}
.legend .sw{display:inline-block;width:18px;height:3px;vertical-align:middle;margin:0 4px 0 12px;border-radius:2px}
details{margin-top:10px}details summary{cursor:pointer;color:#9ca3af;font-size:14px;padding:6px 0}
.unitbtn{background:#374151}
.kv td:first-child{color:#9ca3af;width:110px}
"""

DASHBOARD_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Router Status</title><style>__CSS__</style></head><body>
<header><h1>家庭网络状态 &mdash; Redmi RM2100</h1>
<span><button id="unit-toggle" class="unitbtn" type="button">单位：Mbps</button>
<a href="/logout">Logout</a></span></header>
<main>
<div class="banner" id="stale-banner"></div>
<p class="meta"><span id="meta-time">加载中&hellip;</span> <span id="meta-age"></span></p>
<h2>WAN (eth3)</h2>
<div class="cards">
<div class="card"><div class="v" id="wan-down">&ndash;</div><div class="l">下载</div></div>
<div class="card"><div class="v" id="wan-up">&ndash;</div><div class="l">上传</div></div>
<div class="card"><div class="v" id="wan-state">&ndash;</div><div class="l">接口状态</div></div>
</div>
<table class="kv" style="margin-top:12px"><tbody>
<tr><td>WAN IPv4</td><td id="wan-ip">&ndash;</td></tr>
<tr><td>网关</td><td id="wan-gw">&ndash;</td></tr>
<tr><td>DNS</td><td id="wan-dns">&ndash;</td></tr>
<tr><td>WAN IPv6</td><td id="wan-ip6">&ndash;</td></tr>
<tr><td>组网</td><td id="wan-nat">&ndash;</td></tr>
</tbody></table>
<p class="legend"><span class="sw" style="background:#60a5fa"></span>下载（入）<span class="sw" style="background:#4ade80"></span>上传（出）<span id="chart-range" style="float:right"></span></p>
<canvas id="spark"></canvas>
<h2>接口</h2>
<table><thead><tr><th>接口</th><th>入</th><th>出</th>
<th>累计入</th><th>累计出</th><th>状态</th></tr></thead>
<tbody id="ifaces"></tbody></table>
<details id="other-ifaces-wrap"><summary id="other-ifaces-summary">其他接口</summary>
<table><thead><tr><th>接口</th><th>入</th><th>出</th>
<th>累计入</th><th>累计出</th><th>状态</th></tr></thead>
<tbody id="ifaces-other"></tbody></table></details>
<h2>系统</h2>
<div class="cards">
<div class="card"><div class="v" id="load">&ndash;</div><div class="l">负载 (1/5/15 分钟)</div></div>
<div class="card"><div class="v" id="mem">&ndash;</div><div class="l">内存占用</div></div>
<div class="card"><div class="v" id="uptime">&ndash;</div><div class="l">运行时间</div></div>
<div class="card"><div class="v" id="conntrack">&ndash;</div><div class="l">连接数 (当前 / 上限)</div></div>
</div>
<h2>设备 (<span id="devcount">0</span>)</h2>
<table><thead><tr><th>名称</th><th>IP</th><th>MAC</th><th>连接</th><th>信号</th><th>状态</th><th>最后活跃</th><th>DNS 查询</th><th>拦截</th></tr></thead>
<tbody id="devices"></tbody></table>
<p class="meta" id="adguard-line"></p>
<div id="upstream-section">
<h2>上游设备 (WAN 侧，不计入客户端)</h2>
<table><thead><tr><th>名称</th><th>IP</th><th>MAC</th><th>接口</th><th>角色</th></tr></thead>
<tbody id="upstream"></tbody></table>
</div>
<p class="meta">WAN 为双层 NAT（192.168.1.x，上游为 ONT）。LAN 侧入 / 出按客户端视角显示（路由器 TX = 客户端下载）。WiFi 关联与信号来自路由器驱动实时查询；未关联无线的一律按有线显示。接口速率每 10 秒采样；页面每 5 秒刷新，切到后台时暂停。</p>
</main>
<script>
const $=id=>document.getElementById(id);
// All DOM writes use textContent (never innerHTML), so values are escaped by construction.
const num=v=>(typeof v==='number'&&isFinite(v))?v:null;
const fmtBytes=b=>{if(b==null)return'–';const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<u.length-1){b/=1024;i++}return b.toFixed(1)+' '+u[i]};
let rateUnit=localStorage.getItem('rs-rate-unit')||'mbps';
function fmtRate(mbps){
  const v=num(mbps); if(v==null) return '–';
  if(rateUnit==='mbs'){
    const m=v/8;
    if(m>=1) return m.toFixed(2)+' MB/s';
    return (m*1024).toFixed(m>=0.01?0:1)+' KB/s';
  }
  if(v>=1) return v.toFixed(2)+' Mbps';
  return (v*1000).toFixed(v>=0.01?0:1)+' Kbps';
}
const fmtClock=t=>{const d=new Date(t*1000);const p=n=>String(n).padStart(2,'0');return p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds())};
function fmtAxis(v){ // v in Mbps, for chart gridline labels
  if(rateUnit==='mbs'){ const m=v/8; return m>=1?m.toFixed(1)+' MB/s':Math.round(m*1024)+' KB/s'; }
  return v>=1?v.toFixed(1)+' Mbps':Math.round(v*1000)+' Kbps';
}
function niceCeil(v){ if(!(v>0)) return 1; const p=Math.pow(10,Math.floor(Math.log10(v))); const n=v/p; const c=n<=1?1:n<=2?2:n<=5?5:10; return c*p; }
// Row-level table sync: reuse <tr> by key, update textContent in place,
// append new rows, remove stale ones, keep DOM order == items order.
function syncRows(tbody,items,keyFn,cellsFn,decorate){
  const byKey={};
  for(const tr of tbody.children) byKey[tr.dataset.key]=tr;
  const seen=new Set();
  for(const it of items){
    const k=keyFn(it); seen.add(k);
    let tr=byKey[k];
    if(!tr){ tr=document.createElement('tr'); tr.dataset.key=k; }
    const cells=cellsFn(it);
    while(tr.children.length<cells.length) tr.appendChild(document.createElement('td'));
    while(tr.children.length>cells.length) tr.removeChild(tr.lastChild);
    cells.forEach((v,i)=>{ const td=tr.children[i]; const s=String(v); if(td.textContent!==s) td.textContent=s; });
    if(decorate) decorate(tr,it);
    tbody.appendChild(tr);
  }
  for(const k of Object.keys(byKey)) if(!seen.has(k)) byKey[k].remove();
}
function drawChart(history){
  const c=$('spark'); if(!c) return;
  const dpr=window.devicePixelRatio||1;
  const w=c.clientWidth||c.parentElement.clientWidth||800;
  const h=110;
  const W=Math.max(1,Math.round(w*dpr)), H=Math.round(h*dpr);
  if(c.width!==W||c.height!==H){ c.width=W; c.height=H; }
  const ctx=c.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);
  ctx.font='11px sans-serif';
  if(!history||!history.length){
    ctx.fillStyle='#9ca3af'; ctx.fillText('暂无历史数据',8,16); return;
  }
  const down=history.map(x=>num((x.rx_mbps||{}).eth3)??0);
  const up=history.map(x=>num((x.tx_mbps||{}).eth3)??0);
  const max=niceCeil(Math.max(0,...down,...up));
  const L=52,R=8,T=8,B=18;
  const pw=w-L-R, ph=h-T-B;
  const X=i=>L+(i/Math.max(history.length-1,1))*pw;
  const Y=v=>T+ph-(v/max)*ph;
  ctx.strokeStyle='#374151'; ctx.lineWidth=1; ctx.fillStyle='#9ca3af';
  [0,0.5,1].forEach(f=>{
    const y=Y(max*f);
    ctx.beginPath(); ctx.moveTo(L,y); ctx.lineTo(w-R,y); ctx.stroke();
    ctx.fillText(fmtAxis(max*f),4,y+4);
  });
  const line=(vals,color)=>{ ctx.strokeStyle=color; ctx.lineWidth=2; ctx.beginPath();
    vals.forEach((v,i)=>{ i?ctx.lineTo(X(i),Y(v)):ctx.moveTo(X(i),Y(v)); }); ctx.stroke(); };
  line(down,'#60a5fa'); line(up,'#4ade80');
  ctx.fillStyle='#9ca3af';
  ctx.fillText(fmtClock(history[0].t),L,h-5);
  const endStr=fmtClock(history[history.length-1].t);
  ctx.fillText(endStr,w-R-ctx.measureText(endStr).width,h-5);
  $('chart-range').textContent='最近 '+history.length+' 个采样（约 '+Math.round(history.length*10/60)+' 分钟），峰值下载 '+fmtRate(Math.max(0,...down))+' / 上传 '+fmtRate(Math.max(0,...up));
}
const ORDER=['eth3','br0','eth2','ra0','rai0'];
const LAN_MEMBERS=['eth2','ra0','rai0'];
let lastData=null;
function render(d){
  lastData=d;
  if(!d.ok){
    $('meta-time').textContent='采集器暂无实时数据'+(d.error?'：'+d.error:'');
    return;
  }
  $('meta-time').textContent='更新于 '+(d.updated_at||'');
  const w=d.wan||{};
  $('wan-down').textContent=fmtRate(w.rx_mbps);
  $('wan-up').textContent=fmtRate(w.tx_mbps);
  $('wan-state').textContent=w.up?'UP':'DOWN';
  $('wan-state').style.color=w.up?'#4ade80':'#f87171';
  $('wan-ip').textContent=w.ip?(w.ip+(w.prefix!=null?'/'+w.prefix:'')):'–';
  $('wan-gw').textContent=w.gateway||'–';
  $('wan-dns').textContent=(w.dns&&w.dns.length)?w.dns.join(', '):'–';
  $('wan-ip6').textContent=w.ipv6?(w.ipv6+(w.ipv6_valid_lft_sec!=null?'（剩余约 '+Math.round(w.ipv6_valid_lft_sec/3600)+' 小时）':'')):'–';
  $('wan-nat').textContent=w.double_nat===true?'是 — 双层 NAT（WAN 为 192.168.1.x，上游为 ONT）':w.double_nat===false?'否':'–';
  const s=d.system||{};
  $('load').textContent=[s.load1,s.load5,s.load15].map(v=>v==null?'–':v.toFixed(2)).join(' / ');
  $('mem').textContent=s.mem_used_pct==null?'–':s.mem_used_pct+'%'+(s.mem_available_kb!=null?'（可用 '+fmtBytes(s.mem_available_kb*1024)+'）':'');
  $('uptime').textContent=s.uptime||'–';
  const ct=d.conntrack||{};
  $('conntrack').textContent=(ct.count==null||ct.max==null)?'–':ct.count+' / '+ct.max+(ct.pct!=null?'（'+ct.pct+'%）':'');
  const ifs=d.interfaces||{};
  const main=ORDER.filter(n=>ifs[n]).map(n=>({name:n,...ifs[n]}));
  const others=Object.keys(ifs).filter(n=>!ORDER.includes(n))
    .map(n=>({name:n,...ifs[n]}))
    .filter(i=>i.up||i.rx_bytes>0||i.tx_bytes>0);
  // LAN rows are shown from the client's point of view: router TX = client download.
  const ifaceCells=i=>{
    const sw=i.name!=='eth3';
    const rin=sw?i.tx_mbps:i.rx_mbps, rout=sw?i.rx_mbps:i.tx_mbps;
    const bin=sw?i.tx_bytes:i.rx_bytes, bout=sw?i.rx_bytes:i.tx_bytes;
    const label=(LAN_MEMBERS.includes(i.name)?'└ ':'')+(i.label||i.name)+' ('+i.name+')';
    return [label,fmtRate(rin),fmtRate(rout),fmtBytes(bin),fmtBytes(bout),i.up?'UP':'DOWN'];
  };
  const stateColor=(tr,i)=>{ tr.children[5].style.color=i.up?'#4ade80':'#f87171'; };
  syncRows($('ifaces'),main,i=>i.name,ifaceCells,(tr,i)=>{ tr.style.fontWeight=i.name==='br0'?'700':''; stateColor(tr,i); });
  const wrap=$('other-ifaces-wrap');
  if(others.length){
    wrap.style.display='';
    $('other-ifaces-summary').textContent='其他接口（'+others.length+'）';
    syncRows($('ifaces-other'),others,i=>i.name,ifaceCells,stateColor);
  } else wrap.style.display='none';
  const devs=d.devices||[];
  $('devcount').textContent=devs.length;
  syncRows($('devices'),devs,x=>x.mac,x=>[
    x.name+(x.random_mac?' [随机MAC]':''),
    x.ip+((x.ips&&x.ips.length>1)?' (+'+(x.ips.length-1)+')':''),
    x.mac, x.link||'–', x.rssi==null?'–':x.rssi+' dBm',
    x.online?'在线':'离线/休眠',
    x.online?'—':(x.last_seen||'–'),
    x.dns_queries==null?'–':String(x.dns_queries),
    x.dns_blocked==null?'–':String(x.dns_blocked),
  ],(tr,x)=>{ tr.children[5].style.color=x.online?'#4ade80':'#fbbf24'; });
  const ups=d.upstream||[];
  $('upstream-section').style.display=ups.length?'':'none';
  syncRows($('upstream'),ups,x=>x.mac,x=>[x.name,x.ip,x.mac,x.interface||'',x.role||'']);
  const ag=d.adguard||{};
  $('adguard-line').textContent=ag.ok
    ?('AdGuard（24h）：共 '+(ag.total_queries_24h==null?'–':ag.total_queries_24h)+' 查询，'+(ag.total_blocked_24h==null?'–':ag.total_blocked_24h)+' 拦截；每设备“拦截”列为'+(ag.blocked_window||'最近日志')+'内统计。')
    :('AdGuard 数据暂不可用'+(ag.error?'：'+ag.error:'')+'。');
  drawChart(d.history);
}
function tickAge(){
  if(!lastData||!lastData.updated_at_epoch) return;
  const age=Math.max(0,Math.round(Date.now()/1000-lastData.updated_at_epoch));
  $('meta-age').textContent='（'+age+' 秒前）';
  const b=$('stale-banner');
  if(age>30){ b.style.display='block'; b.textContent='⚠️ 数据已超过 30 秒未更新（'+age+' 秒前），请检查采集器是否在运行。'; }
  else b.style.display='none';
}
let timer=null;
async function refresh(){
  try{
    const r=await fetch('/api/data',{signal:AbortSignal.timeout(4000)});
    if(!r.ok){ $('meta-time').textContent='请求失败：HTTP '+r.status; return; }
    render(await r.json());
  }catch(e){ $('meta-time').textContent='拉取失败：'+e; }
}
async function loop(){
  if(!document.hidden) await refresh();
  timer=setTimeout(loop,5000);
}
document.addEventListener('visibilitychange',()=>{
  if(!document.hidden){ clearTimeout(timer); loop(); }
});
window.addEventListener('resize',()=>{ if(lastData) drawChart(lastData.history); });
setInterval(tickAge,1000);
$('unit-toggle').addEventListener('click',()=>{
  rateUnit=rateUnit==='mbps'?'mbs':'mbps';
  localStorage.setItem('rs-rate-unit',rateUnit);
  $('unit-toggle').textContent='单位：'+(rateUnit==='mbs'?'MB/s':'Mbps');
  if(lastData) render(lastData);
});
$('unit-toggle').textContent='单位：'+(rateUnit==='mbs'?'MB/s':'Mbps');
loop();
</script></body></html>
""".replace("__CSS__", PAGE_CSS)


def form_page(title, action, fields, error=None, note=None):
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    note_html = f'<p class="meta">{note}</p>' if note else ""
    inputs = "".join(
        f'<label>{html.escape(label)}<br>'
        f'<input type="{typ}" name="{name}" required autocomplete="off"></label>'
        for label, name, typ in fields)
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head><body>
<header><h1>Router Status</h1></header>
<main><h2>{html.escape(title)}</h2>{err}{note_html}
<form method="post" action="{action}">{inputs}
<button type="submit">{html.escape(title)}</button></form></main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))
        sys.stderr.flush()

    # -- helpers ---------------------------------------------------------
    def send_html(self, code, body, extra_headers=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, path, extra_headers=None):
        self.send_response(302)
        self.send_header("Location", path)
        self.send_header("Content-Length", "0")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()

    def read_form(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def require_login(self):
        """Returns username or sends a redirect and returns None."""
        if not auth_store.credentials_exist():
            self.redirect("/setup")
            return None
        user = session_user(self)
        if not user:
            self.redirect("/login")
            return None
        return user

    # -- routing ---------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            self.send_json(200, {"status": "ok"})
        elif path == "/":
            if self.require_login() is None:
                return
            self.send_html(200, DASHBOARD_HTML)
        elif path == "/api/data":
            if self.require_login() is None:
                return
            try:
                with open(DATA_FILE, encoding="utf-8") as f:
                    self.send_json(200, json.load(f))
            except (FileNotFoundError, json.JSONDecodeError):
                self.send_json(200, {"ok": False,
                                     "error": "no data collected yet"})
        elif path == "/login":
            if not auth_store.credentials_exist():
                self.redirect("/setup")
                return
            self.send_html(200, form_page(
                "Login", "/login",
                [("Username", "username", "text"),
                 ("Password", "password", "password")]))
        elif path == "/setup":
            if auth_store.credentials_exist():
                self.redirect("/login")
                return
            self.send_html(200, form_page(
                "Set up login", "/setup",
                [("Username", "username", "text"),
                 ("Password (min 8 characters)", "password", "password"),
                 ("Confirm password", "confirm", "password")],
                note="First visit: choose the username and password for this "
                     "dashboard. It is shared with mac-compute."))
        elif path == "/logout":
            self.redirect("/login", [
                ("Set-Cookie", cookie_header("", max_age=0))])
        else:
            self.send_html(404, "<h1>404</h1>")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            if not auth_store.credentials_exist():
                self.redirect("/setup")
                return
            form = self.read_form()
            if auth_store.verify_credentials(form.get("username", ""),
                                             form.get("password", "")):
                token = secrets.token_urlsafe(32)
                SESSIONS[token] = form["username"].strip()
                self.redirect("/", [("Set-Cookie", cookie_header(token))])
            else:
                self.send_html(200, form_page(
                    "Login", "/login",
                    [("Username", "username", "text"),
                     ("Password", "password", "password")],
                    error="Invalid username or password."))
        elif path == "/setup":
            if auth_store.credentials_exist():
                self.redirect("/login")
                return
            form = self.read_form()
            username = form.get("username", "").strip()
            password = form.get("password", "")
            confirm = form.get("confirm", "")
            error = None
            if not username:
                error = "Username must not be empty."
            elif len(password) < 8:
                error = "Password must be at least 8 characters."
            elif password != confirm:
                error = "Passwords do not match."
            if error:
                self.send_html(200, form_page(
                    "Set up login", "/setup",
                    [("Username", "username", "text"),
                     ("Password (min 8 characters)", "password", "password"),
                     ("Confirm password", "confirm", "password")],
                    error=error))
                return
            try:
                auth_store.create_credentials(username, password)
            except (RuntimeError, ValueError) as e:
                self.send_html(200, form_page(
                    "Set up login", "/setup",
                    [("Username", "username", "text"),
                     ("Password (min 8 characters)", "password", "password"),
                     ("Confirm password", "confirm", "password")],
                    error=str(e)))
                return
            self.redirect("/login")
        else:
            self.send_html(404, "<h1>404</h1>")


def pick_tls():
    """Return (cert, key) for the real cert, the self-signed fallback,
    or (None, None) for plain HTTP."""
    global USE_TLS
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        print(f"TLS: real cert {CERT_FILE}", flush=True)
        return CERT_FILE, KEY_FILE
    if os.path.exists(FALLBACK_CERT) and os.path.exists(FALLBACK_KEY):
        print("WARNING: real cert missing, using temporary self-signed cert "
              f"{FALLBACK_CERT} -- replace with the real cert", flush=True)
        return FALLBACK_CERT, FALLBACK_KEY
    print("WARNING: no TLS cert found -- serving PLAIN HTTP (testing only). "
          "Final state must be HTTPS with the real cert.", flush=True)
    return None, None


def serve(server):
    try:
        server.serve_forever()
    except Exception as e:
        print(f"server thread error: {e}", flush=True)


def main():
    global USE_TLS
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    port = int(os.environ.get("ROUTER_STATUS_PORT", port))
    cert, key = pick_tls()
    context = None
    if cert and key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        USE_TLS = True

    servers = []
    # IPv4 (LAN + Tailscale)
    try:
        s4 = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        servers.append(("IPv4 0.0.0.0", s4))
    except OSError as e:
        print(f"IPv4 bind failed: {e}", flush=True)
    # IPv6 (public IPv6) -- macOS '::' is IPv6-only, hence the second server
    try:
        class Server6(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        s6 = Server6(("::", port), Handler)
        servers.append(("IPv6 ::", s6))
    except OSError as e:
        print(f"IPv6 bind failed: {e}", flush=True)
    if not servers:
        print(f"FATAL: could not bind port {port}", flush=True)
        sys.exit(1)
    for _name, srv in servers:
        if context:
            srv.socket = context.wrap_socket(srv.socket, server_side=True)
        t = threading.Thread(target=serve, args=(srv,), daemon=True)
        t.start()
    scheme = "https" if context else "http"
    print(f"router-status server on port {port} ({scheme}): "
          + ", ".join(n for n, _ in servers), flush=True)
    print("auth file:", auth_store.AUTH_FILE,
          "credentials exist:", auth_store.credentials_exist(), flush=True)
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
