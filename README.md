# IBKR Automated Risk Management Bot — Interactive Brokers API (Python)

> Automatically protect every trade with Take Profit, Stop Loss, and Trailing Stop orders — the moment you open a position in Interactive Brokers.

Built with **Python** and the **IBKR API (ib_insync)**, this bot connects to **IB Gateway** or **Trader Workstation (TWS)** and manages risk for your positions in real time — no manual order entry required.

---

## Features

- **Instant TP / SL placement** — as soon as a new position is detected, a Take Profit (Limit) and Stop Loss are placed as an OCA (One Cancels All) group
- **Automatic trailing stop** — when price hits a configurable gain threshold, TP and SL are cancelled and a Trailing Stop is placed automatically
- **Smart recovery on restart** — on reconnect, the bot matches existing open orders to positions and resumes from the correct state without placing duplicate orders
- **Entry Bot (Release 2)** — companion bot that schedules and executes BUY market orders at a specified time (e.g. 09:31 NY), with cash-based position sizing and configurable allocation caps
- **Crash-safe auto-restart** — included Windows batch script restarts the bot automatically if it exits
- **Windows Service deployment** — install as a persistent background service via NSSM for 24/7 operation on a cloud VM
- **Full audit log** — every order placed, cancelled, and filled is logged to `bot.log`
- **Paper and live account support** — works with IB Gateway paper (port 4002) and live (port 4001)
- **All parameters configurable** — no code changes needed; everything is set in `config.yaml`

---

## How It Works

### Risk Management Bot (`bot.py`)

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
Calculate integer shares: floor(allocation / market_price)
        ↓
Place BUY MKT orders → Risk Bot picks up positions automatically
```

---

## Project Structure

```
ibkr-risk-bot/
├── bot.py                  # Risk Bot entry point — connection & reconnect loop
├── entry_bot.py            # Entry Bot — scheduled position opening
├── config.yaml             # Shared configuration for both bots
├── entry_params.json       # Entry Bot parameters (symbols, time, allocation)
├── requirements.txt        # Python dependencies
├── src/
│   ├── bot.py              # RiskBot class — core logic & state machine
│   └── state.py            # ManagedPosition dataclass & State enum
└── scripts/
    ├── install_windows.bat # One-time setup (creates venv, installs deps)
    ├── run.bat             # Start the Risk Bot (with auto-restart loop)
    ├── run_entry.bat       # Start the Entry Bot
    └── setup_service.bat   # Install Risk Bot as a Windows Service via NSSM
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
| Deployment | Windows Service (NSSM), Azure VM, any VPS |
| Config format | YAML (`config.yaml`) + JSON (`entry_params.json`) |

---

## Requirements

| Component | Version |
|---|---|
| Python | 3.10+ (recommended: 3.11 or 3.12) |
| IB Gateway or TWS | Latest stable |
| ib_insync | ≥ 0.9.86 |
| PyYAML | ≥ 6.0 |
| tzdata | Latest (Windows only) |

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
pip install tzdata   # remove timezone warnings
```

---

## Configuration

### `config.yaml`

```yaml
ibkr:
  host: "127.0.0.1"
  port: 4002              # 4002 = paper, 4001 = live
  client_id: 1            # Risk Bot client ID
  entry_client_id: 2      # Entry Bot client ID (must be different)

risk:
  tp_pct: 7.0             # Take Profit % above entry price
  sl_pct: 15.0            # Stop Loss % below entry price
  trigger_pct: 5.0        # Gain % at which TP/SL switch to trailing stop
  trail_pct: 2.0          # Trailing Stop %

bot:
  poll_interval: 5        # Seconds between portfolio scans
  reconnect_delay: 30     # Seconds before reconnect attempt
  order_timeout: 30       # Seconds to wait for order cancel confirmation

logging:
  level: "INFO"           # DEBUG | INFO | WARNING | ERROR
  file: "bot.log"
```

### `entry_params.json`

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

**Windows:**
```bat
scripts\run.bat
```

**macOS / Linux:**
```bash
source venv/bin/activate
python bot.py --config config.yaml
```

### Entry Bot — runs once per trading day

1. Edit `entry_params.json` with tomorrow's date, execution time, and symbols
2. Run:

**Windows:**
```bat
scripts\run_entry.bat
```

**macOS / Linux:**
```bash
python entry_bot.py --config config.yaml --params entry_params.json
```

The Entry Bot waits until the scheduled time, executes all BUY orders, then exits. The Risk Bot automatically detects the new positions and places TP/SL within seconds.

> Start the **Risk Bot first**, then the Entry Bot. Both run simultaneously using different `clientId` values.

---

## Deployment on Azure VM (Windows Server)

For 24/7 automated trading, deploy on a dedicated cloud VM.

### Step 1 — Provision a VM

- Create a **Windows Server 2022** VM (Standard B2s or larger)
- Open inbound port **3389** (RDP)
- Run IB Gateway and the bot on the same VM (localhost communication)

### Step 2 — Install software via RDP

1. Install **IB Gateway**
2. Install **Python 3.11**
3. Copy the `ibkr-risk-bot` folder to `C:\ibkr-risk-bot`
4. Run `scripts\install_windows.bat` as Administrator
5. Configure `config.yaml`

### Step 3 — Install as a Windows Service (NSSM)

The Risk Bot runs as a persistent background service that survives reboots and auto-restarts on crash.

1. Download `nssm.exe` from [nssm.cc](https://nssm.cc/download) → place at `C:\nssm\nssm.exe`
2. Run `scripts\setup_service.bat` as Administrator
3. The service `IBKRRiskBot` is now installed and running

```bat
sc start  IBKRRiskBot
sc stop   IBKRRiskBot
sc query  IBKRRiskBot
```

### Step 4 — Auto-login IB Gateway with IBC

Use **IBC** to keep IB Gateway logged in automatically after reboots and session drops.

- IBC on GitHub: [github.com/IbcAlpha/IBC](https://github.com/IbcAlpha/IBC)
- Or add IB Gateway to Windows **Task Scheduler** on system startup

### Step 5 — Keep VM always on

In Azure Portal → your VM → **Auto-shutdown** → Disable.

---

## State Machine

Each position moves through the following states:

```
NEW → MONITORING → CANCELLING → TRAILING → (closed)
         ↑
    NEEDS_TP (recovery: SL found, TP missing → auto-placed on next tick)
```

| State | Description |
|---|---|
| `NEW` | Position detected, no orders placed yet |
| `NEEDS_TP` | Recovered from previous session with SL only — TP placed on next tick |
| `MONITORING` | TP + SL (OCA) active, bot monitors market price |
| `CANCELLING` | Trigger hit, cancelling TP and SL before placing trailing stop |
| `TRAILING` | Trailing stop active, IBKR manages it automatically |
| `CLOSED` | Position closed, removed from tracking |

---

## Recovery on Restart

When the bot starts or reconnects after a disconnect, it:

1. Calls `reqAllOpenOrders()` to fetch orders from **all sessions** (not just the current one)
2. Matches open orders to current positions by contract ID
3. Resumes each position in the correct state:
   - Trailing stop found → `TRAILING`
   - OCA orders found (TP + SL) → `MONITORING`
   - SL only found → `NEEDS_TP` (fresh TP placed on next tick)
   - No orders found → `NEW` (fresh TP + SL placed)
4. Before switching to trailing stop, sweeps **all residual LMT/STP orders** to ensure a clean slate

No positions are left unprotected after a restart.

---

## Sample Log Output

```
2026-03-11 09:31:00  INFO     Connected to IB Gateway at 127.0.0.1:4002 (clientId=1)
2026-03-11 09:31:02  INFO     RECOVERY: AAPL [LONG 50 @ 211.30] → MONITORING (TP=101 SL=102)
2026-03-11 09:31:05  INFO     New position detected: NVDA [LONG 20 @ 875.40] state=NEW
2026-03-11 09:31:06  INFO     NVDA: TP placed @ 936.68 (id=103)  SL placed @ 743.09 (id=104)  OCA=OCA_NVDA_1741688466
2026-03-11 10:14:33  INFO     NVDA trigger reached (last=919.17 trigger=919.17). Switching to trailing.
2026-03-11 10:14:33  INFO     NVDA: Cancelling TP order 103…
2026-03-11 10:14:34  INFO     NVDA: TP order 103 cancelled.
2026-03-11 10:14:34  INFO     NVDA: Cancelling SL order 104…
2026-03-11 10:14:35  INFO     NVDA: SL order 104 cancelled.
2026-03-11 10:14:35  INFO     NVDA: Trailing Stop placed (2.0%) id=105
```

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `Connection failed` | Ensure IB Gateway / TWS is running with API enabled |
| `open orders request timed out` | Harmless on startup — ib_insync retries automatically |
| `entry_price not available` | avgCost is 0 on first tick — bot retries on next poll |
| TP not visible in IBKR | Ensure you are on the latest version — older versions had a transmit bug (fixed) |
| Recovery shows all positions as NEW | Ensure `reqAllOpenOrders` is called — fixed in current version |
| Error 10089 (market data) | Paper account without data subscription — orders still work; trigger monitoring uses delayed data |
| Error 1100 | IB Gateway session dropped — bot reconnects automatically |
| Service not starting | Check `bot_error.log`; verify Python and venv paths in NSSM config |
| Python version error | Use Python 3.10 / 3.11 / 3.12 — Python 3.14 is not supported |

---

## Security

- The Risk Bot only places **closing orders** (LMT, STP, TRAIL) — it cannot open new positions
- The Entry Bot places **BUY MKT orders only** — no account access beyond what is needed
- Keep IB Gateway configured to **allow connections from localhost only**
- Never expose IB Gateway port to the public internet
- Store your IBKR credentials outside the repository

---

## Built By

**Team NAK | Nadir Ali Khan**
[theteamnak.com](https://theteamnak.com)

> Experienced in Interactive Brokers API, algorithmic trading bots, ib_insync, TWS API, IB Gateway automation, Python trading systems, and cloud deployment on Azure / AWS.
> Available for custom IBKR bot development — risk management, order execution, portfolio automation, and more.
