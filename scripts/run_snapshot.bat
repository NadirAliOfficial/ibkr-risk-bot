@echo off
REM ============================================================
REM  IBKR Portfolio Snapshot Bot — Start Script
REM  Generates an XLSX snapshot of open positions.
REM ============================================================

cd /d "%~dp0\.."

IF NOT EXIST "venv\Scripts\activate.bat" (
    echo ERROR: Virtual environment not found. Run scripts\install_windows.bat first.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

echo [%DATE% %TIME%] Starting IBKR Portfolio Snapshot Bot...
python snapshot_bot.py --config config.yaml --output .
echo [%DATE% %TIME%] Snapshot Bot finished (code %ERRORLEVEL%).
pause
