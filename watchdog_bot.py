from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil
import yaml


# ── Logging setup ────────────────────────────────────────────────────────────

def setup_logging(cfg: dict):
    level = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_file = cfg.get("file")
    if log_file:
        date_prefix = datetime.now().strftime("%Y_%m_%d")
        daily_log = Path(log_file).parent / f"{date_prefix}_watchdog_bot.log"
        handlers.append(logging.FileHandler(daily_log, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ── Process detection ─────────────────────────────────────────────────────────

def is_running(match: str) -> bool:
    """Return True if any running process has match in its command line."""
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
            if match.lower() in cmdline.lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def launch(name: str, path: str):
    """Launch a service via its .bat file (non-blocking)."""
    log = logging.getLogger(__name__)
    try:
        subprocess.Popen(
            [path],
            shell=True,
            creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
        )
        log.info("%s: launched successfully.", name)
    except Exception as exc:
        log.error("%s: failed to launch — %s", name, exc)


# ── Watchdog ──────────────────────────────────────────────────────────────────

log = logging.getLogger(__name__)


class Watchdog:
    def __init__(self, cfg: dict):
        w = cfg["watchdog"]
        self.check_interval: int  = w.get("check_interval_seconds", 60)
        self.delay_ibc_risk: int  = w.get("startup_delay_ibc_to_risk", 20)
        self.delay_risk_entry: int = w.get("startup_delay_risk_to_entry", 20)
        self.cooldown: int        = w.get("restart_cooldown_seconds", 30)

        s = cfg["services"]
        self.ibc   = s["ibc"]
        self.risk  = s["risk_bot"]
        self.entry = s["entry_bot"]

        # Track last restart time per service to enforce cooldown
        self._last_restart: dict[str, float] = {}

    def _should_restart(self, name: str) -> bool:
        last = self._last_restart.get(name, 0)
        return (time.monotonic() - last) >= self.cooldown

    def _check_and_restart(self, svc: dict):
        name  = svc["name"]
        match = svc["process_match"]
        path  = svc["launch_path"]

        if not svc.get("enabled", True):
            return

        if not is_running(match):
            if self._should_restart(name):
                log.warning("%s not running — restarting…", name)
                launch(name, path)
                self._last_restart[name] = time.monotonic()
            else:
                log.warning("%s not running — cooldown active, skipping restart.", name)

    def startup(self):
        """Ordered startup sequence: IBC → Risk Bot → Entry Bot."""
        log.info("=== Watchdog startup sequence ===")

        if self.ibc.get("enabled", True) and not is_running(self.ibc["process_match"]):
            log.info("Starting %s…", self.ibc["name"])
            launch(self.ibc["name"], self.ibc["launch_path"])
            self._last_restart[self.ibc["name"]] = time.monotonic()
            log.info("Waiting %ds for IBC to initialise…", self.delay_ibc_risk)
            time.sleep(self.delay_ibc_risk)
        else:
            log.info("%s already running.", self.ibc["name"])

        if self.risk.get("enabled", True) and not is_running(self.risk["process_match"]):
            log.info("Starting %s…", self.risk["name"])
            launch(self.risk["name"], self.risk["launch_path"])
            self._last_restart[self.risk["name"]] = time.monotonic()
            log.info("Waiting %ds before starting Entry Bot…", self.delay_risk_entry)
            time.sleep(self.delay_risk_entry)
        else:
            log.info("%s already running.", self.risk["name"])

        if self.entry.get("enabled", True) and not is_running(self.entry["process_match"]):
            log.info("Starting %s…", self.entry["name"])
            launch(self.entry["name"], self.entry["launch_path"])
            self._last_restart[self.entry["name"]] = time.monotonic()
        else:
            log.info("%s already running.", self.entry["name"])

        log.info("=== Startup sequence complete ===")

    def monitor(self):
        """Continuous monitoring loop."""
        log.info("Monitoring started (check every %ds, cooldown %ds).",
                 self.check_interval, self.cooldown)
        while True:
            try:
                self._check_and_restart(self.ibc)
                self._check_and_restart(self.risk)
                self._check_and_restart(self.entry)
            except Exception as exc:
                log.error("Unexpected error in monitor loop: %s", exc, exc_info=True)
            time.sleep(self.check_interval)

    def run(self):
        self.startup()
        self.monitor()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IBKR Watchdog Bot")
    parser.add_argument("--config", default="watchdog_config.yaml",
                        help="Path to watchdog config file")
    args = parser.parse_args()

    if not Path(args.config).exists():
        print(f"Config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    cfg = load_config(args.config)
    setup_logging(cfg.get("logging", {}))

    log.info("=== IBKR Watchdog Bot started ===")

    try:
        Watchdog(cfg).run()
    except KeyboardInterrupt:
        log.info("Watchdog stopped by user.")
