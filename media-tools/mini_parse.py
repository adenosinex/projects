#!/usr/bin/env python3
"""
抖音 + B站 最小核心提取：链接 -> 最低画质视频文件
只依赖 requests，无其他依赖。源自 jiuhunwl/short_videos 的核心逻辑，
去掉了聚合入口、图集/实况/音乐/原画/多画质，只留最低画质。

用法：
  pip install requests
  python mini_parse.py "https://v.douyin.com/xxxx/"
  python mini_parse.py "https://www.bilibili.com/video/BV1xxxxxxxxxx/"
  python mini_parse.py "https://b23.tv/xxxx"   # B站短链也行
"""
import re, sys, json
import requests

UA_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
UA_MOBILE = "Mozilla/5.0 (Linux; Android 13; 2210132C Build/TKQ1.220829.002) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36"

def extract_url(text):
    m = re.search(r"https?://[^\s\"'<>]+", text)
    return m.group(0).rstrip("'\"，。；,.;") if m else None

def download(url, filename, referer):
    # 直链常有防盗链，带上 Referer + 浏览器 UA
    r = requests.get(url, headers={"User-Agent": UA_DESKTOP, "Referer": referer},
                     stream=True, timeout=60)
    r.raise_for_status()
    with open(filename, "wb") as f:
        for chunk in r.iter_content(1024 * 256):
            if chunk:
                f.write(chunk)
    print(f"已保存: {filename} ({r.headers.get('Content-Length', '?')} bytes)")
    return filename

# ---------------- 抖音：最低画质 ----------------
def douyin_lowest(share_text):
    url = extract_url(share_text)
    if not url:
        raise ValueError("没找到链接")
    s = requests.Session()
    # 1. 短链跟随重定向，拿到真实地址
    real = s.get(url, headers={"User-Agent": UA_DESKTOP}, timeout=15).url
    # 2. 从地址里提 aweme_id：路径末段或 modal_id/vid 参数
    m = re.search(r"modal_id=(\d+)", real) or re.search(r"video/(\d+)", real)
    aweme_id = m.group(1) if m else real.rstrip("/").split("/")[-1].split(".")[0]
    if not re.fullmatch(r"[1-9]\d{15,19}", aweme_id):
        raise ValueError(f"提取 aweme_id 失败: {real}")

    # 3. 调免 Cookie 详情接口
    api = "https://api.amemv.com/aweme/v1/aweme/detail/"
    r = s.get(api, params={"aweme_id": aweme_id, "aid": "6383",
                           "device_platform": "web", "version_code": "190500",
                           "version_name": "19.5.0"},
              headers={"User-Agent": UA_MOBILE, "Accept": "application/json"},
              timeout=20)
    data = r.json()
    detail = data.get("aweme_detail") or {}
    video = detail.get("video") or {}

    # 4. 最低画质：bit_rate 按码率升序，取第一档
    #    每档结构：{"bit_rate": xxx, "play_addr": {"url_list": [...]}}
    rates = video.get("bit_rate") or video.get("bitRateList") or []
    play_url = None
    if rates:
        rates = sorted(rates, key=lambda x: x.get("bit_rate") or x.get("bitRate") or 0)
        play_url = (rates[0].get("play_addr") or {}).get("url_list", [None])[0]
    if not play_url:  # 兜底：直接用 play_addr
        play_url = (video.get("play_addr") or {}).get("url_list", [None])[0]
    if not play_url:
        raise ValueError(f"详情接口没返回播放地址: {json.dumps(data)[:500]}")

    # 关键：playwm 是带水印地址，换成 play 就是无水印
    play_url = play_url.replace("playwm", "play")
    title = detail.get("desc", "")[:30] or aweme_id
    print(f"抖音 {aweme_id} 最低档: {play_url}")
    return play_url, f"douyin_{aweme_id}.mp4", "https://www.douyin.com/"

# ---------------- B站：最低画质 ----------------
def bilibili_lowest(share_text):
    url = extract_url(share_text)
    if not url:
        raise ValueError("没找到链接")
    s = requests.Session()
    # 1. b23.tv 短链跟随重定向，提 BV 号
    real = s.get(url, headers={"User-Agent": UA_DESKTOP}, timeout=15).url
    m = re.search(r"BV[0-9A-Za-z]{10}", real)
    if not m:
        raise ValueError(f"提取 BV 号失败: {real}")
    bvid = m.group(0)

    # 2. view 接口拿 cid（只取第 1 个分 P）
    view = s.get("https://api.bilibili.com/x/web-interface/view",
                 params={"bvid": bvid},
                 headers={"User-Agent": UA_DESKTOP}, timeout=15).json()
    if view.get("code") != 0:
        raise ValueError(f"view 接口失败: {view}")
    cid = view["data"]["pages"][0]["cid"]
    title = view["data"].get("title", bvid)

    # 3. playurl 接口拿直链：qn=16 就是 360P 最低档
    #    fnval=0 返回 durl（单个 mp4），fnval=16/1 会返回 dash（音画分离），最低画质用 0 最省事
    play = s.get("https://api.bilibili.com/x/player/playurl",
                 params={"otype": "json", "fnver": "0", "fnval": "0",
                         "player": "3", "qn": "16", "bvid": bvid, "cid": cid,
                         "platform": "html5", "high_quality": "0"},
                 headers={"User-Agent": UA_DESKTOP}, timeout=15).json()
    durl = (play.get("data") or {}).get("durl") or []
    if durl:
        play_url = durl[0]["url"]
    else:  # 兜底：dash 格式，取第一条 video
        dash = (play.get("data") or {}).get("dash") or {}
        videos = dash.get("video") or []
        if not videos:
            raise ValueError(f"playurl 接口没返回地址: {json.dumps(play)[:500]}")
        play_url = videos[0]["base_url"]
    print(f"B站 {bvid}《{title}》cid={cid} 最低档 360P: {play_url}")
    return play_url, f"bilibili_{bvid}.mp4", f"https://www.bilibili.com/video/{bvid}/"

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    text = sys.argv[1]
    if "douyin" in text or "v.douyin.com" in text:
        url, name, ref = douyin_lowest(text)
    elif "bilibili" in text or "b23.tv" in text:
        url, name, ref = bilibili_lowest(text)
    else:
        # 按域名兜底：含 BV 号按 B站，否则按抖音
        url, name, ref = bilibili_lowest(text) if "BV" in text else douyin_lowest(text)
    download(url, name, ref)

if __name__ == "__main__":
    main()
