# ============================================================================
# HexBroker 模拟盘 —— 注册三个 Windows 计划任务（08:55 / 13:25 / 20:55）
#
# 用途：让模拟盘看门狗在**沙箱会话之外**的用户会话里长期存活。
# 背景：由自动化沙箱会话拉起的进程会在该会话结束时被进程树回收（2026-09-07 取证，
#       四组 Windows 创建标志均无法规避）；计划任务由 Task Scheduler 服务拉起，
#       不属于任何沙箱会话的进程树，故能长期存活。
#
# 用法：右键 → 使用 PowerShell 运行；或双击同目录 register_paper_schtasks.bat
#
# ⚠️ 本脚本**不需要密码**：以「仅当用户登录时运行」(LogonType Interactive) 注册，
#    不存储凭据。若需要「不管用户是否登录都要运行」，把下面 $LogonType 改成 S4U
#    并用 -User/-Password 提供凭据（那会存储密码，按需自决）。
# ============================================================================
$ErrorActionPreference = "Stop"

$Root   = Split-Path -Parent $PSScriptRoot          # scripts\ 的上级 = 仓库根
$Py     = "C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
$Script = Join-Path $Root "scripts\paper_watchdog_schtask.py"
$Times  = @("08:55", "13:25", "20:55")
$LogonType = "Interactive"

if (-not (Test-Path $Py))     { throw "找不到解释器: $Py" }
if (-not (Test-Path $Script)) { throw "找不到入口脚本: $Script" }

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType $LogonType -RunLevel Limited
# StartWhenAvailable：开机错过了触发点（例如 08:55 时机器没开）→ 补跑，不必等下一天
# ExecutionTimeLimit：3 天，避免任务被 72h 默认上限之外的短限制杀掉
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Days 3)

foreach ($t in $Times) {
    $name = "HexBroker_Paper_" + ($t -replace ":", "")
    $action  = New-ScheduledTaskAction -Execute $Py -Argument ('"{0}"' -f $Script) -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Daily -At $t
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
                           -Principal $principal -Settings $settings -Force | Out-Null
    Write-Host "[OK] 已注册  $name   每日 $t  ->  $Py `"$Script`""
}

Write-Host ""
Write-Host "完成。可用以下命令核对："
foreach ($t in $Times) {
    $name = "HexBroker_Paper_" + ($t -replace ":", "")
    Write-Host "  schtasks /query /tn $name /fo LIST /v"
}
Write-Host "立即触发一次做验证："
Write-Host "  schtasks /run /tn HexBroker_Paper_0855"
Write-Host "卸载见同目录 unregister_paper_schtasks.bat"
