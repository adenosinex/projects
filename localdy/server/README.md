# Localdy 服务端（拆分版）

原版 `videof-dy`（Flask 单体，`app.py` + `core.py` + Vue 前端）拆分后的**服务端**。
视频字节永远不经过这里；服务端只给网页代码、存数据、记文件任务。

设计：`workspace/your_files/localdy-server-client-design.md`（§4/§5/§6/§7/§12/§13）。

## 目录

```
server/
  app.py               原版路由（已改：见“改动清单”）
  core.py              原版数据层（已改：服务端模式 + WAL + hash 查库）
  split_api.py         新增：鉴权、/api/*、/download/*、首页注入、任务队列
  index.html           原版页面（已改：注入 window.LOCALDY、客户端状态、下载链接）
  static/              原版前端（app.js 已改：票据、配对、离线提示）
  client/
    localdy_client.py  占位文件：部署时换成真正的单文件客户端
    localdy_client.bat 占位文件：同上
  videos.db            从原版复制（含 8137 条记录）
  play_records.db      从原版复制（播放记录/评分/备注/hash）
  video_paths.json     从原版复制（历史路径清单，仅作回退展示）
  file_jobs.db         运行时生成：文件任务队列
  client_status.json   运行时生成：客户端心跳
  requirements.txt     Flask / Flask-Cors / SQLAlchemy 1.4.47 / tqdm / waitress
  start.sh             启动脚本
```

## 运行

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
LOCALDY_CODE=<访问码> LOCALDY_CLIENT_TOKEN=<客户端token> ./start.sh
# 默认端口 18930；PORT 环境变量或 --port 覆盖
# HTTPS：./start.sh --cert fullchain.pem --key key.pem
```

环境变量：

| 变量 | 说明 |
|---|---|
| `LOCALDY_CODE` | 网页 + API 访问码（只存 SHA-256，不存明文）。未设置 = 无鉴权，启动时警告 |
| `LOCALDY_CLIENT_TOKEN` | 客户端 <-> 服务端共享 secret。未设置 = 客户端接口暂不校验，启动时警告 |
| `LOCALDY_DATA_DIR` | DB 位置（默认服务端目录） |
| `LOCALDY_SERVER_MODE` | `start.sh` 自动设为 `1`：禁用 core.py 里碰文件的维护函数，hash 查库不读文件 |
| `PORT` | 监听端口，默认 **18930**（设计稿 §13.1 的 :8080 是旧数，以 18930 为准） |

## 鉴权

- 浏览器：`POST /login` 输访问码 → HttpOnly + SameSite=Lax Cookie，30 天。
  API 也可带 `X-Access-Code` 请求头。`/health`、`/login`、`/download/*` 公开。
- 客户端：`X-Client-Token` 请求头，只放行 `/api/index-diff`、
  `/api/file-jobs*`、`/api/client/status`。

## 改动清单

### 新增路由（split_api.py）

| 路由 | 说明 |
|---|---|
| `GET /health` | 公开：ok / mode / auth / videos 数量 / 客户端在线状态 |
| `POST /api/index-diff` | 客户端推索引差异（见下） |
| `POST /api/file-jobs` | 建文件任务（意图先展开成具体文件再入库） |
| `GET /api/file-jobs?status=pending&client_id=` | 客户端取活（取走即认领 running；30 分钟无结果回 pending） |
| `GET /api/file-jobs/<id>` | 查任务状态（浏览器轮询） |
| `POST /api/file-jobs/<id>/result` | 客户端回报结果；`renamed:[{old,new}]` 会更新库里的路径 |
| `GET /api/client/status` | 客户端在线/端口/局域网 IP/上次扫描 |
| `POST /api/client/status` | 客户端心跳（30 秒一次） |
| `GET /download/localdy_client.py` | 公开：下载客户端（当前为占位文件） |
| `GET /download/localdy_client.bat` | 公开：同上 |
| `GET/POST /login`、`GET /logout` | 访问码登录 |

### 改过的原版路由（app.py）

- `GET /`：注入 `<script>window.LOCALDY = {api, client, download, split}</script>`；
  client 地址按访问者子网选择（同机 → 127.0.0.1，同子网 → 该子网的客户端
  局域网 IP，其余 → 127.0.0.1）。
- `GET /videos`、`GET /videos/<id>`：响应加 `file_hash`（供前端拼客户端媒体地址）。
- `GET /videos/<id>/stream` → **410**（视频流改由客户端提供）。
- `GET /video-paths`：只读展示客户端上报的根目录；`POST /video-paths`、
  `POST /video-paths/index`、`/video-paths/index/async` → **410**（路径归客户端管）。
- `POST /notes/apply-tags`：改成建 file-job（意图先展开成文件再下发）；
  `dry_run=true` 只返回计划，不建任务。
- `POST /videos/collect`：改成建 file-job（关键词先在服务端解析成文件）；
  `GET /videos/collect/preview` 保留（纯查库，不碰文件）。
- `POST /videos/<id>/play`、`POST /videos/update_score`、`GET/POST /videos/<id>/note`：
  行为不变，但底层 hash 改为查 `file_hashes`（core.py 服务端模式），不再读文件。
- `POST /videos/<id>/delete`：保留（只删库记录，不删文件，与原版一致）。
- 默认端口 80 → **18930**；`--port` / `--host` / `--cert` / `--key` CLI 参数；
  有证书时走 Flask ssl_context（waitress 不支持 TLS）。

### core.py 的改动（仅服务端副本）

- `LOCALDY_SERVER_MODE=1` 时：`record_play` / `sync_video_score` /
  `get_note` / `save_note` 的 hash 从 `file_hashes` 查，不读文件；
  `init_play_db` 跳过逐文件算 hash 的评分回填；
  `del_1score` / `copy_5score` 拒绝执行（改走 file-job）。
- 两个库开 WAL 模式（读不被写锁挡）。
- NAS UNC 路径（`\\UGREEN-1E55\...`、`\\Synology\...`）只剩常量声明与注释，
  服务端不再解析、不访问；任务 payload 里出现时只当不透明字符串存取。

### 前端改动（static/app.js + index.html，约 60 行）

- `axios.defaults.baseURL` 取 `window.LOCALDY.api`；另建 `clientApi` 实例
  （baseURL = 客户端地址，拦截器带 `X-Client-Token`）。
- 视频地址改走客户端：`${client}/media/${hash}?ticket=...`；票据按 hash 缓存，
  4.5 分钟刷新一次（约定 3：5 分钟内可重复使用）；队列变化时提前给当前 +
  相邻视频取票；加载失败时每个视频自动刷新票据重试一次。
- 侧边栏加客户端状态：离线 → 下载链接；未配对 → 配对码输入框（存 localStorage）；
  已连接 → 状态 + 下载链接。配置弹窗在拆分模式下隐藏添加/更新/移除路径
  （只读展示）。
- `apply-tags` 提交后显示“已提交给客户端执行 + 任务 id”。

## 客户端约定确认（2026-11-07，客户端实测后定死）

1. **index-diff 按 hash 做 UPSERT**：`added` 里 hash 已存在 = 路径更新，
   不建重复记录（`split_api._upsert_by_hash`：同 detail 原地更新；
   同 hash 不同 detail → 移动/改名，更新旧记录的 detail/filename 并改
   `file_hashes` 主键文件名）。换目录清理：`full_scan`/`rescan=true` +
   `root` 时，删 root 下不在本次 added/changed 里的记录；`roots=[...]`
   时，删曾经上报、现已不在列表里的根目录下的记录。两种清理都只删
   `videos` + `file_hashes`，播放/评分/备注保留。
2. **任务先展开成具体文件再下发**：`expand_job_items` 在服务端把意图解析完，
   下发的 job payload = 原参数 + `items:[{hash, path, ...}]`。
   apply-tags（备注 #tag → 视频）、collect（关键词 → 视频）、archive-1star
  （score=1）、copy-5star（score=5）、dedup（重复 hash，分组标 `keep`）、
   rename（{old,new} → {hash, path, new_path}）。`{type:"apply-tags",
   tags:[...]}` 不再直接下发。
3. **媒体票据 5 分钟内可重复使用**（绑定单个 hash），不是一次性：
   票据签发/校验在客户端实现；前端按 hash 取票后缓存复用，4.5 分钟刷新，
   加载失败时刷新重试一次。
4. **探活**：`/api/client/status` 未鉴权时返回 401（已配访问码）或 200
   （未配访问码），两种都表示路由存在——客户端把 401 也算“找到服务端”。
5. **默认端口 18930**（设计稿 §13.1 的 :8080 已作废）。
6. **视频扩展名**：服务端不扫盘；app.py 与 core.py 的 `VIDEO_EXTS` 本来就是
   同一份 12 个（mp4/mkv/avi/mov/wmv/flv/webm/m4v/ts/rmvb/mpg/mpeg），
   无需改动。

## 偏差与假设（实现时做的判断）

1. **两个库无法共享一个事务**：`videos.db` 与 `play_records.db` 是两个
   SQLite 文件，index-diff 只能做到“每个库内一个事务 + 输入先完整校验”。
   极端情况下（第二个库提交失败）可能出现一半写入；校验通过后实际发生
   概率极低。
2. **full_scan 标志**：根目录清理只在客户端明确发 `full_scan`/`rescan=true`
   时执行；如果客户端换目录后只发普通增量 diff，旧目录残留不会被清理，
   需补发一次 full_scan（或在 diff 里带完整 `roots` 列表）。
3. **同名文件冲突**：原版 `file_hashes` 主键是 filename，两个目录下同名
   文件本来就共用一条 hash 记录；hash-upsert 沿用这一限制，不做额外处理。
4. **票据响应格式**：前端同时兼容 `POST /media-tickets` 返回
   `{hash: ticket}` 与 `{tickets: {hash: ticket}}` 两种形状。
5. **`collect` 的 dest_root**：服务端只做展开，不解析目标路径；
   目标根目录由客户端在执行时决定（payload 里的 `dest_root` 原样下发）。
6. **CORS**：沿用原版的全开配置（`origins: *` + 回显 Origin）；
   真正的门是访问码 + 客户端 token，不是 CORS。
7. **Windows 路径**：客户端上报的路径是 Windows 风格（反斜杠），服务端
   跑在 Linux/macOS 上，`os.path.basename` 认不出反斜杠——所有对客户端
   路径取文件名的逻辑都改用 `ntpath.basename`（实测发现并修复：
   否则改名会把 `Video.filename` 写成完整路径，hash 关联随之断开）。

## 本机测试（2026-11-07，合成数据）

测试环境：本 VM，`LOCALDY_DATA_DIR=/tmp/localdy-test-data`（交付目录的 DB
副本，交付目录的 DB 未被改动——事后校验 counts 与原版一致：videos 8137、
play_records 1694、file_hashes 7337、five_star 18、video_notes 3、
video_scores 293，journal_mode 仍为 delete）。

- `/health` 200：`{ok:true, mode:"split", auth:true, videos:8137}`。
- 登录：错码 401、正确码 303 + Cookie；无码访问 `/` → 302 到 `/login`；
  无码 `/api/*` → 401；无码 `GET /api/client/status` → 401（约定 4）。
- 首页注入：`window.LOCALDY = {api, client, download, split}`，含下载链接
  与客户端状态块。
- `/videos/<id>/stream` → 410；`POST /video-paths`、`/video-paths/index/async`
  → 410；`GET /video-paths` → 200 只读。
- index-diff（合成 hash `a…`/`b…` 等）：add 2 → 8139；同 hash 新路径 →
  `moved:1`，数量不变，detail 已更新；remove 2 → 8137；
  full_scan 清理 `cleaned:1`；roots 缩小清理 `cleaned:2`；
  play_records 1694 始终不变（删除不删历史）。
- 任务往返：rename 建任务 → items 已展开 `{hash, path, new_path}`；
  客户端 poll 取走后状态 `running`、claimed_by 正确；上报 result 后
  `done`，`renames_applied:1`，filename 为纯名、hash 关联完好；
  collect 关键词无命中 `files:0`；非法 type → 400。
  apply-tags dry_run 计划 `files:2`；archive-1star `files:72`；
  copy-5star `files:90`；dedup `files:963`。
- 播放/评分/备注（合成视频，hash 查库不读文件）：play 成功 record_id 递增；
  `POST /videos/update_score` 成功；note 保存/读取往返成功；
  删除视频后播放/评分记录保留。
- WAL：测试库两库 `journal_mode: wal`（由服务端连接时开启）。
- `start.sh`：有 Flask 的 python3 时 `--port 18932` 正常启动，`/health` 200；
  无依赖时明确提示建 venv 装 requirements。
- 测试中发现并修复的两个问题：① `os.path.basename` 不认 Windows 反斜杠
  （改名会写坏 filename，见“偏差 7”）；② poll 曾返回取走前的 `pending`
  状态（现返回认领后的 `running`）。另：一次测试中出现的 500 是测试
  脚本 `| head -40` 截断日志管道导致服务端日志写失败的假象，换干净启动
  后全部正常，不是服务端 bug。

## 部署到 Mac（待办，不在本机做）

1. 复制整个 `server/` 到 Mac（如 `~/localdy-server/`），建 venv 装依赖。
2. 把真正的 `localdy_client.py` / `.bat` 放进 `server/client/`（替换占位文件）。
3. 设 `LOCALDY_CODE` 与 `LOCALDY_CLIENT_TOKEN`（与客户端配置里的值一致，
   不要复用 mac-compute 的访问码），`./start.sh` 启动。
4. HTTPS：用已有的 `apple.su7.dpdns.org` 证书：
   `./start.sh --cert ~/mac-compute/certs/apple.su7.dpdns.org.fullchain.crt --key ~/mac-compute/certs/apple.su7.dpdns.org.key`
   （注意用 fullchain，单 leaf 会报 verify 21；或复用 mac-compute 的
   acme.sh 续期流程）。
5. 局域网内用 `http://<Mac 局域网 IP>:18930` 访问（纯 HTTP，不触发浏览器
   Local Network Access 弹窗）；外网用 `https://apple.su7.dpdns.org:18930`。
6. 客户端在 Windows 机器上双击运行，首次输入视频目录；换目录用
   `localdy_client.py --root <新路径>`。

## 联调修复（2026-11-07，客户端 + 服务端真机联调，合成数据）

联调脚本 `/tmp/localdy-integ-test.sh`（本 VM）：25 项检查全部通过。联调中发现并修复的 4 个真问题：

1. **配对闭环缺失**：服务端只认环境变量 `LOCALDY_CLIENT_TOKEN`，而客户端首次启动自己随机生成 token——服务端永远猜不到。补了 `POST /api/pair-client`（浏览器访问码鉴权）：网页配对框里输一次客户端控制台显示的码，服务端存到 `client_token.json`（600 权限），之后客户端推索引/取任务都用它校验；未配对前放行（客户端的心跳/待推送会重试，配对后即开始校验）。
2. **任务下发缺顶层 items**：`_row_to_job` 把 items 只放在 `payload` 里，客户端 `JobStore.create` 直接读顶层 `items`，结果任务 0 个文件。现在 `_row_to_job` 把 `payload.items`/`dest`/`mode`/`tags` 提升到顶层。
3. **改名缺 new_name**：客户端改名执行器只认 item 的 `new_name`（纯文件名），服务端展开时只给了 `new_path`。现在 rename 的 item 同时带 `new_name`（`ntpath.basename`，Windows 路径）。
4. **任务结果包了一层**：客户端回报是 `{ok, status, result: {ok, status, done, total, results:[...]}}`，服务端之前直接读顶层 `results`/`renamed`，改名永远写不回库。现在 `set_job_result` 先拆 `result.result` 再读；且只有 `rename` / `apply-tags` 才把 old->new 写回 `videos.detail`（collect 是复制/硬链接，不能动库路径）。

## 部署形态（Mac mini）

两个进程共享同一份 DB（WAL 模式）：

- HTTP `:18930`（`0.0.0.0`，局域网 + Tailscale）：`./restart.sh` 启动
- HTTPS `:18931`（`::`，仅 IPv6，走 apple 域名 + 已有 Let's Encrypt fullchain 证书）：同一脚本启动

`.env`（600 权限，只放 `LOCALDY_CODE=<访问码>`；`LOCALDY_CLIENT_TOKEN` 不设，走网页配对）。

## 两种模式（2026-11-07，v0.2.0）

- **全局模式（默认）**：客户端启动即从服务端拉取全部 hash->路径表
  （`GET /api/hash-paths`，客户端 token 鉴权），本地不扫盘、不问路径；
  之后每 30 分钟同步一次，票据遇到未知 hash 时即时同步一次（节流 60 秒）。
  全局模式的视频库实际横跨多个路径（video_paths.json 里 5 个 NAS 根目录），
  所以首次运行问单个路径本来就是错的。
- **新路径模式**：`--root <路径>` 扫描该目录并上报差异（added/removed/
  changed 只针对该目录算，其他目录和服务端同步的条目原样保留）；
  该目录记入本地管理的根目录列表，可多个。心跳只上报本地管理的根目录；
  全局模式下上报空列表，服务端 /video-paths 回退到 video_paths.json 展示。
- 合并规则（客户端 sync_global_map）：本地扫描条目（src=scan）更新鲜，
  hash 冲突时保留本地路径；其余按服务端表为准（src=server）；服务端已没有
  的 server 条目删掉；本地扫描条目即使服务端没有也保留。
- `POST /scan` 可带 `{"root": "..."}` 只扫一个目录；不带则扫全部已管理的根目录；
  无管理根目录时返回 500 提示用 --root。

## 新路径会话（2026-11-07，客户端 v0.3.0）

- `--root <路径>` 不再是“加进全局索引”，而是启动一个**临时独立会话**：
  只扫这一个文件夹，索引只放客户端内存，不写盘、不上报、不进全局索引；
  重启即消失，下次换个新路径再开一次。会话页在客户端自己身上：
  `GET /session`（清单+播放+打标签/收藏/归档1星/5星复制），数据接口
  `GET /session.json`（需配对码）。
- 全局模式不受影响：会话里照样能播全局库的视频（先查会话、再查全局索引）；
  会话文件夹本身永远不会被整批索引。
- 会话里改文件后，客户端只把移动结果按文件上报 `POST /api/file-moves`：
  kind=move（rename / apply-tags / archive-1star / dedup）：库里有 old 路径
  才改 detail（复用 apply_renames），库里没有就忽略；
  kind=copy（collect / copy-5star）：只有 new 路径在文库根目录
  （video_paths.json）下、且 hash 不在库里时才入库，已在库里的是重复副本，
  保留原记录。
- 服务端下发的任务也一样：set_job_result 现在对 archive-1star / dedup
  的移动也改库路径（以前只改 rename / apply-tags）；collect / copy-5star
  仍不动库路径（复制，原文件还在）。
- `POST /scan` 现在只刷新当前会话文件夹；全局模式下没有本地扫描目录，
  返回 500 提示用 --root 启动会话。

## 会话记录持久化（2026-11-07，客户端 v0.4.0）

- 会话数据单独存在客户端本地的 `localdy_sessions.json` 里（按文件夹分条，
  一个 JSON 文件），关掉客户端不丢失；下次再 `--root` 同一个路径直接恢复
  处理进度（哪些已打标签/收藏/归档）。记录不上传服务端、不进全局索引，
  用户自己管理：删掉这个文件，或会话页里的“删除会话记录”，即忘掉。
- 磁盘为准：重开时大小没变的文件不重新算 hash、保留状态；新文件/大小变了
  的文件重新算 hash、状态回到“新”；记录里有、磁盘上没了、又不是已处理的
  文件直接丢掉。已处理（归档/去重移出文件夹的）一直保留在记录的 done 里，
  直到用户删会话记录。
