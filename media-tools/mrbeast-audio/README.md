# MrBeast 音频批量下载

你自己转文字用：只下音频（不下视频），YouTube + Bilibili 官方号两边都下。

## 先回答：要不要登录 Cookie？

| 环境 | YouTube | Bilibili 官方号 |
|---|---|---|
| **国内** | **必须有代理**（否则打不开），代理通了一般**不需要登录**；只有被 bot 验证（429 / "Sign in to confirm you're not a bot"）时才需要登录过的 YouTube cookie | **不需要登录、不需要代理**，音频档不需大会员，匿名就能下；只有遇到 412 / 567 或限登录时才需要 Bilibili cookie |
| **国外** | **直连，不需要代理、不需要登录**；被 bot 验证时才需要 cookie | 大部分直连、**不需要登录**；个别视频“仅限中国大陆”时 cookie 也救不了，要回国代理 |

一句话：**Cookie 是备用，不是必需。先匿名跑，报错了再填 `cookies.txt`。**

## 文件说明

| 文件 | 说明 |
|---|---|
| `download_mrbeast_audio.py` | 主脚本，打开直接跑 |
| `youtube_ids.txt` | YouTube 视频 ID 列表（1011 个，`@mrbeast/videos` + `@mrbeast/shorts`） |
| `bilibili_bv.txt` | 不在包里：这台服务器拉 B 站列表被 412/352 拒了；脚本在你电脑上第一次跑 Bilibili 时会自动拉取官方号（UID 1027737427）的 124 个 BV 号并保存为这个文件 |
| `cookies.txt` | 空模板，见上面表格，需要时再填 |
| `README.md` | 本文件 |

## 国内怎么跑

```bash
pip install -U yt-dlp
# 1. 打开你的代理（Clash / v2ray 等），记下本地代理地址，默认是 http://127.0.0.1:7890
# 2. 改脚本顶部 CONFIG：ENV = "cn"，PROXY = "http://127.0.0.1:7890"
#    （代理地址不一样就改成你的；也可以不改代码，用环境变量 YTDLP_PROXY）
# 3. 运行
python download_mrbeast_audio.py
```

只试 3 个先验证通不通：

```bash
python download_mrbeast_audio.py --limit 3
```

## 国外怎么跑

```bash
pip install -U yt-dlp
# 改脚本顶部 CONFIG：ENV = "overseas"，PROXY = ""
python download_mrbeast_audio.py
```

国外下 Bilibili 遇到“仅限中国大陆”的视频：把 `PROXY` 改成回国代理地址再跑，
或跳过它（脚本会自动跳过失败的，下次再试）。

## 去重 / 断点续跑

三重去重，不会重复下载：

1. 输出目录里已有 `<ID>.m4a` 等文件 → 跳过；
2. `audio_youtube/downloaded.log` / `audio_bilibili/downloaded.log` 里有该 ID → 跳过；
3. yt-dlp `--continue --no-overwrites`，断网续传。

中断后直接再运行一次 `python download_mrbeast_audio.py`，只补没下完的。

## 体积估算（66k 档，转文字够用）

- YouTube 全量 281.5 小时：约 7.9 GB
- 其中 ≥100 万播放的 205.8 小时：约 5.8 GB
- Bilibili 官方 124 个（约 40 小时）：约 1.1 GB

想音质更好，把脚本里 `BILIBILI_AUDIO_FORMAT` 从 `30216`（66k）改成
`30232`（100k）或 `30280`（138k），YouTube 会自动选最佳音频。

## 常见报错

| 报错 | 意思 / 怎么办 |
|---|---|
| YouTube 429 / "Sign in to confirm you're not a bot" | 被 bot 验证：填 `cookies.txt`（登录过的 YouTube），或换个代理 IP，等一会儿再跑 |
| Bilibili 412 / 567 | B 站风控：填 `cookies.txt`（登录过的 Bilibili），或换个网络再跑；云服务器 IP 最容易触发，家用宽带一般没事 |
| `SSL: CERTIFICATE_VERIFY_FAILED` | 公司 / 校园网中间人代理：脚本已加 `--no-check-certificate`，仍报错就检查代理设置 |
| `yt-dlp: command not found` | 先 `pip install -U yt-dlp`，再用 `python -m yt_dlp --version` 验证 |

## 注意

- Bilibili 音频也是英文原声，和 YouTube 同一个，中文只是烧在画面上的字幕，
  所以 B 站音频转出来还是英文，不会多一份中文文本。
- 两个源同一个视频只算一次，Bilibili 124 个基本都是 YouTube 已有视频的中文搬运版。
