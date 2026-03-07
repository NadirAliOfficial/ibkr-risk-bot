@echo off
REM ============================================================
REM  IBKR Risk Management Bot — Windows Installation Script
REM  Run once as Administrator to set up the environment.
REM ============================================================

echo === IBKR Risk Management Bot — Installation ===
echo.

REM Check Python
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python not found. Install Python 3.9+ and add it to PATH.
    pause
    exit /b 1
)

python --version

REM Create virtual environment
echo.
echo Creating virtual environment...
python -m venv venv
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to create virtual environment.
    pause
    exit /b 1
)

REM Activate and install dependencies
echo Installing dependencies...
call venv\Scripts\activate.bat
pip install --upgrade pip
pip install -r requirements.txt

IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo === Installation complete ===
echo.
echo Next steps:
echo   1. Edit config.yaml with your IBKR connection settings
echo   2. Run scripts\run.bat to start the bot
echo   3. (Optional) Run scripts\setup_service.bat to install as a Windows Service
echo.
pause
