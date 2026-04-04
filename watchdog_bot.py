from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import os

import psutil
import yaml


# ── Logging setup ────────────────────────────────────────────────────────────

def setup_logging(cfg: dict, config_path: str):
    level = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_file = cfg.get("file")
    if log_file:
        log_path = Path(config_path).parent / log_file
        date_prefix = datetime.now().strftime("%Y_%m_%d")
        daily_log = log_path.parent / f"{date_prefix}_watchdog_bot.log"
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


# ── Process / port detection ──────────────────────────────────────────────────

def is_running(match: str) -> bool:
    """Return True if any running process (excluding this watchdog) has match in its command line."""
    my_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.pid == my_pid:
                continue
            cmdline = " ".join(proc.info["cmdline"] or [])
            if "watchdog" in cmdline.lower():
                continue
            if match.lower() in cmdline.lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def is_port_listening(port: int) -> bool:
    """Return True if any process is listening on the given TCP port."""
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr.port == port and conn.status == "LISTEN":
                return True
    except (psutil.AccessDenied, PermissionError):
        pass
    return False


def service_is_up(svc: dict) -> bool:
    """Check if a service is running — via port if port_check is set, else process name."""
    port = svc.get("port_check")
    if port:
        return is_port_listening(int(port))
    return is_running(svc["process_match"])


# ── Launch ────────────────────────────────────────────────────────────────────

def launch(name: str, path: str, show_window: bool = True):
    """Launch a service via its .bat file (non-blocking)."""
    bat = Path(path)
    if not bat.exists():
        log.error("%s: launch path not found — %s", name, path)
        return
    try:
        if sys.platform == "win32":
            # Invoke cmd.exe directly (no shell=True) so CREATE_NEW_CONSOLE / CREATE_NO_WINDOW
            # apply to the actual console window, not an intermediate wrapper process.
            flags = subprocess.CREATE_NEW_CONSOLE if show_window else subprocess.CREATE_NO_WINDOW
            subprocess.Popen(["cmd.exe", "/c", path], creationflags=flags)
        else:
            subprocess.Popen(path, shell=True)
        log.info("%s: launched successfully.", name)
    except Exception as exc:
        log.error("%s: failed to launch — %s", name, exc)


# ── Watchdog ──────────────────────────────────────────────────────────────────

log = logging.getLogger(__name__)


class Watchdog:
    def __init__(self, cfg: dict):
        w = cfg["watchdog"]
        self.check_interval: int   = w.get("check_interval_seconds", 60)
        self.delay_ibc_risk: int   = w.get("startup_delay_ibc_to_risk", 20)
        self.delay_risk_entry: int = w.get("startup_delay_risk_to_entry", 20)
        self.cooldown: int         = w.get("restart_cooldown_seconds", 30)
        self.monitor_delay: int    = w.get("initial_monitor_delay_seconds", 60)
        self.show_windows: bool    = w.get("show_windows", True)

        s = cfg["services"]
        self.ibc   = s["ibc"]
        self.risk  = s["risk_bot"]
        self.entry = s["entry_bot"]

        self._last_restart: dict[str, float] = {}

    def _should_restart(self, name: str) -> bool:
        last = self._last_restart.get(name, 0)
        return (time.monotonic() - last) >= self.cooldown

    def _launch(self, svc: dict):
        show = svc.get("show_window", self.show_windows)
        launch(svc["name"], svc["launch_path"], show_window=show)

    def _check_and_restart(self, svc: dict):
        name = svc["name"]

        if not svc.get("enabled", True):
            return

        if not svc.get("monitor", True):
            return

        if not service_is_up(svc):
            if self._should_restart(name):
                log.warning("%s not running — restarting…", name)
                self._launch(svc)
                self._last_restart[name] = time.monotonic()
            else:
                log.warning("%s not running — cooldown active, skipping restart.", name)

    def startup(self):
        """Ordered startup sequence: IBC → Risk Bot → Entry Bot."""
        log.info("=== Watchdog startup sequence ===")

        if self.ibc.get("enabled", True) and not service_is_up(self.ibc):
            log.info("Starting %s…", self.ibc["name"])
            self._launch(self.ibc)
            self._last_restart[self.ibc["name"]] = time.monotonic()
            log.info("Waiting %ds for IBC to initialise…", self.delay_ibc_risk)
            time.sleep(self.delay_ibc_risk)
        else:
            log.info("%s already running.", self.ibc["name"])

        if self.risk.get("enabled", True) and not service_is_up(self.risk):
            log.info("Starting %s…", self.risk["name"])
            self._launch(self.risk)
            self._last_restart[self.risk["name"]] = time.monotonic()
            log.info("Waiting %ds before starting Entry Bot…", self.delay_risk_entry)
            time.sleep(self.delay_risk_entry)
        else:
            log.info("%s already running.", self.risk["name"])

        if self.entry.get("enabled", True) and not service_is_up(self.entry):
            log.info("Starting %s…", self.entry["name"])
            self._launch(self.entry)
            self._last_restart[self.entry["name"]] = time.monotonic()
        else:
            log.info("%s already running.", self.entry["name"])

        log.info("=== Startup sequence complete ===")

    def monitor(self):
        """Continuous monitoring loop."""
        log.info("Waiting %ds before first monitor check…", self.monitor_delay)
        time.sleep(self.monitor_delay)
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
    setup_logging(cfg.get("logging", {}), args.config)

    log.info("=== IBKR Watchdog Bot started ===")

    try:
        Watchdog(cfg).run()
    except KeyboardInterrupt:
        log.info("Watchdog stopped by user.")
