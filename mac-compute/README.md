# mac-compute

Mac-only compute service. All compute runs on the Mac mini M4.
Other clients (phone / Windows / VPS) only call the API: upload a
file, watch progress in the console, download the result.

Port: **5850**, HTTPS only. **https://apple.su7.dpdns.org:5850**

## Login (username + password)

All pages and API calls need the username + password.
First visit to the homepage shows a setup form (username +
password, at least 8 characters, 12+ suggested); the same login
also works for the router status dashboard. Browser: log in once,
it stays logged in for 30 days (HttpOnly, Secure session cookie
holding a random token, never the password). API: send HTTP Basic
Auth (`-u username:password`) or header `X-Access-Code: <password>`
with every request. The password is never stored in plain text —
only its salted PBKDF2 hash in `auth.json` (mode 600, written by
`auth_store.py`). `/health` is public (no login needed).

## GitHub 上传

控制台里有 **GitHub 上传** 页（`/github`，同样需要登录）：
填 GitHub 账号、仓库名、token、分支、公开/私有，保存后告诉
Muse，Muse 会用它创建仓库、完成首次推送并开启每小时自动推送。
Token 只保存在 Mac 的 `data/github.json`（600 权限），不会进
代码仓库，也不会出现在聊天记录里。页面上有“验证 Token”按钮，
可以检查 token 属于哪个账号。换账号或换 token 就回来改了保存；
想停用就去 GitHub 上把 token 删掉（Revoke）。

## How to use (also on the homepage)

1. Pick a task type, upload the file, submit. Only the necessary
   file is needed — every parameter has a default.
2. Wait for the progress bar. Heavy tasks (ASR, transcode) run one
   at a time; light tasks run up to 4 in parallel. You can pre-upload
   the next task while the current one runs.
3. Click Download to get a zip with the finished files and report.
   A failed task retries twice, then goes to dead-letter (retry it
   from the console) and never blocks later tasks.

```bash
curl -u username:password -F "file=@a.m4a" -F "type=asr" https://cp.zin7.de:5850/api/jobs
curl -u username:password -F "file=@v.mp4" -F "type=video_transcode" https://cp.zin7.de:5850/api/jobs
curl -u username:password -F "file=@shots.zip" -F "type=image_select" -F 'params={"top_n":10}' https://cp.zin7.de:5850/api/jobs
```

## System stats (built in, no extra service)

A background thread samples CPU / memory / disk usage once a minute
(`stats.py`, psutil) into the same SQLite DB (`stats` table, 30-day
retention, one row per minute). The homepage shows the current values
plus three history charts (15min / 1h / 6h / 24h / 7d / 30d); grey bands mark
when a job was running, so a spike can be matched to the task that
caused it. The **实时** button switches to a live view: samples every
5 seconds (in-memory ring buffer, last 15 min, not stored) and refreshes
every 5 seconds. API (same login): `GET /api/stats?hours=24`,
`GET /api/stats/live`.

## ASR model cache

The Qwen3-ASR model is loaded once per service lifetime and kept
resident (`workers/asr.py`, model pool of up to 2 instances, one per
concurrent job); later jobs reuse it, so only the first ASR job after
a restart pays the ~1s load. Loading is offline-only
(`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`) — a network stall
during load once froze a job for 17 min (2026-11-07). Only one profile
is kept resident per pool slot; switching profiles evicts the other.
On startup, jobs left `running` by a killed process are re-queued
(retries+1) instead of blocking the queue forever.

**Experiment 2026-11-07: 2-way ASR parallelism** (`ASR_MAX_CONCURRENT=2`
in config.py). ASR jobs run up to 2 at once (each with its own model
instance); other heavy types stay serial and never run alongside ASR.
The dispatcher (`app.py`) runs each job in its own thread; concurrency
is gated by `jobs.next_queued()` on the DB status. Measure before
deciding to keep it: serial baseline was ~4–13s per short file and
55s for 830s of audio.

## Access / HTTPS / IPv6

- Primary URL is **https://apple.su7.dpdns.org:5850** — a real
  Let's Encrypt cert for that domain (acme.sh, HTTP-01 over public
  IPv6, in `certs/apple.su7.dpdns.org.crt/.key`, valid to 2027-02-05).
  Renewal: `acme.sh --renew -d apple.su7.dpdns.org` + the
  `--install-cert` reload hook restarts this service; also add a
  cron job if you want it fully automatic.
- The Mac **does have a public IPv6**: en0
  `2409:8a60:c025:888c:487:a53d:71d0:d40f`, kept current by
  my_web_center's dns_extension DDNS — `apple.su7.dpdns.org` (AAAA)
  points at it, `apple4.su7.dpdns.org` (A) is the LAN IPv4. Inbound
  IPv6 to the Mac works (firewall off); the domain has no A record,
  so access is IPv6-only.
- Fallback URL (Tailscale only): `https://zindemac-mini.tail35e420.ts.net:5850`
  with the Tailscale Let's Encrypt cert in `certs/` (renewed by
  `restart.sh` when <30 days remain). Mac Tailscale IPv6 is
  `fd7a:115c:a1e0::7201:764a`.
- The service binds dual-stack (one IPv4 + one IPv6 server in one
  process; macOS `::` alone is IPv6-only).
- No login is configured on mac-compute yet; the apple domain is
  reachable from the whole IPv6 internet, so add an access code
  before sharing the URL widely. Do not expose port 5850 on the router
  for IPv4 (there is no A record, so IPv4 cannot reach it anyway).

## Task types

| type | What it does | Default tool / model |
|---|---|---|
| `asr` | Audio/video -> text | mlx-audio + Qwen3-ASR-0.6B (daily) / 1.7B (accurate) |
| `asr_batch` | Many audio/video files in one job | Same ASR, one model instance for the whole batch, serial; one bad file is recorded failed and never blocks the rest; result zip = one subdir per file + INDEX.csv + batch.txt (all texts) |
| `video_transcode` | Video compress / transcode | ffmpeg + VideoToolbox HEVC, default 1080p, **never upscale** |
| `image_select` | Pick best screenshots from many images | OpenCV scoring (sharpness / brightness / contrast / colour) |
| `video_thumbs` | Extract frames from video, then score them | ffmpeg + same scorer as image_select |
| `image_batch` | Batch compress / convert / dedup images | Pillow, JPG 85 / long edge 1920 |
| `ocr` | Image / PDF -> text | RapidOCR-ONNX (fast, default); `mode: vl` = PaddleOCR-VL-1.6; `mode: long` = Unlimited OCR 2 (long docs, test first) |
| `face_cluster` | Group photos by person | OpenCV YuNet + SFace + DBSCAN |
| `data_csv` | Clean / dedup large CSV, log analysis | polars, streaming |

## Defaults

- Video transcode: **1080p default**. If the source is smaller
  (e.g. 720p, 480p), keep the **original resolution** — never upscale.
  1080p -> HEVC 5 Mbps, 720p -> 2.5 Mbps, 480p -> 1.2 Mbps.
  Audio AAC 128k, MP4 faststart.
- ASR chunking: 45s target, 60s hard cap, 27s minimum,
  two-tier silence (-32dB then -20dB), short tail <10s merged.
- Image batch: JPG quality 85, WebP 80, long edge max 1920.
- OCR: RapidOCR, Chinese + English.
- Queue: heavy tasks run **serially, one at a time**; light tasks
  up to 4 in parallel. A failed task retries twice, then goes to
  dead-letter — it never blocks the queue. Upload of the next task
  can happen while the current one runs (pre-upload).
- ASR parallelism: 2-way parallel was measured 2026-11-07 and gave
  NO gain (two 830s jobs: 103s+104s each, 108s wall vs 110s serial),
  so ASR stays serial (`ASR_MAX_CONCURRENT = 1`). MLX streams
  serialize on the Apple GPU; real batching would need a batched
  generate inside mlx-audio, which does not exist yet.
- Batch mode: `asr_batch` accepts many files in one multipart
  request (`-F "file=@a.m4a" -F "file=@b.m4a" ...`), one job, one
  progress bar, one result zip. It is tidier, not faster per file —
  files still run one after another.

## API

- `GET /` — console (upload, progress, pause / cancel / retry, download)
- `GET /health` — `{"status":"ok"}`
- `POST /api/jobs` — multipart: `file`, `type`, `params` (JSON string, optional)
- `GET /api/jobs` — list jobs (latest first)
- `GET /api/jobs/{id}` — one job
- `GET /api/jobs/{id}/download` — result zip / file
- `POST /api/jobs/{id}/cancel` — cancel a queued / running job
- `POST /api/jobs/{id}/retry` — retry a failed / dead job

Example:



## Run

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
# or: ./start.sh
```

Data lives under `data/` (input / output / work) and `jobs.db`.
Old transcriber at `~/my_web_center/pys/mlx-flask-transcriber`
is kept as reference; this project does not import from it.
