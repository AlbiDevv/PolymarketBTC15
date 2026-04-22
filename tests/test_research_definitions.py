"""Tests for research definitions: target and p_market helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research.definitions import (
    p_market_fallback_no_from_yes_complement,
    p_market_from_token_mid,
    resolved_outcome_for_side,
)


class TestResolvedOutcomeForSide:
    def test_yes_win(self):
        assert resolved_outcome_for_side("YES", "YES") == 1
        assert resolved_outcome_for_side("YES", "NO") == 0

    def test_no_win(self):
        assert resolved_outcome_for_side("NO", "NO") == 1
        assert resolved_outcome_for_side("NO", "YES") == 0


class TestPMarketNative:
    def test_yes_uses_yes_mid(self):
        assert p_market_from_token_mid(0.6, 0.4, "YES") == 0.6

    def test_no_uses_no_mid(self):
        assert p_market_from_token_mid(0.6, 0.35, "NO") == 0.35

    def test_fallback_complement(self):
        assert abs(p_market_fallback_no_from_yes_complement(0.6) - 0.4) < 1e-9
