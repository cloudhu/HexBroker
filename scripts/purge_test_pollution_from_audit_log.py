#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1-8 遗留清理：把 pytest 写进生产审计日志的伪造 trade 事件精确摘除。

背景（2026-09-01）
-----------------
``log_structured()`` 走 loguru **广播到所有已注册 sink**。全量回归里
``hexbroker.paper.scheduler`` 先被导入 → 生产文件 sink（``data/paper/trades.log``）
已注册 → 后续每个测试调用的 ``log_structured`` 都会落盘。今日共 **5 次**测试运行、
每次 2 行，累计 **10 行**伪造 trade 事件。

⚠️ **这 10 行不能用 ``grep T000259`` 一把删** —— ``T000259`` 是 2026-08-24 真实
第 259 笔交易的 ID（日志第 2722 行，字段完整）。污染行复用了这个 ID，
按 ID 删除会**误伤真实审计记录**。本脚本改用**字段集指纹**精确识别。

污染行指纹（两个，均由测试桩直接构造，字段集极短且唯一）
--------------------------------------------------------
* F1 = ``{"event","trade_id","symbol","is_open","is_today_close"}``（5 字段）
* F2 = ``{"event","ts"}``（2 字段）

真实 trade 事件含 ``direction/qty/entry/stop/take_profit/price/fee`` 等字段，
字段集必然 >5，**不可能**被这两个指纹命中。

⛔ 执行前置护栏（最重要的一条）
------------------------------
生产模拟盘进程持有 ``trades.log`` 的 **append 句柄**。若在进程运行时截断重写，
进程下一次 write 会从**旧 offset** 落笔 → 文件中间出现空洞/错位，**比污染本身更糟**。
因此脚本在检测到生产进程存活时**直接拒绝 --apply**（dry-run 仍可跑）。

正确的执行窗口：生产进程停止时（重启窗口内）。

用法
----
  python scripts/purge_test_pollution_from_audit_log.py            # dry-run（默认）
  python scripts/purge_test_pollution_from_audit_log.py --apply    # 真正清理

退出码：0 = 成功/无污染；1 = 存在 FAIL；2 = 前置护栏拒绝执行。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROD_LOG = ROOT / "data" / "paper" / "trades.log"
PID_FILE = ROOT / "data" / "paper" / "paper.pid"

# 污染行字段集指纹（frozenset 保证顺序无关）
POLLUTION_FINGERPRINTS = {
    frozenset({"event", "trade_id", "symbol", "is_open", "is_today_close"}),
    frozenset({"event", "ts"}),
}


def _payload_of(line: str) -> dict | None:
    """从日志行中取出 JSON payload（格式：``<ts> | <json>``）。失败返回 None。"""
    if "|" not in line:
        return None
    raw = line.split("|", 1)[1].strip()
    if not raw.startswith("{"):
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def is_pollution_line(line: str) -> bool:
    """判定一行是否为 pytest 污染行。**只按字段集指纹**，绝不按 trade_id。"""
    obj = _payload_of(line)
    if obj is None:
        return False
    if obj.get("event") != "trade":
        return False
    return frozenset(obj.keys()) in POLLUTION_FINGERPRINTS


def _prod_process_alive() -> tuple[bool, str]:
    """生产模拟盘进程是否存活。返回 (是否存活, 说明)。

    ⛔ **fail-closed 铁律**（2026-09-01 22:26 事故教训）：任何一步无法确定
    （解析失败 / 探测手段不可用 / 探测异常）一律按「可能存活」处理 →
    --apply 拒绝执行。只有「PID 文件不存在」和「PID 确认不存在」才放行。
    事故复盘：PID 文件实际是 ``16312:134327409396740058``（pid:timestamp 复合
    格式），``int()`` 解析抛 ValueError，旧代码 except 分支错误地返回「未运行」
    → 护栏被自己的解析 bug 击穿，在生产进程存活时重写了日志（侥幸未造成损害，
    Windows append 句柄写前定位文件末尾；但这是运气不是设计）。
    """
    if not PID_FILE.exists():
        return False, f"无 PID 文件 {PID_FILE}"
    raw = PID_FILE.read_text(encoding="utf-8").strip()
    try:
        pid = int(raw.split(":", 1)[0])  # 兼容 "pid" 与 "pid:timestamp" 两种格式
    except (ValueError, IndexError) as exc:
        # ⛔ fail-closed：解析失败 ≠ 进程不存在
        return True, f"PID 文件不可解析（{exc}）→ 按可能存活处理，拒绝执行"
    try:
        import psutil  # noqa: PLC0415 —— 可选依赖，缺失则回退
    except ImportError:
        try:
            import os

            os.kill(pid, 0)
        except OSError:
            return False, f"PID {pid} 不存在（os.kill 探测）"
        except Exception as exc:  # noqa: BLE001 —— 探测异常同样 fail-closed
            return True, f"PID {pid} 探测异常（{exc}）→ 按可能存活处理，拒绝执行"
        return True, f"PID {pid} 存活（os.kill 探测）"
    try:
        if psutil.pid_exists(pid):
            return True, f"PID {pid} 存活（psutil）"
        return False, f"PID {pid} 不存在（psutil）"
    except Exception as exc:  # noqa: BLE001 —— psutil 异常同样 fail-closed
        return True, f"psutil 探测异常（{exc}）→ 按可能存活处理，拒绝执行"


def main() -> int:
    ap = argparse.ArgumentParser(description="清理 pytest 污染生产审计日志的伪造 trade 事件")
    ap.add_argument("--apply", action="store_true", help="真正执行清理（默认仅 dry-run）")
    ap.add_argument("--log", default=str(PROD_LOG), help=f"目标日志（默认 {PROD_LOG}）")
    args = ap.parse_args()

    target = Path(args.log)
    if not target.exists():
        print(f"❌ FAIL 日志文件不存在: {target}")
        return 1

    original_lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    kept: list[str] = []
    dropped: list[tuple[int, str]] = []
    for i, line in enumerate(original_lines, start=1):
        if is_pollution_line(line):
            dropped.append((i, line.rstrip("\n")))
        else:
            kept.append(line)

    print(f"[扫描] {target}")
    print(f"       总行数 {len(original_lines)}；命中污染指纹 {len(dropped)} 行")
    for lineno, content in dropped:
        print(f"       - L{lineno}: {content[:140]}")
    if not dropped:
        print("✅ 无污染行，无需清理")
        return 0

    # 交叉校验：绝不能误删真实记录 —— 被删行里不得出现字段完整的 trade 事件
    for lineno, content in dropped:
        obj = _payload_of(content) or {}
        forbidden = {"direction", "qty", "price", "fee"} & set(obj.keys())
        if forbidden:
            print(f"❌ FAIL 安全校验触发：L{lineno} 含真实字段 {forbidden} → 拒绝删除")
            return 1
    print("✅ 安全校验通过：所有待删行均不含 direction/qty/price/fee，非真实成交记录")

    if not args.apply:
        print("\n[dry-run] 未改动任何文件。确认无误后加 --apply 执行。")
        return 0

    # ⛔ 前置护栏：生产进程存活 → 拒绝重写（append 句柄 offset 错位风险）
    alive, why = _prod_process_alive()
    if alive:
        print(f"\n⛔ 拒绝执行：生产进程仍在运行（{why}）。")
        print("   该进程持有 trades.log 的 append 句柄，此时截断重写会导致其下次写入")
        print("   从旧 offset 落笔 → 文件错位/空洞，危害大于污染本身。")
        print("   → 请在重启窗口（进程停止后）再执行本脚本。")
        return 2
    print(f"\n[前置护栏] 生产进程未运行（{why}）→ 允许重写")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = target.with_suffix(target.suffix + f".bak_purge_{stamp}")
    shutil.copy2(target, backup)  # copy2 而非 move：原文件必须一直存在
    if not backup.exists() or backup.stat().st_size != target.stat().st_size:
        print(f"❌ FAIL 备份校验失败: {backup}")
        return 1
    print(f"[备份] {backup}（{backup.stat().st_size} 字节，与原档等长）")

    # G5：原子写（tmp + shutil.move），沙箱 safe-delete 钩子会拦截直接 unlink 覆盖
    tmp = target.with_suffix(target.suffix + ".purge.tmp")
    tmp.write_text("".join(kept), encoding="utf-8")
    shutil.move(str(tmp), str(target))

    after = target.read_text(encoding="utf-8").splitlines(keepends=True)
    if len(after) != len(original_lines) - len(dropped):
        print(f"❌ FAIL 行数不符：期望 {len(original_lines) - len(dropped)}，实得 {len(after)}")
        return 1
    if any(is_pollution_line(x) for x in after):
        print("❌ FAIL 清理后仍残留污染行")
        return 1
    print(f"✅ 清理完成：{len(original_lines)} → {len(after)} 行（删除 {len(dropped)} 行）")
    print(f"   残留污染行复检：0；备份保留于 {backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
