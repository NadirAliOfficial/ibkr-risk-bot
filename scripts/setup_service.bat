@echo off
REM ============================================================
REM  IBKR Risk Management Bot — Windows Service Setup
REM  Uses NSSM (Non-Sucking Service Manager).
REM
REM  Prerequisites:
REM    - Run as Administrator
REM    - Download nssm.exe from https://nssm.cc/download
REM      and place it in C:\nssm\nssm.exe  (or adjust path below)
REM ============================================================

SET SERVICE_NAME=IBKRRiskBot
SET NSSM=C:\nssm\nssm.exe
SET BOT_DIR=%~dp0..
SET RUN_SCRIPT=%BOT_DIR%\scripts\run.bat

IF NOT EXIST "%NSSM%" (
    echo ERROR: nssm.exe not found at %NSSM%
    echo Download from https://nssm.cc/download and place at %NSSM%
    pause
    exit /b 1
)

echo Installing Windows Service: %SERVICE_NAME%
echo Bot directory: %BOT_DIR%

REM Remove existing service if present
%NSSM% stop  %SERVICE_NAME% >nul 2>&1
%NSSM% remove %SERVICE_NAME% confirm >nul 2>&1

REM Install service
%NSSM% install %SERVICE_NAME% "%RUN_SCRIPT%"
%NSSM% set     %SERVICE_NAME% AppDirectory   "%BOT_DIR%"
%NSSM% set     %SERVICE_NAME% DisplayName    "IBKR Risk Management Bot"
%NSSM% set     %SERVICE_NAME% Description    "Automatically manages TP/SL/Trailing Stop for IBKR positions"
%NSSM% set     %SERVICE_NAME% Start          SERVICE_AUTO_START
%NSSM% set     %SERVICE_NAME% AppStdout      "%BOT_DIR%\bot.log"
%NSSM% set     %SERVICE_NAME% AppStderr      "%BOT_DIR%\bot_error.log"
%NSSM% set     %SERVICE_NAME% AppRotateFiles 1
%NSSM% set     %SERVICE_NAME% AppRotateBytes 10485760

REM Start the service
%NSSM% start %SERVICE_NAME%

echo.
echo Service installed and started: %SERVICE_NAME%
echo.
echo Useful commands:
echo   sc start  %SERVICE_NAME%
echo   sc stop   %SERVICE_NAME%
echo   sc query  %SERVICE_NAME%
echo   %NSSM% edit %SERVICE_NAME%
echo.
pause
