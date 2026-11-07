# projects

个人小项目合集（公开仓库），按文件夹分类：

- `localdy/` — 本地视频库，拆分为服务端 + 单文件客户端（视频流不经过服务端）
- `mac-compute/` — Mac mini 算力平台（FastAPI + 任务队列）；`apps/mlx-flask-transcriber/` 是独立的 Qwen3-ASR 转文字服务，也是算力平台 ASR 能力的前身
- `media-tools/` — 短视频解析、MrBeast 音频下载等小工具
- `homelab/` — LibreSpeed 测速节点、Tailscale SSH helper

说明：

- 仓库里只有代码，不含任何密钥、数据库和使用数据（`.gitignore` + 同步脚本双重排除）。
- mac-compute 的访问码、localdy 的配对 token、数据库等都只留在部署的机器上；照着 `config.example.py` / `video_paths.example.json` 配自己的。
- 本仓库由同步脚本定时自动推送。
