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
        self.tp_pct: float      = r["tp_pct"]
        self.sl_pct: float      = r["sl_pct"]
        self.trigger_pct: float = r["trigger_pct"]
        self.trail_pct: float   = r["trail_pct"]

        b = cfg["bot"]
        self.poll_interval: int = b["poll_interval"]
        self.order_timeout: int = b["order_timeout"]

        # conid → ManagedPosition
        self._positions: Dict[int, ManagedPosition] = {}

        # conid → Ticker (market data subscriptions, one per position)
        self._tickers: Dict[int, Ticker] = {}

        # Register IBKR callbacks
        self.ib.orderStatusEvent += self._on_order_status
        self.ib.positionEvent    += self._on_position_event

    # ── Public entry point ───────────────────────────────────────────────────

    async def run_forever(self):
        """Main loop: scans portfolio and manages each position."""
        # Request delayed market data (type 3) — works on paper accounts without
        # live data subscriptions. Switch to type 1 for live accounts with subscriptions.
        # 3 = delayed (15 min), 4 = delayed-frozen (last delayed when market closed).
        # Type 4 works on paper accounts in TWS without any data subscription.
        self.ib.reqMarketDataType(4)

        log.info("Bot started. Performing initial portfolio recovery scan…")
        await self._recover()

        while True:
            try:
                await self._scan_positions()
                await self._tick_all()
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
                log.info("RECOVERY: %s → TRAILING (orderId=%d)", mp, mp.trail_order_id)
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
        """Detect new positions; clean up closed ones."""
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
            if mp.trigger_hit(last, self.trigger_pct):
                log.info(
                    "%s trigger reached (last=%.2f trigger=%.2f). Switching to trailing.",
                    mp.symbol, last, mp.trigger_price(self.trigger_pct),
                )
                mp.state = State.CANCELLING
                await self._cancel_oca_and_trail(mp)

        elif mp.state in (State.TRAILING, State.CLOSED):
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
            await self._place_trailing_stop(mp)

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

    async def _place_trailing_stop(self, mp: ManagedPosition):
        contract = self._order_contract(mp)
        trail_order = Order(
            action          = mp.close_action,
            orderType       = "TRAIL",
            totalQuantity   = mp.abs_qty,
            trailingPercent = self.trail_pct,
            tif             = "GTC",
            transmit        = True,
        )
        trail_trade = self.ib.placeOrder(contract, trail_order)
        await asyncio.sleep(1)

        mp.trail_order_id = trail_trade.order.orderId
        mp.state          = State.TRAILING

        log.info(
            "%s: Trailing Stop placed (%.1f%%) id=%d",
            mp.symbol, self.trail_pct, mp.trail_order_id,
        )

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
        """Poll until the order is confirmed cancelled or timeout."""
        deadline = time.monotonic() + self.order_timeout
        terminal = {"Cancelled", "Inactive"}
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
