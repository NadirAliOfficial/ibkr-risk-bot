"""Tests for ManagedPosition and State."""

from src.state import ManagedPosition, State


def _long_pos(**kw):
    defaults = dict(
        conid=1, symbol="AAPL", sec_type="STK",
        exchange="SMART", currency="USD",
        quantity=10, entry_price=100.0,
    )
    defaults.update(kw)
    return ManagedPosition(**defaults)


def _short_pos(**kw):
    return _long_pos(quantity=-10, **kw)


# ── Basic properties ──────────────────────────────────────────────────────

class TestManagedPositionProperties:
    def test_long_properties(self):
        mp = _long_pos()
        assert mp.is_long is True
        assert mp.close_action == "SELL"
        assert mp.abs_qty == 10

    def test_short_properties(self):
        mp = _short_pos()
        assert mp.is_long is False
        assert mp.close_action == "BUY"
        assert mp.abs_qty == 10

    def test_default_state(self):
        mp = _long_pos()
        assert mp.state == State.NEW
        assert mp.current_trail_level == -1


# ── Price calculations ────────────────────────────────────────────────────

class TestPriceCalculations:
    def test_tp_price_long(self):
        mp = _long_pos(entry_price=100.0)
        assert mp.tp_price(5.0) == 105.0
        assert mp.tp_price(0.1) == 100.10

    def test_tp_price_short(self):
        mp = _short_pos(entry_price=100.0)
        assert mp.tp_price(5.0) == 95.0

    def test_sl_price_long(self):
        mp = _long_pos(entry_price=100.0)
        assert mp.sl_price(15.0) == 85.0

    def test_sl_price_short(self):
        mp = _short_pos(entry_price=100.0)
        assert mp.sl_price(15.0) == 115.0

    def test_trigger_price_long(self):
        mp = _long_pos(entry_price=100.0)
        assert mp.trigger_price(2.5) == 102.5
        assert mp.trigger_price(5.0) == 105.0

    def test_trigger_price_short(self):
        mp = _short_pos(entry_price=100.0)
        assert mp.trigger_price(2.5) == 97.5


# ── Trigger hit ───────────────────────────────────────────────────────────

class TestTriggerHit:
    def test_long_trigger_not_hit(self):
        mp = _long_pos(entry_price=100.0)
        assert mp.trigger_hit(102.0, 2.5) is False

    def test_long_trigger_exactly_hit(self):
        mp = _long_pos(entry_price=100.0)
        assert mp.trigger_hit(102.5, 2.5) is True

    def test_long_trigger_exceeded(self):
        mp = _long_pos(entry_price=100.0)
        assert mp.trigger_hit(110.0, 2.5) is True

    def test_short_trigger_not_hit(self):
        mp = _short_pos(entry_price=100.0)
        assert mp.trigger_hit(98.0, 2.5) is False

    def test_short_trigger_hit(self):
        mp = _short_pos(entry_price=100.0)
        assert mp.trigger_hit(97.5, 2.5) is True

    def test_short_trigger_exceeded(self):
        mp = _short_pos(entry_price=100.0)
        assert mp.trigger_hit(90.0, 2.5) is True

    def test_multi_level_triggers_long(self):
        """Verify each trailing level trigger works correctly."""
        mp = _long_pos(entry_price=100.0)
        levels = [2.5, 5.0, 7.0, 10.0]
        # Price at 104 → only L1 (2.5%) hit
        assert mp.trigger_hit(104.0, levels[0]) is True
        assert mp.trigger_hit(104.0, levels[1]) is False
        # Price at 106 → L1 and L2 hit
        assert mp.trigger_hit(106.0, levels[1]) is True
        assert mp.trigger_hit(106.0, levels[2]) is False
        # Price at 111 → all levels hit
        assert mp.trigger_hit(111.0, levels[3]) is True
