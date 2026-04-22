from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from tarfile import open as tar_open

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_settings


def _sqlite_path(db_url: str) -> Path:
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        raise ValueError(f"Only sqlite URLs are supported, got: {db_url}")
    return Path(db_url[len(prefix):]).resolve()


def backup_sqlite(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as source:
        with sqlite3.connect(dst) as target:
            source.backup(target)


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe export bundle for analysis")
    parser.add_argument("--config", default=None)
    parser.add_argument("--output-dir", default="exports")
    parser.add_argument("--include-logs", action="store_true")
    args = parser.parse_args()

    settings = load_settings(args.config)
    db_path = _sqlite_path(settings.database.url)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.output_dir).resolve() / f"analysis_bundle_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    backup_path = out_dir / db_path.name
    backup_sqlite(db_path, backup_path)

    if args.include_logs:
        log_path = Path(settings.logging.file).resolve()
        if log_path.exists():
            shutil.copy2(log_path, out_dir / log_path.name)

    archive_path = out_dir.with_suffix(".tar.gz")
    with tar_open(archive_path, "w:gz") as archive:
        archive.add(out_dir, arcname=out_dir.name)

    print(f"bundle_dir={out_dir}")
    print(f"archive={archive_path}")


if __name__ == "__main__":
    main()
