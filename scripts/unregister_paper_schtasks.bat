@echo off
rem ============================================================================
rem HexBroker 模拟盘 —— 卸载三个 Windows 计划任务（完整回滚）
rem
rem 等价单条命令（可手敲，无需本文件）：
rem   schtasks /delete /tn HexBroker_Paper_0855 /f
rem   schtasks /delete /tn HexBroker_Paper_1325 /f
rem   schtasks /delete /tn HexBroker_Paper_2055 /f
rem
rem ⚠️ 删除任务**不会**杀掉已经在跑的看门狗/引擎。若要连进程一起停掉，
rem    另外执行（先在任务管理器确认 pid，或用下面两行按名筛）：
rem     wmic process where "name='python.exe'" get ProcessId,CommandLine
rem     taskkill /PID <看门狗pid> /T /F
rem    注意：引擎是看门狗的子进程但**不挂控制台**，/T 未必能带上它；
rem          不确定时优先 taskkill 引擎自身 pid，避免留下孤儿占着单实例锁
rem          （孤儿引擎会让后续所有计划任务被预检跳过 = 系统永远起不来）。
rem ============================================================================
chcp 65001 >nul

for %%T in (0855 1325 2055) do (
  echo 删除任务 HexBroker_Paper_%%T ...
  schtasks /delete /tn "HexBroker_Paper_%%T" /f
)

echo.
echo 已卸载全部三个计划任务。
echo 仍存活的看门狗/引擎进程不会自动停止，请按文件头注释处理。
pause
