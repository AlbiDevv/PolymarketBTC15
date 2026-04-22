from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_settings
from historical import read_dataset


def _coverage_days(frame: pd.DataFrame, column: str) -> int:
    if frame.empty or column not in frame.columns:
        return 0
    values = pd.to_datetime(frame[column], utc=True, format="mixed", errors="coerce").dropna()
    if values.empty:
        return 0
    return max(0, int((values.max() - values.min()).days))


def main() -> None:
    parser = argparse.ArgumentParser(description="Report Crypto15m training readiness")
    parser.add_argument("--config", default="config/settings.crypto15m.yaml")
    args = parser.parse_args()
    settings = load_settings(args.config)

    resolutions = read_dataset(settings.historical.price_window.out_dir, "crypto15m_resolutions")
    price_windows = read_dataset(settings.historical.price_window.out_dir, "crypto15m_price_windows")
    candidates = read_dataset(settings.crypto_data.out_dir, "crypto15m_candidates")
    manifest_path = Path(settings.strategy.crypto15m_model.artifact_path)
    verdict_path = manifest_path.with_name("latest_verdict.json")
    verdict = {}
    if verdict_path.exists():
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))

    resolutions_unique = resolutions.drop_duplicates("market_id") if not resolutions.empty and "market_id" in resolutions.columns else resolutions
    price_unique = price_windows.drop_duplicates("market_id") if not price_windows.empty and "market_id" in price_windows.columns else price_windows
    candidate_unique = candidates.drop_duplicates("market_id") if not candidates.empty and "market_id" in candidates.columns else candidates

    readiness = {
        "history_days_target": settings.crypto_data.history_days,
        "resolution_markets": int(len(resolutions_unique)),
        "resolution_coverage_days": _coverage_days(resolutions_unique, "settled_at"),
        "price_window_markets": int(len(price_unique)),
        "price_window_rows": int(len(price_windows)),
        "candidate_markets": int(len(candidate_unique)),
        "candidate_rows": int(len(candidates)),
        "artifact_accepted": bool(verdict.get("accepted", False)),
        "artifact_reason": verdict.get("reason", "missing_verdict"),
        "artifact_rows": verdict.get("rows", 0),
        "artifact_markets_used": verdict.get("markets_used", 0),
        "artifact_coverage_days": verdict.get("coverage_days", 0),
        "ready_for_backtest": bool(verdict.get("accepted", False)),
    }
    print(json.dumps(readiness, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
