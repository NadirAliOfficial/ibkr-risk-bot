# IBKR Automated Trading Suite — Interactive Brokers API (Python)

> A suite of trading bots built with Python and ib_insync — risk management, scheduled entry, system watchdog, and real-time position monitoring. Connects to IB Gateway or TWS.

---

## Bots Included

| Bot | File | Purpose |
|---|---|---|
| Risk Bot | `bot.py` | Auto-places TP / SL / Trailing Stop on every open position |
| Entry Bot | `entry_bot.py` | Schedules and executes BUY market orders at a set time |
| Watchdog Bot | `watchdog_bot.py` | Monitors and auto-restarts all bots and IBC |
| Price Monitor | `price_monitor.py` | Real-time intraday chart of open positions in the browser |

---

## Features

### Risk Bot
- **Instant TP / SL placement** — as soon as a new position is detected, a Take Profit (Limit) and Stop Loss are placed as an OCA group
- **Automatic trailing stop** — when price hits a configurable gain threshold, TP and SL are cancelled and a Trailing Stop is placed automatically
- **Smart recovery on restart** — on reconnect, the bot matches existing open orders to positions and resumes from the correct state without placing duplicate orders

### Entry Bot
- Schedules BUY market orders at a specified time (e.g. 09:31 NY)
- Cash-based position sizing with configurable allocation caps
- Hands off automatically to the Risk Bot after execution

### Watchdog Bot
- Monitors IBC, Risk Bot, and Entry Bot via TCP port checks and process detection
- Auto-restarts any service that goes down
- Per-service `show_window` control — CMD windows visible or hidden
- Entry Bot configured as `monitor: false` — launched at startup but never force-restarted
- Configurable initial delay before monitoring begins

### Price Monitor
- Connects to IB Gateway read-only with a dedicated client ID
- Fetches all open positions automatically
- Captures price snapshots every N minutes and displays a live chart at `http://localhost:8050`
- **Price mode** — raw price per symbol
- **Relative mode (Base 100)** — normalizes all positions to 100 at first snapshot for easy performance comparison
- SPY (S&P 500) and QQQ (Nasdaq) benchmark overlays in relative mode (dashed lines)
- Closed positions remain visible as dotted lines and stop updating
- New positions added mid-session appear automatically on the next poll
- X-axis auto-zooms to the actual data window

---

## Project Structure

```
ibkr-risk-bot/
├── bot.py                    # Risk Bot — connection & reconnect loop
├── entry_bot.py              # Entry Bot — scheduled position opening
├── watchdog_bot.py           # Watchdog Bot — monitors and restarts all services
├── price_monitor.py          # Price Monitor — real-time browser chart
├── config.yaml               # Risk Bot + Entry Bot configuration
├── watchdog_config.yaml      # Watchdog Bot configuration
├── price_monitor_config.yaml # Price Monitor configuration
├── entry_params.json         # Entry Bot parameters (symbols, time, allocation)
├── requirements.txt          # Python dependencies
├── src/
│   ├── bot.py                # RiskBot class — core logic & state machine
│   └── state.py              # ManagedPosition dataclass & State enum
└── scripts/
    ├── install_windows.bat   # One-time setup (creates venv, installs deps)
    ├── run.bat               # Start the Risk Bot
    ├── run_entry.bat         # Start the Entry Bot
    └── setup_service.bat     # Install Risk Bot as a Windows Service via NSSM
```

---

## How It Works

### Risk Bot (`bot.py`)

```
Position opened (manually or via Entry Bot)
        ↓
Bot detects new position
        ↓
Places Take Profit (LMT) + Stop Loss (STP) as OCA group
        ↓
Monitors market price every N seconds
        ↓
Price hits trigger threshold (+5% default)
        ↓
Cancels TP + SL → Places Trailing Stop (2% default)
        ↓
IBKR manages trailing stop automatically
```

### Entry Bot (`entry_bot.py`)

```
Read entry_params.json (symbols, date, time, allocation)
        ↓
Wait until scheduled execution time (e.g. 09:31 NY)
        ↓
Fetch available USD cash balance
        ↓
Apply cash buffer (10% default) → calculate usable cash
        ↓
Distribute evenly across symbols (cap: max_usd_per_position)
        ↓
Place BUY MKT orders → Risk Bot picks up positions automatically
```

### Watchdog Bot (`watchdog_bot.py`)

```
Start all services on launch
        ↓
Wait initial_monitor_delay_seconds
        ↓
Every check_interval_seconds:
  - IBC: check TCP port 4002 (not process name)
  - Risk Bot: check process name
  - Entry Bot: skip (monitor: false)
        ↓
If service is down → re-launch with correct window visibility
```

### Price Monitor (`price_monitor.py`)

```
Connect to IB Gateway (read-only)
        ↓
Fetch open positions
        ↓
Every poll_interval_seconds:
  - Get price for each position (last trade → bid/ask midpoint)
  - Get SPY and QQQ prices
  - Append to history
        ↓
Dash chart at localhost:8050 updates automatically
```

---

## Tech Stack

| Component | Details |
|---|---|
| Language | Python 3.10+ |
| IBKR API library | [ib_insync](https://github.com/erdewit/ib_insync) |
| Broker | Interactive Brokers (IB Gateway / TWS) |
| Order types | LMT, STP, TRAIL, MKT |
| Order grouping | OCA (One Cancels All) |
| Chart UI | Dash + Plotly (localhost, no server needed) |
| Deployment | Windows Service (NSSM), Azure VM, any VPS |
| Config format | YAML + JSON |

---

## Requirements

| Component | Version |
|---|---|
| Python | 3.10+ (recommended: 3.11 or 3.12) |
| IB Gateway or TWS | Latest stable |
| ib_insync | ≥ 0.9.86 |
| PyYAML | ≥ 6.0 |
| psutil | ≥ 5.9.0 |
| dash | ≥ 2.14.0 |
| plotly | ≥ 5.18.0 |

> **Python 3.14 is not supported.** Use Python 3.10, 3.11, or 3.12.

---

## Installation

### 1. Install IB Gateway or TWS

Download **IB Gateway** from [interactivebrokers.com](https://www.interactivebrokers.com/en/trading/ibgateway-stable.php) or use **Trader Workstation (TWS)**.

In the API settings:
- Enable **"Enable ActiveX and Socket Clients"**
- Set Socket port: `4002` (paper) or `4001` (live)
- Enable **"Allow connections from localhost only"**

### 2. Install Python

Download Python 3.11 from [python.org](https://www.python.org/downloads/).
Check **"Add Python to PATH"** during installation.

### 3. Install dependencies

**Windows (one command):**
```bat
scripts\install_windows.bat
```

**macOS / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Additional dependency for Price Monitor:**
```bat
venv\Scripts\pip install dash plotly
```

---

## Configuration

### `config.yaml` — Risk Bot + Entry Bot

```yaml
ibkr:
  host: "127.0.0.1"
  port: 4002              # 4002 = paper, 4001 = live
  client_id: 1
  entry_client_id: 2

risk:
  tp_pct: 7.0
  sl_pct: 15.0
  trigger_pct: 5.0
  trail_pct: 2.0

bot:
  poll_interval: 5
  reconnect_delay: 30
  order_timeout: 30

logging:
  level: "INFO"
  file: "bot.log"
```

### `watchdog_config.yaml` — Watchdog Bot

```yaml
show_windows: true
initial_monitor_delay_seconds: 60
check_interval_seconds: 60

services:
  - name: "IBC"
    path: "C:\\IBC\\start_ibc.bat"
    port_check: 4002
    monitor: true
    show_window: false

  - name: "Risk Bot"
    path: "C:\\ibkr-risk-bot\\scripts\\run.bat"
    process_match: " bot.py"
    monitor: true

  - name: "Entry Bot"
    path: "C:\\ibkr-risk-bot\\scripts\\run_entry.bat"
    process_match: "entry_bot.py"
    monitor: false
```

### `price_monitor_config.yaml` — Price Monitor

```yaml
host: "127.0.0.1"
port: 4002
client_id: 10
poll_interval_seconds: 120
web_port: 8050
session_start: "15:30"
session_end: "22:00"
```

### `entry_params.json` — Entry Bot

```json
{
  "execution_date": "2026-03-12",
  "execution_time": "09:31:00",
  "timezone": "America/New_York",
  "cash_buffer_percent": 10,
  "max_usd_per_position": 4000,
  "symbols": ["PANW", "GOOGL", "NVDA"]
}
```

---

## Running the Bots

### Risk Bot — runs 24/7
```bat
scripts\run.bat
```

### Entry Bot — runs once per trading day
1. Edit `entry_params.json` with tomorrow's date, execution time, and symbols
2. Run:
```bat
scripts\run_entry.bat
```

> Start the **Risk Bot first**, then the Entry Bot. Both run simultaneously using different `clientId` values.

### Watchdog Bot — monitors and auto-restarts everything
```bat
venv\Scripts\python watchdog_bot.py
```

### Price Monitor — live browser chart
```bat
venv\Scripts\python price_monitor.py
```
Then open `http://localhost:8050` in your browser (opens automatically).

---

## Deployment on Azure VM (Windows Server)

For 24/7 automated trading, deploy on a dedicated cloud VM.

### Step 1 — Provision a VM
- Create a **Windows Server 2022** VM (Standard B2s or larger)
- Open inbound port **3389** (RDP)

### Step 2 — Install software via RDP
1. Install **IB Gateway** + **IBC** for auto-login
2. Install **Python 3.11**
3. Copy the `ibkr-risk-bot` folder to `C:\ibkr-risk-bot`
4. Run `scripts\install_windows.bat` as Administrator
5. Configure `config.yaml` and `watchdog_config.yaml`

### Step 3 — Install Risk Bot as a Windows Service (NSSM)
1. Download `nssm.exe` from [nssm.cc](https://nssm.cc/download) → place at `C:\nssm\nssm.exe`
2. Run `scripts\setup_service.bat` as Administrator

```bat
sc start  IBKRRiskBot
sc stop   IBKRRiskBot
sc query  IBKRRiskBot
```

### Step 4 — Launch Watchdog on startup
Add `watchdog_bot.py` to Windows Task Scheduler to run at logon — it will start IBC, Risk Bot, and Entry Bot automatically.

---

## State Machine (Risk Bot)

```
NEW → MONITORING → CANCELLING → TRAILING → (closed)
         ↑
    NEEDS_TP (recovery: SL found, TP missing → auto-placed on next tick)
```

| State | Description |
|---|---|
| `NEW` | Position detected, no orders placed yet |
| `NEEDS_TP` | Recovered with SL only — TP placed on next tick |
| `MONITORING` | TP + SL (OCA) active, bot monitors price |
| `CANCELLING` | Trigger hit, cancelling TP + SL before trailing stop |
| `TRAILING` | Trailing stop active, IBKR manages it |
| `CLOSED` | Position closed, removed from tracking |

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `Connection failed` | Ensure IB Gateway / TWS is running with API enabled |
| `open orders request timed out` | Harmless on startup — ib_insync retries automatically |
| `entry_price not available` | avgCost is 0 on first tick — bot retries on next poll |
| IBC detected as down every minute | IBC's `.bat` exits after launching TWS — use `port_check: 4002` instead of process name |
| CMD windows not visible | Set `show_windows: true` in `watchdog_config.yaml` |
| SPY not resolving via API | Ensure ARCA (Network B) market data subscription is active in IBKR |
| Error 10089 (market data) | Paper account without subscription — orders still work |
| Error 1100 | IB Gateway session dropped — bot reconnects automatically |
| Service not starting | Check `bot_error.log`; verify Python and venv paths in NSSM config |
| Python version error | Use Python 3.10 / 3.11 / 3.12 — Python 3.14 is not supported |

---

## Security

- The Risk Bot only places **closing orders** (LMT, STP, TRAIL) — it cannot open new positions
- The Entry Bot places **BUY MKT orders only**
- The Price Monitor is **read-only** — no orders placed
- Keep IB Gateway configured to **allow connections from localhost only**
- Never expose IB Gateway port to the public internet
- Store your IBKR credentials outside the repository

---

## Built By

**Team NAK | Nadir Ali Khan**
[theteamnak.com](https://theteamnak.com)

> Experienced in Interactive Brokers API, algorithmic trading bots, ib_insync, TWS API, IB Gateway automation, Python trading systems, and cloud deployment on Azure / AWS.
> Available for custom IBKR bot development — risk management, order execution, portfolio automation, and more.
