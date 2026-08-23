@echo off
rem ===========================================================================
rem 模拟盘交易系统一键启动（P0 R1：双击即启动，<=60s，失败输出中文提示）
rem 文档：docs/system_design.md §4.1 启动调用链
rem ===========================================================================
chcp 65001 >nul
setlocal enabledelayedexpansion

rem ---- 切换到项目根目录（bat 所在目录） ----
cd /d "%~dp0"

rem ---- 设置 PYTHONPATH（项目根，保证 import hexbroker 命中工作区代码） ----
if "%PYTHONPATH%"=="" (
  set "PYTHONPATH=%CD%"
) else (
  set "PYTHONPATH=%CD%;%PYTHONPATH%"
)

rem ---- 定位 Python：优先 PATH 中的 python，其次项目默认虚拟环境 ----
set "PYEXE="
where python >nul 2>nul
if not errorlevel 1 (
  set "PYEXE=python"
) else (
  set "PYENV=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
  if exist "%PYENV%" (
    set "PYEXE=%PYENV%"
  )
)

if "%PYEXE%"=="" (
  echo [错误] 未找到 Python 解释器。
  echo        请安装 Python 3.10+ 并加入 PATH，或确认默认虚拟环境存在。
  pause
  exit /b 1
)

rem ---- 检查入口脚本存在 ----
if not exist "scripts\paper_trading_main.py" (
  echo [错误] 缺少入口脚本 scripts\paper_trading_main.py，请确认工作区完整。
  pause
  exit /b 1
)

echo [模拟盘] 正在启动模拟盘交易系统（PYTHONPATH=%PYTHONPATH%）...
echo [模拟盘] 使用解释器: %PYEXE%

rem ---- 启动（透传命令行参数，如 --days 20 / --offline） ----
"%PYEXE%" scripts\paper_trading_main.py %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo [错误] 模拟盘进程异常退出（退出码 %EXIT_CODE%）。
  echo        请检查上方日志；常见原因：信号缓存缺失 / 配置非法 / 网络不可达。
  pause
  exit /b %EXIT_CODE%
)

endlocal
