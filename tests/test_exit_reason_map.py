import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.exit_reason_map import map_exit_reason_from_audit


def test_maps_stop_take_time():
    assert map_exit_reason_from_audit("STOP-LOSS (x)") == "stop_loss"
    assert map_exit_reason_from_audit("TAKE-PROFIT (x)") == "take_profit"
    assert map_exit_reason_from_audit("TIME-EXIT (open") == "time_exit"


def test_unknown_when_empty():
    assert map_exit_reason_from_audit("") == "unknown"
    assert map_exit_reason_from_audit(None) == "unknown"
