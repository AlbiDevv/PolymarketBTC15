import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.cycle_alert_throttle import CycleAlertThrottle


def test_first_failure_emits():
    t = CycleAlertThrottle()
    msgs = t.on_failure("boom")
    assert len(msgs) == 1
    assert "boom" in msgs[0]


def test_repeated_same_suppresses_until_10():
    t = CycleAlertThrottle()
    t.on_failure("same")
    for _ in range(8):
        assert t.on_failure("same") == []
    m10 = t.on_failure("same")
    assert len(m10) == 1
    assert "10x" in m10[0]


def test_recovery_after_success():
    t = CycleAlertThrottle()
    t.on_failure("e")
    assert t.on_success() == ["Trading cycle recovered (OK)."]
    assert t.on_success() == []


def test_different_error_resets_window_message():
    t = CycleAlertThrottle()
    t.on_failure("a")
    assert len(t.on_failure("b")) == 1
