"""Image select (many screenshots) + image batch compress."""
import io
import zipfile
from pathlib import Path

import cv2
from PIL import Image

from config import IMAGE_BATCH_DEFAULTS, IMAGE_SELECT_DEFAULTS
from workers.common import hamming_hex, phash_hex, score_image


def _iter_images(src):
    """Yield (name, path-or-bytes) for a file, a zip, or a directory."""
    src = Path(src)
    if src.is_dir():
        for p in sorted(src.rglob("*")):
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
                yield p.name, p
        return
    if src.suffix.lower() == ".zip":
        with zipfile.ZipFile(src) as z:
            for name in z.namelist():
                if name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp")):
                    yield Path(name).name, z.read(name)
        return
    yield src.name, src


def _read_cv2(item):
    name, data = item
    if isinstance(data, (bytes, bytearray)):
        arr = cv2.imdecode(__import__("numpy").frombuffer(data, __import__("numpy").uint8), cv2.IMREAD_COLOR)
        return arr
    return cv2.imread(str(data))


def run_image_select(src, out_dir, params, progress_cb):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    top_n = int(params.get("top_n", IMAGE_SELECT_DEFAULTS["top_n"]))
    face = bool(params.get("face", IMAGE_SELECT_DEFAULTS["face"]))
    sharp_min = float(params.get("sharpness_min", IMAGE_SELECT_DEFAULTS["sharpness_min"]))
    b_min = float(params.get("brightness_min", IMAGE_SELECT_DEFAULTS["brightness_min"]))
    b_max = float(params.get("brightness_max", IMAGE_SELECT_DEFAULTS["brightness_max"]))
    items = list(_iter_images(src))
    scored = []
    for i, item in enumerate(items):
        img = _read_cv2(item)
        if img is None:
            continue
        score, details = score_image(img, face=face)
        details["file"] = item[0]
        scored.append((score, details, img, item[0]))
        if i % 20 == 0:
            progress_cb(0.1 + 0.7 * (i / max(1, len(items))), f"scoring {i}/{len(items)}")
    kept, dropped = [], []
    for score, details, img, name in sorted(scored, key=lambda x: -x[0]):
        # Hard gate: only sharpness. Brightness / contrast are scoring
        # components, not gates — a mostly-white screenshot has
        # brightness ~255 and contrast ~4 and is still a valid shot.
        if details["sharpness"] < sharp_min:
            dropped.append({**details, "reason": "blurry"})
            continue
        h = phash_hex(img)
        if params.get("dedup", IMAGE_SELECT_DEFAULTS["dedup"]) and any(
            hamming_hex(h, k[0]) < 5 for k in kept
        ):
            dropped.append({**details, "reason": "duplicate"})
            continue
        kept.append((h, score, details, img, name))
        if len(kept) >= top_n:
            break
    best_dir = out_dir / "best"
    best_dir.mkdir(exist_ok=True)
    rows = []
    for i, (_, score, details, img, name) in enumerate(kept, 1):
        dest = best_dir / f"best_{i:03d}.jpg"
        cv2.imwrite(str(dest), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        rows.append({**details, "rank": i, "output": dest.name, "source": name})
    import csv
    with open(out_dir / "scores.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["rank", "file", "score", "sharpness", "brightness", "contrast", "colourfulness", "face", "output", "source"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    progress_cb(1.0, "done")
    return {"total": len(items), "kept": len(rows), "dropped": len(dropped),
            "scores": rows, "files": [f"best/best_{i:03d}.jpg" for i in range(1, len(rows) + 1)] + ["scores.csv"]}


def run_image_batch(src, out_dir, params, progress_cb):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = params.get("format", IMAGE_BATCH_DEFAULTS["format"])
    quality = int(params.get("quality", IMAGE_BATCH_DEFAULTS["quality"]))
    max_size = int(params.get("max_size", IMAGE_BATCH_DEFAULTS["max_size"]))
    items = list(_iter_images(src))
    out_images = out_dir / "images"
    out_images.mkdir(exist_ok=True)
    seen_hashes, rows = set(), []
    for i, item in enumerate(items):
        name, data = item
        try:
            if isinstance(data, (bytes, bytearray)):
                im = Image.open(io.BytesIO(data))
            else:
                im = Image.open(data)
            im = im.convert("RGB")
        except Exception as e:
            rows.append({"file": name, "error": str(e)})
            continue
        # Dedup by 16x16 average hash (256 bits). The old 8x8 hash was
        # too coarse: a blurred copy hashed the same as the sharp original.
        if params.get("dedup", IMAGE_BATCH_DEFAULTS["dedup"]):
            small = im.resize((16, 16)).convert("L")
            px = list(small.getdata())
            avg = sum(px) / len(px)
            h = "".join("1" if p > avg else "0" for p in px)
            if any(sum(a != b for a, b in zip(h, seen)) < 10 for seen in seen_hashes):
                rows.append({"file": name, "status": "duplicate_skipped"})
                continue
            seen_hashes.add(h)
        w, h = im.size
        scale = min(1.0, max_size / max(w, h))
        if scale < 1.0:
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        stem = Path(name).stem
        if fmt == "webp":
            dest = out_images / f"{stem}.webp"
            im.save(dest, "WEBP", quality=80 if quality == 85 else quality)
        elif fmt == "jpg":
            dest = out_images / f"{stem}.jpg"
            im.save(dest, "JPEG", quality=quality)
        else:
            dest = out_images / f"{stem}.jpg"
            im.save(dest, "JPEG", quality=quality)
        rows.append({"file": name, "output": dest.name,
                     "size": dest.stat().st_size, "width": im.size[0], "height": im.size[1]})
        if i % 20 == 0:
            progress_cb(0.1 + 0.8 * (i / max(1, len(items))), f"compressing {i}/{len(items)}")
    # Zip the result.
    zip_path = out_dir / "compressed.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out_images.glob("*")):
            z.write(p, p.name)
    progress_cb(1.0, "done")
    return {"total": len(items), "kept": len([r for r in rows if "output" in r]),
            "rows": rows, "files": ["compressed.zip"]}
