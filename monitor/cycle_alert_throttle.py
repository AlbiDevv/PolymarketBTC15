"""Dedup / cooldown for repeated cycle failure Telegram alerts (in-process)."""

from __future__ import annotations

import time


class CycleAlertThrottle:
    """First failure emits immediately; identical errors in a window aggregate; recovery clears."""

    COOLDOWN_SEC = 300.0

    def __init__(self) -> None:
        self._in_cycle_failure = False
        self._last_key: str | None = None
        self._repeat = 0
        self._window_start = 0.0

    def on_failure(self, err: str) -> list[str]:
        now = time.time()
        key = err.strip()[:500]
        out: list[str] = []
        if not self._in_cycle_failure:
            self._in_cycle_failure = True
            self._last_key = key
            self._repeat = 1
            self._window_start = now
            out.append(f"Cycle failed: {err[:450]}")
            return out
        same = key == self._last_key
        in_win = (now - self._window_start) < self.COOLDOWN_SEC
        if same and in_win:
            self._repeat += 1
            if self._repeat == 10:
                out.append(
                    "Cycle error repeated 10x within 5m (same signature). "
                    f"Preview: {err[:180]}"
                )
            elif self._repeat > 10 and self._repeat % 10 == 0:
                out.append(f"Cycle error repeated {self._repeat}x within 5m window.")
            return out
        self._last_key = key
        self._repeat = 1
        self._window_start = now
        out.append(f"Cycle failed: {err[:450]}")
        return out

    def on_success(self) -> list[str]:
        if not self._in_cycle_failure:
            return []
        self._in_cycle_failure = False
        self._last_key = None
        self._repeat = 0
        return ["Trading cycle recovered (OK)."]
