#!/usr/bin/env python3
"""
Bridge Bot — reads the latest scan CSV, filters candidates by score,
and updates entry_params.json with execution_date and symbols only.
"""

import csv
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

CONFIG_PATH = "bridge_bot_config.yaml"


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def next_trading_day(from_date: date, days_ahead: int = 1) -> date:
    d = from_date
    added = 0
    while added < days_ahead:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def find_latest_csv(scan_dir: str) -> Path:
    p = Path(scan_dir)
    csvs = sorted(p.glob("scan_*.csv"), reverse=True)
    if not csvs:
        raise FileNotFoundError(f"No scan CSV found in {scan_dir}")
    return csvs[0]


def read_candidates(csv_path: Path, score_threshold: int,
                    candidate_flag_only: bool, max_symbols: int) -> list:
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if candidate_flag_only and row.get("CandidateFlag") != "TRUE":
                continue
            try:
                score = int(row["Score"])
            except (ValueError, KeyError):
                continue
            if score >= score_threshold:
                rows.append((score, row["Symbol"]))

    rows.sort(key=lambda x: x[0], reverse=True)
    return [sym for _, sym in rows[:max_symbols]]


def main():
    setup_logging()
    log = logging.getLogger(__name__)

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    if cfg.get("mode", "ON").upper() == "OFF":
        log.info("Bridge Bot is OFF. No changes made.")
        return

    scan_dir            = cfg["scan_output_dir"]
    entry_params_file   = cfg.get("entry_params_file", "entry_params.json")
    score_threshold     = int(cfg.get("score_threshold", 3))
    candidate_flag_only = bool(cfg.get("candidate_flag_only", True))
    max_symbols         = int(cfg.get("max_symbols", 5))
    trading_days_ahead  = int(cfg.get("trading_days_ahead", 1))

    try:
        csv_path = find_latest_csv(scan_dir)
    except FileNotFoundError as e:
        log.error("%s", e)
        sys.exit(1)

    log.info("Scan CSV: %s", csv_path)

    symbols = read_candidates(csv_path, score_threshold, candidate_flag_only, max_symbols)
    log.info("Candidates after filtering: %s", symbols)

    if not symbols:
        log.warning("No symbols passed the filters. entry_params.json not updated.")
        sys.exit(0)

    exec_date     = next_trading_day(date.today(), trading_days_ahead)
    exec_date_str = exec_date.strftime("%Y-%m-%d")

    ep_path = Path(entry_params_file)
    with open(ep_path) as f:
        entry_params = json.load(f)

    entry_params["execution_date"] = exec_date_str
    entry_params["symbols"]        = symbols

    with open(ep_path, "w") as f:
        json.dump(entry_params, f, indent=2)

    log.info("entry_params.json updated:")
    log.info("  execution_date = %s", exec_date_str)
    log.info("  symbols        = %s", symbols)


if __name__ == "__main__":
    main()
