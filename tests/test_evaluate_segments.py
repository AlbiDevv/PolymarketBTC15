import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research.evaluate import augment_p_market_source, segment_metrics_block


def test_augment_from_quality_flags():
    df = pd.DataFrame({
        "side": ["YES", "NO"],
        "quality_flags_json": ['[]', '["fallback_complement_no_book"]'],
    })
    out = augment_p_market_source(df)
    assert list(out["p_market_source"]) == ["native_yes", "complement_fallback"]


def test_segment_block_keys():
    df = pd.DataFrame({
        "resolved_outcome_for_side": [1, 0, 1],
        "p_market": [0.5, 0.5, 0.5],
        "p_market_source": ["native_yes", "native_no", "complement_fallback"],
        "decision_ts": pd.to_datetime(
            ["2024-01-01", "2024-01-02", "2024-01-03"], utc=True
        ),
    })
    blk = segment_metrics_block(df, None, None)
    assert set(blk.keys()) == {"clean_native", "complement_fallback", "all_rows"}
    assert blk["clean_native"]["n"] == 2
    assert blk["complement_fallback"]["n"] == 1
