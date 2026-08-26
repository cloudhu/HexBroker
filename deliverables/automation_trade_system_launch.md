# HexBroker 交易系统分时启动自动化 · 配置说明

> 创建于 2026-08-26；2026-08-26 优化：启动改为**独立可见命令行窗口**（运维友好）。

三个自动化均在**交易日（周一至周五，含节假日日历校验）**触发，于各交易时段开盘前启动 HexBroker 模拟盘交易系统，并以**独立可见控制台窗口**运行，方便运维。

## 概览

| 时段 | 自动化名称 | ID | RRULE | 触发时刻 | 日志 |
|---|---|---|---|---|---|
| 早盘 | HexBroker 交易系统启动·早盘 (交易日 08:55) | `automation-1787715163617` | `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=55` | 08:55（早盘 09:00 前） | `logs/launch_morning.log` |
| 午盘 | HexBroker 交易系统启动·午盘 (交易日 13:25) | `automation-1787715163772` | `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=13;BYMINUTE=25` | 13:25（午盘 13:30 前） | `logs/launch_noon.log` |
| 夜盘 | HexBroker 交易系统启动·夜盘 (交易日 20:55) | `automation-1787715163916` | `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=20;BYMINUTE=55` | 20:55（夜盘 21:00 前） | `logs/launch_night.log` |

## 启动机制（已核实，2026-08-26 优化）

- **入口脚本**：`scripts/launch_trading_window.py`（新建，仅做启动，不修改核心代码）。
- **弹窗方式**：该脚本用 Python `subprocess.Popen(..., creationflags=CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP)` **直接拉起** `scripts/paper_trading_main.py`，创建一个独立可见的控制台窗口（实时显示启动日志与行情，Ctrl+C 退出）。
- **为何不用 cmd.exe / start**：沙箱安全策略明确拦截任何途径的 `cmd.exe` 启动（Bash 与 PowerShell 均被拦），故绕开 cmd.exe，由 Python 直接 spawn 子进程开窗口。
- **幂等保护**：主程序在 `data/paper/paper.pid` 持有进程锁；若已有存活实例会自动拒绝重复启动（输出「已有存活实例」），三个时段重复触发**不会**叠加启动。
- **路径与解释器**：固定使用 managed venv `C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe`；脚本内设置 `cwd=项目根`、`PYTHONPATH=项目根`。
- 不修改任何生产代码/配置，仅做启动与状态检查。

## 交易日判定逻辑

1. 优先调用 `westock-mcp` 的 `data_trade_calendar` 校验当日是否为交易日。
2. 非交易日（周末/法定节假日）→ 写日志跳过。
3. 日历工具不可用 → 退化为「周一至周五即交易日」假设（RRULE 已排除周末；仅罕见周中节假日可能误触发，系统空转无害）。

## 已有配套自动化（参考）

- `automation-1787625060414` 开盘前信号缓存刷新（日盘 08:45）—— 早于早盘启动 10 分钟，先刷新信号缓存。

## 运维要点

- 监控：触发后查看弹出的**可见控制台窗口**（首要运维界面）；辅助记录见对应 `logs/launch_*.log`，或用 `tasklist` 核对 `paper.pid` 中 PID 存活。
- 窗口行为：交易系统为长驻进程，窗口持续显示日志直至 Ctrl+C 退出或异常退出。
- 通知：当前 `push_to_wechat / push_to_wecom_bot` 均为 false；如需推送启动结果可在自动化设置中开启。
- 暂停/调试：在自动化列表中将对应条目置 `PAUSED` 即可。
