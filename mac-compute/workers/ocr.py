"""OCR worker: RapidOCR-ONNX by default (fast, CPU, Chinese+English).
Modes: fast (RapidOCR), vl (PaddleOCR-VL-1.6, not yet installed),
long (Unlimited OCR 2, long docs, test first)."""
import json
import subprocess
from pathlib import Path


def _pdf_to_images(pdf_path, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(exist_ok=True)
    # PyMuPDF if available, else pdf via ffmpeg? Try fitz.
    try:
        import fitz
        doc = fitz.open(pdf_path)
        paths = []
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=200)
            p = out_dir / f"page_{i+1:04d}.png"
            pix.save(p)
            paths.append(p)
        return paths
    except Exception:
        # Fallback: use `sips` on macOS to render? No. Raise.
        raise RuntimeError("PDF needs PyMuPDF (pip install PyMuPDF) to render pages")


def run_ocr(src, out_dir, params, progress_cb):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mode = params.get("mode", "fast")
    if mode != "fast":
        raise RuntimeError(
            f"OCR mode '{mode}' is not installed yet in this build. "
            "Use mode=fast (RapidOCR). vl/long need a separate test run first."
        )
    src = Path(src)
    if src.suffix.lower() == ".pdf":
        images = _pdf_to_images(src, out_dir / "pages")
    elif src.suffix.lower() == ".zip":
        import zipfile
        zdir = out_dir / "unzipped"
        zdir.mkdir(exist_ok=True)
        with zipfile.ZipFile(src) as z:
            z.extractall(zdir)
        images = sorted(p for p in zdir.rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    elif src.is_dir():
        images = sorted(p for p in src.rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    else:
        images = [src]

    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR()
    all_text, details = [], []
    for i, img in enumerate(images):
        result, _ = engine(str(img))
        lines = []
        if result:
            for box, text, conf in result:
                if float(conf) < float(params.get("conf_min", 0.5)):
                    continue
                lines.append(text)
                details.append({"file": img.name, "text": text, "conf": round(float(conf), 3)})
        all_text.append("\n".join(lines))
        progress_cb(0.1 + 0.85 * ((i + 1) / max(1, len(images))), f"OCR {i+1}/{len(images)}")
    (out_dir / "ocr.txt").write_text("\n\n".join(all_text), encoding="utf-8")
    (out_dir / "ocr.json").write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    progress_cb(1.0, "done")
    return {"pages": len(images), "lines": len(details),
            "char_count": sum(len(t) for t in all_text),
            "files": ["ocr.txt", "ocr.json"]}
