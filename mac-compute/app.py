"""mac-compute: FastAPI service, SQLite queue, single-node Mac compute.

Clients only upload / poll / download. All compute runs here.
Access is protected by an access code (see config.py: only its
SHA-256 is stored). Browser users log in once on the homepage;
API clients send header X-Access-Code with each request.
"""
import hashlib
import hmac
import json
import shutil
import threading
import time
import zipfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

import jobs as jobstore
import stats as sysstats
from config import (
    ACCESS_CODE, ACCESS_CODE_HASH, APPLE_CERT_FILE, APPLE_KEY_FILE,
    AUTH_COOKIE, CERT_FILE, HEAVY_TYPES, HOST, INPUT_DIR, KEY_FILE,
    MAX_RETRIES, OUTPUT_DIR, PORT, WORK_DIR,
)
from dispatch import JOB_TYPES, run_job

app = FastAPI(title="mac-compute")
jobstore.init_db()
sysstats.init_stats_table()


def _auth_token(code):
    """Cookie token for a code: HMAC-style hash, never the code itself."""
    return hashlib.sha256(f"mac-compute:{code}".encode()).hexdigest()


def _code_ok(code):
    if not code:
        return False
    if ACCESS_CODE and hmac.compare_digest(code, ACCESS_CODE):
        return True
    return hmac.compare_digest(_auth_token(code), ACCESS_CODE_HASH)


def _is_authed(request: Request):
    # API: header X-Access-Code. Browser: login cookie.
    # Deliberately no ?code= in URLs — the code would land in logs.
    if _code_ok(request.headers.get("X-Access-Code", "")):
        return True
    return hmac.compare_digest(
        request.cookies.get(AUTH_COOKIE, ""), ACCESS_CODE_HASH)


def _require_auth(request: Request):
    if not _is_authed(request):
        raise HTTPException(401, "access code required: header X-Access-Code")


LOGIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>mac-compute · 登录</title><style>body{font-family:sans-serif;max-width:480px;margin:4em auto;padding:0 1em}</style></head><body>
<h1>mac-compute</h1>
<p>Mac mini M4 上的算力服务，请输入访问码。</p>
<form method="post" action="/login">
<input type="password" name="code" placeholder="访问码" autofocus required>
<button>进入</button></form>
<p style="color:#888">访问码不对会回到这里。不要把访问码发给别人。</p>
</body></html>"""

CONSOLE_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>mac-compute</title><style>body{font-family:sans-serif;max-width:900px;margin:2em auto;padding:0 1em}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px;text-align:left}
progress{width:120px}code{background:#f0f0f0;padding:1px 4px}pre{background:#f6f6f6;padding:10px;overflow:auto}</style></head><body>
<h1>mac-compute · Mac mini M4</h1>
<p>所有算力都在 Mac 上，客户端只上传 / 看进度 / 下载结果。<a href="/logout">退出登录</a></p>

<h2>怎么用</h2>
<ol>
<li><b>选类型、传文件、提交。</b>只传必要的文件就行，参数都有默认值，不想调就不用填。</li>
<li><b>等进度条走完。</b>重任务（转写、转码）一次只跑一个，轻任务最多 4 个并行；下一个任务可以提前上传，不用等上一个跑完。</li>
<li><b>点“下载”拿结果。</b>结果是一个 zip，里面是处理好的文件和报告。</li>
</ol>
<p>任务失败会自动重试 2 次，第 3 次失败会进死信，不会卡住后面的任务，死信可以点“重试”。进行中的任务可以点“取消”。</p>

<h2>任务类型和默认值</h2>
<table><tr><th>类型</th><th>做什么</th><th>默认值</th></tr>
<tr><td>asr</td><td>音频 / 视频转文字</td><td>Qwen3-ASR 0.6B，45 秒分片；想更准传 <code>{"model":"accurate"}</code></td></tr>
<tr><td>asr_batch</td><td>多个音频 / 视频一次转完</td><td>一次传多个文件，串行处理，一个模型跑完全批；坏文件只记失败不卡整批，结果一个 zip：每个文件一个子目录 + INDEX.csv + batch.txt（全部文本合集）</td></tr>
<tr><td>video_transcode</td><td>视频转码 / 压缩</td><td>默认 1080p，源视频更小就按原分辨率，绝不放大；HEVC</td></tr>
<tr><td>video_thumbs</td><td>从视频里抽帧选优</td><td>每秒 1 帧，留 Top 10</td></tr>
<tr><td>image_select</td><td>多张截图选优（传 zip）</td><td>留 Top 10，糊的丢掉，重复的只留最好那张</td></tr>
<tr><td>image_batch</td><td>图片批量压缩 / 转换</td><td>JPG 85，长边 1920，重复图跳过</td></tr>
<tr><td>ocr</td><td>图片 / PDF 转文字</td><td>RapidOCR，中英文</td></tr>
<tr><td>data_csv</td><td>CSV 清洗去重</td><td>去重、去空行</td></tr>
</table>

<h2>命令行也可以用</h2>
<pre>curl -H "X-Access-Code: 访问码" -F "file=@视频.mp4" -F "type=video_transcode" https://apple.su7.dpdns.org:5850/api/jobs
curl -H "X-Access-Code: 访问码" -F "file=@截图.zip" -F "type=image_select" -F 'params={"top_n":10}' https://apple.su7.dpdns.org:5850/api/jobs
curl -H "X-Access-Code: 访问码" -F "file=@a.m4a" -F "file=@b.m4a" -F "file=@c.m4a" -F "type=asr_batch" https://apple.su7.dpdns.org:5850/api/jobs
curl -H "X-Access-Code: 访问码" https://apple.su7.dpdns.org:5850/api/jobs/&lt;任务ID&gt;/download -o 结果.zip</pre>
<p>访问地址：<code>https://apple.su7.dpdns.org:5850</code>（IPv6），Tailscale 内也可以用 <code>https://zindemac-mini.tail35e420.ts.net:5850</code>（打开会报证书名字不符，因为证书是给 apple 那个域名发的）。</p>

<h2>系统占用 <span id="now" style="font-size:14px;font-weight:normal"></span></h2>
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

<h2>提交任务</h2>
<form id="f" enctype="multipart/form-data">
<input type="file" name="file" multiple required>
<select name="type">
<option value="asr">asr 音频转文字 (Qwen3-ASR)</option>
<option value="asr_batch">asr_batch 多个音频一次转完 (选多个文件)</option>
<option value="video_transcode">video_transcode 视频转码 (默认1080p，小于原分辨率不放大)</option>
<option value="video_thumbs">video_thumbs 视频截图选优</option>
<option value="image_select">image_select 多张截图选优</option>
<option value="image_batch">image_batch 图片批量压缩</option>
<option value="ocr">ocr 图片/PDF转文字 (RapidOCR)</option>
<option value="data_csv">data_csv CSV清洗去重</option>
</select>
<input name="params" placeholder='{"top_n":10}' size="20">
<button>提交任务</button></form>
<p id="msg"></p>
<h2>任务</h2><table><tr><th>ID</th><th>类型</th><th>文件</th><th>状态</th><th>进度</th><th>信息</th><th>操作</th></tr><tbody id="tb"></tbody></table>
<script>
async function load(){let r=await fetch('/api/jobs');if(r.status===401){location.href='/';return;}let j=await r.json();
document.getElementById('tb').innerHTML=j.map(x=>`<tr><td>${x.id}</td><td>${x.type}</td><td>${x.filename}</td><td>${x.status}</td><td><progress value="${x.progress}" max="1"></progress> ${Math.round(x.progress*100)}%</td><td>${x.message||''} ${x.error||''}</td><td>${x.status==='done'?`<a href="/api/jobs/${x.id}/download">下载</a>`:''} ${['failed','dead'].includes(x.status)?`<button onclick="retry('${x.id}')">重试</button>`:''} ${['queued','running'].includes(x.status)?`<button onclick="cancel('${x.id}')">取消</button>`:''}</td></tr>`).join('');}
async function retry(id){await fetch(`/api/jobs/${id}/retry`,{method:'POST'});load();}
async function cancel(id){await fetch(`/api/jobs/${id}/cancel`,{method:'POST'});load();}
document.getElementById('f').onsubmit=async e=>{e.preventDefault();let r=await fetch('/api/jobs',{method:'POST',body:new FormData(e.target)});let j=await r.json();document.getElementById('msg').textContent=r.ok?('已提交 '+j.id):('失败 '+JSON.stringify(j));load();};
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
  // job run bands
  ctx.fillStyle='rgba(0,0,0,0.07)';
  (jobs||[]).forEach(j=>{let x0=(Math.max(j.started_at,t0)-t0)/(t1-t0)*W,x1=((j.finished_at||t1)-t0)/(t1-t0)*W;if(x1>0&&x0<W)ctx.fillRect(Math.max(x0,0),0,Math.min(x1,W)-Math.max(x0,0),H);});
  // gridlines 0/50/100%
  ctx.strokeStyle='#eee';ctx.fillStyle='#888';ctx.font='10px sans-serif';
  [0,50,100].forEach(v=>{let y=H-4-(v/100)*(H-14);ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke();ctx.fillText(v+'%',2,y-2);});
  if(!pts.length){ctx.fillText('暂无数据，等一分钟采样',10,H/2);return;}
  ctx.strokeStyle='#2563eb';ctx.lineWidth=1.5;ctx.beginPath();
  pts.forEach((p,i)=>{let x=(p.ts-t0)/(t1-t0)*W,y=H-4-(p[key]/100)*(H-14);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
  ctx.stroke();
  // time labels
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
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def console(request: Request):
    if not _is_authed(request):
        return LOGIN_HTML
    return CONSOLE_HTML


@app.post("/login")
def login(code: str = Form(...)):
    if not _code_ok(code):
        return HTMLResponse(LOGIN_HTML.replace(
            "访问码不对会回到这里。",
            "访问码不对，请重试。"), status_code=401)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(AUTH_COOKIE, ACCESS_CODE_HASH, httponly=True,
                    samesite="lax", path="/", max_age=30 * 24 * 3600)
    return resp


@app.get("/logout")
def logout():
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


@app.post("/api/jobs")
async def create_job(request: Request, type: str = Form(...),
                     params: str = Form("{}"),
                     file: list[UploadFile] = File(default=[])):
    _require_auth(request)
    if type not in JOB_TYPES:
        raise HTTPException(400, f"unknown type {type}, use one of {JOB_TYPES}")
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
    if not uploads:
        raise HTTPException(400, "no file uploaded")
    if type == "asr_batch":
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
def list_jobs(request: Request):
    _require_auth(request)
    return jobstore.list_jobs()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    _require_auth(request)
    j = jobstore.get_job(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return j


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, request: Request):
    _require_auth(request)
    j = jobstore.get_job(job_id)
    if not j or j["status"] != "done":
        raise HTTPException(404, "result not ready")
    out_dir = OUTPUT_DIR / job_id
    zip_path = out_dir / "result.zip"
    if not zip_path.exists():
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(out_dir.rglob("*")):
                if p.is_file() and p.name != "result.zip":
                    z.write(p, p.relative_to(out_dir))
    return FileResponse(zip_path, filename=f"{job_id}.zip")


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


def _run_one_job(job):
    """Run a single job in its own thread. One failure never blocks
    the queue: retry twice, then dead-letter."""
    job_id = job["id"]
    inputs = sorted(INPUT_DIR.glob(f"{job_id}_*"))
    if not inputs:
        jobstore.update_job(job_id, status="failed", error="input file missing", finished_at=time.time())
        return
    # Batch jobs get the whole input list; single-file jobs get one path.
    src = inputs if job["type"] == "asr_batch" else inputs[0]
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
    threading.Thread(target=_worker_loop, daemon=True).start()
    threading.Thread(target=sysstats.collector_loop, daemon=True).start()
    threading.Thread(target=sysstats.fast_sample_loop, daemon=True).start()
    # Prefer the apple.su7.dpdns.org cert; fall back to the Tailscale cert.
    cert_file, key_file = None, None
    for cf, kf in ((APPLE_CERT_FILE, APPLE_KEY_FILE), (CERT_FILE, KEY_FILE)):
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
