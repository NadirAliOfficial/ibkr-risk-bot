"""
Position state machine definitions.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class State(Enum):
    NEW        = "NEW"        # Position detected; no orders placed yet
    MONITORING = "MONITORING" # TP + SL (OCA) are active; watching trigger price
    CANCELLING = "CANCELLING" # Trigger hit; waiting for TP/SL cancel confirmations
    TRAILING   = "TRAILING"   # Trailing stop is active; IBKR manages it
    CLOSED     = "CLOSED"     # Position fully closed


@dataclass
class ManagedPosition:
    """Represents a single user-opened position being managed by the bot."""

    # Immutable identity
    conid: int
    symbol: str
    sec_type: str            # STK, FUT, OPT, etc.
    exchange: str
    currency: str

    # Position details (set when position is first detected)
    quantity: float          # positive = long, negative = short
    entry_price: float

    # Orders (set after placement)
    tp_order_id: Optional[int] = None
    sl_order_id: Optional[int] = None
    trail_order_id: Optional[int] = None
    oca_group: str = ""

    # Cancellation tracking
    tp_cancelled: bool = False
    sl_cancelled: bool = False

    # State machine
    state: State = State.NEW

    # ── Derived helpers ──────────────────────────────────────────────────────

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def close_action(self) -> str:
        """Action needed to close this position."""
        return "SELL" if self.is_long else "BUY"

    @property
    def abs_qty(self) -> float:
        return abs(self.quantity)

    def tp_price(self, tp_pct: float) -> float:
        if self.is_long:
            return round(self.entry_price * (1 + tp_pct / 100), 2)
        return round(self.entry_price * (1 - tp_pct / 100), 2)

    def sl_price(self, sl_pct: float) -> float:
        if self.is_long:
            return round(self.entry_price * (1 - sl_pct / 100), 2)
        return round(self.entry_price * (1 + sl_pct / 100), 2)

    def trigger_price(self, trigger_pct: float) -> float:
        if self.is_long:
            return round(self.entry_price * (1 + trigger_pct / 100), 2)
        return round(self.entry_price * (1 - trigger_pct / 100), 2)

    def trigger_hit(self, last_price: float, trigger_pct: float) -> bool:
        """Returns True when the market price has reached the trigger threshold."""
        t = self.trigger_price(trigger_pct)
        return last_price >= t if self.is_long else last_price <= t

    def __str__(self) -> str:
        direction = "LONG" if self.is_long else "SHORT"
        return (
            f"{self.symbol} [{direction} {self.abs_qty} @ {self.entry_price:.2f}] "
            f"state={self.state.value}"
        )
