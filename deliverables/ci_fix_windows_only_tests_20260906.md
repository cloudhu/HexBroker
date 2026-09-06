# CI 修复：22 条 Windows-only 测试在 ubuntu runner 上连崩（2026-09-06）

## 1. 故障现象

GitHub Actions `test` job（ubuntu-latest，py3.11/3.12）报 `22 failed, 1377 passed, 23 skipped, 1 xfailed`。本地 Windows 全量 1424 passed 全绿 —— **纯平台可移植性问题**，非功能缺陷。

| 失败组 | 条数 | 症状 |
|---|---|---|
| `test_czce_source.py` | 7 | `ModuleNotFoundError: No module named 'requests'` |
| `test_paper_watchdog.py` | 6 | `assert 1 == 0` / `counter.txt` FileNotFoundError / `child Popen 未被调用` |
| `test_launch_trading_window.py` | 9 | `AttributeError: subprocess.CREATE_NO_WINDOW`、`_load_engine_module() is None`、`E:\Workspace\HexBroker` 路径 FileNotFoundError |

## 2. 根因（三分类，全部实证）

### 2.1 requests 未进 CI 依赖（7 条）
`requests` 属 pyproject `[sources]` extra（可选数据源，懒加载设计：缺依赖不阻断 import）。CI 只装 `-r requirements.txt` + `-e . --no-deps` → 没有 requests。`czce_source.py` 生产代码顶部**无** requests import（契约合规）；但测试 `_mk_source` 需要 mock `requests.get`，必须可 import。

### 2.2 `paper_watchdog.py` Popen 调用点无条件求值 Windows-only 常量（6 条）
```python
creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
```
`subprocess.CREATE_*` 仅 Windows 存在，POSIX 上 kwargs 求值即 AttributeError → 被 run() 的 `except Exception` 吞成「子进程启动失败」（crash_count++）→ 5 条真实 spawn 集成用例连崩（counter.txt 永不生成）、mock Popen 用例「Popen 未被调用」。**反面教材价值**：宽 except 把平台 bug 伪装成了「启动失败」，失败面完全失真。

### 2.3 launch 测试的平台前提（9 条）
- **3 条 flags 断言**：测试行 `assert flags & subprocess.CREATE_NO_WINDOW` —— 生产代码用的是自定义字面量常量（0x200 | 0x08000000，Linux 求值安全），崩在**测试断言行**。
- **5 条引擎同源契约**：`_load_engine_module()` 加载 `paper_trading_main.py`（msvcrt 字节锁），POSIX 上本质不可加载 → 返回 None → 同源偏移断言必红。**这是本质性平台前提，不是 bug**。
- **1 条暴露真 bug**：`launch_trading_window.py:62` `ROOT = r"E:\Workspace\HexBroker"` **硬编码绝对路径** —— 换机/换盘/CI checkout 全部路径常量（MAIN/WATCHDOG/CONSOLE_LOG/PID_FILE/PYTHONPATH/cwd）静默失效。本地能跑纯因路径恰好对。

## 3. 修复（5 处改动）

| 文件 | 改动 | 性质 |
|---|---|---|
| `scripts/paper_watchdog.py` | 新增模块级 `_CHILD_CREATIONFLAGS`：win 取 `CREATE_NEW_PROCESS_GROUP\|CREATE_NO_WINDOW`，POSIX 取 0；Popen 改用它 | **生产代码跨平台修复** |
| `scripts/launch_trading_window.py` | `ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` 动态推导 | **生产代码可移植性修复** |
| `tests/test_paper_watchdog.py` | `test_watchdog_child_uses_windowless_flags` 加 skipif（+补 `import pytest`） | 测试平台守卫 |
| `tests/test_launch_trading_window.py` | 文件级 `_WIN_ONLY` 标记装饰 8 条（3 flags + 5 引擎同源） | 测试平台守卫 |
| `.github/workflows/ci.yml` | test job Install dependencies 补 `pip install requests`（附注释说明为何只装它、不装整个 [sources]） | CI 配置 |

**设计取舍**：
- watchdog 真实 spawn 用例**修生产代码而非 skip** —— 看门狗逻辑跨平台无害，POSIX 上 `creationflags=0` 合法（文档行为），修完 CI 能真跑 5 条高价值集成测试（P2-2 句柄契约）。
- flags/引擎同源用例 **skipif 而非硬改** —— 它们验证的就是 Windows 语义（无窗口契约、msvcrt 字节锁同源），POSIX 上前提不成立，skip 是诚实表达。
- `PY` 解释器路径仍是本机绝对路径（launcher 只在本机生产运行，CI 断言 `cmd[0]==PY` 自比照不依赖具体值）—— 注释已声明迁移时需同步改，**本次不动**（13:25 自动化在即，最小改动面）。

## 4. 验证证据

| 项 | 结果 |
|---|---|
| 定向三文件（launch 22 + watchdog 12 + czce 9） | **43 / 0 failed / 0 errors / 0 skipped**（junitxml 解析） |
| 全量回归（不带路径参数，basetemp 仓库外） | **1424 tests / 0 failed / 0 errors / 1 skipped**（169s）—— 与基线逐值一致，零回归 |
| ROOT 推导正确性 | `test_pid_file_matches_engine_data_dir`（真实 configs/paper.yaml 逐值比对 PID_FILE）全量内通过 = 推导结果与原硬编码逐字符一致 |
| py_compile 四文件 | 全过 |
| 全仓扫描 `subprocess.CREATE_*` 裸引用 | 生产代码仅剩 `paper_watchdog.py:49`（平台分支内）；测试引用均在 skipif 保护内；`third_party` 一处不在 testpaths 白名单 CI 不触及 |

**CI 预期**：22 failed → 0；新增 9 条 skip（3+5 launch + 1 watchdog）→ skipped 23→32；czce 7 + watchdog 5 + pid_file 1 在 Linux 真跑转绿 → passed 1377→1390。总 1423 = 1390 + 32 + 1 xfailed ✓。

## 5. 经验沉淀

1. **Windows-only 常量必须模块级平台分支取**，不能写在 Popen 调用点 kwargs 里 —— 平台 AttributeError 会混入业务异常路径被宽 except 吞掉，故障面完全失真（本次 6 条连崩的机理）。
2. **生产脚本禁止硬编码绝对路径**（ROOT 教训）：一切从 `__file__` 推导；本机特定值（如 PY 解释器）须注释声明「仅本机运行，迁移需同步改」。
3. CI runner 是 ubuntu：新增脚本/测试时，凡是引用 `msvcrt` / `subprocess.CREATE_*` / Windows 盘符的用例，**默认要给平台守卫**，不要等 CI 红了再补。
