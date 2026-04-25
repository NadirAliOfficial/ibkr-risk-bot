#!/usr/bin/env python3
"""
IBKR Position Price Monitor
Connects to IB Gateway, fetches open positions, and displays a real-time
price chart at http://localhost:8050. Read-only — no orders placed.
"""

import math
import sys
import threading
import webbrowser
from collections import defaultdict
from datetime import datetime, date, timedelta

import yaml
from ib_insync import IB, Stock
import dash
from dash import dcc, html
from dash.dependencies import Input, Output
import plotly.graph_objects as go

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = "price_monitor_config.yaml"

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

cfg = load_config()
HOST          = cfg.get("host", "127.0.0.1")
PORT          = cfg.get("port", 4002)
CLIENT_ID     = cfg.get("client_id", 10)
POLL_SEC      = cfg.get("poll_interval_seconds", 120)
WEB_PORT      = cfg.get("web_port", 8050)
SESSION_START = cfg.get("session_start", "15:30")
SESSION_END   = cfg.get("session_end", "22:00")

BENCHMARKS = {
    "SPY": {"color": "#444444", "label": "SPY (S&P 500)", "primaryExch": "ARCA"},
    "QQQ": {"color": "#888888", "label": "QQQ (Nasdaq)",  "primaryExch": "NASDAQ"},
}

# ── Shared state ──────────────────────────────────────────────────────────────

price_history:     dict[str, list[tuple[datetime, float]]] = defaultdict(list)
benchmark_history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
closed_symbols: set[str] = set()
state_lock = threading.Lock()

ib = IB()

# ── IBKR helpers ──────────────────────────────────────────────────────────────

def open_position_contracts() -> dict[str, object]:
    positions = ib.positions()
    return {p.contract.symbol: p.contract for p in positions if p.position != 0}

def _valid(val) -> bool:
    try:
        return val is not None and not math.isnan(val) and val > 0
    except TypeError:
        return False

def _get_price(ticker) -> float | None:
    if _valid(ticker.last):
        return ticker.last
    if _valid(ticker.bid) and _valid(ticker.ask):
        return (ticker.bid + ticker.ask) / 2
    return None

def fetch_prices(contracts: dict) -> dict[str, float]:
    prices = {}
    for symbol, contract in contracts.items():
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(2.0)
        price = _get_price(ticker)
        if price:
            prices[symbol] = price
        ib.cancelMktData(contract)
    return prices

def fetch_benchmark_prices() -> dict[str, float]:
    prices = {}
    for symbol, meta in BENCHMARKS.items():
        contract = Stock(symbol, "SMART", "USD")
        contract.primaryExch = meta["primaryExch"]
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(2.0)
        price = _get_price(ticker)
        if price:
            prices[symbol] = price
        ib.cancelMktData(contract)
    return prices

# ── Polling loop (runs in main thread via ib.sleep) ───────────────────────────

def poll():
    while True:
        try:
            open_contracts = open_position_contracts()
            open_symbols = set(open_contracts.keys())
            prices = fetch_prices(open_contracts)
            bench_prices = fetch_benchmark_prices()
            now = datetime.now()

            with state_lock:
                tracked = set(price_history.keys())
                for sym in tracked - open_symbols:
                    closed_symbols.add(sym)
                for sym, price in prices.items():
                    price_history[sym].append((now, price))
                for sym, price in bench_prices.items():
                    benchmark_history[sym].append((now, price))

        except Exception as exc:
            print(f"[poll] error: {exc}", flush=True)

        ib.sleep(POLL_SEC)

# ── Dash app ──────────────────────────────────────────────────────────────────

app = dash.Dash(__name__, update_title=None)
app.title = "Position Monitor"

app.layout = html.Div(
    style={"fontFamily": "Arial, sans-serif", "padding": "20px", "backgroundColor": "#fafafa"},
    children=[
        html.Div(
            style={"display": "flex", "alignItems": "center", "marginBottom": "4px", "gap": "24px"},
            children=[
                html.H2("Open Position Price Monitor", style={"margin": "0"}),
                dcc.RadioItems(
                    id="display-mode",
                    options=[
                        {"label": "Price", "value": "price"},
                        {"label": "Relative (Base 100)", "value": "relative"},
                    ],
                    value="price",
                    inline=True,
                    style={"fontSize": "14px"},
                    inputStyle={"marginRight": "4px"},
                    labelStyle={"marginRight": "16px"},
                ),
            ],
        ),
        html.P(
            id="last-update",
            style={"color": "#888", "fontSize": "13px", "marginBottom": "16px"}
        ),
        dcc.Graph(
            id="price-chart",
            style={"height": "540px"},
            config={"displayModeBar": False}
        ),
        dcc.Interval(id="interval", interval=POLL_SEC * 1000, n_intervals=0),
    ]
)

@app.callback(
    Output("price-chart", "figure"),
    Output("last-update", "children"),
    Input("interval", "n_intervals"),
    Input("display-mode", "value"),
)
def update_chart(_n, display_mode):
    fig = go.Figure()

    today = date.today()
    session_start_dt = datetime.combine(today, datetime.strptime(SESSION_START, "%H:%M").time())
    session_end_dt   = datetime.combine(today, datetime.strptime(SESSION_END,   "%H:%M").time())

    with state_lock:
        history_snapshot   = {k: list(v) for k, v in price_history.items()}
        benchmark_snapshot = {k: list(v) for k, v in benchmark_history.items()}
        closed_snapshot    = set(closed_symbols)

    all_times = [t for hist in history_snapshot.values() for t, _ in hist]

    if all_times:
        data_min = min(all_times)
        data_max = max(all_times)
        span = max((data_max - data_min).total_seconds(), 300)
        pad  = timedelta(seconds=span * 0.15)
        x_start = max(session_start_dt, data_min - pad)
        x_end   = min(session_end_dt,   data_max + pad)
    else:
        x_start = session_start_dt
        x_end   = session_end_dt

    # ── Position traces ───────────────────────────────────────────────────────
    for symbol, history in history_snapshot.items():
        if not history:
            continue
        is_closed = symbol in closed_snapshot
        times  = [h[0] for h in history]
        prices = [h[1] for h in history]

        if display_mode == "relative":
            base = prices[0]
            y_values = [round((p / base) * 100, 2) for p in prices]
        else:
            y_values = prices

        label = f"{symbol} (closed)" if is_closed else symbol
        fig.add_trace(go.Scatter(
            x=times,
            y=y_values,
            mode="lines",
            name=label,
            line=dict(dash="dot" if is_closed else "solid", width=2),
            opacity=0.5 if is_closed else 1.0,
        ))

    # ── Benchmark traces (relative mode only) ────────────────────────────────
    if display_mode == "relative":
        for symbol, history in benchmark_snapshot.items():
            if not history:
                continue
            times  = [h[0] for h in history]
            prices = [h[1] for h in history]
            base   = prices[0]
            y_values = [round((p / base) * 100, 2) for p in prices]
            meta = BENCHMARKS[symbol]
            fig.add_trace(go.Scatter(
                x=times,
                y=y_values,
                mode="lines",
                name=meta["label"],
                line=dict(dash="dash", width=1.5, color=meta["color"]),
                opacity=0.8,
            ))

    y_title = "Base 100 (% relative)" if display_mode == "relative" else "Price"
    interval_label = f"{POLL_SEC}s" if POLL_SEC < 60 else f"{POLL_SEC // 60} min"

    fig.update_layout(
        xaxis=dict(
            title="Time (ET)",
            type="date",
            range=[x_start, x_end],
            tickformat="%H:%M",
        ),
        yaxis=dict(title=y_title),
        legend=dict(title="Symbol", orientation="v"),
        template="plotly_white",
        margin=dict(l=60, r=20, t=20, b=60),
        hovermode="x unified",
    )

    last = datetime.now().strftime("%H:%M:%S")
    return fig, f"Last updated: {last}  —  refreshes every {interval_label}"

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    print(f"Connecting to IB Gateway at {HOST}:{PORT} (clientId={CLIENT_ID}) …")
    try:
        ib.connect(HOST, PORT, clientId=CLIENT_ID, readonly=True)
    except Exception as exc:
        print(f"Connection failed: {exc}")
        sys.exit(1)

    print("Connected. Running initial poll …")
    poll()


if __name__ == "__main__":
    dash_thread = threading.Thread(
        target=lambda: app.run(
            host="127.0.0.1",
            port=WEB_PORT,
            debug=False,
            use_reloader=False,
        ),
        daemon=True,
    )
    dash_thread.start()

    def open_browser():
        import time
        time.sleep(2)
        webbrowser.open(f"http://localhost:{WEB_PORT}")
    threading.Thread(target=open_browser, daemon=True).start()

    print(f"Chart available at http://localhost:{WEB_PORT}")
    main()
