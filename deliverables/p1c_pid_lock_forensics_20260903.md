# P1-C：PID 锁互斥缺口取证与根治方案（2026-09-03）

> 状态：**方案①已获主理人终裁并实施（R26g）**。承接 P1-B（冷却覆写已修 R26e，`470e0be`）——合并写是防御层，PID 锁缺口是根因。

## 一、现状（实读代码，非陈旧上下文）

| 组件 | 位置 | 现状 |
|---|---|---|
| 锁获取 | `scripts/paper_trading_main.py::L34-85::_try_acquire_pid_lock` | **内容指纹式**：读锁文件 → `_is_pid_alive` 判存活 → 活则拒、死则覆盖写 `PID:CREATION_TIME` |
| 降级分支 | 同上 L83-85 | `except Exception: return True` —— **fail-open**：锁路径任何异常都不阻塞启动 |
| 存活判定 | `hexbroker/diagnostics/health_check.py::L78-93::_win_pid_alive` | `OpenProcess(QUERY_INFORMATION)`，**`if not process: return False`** —— ACCESS_DENIED（无权限）与进程已死**不区分，一律判死**（fail-open） |
| 删锁 | `scripts/paper_trading_main.py::L568-573` | `finally: pid_path.unlink()` —— 仅优雅退出删；被杀（窗口关闭/kill）不删 → 残留 |
| 拒启路径 | L555-558 | 判活失败 → `return 1` 拒绝第二实例 |
| smoke 路径 | L545-550 | `--smoke` 在锁获取之前 `return 0`，**不拿锁**（不受本次改动影响） |
| 既有测试 | `tests/test_pid_lock.py`（4）+ `tests/test_pid_lock_identity.py`（3） | 8 用例全部编码**旧内容式语义**（存活判定/僵尸覆盖） |

## 二、根因定罪（09-01 时间线闭环）

时间线：08:58:54 A 启动（写锁，14:30 真实开仓）→ 13:25:58 B 启动成功 → 20:55:41 C 启动成功 → 21:50:54 **双配置日志（A 与 B/C 并存铁证）** → 21:58:59 A 退出覆写冷却（P1-B）。

**矛盾点**：A 全程存活（14:30→21:58），B/C 启动时锁文件内容是 `pid_A:ct_A`（A 自己写的），按语义应拒绝 —— 但 B/C 都启动成功。

⇒ 只有两类解释，且**都在代码里找到了 fail-open 出口**：

1. **`_is_pid_alive` 假阴性**：`OpenProcess` 失败（典型：自动化以不同 token/session 调起进程 → ERROR_ACCESS_DENIED）被判死 → 走「僵尸覆盖」分支放行。代码 L86-87 `if not process: return False` **没有区分「死」与「打不开」**。
2. **降级分支**：锁读/写路径任何异常 → `return True` 放行。

⇒ **结构病根**：内容指纹式锁（读→判→覆盖）本质是 best-effort，互斥质量取决于存活判定的完备性，而存活判定在 Windows 上**不可能完备**（权限边界无法消除）。且非原子（TOCTOU）：读-判-写之间无 OS 仲裁。

## 三、方案对比

| 方案 | 内容 | 优点 | 缺点 | 评价 |
|---|---|---|---|---|
| ① **OS 句柄锁**（推荐） | 启动时打开 `paper.pid` 句柄 + 独占字节锁（Windows `msvcrt.locking LK_NBLCK` / POSIX `fcntl.flock`），**句柄持到进程退出**；锁文件常驻不删（内容仍写 `pid:ct` 供诊断）；锁被占 → 拒启 exit 1 | 进程死 → OS 自动释放句柄，**零僵尸锁**；锁文件常驻**消除删锁 TOCTOU**；互斥完全交 OS 仲裁，**不再依赖任何存活判定**；~50 行自写，零新依赖 | 旧实例 hang 但存活 → 新实例拒启（需人工，但 hang 本来就该人工）；`_win_pid_alive` 假阴性仍存在（降级为**仅诊断层**，不再进互斥路径） | ✅ **根治** |
| ② 最小修补 | `_win_pid_alive` ACCESS_DENIED → 保守拒；降级分支改 fail-closed | 改动小 | 内容式 TOCTOU 仍在；僵尸覆盖路径仍在（kill 后残留锁永远依赖不完备的存活判定）；**治标** | ⛔ 不推荐 |
| ③ 引入 third_party filelock | 用 `third_party/pylibs/filelock`（Windows 原生 LockFileEx） | 现成实现 | paper 主入口新增 third_party 路径依赖（现仅 Kronos 链使用）；SoftFileLock 默认仍内容式；收益不比 ① 多 | 🟡 备选 |

## 四、方案①语义细则（终裁后实施）

1. `paper_trading_main.py`：`_try_acquire_pid_lock` → `_acquire_instance_lock(pid_path)` 返回**锁句柄**（`None`=被占/异常 → 拒启 exit 1，**fail-closed**）；主流程持有句柄，`finally` 显式 unlock+close（**不再 unlink**，锁文件常驻）。
2. 锁内容仍写 `pid:ct`（`truncate(0)+write+flush`），仅作事后诊断（ forensics 时能读到最后持有者身份）。
3. 附带修复（诊断层）：`_win_pid_alive` ACCESS_DENIED（`GetLastError()==5`）→ 返回与「活」一致的保守处理交给上层判 ct；实际改为「无权限→无法判定」，由调用方（未来任何 alive 消费者）走保守分支。
4. **既有 8 个 PID 锁测试按新语义重写**（内容式判定退役）+ 新增：双句柄互斥（同进程二次加锁必败）、释放后可复得、锁内容写入、被占时拒启返回 None、（Windows）子进程持锁退出后主进程可复得（OS 自动释放实证）。
5. 风险自查（对照「不得引入新全停」护栏原则）：拒启 ≠ 交易全停——旧实例存活时拒新实例，旧实例继续交易；旧实例死亡时句柄自动释放，新实例畅通。唯一全停场景 = 旧实例 hang 存活，需人工，属正确 fail-closed。

## 五、影响面

- `scripts/paper_trading_main.py`（锁获取 + finally 退出路径）
- `hexbroker/diagnostics/health_check.py`（`_win_pid_alive` 附带修复，仅诊断语义）
- `tests/test_pid_lock.py` / `tests/test_pid_lock_identity.py`（重写）+ 新建 `tests/test_pid_lock_mutex.py`
- 基线预期：1181 → 1181+N（重写后计数以实际为准，回归报告如实记载）

## 六、实施记录（R26g，2026-09-03）

按 §四细则实施，与方案有一处实测修正：

| 项 | 实施 |
|---|---|
| `_acquire_instance_lock` | ✅ 新函数替换 `_try_acquire_pid_lock`，返回 `_InstanceLock` 句柄（`None`=被占/异常）；锁区在 `_LOCK_OFFSET=4096`（**实测修正**：`msvcrt.locking` 锁住的字节对其他句柄**读写全拒**，锁 byte 0 会让健康检查在实例运行期间读不了诊断内容 → 实测 PermissionError 后把锁区移到 EOF 之外的 4096 偏移，LockFileEx 允许锁 EOF 之外区间；POSIX flock 整文件语义不受影响） |
| 主流程 | ✅ 锁被占 → 拒启 exit 1（fail-closed）；`finally: instance_lock.release()`（**不再 unlink**） |
| `_win_pid_alive` | ✅ `OpenProcess` 失败区分 `GetLastError()==5`（ACCESS_DENIED → 保守视为可能存活），其余错误码判死 |
| `_read_pid_file` | ✅ 死亡 PID **不再 unlink 常驻锁文件**（「删除→重建」空窗是句柄锁 TOCTOU 回归点）；连带 **审计豁免清单整改清零**：`health_check.py` 原有 1 处 unlink 已移除 → `AUDIT_PACKAGE_EXEMPT` 同步移除该条（14 passed 验证） |
| 测试 | ✅ `test_pid_lock.py` 重写 6 用例（句柄+内容 / 同进程二次拒 / 释放可复得 / 常驻 / 垃圾重写 / **子进程持锁互斥 + 被杀后 OS 自动释放实证**）；`test_pid_lock_identity.py` 重写 3 用例（pid:ct 取证格式 / 释放后内容保留 / 被拒不覆写）——旧内容式语义 7 用例退役 |
| 子进程测试容错 | 被杀后锁释放有内核异步延迟（独立脚本实测 ≤0.05s），测试容错重试 2s，超时给出 returncode 诊断 |

**回归**：全量 **1183 passed**（junitxml 权威：tests=1183 / failures=0 / errors=0；口径 1181 − 7 旧 + 9 新 + 审计清单同步）。

## 七、QA fresh-eyes 复核（R26g，PASS：0🔴/4🟡，🟡 当轮全堵）

QA 独立实证（探针 + 重跑，非复述声明）：目标测试 30/30 passed、junit_r26g.xml 权威 1183/0/0/0、两提交无夹带、锁区/内容区真隔离实测（持锁时文件头读写成功、4096 锁字节 PermissionError errno=13、release 后恢复）、fd 无泄漏、合并方向严格大于与 docstring 一致。小观察：junit 早于 commit ~2.5min（commit 前验证模式），可接受。

| 🟡 | 内容 | 处置 |
|---|---|---|
| 🟡1 | 合并比较在构造 try 之外：磁盘 opened_at 为 tz-aware 时 naive/aware 混比较抛 TypeError → 外层 except 吞掉 → **整次合并放弃 → fail-open 整体写内存 → 陈旧内存回滚新鲜磁盘**（恰好复刻 P1-B）。系统自产 opened_at 恒 naive（datetime.now()），当前不可触发故 🟡；未来引入 aware 序列化即升级 🔴 | ✅ 已修：比较前 naive 归一化（scheduler.py）+ 新增回归用例 `test_p1b_tz_aware_disk_record_still_wins` |
| 🟡2 | acquire 成功与 try/finally 之间夹 governance 自检与新鲜度检查，二者抛异常则锁无显式 release（靠进程退出兜底，正确性不受损） | ✅ 已修：try 上提覆盖两步（paper_trading_main.py） |
| 🟡3 | health_check `_win_pid_creation_time`/`_pid_creation_time` docstring 仍写「PID 锁身份校验」——语义已退役，文档腐烂易误导复活旧判活锁 | ✅ 已修：docstring 更新为「仅取证内容生成，不参与互斥」 |
| 🟡4 | 测试卫生：子进程用例被拒断言若意外取得锁，fd 无 release 通道悬挂 | ✅ 已修：finally 内兜底 release（test_pid_lock.py） |

**修复后回归**：目标 44 passed（P1-B 8 + PID 锁 9 + 审计 14 + 冷却 13）；全量 **1184 passed**（junit_r26g2.xml 权威，口径 1183 + 🟡1新用例）。

## 八、遗留与后续

- 僵尸锁概念已随句柄锁消亡（进程退出即释放），`launch_trading_window.py` 文档语义不变（重复调用仍被安全拒绝）。
- `purge_test_pollution_from_audit_log.py` 对 `paper.pid` 的 fail-closed 只读探测兼容常驻文件（PID 可能是上一个已退出实例，psutil/os.kill 探测仍正确放行/拒绝）。
- P1-B 合并写 + P1-C 句柄锁构成双层防护：进程共存缺口已根治，冷却记录双保险。
