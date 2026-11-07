"""Shared helpers: video probing, image scoring."""
import json
import subprocess

import cv2
import numpy as np


def probe_video(path):
    """Return (width, height, fps, duration, vcodec) via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    data = json.loads(out.stdout or "{}")
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            fps = s.get("avg_frame_rate", "0/1")
            try:
                num, den = fps.split("/")
                fps_f = float(num) / float(den) if float(den) else 0.0
            except Exception:
                fps_f = 0.0
            return {
                "width": int(s.get("width", 0)),
                "height": int(s.get("height", 0)),
                "fps": fps_f,
                "duration": float(data.get("format", {}).get("duration", 0) or 0),
                "vcodec": s.get("codec_name", ""),
            }
    return {"width": 0, "height": 0, "fps": 0.0, "duration": 0.0, "vcodec": ""}


def target_height(source_height, scale_param):
    """Default 1080p, but never upscale: if source is smaller,
    keep the original resolution."""
    if scale_param in (None, "", "original"):
        return source_height
    want = int(str(scale_param).replace("p", ""))
    if source_height and source_height < want:
        return source_height
    return want


def score_image(img_bgr, face=False):
    """Score one image 0-100. Returns (score, details)."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    sharpness = float(lap.var())
    brightness = float(np.mean(gray))
    contrast = float(np.std(gray))
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    colourfulness = float(np.mean(hsv[:, :, 1]))
    # Normalised sub-scores, each 0-100.
    s_sharp = min(100.0, sharpness / 3.0)
    s_bright = 100.0 - min(100.0, abs(brightness - 125) / 1.25)
    s_contrast = min(100.0, contrast / 0.6)
    s_colour = min(100.0, colourfulness / 1.0)
    score = 0.4 * s_sharp + 0.2 * s_bright + 0.2 * s_contrast + 0.2 * s_colour
    has_face = False
    if face:
        try:
            cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            )
            faces = cascade.detectMultiScale(gray, 1.1, 4)
            has_face = len(faces) > 0
            if has_face:
                score = min(100.0, score + 10)
        except Exception:
            pass
    return score, {
        "sharpness": round(sharpness, 1),
        "brightness": round(brightness, 1),
        "contrast": round(contrast, 1),
        "colourfulness": round(colourfulness, 1),
        "face": bool(has_face),
        "score": round(score, 1),
    }


def phash_hex(img_bgr):
    """Simple average hash, 16 hex chars, for dedup."""
    small = cv2.resize(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY), (8, 8))
    avg = float(np.mean(small))
    bits = (small > avg).flatten()
    h = 0
    for b in bits:
        h = (h << 1) | int(b)
    return f"{h:016x}"


def hamming_hex(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")
