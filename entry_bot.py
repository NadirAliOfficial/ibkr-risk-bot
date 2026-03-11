from __future__ import annotations

"""
IBKR Entry Bot — Release 2
Reads a JSON parameter file and places BUY market orders at the scheduled time.
The Risk Management Bot (bot.py) handles TP / SL / trailing stop automatically.
"""

import argparse
import asyncio
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from ib_insync import IB, Contract, Order, util


# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging(cfg: dict):
    level = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_file = cfg.get("file")
    if log_file:
        # Write entry bot logs to a separate file
        entry_log = log_file.replace("bot.log", "entry_bot.log") if "bot.log" in log_file else f"entry_{log_file}"
        handlers.append(logging.FileHandler(entry_log, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    logging.getLogger("ib_insync").setLevel(logging.WARNING)


# ── Config / params loading ───────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_params(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ── IB helpers ────────────────────────────────────────────────────────────────

def make_contract(symbol: str) -> Contract:
    c = Contract()
    c.symbol   = symbol
    c.secType  = "STK"
    c.exchange = "SMART"
    c.currency = "USD"
    return c


def get_cash_balance(ib: IB) -> float:
    """Return available USD cash from account summary."""
    for av in ib.accountValues():
        if av.tag == "AvailableFunds" and av.currency == "USD":
            return float(av.value)
    return 0.0


def get_last_price(ib: IB, contract: Contract) -> float | None:
    """Request a snapshot price and return last / close price."""
    ib.reqMarketDataType(4)  # delayed-frozen, works without subscription
    ticker = ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
    # Wait up to 5 seconds for snapshot data
    for _ in range(10):
        ib.sleep(0.5)
        price = ticker.last
        if price and price == price and price > 0:  # not NaN
            ib.cancelMktData(contract)
            return price
        price = ticker.close
        if price and price == price and price > 0:
            ib.cancelMktData(contract)
            return price
    ib.cancelMktData(contract)
    return None


def has_open_position(ib: IB, symbol: str) -> bool:
    for pos in ib.positions():
        if pos.contract.symbol == symbol and pos.position != 0:
            return True
    return False


def has_pending_order(ib: IB, symbol: str) -> bool:
    for trade in ib.openTrades():
        if trade.contract.symbol == symbol:
            return True
    return False


# ── Core execution logic ──────────────────────────────────────────────────────

async def run_entry(ib: IB, params: dict):
    log = logging.getLogger(__name__)

    tz          = ZoneInfo(params.get("timezone", "America/New_York"))
    exec_date   = params["execution_date"]          # "YYYY-MM-DD"
    exec_time   = params["execution_time"]          # "HH:MM:SS"
    symbols     = params["symbols"]
    buffer_pct  = float(params.get("cash_buffer_percent", 10))
    max_usd     = float(params.get("max_usd_per_position", 4000))

    # ── Wait until scheduled execution time ──────────────────────────────────
    target_dt = datetime.fromisoformat(f"{exec_date}T{exec_time}").replace(tzinfo=tz)
    now       = datetime.now(tz=tz)

    if now >= target_dt:
        log.warning("Scheduled time %s is in the past — executing immediately.", target_dt)
    else:
        wait_secs = (target_dt - now).total_seconds()
        log.info("Waiting %.0f seconds until %s (%s)…", wait_secs, exec_time, params["timezone"])
        await asyncio.sleep(wait_secs)

    log.info("=== Entry Bot execution started ===")
    log.info("Symbols    : %s", symbols)
    log.info("Max/pos    : $%.2f", max_usd)
    log.info("Buffer     : %.1f%%", buffer_pct)

    # ── Cash calculation ──────────────────────────────────────────────────────
    ib.reqAccountUpdates(True)
    await asyncio.sleep(2)  # let account data populate

    available_cash = get_cash_balance(ib)
    buffer_amt     = available_cash * (buffer_pct / 100)
    usable_cash    = available_cash - buffer_amt

    log.info("Available cash : $%.2f", available_cash)
    log.info("Buffer (%.1f%%) : $%.2f", buffer_pct, buffer_amt)
    log.info("Usable cash    : $%.2f", usable_cash)

    if usable_cash <= 0:
        log.error("No usable cash available. Aborting.")
        return

    # ── Capital distribution ──────────────────────────────────────────────────
    n_symbols  = len(symbols)
    alloc_each = min(usable_cash / n_symbols, max_usd)
    log.info("Symbols count  : %d", n_symbols)
    log.info("Allocation/sym : $%.2f", alloc_each)

    # ── Fetch all open orders once ────────────────────────────────────────────
    ib.reqAllOpenOrders()
    await asyncio.sleep(1)

    # ── Place orders ──────────────────────────────────────────────────────────
    for symbol in symbols:
        log.info("─── Processing %s ───", symbol)

        if has_open_position(ib, symbol):
            log.warning("%s skipped — position already exists.", symbol)
            continue

        if has_pending_order(ib, symbol):
            log.warning("%s skipped — pending order already exists.", symbol)
            continue

        contract = make_contract(symbol)

        # Qualify contract so IBKR assigns a conId
        try:
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                log.error("%s skipped — could not qualify contract.", symbol)
                continue
            contract = qualified[0]
        except Exception as exc:
            log.error("%s skipped — contract qualification error: %s", symbol, exc)
            continue

        price = get_last_price(ib, contract)
        if price is None:
            log.error("%s skipped — could not retrieve market price.", symbol)
            continue

        log.info("%s market price : $%.4f", symbol, price)

        quantity = math.floor(alloc_each / price)
        if quantity < 1:
            log.warning(
                "%s skipped — allocation too small for 1 share "
                "(alloc=$%.2f price=$%.4f).",
                symbol, alloc_each, price,
            )
            continue

        order_value = quantity * price
        log.info("%s quantity      : %d shares (~$%.2f)", symbol, quantity, order_value)

        order = Order(
            action        = "BUY",
            orderType     = "MKT",
            totalQuantity = quantity,
            transmit      = True,
        )

        try:
            trade = ib.placeOrder(contract, order)
            await asyncio.sleep(1)
            log.info(
                "%s order placed  : orderId=%d status=%s",
                symbol, trade.order.orderId, trade.orderStatus.status,
            )
        except Exception as exc:
            log.error("%s order failed   : %s", symbol, exc)

    ib.reqAccountUpdates(False)
    log.info("=== Entry Bot execution complete ===")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(cfg: dict, params: dict):
    log = logging.getLogger(__name__)
    ic  = cfg["ibkr"]

    ib = IB()
    try:
        ib.connect(
            host     = ic["host"],
            port     = ic["port"],
            clientId = ic.get("entry_client_id", 2),  # different clientId from risk bot
            timeout  = 20,
            readonly = False,
        )
        log.info(
            "Connected to IB Gateway at %s:%s (clientId=%s)",
            ic["host"], ic["port"], ic.get("entry_client_id", 2),
        )
    except Exception as exc:
        log.error("Connection failed: %s", exc)
        sys.exit(1)

    try:
        await run_entry(ib, params)
    except asyncio.CancelledError:
        log.info("Entry bot stopped by user.")
    except Exception as exc:
        log.error("Fatal error: %s", exc, exc_info=True)
    finally:
        ib.disconnect()
        log.info("Disconnected.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IBKR Entry Bot")
    parser.add_argument("--config", default="config.yaml",  help="Path to YAML config")
    parser.add_argument("--params", default="entry_params.json", help="Path to entry params JSON")
    args = parser.parse_args()

    if not Path(args.config).exists():
        print(f"Config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)
    if not Path(args.params).exists():
        print(f"Params file not found: {args.params}", file=sys.stderr)
        sys.exit(1)

    cfg    = load_config(args.config)
    params = load_params(args.params)

    setup_logging(cfg.get("logging", {}))
    util.patchAsyncio()

    try:
        asyncio.run(main(cfg, params))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Stopped by user.")
