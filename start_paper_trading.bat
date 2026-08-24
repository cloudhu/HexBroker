@echo off
rem ===========================================================================
rem Paper-trading system one-click launcher (P0 R1: double-click to start,
rem <=60s; runtime messages are printed in Chinese by the Python app itself).
rem Docs: docs/system_design.md section 4.1 startup chain.
rem
rem [Launcher hardening]
rem  - Interpreter resolution: (1) known-good managed venv python first
rem                            (2) fallback to PATH python
rem  - Window never auto-closes: pause on both error and normal exit, so a
rem    crash is always visible instead of a silent flash on launch.
rem  - On failure, run this .bat from an already-open cmd to see full traceback.
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
  echo         Expected: %PYENV%
  echo         Install Python 3.10+ and add to PATH, or ensure the default venv exists.
  pause
  exit /b 1
)

if not exist "scripts\paper_trading_main.py" (
  echo [ERROR] Missing entry script scripts\paper_trading_main.py. Please verify workspace.
  pause
  exit /b 1
)

echo [PAPER] Project root: %CD%
echo [PAPER] Interpreter: %PYEXE%
echo [PAPER] Tip: window stays open while running (live polling). Stop with Ctrl+C.

rem ---- Step 1: system health check (data-source / modules / lifecycle) ----
echo [PAPER] ============================================================
echo [PAPER] Step 1/2: running system health check...
echo [PAPER] ============================================================
"%PYEXE%" scripts\paper_trading_main.py --health-check
echo [PAPER] Health check finished. See report above (also logs/health_check.log).
echo.

rem ---- Step 2: start the paper-trading system ----
echo [PAPER] ============================================================
echo [PAPER] Step 2/2: starting paper-trading system...
echo [PAPER] ============================================================
"%PYEXE%" scripts\paper_trading_main.py %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] Paper-trading process exited abnormally.
  echo         Check the log above. Common causes: missing signal cache / invalid config / network unreachable / missing dependency.
  echo         To see the full Python traceback, run this .bat from an open cmd window.
  pause
  exit /b %EXIT_CODE%
)

echo.
echo [PAPER] Process exited normally (code 0).
pause
endlocal
