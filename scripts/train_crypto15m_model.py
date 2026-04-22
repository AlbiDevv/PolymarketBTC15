from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_settings
from historical import read_dataset
from research.crypto15m import train_crypto15m_model


def main():
    parser = argparse.ArgumentParser(description="Train Crypto15m tradable-EV model")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    settings = load_settings(args.config)
    dataset = read_dataset(settings.crypto_data.out_dir, "crypto15m_candidates")
    verdict = train_crypto15m_model(
        dataset,
        artifact_dir=Path(settings.strategy.crypto15m_model.artifact_path).parent,
        min_net_ev=settings.strategy.crypto15m_model.min_net_ev,
        min_confidence=settings.strategy.crypto15m_model.min_confidence,
        max_entry_price=settings.lab.crypto15m.max_entry_price,
        min_abs_return_zscore_15m=settings.lab.crypto15m.min_abs_return_zscore_15m,
        min_trend_consistency_15m=settings.lab.crypto15m.min_trend_consistency_15m,
        min_rows=3000,
        min_coverage_days=settings.historical.price_window.min_coverage_days,
        holdout_window_days=settings.research.holdouts.window_days,
        holdout_windows_count=settings.research.holdouts.windows_count,
        min_selected_accuracy=settings.research.holdouts.min_high_conf_accuracy,
        min_rows_per_holdout=settings.research.holdouts.min_rows_per_holdout,
        min_high_conf_count_per_holdout=settings.research.holdouts.min_high_conf_count_per_holdout,
    )
    print(verdict)


if __name__ == "__main__":
    main()
