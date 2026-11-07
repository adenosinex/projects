"""Dispatcher: route a job type to its worker function."""
from pathlib import Path

from config import (
    ASR_DEFAULTS, DATA_DEFAULTS, IMAGE_BATCH_DEFAULTS,
    IMAGE_SELECT_DEFAULTS, OCR_DEFAULTS, VIDEO_DEFAULTS,
    VIDEO_THUMBS_DEFAULTS,
)


def run_job(job_type, src, out_dir, params, progress_cb):
    if job_type == "asr":
        from workers.asr import run_asr
        return run_asr(src, out_dir, {**ASR_DEFAULTS, **params}, progress_cb)
    if job_type == "asr_batch":
        # src is a list of input paths for batch jobs.
        from workers.asr import run_asr_batch
        return run_asr_batch(src, out_dir, {**ASR_DEFAULTS, **params}, progress_cb)
    if job_type == "video_transcode":
        from workers.video import run_transcode
        return run_transcode(src, out_dir, {**VIDEO_DEFAULTS, **params}, progress_cb)
    if job_type == "video_thumbs":
        from workers.video import run_video_thumbs
        return run_video_thumbs(src, out_dir, {**VIDEO_THUMBS_DEFAULTS, **params}, progress_cb)
    if job_type == "image_select":
        from workers.images import run_image_select
        return run_image_select(src, out_dir, {**IMAGE_SELECT_DEFAULTS, **params}, progress_cb)
    if job_type == "image_batch":
        from workers.images import run_image_batch
        return run_image_batch(src, out_dir, {**IMAGE_BATCH_DEFAULTS, **params}, progress_cb)
    if job_type == "ocr":
        from workers.ocr import run_ocr
        return run_ocr(src, out_dir, {**OCR_DEFAULTS, **params}, progress_cb)
    if job_type == "data_csv":
        from workers.data import run_data_csv
        return run_data_csv(src, out_dir, {**DATA_DEFAULTS, **params}, progress_cb)
    if job_type == "face_cluster":
        raise RuntimeError("face_cluster is not implemented in this build yet")
    if job_type == "novel_index":
        raise RuntimeError("novel_index is not implemented in this build yet")
    raise RuntimeError(f"unknown job type: {job_type}")


JOB_TYPES = ["asr", "asr_batch", "video_transcode", "video_thumbs", "image_select",
             "image_batch", "ocr", "face_cluster", "data_csv", "novel_index"]
