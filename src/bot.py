from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional

from ib_insync import IB, Contract, Order, Position, Ticker, Trade

from .state import ManagedPosition, State

log = logging.getLogger(__name__)


class RiskBot:
    def __init__(self, ib: IB, cfg: dict):
        self.ib = ib
        self.cfg = cfg

        r = cfg["risk"]
        self.tp_pct: float = r["tp_pct"]
        self.sl_pct: float = r["sl_pct"]

        # Multi-level trailing stop: if trailing_levels is defined, use it;
        # otherwise fall back to single trigger_pct/trail_pct for backward compat.
        if "trailing_levels" in r and r["trailing_levels"]:
            self.trailing_levels = sorted(r["trailing_levels"], key=lambda x: x["trigger"])
        else:
            self.trailing_levels = [
                {"trigger": r["trigger_pct"], "trailing": r["trail_pct"]}
            ]

        b = cfg["bot"]
        self.poll_interval: int = b["poll_interval"]
        self.order_timeout: int = b["order_timeout"]
        self.protection_check_interval: int = b.get("protection_check_interval", 30)

        # conid → ManagedPosition
        self._positions: Dict[int, ManagedPosition] = {}

        # conid → Ticker (market data subscriptions, one per position)
        self._tickers: Dict[int, Ticker] = {}

        # Protection loop timestamp
        self._last_protection_check: float = 0.0

        # Register IBKR callbacks
        self.ib.orderStatusEvent += self._on_order_status
        self.ib.positionEvent    += self._on_position_event
        self.ib.errorEvent       += self._on_error

    # ── Public entry point ───────────────────────────────────────────────────

    async def run_forever(self):
        """Main loop: scans portfolio and manages each position."""
        # Request delayed market data (type 3) — works on paper accounts without
        # live data subscriptions. Switch to type 1 for live accounts with subscriptions.
        # 3 = delayed (15 min), 4 = delayed-frozen (last delayed when market closed).
        # Type 4 works on paper accounts in TWS without any data subscription.
        self.ib.reqMarketDataType(4)

        levels_desc = ", ".join(
            f"L{i+1}: +{lv['trigger']}%→{lv['trailing']}% trail"
            for i, lv in enumerate(self.trailing_levels)
        )
        log.info("Trailing levels: %s", levels_desc)
        log.info("Protection check interval: %ds", self.protection_check_interval)
        log.info("Bot started. Performing initial portfolio recovery scan…")
        await self._recover()

        while True:
            try:
                await self._scan_positions()
                await self._tick_all()
                await self._maybe_check_protection()
            except Exception as exc:
                log.error("Unexpected error in main loop: %s", exc, exc_info=True)
            await asyncio.sleep(self.poll_interval)

    # ── Recovery (on start / reconnect) ─────────────────────────────────────

    async def _recover(self):
        """
        On startup, reconcile existing IBKR positions and open orders so the
        bot can manage positions that were already open before it started.
        """
        self.ib.reqPositions()
        # reqAllOpenOrders fetches orders from ALL sessions (not just the current
        # one), which is required to detect TP/SL orders placed before a restart.
        self.ib.reqAllOpenOrders()
        await asyncio.sleep(2)

        open_trades = self.ib.openTrades()
        trades_by_conid: Dict[int, list] = {}
        for t in open_trades:
            cid = t.contract.conId
            trades_by_conid.setdefault(cid, []).append(t)

        for pos in self.ib.positions():
            if pos.position == 0:
                continue
            conid = pos.contract.conId
            mp = self._make_managed(pos)

            existing    = trades_by_conid.get(conid, [])
            oca_orders  = [t for t in existing if t.order.ocaGroup]
            trail_orders = [t for t in existing if t.order.orderType == "TRAIL"]

            if trail_orders:
                mp.state = State.TRAILING
                mp.trail_order_id = trail_orders[0].order.orderId
                # Determine current trailing level from the order's trailingPercent
                recovered_pct = trail_orders[0].order.trailingPercent
                mp.current_trail_level = 0
                if recovered_pct is not None:
                    for i, lv in enumerate(self.trailing_levels):
                        if abs(lv["trailing"] - recovered_pct) < 0.01:
                            mp.current_trail_level = i
                            break
                log.info(
                    "RECOVERY: %s → TRAILING L%d (orderId=%d, trail=%.1f%%)",
                    mp, mp.current_trail_level + 1, mp.trail_order_id,
                    recovered_pct or 0,
                )
            elif oca_orders:
                # At least one OCA order found — resume monitoring.
                # Use the first STP as SL and first LMT as TP (if present).
                mp.state = State.MONITORING
                for t in oca_orders:
                    ot = t.order.orderType
                    if ot == "LMT" and mp.tp_order_id is None:
                        mp.tp_order_id = t.order.orderId
                        mp.oca_group   = t.order.ocaGroup
                    elif ot == "STP" and mp.sl_order_id is None:
                        mp.sl_order_id = t.order.orderId
                        mp.oca_group   = t.order.ocaGroup
                log.info(
                    "RECOVERY: %s → MONITORING (TP=%s SL=%s)",
                    mp, mp.tp_order_id, mp.sl_order_id,
                )
                if mp.tp_order_id is None:
                    log.warning(
                        "RECOVERY: %s has no TP order — will place fresh TP on next tick.",
                        mp.symbol,
                    )
                    mp.state = State.NEEDS_TP
            else:
                log.info("RECOVERY: %s → NEW (will place TP/SL)", mp)

            self._positions[conid] = mp

    # ── Portfolio scan ───────────────────────────────────────────────────────

    async def _scan_positions(self):
        """Detect new positions, clean up closed ones, sweep orphan orders."""
        for pos in self.ib.positions():
            if pos.position == 0:
                continue
            conid = pos.contract.conId
            if conid not in self._positions:
                mp = self._make_managed(pos)
                self._positions[conid] = mp
                log.info("New position detected: %s", mp)

        current_conids = {p.contract.conId for p in self.ib.positions() if p.position != 0}
        for conid in list(self._positions.keys()):
            if conid not in current_conids:
                mp = self._positions.pop(conid)
                self._cancel_market_data(conid)
                log.info("Position closed and removed from tracking: %s", mp.symbol)

        # Orphan order sweep: cancel any open orders for symbols with no position.
        await self._cancel_orphan_orders(current_conids)

    async def _cancel_orphan_orders(self, active_conids: set):
        """
        Cancel any open LMT, STP, or TRAIL orders whose contract no longer
        has an active position. Prevents accidental short positions from
        leftover stop orders after a position is closed.
        """
        terminal = {"Cancelled", "Inactive", "Filled"}
        orphans = [
            t for t in self.ib.openTrades()
            if t.contract.conId not in active_conids
            and t.order.orderType in ("LMT", "STP", "TRAIL")
            and t.orderStatus.status not in terminal
        ]
        for t in orphans:
            log.warning(
                "Orphan order detected — cancelling %s %s order %d for %s (no active position).",
                t.order.orderType, t.order.action, t.order.orderId, t.contract.symbol,
            )
            self.ib.cancelOrder(t.order)

    def _make_managed(self, pos: Position) -> ManagedPosition:
        c = pos.contract
        avg = pos.avgCost
        return ManagedPosition(
            conid=c.conId,
            symbol=c.symbol,
            sec_type=c.secType,
            exchange=c.exchange or "SMART",
            currency=c.currency,
            quantity=pos.position,
            entry_price=avg if avg > 0 else 0.0,
        )

    # ── Per-position tick ────────────────────────────────────────────────────

    async def _tick_all(self):
        for conid, mp in list(self._positions.items()):
            try:
                await self._tick(mp)
            except Exception as exc:
                log.error("Error ticking %s: %s", mp.symbol, exc, exc_info=True)

    async def _tick(self, mp: ManagedPosition):
        if mp.state == State.NEW:
            await self._place_tp_sl(mp)

        elif mp.state == State.NEEDS_TP:
            await self._place_missing_tp(mp)

        elif mp.state == State.MONITORING:
            last = self._get_last_price(mp)
            if last is None:
                return
            first_trigger = self.trailing_levels[0]["trigger"]
            if mp.trigger_hit(last, first_trigger):
                log.info(
                    "%s trigger L1 reached (last=%.2f trigger=%.2f). Switching to trailing.",
                    mp.symbol, last, mp.trigger_price(first_trigger),
                )
                mp.state = State.CANCELLING
                await self._cancel_oca_and_trail(mp)

        elif mp.state == State.TRAILING:
            next_level = mp.current_trail_level + 1
            if next_level >= len(self.trailing_levels):
                return  # already at max level
            last = self._get_last_price(mp)
            if last is None:
                return
            if not mp.trigger_hit(last, self.trailing_levels[next_level]["trigger"]):
                return
            # Find the highest level whose trigger is met (handles price gaps)
            target_level = next_level
            for i in range(next_level + 1, len(self.trailing_levels)):
                if mp.trigger_hit(last, self.trailing_levels[i]["trigger"]):
                    target_level = i
                else:
                    break
            await self._upgrade_trailing_stop(mp, target_level)

        elif mp.state == State.CLOSED:
            pass

    # ── Order placement ──────────────────────────────────────────────────────

    async def _place_tp_sl(self, mp: ManagedPosition):
        if mp.entry_price <= 0:
            log.warning(
                "%s: entry_price not available yet (avgCost=%.4f). Retrying next tick.",
                mp.symbol, mp.entry_price,
            )
            return

        contract  = self._order_contract(mp)
        oca_group = f"OCA_{mp.symbol}_{int(time.time())}"
        tp_price  = mp.tp_price(self.tp_pct)
        sl_price  = mp.sl_price(self.sl_pct)

        tp_order = Order(
            action        = mp.close_action,
            orderType     = "LMT",
            totalQuantity = mp.abs_qty,
            lmtPrice      = tp_price,
            ocaGroup      = oca_group,
            ocaType       = 1,      # cancel with block
            tif           = "GTC",
            transmit      = True,
        )
        sl_order = Order(
            action        = mp.close_action,
            orderType     = "STP",
            totalQuantity = mp.abs_qty,
            auxPrice      = sl_price,
            ocaGroup      = oca_group,
            ocaType       = 1,
            tif           = "GTC",
            transmit      = True,
        )

        tp_trade = self.ib.placeOrder(contract, tp_order)
        sl_trade = self.ib.placeOrder(contract, sl_order)

        await asyncio.sleep(1)  # allow IBKR to assign order IDs

        mp.oca_group   = oca_group
        mp.tp_order_id = tp_trade.order.orderId
        mp.sl_order_id = sl_trade.order.orderId
        mp.state       = State.MONITORING

        log.info(
            "%s: TP placed @ %.2f (id=%d)  SL placed @ %.2f (id=%d)  OCA=%s",
            mp.symbol, tp_price, mp.tp_order_id,
            sl_price,  mp.sl_order_id, oca_group,
        )

    async def _place_missing_tp(self, mp: ManagedPosition):
        """
        Place only the TP order for a position recovered with a SL but no TP.
        The new TP is a standalone GTC limit — it won't be OCA-linked to the
        existing SL, but it will be detected and cancelled by
        _cancel_all_protective_orders when the trigger fires.
        """
        if mp.entry_price <= 0:
            log.warning(
                "%s: entry_price not available yet. Retrying next tick.", mp.symbol
            )
            return

        contract = self._order_contract(mp)
        tp_price = mp.tp_price(self.tp_pct)

        tp_order = Order(
            action        = mp.close_action,
            orderType     = "LMT",
            totalQuantity = mp.abs_qty,
            lmtPrice      = tp_price,
            tif           = "GTC",
            transmit      = True,
        )
        tp_trade = self.ib.placeOrder(contract, tp_order)
        await asyncio.sleep(1)

        mp.tp_order_id = tp_trade.order.orderId
        mp.state       = State.MONITORING

        log.info(
            "%s: Missing TP placed @ %.2f (id=%d)",
            mp.symbol, tp_price, mp.tp_order_id,
        )

    # ── Cancellation + trailing stop ─────────────────────────────────────────

    async def _cancel_oca_and_trail(self, mp: ManagedPosition):
        """Cancel TP then SL (with confirmation), then place trailing stop."""

        if not mp.tp_cancelled:
            if mp.tp_order_id is not None:
                log.info("%s: Cancelling TP order %d…", mp.symbol, mp.tp_order_id)
                tp_trade = self._find_trade(mp.tp_order_id)
                if tp_trade:
                    self.ib.cancelOrder(tp_trade.order)
                    if await self._wait_cancel(mp.tp_order_id):
                        mp.tp_cancelled = True
                        log.info("%s: TP order %d cancelled.", mp.symbol, mp.tp_order_id)
                    else:
                        log.error(
                            "%s: Timeout waiting for TP cancel (id=%d). Will retry.",
                            mp.symbol, mp.tp_order_id,
                        )
                        mp.state = State.MONITORING
                        return
                else:
                    mp.tp_cancelled = True
                    log.info("%s: TP order %d already gone.", mp.symbol, mp.tp_order_id)
            else:
                mp.tp_cancelled = True
                log.info("%s: No TP order ID recorded; skipping TP cancel.", mp.symbol)

        if not mp.sl_cancelled:
            if mp.sl_order_id is not None:
                log.info("%s: Cancelling SL order %d…", mp.symbol, mp.sl_order_id)
                sl_trade = self._find_trade(mp.sl_order_id)
                if sl_trade:
                    self.ib.cancelOrder(sl_trade.order)
                    if await self._wait_cancel(mp.sl_order_id):
                        mp.sl_cancelled = True
                        log.info("%s: SL order %d cancelled.", mp.symbol, mp.sl_order_id)
                    else:
                        log.error(
                            "%s: Timeout waiting for SL cancel (id=%d). Will retry.",
                            mp.symbol, mp.sl_order_id,
                        )
                        mp.state = State.MONITORING
                        return
                else:
                    mp.sl_cancelled = True
                    log.info("%s: SL order %d already gone.", mp.symbol, mp.sl_order_id)
            else:
                mp.sl_cancelled = True
                log.info("%s: No SL order ID recorded; skipping SL cancel.", mp.symbol)

        if mp.tp_cancelled and mp.sl_cancelled:
            # Safety sweep: cancel ANY remaining LMT or STP orders for this
            # contract before placing the trailing stop. This catches untracked
            # orders from previous sessions that recovery may have missed.
            await self._cancel_all_protective_orders(mp)
            first_trail_pct = self.trailing_levels[0]["trailing"]
            await self._place_trailing_stop(mp, first_trail_pct, level=0)

    async def _cancel_all_protective_orders(self, mp: ManagedPosition):
        """
        Cancel all open LMT and STP orders for this contract.
        Called immediately before placing the trailing stop to guarantee a
        clean slate — no stale stop or limit orders are left active.
        """
        terminal = {"Cancelled", "Inactive", "Filled"}
        to_cancel = [
            t for t in self.ib.openTrades()
            if t.contract.conId == mp.conid
            and t.order.orderType in ("LMT", "STP")
            and t.orderStatus.status not in terminal
        ]
        for t in to_cancel:
            log.info(
                "%s: Sweeping residual %s order %d before trailing stop.",
                mp.symbol, t.order.orderType, t.order.orderId,
            )
            self.ib.cancelOrder(t.order)
        # Wait for all cancellations concurrently
        if to_cancel:
            await asyncio.gather(
                *[self._wait_cancel(t.order.orderId) for t in to_cancel]
            )

    async def _place_trailing_stop(self, mp: ManagedPosition, trail_pct: float, level: int = 0):
        contract = self._order_contract(mp)
        trail_order = Order(
            action          = mp.close_action,
            orderType       = "TRAIL",
            totalQuantity   = mp.abs_qty,
            trailingPercent = trail_pct,
            tif             = "GTC",
            transmit        = True,
        )
        trail_trade = self.ib.placeOrder(contract, trail_order)
        await asyncio.sleep(1)

        mp.trail_order_id    = trail_trade.order.orderId
        mp.current_trail_level = level
        mp.state             = State.TRAILING

        log.info(
            "%s: Trailing Stop L%d placed (%.1f%%) id=%d",
            mp.symbol, level + 1, trail_pct, mp.trail_order_id,
        )

    async def _upgrade_trailing_stop(self, mp: ManagedPosition, level: int):
        level_cfg = self.trailing_levels[level]
        log.info(
            "%s: Upgrading trailing stop to L%d (trigger=+%.1f%%, trail=%.1f%%)",
            mp.symbol, level + 1, level_cfg["trigger"], level_cfg["trailing"],
        )

        # Cancel existing trailing stop
        if mp.trail_order_id is not None:
            trade = self._find_trade(mp.trail_order_id)
            if trade:
                self.ib.cancelOrder(trade.order)
                if not await self._wait_cancel(mp.trail_order_id):
                    log.error(
                        "%s: Timeout cancelling trail order %d for upgrade. Will retry.",
                        mp.symbol, mp.trail_order_id,
                    )
                    return
                log.info("%s: Old trailing stop %d cancelled.", mp.symbol, mp.trail_order_id)

        await self._place_trailing_stop(mp, level_cfg["trailing"], level=level)

    # ── Protection loop ─────────────────────────────────────────────────────

    async def _maybe_check_protection(self):
        now = time.monotonic()
        if now - self._last_protection_check < self.protection_check_interval:
            return
        self._last_protection_check = now
        await self._check_protection()

    async def _check_protection(self):
        """Verify all managed positions still have their protective orders active."""
        log.debug("Protection check running — %d position(s) tracked.", len(self._positions))
        for mp in list(self._positions.values()):
            if mp.state == State.MONITORING:
                # Skip if a cancel/trail transition is in progress
                if mp.tp_cancelled or mp.sl_cancelled:
                    continue
                tp_ok = self._is_order_active(mp.tp_order_id)
                sl_ok = self._is_order_active(mp.sl_order_id)
                if not tp_ok or not sl_ok:
                    log.warning(
                        "PROTECTION: %s missing orders (TP=%s SL=%s). Recreating protection.",
                        mp.symbol,
                        "ok" if tp_ok else "MISSING",
                        "ok" if sl_ok else "MISSING",
                    )
                    # Cancel any remaining orders for a clean slate
                    await self._cancel_all_protective_orders(mp)
                    mp.tp_order_id = None
                    mp.sl_order_id = None
                    mp.tp_cancelled = False
                    mp.sl_cancelled = False
                    mp.oca_group = ""
                    mp.state = State.NEW

            elif mp.state == State.TRAILING:
                if not self._is_order_active(mp.trail_order_id):
                    log.warning(
                        "PROTECTION: %s trailing stop missing. Recreating at L%d.",
                        mp.symbol, mp.current_trail_level + 1,
                    )
                    level_idx = max(mp.current_trail_level, 0)
                    trail_pct = self.trailing_levels[level_idx]["trailing"]
                    await self._place_trailing_stop(mp, trail_pct, level=level_idx)

            elif mp.state == State.NEEDS_TP:
                if mp.sl_order_id and not self._is_order_active(mp.sl_order_id):
                    log.warning(
                        "PROTECTION: %s SL order also missing. Resetting to NEW.",
                        mp.symbol,
                    )
                    mp.tp_order_id = None
                    mp.sl_order_id = None
                    mp.oca_group = ""
                    mp.state = State.NEW

    def _is_order_active(self, order_id: Optional[int]) -> bool:
        if order_id is None:
            return False
        terminal = {"Cancelled", "Inactive", "Filled"}
        trade = self._find_trade(order_id)
        if trade is None:
            return False
        return trade.orderStatus.status not in terminal

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _order_contract(self, mp: ManagedPosition) -> Contract:
        """Contract used for placing orders — always SMART routing."""
        c = Contract()
        c.conId    = mp.conid
        c.symbol   = mp.symbol
        c.secType  = mp.sec_type
        c.exchange = "SMART"
        c.currency = mp.currency
        return c

    def _find_trade(self, order_id: int) -> Optional[Trade]:
        for t in self.ib.openTrades():
            if t.order.orderId == order_id:
                return t
        return None

    def _get_last_price(self, mp: ManagedPosition) -> Optional[float]:
        """
        Return the last price for a managed position.
        Subscribes to market data once; reuses the ticker on subsequent calls.
        """
        conid = mp.conid

        if conid not in self._tickers:
            contract = self._order_contract(mp)
            ticker = self.ib.reqMktData(contract, "", False, False)
            self._tickers[conid] = ticker
            return None  # data not available yet on first call

        ticker = self._tickers[conid]
        price = ticker.last
        if price is None or price != price:  # NaN check
            price = ticker.close
        if price is None or price != price:
            return None
        return price

    def _cancel_market_data(self, conid: int):
        ticker = self._tickers.pop(conid, None)
        if ticker is not None:
            self.ib.cancelMktData(ticker.contract)

    async def _wait_cancel(self, order_id: int) -> bool:
        """Poll until the order is confirmed cancelled (or filled) or timeout."""
        deadline = time.monotonic() + self.order_timeout
        terminal = {"Cancelled", "Inactive", "Filled"}
        while time.monotonic() < deadline:
            trade = self._find_trade(order_id)
            if trade is None:
                return True  # gone from open trades → confirmed
            if trade.orderStatus.status in terminal:
                return True
            await asyncio.sleep(0.5)
        return False

    # ── IBKR event callbacks ─────────────────────────────────────────────────

    def _on_order_status(self, trade: Trade):
        log.debug(
            "orderStatus: orderId=%d status=%s",
            trade.order.orderId, trade.orderStatus.status,
        )

    def _on_position_event(self, position: Position):
        """
        Called by ib_insync when position data changes.
        ib_insync emits positionEvent with a single Position namedtuple.
        """
        if position.position == 0:
            conid = position.contract.conId
            if conid in self._positions:
                mp = self._positions.pop(conid)
                self._cancel_market_data(conid)
                log.info("Position closed via event: %s", position.contract.symbol)

    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract):
        # 10089 — no market data subscription; bot uses delayed/frozen data gracefully.
        # 1100  — connectivity lost; bot reconnects automatically.
        # 1102  — connectivity restored.
        # 2104/2106/2158 — market data farm connection notices (informational).
        ignored = {2104, 2106, 2158, 2119}
        warnings = {10089, 1100, 1101, 1102, 321, 2151, 2137}
        if errorCode in ignored:
            return
        if errorCode in warnings:
            if errorCode == 10089:
                symbol = contract.symbol if contract else "?"
                log.warning(
                    "No market data subscription for %s — trigger monitoring paused "
                    "until data is available (error 10089).", symbol,
                )
            else:
                log.warning("IBKR notice %d: %s", errorCode, errorString)
        else:
            log.error("IBKR error %d (reqId=%d): %s", errorCode, reqId, errorString)
