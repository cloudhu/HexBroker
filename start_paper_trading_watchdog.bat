@echo off
rem ===========================================================================
rem Paper-trading system launcher WITH auto-restart watchdog (P2-5).
rem Wraps scripts/paper_trading_main.py: on crash the watchdog restarts it
rem with exponential backoff, up to --max-restarts consecutive crashes, then
rem stops and raises a CRITICAL alert (avoids infinite restart churn).
rem
rem The child process owns the pid lock, so repeated launches are safe
rem (a stale pid file from a crashed child is detected as a zombie and overwritten).
rem
rem Interpreter resolution mirrors start_paper_trading.bat.
rem ===========================================================================
chcp 65001 >nul
setlocal enabledelayedexpansion

cd /d "%~dp0"

if not exist "logs" mkdir "logs" >nul 2>nul

if "%PYTHONPATH%"=="" (
  set "PYTHONPATH=%CD%"
) else (
  set "PYTHONPATH=%CD%;%PYTHONPATH%"
)

set "PYEXE="
set "PYENV=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if exist "%PYENV%" (
  set "PYEXE=%PYENV%"
) else (
  where python >nul 2>nul
  if not errorlevel 1 (
    set "PYEXE=python"
  )
)

if "%PYEXE%"=="" (
  echo [ERROR] Python interpreter not found.
  pause
  exit /b 1
)

if not exist "scripts\paper_watchdog.py" (
  echo [ERROR] Missing entry script scripts\paper_watchdog.py.
  pause
  exit /b 1
)

echo [WATCHDOG] Project root: %CD%
echo [WATCHDOG] Interpreter: %PYEXE%
echo [WATCHDOG] Tip: Ctrl+C stops the watchdog (and forwards to the child).
echo [WATCHDOG] Pass-through args: %*

rem ---- Step 1: system health check ----
echo [WATCHDOG] ============================================================
echo [WATCHDOG] Step 1/2: running system health check...
echo [WATCHDOG] ============================================================
"%PYEXE%" scripts\paper_trading_main.py --health-check
echo [WATCHDOG] Health check finished.
echo.

rem ---- Step 2: start the watchdog (which supervises the trading process) ----
echo [WATCHDOG] ============================================================
echo [WATCHDOG] Step 2/2: starting watchdog (auto-restart on crash)...
echo [WATCHDOG] ============================================================
"%PYEXE%" scripts\paper_watchdog.py %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo [WATCHDOG][CRITICAL] Watchdog stopped after reaching max consecutive crashes.
  echo         Check the child's traceback above. Common causes: missing signal cache,
  echo         invalid config, unrecoverable runtime error. Fix the root cause, then re-run.
  pause
  exit /b %EXIT_CODE%
)

echo.
echo [WATCHDOG] Watchdog exited normally (child exited code 0).
pause
endlocal
