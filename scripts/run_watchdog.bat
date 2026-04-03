@echo off
REM ============================================================
REM  IBKR Watchdog Bot — Start Script
REM  Monitors and auto-restarts IBC, Risk Bot, and Entry Bot.
REM  Add this to Windows Task Scheduler to run on VPS login.
REM ============================================================

cd /d "%~dp0\.."

IF NOT EXIST "venv\Scripts\activate.bat" (
    echo ERROR: Virtual environment not found. Run scripts\install_windows.bat first.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

:restart
echo [%DATE% %TIME%] Starting IBKR Watchdog Bot...
python watchdog_bot.py --config watchdog_config.yaml
echo [%DATE% %TIME%] Watchdog exited (code %ERRORLEVEL%). Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto restart
