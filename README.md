# projects

个人小项目合集（公开仓库），按文件夹分类。所有服务都跑在 Mac mini 上，源码是这里唯一的备份。

| 文件夹 | 是什么 | 端口 |
|---|---|---|
| `mac-compute/` | Mac mini 算力平台（FastAPI + 任务队列：ASR、转码、OCR 等） | 5850 |
| `mlx-flask-transcriber/` | 独立的 Qwen3-ASR 转文字服务（算力平台 ASR 能力的前身） | — |
| `localdy-server/` | 本地视频库服务端（视频流不经过服务端） | 18930 / 18931 |
| `localdy/` | Localdy 的早期版本（客户端 + 服务端） | — |
| `sub-node/` | 自建代理节点（mihomo，AnyTLS + Hysteria2） | 8443 / 8444 |
| `sub-convert/` | 订阅转换器（测速、精选、精选中继、模板清洗） | 18937 |
| `proxy-test/` | 代理订阅测速（直连 + 中继） | 18935 |
| `ping-test/` | IP / 域名 ping 延迟测试 | 18938 |
| `dy-filter/` | 115 网盘视频预筛（在线播放、打标、结算归档） | 18939 |
| `nav/` | 导航页 cpl.zin7.de | 18936 |
| `router-status/` | 家庭网络状态面板（路由器 + AdGuard 数据采集） | 18933 |
| `doh-relay-probe/` | DoH 中继探测（15 分钟一次） | — |
| `homelab/` | 家庭实验室：nginx 配置、测速节点、SSH helper | — |
| `media-tools/` | 短视频解析、MrBeast 音频下载等小工具 | — |
| `scripts/` | 同步与运维脚本（SSH 助手、打包、自动推送） | — |

说明：

- 仓库里只有代码，不含任何密钥、数据库和使用数据（`.gitignore` + 同步脚本双重排除）。
- mac-compute 的访问码、localdy 的配对 token、115 Cookie、订阅链接、节点密码、数据库等都只留在部署的机器上；照着 `config.example.py` / `video_paths.example.json` 配自己的。
- 同步：`scripts/github-auto-push.sh` 每 3 天自动从 Mac 同步一次源码，有改动才推送；`scripts/make-projects-tarball.sh` 是 Mac 侧的打包脚本（排除 venv、数据库、密钥等）。
