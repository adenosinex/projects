#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MrBeast 音频批量下载 — YouTube + Bilibili 官方号
================================================
打开直接能跑（先看下面“使用方法”）。

依赖：Python 3.9+，yt-dlp（pip install yt-dlp）
    pip install -U yt-dlp

使用方法（国内 / 国外都在下面，改顶部 CONFIG 即可）：
    1. 国内：先确认能打开 YouTube（代理），Bilibili 直连即可。
    2. 国外：YouTube 直连，Bilibili 大部分直连，个别视频可能限中国大陆。
    3. 直接运行：python download_mrbeast_audio.py
       只下 YouTube：python download_mrbeast_audio.py --source youtube
       只下 Bilibili：python download_mrbeast_audio.py --source bilibili
       先试 3 个：  python download_mrbeast_audio.py --limit 3
       断点续跑：   直接再运行一次本脚本即可，已下载的自动跳过。

去重逻辑（三重，缺一不可）：
    1. 输出目录里已有 <ID>.m4a / .opus / .mp3 / .webm → 跳过；
    2. downloaded.log 里有该 ID → 跳过；
    3. yt-dlp 自带 --no-overwrites --continue，断点续传不重复下载。

Cookie（默认不需要，见下面说明）：
    - cookies.txt 为空 / 只有注释行 → 不传 cookie，直接匿名下载。
    - 需要时：把浏览器导出的 Netscape 格式 cookie 保存为同目录的
      cookies.txt（本文件已建好一个空模板，照里面的格式填），
      脚本会自动检测并加上 --cookies cookies.txt。
    - 也可以用浏览器直接导出：yt-dlp --cookies-from-browser chrome
      （需先关闭 Chrome），但 cookies.txt 更稳妥、可复制到别的机器。

什么时候才需要 Cookie？
    国内：
      Bilibili：一般不需要。音频轨道（66k/100k/138k）不需大会员、
        匿名即可；只有遇到 412 / 567、或视频限登录时才需要。
      YouTube：必须先有代理（见 PROXY），代理通了一般不需要登录；
        只有 YouTube 弹出 "Sign in to confirm you're not a bot"
        或 429 时才需要登录过的 YouTube cookie。
    国外：
      YouTube：直连，一般不需要登录；同上，被 bot 验证时才需要。
      Bilibili：大部分直连不需要登录；个别视频提示“仅限中国大陆”
        时，cookie 也救不了，需要回国代理（把 PROXY 设为回国节点）。
"""

import argparse
import os
import subprocess
import sys
import time

# ==================== CONFIG：按你的环境改这里 ====================
# 环境： "cn"（国内） 或 "overseas"（国外）
ENV = "cn"

# 代理（yt-dlp 的 --proxy，http/https/socks5 都行）：
#   国内下 YouTube 必须填，例如 Clash 默认 http://127.0.0.1:7890
#   国外下 YouTube 留空 ""；国外下 Bilibili 限大陆视频时填回国代理。
#   也可以不改代码，设环境变量 YTDLP_PROXY，优先级高于这里。
PROXY = "http://127.0.0.1:7890" if ENV == "cn" else ""

# 下载源： "both" / "youtube" / "bilibili"
SOURCE = "both"

# 音频档位（转文字够用，体积最小优先）：
#   YouTube：bestaudio，yt-dlp 会自动选 m4a/opus，约 128k
#   Bilibili：30216=66k（最省，约 0.5MB/分钟）、30232=100k、30280=138k
BILIBILI_AUDIO_FORMAT = "30216"   # 改 30232 / 30280 可提高音质

# 下载目录（脚本所在目录下）
OUTDIR_YT = "audio_youtube"
OUTDIR_BILI = "audio_bilibili"

# ID 列表文件（同目录；没有时脚本会尝试自动拉取，见 fetch_*）
YOUTUBE_IDS_FILE = "youtube_ids.txt"
BILIBILI_IDS_FILE = "bilibili_bv.txt"

# Cookie 文件（同目录；空文件 = 不用 cookie）
COOKIES_FILE = "cookies.txt"

# 每两个视频之间停几秒，防限速（YouTube 建议 2~5 秒）
SLEEP_SECONDS = 3
# ===================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def has_real_cookies(path):
    """cookies.txt 里有没有真实内容（非空、非注释行）。"""
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                return True
    return False


def load_done_log(outdir):
    """读已下载记录 downloaded.log，返回 set(ID)。"""
    done = set()
    p = os.path.join(outdir, "downloaded.log")
    if os.path.isfile(p):
        with open(p, encoding="utf-8", errors="ignore") as f:
            for line in f:
                done.add(line.strip().split("\t")[0])
    return done


def mark_done(outdir, vid):
    p = os.path.join(outdir, "downloaded.log")
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"{vid}\t{time.strftime('%Y-%m-%d %H:%M:%S')}\n")


def already_on_disk(outdir, vid):
    for ext in ("m4a", "opus", "mp3", "webm", "ogg", "aac"):
        if os.path.isfile(os.path.join(outdir, f"{vid}.{ext}")):
            return True
    return False


def read_ids(path):
    """读 ID 列表：一行一个 ID，支持 'ID' 或 'ID<TAB>标题'，# 开头为注释。"""
    ids = []
    if not os.path.isfile(path):
        return ids
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ids.append(line.split()[0].split("\t")[0])
    # 保序去重
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def fetch_list(cmd, outfile):
    """用 yt-dlp --flat-playlist 拉取频道列表，失败返回 False（不致命）。"""
    log(f"列表文件缺失，尝试自动拉取 -> {outfile}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not os.path.isfile(outfile):
        log("自动拉取失败（可能是网络/风控），请手动把 ID 贴进 " + outfile)
        log("失败信息（后 3 行）：" + "\n".join(r.stderr.strip().splitlines()[-3:]))
        return False
    return True


def fetch_youtube_ids(proxy, cookies):
    """拉取 @mrbeast 长视频 + Shorts 的 ID，去重后写 youtube_ids.txt。"""
    out = os.path.join(BASE_DIR, YOUTUBE_IDS_FILE)
    tmp = os.path.join(BASE_DIR, ".yt_flat.json")
    ids = []
    for channel in ("https://www.youtube.com/@mrbeast/videos",
                    "https://www.youtube.com/@mrbeast/shorts"):
        cmd = [sys.executable, "-m", "yt_dlp", "--no-check-certificate",
               "--flat-playlist", "--no-warnings", "-J", channel]
        if proxy:
            cmd += ["--proxy", proxy]
        if cookies:
            cmd += ["--cookies", cookies]
        cmd += ["-o", tmp]
        # yt-dlp -J 直接输出 JSON 到 stdout，这里改用 shell 重定向更稳：
        # 为简单起见，直接跑 -J 并解析 stdout
        cmd = cmd[:-2]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            log(f"拉取 {channel} 失败，跳过")
            continue
        try:
            import json
            data = json.loads(r.stdout)
            for e in data.get("entries", []):
                if e and e.get("id"):
                    ids.append(e["id"])
        except Exception as e:
            log(f"解析 {channel} 失败：{e}")
    ids = list(dict.fromkeys(ids))
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(ids) + "\n")
    log(f"YouTube 列表已写入 {out}，共 {len(ids)} 个")
    return ids


def fetch_bilibili_ids(proxy, cookies):
    """拉取 Bilibili 官方号（UID 1027737427）投稿列表，写 bilibili_bv.txt。"""
    out = os.path.join(BASE_DIR, BILIBILI_IDS_FILE)
    cmd = [sys.executable, "-m", "yt_dlp", "--no-check-certificate",
           "--flat-playlist", "--no-warnings",
           "--print", "%(id)s\t%(title)s",
           "https://space.bilibili.com/1027737427/video"]
    if proxy:
        cmd += ["--proxy", proxy]
    if cookies:
        cmd += ["--cookies", cookies]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        log("拉取 Bilibili 列表失败，请手动把 BV 号贴进 " + out)
        return []
    lines = [l for l in r.stdout.splitlines() if l.startswith("BV")]
    lines = list(dict.fromkeys(lines))
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log(f"Bilibili 列表已写入 {out}，共 {len(lines)} 个")
    return [l.split("\t")[0] for l in lines]


def download_one(url, outdir, vid, fmt, proxy, cookies):
    """下载单个音频，成功返回 True。"""
    os.makedirs(outdir, exist_ok=True)
    cmd = [sys.executable, "-m", "yt_dlp",
           "--no-check-certificate",
           "-f", fmt,
           "--no-playlist",
           "--retries", "10",
           "--fragment-retries", "10",
           "--continue",
           "--no-overwrites",
           "--sleep-interval", str(SLEEP_SECONDS),
           "--max-sleep-interval", str(SLEEP_SECONDS + 3),
           "-o", os.path.join(outdir, "%(id)s.%(ext)s"),
           url]
    if proxy:
        cmd += ["--proxy", proxy]
    if cookies:
        cmd += ["--cookies", cookies]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode == 0 and already_on_disk(outdir, vid):
        return True
    # yt-dlp 有时返回 0 但文件是别的扩展名，再兜底检查一次
    if already_on_disk(outdir, vid):
        return True
    log("  失败（后 5 行）：" + "\n".join((r.stderr or r.stdout).strip().splitlines()[-5:]))
    return False


def run_source(name, ids, outdir, fmt, url_tpl, proxy, cookies, limit):
    os.makedirs(outdir, exist_ok=True)
    done = load_done_log(outdir)
    total, ok, skip, fail = len(ids), 0, 0, 0
    log(f"=== {name}：共 {total} 个，已记录 {len(done)} 个 ===")
    for i, vid in enumerate(ids, 1):
        if limit and i > limit:
            break
        if vid in done or already_on_disk(outdir, vid):
            skip += 1
            continue
        log(f"[{i}/{total}] {vid} ...")
        if download_one(url_tpl.format(vid=vid), outdir, vid, fmt, proxy, cookies):
            mark_done(outdir, vid)
            done.add(vid)
            ok += 1
        else:
            fail += 1
            log(f"  ^ {vid} 这次失败，已跳过，继续下一个（下次重跑会重试它）")
    log(f"=== {name} 完成：成功 {ok}，跳过 {skip}，失败 {fail} ===")
    return ok, skip, fail


def main():
    ap = argparse.ArgumentParser(description="MrBeast 音频批量下载")
    ap.add_argument("--source", choices=["both", "youtube", "bilibili"],
                    default=SOURCE)
    ap.add_argument("--env", choices=["cn", "overseas"], default=ENV,
                    help="cn=国内（YouTube 需代理），overseas=国外（直连）")
    ap.add_argument("--proxy", default=None,
                    help="代理地址，如 http://127.0.0.1:7890，优先于 CONFIG")
    ap.add_argument("--limit", type=int, default=0, help="只下前 N 个（测试用）")
    ap.add_argument("--cookies", default=None,
                    help="cookie 文件路径，默认用同目录 cookies.txt（空则不用）")
    args = ap.parse_args()

    # 代理：命令行 > 环境变量 > CONFIG（国内默认 127.0.0.1:7890）
    proxy = args.proxy or os.environ.get("YTDLP_PROXY") or ""
    if not proxy and args.env == "cn":
        proxy = PROXY
    cookies_path = args.cookies or os.path.join(BASE_DIR, COOKIES_FILE)
    use_cookies = cookies_path if has_real_cookies(cookies_path) else None

    log(f"环境={args.env}  代理={'已设置' if proxy else '无'}  "
        f"Cookie={'使用 ' + use_cookies if use_cookies else '不使用（匿名）'}")
    if args.env == "cn" and not proxy and args.source in ("both", "youtube"):
        log("警告：国内环境没有代理，YouTube 会打不开。")
        log("  请把 CONFIG 里的 PROXY 改成你的代理，")
        log("  如 Clash：http://127.0.0.1:7890，或用 --proxy http://127.0.0.1:7890")
        log("  Bilibili 部分不受影响，会继续跑。")

    # 检查 yt-dlp
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log("未安装 yt-dlp，先运行：pip install -U yt-dlp")
        sys.exit(1)
    log(f"yt-dlp 版本：{r.stdout.strip()}")

    if args.source in ("both", "youtube"):
        ids = read_ids(os.path.join(BASE_DIR, YOUTUBE_IDS_FILE))
        if not ids:
            ids = fetch_youtube_ids(proxy, use_cookies)
        if ids:
            run_source("YouTube", ids, os.path.join(BASE_DIR, OUTDIR_YT),
                       "bestaudio[ext=m4a]/bestaudio",
                       "https://www.youtube.com/watch?v={vid}",
                       proxy, use_cookies, args.limit)
        else:
            log("YouTube 列表为空，跳过")

    if args.source in ("both", "bilibili"):
        ids = read_ids(os.path.join(BASE_DIR, BILIBILI_IDS_FILE))
        if not ids:
            ids = fetch_bilibili_ids(proxy, use_cookies)
        if ids:
            run_source("Bilibili", ids, os.path.join(BASE_DIR, OUTDIR_BILI),
                       BILIBILI_AUDIO_FORMAT,
                       "https://www.bilibili.com/video/{vid}",
                       proxy, use_cookies, args.limit)
        else:
            log("Bilibili 列表为空，跳过")

    log("全部完成。再运行一次本脚本会自动跳过已下载的，只补失败的。")


if __name__ == "__main__":
    main()
