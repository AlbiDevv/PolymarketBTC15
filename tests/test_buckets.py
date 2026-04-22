import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research.buckets import spread_bucket, tte_bucket, tail_bucket


def test_tte_bucket():
    assert tte_bucket(3600) == "0-1d"
    assert tte_bucket(10 * 86400) == "7-30d"


def test_spread():
    assert spread_bucket(0.01) == "tight"


def test_tail():
    assert tail_bucket(0.05) == "low_tail"
    assert tail_bucket(0.95) == "high_tail"
