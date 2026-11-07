"""Video transcode + thumbnails.

Default: 1080p. If source is smaller, keep ORIGINAL resolution
(never upscale). HEVC via VideoToolbox, AAC audio, MP4 faststart.
"""
import subprocess
from pathlib import Path

from config import VIDEO_BITRATE, VIDEO_DEFAULTS
from workers.common import probe_video, score_image, target_height
import cv2


def run_transcode(src, out_dir, params, progress_cb):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    info = probe_video(src)
    src_h = info["height"] or 1080
    scale = params.get("scale", VIDEO_DEFAULTS["scale"])
    tgt_h = target_height(src_h, scale)
    vcodec = params.get("vcodec", VIDEO_DEFAULTS["vcodec"])
    encoder = "hevc_videotoolbox" if vcodec == "hevc" else "h264_videotoolbox"
    bitrate = VIDEO_BITRATE.get(tgt_h, "2500k")
    # Allow explicit bitrate override.
    bitrate = params.get("bitrate", bitrate)
    fps = params.get("fps", VIDEO_DEFAULTS["fps"])
    out_path = out_dir / f"transcoded_{tgt_h}p.mp4"

    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(src)]
    if tgt_h != src_h:
        cmd += ["-vf", f"scale=-2:{tgt_h}"]
    if fps:
        cmd += ["-r", str(min(float(fps), 30))]
    cmd += ["-c:v", encoder, "-b:v", bitrate,
            "-c:a", "aac", "-b:a", VIDEO_DEFAULTS["audio_bitrate"],
            "-movflags", "+faststart", str(out_path)]
    progress_cb(0.1, f"transcoding to {tgt_h}p {vcodec} {bitrate}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
    if r.returncode or not out_path.exists():
        raise RuntimeError((r.stderr or "ffmpeg transcode failed")[-500:])
    progress_cb(1.0, "done")
    return {
        "source": info,
        "target_height": tgt_h,
        "target_codec": vcodec,
        "bitrate": bitrate,
        "upscaled": False,
        "output_file": out_path.name,
        "output_bytes": out_path.stat().st_size,
        "input_bytes": Path(src).stat().st_size,
    }


def run_video_thumbs(src, out_dir, params, progress_cb):
    """Extract frames at fps, score them, keep top_n."""
    from config import VIDEO_THUMBS_DEFAULTS
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fps = float(params.get("fps", VIDEO_THUMBS_DEFAULTS["fps"]))
    top_n = int(params.get("top_n", VIDEO_THUMBS_DEFAULTS["top_n"]))
    face = bool(params.get("face", VIDEO_THUMBS_DEFAULTS["face"]))
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src), "-vf", f"fps={fps}", str(frames_dir / "f_%04d.jpg")],
        capture_output=True, text=True, timeout=3600,
    )
    if r.returncode:
        raise RuntimeError((r.stderr or "ffmpeg frame extraction failed")[-500:])
    files = sorted(frames_dir.glob("*.jpg"))
    scored = []
    for i, f in enumerate(files):
        img = cv2.imread(str(f))
        if img is None:
            continue
        score, details = score_image(img, face=face)
        details["file"] = f.name
        scored.append((score, details, f))
        if i % 20 == 0:
            progress_cb(0.1 + 0.8 * (i / max(1, len(files))), f"scoring {i}/{len(files)}")
    # Drop blurry frames only; brightness / contrast are scoring
    # components, not gates (white screenshots are valid).
    from workers.common import phash_hex, hamming_hex
    kept = []
    for score, details, f in sorted(scored, key=lambda x: -x[0]):
        if details["sharpness"] < float(params.get("sharpness_min", VIDEO_THUMBS_DEFAULTS["sharpness_min"])):
            continue
        img = cv2.imread(str(f))
        h = phash_hex(img)
        if any(hamming_hex(h, k[0]) < 5 for k in kept):
            continue
        kept.append((h, score, details, f))
        if len(kept) >= top_n:
            break
    best_dir = out_dir / "best"
    best_dir.mkdir(exist_ok=True)
    rows = []
    for i, (_, score, details, f) in enumerate(kept, 1):
        dest = best_dir / f"best_{i:03d}.jpg"
        dest.write_bytes(f.read_bytes())
        rows.append({**details, "rank": i, "output": dest.name})
    progress_cb(1.0, "done")
    return {"frames_extracted": len(files), "kept": len(rows),
            "scores": rows, "files": [f"best/best_{i:03d}.jpg" for i in range(1, len(rows) + 1)]}
