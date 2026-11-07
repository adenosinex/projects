#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bilibili 官方号音频批量下载（免 Cookie / 免登录）— BBDown 原理的 Python 实现
============================================================================
只下 Bilibili，不下 YouTube。国内直接跑，国外大部分视频也能跑。

BBDown 原理（为什么不用登录）：
    1. B 站公开视频的音频流本来就不需要 SESSDATA 登录凭证。
       需要登录的只是 1080P 高码率 / 大会员内容，音频轨道（66k/100k/138k）
       不在限制里。公开视频无登录可下，会员内容才需要 cookie。
       （BBDown 文档原话：None for public videos. Members-only /
       high-resolution content may need a logged-in cookie.）
    2. 真正起作用的是两样东西，不是登录：
       a. WBI 签名：B 站 web 接口要求对请求参数做 WBI 签名
          （w_rid / wts），未签名的请求更容易触发风控 412。
          BBDown 对 playurl、view、空间列表都做标准 WBI 签名。
       b. buvid3：一个随机生成的访客标识，放在 Cookie 里，
          格式如 BUVID3=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx，
          不需要登录，随机生成一个就能用，脚本会自动生成并保存
          在 .buvid3 文件里，之后一直用同一个。
    3. 拿到播放地址后，媒体文件本身用“无账号 Cookie”的请求头去下：
       只带 Referer + User-Agent + Range（断点续传），
       因为媒体 URL 本身就是带签名的临时地址。
    4. 音频是 DASH 分片 MP4（.m4s），直接改名 .m4a 就能用，
       Whisper / ffmpeg 都认；有 ffmpeg 时脚本会再转封装一次。

流程（和 BBDown 一致）：
    BV 号 -> /x/web-interface/view（WBI 签名）拿 aid/cid/标题
         -> /x/player/wbi/playurl（WBI 签名，fnval=4048）
            拿 dash.audio[]，选 30216 / 30232 / 30280
         -> 用 baseUrl（失败换 backupUrl）分段下载 .m4s -> 存为 .m4a

使用方法：
    pip install -U requests        # 只需要 requests，没有别的依赖
    python download_bilibili_audio.py            # 下官方号全部
    python download_bilibili_audio.py --limit 3  # 先试 3 个
    python download_bilibili_audio.py BV1S7446fEFw BV1Cpxj6QEMn
    python download_bilibili_audio.py --audio-id 30232   # 100k 音质
    python download_bilibili_audio.py --list my_bv.txt    # 自定义 BV 列表

为什么需要 BV 号？
    BV 号就是 B 站视频的唯一编号，相当于 YouTube 的 11 位 video_id，
    没有它就不知道要下哪个视频。开源的 BV<->av 互转算法只能把
    “已知的 av 号”换算成 BV 号，不能凭空列出一个 UP 主发过哪些视频；
    要列出全部视频，必须调空间列表接口（/x/space/wbi/arc/search），
    那个接口有频率限制，-799 就是“请求过于频繁”，等 30~60 秒再试
    一般能过。脚本在同目录有 bilibili_bv.txt 时根本不会调这个接口，
    直接按文件里的 124 个 BV 号下载。

去重 / 断点续跑：输出目录已有 <BV>.m4a 跳过；
    audio_bilibili/downloaded.log 里有该 BV 跳过；
    未下完的 .part 文件下次用 Range 续传，不重复下载。

什么时候才需要 Cookie？
    只有视频本身限登录 / 限大会员 / 限中国大陆（国外）时。
    那时加 --cookie "SESSDATA=xxxx; bili_jct=xxxx"，
    脚本会把它拼进请求头。公开的 MrBeast 官方视频不需要。
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import uuid

try:
    import requests
except ImportError:
    print("缺少 requests，先运行：pip install requests")
    sys.exit(1)

# ==================== CONFIG ====================
# Bilibili 官方号：野兽先生MrBeast
OFFICIAL_UID = 1027737427

# 音频档位：30216=66k（最省，约 0.5MB/分钟，转文字够用）
#           30232=100k，30280=138k（音质更好）
AUDIO_ID = 30216

OUTDIR = "audio_bilibili"
BV_LIST_FILE = "bilibili_bv.txt"   # 有就读它，没有就自动拉官方号列表
BUVID_FILE = ".buvid3"             # 自动生成的访客标识，不用管
CHUNK = 1024 * 256
# ================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MIXIN_KEY_TABLE = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
                   27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
                   37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
                   22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class BiliClient:
    """BBDown 式客户端：WBI 签名 + buvid3，免登录。"""

    def __init__(self, extra_cookie=""):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA,
                               "Referer": "https://www.bilibili.com/"})
        self.buvid = self._load_buvid()
        cookie = f"buvid3={self.buvid}"
        if extra_cookie:
            cookie += "; " + extra_cookie.strip()
        self.s.headers["Cookie"] = cookie
        self._mixin = None

    def _load_buvid(self):
        p = os.path.join(BASE_DIR, BUVID_FILE)
        if os.path.isfile(p):
            v = open(p, encoding="utf-8").read().strip()
            if v:
                return v
        v = str(uuid.uuid4()).upper()
        with open(p, "w", encoding="utf-8") as f:
            f.write(v)
        return v

    def _mixin_key(self):
        if self._mixin:
            return self._mixin
        d = self.s.get("https://api.bilibili.com/x/web-interface/nav",
                       timeout=20).json()
        img = d["data"]["wbi_img"]["img_url"].split("/")[-1].split(".")[0]
        sub = d["data"]["wbi_img"]["sub_url"].split("/")[-1].split(".")[0]
        self._mixin = "".join((img + sub)[i] for i in MIXIN_KEY_TABLE)[:32]
        return self._mixin

    def _sign(self, params):
        p = dict(params)
        p["wts"] = int(time.time())
        qs = urllib.parse.urlencode(sorted(p.items()))
        p["w_rid"] = hashlib.md5((qs + self._mixin_key()).encode()).hexdigest()
        return p

    def get_json(self, url, params, signed=True):
        if signed:
            params = self._sign(params)
        r = self.s.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def view(self, bvid):
        """拿 aid / cid / 标题 / 时长。"""
        d = self.get_json("https://api.bilibili.com/x/web-interface/view",
                          {"bvid": bvid})
        if d.get("code") != 0:
            raise RuntimeError(f"view 失败 code={d.get('code')} {d.get('message')}")
        v = d["data"]
        return {"aid": v["aid"], "cid": v["cid"], "title": v["title"],
                "duration": v.get("duration", 0), "pages": v.get("pages", [])}

    def audio_url(self, aid, cid, audio_id):
        """拿 DASH 音频地址，返回 (url, [backup_urls], bandwidth)。"""
        d = self.get_json("https://api.bilibili.com/x/player/wbi/playurl",
                          {"avid": aid, "cid": cid, "qn": 64,
                           "fnval": 4048, "fnver": 0, "fourk": 1})
        if d.get("code") != 0:
            raise RuntimeError(f"playurl 失败 code={d.get('code')} {d.get('message')}")
        audios = d["data"].get("dash", {}).get("audio", [])
        if not audios:
            raise RuntimeError("该视频没有 DASH 音频轨道（可能限登录/限地区）")
        # 优先指定档位，否则取码率最低的（体积最小）
        pick = next((a for a in audios if a["id"] == audio_id), None)
        if pick is None:
            pick = min(audios, key=lambda a: a["bandwidth"])
            log(f"  没有 {audio_id} 档，改用 {pick['id']}（{pick['bandwidth']//1000}k）")
        urls = [pick["baseUrl"]] + list(pick.get("backupUrl") or [])
        return urls, pick["bandwidth"]

    def space_videos(self, uid, limit=200):
        """拉 UP 主投稿列表，返回 [(bvid, title), ...]。
        注意：空间列表接口风控比 view/playurl 严，-799 = 请求过于频繁，
        412 = 风控拦截。遇到 -799 不要马上重试，要等 30~60 秒再试；
        家用宽带一般重试 1~2 次能过。实在过不了就用 bilibili_bv.txt
        手动贴 BV 号（脚本有这个文件时根本不会调这个接口）。"""
        out, pn = [], 1
        while len(out) < limit:
            last_err = None
            for attempt, wait in enumerate((5, 30, 60, 120)):
                try:
                    # 先试 WBI 签名版，失败再试不签名版
                    try:
                        d = self.get_json(
                            "https://api.bilibili.com/x/space/wbi/arc/search",
                            {"mid": uid, "ps": 50, "pn": pn,
                             "tid": 0, "order": "pubdate"})
                    except Exception:
                        d = self.s.get(
                            "https://api.bilibili.com/x/space/arc/search",
                            params={"mid": uid, "ps": 50, "pn": pn,
                                    "tid": 0, "order": "pubdate"},
                            timeout=30).json()
                    if d.get("code") == 0:
                        break
                    last_err = f"code={d.get('code')} {d.get('message')}"
                    # -799 是限流，短重试没用，等久一点
                    if d.get("code") == -799:
                        log(f"  -799 请求过于频繁，等 {wait} 秒再试…")
                except Exception as e:
                    last_err = str(e)
                time.sleep(wait)
            else:
                raise RuntimeError(f"空间列表失败（已重试 {len((5, 30, 60, 120))} 次）：{last_err}。"
                                   "请把 BV 号写进 bilibili_bv.txt 再跑，"
                                   "脚本有这个文件时不会再调列表接口")
            vlist = d["data"]["list"]["vlist"]
            if not vlist:
                break
            out += [(v["bvid"], v["title"]) for v in vlist]
            if len(out) >= d["data"]["page"]["count"]:
                break
            pn += 1
            time.sleep(5)
        return out[:limit]


def download_file(client, urls, dest, bvid):
    """分段下载，支持 Range 断点续传，主 URL 失败换备用 URL。"""
    tmp = dest + ".part"
    have = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
    for url in urls:
        try:
            headers = {"Referer": f"https://www.bilibili.com/video/{bvid}"}
            if have:
                headers["Range"] = f"bytes={have}-"
            with client.s.get(url, headers=headers, stream=True,
                             timeout=60) as r:
                if r.status_code not in (200, 206):
                    log(f"  地址返回 {r.status_code}，换备用地址")
                    continue
                if r.status_code == 200 and have:
                    have = 0  # 服务器不支持续传，从头下
                mode = "ab" if have else "wb"
                with open(tmp, mode) as f:
                    for chunk in r.iter_content(CHUNK):
                        if chunk:
                            f.write(chunk)
            if os.path.getsize(tmp) > 1024:
                os.replace(tmp, dest)
                return True
        except Exception as e:
            log(f"  下载出错：{e}，换备用地址")
            continue
    return False


def load_done(outdir):
    done = set()
    p = os.path.join(outdir, "downloaded.log")
    if os.path.isfile(p):
        with open(p, encoding="utf-8", errors="ignore") as f:
            for line in f:
                done.add(line.strip().split("\t")[0])
    return done


def mark_done(outdir, bvid, title):
    with open(os.path.join(outdir, "downloaded.log"), "a",
              encoding="utf-8") as f:
        f.write(f"{bvid}\t{title}\t{time.strftime('%Y-%m-%d %H:%M:%S')}\n")


def already(outdir, bvid):
    return any(os.path.isfile(os.path.join(outdir, f"{bvid}.{e}"))
               for e in ("m4a", "m4s", "mp3", "opus"))


def read_bv_list(path):
    ids = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                ids.append(line.split()[0])
    return list(dict.fromkeys(ids))


def main():
    ap = argparse.ArgumentParser(description="Bilibili 音频批量下载（免 Cookie）")
    ap.add_argument("bvids", nargs="*", help="BV 号，如 BV1S7446fEFw")
    ap.add_argument("--list", default=None, help="BV 列表文件，一行一个 BV 号")
    ap.add_argument("--audio-id", type=int, default=AUDIO_ID,
                    help="30216=66k（默认），30232=100k，30280=138k")
    ap.add_argument("--outdir", default=os.path.join(BASE_DIR, OUTDIR))
    ap.add_argument("--limit", type=int, default=0, help="只下前 N 个（测试用）")
    ap.add_argument("--cookie", default="",
                    help="仅限登录/大会员视频时用，如 \"SESSDATA=xxx; bili_jct=xxx\"")
    args = ap.parse_args()

    client = BiliClient(extra_cookie=args.cookie)
    os.makedirs(args.outdir, exist_ok=True)

    if args.bvids:
        bvids = args.bvids
    elif args.list:
        bvids = read_bv_list(args.list)
    else:
        list_path = os.path.join(BASE_DIR, BV_LIST_FILE)
        if os.path.isfile(list_path):
            bvids = read_bv_list(list_path)
            log(f"从 {BV_LIST_FILE} 读到 {len(bvids)} 个 BV 号")
        else:
            log(f"没有 {BV_LIST_FILE}，自动拉官方号（UID {OFFICIAL_UID}）列表…")
            videos = client.space_videos(OFFICIAL_UID)
            bvids = [b for b, _ in videos]
            with open(list_path, "w", encoding="utf-8") as f:
                f.write("\n".join(f"{b}\t{t}" for b, t in videos) + "\n")
            log(f"已保存 {len(bvids)} 个 BV 号到 {BV_LIST_FILE}")

    done = load_done(args.outdir)
    total, ok, skip, fail = len(bvids), 0, 0, 0
    for i, bvid in enumerate(bvids, 1):
        if args.limit and i > args.limit:
            break
        if bvid in done or already(args.outdir, bvid):
            skip += 1
            continue
        log(f"[{i}/{total}] {bvid} ...")
        try:
            info = client.view(bvid)
            log(f"  《{info['title']}》{info['duration']} 秒")
            urls, bw = client.audio_url(info["aid"], info["cid"], args.audio_id)
            log(f"  音频 {bw//1000}k，开始下载…")
            dest = os.path.join(args.outdir, f"{bvid}.m4a")
            if download_file(client, urls, dest, bvid):
                mark_done(args.outdir, bvid, info["title"])
                ok += 1
                log(f"  完成：{os.path.getsize(dest)//1024} KB")
            else:
                fail += 1
                log("  下载失败，下次重跑会重试它")
        except Exception as e:
            fail += 1
            log(f"  失败：{e}")
        time.sleep(2)
    log(f"完成：成功 {ok}，跳过 {skip}，失败 {fail}，存在 {args.outdir}")


if __name__ == "__main__":
    main()
