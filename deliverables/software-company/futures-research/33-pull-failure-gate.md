# 33 · P1-a 防再发：行情拉取失败不得静默降级

> 阶段：实现 + 回归锁 · 日期：2026-08-28
> 上游：`32-stall-root-cause-final.md`（根因终裁：Pandadata 配额耗尽）
> 铁律：最小变更 · fail-safe · 向后兼容 · feat+docs 双提交

---

## TL;DR

停摆的**直接触发点**不是"没人刷新"，而是**刷新失败被系统当成"属预期"吞掉了**：

```
Pandadata 500009 配额耗尽 → 18/18 拉取失败 → 目录 0 个 json
   → p6_4_apply_persisted_dir.py：「目录无 *.json（无新数据可融合，属预期）」→ exit 0
   → p22 未重建 → 缓存停在 08-26 → 夜盘 fd=1 → 0 开仓（静默）
```

`artifacts/p6_4_pull_20260827/_REPORT.md` 虽已记录失败，但**退出码仍为 0**，
下游无任何硬信号——这正是"系统照常运行却不交易"的成型点。

**修复：让「数据源拉取失败」与「非交易日/无新数据」在退出码层面可区分。**

---

## 一、实现

### 1. `scripts/p6_4_apply_persisted_dir.py`

| 变更 | 说明 |
|------|------|
| `--trading-day` | 调用方声明「今日为交易日」（须先用交易日历校验）。置位时目录 0 个 `*.json` → **判定拉取失败** |
| `--expect N`（默认 18） | 期望品种数，仅用于提示文案 |
| `_print_pull_failure_banner()` | 醒目横幅：目录/期望数/常见原因（含 500009）/后果/处置建议 |
| 退出码 **3** | 新增「数据源拉取失败」语义 |
| 顺带修复 | 预存 F821（`-> "pd.DataFrame"` 字符串注解），改 `TYPE_CHECKING` 引入 |

退出码语义（已写入 docstring）：

| 码 | 含义 |
|----|------|
| 0 | 正常完成 / 非交易日安全返回（旧语义，**未变**） |
| 1 | 融合或 p22 失败 |
| 2 | 目录不存在 |
| **3** | **数据源拉取失败（仅 `--trading-day` 时可能）** |

**向后兼容**：不传 `--trading-day` 时行为与修复前完全一致（WARN + exit 0），
非交易日/无新数据场景零影响。

### 2. 自动化接线（两条刷新任务）

- 日盘 `automation-1787625060414`、夜盘 `automation-1787625085581`
- 步骤 2c 命令追加 `--trading-day`
- 新增步骤 0：先用 westock 交易日历校验，非交易日直接跳过
- 报告规则：`0/18` 或 exit 3 → 标题必须以「⛔ 行情拉取失败」开头，并写明原因码/后果/补刷建议
- 日盘任务新增补报要求：发现昨日目录为 0 json 时明示
- 顺带修正：日盘任务名称「08:45」与实际 rrule `BYHOUR=8;BYMINUTE=0` 不符 → 改名「日盘 08:00」
- 提示词补入**大写 underlying 代码**警告（小写 `cu0` 会返回空 parquet 死路，08-28 早晨实测确认）

### 3. 回归锁 `tests/test_p6_4_pull_failure_gate.py`（5 用例）

| 用例 | 锁定内容 |
|------|----------|
| `test_trading_day_with_zero_json_is_hard_failure` | 交易日+0落盘 → exit 3 + 横幅 + 500009 提示 |
| `test_without_trading_day_keeps_legacy_soft_return` | 未声明 → exit 0 + "属预期"（向后兼容） |
| `test_missing_dir_still_exit_2` | 目录缺失优先级高于交易日判定 |
| `test_expect_only_affects_message` | `--expect` 不改变判定 |
| `test_real_0827_failure_dir_reproduces` | **用 08-27 真实失败目录复现** exit 3 |

---

## 二、验证

| 项 | 结果 |
|----|------|
| 全量 pytest（生产解释器） | ✅ **663 passed**（658 → +5），exit 0 |
| ruff（改动文件） | ✅ All checks passed |
| CLI 冒烟（真实失败目录） | ✅ 横幅正确打印，`EXIT=3` |

---

## 三、遗留

| 编号 | 项 | 状态 |
|------|-----|------|
| P1-b | 配额重置后**自动补刷**（现为人工重跑） | 待立项 |
| P1-c | **数据源冗余**（Pandadata 单点；akshare 已证死路）→ 评估 westock/tdx/sina | 待立项 |
| P1-d | 08-27 **早晨**任务亦未落盘（dir 仅存夜间报告） | 待查 |

## 状态

✅ P1-a 实现 ✅ 自动化已接线 ✅ 663 全绿 ✅ ruff 通过
⏭️ 今晚 20:30 夜盘刷新若配额可用 → fd=0 → 夜盘自动恢复交易
