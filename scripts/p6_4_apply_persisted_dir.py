#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P步-A/B 固化应用器：把一批 persisted JSON（来自 PandaData close_pcr MCP 拉取）
确定性地融合进 18 品种日线并重建信号缓存尾折。

设计
----
- 输入目录 `artifacts/p6_4_pull_{YYYYMMDD}/` 下每个 `<sym0>.json`：
    {"result": {"type":"dataframe","columns":[...],"rows":[[...]]}}
  （直接由 pandadata MCP `get_future_daily_post(method=close_pcr)` 的返回值落盘，无需转换）
- 本脚本对每个文件：子集到安全列 → 从数据日期推导 seg(YYYYMMDD_YYYYMMDD)
  → 调用 `p6_4_fill_gaps.py --stage parse` → 写回分片（p6_4 内部已做备份/原子写/重叠校验）
- 全部品种 parse 完成后，调用 `p22_tail_ext.py` 重建尾折信号缓存（延伸至最新交易日）

为何存在
------
原刷新自动化 Step 2 走 `fetch_data.py --source akshare`（仅 SHFE.cu 且 raw sina 口径错）
或 `p22_tail_ext.py --skip-eval`（只重建、不拉新数据），缓存永远停在最后手动日期 → fd 恒>0。
本脚本 + 自动化中的 pandadata MCP 拉取，构成真正能让 fd=0 的 P步-A/B 管线。

用法
----
  python scripts/p6_4_apply_persisted_dir.py --dir artifacts/p6_4_pull_20260825
  python scripts/p6_4_apply_persisted_dir.py --dir artifacts/p6_4_pull_20260825 --skip-p22
  # 交易日场景（推荐自动化使用）：拉取结果体检不通过 → 醒目横幅 + exit 3
  python scripts/p6_4_apply_persisted_dir.py --dir artifacts/p6_4_pull_20260825 --trading-day
  # 指定基准交易日与容忍天数
  python scripts/p6_4_apply_persisted_dir.py --dir artifacts/p6_4_pull_20260825 \
      --trading-day --asof 2026-08-28 --stale-days 5

退出码
------
  0 正常完成 / 非交易日安全返回 | 1 融合或 p22 失败 | 2 目录不存在 | 3 数据源拉取失败（--trading-day）
  4 **P1-2 盘中禁写**：K 线融合已成功入湖，仅信号缓存重建被跳过/拦截（非融合失败）

⚠️ P1-2 交易时段禁写（2026-09-01）
-------------------------------
K 线融合与信号缓存重建是**两件风险等级不同的事**：融合写入的是尚未开盘的当日数据，
盘中做也无妨；而重写生产信号缓存会让「模拟盘进程已加载的旧信号」与「盘上重写后的
新信号」并存，下一次窗口重启（13:25 / 20:55）将静默翻转全部开仓与成本门禁结论。

故本脚本在调用 p22 前做预检：交易时段内**跳过** p22 并返回 exit 4（K 线融合结果
保留），绝不因信号缓存被拦而把整条数据链路报成失败。p22 内部硬护栏按**落盘时刻**
二次裁定，覆盖「盘后启动、重训超时、落盘时已开盘」的越界场景。

``--trading-day`` 体检三重判定（任一不通过 → exit 3）
---------------------------------------------------
  1. **EMPTY**   目录 0 个 json
  2. **PARTIAL** 品种数 < ``--expect``（旧版漏网：18 只落 5 个也 exit 0）
  3. **STALE**   数据最新日期 早于 ``--asof`` - ``--stale-days``
                 （旧版漏网：目录非空但数据是几天前的，照样 exit 0，
                   把"真实拉取失败"伪装成"刷新成功"）

历史回填场景应**不加** ``--trading-day``，以免被陈旧判定误杀。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型检查期引入，避免运行期硬依赖 pandas（ruff F821 修复）
    import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
P6_4 = PROJECT_ROOT / "scripts" / "p6_4_fill_gaps.py"
P22 = PROJECT_ROOT / "scripts" / "p22_tail_ext.py"
TAIL_EXT_CACHE = PROJECT_ROOT / "artifacts" / "signals_cache18_grouped_v8_tail_ext.parquet"

# p6_4_append_20260824 已验证可用的列子集（pandadata 返回的超集含这些，安全降维）
SAFE_COLS = [
    "date", "underlying_symbol", "open", "high", "low", "close",
    "volume", "open_interest",
]


def _resolve_result(raw: dict) -> dict:
    """兼容 {result:{...}} / 裸 {type,columns,rows} / {rows,columns}。"""
    if isinstance(raw, dict):
        if "result" in raw and isinstance(raw["result"], dict):
            return raw["result"]
        if "rows" in raw or "columns" in raw:
            return raw
    raise ValueError(f"无法识别的 persisted 结构，键: {list(raw.keys()) if isinstance(raw, dict) else type(raw)}")


def _load_df(payload: dict) -> "pd.DataFrame":
    import pandas as pd
    if payload.get("type") == "dataframe" or ("columns" in payload and "rows" in payload):
        cols = payload.get("columns") or []
        rows = payload.get("rows") or []
        return pd.DataFrame(rows, columns=cols)
    if "data" in payload:
        return pd.DataFrame(payload["data"])
    raise ValueError(f"result 结构无法识别: {list(payload.keys())}")


def _seg_from_dates(df) -> str:
    dates = df["date"].astype(str).tolist()
    return f"{min(dates)}_{max(dates)}"


def _norm_date(value) -> str:
    """把 ``20260828`` / ``2026-08-28`` / ``2026/08/28`` 统一成 ``YYYYMMDD`` 字符串。

    persisted 文件里的日期格式随上游变化（pandadata 给 ``20260828``，
    部分源给 ``2026-08-28``），统一后可做字典序比较。
    """
    return re.sub(r"\D", "", str(value))[:8]


def _scan_latest_date(json_files: list[Path]) -> tuple[str | None, dict[str, str | None]]:
    """扫描全部 persisted 文件，返回 (全局最新日期, 每品种最新日期)。

    纯 json 解析，不依赖 pandas —— 保证"拉取失败"快速路径在数据环境异常时也能出结论。
    单文件解析失败不中断整体扫描，记为 None 交由上层按缺失处理。
    """
    latest: str | None = None
    per_sym: dict[str, str | None] = {}
    for fp in json_files:
        sym = fp.stem
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            res = _resolve_result(raw)
            cols = res.get("columns") or []
            rows = res.get("rows") or []
            if "date" not in cols or not rows:
                per_sym[sym] = None
                continue
            i = cols.index("date")
            vals = [_norm_date(r[i]) for r in rows if i < len(r) and r[i] is not None]
            per_sym[sym] = max(vals) if vals else None
        except Exception:  # noqa: BLE001 - 单文件损坏不应阻断整体判定
            per_sym[sym] = None
        m = per_sym.get(sym)
        if m and (latest is None or m > latest):
            latest = m
    return latest, per_sym


def _print_stale_banner(d: Path, expect: int, got: int, latest: str | None,
                        asof: str, earliest_ok: str, stale_days: int,
                        missing: list[str], reason: str = "STALE") -> None:
    """P1-c：目录非空但数据陈旧 / 品种不全 —— 与"0 落盘"同样的醒目横幅 + exit 3。

    背景（2026-08-29 补齐）：原 ``--trading-day`` 只判「目录 0 个 json」。
    实际生产中出现过两类漏网：
      1. **数据陈旧**：目录里是几天前（甚至更旧）的数据，本脚本照常融合并 exit 0，
         把"真实拉取失败"伪装成"刷新成功"；
      2. **品种不全**：18 个只落了 5 个，同样 exit 0，而下游按 18 品种出信号。
    两者与「0 落盘」后果相同：缓存不延长 → fd>0 → 隔夜过期门禁禁开。
    失败原因不同（配额/网络/部分超时），但**都必须是显式失败**。
    """
    print("=" * 66)
    print("⛔ 行情数据不可用 —— 缓存【不会延长】，请检查数据源")
    print("=" * 66)
    print(f"  · 目录：{d}")
    print(f"  · 期望 {expect} 个 <sym0>.json，实际 {got}")
    if latest:
        print(f"  · 数据最新日期：{latest}（要求不早于 {earliest_ok}，容忍 {stale_days} 日历日）")
        print(f"  · 基准交易日（--asof）：{asof}")
    else:
        print(f"  · 基准交易日（--asof）：{asof}")
        print("  · 无有效日期：全部文件均无 date 列或为空")
    # 判定文案必须与事实一致：误导性告警比没有告警更糟
    if reason == "PARTIAL":
        print("  · 判定：品种不全 —— 部分品种未落盘，下游按全品种出信号会缺腿")
    elif reason == "EMPTY_DATE":
        print("  · 判定：无有效日期 —— 文件存在但取不到日期字段，无法确认新鲜度")
    else:
        print("  · 判定：数据陈旧 —— 拉取到的是旧数据，不得当作刷新成功")
    if missing:
        shown = missing[:8]
        tail = f" … 共 {len(missing)} 个" if len(missing) > 8 else ""
        print(f"  · 缺失/无有效日期品种：{', '.join(shown)}{tail}")
    print("  · 常见原因：Pandadata 网关 500009 单日总流量超限 / MCP 未接线 / "
          "网络中断 / 部分品种超时")
    print("  · 后果：阈值=0 隔夜过期门禁下主源信号全部过期 → 夜盘仅技术兜底或 0 开仓")
    print("  → 处置：确认数据源配额与连接器接线；配额重置后（通常本地 0 点）重跑本管线补刷")
    print("  → 若今日确为非交易日，请去掉 --trading-day 重跑（走旧的安全返回语义）")
    print("=" * 66)


def _check_trading_day_pull(d: Path, json_files: list[Path], expect: int,
                            asof: str, stale_days: int) -> tuple[bool, str]:
    """交易日拉取结果体检。返回 (是否通过, 失败原因)。

    三重判定，任一不通过即显式失败：
      1. 完全没落盘（0 个 json）
      2. 品种不全（少于 expect）
      3. 数据陈旧（全局最新日期 早于 asof - stale_days）
    """
    # 1) 完全没落盘 —— 沿用原有横幅（文案针对该场景）
    if not json_files:
        _print_pull_failure_banner(d, expect)
        return False, "EMPTY"

    # 2) 品种不全
    if len(json_files) < expect:
        latest, per_sym = _scan_latest_date(json_files)
        _print_stale_banner(
            d, expect, len(json_files), latest, asof,
            _earliest_ok(asof, stale_days), stale_days, [], reason="PARTIAL"
        )
        return False, "PARTIAL"

    # 3) 数据陈旧 / 无有效日期
    latest, per_sym = _scan_latest_date(json_files)
    earliest_ok = _earliest_ok(asof, stale_days)
    missing = sorted(s for s, v in per_sym.items() if not v)
    if latest is None:
        _print_stale_banner(
            d, expect, len(json_files), None, asof, earliest_ok, stale_days,
            missing, reason="EMPTY_DATE"
        )
        return False, "EMPTY_DATE"
    if latest < earliest_ok:
        _print_stale_banner(
            d, expect, len(json_files), latest, asof, earliest_ok, stale_days,
            missing, reason="STALE"
        )
        return False, "STALE"
    return True, "OK"


def _earliest_ok(asof: str, stale_days: int) -> str:
    """新鲜度下限：``asof - stale_days``，返回 YYYYMMDD。"""
    d = datetime.strptime(_norm_date(asof), "%Y%m%d").date()
    return (d - timedelta(days=stale_days)).strftime("%Y%m%d")


def _default_stale_days() -> int:
    """容忍天数默认与数据层门禁保持一致（避免两处口径漂移）。

    惰性导入：本脚本的"拉取失败"快速路径不应因 pandas 缺失而无法出结论。
    """
    try:
        from hexbroker.data.freshness import DEFAULT_MAX_STALE_DAYS

        return int(DEFAULT_MAX_STALE_DAYS)
    except Exception:  # noqa: BLE001
        return 5


def _print_pull_failure_banner(d: Path, expect: int) -> None:
    """P1-a：数据源拉取失败醒目横幅。

    背景 2026-08-28 停摆：Pandadata 网关 500009「单日总流量超限」导致 18/18 拉取
    失败，而本脚本把「目录无 json」一律当「非交易日/无新数据」**安全返回 exit 0**
    ——数据源中断与休市不可区分，失败被静默吞掉，最终表现为"系统照常运行却不交易"。
    调用方须先用交易日历校验并传 ``--trading-day``，本横幅才会触发。
    """
    print("=" * 66)
    print("⛔ 行情拉取失败 —— 今日为交易日但 0 个品种落盘，缓存【不会延长】")
    print("=" * 66)
    print(f"  · 目录：{d}")
    print(f"  · 期望 {expect} 个 <sym0>.json，实际 0")
    print("  · 常见原因：Pandadata 网关 500009 单日总流量超限 / MCP 未接线 / 网络中断")
    print("  · 后果：阈值=0 隔夜过期门禁下主源信号全部过期 → 夜盘仅技术兜底或 0 开仓")
    print("  → 处置：确认数据源配额；配额重置后（通常本地 0 点）重跑本管线补刷")
    print("  → 若今日确为非交易日，请去掉 --trading-day 重跑（走旧的安全返回语义）")
    print("=" * 66)


def _p22_rewrite_blocked(now=None) -> tuple[bool, str]:
    """P1-2 预检：返回 ``(是否被禁, 横幅文本)``；未被禁时横幅为空串。

    背景：本脚本在 K 线融合后**无条件**调用 ``p22_tail_ext --force`` 重建生产信号缓存。
    20:30 夜盘自动化 = 18 品种拉取 + 约 8 分钟全量重训，而 **rb0 夜盘 21:00 开盘**、
    模拟盘 **20:55** 已启动 —— 只要链路稍有超时，落盘就跨入夜盘。2026-09-01 09:10:12
    的盘中重写（rb0 p_up 0.8667 → 0.999999）即同构事故。

    这里的预检是**提前止损**（避免白跑重训），权威闸门仍是 ``p22_tail_ext.merge_tail_ext``
    内的硬护栏——后者按**落盘时刻**裁定，能拦住「盘后启动、重训超时、落盘时已开盘」。

    ⚠️ 延迟导入：本脚本刻意不在运行期硬依赖 pandas（见文件头 TYPE_CHECKING 说明），
    而 ``hexbroker.market.session`` → ``data.calendar`` 会带入 pandas。
    """
    try:
        from hexbroker.market.session import (
            format_cache_rewrite_block_banner,
            is_cache_rewrite_blocked,
        )
    except Exception as exc:  # noqa: BLE001
        # 守卫不可用时按**放行**处理并显式告警：本预检只是提前止损，p22 内部硬护栏
        # 仍是权威闸门；若连 pandas 都不可用，p22 本身根本起不来，不会失去保护。
        print(f"[WARN] P1-2 禁写预检不可用（{type(exc).__name__}: {exc}）——"
              f"跳过预检，由 p22 内部硬护栏兜底")
        return False, ""
    ts = now if now is not None else datetime.now()
    if not is_cache_rewrite_blocked(ts):
        return False, ""
    return True, format_cache_rewrite_block_banner(
        ts,
        cache_path=str(TAIL_EXT_CACHE),
        extra="K 线融合【已完成并入湖】，仅信号缓存重建被跳过（P1-2）。"
              "请在允许窗口（11:30–13:20 / 15:00–20:50 / 02:30–08:50）内"
              "用 --skip-p22 重跑本脚本的融合部分、再单独跑 p22；"
              "确需盘中重建则人工加 --force-in-session 并同步重启模拟盘进程。",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="P步-A/B 固化应用器")
    ap.add_argument("--dir", required=True, help="含 <sym0>.json 的 persisted 目录")
    ap.add_argument("--sym0-map", default=None,
                    help="可选 sym0 文件名前缀→实际 sym0 映射（默认用文件名 stem）")
    ap.add_argument("--skip-p22", action="store_true", help="跳过 p22_tail_ext 重建（仅做 K 线融合）")
    ap.add_argument("--trading-day", action="store_true",
                    help="今日为交易日（调用方须先用交易日历校验）。置位时『目录存在但 0 个 json』"
                         "判定为【数据源拉取失败】→ 醒目横幅 + exit 3；不置位维持旧语义"
                         "（WARN + exit 0，兼容非交易日/无新数据）")
    ap.add_argument("--expect", type=int, default=18,
                    help="期望品种数（--trading-day 下参与『品种不全』判定，默认 18）")
    ap.add_argument("--asof", default=None,
                    help="基准交易日 YYYY-MM-DD / YYYYMMDD，默认今天。"
                         "仅 --trading-day 下用于陈旧判定")
    ap.add_argument("--stale-days", type=int, default=None,
                    help="陈旧容忍日历日，默认取数据层 "
                         "hexbroker.data.freshness.DEFAULT_MAX_STALE_DAYS（当前 5）")
    ap.add_argument("--python",
                    default=r"C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe",
                    help="python 解释器（默认 managed venv；缺失时回退 sys.executable）")
    args = ap.parse_args()

    d = Path(args.dir)
    if not d.is_dir():
        print(f"[FAIL] 目录不存在: {d}")
        return 2

    json_files = sorted(p for p in d.glob("*.json") if not p.name.endswith(".clean.json"))

    if args.trading_day:
        asof = args.asof or date.today().isoformat()
        stale_days = args.stale_days if args.stale_days is not None else _default_stale_days()
        passed, reason = _check_trading_day_pull(
            d, json_files, args.expect, asof, stale_days
        )
        if not passed:
            print(f"[FAIL] 交易日拉取体检未通过：{reason}")
            return 3
        latest, _ = _scan_latest_date(json_files)
        print(f"[GATE] 交易日体检通过：{len(json_files)}/{args.expect} 品种，"
              f"数据最新 {latest}，基准 {asof}，容忍 {stale_days} 日历日")
    elif not json_files:
        print(f"[WARN] 目录无 *.json（排除 .clean.json）: {d}（无新数据可融合，属预期）")
        return 0

    py = args.python
    if not Path(py).exists():
        py = sys.executable
        print(f"[INFO] 默认 python 不存在，回退 sys.executable: {py}")
    ok, fail = [], []
    for fp in json_files:
        sym0 = fp.stem  # 文件名即 sym0（ag0/al0/...）
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            res = _resolve_result(raw)
            df = _load_df(res)
            # 安全降维：仅保留 p6_4 验证过的列（不存在则跳过该列）
            keep = [c for c in SAFE_COLS if c in df.columns]
            df = df[keep]
            if "date" not in df.columns or df.empty:
                print(f"[SKIP] {sym0}: 无有效日期列或空数据")
                continue
            seg = _seg_from_dates(df)
            # 写清理后的 persisted 文件到独立子目录（避免污染输入目录被重复 glob）
            clean_dir = d / "_clean"
            clean_dir.mkdir(exist_ok=True)
            clean = clean_dir / f"{sym0}.json"
            clean.write_text(
                json.dumps({"result": {"type": "dataframe",
                                        "columns": list(df.columns),
                                        "rows": df.astype(object).where(df.notna(), None).values.tolist()}},
                           ensure_ascii=False),
                encoding="utf-8",
            )
            cmd = [py, str(P6_4), "--stage", "parse", str(clean), "--sym", sym0,
                   "--seg", seg, "--force", "--scale", "1.0"]
            print(f"\n[PARSE] {sym0} seg={seg} rows={len(df)}")
            r = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
            if r.returncode != 0:
                print(r.stdout[-1500:])
                print(r.stderr[-1500:])
                raise RuntimeError(f"p6_4 parse 失败 rc={r.returncode}")
            ok.append(sym0)
        except Exception as exc:  # noqa: BLE001
            fail.append((sym0, str(exc)))
            print(f"[FAIL] {sym0}: {exc}")

    print(f"\n[KLINE] 融合完成：成功 {len(ok)} / 失败 {len(fail)}")
    for s, e in fail:
        print(f"   - {s}: {e}")

    if args.skip_p22:
        print("[SKIP] 已跳过 p22_tail_ext 重建（--skip-p22）")
        return 1 if fail else 0

    if not ok:
        print("[SKIP] 无成功融合的品种，跳过 p22 重建")
        return 1 if fail else 0

    # ---- P1-2 预检：交易时段禁止重写生产信号缓存 ----
    blocked, banner = _p22_rewrite_blocked()
    if blocked:
        print("\n" + banner)
        print("[SKIP] 信号缓存重建已跳过（P1-2 盘中禁写）；K 线融合不受影响，已入湖")
        return 4

    print("\n[P22] 重建信号缓存尾折（p22_tail_ext --force，全量重训约 8 分钟）...")
    r = subprocess.run([py, str(P22), "--force"], cwd=str(PROJECT_ROOT), capture_output=True, text=True)
    if r.returncode != 0:
        # exit 2 = 被 p22 内部硬护栏拦下（多数为「盘后启动、重训超时、落盘时已开盘」）。
        # 此时 K 线融合同样已成功，故与预检拦截同口径返回 4，不得报成融合失败。
        if r.returncode == 2:
            print(r.stdout[-2000:])
            print("[SKIP] 信号缓存重建被 p22 硬护栏拦截（落盘时刻已处于交易时段）；"
                  "K 线融合不受影响，已入湖")
            return 4
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        print("[FAIL] p22_tail_ext 重建失败")
        return 1
    print(r.stdout[-2000:])
    print("[OK] P步-A/B 完成：K线融合 + 信号缓存重建")
    return 0


if __name__ == "__main__":
    sys.exit(main())
