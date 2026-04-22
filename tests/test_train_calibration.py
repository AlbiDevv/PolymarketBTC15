import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research.train_calibration import apply_bucket_calibrator, empirical_bucket_calibrate


def test_bucket_roundtrip():
    p = np.array([0.1, 0.5, 0.9])
    y = np.array([0.0, 1.0, 1.0])
    art = empirical_bucket_calibrate(p, y)
    out = apply_bucket_calibrator(p, art)
    assert out.shape == p.shape
    assert np.all((out >= 0.01) & (out <= 0.99))
