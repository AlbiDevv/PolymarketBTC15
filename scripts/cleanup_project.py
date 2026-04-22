from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROTECTED_NAMES = {".env", ".env.example", "prediction_trader.db"}
PROTECTED_PREFIXES = {
    PROJECT_ROOT / "data" / "crypto_ohlcv",
    PROJECT_ROOT / "data" / "polymarket_historical",
    PROJECT_ROOT / "research" / "artifacts",
}
CACHE_DIR_NAMES = {"__pycache__", ".pytest_cache", ".pytest-tmp", ".tmp"}
ARCHIVE_FILE_PATTERNS = [
    "shadow_lab_smoke*.db",
    "shadow_lab_debug.db",
    "prediction_trader_e2e.db*",
    "prediction_trader_smoke.db",
]
ARCHIVE_LOG_PATTERNS = [
    "shadow_lab*.log",
    "trader_e2e.log",
]


def _is_protected(path: Path) -> bool:
    resolved = path.resolve()
    if resolved.name in PROTECTED_NAMES:
        return True
    parts = resolved.parts
    if "data" in parts:
        idx = parts.index("data")
        if len(parts) > idx + 1 and parts[idx + 1] in {"crypto_ohlcv", "polymarket_historical"}:
            return True
    if "research" in parts:
        idx = parts.index("research")
        if len(parts) > idx + 1 and parts[idx + 1] == "artifacts":
            return True
    for prefix in PROTECTED_PREFIXES:
        try:
            resolved.relative_to(prefix.resolve())
            return True
        except ValueError:
            continue
    return False


def collect_cleanup_candidates(root: Path = PROJECT_ROOT) -> dict[str, list[str]]:
    delete_paths: list[Path] = []
    archive_paths: list[Path] = []

    for path in root.rglob("*"):
        if _is_protected(path):
            continue
        if path.is_dir() and path.name in CACHE_DIR_NAMES:
            delete_paths.append(path)
        elif path.is_file() and path.suffix == ".pyc":
            delete_paths.append(path)

    for pattern in ARCHIVE_FILE_PATTERNS:
        archive_paths.extend(path for path in root.glob(pattern) if path.is_file() and not _is_protected(path))
    logs_dir = root / "logs"
    if logs_dir.exists():
        for pattern in ARCHIVE_LOG_PATTERNS:
            archive_paths.extend(path for path in logs_dir.glob(pattern) if path.is_file() and not _is_protected(path))

    delete_unique = sorted({path.resolve() for path in delete_paths}, key=lambda p: str(p))
    archive_unique = sorted({path.resolve() for path in archive_paths}, key=lambda p: str(p))
    return {
        "delete": [str(path) for path in delete_unique],
        "archive": [str(path) for path in archive_unique],
    }


def apply_cleanup(candidates: dict[str, list[str]], root: Path = PROJECT_ROOT) -> dict[str, list[str]]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    archive_dir = root / "archive" / f"cleanup_{timestamp}"
    deleted: list[str] = []
    archived: list[str] = []

    for raw in candidates["delete"]:
        path = Path(raw)
        if _is_protected(path) or not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        deleted.append(str(path))

    for raw in candidates["archive"]:
        path = Path(raw)
        if _is_protected(path) or not path.exists():
            continue
        relative = path.resolve().relative_to(root.resolve())
        target = archive_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        archived.append(str(path))

    return {"deleted": deleted, "archived": archived, "archive_dir": [str(archive_dir)] if archived else []}


def main():
    parser = argparse.ArgumentParser(description="Safe project cleanup for generated cache and old smoke artifacts")
    parser.add_argument("--apply", action="store_true", help="Apply cleanup. Without this flag only prints dry-run candidates.")
    parser.add_argument("--dry-run", action="store_true", help="Print candidates without changing files. This is the default.")
    args = parser.parse_args()

    candidates = collect_cleanup_candidates(PROJECT_ROOT)
    result = {"mode": "apply" if args.apply else "dry_run", "candidates": candidates}
    if args.apply:
        result["applied"] = apply_cleanup(candidates, PROJECT_ROOT)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
