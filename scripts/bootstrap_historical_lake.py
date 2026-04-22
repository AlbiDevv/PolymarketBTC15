from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_settings
from historical import append_manifest, write_partitioned_parquet


def _load_frame(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() == ".json":
        return pd.read_json(path)
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported input format: {path}")


def _dataset_name(frame: pd.DataFrame) -> str:
    cols = {str(col) for col in frame.columns}
    if {"market_id", "outcome"} <= cols or {"question", "yes_token_id"} <= cols:
        return "resolutions"
    if {"event_id", "title"} <= cols or {"slug", "category"} <= cols:
        return "events"
    if {"timestamp", "price", "token_id"} <= cols:
        return "price_windows"
    return "bootstrap"


def main():
    parser = argparse.ArgumentParser(description="Bootstrap Polymarket historical parquet lake from local snapshots")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--input", action="append", default=[])
    args = parser.parse_args()

    settings = load_settings(args.config)
    source_paths = list(args.input or settings.historical.bootstrap.source_paths or [])
    if not source_paths:
        raise SystemExit("No bootstrap inputs supplied")

    out_dir = settings.historical.bootstrap.out_dir
    manifest_items: list[dict] = []
    for source in source_paths:
        frame = _load_frame(Path(source))
        if frame.empty:
            continue
        dataset = _dataset_name(frame)
        rows = frame.to_dict(orient="records")
        written = write_partitioned_parquet(out_dir, dataset, rows, partition_key="date", filename_prefix=dataset)
        manifest_items.append({
            "source_path": source,
            "dataset": dataset,
            "rows": len(rows),
            "written_files": [str(path) for path in written],
        })

    append_manifest(out_dir, "bootstrap", {
        "source_paths": source_paths,
        "items": manifest_items,
        "rows": sum(item["rows"] for item in manifest_items),
    })
    print(f"bootstrap rows={sum(item['rows'] for item in manifest_items)} items={len(manifest_items)}")


if __name__ == "__main__":
    main()
