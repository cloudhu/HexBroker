# 2026-09-07 创建标志组合真值表实验结论（根治 window-CLOSE）

- 实验脚本：`artifacts/_tmp/probe_console_flags.py`（v3，一次性，不入包）
- 原始数据：`deliverables/2026-09-07_console_flags_truth_table.json`
- 生产解释器：`C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
  （与 `scripts/register_paper_schtasks.ps1` 的 `$Py` 同一路径；实测为 **venv 重定向器**：
  `popen_pid ≠ 真实子进程 pid`，Popen 的 creationflags 只作用在重定向器上）
- 安全：探测子进程自足脚本，不 import 引擎、不碰生产配置、不碰单实例锁；生产实例全程未接触；
  CTRL_BREAK 只做**定向**投递（绝不 pid=0 广播——v2 教训：pid=0 会命中共享 caller console 的所有进程）。

## 真值表（父进程基线：GetConsoleWindow=1247342，console 上共 11 个进程）

| 组合 | flags | 子进程 GetConsoleWindow | 子进程 console 进程数 | 定向 CTRL_BREAK 送达 | 判定 |
|---|---|---|---|---|---|
| C0 继承 | 0x0 | 1247342（=父） | 7 | n/a（无组可投） | 挂父 console，可收 CTRL_CLOSE |
| C1 NPG | 0x200 | 1247342（=父） | 7 | **送达** | 挂父 console，可收 CTRL_CLOSE |
| C2 NO_WINDOW+NPG（旧 tier-3） | 0x8000200 | **0** | 2 | 未送达 | ✅ **无窗口 console** |
| C3 DETACHED+NPG | 0x208 | **4064166（新窗）** | 1 | 未送达 | ❌ 真解释器重新获得带窗 console |
| C4 DETACHED+NO_WINDOW+NPG「嫌疑冲突」 | 0x8000208 | **4129702（新窗）** | 1 | 未送达 | ❌ 同上 |
| C5 BREAKAWAY+NPG | 0x1000200 | 1247342（=父） | 10 | **送达** | 挂父 console，可收 CTRL_CLOSE |
| C6 BREAKAWAY+NO_WINDOW+NPG | 0x9000200 | **0** | 2 | 未送达 | ✅ **无窗口 console** |
| C7 DETACHED+BREAKAWAY+NPG | 0x1000208 | **1247242（新窗）** | 1 | 未送达 | ❌ 重新获得带窗 console |
| C8 tier-1 全家桶（旧） | 0x9000208 | **4326310（新窗）** | 1 | 未送达 | ❌ 重新获得带窗 console |

## 结论（回答「哪个组合产出收不到 CTRL_CLOSE_EVENT 的子进程」）

1. **唯一可行家族 = CREATE_NO_WINDOW 且不含 DETACHED_PROCESS**（C6=0x9000200、C2=0x8000200）：
   子进程 `GetConsoleWindow()==0`——console 没有窗口，无人能「关窗口」，
   物理上收不到 CTRL_CLOSE_EVENT（也不收任何 CTRL_*_EVENT）。
2. **DETACHED_PROCESS(0x8) 被定罪并全链禁用**：生产链路是
   `Popen(重定向器) → 重定向器再拉起真解释器`，creationflags 不会传给真解释器；
   重定向器无 console 后，Windows 给真解释器**新建一条带窗口的 console**
   （C3/C4/C7/C8 实测 `GetConsoleWindow()≠0`）→ 引擎重新可被 window-CLOSE 杀死。
   这正是旧 tier-1（0x9000208）拦不住 `forrtl: error (200): window-CLOSE` 的根因。
   DETACHED 与 CREATE_NO_WINDOW 并非「叠加表达彻底脱离」，而是互相拆台。
3. CREATE_BREAKAWAY_FROM_JOB 对窗口归属无影响（C5/C6 与 C1/C2 窗口形态一致），
   仅用于脱离 Job Object；Job 未授权时 CreateProcess 会失败 → 只放降级链第 1 级，失败即降级。
4. 无窗口 console 下 stdout/stderr **文件重定向完全可用**（C2/C6 实测 STDOUT-OK/STDERR-OK），
   引擎日志不受影响。
5. 无任何组合可行时才需要 pythonw.exe——本实验存在可行组合（第 1 条），故**不需要** pythonw 方案。

## 落地的修复（本次 commit）

- `scripts/launch_trading_window.py::_engine_creationflags_tiers()` 重排为
  `0x9000200 → 0x8000200 → 0x200`，删除 `DETACHED_PROCESS` 常量；
- `scripts/paper_watchdog.py::_child_creationflags_tiers()` 同口径重排（测试钉死两边逐值相等）；
- 看门狗新增死亡取证：子进程异常退出时写一行
  `[WATCHDOG][FORENSIC] rc=…, runtime=…s, flags=0x…, 疑似死因=…`（读 paper_console.log 尾部特征，
  fail-open，绝不影响重启决策）；
- 测试：launcher 5 条改/新增 + 真值表关键断言；watchdog 4 条改/新增 + 取证 6 条。
