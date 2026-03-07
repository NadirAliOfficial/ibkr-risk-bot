@echo off
REM ============================================================
REM  IBKR Risk Management Bot — Start Script
REM  Double-click or call from Task Scheduler / NSSM.
REM ============================================================

cd /d "%~dp0\.."

IF NOT EXIST "venv\Scripts\activate.bat" (
    echo ERROR: Virtual environment not found. Run scripts\install_windows.bat first.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

:restart
echo [%DATE% %TIME%] Starting IBKR Risk Management Bot...
python bot.py --config config.yaml
echo [%DATE% %TIME%] Bot exited (code %ERRORLEVEL%). Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto restart
