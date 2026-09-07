@echo off
rem ============================================================================
rem HexBroker 模拟盘 —— 实时日志窗口（tail viewer）
rem 双击打开一个持续滚动的命令行窗口，实时跟踪 logs/paper_console.log 的尾部。
rem 关闭窗口或按 Ctrl+C 只退出查看器，绝不影响模拟盘进程本身。
rem 模拟盘的完整历史日志同目录可见：logs/paper_console.log（引擎+看门狗）
rem ============================================================================
chcp 65001 >nul
title HexBroker 模拟盘实时日志 - logs/paper_console.log
cd /d "%~dp0.."
echo [查看器] 正在实时跟踪 logs/paper_console.log（Ctrl+C 或关窗退出，不影响模拟盘）
echo.
powershell -NoProfile -Command "Get-Content -LiteralPath 'logs\paper_console.log' -Tail 30 -Wait -Encoding UTF8"
echo.
echo [查看器已退出] 模拟盘本身仍在运行，不受影响。
pause
