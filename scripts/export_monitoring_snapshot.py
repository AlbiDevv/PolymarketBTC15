from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_settings
from lab import LabStatsService


def main():
    parser = argparse.ArgumentParser(description="Export nightly monitoring JSON for Grafana/ops")
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args()

    settings = load_settings(args.config)
    stats = LabStatsService(settings.database.url, settings.bankroll.initial, mode="shadow_maker")
    out_dir = Path(settings.research.holdouts.monitoring_export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "overview": stats.overview(),
        "learning": stats.latest_learning_artifact(),
        "verdict": stats.verdict(),
        "motifs": stats.motifs(20),
        "metrics_text": stats.prometheus_metrics(),
    }
    out_path = out_dir / "shadow_lab_snapshot.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(out_path))


if __name__ == "__main__":
    main()
