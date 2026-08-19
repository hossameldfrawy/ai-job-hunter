@echo off
REM ===================================================================
REM  AI JOB HUNTER -- Mission Control dashboard
REM
REM  Read-only. Opening, watching and closing this window cannot start,
REM  stop or change the bot -- the databases are opened mode=ro, which
REM  SQLite enforces. Ctrl-C closes the dashboard, not the hunter.
REM ===================================================================
setlocal

REM UTF-8 codepage. Without it the console renders every Arabic job title,
REM company name and reply as mojibake -- and Arabic is most of them.
chcp 65001 >/dev/null 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

cd /d "%~dp0.."
title AI Job Hunter -- Mission Control

where python >/dev/null 2>&1
if errorlevel 1 (
  echo(
  echo   Python is not on PATH. Install it, or open this from a shell
  echo   where "python" resolves.
  echo(
  pause
  exit /b 1
)

python scripts\monitor.py %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo(
  echo   Dashboard exited with code %RC%.
  pause
)
endlocal
