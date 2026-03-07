#!/usr/bin/env python3
"""
IBKR Risk Management Bot — entry point.

Usage
─────
    python bot.py [--config config.yaml]

The bot connects to IB Gateway, monitors the portfolio for manually opened
positions, and automatically manages TP / SL / Trailing Stop orders.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

import yaml
from ib_insync import IB, util

from src.bot import RiskBot


# ── Logging setup ────────────────────────────────────────────────────────────

def setup_logging(cfg: dict):
    level = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_file = cfg.get("file")
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

    # Suppress ib_insync internal noise (position dumps, order verbose logs,
    # market data subscription notices, farm connection messages, etc.)
    # Real errors from ib_insync (WARNING and above) still come through.
    logging.getLogger("ib_insync").setLevel(logging.WARNING)


# ── Config loading ───────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ── Connection ───────────────────────────────────────────────────────────────

def connect(ib: IB, cfg: dict) -> bool:
    ic = cfg["ibkr"]
    try:
        ib.connect(
            host     = ic["host"],
            port     = ic["port"],
            clientId = ic["client_id"],
            timeout  = 20,
            readonly = False,
        )
        logging.getLogger(__name__).info(
            "Connected to IB Gateway at %s:%s (clientId=%s)",
            ic["host"], ic["port"], ic["client_id"],
        )
        return True
    except Exception as exc:
        logging.getLogger(__name__).error("Connection failed: %s", exc)
        return False


# ── Main loop with reconnect ─────────────────────────────────────────────────

async def main(cfg: dict):
    log = logging.getLogger(__name__)
    reconnect_delay: int = cfg["bot"]["reconnect_delay"]

    while True:
        ib = IB()

        # Try to connect; retry indefinitely on failure
        while not connect(ib, cfg):
            log.info("Retrying connection in %d seconds…", reconnect_delay)
            await asyncio.sleep(reconnect_delay)

        bot = RiskBot(ib, cfg)

        # Register disconnection handler to break the inner loop
        disconnected = asyncio.Event()

        def on_disconnected():
            log.warning("Disconnected from IB Gateway.")
            disconnected.set()

        ib.disconnectedEvent += on_disconnected

        try:
            # Run bot and disconnect watcher concurrently
            bot_task  = asyncio.create_task(bot.run_forever())
            disc_task = asyncio.create_task(disconnected.wait())

            done, pending = await asyncio.wait(
                {bot_task, disc_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            if disconnected.is_set():
                log.info("Reconnecting in %d seconds…", reconnect_delay)
                await asyncio.sleep(reconnect_delay)

        except asyncio.CancelledError:
            log.info("Bot shutdown requested.")
            break
        except Exception as exc:
            log.error("Fatal error: %s", exc, exc_info=True)
            log.info("Restarting in %d seconds…", reconnect_delay)
            await asyncio.sleep(reconnect_delay)
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IBKR Risk Management Bot")
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )
    args = parser.parse_args()

    if not Path(args.config).exists():
        print(f"Config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    cfg = load_config(args.config)
    setup_logging(cfg.get("logging", {}))

    util.patchAsyncio()   # make ib_insync work with standard asyncio

    try:
        asyncio.run(main(cfg))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Stopped by user.")
