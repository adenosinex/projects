# nav — 导航页 cpl.zin7.de

FastAPI 导航页，列出用户的服务，登录账号密码与 cp.zin7.de（mac-compute）相同。

- 入口：https://cpl.zin7.de/（Mac nginx status-443 实例转发）
- 本地：http://127.0.0.1:18936
- 重启：`bash /Users/zin/nav/restart.sh`
- 日志：`/Users/zin/nav/app.log`
- 登录：读 ~/mac-compute/auth.json，用同一套 PBKDF2-HMAC-SHA256（60 万次）验证；登录后发 30 天 HttpOnly Secure 会话 cookie。密码不存、不记日志。
- /proxy-test/ 由 nginx 转发到 127.0.0.1:18935（proxy-test 应用），并经 nginx `auth_request` 用本应用的 /auth-check 做登录校验；/api/ 同样转发到 proxy-test。
- 会话密钥：/Users/zin/nav/session_secret（600，首次启动自动生成）。
