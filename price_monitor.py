#!/usr/bin/env python3
"""
IBKR Position Price Monitor
Connects to IB Gateway, fetches open positions, and displays a real-time
price chart at http://localhost:8050. Read-only — no orders placed.
"""

import sys
import threading
import webbrowser
from collections import defaultdict
from datetime import datetime, date

import yaml
from ib_insync import IB, util
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
HOST        = cfg.get("host", "127.0.0.1")
PORT        = cfg.get("port", 4002)
CLIENT_ID   = cfg.get("client_id", 10)
POLL_SEC    = cfg.get("poll_interval_seconds", 120)
WEB_PORT    = cfg.get("web_port", 8050)
SESSION_START = cfg.get("session_start", "15:30")
SESSION_END   = cfg.get("session_end", "22:00")

# ── Shared state ──────────────────────────────────────────────────────────────

price_history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
closed_symbols: set[str] = set()
state_lock = threading.Lock()

ib = IB()

# ── IBKR helpers ──────────────────────────────────────────────────────────────

def open_position_contracts() -> dict[str, object]:
    positions = ib.positions()
    return {p.contract.symbol: p.contract for p in positions if p.position != 0}

def fetch_prices(contracts: dict) -> dict[str, float]:
    prices = {}
    for symbol, contract in contracts.items():
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(0.3)
        price = ticker.last if (ticker.last and ticker.last > 0) else ticker.close
        if price and price > 0:
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
            now = datetime.now()

            with state_lock:
                tracked = set(price_history.keys())
                # positions that disappeared → mark closed
                for sym in tracked - open_symbols:
                    closed_symbols.add(sym)
                # record new prices
                for sym, price in prices.items():
                    price_history[sym].append((now, price))

        except Exception as exc:
            print(f"[poll] error: {exc}", flush=True)

        ib.sleep(POLL_SEC)

# ── Dash app ──────────────────────────────────────────────────────────────────

app = dash.Dash(__name__, update_title=None)
app.title = "Position Monitor"

app.layout = html.Div(
    style={"fontFamily": "Arial, sans-serif", "padding": "20px", "backgroundColor": "#fafafa"},
    children=[
        html.H2("Open Position Price Monitor", style={"marginBottom": "4px"}),
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
)
def update_chart(_n):
    fig = go.Figure()

    today = date.today()
    x_start = f"{today} {SESSION_START}:00"
    x_end   = f"{today} {SESSION_END}:00"

    with state_lock:
        history_snapshot = {k: list(v) for k, v in price_history.items()}
        closed_snapshot  = set(closed_symbols)

    for symbol, history in history_snapshot.items():
        if not history:
            continue
        is_closed = symbol in closed_snapshot
        times  = [h[0] for h in history]
        prices = [h[1] for h in history]
        label  = f"{symbol} (closed)" if is_closed else symbol
        fig.add_trace(go.Scatter(
            x=times,
            y=prices,
            mode="lines+markers",
            name=label,
            line=dict(dash="dot" if is_closed else "solid", width=2),
            opacity=0.5 if is_closed else 1.0,
            marker=dict(size=5),
        ))

    fig.update_layout(
        xaxis=dict(
            title="Time (ET)",
            range=[x_start, x_end],
            tickformat="%H:%M",
        ),
        yaxis=dict(title="Price"),
        legend=dict(title="Symbol", orientation="v"),
        template="plotly_white",
        margin=dict(l=60, r=20, t=20, b=60),
        hovermode="x unified",
    )

    last = datetime.now().strftime("%H:%M:%S")
    return fig, f"Last updated: {last}  —  refreshes every {POLL_SEC // 60} min"

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    print(f"Connecting to IB Gateway at {HOST}:{PORT} (clientId={CLIENT_ID}) …")
    try:
        ib.connect(HOST, PORT, clientId=CLIENT_ID, readonly=True)
    except Exception as exc:
        print(f"Connection failed: {exc}")
        sys.exit(1)

    print("Connected. Running initial poll …")
    poll()  # first snapshot immediately (won't return — uses ib.sleep loop)


if __name__ == "__main__":
    # Dash server runs in background thread
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

    # Open browser after a short delay
    def open_browser():
        import time
        time.sleep(2)
        webbrowser.open(f"http://localhost:{WEB_PORT}")
    threading.Thread(target=open_browser, daemon=True).start()

    print(f"Chart available at http://localhost:{WEB_PORT}")
    main()
