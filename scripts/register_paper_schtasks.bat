@echo off
rem ============================================================================
rem HexBroker 模拟盘 —— 注册三个 Windows 计划任务（08:55 / 13:25 / 20:55）
rem
rem 双击本文件即可（内部转调同目录 register_paper_schtasks.ps1，
rem -ExecutionPolicy Bypass 仅作用于本次调用，不改变系统策略）。
rem
rem 注册的命令由 Task Scheduler 服务拉起，进程树不属于任何沙箱会话，
rem 这是 2026-09-07 取证后确认的唯一长期存活路径。
rem 卸载：见 unregister_paper_schtasks.bat
rem ============================================================================
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_paper_schtasks.ps1"
echo.
echo 退出码: %ERRORLEVEL%
pause
