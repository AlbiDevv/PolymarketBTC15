from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lake_path(base_dir: str | Path) -> Path:
    path = Path(base_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _coerce_partition_date(value) -> str:
    if value is None or value == "":
        return _utcnow().date().isoformat()
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.date().isoformat()
    rendered = str(value).strip()
    if not rendered:
        return _utcnow().date().isoformat()
    if rendered.endswith("Z"):
        rendered = rendered[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(rendered).date().isoformat()
    except ValueError:
        return rendered[:10]


def write_partitioned_parquet(
    base_dir: str | Path,
    dataset: str,
    rows: Iterable[dict],
    *,
    partition_key: str = "date",
    filename_prefix: str | None = None,
) -> list[Path]:
    records = list(rows)
    if not records:
        return []

    filename_prefix = filename_prefix or dataset
    root = _lake_path(base_dir) / dataset
    root.mkdir(parents=True, exist_ok=True)

    frame = pd.DataFrame.from_records(records)
    if partition_key not in frame.columns:
        frame[partition_key] = _utcnow().date().isoformat()
    frame["_partition_date"] = frame[partition_key].map(_coerce_partition_date)

    written: list[Path] = []
    batch_stamp = _utcnow().strftime("%Y%m%dT%H%M%SZ")
    for partition_date, group in frame.groupby("_partition_date", sort=True):
        target_dir = root / f"date={partition_date}"
        target_dir.mkdir(parents=True, exist_ok=True)
        parquet_path = target_dir / f"{filename_prefix}-{batch_stamp}.parquet"
        out_path = parquet_path
        payload = group.drop(columns=["_partition_date"])
        try:
            payload.to_parquet(parquet_path, index=False)
        except Exception:
            out_path = target_dir / f"{filename_prefix}-{batch_stamp}.jsonl"
            payload.to_json(out_path, orient="records", lines=True, force_ascii=False)
        written.append(out_path)
    return written


def replace_dataset(base_dir: str | Path, dataset: str) -> None:
    base = _lake_path(base_dir).resolve()
    target = (base / dataset).resolve()
    if target == base or base not in target.parents:
        raise RuntimeError(f"Refusing to replace unsafe dataset path: {target}")
    if target.exists():
        shutil.rmtree(target)


def append_manifest(base_dir: str | Path, name: str, payload: dict) -> Path:
    root = _lake_path(base_dir) / "manifests"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.json"
    existing: list[dict] = []
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            try:
                existing = json.load(fh) or []
            except json.JSONDecodeError:
                existing = []
    entry = {"generated_at": _utcnow().isoformat(), **payload}
    existing.append(entry)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, ensure_ascii=False, indent=2)
    return path


def read_dataset(base_dir: str | Path, dataset: str) -> pd.DataFrame:
    root = _lake_path(base_dir) / dataset
    files = sorted(root.glob("date=*/*.parquet"))
    jsonl_files = sorted(root.glob("date=*/*.jsonl"))
    if not files:
        files = []
    frames = [pd.read_parquet(path) for path in files]
    frames.extend([
        pd.read_json(path, orient="records", lines=True, dtype=False)
        for path in jsonl_files
    ])
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
