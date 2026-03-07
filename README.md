# IBKR Risk Management Bot

A Python bot that connects to Interactive Brokers (IB Gateway) and automatically manages risk for positions you open manually.

## What it does

1. You open a position manually in IBKR Desktop or Mobile.
2. The bot detects the new position.
3. It immediately places a **Take Profit** (+7%) and **Stop Loss** (−15%) linked with an **OCA** (One Cancels All) group.
4. It watches the market price. When price rises **+5%** above entry:
   - Cancels the TP order and waits for confirmation.
   - Cancels the SL order and waits for confirmation.
   - Places a **2% Trailing Stop** — IBKR then manages it automatically.

All percentages are configurable in `config.yaml`.

---

## Project Structure

```
ibkr-risk-bot/
├── bot.py                  # Entry point — connection, reconnect loop
├── config.yaml             # Configuration (edit before running)
├── requirements.txt        # Python dependencies
├── src/
│   ├── bot.py              # RiskBot class — core logic & state machine
│   └── state.py            # ManagedPosition dataclass & State enum
└── scripts/
    ├── install_windows.bat # One-time setup (creates venv, installs deps)
    ├── run.bat             # Start the bot (with auto-restart loop)
    └── setup_service.bat   # Install as a Windows Service via NSSM
```

---

## Requirements

| Component | Version |
|---|---|
| Python | 3.9 + |
| IB Gateway | Latest (paper or live) |
| ib_insync | ≥ 0.9.86 |
| PyYAML | ≥ 6.0 |

---

## Installation

### 1. Install IB Gateway

Download and install **IB Gateway** from [Interactive Brokers](https://www.interactivebrokers.com/en/trading/ibgateway-stable.php).

- Use **Paper Trading** credentials for testing.
- In IB Gateway settings → API → Settings:
  - Enable **"Enable ActiveX and Socket Clients"**
  - Set **Socket port** to `4002` (paper) or `4001` (live)
  - Enable **"Allow connections from localhost only"**

### 2. Install Python

Download Python 3.9+ from [python.org](https://www.python.org/downloads/).
During installation, check **"Add Python to PATH"**.

### 3. Install bot dependencies

Open a terminal in the `ibkr-risk-bot` folder and run:

**Windows:**
```bat
scripts\install_windows.bat
```

**macOS / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Configuration

Edit `config.yaml` before starting the bot:

```yaml
ibkr:
  host: "127.0.0.1"   # IB Gateway host
  port: 4002           # 4002 = paper, 4001 = live
  client_id: 1         # Unique integer; change if running multiple bots

risk:
  tp_pct: 7.0          # Take Profit percent above entry
  sl_pct: 15.0         # Stop Loss percent below entry
  trigger_pct: 5.0     # Price gain % to switch to trailing stop
  trail_pct: 2.0       # Trailing Stop percent

bot:
  poll_interval: 5     # Seconds between portfolio scans
  reconnect_delay: 30  # Seconds to wait before reconnecting
  order_timeout: 30    # Seconds to wait for order cancel confirmation

logging:
  level: "INFO"        # DEBUG | INFO | WARNING | ERROR
  file: "bot.log"      # Set to null to disable file logging
```

> **Never commit a config with live account credentials to a public repository.**
> Use `config.local.yaml` (git-ignored) for sensitive overrides.

---

## Running the bot

### Manual start

**Windows:**
```bat
scripts\run.bat
```

**macOS / Linux:**
```bash
source venv/bin/activate
python bot.py --config config.yaml
```

The `run.bat` script includes an auto-restart loop — if the bot crashes it restarts after 10 seconds.

### Custom config path

```bash
python bot.py --config /path/to/my_config.yaml
```

---

## Deployment on Azure VM (Windows)

### Step 1 — Provision a VM

- Create a **Windows Server** VM in Azure (Standard B2s or larger is sufficient).
- Open inbound port **3389** (RDP) to connect.
- Keep IB Gateway and the bot on the **same VM** so they communicate over localhost.

### Step 2 — Install software on the VM

Via RDP:
1. Install **IB Gateway** (see Installation above).
2. Install **Python 3.9+**.
3. Copy the `ibkr-risk-bot` folder to the VM (e.g. `C:\ibkr-risk-bot`).
4. Run `scripts\install_windows.bat` as Administrator.
5. Edit `config.yaml`.

### Step 3 — Install as a Windows Service (NSSM)

Using NSSM ensures the bot starts automatically when the VM boots and restarts if it crashes.

1. Download **nssm.exe** from [nssm.cc](https://nssm.cc/download) and place it at `C:\nssm\nssm.exe`.
2. Run `scripts\setup_service.bat` **as Administrator**.
3. The service `IBKRRiskBot` will be installed and started.

Useful service commands:
```bat
sc start  IBKRRiskBot
sc stop   IBKRRiskBot
sc query  IBKRRiskBot
```

### Step 4 — Auto-start IB Gateway on boot

IB Gateway must be running before the bot can connect.

- Configure IB Gateway to auto-login using **IBC** ([github.com/IbcAlpha/IBC](https://github.com/IbcAlpha/IBC)).
- Or add IB Gateway to Windows **Task Scheduler** triggered on system startup.

### Step 5 — Enable VM auto-start

In the Azure Portal:
- Navigate to your VM → **Auto-shutdown** → disable auto-shutdown.
- Under **Availability + scale** → consider **Azure VM Start/Stop** or a schedule if cost matters.

---

## State Machine

Each position moves through these states:

```
NEW → MONITORING → CANCELLING → TRAILING → (position closed)
```

| State | Meaning |
|---|---|
| `NEW` | Position detected; no orders placed yet |
| `MONITORING` | TP + SL (OCA) active; bot watches market price |
| `CANCELLING` | Trigger price hit; cancelling TP and SL |
| `TRAILING` | Trailing stop active; IBKR manages it |
| `CLOSED` | Position closed; no longer tracked |

---

## Recovery on restart

When the bot starts (or reconnects), it:

1. Fetches all current positions from IBKR.
2. Fetches all open orders.
3. Matches orders to positions:
   - Existing OCA orders → resumes in `MONITORING` state.
   - Existing trailing stop → resumes in `TRAILING` state.
   - No orders → places new TP/SL orders (`NEW` → `MONITORING`).

This means the bot can be safely restarted without losing risk management coverage.

---

## Logging

Logs are written to both stdout and `bot.log` (configurable).

Example output:
```
2026-03-07 10:01:00  INFO      __main__   Connected to IB Gateway at 127.0.0.1:4002
2026-03-07 10:01:05  INFO      src.bot    New position detected: AAPL [LONG 10 @ 175.50] state=NEW
2026-03-07 10:01:06  INFO      src.bot    AAPL: TP placed @ 187.79 (id=1)  SL placed @ 149.18 (id=2)  OCA=OCA_AAPL_1741341666
2026-03-07 10:15:22  INFO      src.bot    AAPL trigger reached (last=184.28 trigger=184.28). Switching to trailing.
2026-03-07 10:15:22  INFO      src.bot    AAPL: Cancelling TP order 1…
2026-03-07 10:15:23  INFO      src.bot    AAPL: TP order 1 cancelled.
2026-03-07 10:15:23  INFO      src.bot    AAPL: Cancelling SL order 2…
2026-03-07 10:15:24  INFO      src.bot    AAPL: SL order 2 cancelled.
2026-03-07 10:15:24  INFO      src.bot    AAPL: Trailing Stop placed (2.0%) id=3
```

---

## Testing with Paper Trading

1. Set `port: 4002` in `config.yaml`.
2. Log in to IB Gateway with paper trading credentials.
3. Start the bot.
4. Open a test position in IBKR Trader Workstation or the mobile app.
5. Confirm in the logs that TP and SL orders appear.
6. Confirm orders appear under **Open Orders** in TWS.
7. Test trigger by manually editing the position's average cost in a test scenario, or wait for market movement.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `Connection failed` | Ensure IB Gateway is running and API is enabled in its settings |
| `entry_price not available` | avgCost may be 0 on first tick; bot retries automatically |
| Orders placed with wrong prices | Check that `entry_price` in logs matches your actual fill price |
| Bot not detecting position | Confirm position shows in TWS Portfolio tab; check `poll_interval` |
| Service not starting | Check `bot_error.log`; verify Python and venv paths in NSSM config |

---

## Security Notes

- The bot only places **closing orders** (Limit, Stop, Trailing Stop). It cannot open new positions.
- Keep IB Gateway configured to **allow connections from localhost only**.
- Do not expose IB Gateway port to the public internet.
- Store credentials outside the repository (environment variables or a separate secrets file).
