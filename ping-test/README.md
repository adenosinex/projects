# ping-test — IP 延迟测试

从家里（重庆移动）直接测目标 IP / 域名的延迟。

- 地址：https://cpl.zin7.de/ping/<32-hex-token>/ —— 公开 token 链接，打开就能测，不用登录。token 存在 Mac 上 `/Users/zin/ping-test/token`（600），token 就是凭证，不要外传。链接也显示在登录后的导航页 https://cpl.zin7.de/ 上（导航页每次渲染时读 token 文件）。
- 管理：没有密码、没有管理页面。token 是单个固定值；需要轮换时直接在 Mac 上操作（换掉 `/Users/zin/ping-test/token` 后重启 app）。
- 本地：FastAPI，127.0.0.1:18938，`bash /Users/zin/ping-test/restart.sh`
- 输入：IP / 域名，每行一个，也可用逗号分隔；域名先解析成 IP 再测
- 模式：ICMP ping（默认，`ping -c N -t T`，IPv6 用 ping6）；TCP 建连耗时（禁 ping 的主机用，默认 443 端口，3 次取平均）
- 并行：ThreadPoolExecutor，默认 10 线程（页面上 1–20 可调），每个 ping 是独立 subprocess，无共享状态
- 结果：解析 IP、收发包数、丢包率、min/avg/max、状态（正常 / 不可达 / 超时 / DNS 失败）；表格可按列排序，默认按平均延迟
- 历史：最近 20 次运行摘要存在 `data/history.json`
- 限流（链接公开，需防滥用）：同时只跑 1 个测试，每次最多 50 个目标；token 错了返回 404

## 命令行调用（curl）

不用打开网页，直接用 curl 测。两个同步接口，和网页版共用同一套校验、限幅、50 目标上限和 1 并发规则（正在跑时返回 409）：

```bash
# GET：最简单，target 参数可重复；也可用一个逗号分隔的 targets 参数
curl --max-time 120 "https://cpl.zin7.de/ping/<token>/api/ping?target=8.8.8.8&target=1.1.1.1&count=4&workers=10"

# POST：JSON 传参，和网页版一样的字段
curl --max-time 120 -X POST "https://cpl.zin7.de/ping/<token>/api/test-sync" \
  -H "Content-Type: application/json" \
  -d '{"targets":["8.8.8.8","1.1.1.1","223.5.5.5"],"mode":"ping","count":4,"workers":10}'

# TCP 模式（禁 ping 的主机）
curl --max-time 120 "https://cpl.zin7.de/ping/<token>/api/ping?target=223.5.5.5&mode=tcp&tcp_port=443"
```

参数（和网页版一致，全部可选，超限自动限幅）：`mode` ping|tcp（默认 ping）、`count` 发包数 1–20（默认 4）、`timeout` 超时秒数 1–30（默认 5）、`workers` 并发线程 1–20（默认 10）、`tcp_port`（默认 443）、`tcp_attempts`（默认 3）。网页版上也有“命令行调用”一节，示例里的 token 是当前值（页面渲染时填入）。

返回示例（完整跑完的结果 JSON，和 `GET /api/test/{run_id}` 的结构一致）：

```json
{
  "run_id": "a1b2c3d4e5f6", "status": "done", "mode": "ping",
  "workers": 10, "done": 3, "total": 3,
  "started_at": 1794540000.0, "finished_at": 1794540005.6, "elapsed_s": 5.6,
  "results": [
    {"target": "8.8.8.8", "ip": "8.8.8.8", "status": "ok", "detail": "",
     "sent": 4, "received": 4, "loss_pct": 0.0,
     "min_ms": 270.1, "avg_ms": 276.4, "max_ms": 285.2},
    {"target": "192.0.2.1", "ip": "192.0.2.1", "status": "unreachable", "detail": "",
     "sent": 4, "received": 0, "loss_pct": 100.0,
     "min_ms": null, "avg_ms": null, "max_ms": null}
  ],
  "summary": {"run_id": "a1b2c3d4e5f6", "elapsed_s": 5.6, "mode": "ping",
              "targets": 3, "ok": 2, "failed": 1,
              "best_avg_ms": 276.4, "worst_avg_ms": 299.1}
}
```

注意：
- 同步调用会阻塞到测完才返回：目标多、超时大时一次请求可能要几十秒，`--max-time` 建议给 120 秒；不要在服务端再加一个比测试本身还短的全局超时。
- 409：已有测试在跑时，同步和异步启动都会返回 `{"error": "a test is already running", "run_id": "..."}`。
- 404：token 错了两个新接口都返回 404；目标为空返回 400，超过 50 个目标返回 400。
- 异步接口仍可用：`POST /api/test` 启动后用 `GET /api/test/{run_id}` 轮询；同步接口只是省掉轮询。

Nginx：cpl.zin7.de 的 `/ping/` 是 PUBLIC（照 sub-convert 的 `/s/` 样式，不走 auth_request），把完整路径透传给 127.0.0.1:18938，由 app 按 URL 里的 token 校验。
重载（需要 sudo，配置改完后由用户执行）：
`sudo kill -HUP $(cat /opt/homebrew/var/run/nginx-status-443.pid)`

## 验证

- 2026-11-13（首版）：6 个目标并行 10 线程，5.0 秒跑完（串行要 25 秒以上）；223.5.5.5 平均 58.6ms、8.8.8.8 109.9ms、1.1.1.1 76.5ms（25% 丢包）、114.114.114.114 与 192.0.2.1 均 100% 丢包记不可达、www.google.com 解析到 69.171.235.22 后 100% 丢包。
- 2026-11-13（命令行调用）：对公开链接实测 curl——GET /api/ping（8.8.8.8、1.1.1.1、192.0.2.1）5.0 秒返回完整 JSON：1.1.1.1 平均 49.0ms、8.8.8.8 平均 66.5ms（均为 0% 丢包）、192.0.2.1 100% 丢包记不可达；POST /api/test-sync（8.8.8.8、223.5.5.5、1.1.1.1）5.0 秒返回：223.5.5.5 平均 40.2ms、8.8.8.8 平均 66.2ms、1.1.1.1 25% 丢包平均 48.8ms；错误 token 在两个新接口上都返回 404；重启后 health 与 token 页均为 200；页面上的“命令行调用”示例已填入当前 token（无残留占位符）。注：延迟逐轮波动较大（同批目标另一轮曾测得 274–299ms），以实测值为准。
