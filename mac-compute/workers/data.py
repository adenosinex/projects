"""Data CSV worker: polars, streaming, dedup + clean."""
from pathlib import Path

import polars as pl


def run_data_csv(src, out_dir, params, progress_cb):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_cb(0.1, "reading CSV")
    df = pl.scan_csv(src, infer_schema_length=10000).collect(streaming=True)
    rows_before = df.height
    if params.get("dedup", True):
        df = df.unique(maintain_order=True)
    df = df.drop_nulls()
    out_path = out_dir / "cleaned.csv"
    df.write_csv(out_path)
    progress_cb(1.0, "done")
    return {
        "rows_before": rows_before,
        "rows_after": df.height,
        "columns": df.columns,
        "output_file": out_path.name,
        "files": ["cleaned.csv"],
    }
