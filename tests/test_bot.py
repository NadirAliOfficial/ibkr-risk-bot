"""Tests for RiskBot — uses mocked IB connection."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

from src.bot import RiskBot
from src.state import ManagedPosition, State


# ── Helpers / Fakes ───────────────────────────────────────────────────────

@dataclass
class FakeOrderStatus:
    status: str = "Submitted"

@dataclass
class FakeOrder:
    orderId: int = 0
    orderType: str = "LMT"
    action: str = "SELL"
    ocaGroup: str = ""
    trailingPercent: Optional[float] = None

@dataclass
class FakeTrade:
    order: FakeOrder = field(default_factory=FakeOrder)
    contract: MagicMock = field(default_factory=lambda: MagicMock(conId=1, symbol="AAPL"))
    orderStatus: FakeOrderStatus = field(default_factory=FakeOrderStatus)

@dataclass
class FakeTicker:
    last: Optional[float] = None
    close: Optional[float] = None
    contract: MagicMock = field(default_factory=MagicMock)

@dataclass
class FakePosition:
    contract: MagicMock = field(default_factory=lambda: MagicMock(
        conId=1, symbol="AAPL", secType="STK", exchange="SMART", currency="USD"
    ))
    position: float = 10
    avgCost: float = 100.0


def _make_config(trailing_levels=None, protection_interval=30):
    cfg = {
        "risk": {"tp_pct": 0.1, "sl_pct": 15.0},
        "bot": {
            "poll_interval": 5,
            "order_timeout": 2,
            "protection_check_interval": protection_interval,
        },
    }
    if trailing_levels:
        cfg["risk"]["trailing_levels"] = trailing_levels
    else:
        cfg["risk"]["trigger_pct"] = 5.0
        cfg["risk"]["trail_pct"] = 2.0
    return cfg


def _make_bot(cfg=None, open_trades=None):
    if cfg is None:
        cfg = _make_config()
    ib = MagicMock()
    ib.openTrades.return_value = open_trades or []
    ib.positions.return_value = []
    # Make placeOrder return a FakeTrade with a proper orderId
    _next_id = [100]
    def fake_place(contract, order):
        t = FakeTrade()
        t.order = FakeOrder(orderId=_next_id[0], orderType=order.orderType)
        _next_id[0] += 1
        return t
    ib.placeOrder.side_effect = fake_place
    bot = RiskBot(ib, cfg)
    return bot


def _long_mp(**kw):
    defaults = dict(
        conid=1, symbol="AAPL", sec_type="STK",
        exchange="SMART", currency="USD",
        quantity=10, entry_price=100.0,
    )
    defaults.update(kw)
    return ManagedPosition(**defaults)


# ── Trailing levels config parsing ────────────────────────────────────────

class TestTrailingLevelsConfig:
    def test_single_level_fallback(self):
        cfg = _make_config()
        bot = _make_bot(cfg)
        assert len(bot.trailing_levels) == 1
        assert bot.trailing_levels[0] == {"trigger": 5.0, "trailing": 2.0}

    def test_multi_level_parsing(self):
        levels = [
            {"trigger": 5.0, "trailing": 2.0},
            {"trigger": 2.5, "trailing": 1.5},
            {"trigger": 10.0, "trailing": 3.0},
            {"trigger": 7.0, "trailing": 2.5},
        ]
        cfg = _make_config(trailing_levels=levels)
        bot = _make_bot(cfg)
        # Should be sorted by trigger ascending
        assert len(bot.trailing_levels) == 4
        assert bot.trailing_levels[0]["trigger"] == 2.5
        assert bot.trailing_levels[1]["trigger"] == 5.0
        assert bot.trailing_levels[2]["trigger"] == 7.0
        assert bot.trailing_levels[3]["trigger"] == 10.0

    def test_empty_trailing_levels_falls_back(self):
        cfg = _make_config()
        cfg["risk"]["trailing_levels"] = []
        cfg["risk"]["trigger_pct"] = 3.0
        cfg["risk"]["trail_pct"] = 1.0
        bot = _make_bot(cfg)
        assert len(bot.trailing_levels) == 1
        assert bot.trailing_levels[0] == {"trigger": 3.0, "trailing": 1.0}

    def test_protection_check_interval_default(self):
        cfg = _make_config()
        del cfg["bot"]["protection_check_interval"]
        bot = _make_bot(cfg)
        assert bot.protection_check_interval == 30

    def test_protection_check_interval_custom(self):
        cfg = _make_config(protection_interval=60)
        bot = _make_bot(cfg)
        assert bot.protection_check_interval == 60


# ── Tick: MONITORING → TRAILING (first level trigger) ─────────────────────

class TestTickMonitoring:
    @pytest.mark.asyncio
    async def test_no_trigger_when_price_below(self):
        levels = [{"trigger": 2.5, "trailing": 1.5}, {"trigger": 5.0, "trailing": 2.0}]
        bot = _make_bot(_make_config(trailing_levels=levels))
        mp = _long_mp(state=State.MONITORING)
        # Price at 101 → +1% → below L1 trigger of 2.5%
        bot._tickers[1] = FakeTicker(last=101.0)
        await bot._tick(mp)
        assert mp.state == State.MONITORING

    @pytest.mark.asyncio
    async def test_trigger_l1_switches_to_cancelling(self):
        levels = [{"trigger": 2.5, "trailing": 1.5}, {"trigger": 5.0, "trailing": 2.0}]
        bot = _make_bot(_make_config(trailing_levels=levels))
        mp = _long_mp(state=State.MONITORING, tp_order_id=10, sl_order_id=11)
        mp.tp_cancelled = True
        mp.sl_cancelled = True
        # Price at 103 → +3% → above L1 trigger of 2.5%
        bot._tickers[1] = FakeTicker(last=103.0)
        await bot._tick(mp)
        # Should have attempted to cancel and place trailing stop
        assert mp.state == State.TRAILING
        assert mp.current_trail_level == 0

    @pytest.mark.asyncio
    async def test_no_action_when_price_is_none(self):
        bot = _make_bot()
        mp = _long_mp(state=State.MONITORING)
        bot._tickers[1] = FakeTicker(last=None, close=None)
        await bot._tick(mp)
        assert mp.state == State.MONITORING


# ── Tick: TRAILING level upgrades ─────────────────────────────────────────

class TestTickTrailingUpgrade:
    @pytest.mark.asyncio
    async def test_no_upgrade_when_below_next_level(self):
        levels = [
            {"trigger": 2.5, "trailing": 1.5},
            {"trigger": 5.0, "trailing": 2.0},
        ]
        bot = _make_bot(_make_config(trailing_levels=levels))
        mp = _long_mp(state=State.TRAILING, current_trail_level=0, trail_order_id=50)
        # Price at 103 → +3% → below L2 trigger of 5%
        bot._tickers[1] = FakeTicker(last=103.0)
        await bot._tick(mp)
        assert mp.state == State.TRAILING
        assert mp.current_trail_level == 0

    @pytest.mark.asyncio
    async def test_upgrade_when_next_level_reached(self):
        levels = [
            {"trigger": 2.5, "trailing": 1.5},
            {"trigger": 5.0, "trailing": 2.0},
        ]
        bot = _make_bot(_make_config(trailing_levels=levels))
        mp = _long_mp(state=State.TRAILING, current_trail_level=0, trail_order_id=50)
        # Mock the old trail order so it can be cancelled
        old_trade = FakeTrade(
            order=FakeOrder(orderId=50, orderType="TRAIL"),
            orderStatus=FakeOrderStatus(status="Submitted"),
        )
        bot.ib.openTrades.return_value = [old_trade]
        # Price at 106 → +6% → above L2 trigger of 5%
        bot._tickers[1] = FakeTicker(last=106.0)

        # Make _wait_cancel return True (cancel confirmed)
        with patch.object(bot, '_wait_cancel', return_value=True):
            await bot._tick(mp)

        assert mp.state == State.TRAILING
        assert mp.current_trail_level == 1
        # New trail order should have been placed
        assert mp.trail_order_id != 50

    @pytest.mark.asyncio
    async def test_price_gap_skips_to_highest_level(self):
        """Price jumps past L2 and L3 — should upgrade directly to L4."""
        levels = [
            {"trigger": 2.5, "trailing": 1.5},
            {"trigger": 5.0, "trailing": 2.0},
            {"trigger": 7.0, "trailing": 2.5},
            {"trigger": 10.0, "trailing": 3.0},
        ]
        bot = _make_bot(_make_config(trailing_levels=levels))
        mp = _long_mp(state=State.TRAILING, current_trail_level=0, trail_order_id=50)
        old_trade = FakeTrade(
            order=FakeOrder(orderId=50, orderType="TRAIL"),
            orderStatus=FakeOrderStatus(status="Submitted"),
        )
        bot.ib.openTrades.return_value = [old_trade]
        # Price at 111 → +11% → above all triggers (L1=2.5, L2=5, L3=7, L4=10)
        bot._tickers[1] = FakeTicker(last=111.0)

        with patch.object(bot, '_wait_cancel', return_value=True):
            await bot._tick(mp)

        assert mp.current_trail_level == 3  # jumped straight to L4
        assert mp.trail_order_id != 50

    @pytest.mark.asyncio
    async def test_no_upgrade_at_max_level(self):
        levels = [{"trigger": 2.5, "trailing": 1.5}]
        bot = _make_bot(_make_config(trailing_levels=levels))
        mp = _long_mp(state=State.TRAILING, current_trail_level=0, trail_order_id=50)
        bot._tickers[1] = FakeTicker(last=200.0)  # way above any trigger
        await bot._tick(mp)
        # Should stay at level 0, no upgrade attempted
        assert mp.current_trail_level == 0

    @pytest.mark.asyncio
    async def test_upgrade_cancel_timeout_retries(self):
        levels = [
            {"trigger": 2.5, "trailing": 1.5},
            {"trigger": 5.0, "trailing": 2.0},
        ]
        bot = _make_bot(_make_config(trailing_levels=levels))
        mp = _long_mp(state=State.TRAILING, current_trail_level=0, trail_order_id=50)
        old_trade = FakeTrade(
            order=FakeOrder(orderId=50, orderType="TRAIL"),
            orderStatus=FakeOrderStatus(status="Submitted"),
        )
        bot.ib.openTrades.return_value = [old_trade]
        bot._tickers[1] = FakeTicker(last=106.0)

        # _wait_cancel returns False (timeout)
        with patch.object(bot, '_wait_cancel', return_value=False):
            await bot._tick(mp)

        # Should stay at level 0 — upgrade failed, will retry next tick
        assert mp.current_trail_level == 0
        assert mp.trail_order_id == 50


# ── Protection loop ──────────────────────────────────────────────────────

class TestProtectionLoop:
    @pytest.mark.asyncio
    async def test_protection_check_respects_interval(self):
        bot = _make_bot(_make_config(protection_interval=30))
        bot._last_protection_check = time.monotonic()  # just checked
        with patch.object(bot, '_check_protection') as mock_check:
            await bot._maybe_check_protection()
            mock_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_protection_check_fires_after_interval(self):
        bot = _make_bot(_make_config(protection_interval=0))
        bot._last_protection_check = 0  # long ago
        with patch.object(bot, '_check_protection') as mock_check:
            await bot._maybe_check_protection()
            mock_check.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_monitoring_with_cancel_in_progress(self):
        """Protection loop should not interfere when cancel/trail transition is underway."""
        bot = _make_bot()
        mp = _long_mp(
            state=State.MONITORING,
            tp_order_id=10,
            sl_order_id=11,
            oca_group="OCA_AAPL_123",
        )
        mp.tp_cancelled = True  # cancel in progress
        bot._positions[1] = mp
        bot.ib.openTrades.return_value = []  # orders are gone

        await bot._check_protection()

        # Should NOT reset to NEW — cancel transition is in progress
        assert mp.state == State.MONITORING
        assert mp.tp_cancelled is True

    @pytest.mark.asyncio
    async def test_monitoring_missing_tp_resets_to_new(self):
        bot = _make_bot()
        mp = _long_mp(
            state=State.MONITORING,
            tp_order_id=10,
            sl_order_id=11,
            oca_group="OCA_AAPL_123",
        )
        bot._positions[1] = mp
        # SL exists, TP does not
        sl_trade = FakeTrade(
            order=FakeOrder(orderId=11, orderType="STP"),
            orderStatus=FakeOrderStatus(status="Submitted"),
            contract=MagicMock(conId=1),
        )
        bot.ib.openTrades.return_value = [sl_trade]

        await bot._check_protection()

        assert mp.state == State.NEW
        assert mp.tp_order_id is None
        assert mp.sl_order_id is None
        assert mp.oca_group == ""

    @pytest.mark.asyncio
    async def test_monitoring_missing_sl_resets_to_new(self):
        bot = _make_bot()
        mp = _long_mp(
            state=State.MONITORING,
            tp_order_id=10,
            sl_order_id=11,
            oca_group="OCA_AAPL_123",
        )
        bot._positions[1] = mp
        # TP exists, SL does not
        tp_trade = FakeTrade(
            order=FakeOrder(orderId=10, orderType="LMT"),
            orderStatus=FakeOrderStatus(status="Submitted"),
            contract=MagicMock(conId=1),
        )
        bot.ib.openTrades.return_value = [tp_trade]

        await bot._check_protection()

        assert mp.state == State.NEW

    @pytest.mark.asyncio
    async def test_monitoring_both_present_no_change(self):
        bot = _make_bot()
        mp = _long_mp(
            state=State.MONITORING,
            tp_order_id=10,
            sl_order_id=11,
        )
        bot._positions[1] = mp
        trades = [
            FakeTrade(order=FakeOrder(orderId=10, orderType="LMT"),
                      orderStatus=FakeOrderStatus(status="Submitted"),
                      contract=MagicMock(conId=1)),
            FakeTrade(order=FakeOrder(orderId=11, orderType="STP"),
                      orderStatus=FakeOrderStatus(status="Submitted"),
                      contract=MagicMock(conId=1)),
        ]
        bot.ib.openTrades.return_value = trades

        await bot._check_protection()

        assert mp.state == State.MONITORING

    @pytest.mark.asyncio
    async def test_trailing_missing_recreates(self):
        levels = [{"trigger": 2.5, "trailing": 1.5}, {"trigger": 5.0, "trailing": 2.0}]
        bot = _make_bot(_make_config(trailing_levels=levels))
        mp = _long_mp(state=State.TRAILING, trail_order_id=50, current_trail_level=1)
        bot._positions[1] = mp
        # Trail order is gone
        bot.ib.openTrades.return_value = []

        await bot._check_protection()

        # Should recreate at level 1
        assert mp.state == State.TRAILING
        assert mp.current_trail_level == 1
        assert mp.trail_order_id != 50  # new order placed

    @pytest.mark.asyncio
    async def test_trailing_present_no_change(self):
        bot = _make_bot()
        mp = _long_mp(state=State.TRAILING, trail_order_id=50, current_trail_level=0)
        bot._positions[1] = mp
        trail_trade = FakeTrade(
            order=FakeOrder(orderId=50, orderType="TRAIL"),
            orderStatus=FakeOrderStatus(status="Submitted"),
            contract=MagicMock(conId=1),
        )
        bot.ib.openTrades.return_value = [trail_trade]

        await bot._check_protection()

        assert mp.state == State.TRAILING
        assert mp.trail_order_id == 50  # unchanged

    @pytest.mark.asyncio
    async def test_needs_tp_missing_sl_resets_to_new(self):
        bot = _make_bot()
        mp = _long_mp(state=State.NEEDS_TP, sl_order_id=11)
        bot._positions[1] = mp
        bot.ib.openTrades.return_value = []

        await bot._check_protection()

        assert mp.state == State.NEW
        assert mp.sl_order_id is None


# ── is_order_active ──────────────────────────────────────────────────────

class TestIsOrderActive:
    def test_none_order_id(self):
        bot = _make_bot()
        assert bot._is_order_active(None) is False

    def test_order_not_found(self):
        bot = _make_bot()
        bot.ib.openTrades.return_value = []
        assert bot._is_order_active(99) is False

    def test_order_submitted(self):
        bot = _make_bot()
        trade = FakeTrade(
            order=FakeOrder(orderId=10),
            orderStatus=FakeOrderStatus(status="Submitted"),
        )
        bot.ib.openTrades.return_value = [trade]
        assert bot._is_order_active(10) is True

    def test_order_cancelled(self):
        bot = _make_bot()
        trade = FakeTrade(
            order=FakeOrder(orderId=10),
            orderStatus=FakeOrderStatus(status="Cancelled"),
        )
        bot.ib.openTrades.return_value = [trade]
        assert bot._is_order_active(10) is False

    def test_order_filled(self):
        bot = _make_bot()
        trade = FakeTrade(
            order=FakeOrder(orderId=10),
            orderStatus=FakeOrderStatus(status="Filled"),
        )
        bot.ib.openTrades.return_value = [trade]
        assert bot._is_order_active(10) is False


# ── Recovery with trailing levels ────────────────────────────────────────

class TestRecoveryTrailingLevels:
    @pytest.mark.asyncio
    async def test_recovery_detects_trailing_level(self):
        levels = [
            {"trigger": 2.5, "trailing": 1.5},
            {"trigger": 5.0, "trailing": 2.0},
            {"trigger": 7.0, "trailing": 2.5},
        ]
        bot = _make_bot(_make_config(trailing_levels=levels))

        pos = FakePosition()
        bot.ib.positions.return_value = [pos]

        # Existing trail order at 2.0% → should match level 1 (index 1)
        trail_trade = FakeTrade(
            order=FakeOrder(orderId=50, orderType="TRAIL", trailingPercent=2.0),
            orderStatus=FakeOrderStatus(status="Submitted"),
            contract=pos.contract,
        )
        bot.ib.openTrades.return_value = [trail_trade]

        await bot._recover()

        mp = bot._positions[1]
        assert mp.state == State.TRAILING
        assert mp.current_trail_level == 1
        assert mp.trail_order_id == 50

    @pytest.mark.asyncio
    async def test_recovery_unknown_trail_pct_defaults_to_0(self):
        levels = [{"trigger": 2.5, "trailing": 1.5}]
        bot = _make_bot(_make_config(trailing_levels=levels))

        pos = FakePosition()
        bot.ib.positions.return_value = [pos]

        # Trail at 9.9% — doesn't match any level
        trail_trade = FakeTrade(
            order=FakeOrder(orderId=50, orderType="TRAIL", trailingPercent=9.9),
            orderStatus=FakeOrderStatus(status="Submitted"),
            contract=pos.contract,
        )
        bot.ib.openTrades.return_value = [trail_trade]

        await bot._recover()

        mp = bot._positions[1]
        assert mp.state == State.TRAILING
        assert mp.current_trail_level == 0  # defaults to 0


# ── Place TP/SL ──────────────────────────────────────────────────────────

class TestPlaceTPSL:
    @pytest.mark.asyncio
    async def test_place_tp_sl_sets_monitoring(self):
        bot = _make_bot()
        mp = _long_mp()
        assert mp.state == State.NEW

        await bot._place_tp_sl(mp)

        assert mp.state == State.MONITORING
        assert mp.tp_order_id is not None
        assert mp.sl_order_id is not None
        assert mp.oca_group.startswith("OCA_AAPL_")

    @pytest.mark.asyncio
    async def test_place_tp_sl_skips_zero_entry(self):
        bot = _make_bot()
        mp = _long_mp(entry_price=0.0)

        await bot._place_tp_sl(mp)

        assert mp.state == State.NEW  # unchanged
        assert mp.tp_order_id is None


# ── Short position handling ──────────────────────────────────────────────

class TestShortPositions:
    @pytest.mark.asyncio
    async def test_short_trigger_in_monitoring(self):
        levels = [{"trigger": 2.5, "trailing": 1.5}]
        bot = _make_bot(_make_config(trailing_levels=levels))
        mp = _long_mp(quantity=-10, state=State.MONITORING, tp_order_id=10, sl_order_id=11)
        mp.tp_cancelled = True
        mp.sl_cancelled = True
        # Short at 100, trigger at -2.5% → 97.5. Price at 97 → hit
        bot._tickers[1] = FakeTicker(last=97.0)
        await bot._tick(mp)
        assert mp.state == State.TRAILING
        assert mp.current_trail_level == 0

    @pytest.mark.asyncio
    async def test_short_trigger_not_hit(self):
        levels = [{"trigger": 2.5, "trailing": 1.5}]
        bot = _make_bot(_make_config(trailing_levels=levels))
        mp = _long_mp(quantity=-10, state=State.MONITORING)
        # Short at 100, trigger at 97.5. Price at 99 → not hit
        bot._tickers[1] = FakeTicker(last=99.0)
        await bot._tick(mp)
        assert mp.state == State.MONITORING
