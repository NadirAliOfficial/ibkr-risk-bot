@echo off
REM ============================================================
REM  IBKR Entry Bot — Start Script
REM  Edit entry_params.json before running.
REM ============================================================

cd /d "%~dp0\.."

IF NOT EXIST "venv\Scripts\activate.bat" (
    echo ERROR: Virtual environment not found. Run scripts\install_windows.bat first.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

echo [%DATE% %TIME%] Starting IBKR Entry Bot...
python entry_bot.py --config config.yaml --params entry_params.json
echo [%DATE% %TIME%] Entry Bot finished (code %ERRORLEVEL%).
pause
