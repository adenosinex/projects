# 测速节点 - LibreSpeed

自建测速点，浏览器打开就能测 ping / jitter / 下载 / 上传。

## 方案 A：Railway Free（最快，适合临时测）

1. 打开 https://railway.com/deploy/librespeed ，点 Deploy
2. 等部署完，Railway 会给你一个 `xxx.up.railway.app` 域名
3. 浏览器打开这个域名，点 Start，约 40 秒出结果

注意成本：测速本身很耗流量。
- Railway 出站 $0.05/GB，Free 每月 $1 额度约 16GB
- 一次完整测试约 200-500MB，一天测 10 次就差不多 2-5GB
- 只适合偶尔测几次，别拿它当公开测速站

## 方案 B：VPS / Mac（推荐，流量不限才准）

```bash
docker compose up -d
# 打开 http://你的IP:8080
```

Railway 官方镜像就是一行命令：
`docker run -p 80:8080 -d --name speedtest ghcr.io/librespeed/speedtest`

## 客户端怎么测

### 手机 / 电脑浏览器（最常用）
打开测速地址，点 Start，等 40 秒，看 4 个数：
- Ping：延迟，越低越好
- Jitter：抖动，越低越稳定
- Download：下载速度
- Upload：上传速度

测 3 次取中间值，别只测 1 次。

### 命令行（适合批量、写脚本）
```bash
./client-test.sh https://xxx.up.railway.app
```

### 测代理节点速度（如果你要测节点本身）
先在客户端挂上代理，再打开测速地址测，得到的就是“走代理后的速度”。
不挂代理直接测，得到的是“你家宽带到测速节点的速度”，两者别混了。

## 常见问题
- 速度忽高忽低：先看是不是 Railway Free 额度用完了，服务会暂停
- 只能测 TCP：LibreSpeed 是网页测速，不测 UDP，游戏延迟要另外用 ping 测
- 测速结果偏低：VPS 本身只有 1 核 0.5GB，跑满 200Mbps 以上会瓶颈，换大一点的 VPS 再测
