@echo off
REM ===================================================================
REM  AI JOB HUNTER -- one-click launcher
REM
REM  1. checks Python and the dependencies that actually matter
REM  2. starts the supervised services, if they are not already up
REM  3. opens Mission Control
REM
REM  Closing this window does NOT stop the bot: the services run under
REM  detached supervisors. Stop them with scripts\stop_hunter.ps1.
REM ===================================================================
setlocal EnableDelayedExpansion

REM UTF-8 first, before anything prints. The job titles are Arabic.
chcp 65001 >/dev/null 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

cd /d "%~dp0"
title AI Job Hunter -- Launcher
color 0F
mode con: cols=112 lines=42

echo(
echo   ================================================================
echo     A I   J O B   H U N T E R      starting up
echo   ================================================================
echo(

REM -- 1. interpreter ------------------------------------------------
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if exist "venv\Scripts\python.exe"  set "PY=venv\Scripts\python.exe"

"%PY%" --version >/dev/null 2>&1
if errorlevel 1 (
  echo   [X] Python was not found.
  echo       Install Python 3.11+ and re-run, or create a virtualenv here.
  echo(
  pause
  exit /b 1
)
for /f "delims=" %%v in ("%PY%" --version 2^>^&1) do echo   [ok] %%v  ^(%PY%^)

REM -- 2. dependencies ------------------------------------------------
"%PY%" scripts\preflight.py
if errorlevel 2 (
  echo(
  echo   Install what is missing, then run this again:
  echo       "%PY%" -m pip install -r requirements.txt
  echo(
  pause
  exit /b 1
)

REM -- 3. services ----------------------------------------------------
echo(
echo   Starting supervised services ^(detached^)...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\run_service.ps1" -Service listener -Detached
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\run_service.ps1" -Service hunter -Detached -IntervalMinutes 60

REM Give the supervisors a moment to write their first log lines, so the
REM dashboard opens with real content instead of an empty activity pane.
timeout /t 4 /nobreak >/dev/null 2>&1

REM -- 4. mission control ---------------------------------------------
echo(
echo   Opening Mission Control.  Ctrl-C closes the DASHBOARD only --
echo   the bot keeps running. To stop it:
echo       powershell -ExecutionPolicy Bypass -File scripts\stop_hunter.ps1
echo(
timeout /t 2 /nobreak >/dev/null 2>&1

title AI Job Hunter -- Mission Control
"%PY%" scripts\monitor.py

echo(
echo   Dashboard closed. The bot is still running in the background.
echo   Re-open it any time with:  scripts\start_hunter_dashboard.bat
echo(
pause
endlocal
