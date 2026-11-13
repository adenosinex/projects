"""mac-compute config: defaults for every task type.

Rule: uploading only the necessary file must be enough to finish.
Every parameter has a default; `params` in the API only overrides.
"""
import os
from pathlib import Path

import auth_store  # noqa: F401  (username+password auth; credentials in auth.json)

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
INPUT_DIR = DATA / "input"
OUTPUT_DIR = DATA / "output"
WORK_DIR = DATA / "work"
for d in (INPUT_DIR, OUTPUT_DIR, WORK_DIR):
    d.mkdir(parents=True, exist_ok=True)

DB_PATH = BASE / "jobs.db"
HOST = os.getenv("MAC_COMPUTE_HOST", "::")  # "::" = dual-stack IPv4+IPv6
PORT = int(os.getenv("MAC_COMPUTE_PORT", "5850"))

# Auth: username + password, stored as a salted PBKDF2 hash in
# auth.json (mode 600) by auth_store.py. The same file is shared
# with the router status dashboard. First visit shows a setup
# form; there is no password in this file and no access code.
AUTH_COOKIE = "mc_auth"

# HTTPS: primary cert is Let's Encrypt for cp.zin7.de
# (acme.sh, Cloudflare DNS-01, in certs/cp.zin7.de.*).
# Fallback is the apple.su7.dpdns.org cert (still on disk, also used
# by sub-node :18932 and Localdy :18931), then the Tailscale cert
# for zindemac-mini.tail35e420.ts.net.
# Home broadband has no IPv6, so IPv6 access is via Tailscale:
# Mac Tailscale IPv6 fd7a:115c:a1e0::7201:764a, reachable only
# from devices on the same tailnet.
# Correction (2026-11-07): the Mac DOES have a public IPv6 on en0
# (2409:8a60:c025:888c:487:a53d:71d0:d40f, China Mobile), kept current
# by my_web_center's dns_extension DDNS: apple.su7.dpdns.org (AAAA)
# -> that address. Inbound IPv6 to the Mac works (firewall off).
# cp.zin7.de AAAA is kept current by ddns-zin7.sh (Cloudflare API).
CP_CERT_FILE = BASE / "certs" / "cp.zin7.de.fullchain.crt"
CP_KEY_FILE = BASE / "certs" / "cp.zin7.de.key"
APPLE_CERT_FILE = BASE / "certs" / "apple.su7.dpdns.org.fullchain.crt"
APPLE_KEY_FILE = BASE / "certs" / "apple.su7.dpdns.org.key"
CERT_FILE = BASE / "certs" / "zindemac-mini.tail35e420.ts.net.crt"
KEY_FILE = BASE / "certs" / "zindemac-mini.tail35e420.ts.net.key"
HTTPS_URL = "https://cp.zin7.de:5850"

# Queue: heavy tasks serial, light tasks up to 4 parallel.
HEAVY_TYPES = {"asr", "asr_batch", "video_transcode", "video_thumbs", "face_cluster", "novel_index"}
MAX_LIGHT_PARALLEL = 4
MAX_RETRIES = 2
# Experiment 2026-11-07 VERDICT: 2-way parallel gave NO gain (two 830s jobs
# took 103s+104s each, 108s wall vs 55s+55s=110s serial) — reverted to 1.
# MLX streams serialize on the Apple GPU; batching would help, separate
# instances do not. See MEMORY.md.
ASR_MAX_CONCURRENT = 1

# --- ASR (Qwen3-ASR via mlx-audio) ---
ASR_MODELS = {
    "daily": "mlx-community/Qwen3-ASR-0.6B-8bit",
    "accurate": "mlx-community/Qwen3-ASR-1.7B-8bit",
}
ASR_DEFAULTS = {
    "model": "daily",
    "language": "auto",
    "chunk_seconds": 45.0,
    "chunk_max_factor": 1.33,   # hard cap 60s
    "chunk_min_factor": 0.6,    # earliest cut 27s
    "short_tail_seconds": 10.0,
    "max_tokens": 2048,
}

# --- Video transcode ---
# Default target 1080p. If source height <= 1080, keep ORIGINAL
# resolution (never upscale). Bitrate by target height.
VIDEO_DEFAULTS = {
    "scale": "1080p",          # 1080p / 720p / 480p / original
    "vcodec": "hevc",          # hevc / h264 (VideoToolbox)
    "fps": None,               # None = keep original, max 30
    "audio_bitrate": "128k",
    "format": "mp4",
}
VIDEO_BITRATE = {1080: "5000k", 720: "2500k", 480: "1200k", 360: "800k"}

# --- Image select (screenshots) ---
IMAGE_SELECT_DEFAULTS = {
    "top_n": 10,
    "sharpness_min": 20.0,     # Laplacian variance; below = blurry, drop.
    # Screenshots are often mostly white (brightness ~250) with little
    # contrast, so the photo-style 30-220 / contrast 15 limits are wrong
    # here. Only drop truly black / blown-out frames.
    "brightness_min": 10.0,
    "brightness_max": 250.0,
    "contrast_min": 5.0,
    "face": False,             # +10 points if a face is found
    "dedup": True,             # perceptual-hash dedup, keep best of group
}
VIDEO_THUMBS_DEFAULTS = {**IMAGE_SELECT_DEFAULTS, "fps": 1.0}

# --- Image batch ---
IMAGE_BATCH_DEFAULTS = {
    "format": "keep",          # keep / webp / jpg
    "quality": 85,             # JPG 85, WebP 80 when format=webp
    "max_size": 1920,          # long edge max, keep aspect
    "dedup": True,
}

# --- OCR ---
OCR_DEFAULTS = {
    "mode": "fast",            # fast = RapidOCR; vl = PaddleOCR-VL-1.6; long = Unlimited OCR 2
    "lang": "ch_en",
    "format": "txt",           # txt / json
    "conf_min": 0.5,
}

# --- Face cluster ---
FACE_DEFAULTS = {"eps": 0.4, "min_samples": 1}

# --- Data CSV ---
DATA_DEFAULTS = {"dedup": True}

# --- VPS probe (direct from this Mac, never via proxy) ---
# No file input: the target comes from params.
VPS_PROBE_DEFAULTS = {
    "target": "",               # required: IP, host, or http(s) URL
    "ping_count": 4,            # ICMP pings
    "timeout": 10,              # seconds, per request
    "download_bytes": 10000000,  # nominal test size (10MB)
    "download_url": None,       # optional explicit file URL for the speed test
}
