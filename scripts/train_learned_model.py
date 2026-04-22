from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_settings
from db.models import ResearchModelArtifactRow, ResearchMotifRow
from db.session import get_session, init_db
from research.motif_learning import (
    build_learning_frame,
    evaluate_holdout,
    extract_motifs,
    frame_readiness,
    publish_artifact,
    split_holdouts,
    train_model,
)


def main():
    parser = argparse.ArgumentParser(description="Train Polymarket-only learned scorer and publish artifact")
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args()

    settings = load_settings(args.config)
    init_db(settings.database.url)
    artifact_root = Path(settings.strategy.learned_model.artifact_path).parent
    artifact_root.mkdir(parents=True, exist_ok=True)

    frame = build_learning_frame(
        settings.historical.sync.out_dir,
        candidate_stride_sec=settings.strategy.learned_model.candidate_stride_sec,
    )
    if frame.empty:
        verdict = {
            "accepted": False,
            "reason": "historical_lake_empty",
            "mean_high_conf_net_ev": 0.0,
            "mean_high_conf_accuracy": 0.0,
            "coverage_days": 0,
            "markets_used": 0,
        }
        (artifact_root / "latest_verdict.json").write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps({"status": "not_enough_history", "verdict": verdict}, ensure_ascii=False, indent=2))
        raise SystemExit("historical lake is empty; sync data first")

    readiness = frame_readiness(
        frame,
        min_markets_required=settings.historical.price_window.min_markets_required,
        min_coverage_days=settings.historical.price_window.min_coverage_days,
    )
    if not readiness["ready"]:
        verdict = {
            "accepted": False,
            "reason": "not_enough_history",
            "details": readiness["reason"],
            "mean_high_conf_net_ev": 0.0,
            "mean_high_conf_accuracy": 0.0,
            "coverage_days": readiness["coverage_days"],
            "markets_used": readiness["markets_used"],
        }
        (artifact_root / "latest_verdict.json").write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps({"status": "not_enough_history", "verdict": verdict}, ensure_ascii=False, indent=2))
        if settings.historical.price_window.fail_on_insufficient_history:
            raise SystemExit("not_enough_history")
        return

    train, holdouts = split_holdouts(
        frame,
        window_days=settings.research.holdouts.window_days,
        windows_count=settings.research.holdouts.windows_count,
    )
    if train.empty or not holdouts:
        raise SystemExit("not enough data for chronological holdouts")

    model, priors = train_model(train)
    holdout_reports = [
        evaluate_holdout(
            model,
            holdout,
            high_conf_threshold=settings.strategy.learned_model.min_candidate_confidence,
            fee_rate=settings.strategy.fee_rate,
            min_candidate_net_ev=settings.strategy.learned_model.min_candidate_net_ev,
            max_candidate_entry_price=settings.strategy.learned_model.max_candidate_entry_price,
            estimated_slippage=settings.strategy.learned_model.estimated_slippage,
        )
        for holdout in holdouts
    ]
    motifs = extract_motifs(frame, holdout_reports)
    artifact = publish_artifact(
        out_dir=artifact_root,
        model=model,
        category_priors=priors,
        holdouts=holdout_reports,
        motifs=motifs,
        high_conf_threshold=settings.strategy.learned_model.min_candidate_confidence,
        fee_rate=settings.strategy.fee_rate,
        min_high_conf_accuracy=settings.research.holdouts.min_high_conf_accuracy,
        max_calibration_error=settings.research.holdouts.max_calibration_error,
        min_rows_per_holdout=settings.research.holdouts.min_rows_per_holdout,
        min_high_conf_count_per_holdout=settings.research.holdouts.min_high_conf_count_per_holdout,
        readiness=readiness,
        min_candidate_net_ev=settings.strategy.learned_model.min_candidate_net_ev,
        max_candidate_entry_price=settings.strategy.learned_model.max_candidate_entry_price,
        estimated_slippage=settings.strategy.learned_model.estimated_slippage,
    )

    session = get_session(settings.database.url)
    try:
        session.query(ResearchModelArtifactRow).update({"enabled": False})
        row = ResearchModelArtifactRow(
            artifact_key=artifact.artifact_key,
            model_type="logistic_regression",
            artifact_path=str(artifact.model_path),
            manifest_path=str(artifact.manifest_path),
            metrics_json=artifact.metrics,
            holdout_summary_json=artifact.holdouts,
            accepted=artifact.accepted,
            enabled=artifact.accepted,
            high_conf_accuracy=float(artifact.metrics.get("mean_high_conf_accuracy") or 0.0),
            high_conf_net_ev=float(artifact.metrics.get("mean_high_conf_net_ev") or 0.0),
            calibration_error=float(artifact.metrics.get("mean_calibration_error") or 0.0),
            training_fresh_until=datetime.fromisoformat(json.loads(Path(artifact.manifest_path).read_text(encoding="utf-8"))["training_fresh_until"]),
        )
        session.add(row)
        for motif in artifact.motifs:
            session.add(ResearchMotifRow(
                artifact_key=artifact.artifact_key,
                motif_key=motif["motif_key"],
                feature_signature=motif["feature_signature"],
                pre_event_window=motif["pre_event_window"],
                time_lag_sec=motif["time_lag_sec"],
                sample_size=motif["sample_size"],
                hit_rate=motif["hit_rate"],
                expected_value=motif["expected_value"],
                confidence_score=motif["confidence_score"],
                holdout_metrics=motif["holdout_metrics"],
            ))
        session.commit()
    finally:
        session.close()

    print(json.dumps({
        "artifact_key": artifact.artifact_key,
        "accepted": artifact.accepted,
        "metrics": artifact.metrics,
        "holdouts": artifact.holdouts,
        "motifs": len(artifact.motifs),
        "verdict": artifact.verdict,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
