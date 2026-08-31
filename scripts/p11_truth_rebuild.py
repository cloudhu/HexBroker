"""P0-11：年度真值重建驱动器（首例 rb0/2020 缺失分区；支持既有分区整年替换）。

背景（2026-08-29 11:49 pytest 污染事故）
----------------------------------------
AkshareSource 曾以 ``save=True`` 为默认 + DataLake.save_processed 整文件
覆盖，rb0/2020 分区被写成 2 行合成数据，已隔离（artifacts/quarantine/）
并挂 ``_MISSING_2020.json``。留一法实测插值重建不可行（53.22 bp 均值误差），
唯一出路 = pandadata ``get_future_daily_post(method=close_pcr)`` 重拉 2020
全年真值 + P0-9 rebuild 流水线整分区新建。

（2026-08-30 备注：三源 + czce 的 ``save`` 默认已改 ``False``，并新增
save_processed 分区缩水门禁 —— 本节描述的触发条件已消除，历史留档。）

扩展（主理人 2026-08-29 拍板 ③真值重建）
----------------------------------------
cu0/rb0 2023 为名义价冒充（P0-10 审计定罪，假跳空 ±27%~51%），沿同一路线
根治。``rebuild_partition`` 对既有分区同样执行"同日期逐行替换 + 新日期
追加"（kind 只影响清标记分支），故 kind="missing" + 全年真值即等价于
整年替换。**前置覆盖预检**：既有分区每个日期必须被真值覆盖，否则中止
（真值缺口会让脏行静默残留，铁律禁止）。

用法（两步走）
--------------
1. 拉真值（需 pandadata MCP 工具，本会话未注册时在新会话执行）::

     mcp pandadata get_future_daily_post
       underlying_symbol=["RB"] start_date=20200101 end_date=20201231
       method=close_pcr
     → 结果 JSON 存为 artifacts/p11_rb0_2020_truth.json
       （格式与 p6_4 persisted 一致：{"result": {"columns": [...], "rows": [...]}}）

2. 重建（默认 dry-run，--apply 才写盘）::

     python scripts/p11_truth_rebuild.py --persisted artifacts/p11_rb0_2020_truth.json
     python scripts/p11_truth_rebuild.py --persisted ... --apply

序 3 口径重建（ag0/au0/m0 adj_close 污染，2026-08-30 新增）
----------------------------------------------------------
三品种的 ``adj_close`` 是名义价的拷贝（应以后复权真值整体替换），而
``raw_close`` 是三列中**唯一正确**的列（经 pandadata 单合约名义价交叉
验证）。因此序 3 必须同时开两个开关::

     python scripts/p11_truth_rebuild.py \
       --persisted artifacts/p11_ag0_2019_truth.json --sym ag0 --year 2019 \
       --no-scale-fix --keep-raw-close [--apply]

- ``--no-scale-fix`` / ``--scale 1.0``：禁用 p6_4 的 ``RAW_SCALE_FIX``
  常数（该常数基于"既有序列是未复权原始价口径"的错误前提，会把正确的
  close_pcr 真值反过来污染成名义价）。
- ``--keep-raw-close``：**跳过** ``enrich_raw_close``（sina 备源有 ~0.8%
  单日坏 bar），改为从现有分区按日期逐行继承 ``raw_close``，保证正确的
  名义价列零损伤。若真值含现有分区没有的日期 → **中止并报错**，
  不得填 NaN、不得回退到 adj 复制品。
- ``--tqsdk-raw``：``enrich_raw_close`` 的备源改用**天勤 TqsdkSource**
  深拉名义价（``get_kline_serial(data_length=8000)`` 触达 2024 全量），
  不经 sina。适用于：①主源(pandadata)不可访问时按"天勤兜底"补 raw_close；
  ②**截断分区**（仅含年初~07-17 的分区）需补 07-17 后名义价——此时
  ``--keep-raw-close`` 因缺失日期必失败，必须走本开关。
  天勤 KQ.m 即未复权名义价（≈ 主湖 raw_close），与既有分区 raw_close 逐位吻合。

- ``--drop-uncovered``：整年替换语义下，既有分区中**真值没有的日期**默认
  会让 PRECHECK 中止（``rebuild_partition`` 只有替换+追加、**没有删除**
  语义，残留即脏行）。这些"幽灵行"经取证全部是**节假日废 bar**且
  ``k = adj_close/raw_close`` 恰为 1.000000（与本次要清除的污染同源），
  故允许显式 opt-in 删除。**校验全中才放行，任一硬判据不中立即中止零写盘**：

  ① 待删日期不在 pandadata 真值中（**硬判据**，由构造保证恒真，仍显式记录）
  ② 不在 15 个健康品种（al0/cf0/cu0/hc0/i0/j0/jm0/ni0/p0/rb0/sc0/sr0/
    ta0/y0/zn0）同年日期**并集**中（**硬判据**，判定非真实交易日）
  ③ ``|k − 1.0| < 1e-9``（**仅打印的观察项，不参与拦截**）

  ⛔ 校验③已于 2026-08-30 由"兜底锁"**降级为纯观察项**，实证依据见
  ``artifacts/_gate3_out.txt``：

  - d5-attributor 指出 k==1 存在**基数谬误**（ag0/2024 有 82.5% 的行
    raw≡adj），实测确认且**比其估计更严重**——2018~2024 全部污染年份的
    k==1 占比在 **82.4%~100.0%**，其中 ag0/au0/m0 的 2018 年恰为
    **100.0%**，似然比 = 1.00，③ 携带的信息量为**零**；
  - ③ 的判别力呈**反向非对称**：在 2025/2026 两个干净年份 k==1 占比为
    **0/243 与 0/156**，③ 判别力最强；而 12 个幽灵行全部落在
    2020/2021/2022/2024，正是③最弱的年份。**③ 强的地方没有幽灵行，
    有幽灵行的地方③最弱**，故其作为拦截器的现实贡献≈0，却可能在未来
    （分区已重建、k 不再为 1 时）**误拦正确删除**；
  - ③ 降级后，拦截载荷全部压到②。② 的**安全边际已实测**（见
    ``artifacts/_gate2_out.txt``）：三污染品种 9 个年份的**每一个日期**
    在 15 健康品种中的支持票非 0 即 14~15 票，**1~13 票的中间带为空**，
    阈值两侧完全分离；4 个幽灵日期均获 **0/15 票**；18 个品种**零周末行**，
    且 2019~2026 年 15 健康品种日期集合**完全一致**（2018 唯一分歧是
    sc0 于 2018-03-26 上市前的 54 个自然缺失日，并集仍为 243，不受影响）。

  执行前**必须打印完整待删清单**，禁止静默删。

pandadata 拉取规程（2026-08-30 事故后立规，强制执行）
----------------------------------------------------
2026 年度真值拉取时，网关内联返回**被静默截断**：报文 ``row_count=156``，
实际只渲染出 150 行，丢失的正是 ``20260623~20260630`` 六个交易日，且
**全程无任何报错**。若照内联内容手抄进 JSON，会静默丢 6 个交易日。

1. 结果**一律以磁盘落盘文件为准**，**绝不手抄内联返回**；
2. 强制校验 ``len(rows) == total_rows``，不等即中止；
3. 归档必须**程序化**（参考 ``artifacts/_stepA_archive.py``：按
   ``params.start_date`` 自动定年份 + 逐份校验 rows/total/日期区间/
   ``underlying_symbol`` 唯一性/重复日期）。

安全语义
--------
- dry-run 默认：只解析/规范化/名义价回填/预览，零写盘；
- --apply 走 P0-9 ``rebuild_partition``（missing 路径）：分区不存在则新建，
  已存在则同日期逐行替换 + 新日期追加（整年替换语义）；自动清
  ``_MISSING_{year}.json``；manifest 经 save_processed 按"最近一次写入"
  语义重写（source="lake"，描述本次写入的分区）；
- 既有分区前置预检：列集合一致 + 真值日期全覆盖，违反即 rc=1 零写盘；
- 真值仅取目标年度行（rebuild_partition 跨界拒绝双保险）；
- 名义价回填（P1-c ``enrich_raw_close``）默认开启，失败大声降级。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import date
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT_ROOT / "data" / "raw"


class TqsdkRawFetcher:
    """``enrich_raw_close`` 的可注入 fetcher（鸭子类型：仅 ``fetch_raw``）。

    用天勤 TqsdkSource 深拉未复权名义价（KQ.m 连续主合约 = 当日主力合约
    原始价，硬拼接不复权 → 即主湖 raw_close）。``get_kline_serial`` 默认取
    "当前时刻往前" 的窗口，故必须用 ``data_length=8000`` 一路深拉到 2024，
    再按 [start, end] 切片，否则只够到近 ~1.6 年。
    """

    source_name = "tqsdk"

    def fetch_raw(self, symbols: list[str], start: str, end: str) -> dict:
        import time

        from tqsdk import TqApi, TqAuth

        from hexbroker.data.backup import RawPull
        from hexbroker.data.sources.tqsdk_source import TQ_SYMBOLS, load_tqsdk_auth

        user, pwd = load_tqsdk_auth()
        out: dict = {}
        for sym in symbols:
            if sym not in TQ_SYMBOLS:
                raise ValueError(f"tqsdk 源未配置主力连续映射的品种: {sym}")
            api = TqApi(auth=TqAuth(user, pwd))
            try:
                kl = api.get_kline_serial(TQ_SYMBOLS[sym], 86400, data_length=8000)
                dl = time.time() + 40
                while not api.is_serial_ready(kl):
                    if not api.wait_update(deadline=dl):
                        raise RuntimeError(
                            f"tqsdk {sym} 2024 深拉 40s 内未取齐（is_serial_ready=False）")
                df = pd.DataFrame(kl)
            finally:
                try:
                    api.close()
                except Exception:
                    pass
            recs = []
            for _, r in df.iterrows():
                ts = r.get("datetime")
                if pd.isna(ts) or int(ts) <= 0 or pd.isna(r.get("close")):
                    continue
                dt = (pd.to_datetime(int(ts), unit="ns", utc=True)
                      .tz_convert("Asia/Shanghai").tz_localize(None))
                recs.append((dt, float(r["close"])))
            s = pd.Series({dt: c for dt, c in recs})
            s.index = pd.to_datetime(s.index).normalize()
            s = s[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
            if s.empty:
                raise RuntimeError(f"tqsdk {sym} 在 {start}~{end} 无数据")
            out[sym] = RawPull(symbol=sym, close=s, source="tqsdk", warnings=[])
        return out


#: 15 个健康品种（三污染品种 ag0/au0/m0 之外的全部），其日期并集作为
#: "真实交易日"参考日历 —— 判据比单/双品种更权威，抗单品种日历异常。
HEALTHY_SYMBOLS: tuple[str, ...] = (
    "al0", "cf0", "cu0", "hc0", "i0", "j0", "jm0",
    "ni0", "p0", "rb0", "sc0", "sr0", "ta0", "y0", "zn0",
)

#: 幽灵行污染指纹判据：k = adj_close/raw_close 与 1.0 的绝对误差上限。
#: ⛔ **仅用于打印观察，不得作为拦截判据** —— 实证见 artifacts/_gate3_out.txt：
#: 2018~2024 污染年份 k==1 占比高达 82.4%~100.0%（2018 年三品种恰为
#: 100.0%，似然比 1.00，零信息量），而 2025/2026 干净年份为 0%。判别力与
#: 幽灵行分布呈反向错位，作为拦截器现实贡献≈0 且会误拦正确删除。
GHOST_K_TOL = 1e-9


def load_reference_calendar(data_root: str, year: int) -> tuple[set, list[str]]:
    """返回 (15 健康品种 ``year`` 年日期并集, 实际参与并集的品种列表)。

    缺失该年分区的品种静默跳过（不阻塞）；若一个都没有则调用方须报错，
    因为空并集会让第 2 条校验恒真 —— 而 2026-08-30 起③已降级为观察项，
    ② 是**唯一**的拦截判据，② 失守即意味着"任何待删日期都会放行"，
    必须硬中止。
    """
    dates: set = set()
    used: list[str] = []
    for sym in HEALTHY_SYMBOLS:
        p = Path(data_root) / "processed" / sym / "1d" / f"{year}.parquet"
        if not p.exists():
            continue
        d = pd.read_parquet(p, columns=["datetime"])
        dates |= set(pd.to_datetime(d["datetime"]).dt.normalize())
        used.append(sym)
    return dates, used


def judge_uncovered(cur: pd.DataFrame,
                    truth: pd.DataFrame,
                    ref_cal: set) -> list[dict]:
    """对"既有有、真值没有"的日期逐日做校验，返回明细清单。

    三条判据（2026-08-30 起，仅 ①② 参与拦截）：
      ① 该日期不在真值中（**硬判据**，由构造保证恒真，仍显式记录以便审阅）；
      ② 该日期不在 15 健康品种参考日历中（**硬判据**，判定为幽灵行）；
      ③ ``|adj_close/raw_close − 1.0| < GHOST_K_TOL``（**观察项，不拦截**）。
    """
    tset = set(truth["datetime"])
    rows: list[dict] = []
    for d in sorted(set(cur["datetime"]) - tset):
        row = cur.loc[cur["datetime"] == d].iloc[-1]
        raw = float(row["raw_close"])
        adj = float(row["adj_close"])
        k = adj / raw if raw else float("nan")
        rows.append({
            "date": d,
            "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "raw_close": raw, "adj_close": adj, "k": k,
            "c1_not_in_truth": d not in tset,
            "c2_not_in_refcal": d not in ref_cal,
            "c3_k_is_one": bool(abs(k - 1.0) < GHOST_K_TOL),
        })
    return rows


def _load_p6_4():
    """复用 p6_4 的持久化解析与规范化（已测路径，避免第三份实现）。"""
    spec = importlib.util.spec_from_file_location(
        "p6_4_fill_gaps", PROJECT_ROOT / "scripts" / "p6_4_fill_gaps.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p6_4_fill_gaps"] = mod
    spec.loader.exec_module(mod)
    return mod


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="P0-11 年度真值重建（缺失分区新建 / 既有分区整年替换）")
    ap.add_argument("--persisted", required=True,
                    help="pandadata 拉取结果 JSON（p6_4 persisted 格式）")
    ap.add_argument("--sym", default="rb0", help="品种 sym0（默认 rb0）")
    ap.add_argument("--year", type=int, default=2020, help="目标年度（默认 2020）")
    ap.add_argument("--data-root", default=str(DEFAULT_ROOT), help="数据湖根目录")
    ap.add_argument("--apply", action="store_true",
                    help="实际写盘（默认 dry-run 只预览）")
    ap.add_argument("--skip-nominal", action="store_true",
                    help="跳过 P1-c 名义价回填（离线场景）")
    # ── 序 3 口径重建开关 ─────────────────────────────────────────────
    ap.add_argument("--scale", type=float, default=None,
                    help="价格口径换算系数；显式给值即覆盖 p6_4 的 RAW_SCALE_FIX"
                         "（序 3 三品种重建应传 1.0 或改用 --no-scale-fix）")
    ap.add_argument("--no-scale-fix", action="store_true",
                    help="等价于 --scale 1.0：禁用 RAW_SCALE_FIX 常数换算")
    ap.add_argument("--keep-raw-close", action="store_true",
                    help="跳过 enrich_raw_close，改为从现有分区按日期逐行继承"
                         " raw_close（保护唯一正确的名义价列）；"
                         "真值含现有分区没有的日期时中止并报错")
    ap.add_argument("--drop-uncovered", action="store_true",
                    help="既有分区中真值没有的日期，经三重校验（真值未覆盖 + "
                         "非 15 健康品种交易日 + k==1.000000）全中后剔除；"
                         "打印完整待删清单后才执行，任一校验不中即中止零写盘")
    ap.add_argument("--tqsdk-raw", action="store_true",
                    help="enrich_raw_close 备源改用天勤 TqsdkSource 深拉名义价"
                         "（替代 sina）；用于截断分区补 07-17 后 raw_close")
    args = ap.parse_args(argv)

    scale: float | None = args.scale
    if args.no_scale_fix:
        if scale is not None and scale != 1.0:
            print(f"[FAIL] --no-scale-fix 与 --scale {scale} 冲突（前者等价 --scale 1.0）")
            return 2
        scale = 1.0

    from hexbroker.data.rebuild import RebuildNeeded, rebuild_partition
    from hexbroker.data.store import DataLake

    p6_4 = _load_p6_4()

    persisted = Path(args.persisted)
    if not persisted.exists():
        print(f"[FAIL] 真值文件不存在: {persisted}")
        return 2
    if not args.apply:
        print("[DRY-RUN] 预览模式，不写盘（加 --apply 执行重建）")

    # 1) 解析 + 规范化（复用 p6_4 已测路径；scale 显式给定时绕过 RAW_SCALE_FIX）
    df = p6_4.load_persisted_rows(persisted)
    print(f"[PARSE] 真值文件 {len(df)} 行，列: {list(df.columns)[:12]}...")
    underlying, sym0 = p6_4.normalize_sym_arg(args.sym)
    truth = p6_4.normalize_new_df(
        df, underlying, sym0,
        date(args.year, 1, 1), date(args.year, 12, 31),
        scale=scale,
    )
    if truth.empty:
        print(f"[FAIL] 规范化后无 {sym0} 在 {args.year} 年的行 —— "
              f"检查拉取参数（underlying_symbol/日期区间/method）")
        return 1
    year_rows = truth[truth["datetime"].dt.year == args.year]
    if len(year_rows) != len(truth):
        print(f"[WARN] 真值含非 {args.year} 年行 {len(truth) - len(year_rows)} 条，"
              f"已过滤（rebuild_partition 亦有跨界拒绝双保险）")
        truth = year_rows
    print(f"[PARSE] 目标真值 {len(truth)} 行，"
          f"{truth['datetime'].min().date()} ~ {truth['datetime'].max().date()}")

    # 2) raw_close 处置（三选一，互斥优先级：keep-raw-close > skip-nominal > 回填）
    #    序 3 前提：raw_close 是 ag0/au0/m0 三列中唯一正确的列，
    #    sina 备源有 ~0.8% 单日坏 bar，回填反会损伤它。
    target = (Path(args.data_root) / "processed" / sym0 / "1d"
              / f"{args.year}.parquet")
    replacing = target.exists()
    cur = None
    if replacing:
        cur = pd.read_parquet(target)
        cur["datetime"] = pd.to_datetime(cur["datetime"]).dt.normalize()

    if args.keep_raw_close:
        if not replacing:
            print(f"[FAIL] --keep-raw-close 需要既有分区以继承 raw_close，"
                  f"但 {target} 不存在（新建分区无源可继承）；"
                  f"若确需新建请改用 --skip-nominal 或默认回填路径")
            return 1
        assert cur is not None
        if "raw_close" not in cur.columns:
            print("[FAIL] 既有分区缺 raw_close 列，无法继承 —— 中止（零写盘）")
            return 1
        by_date = (cur.drop_duplicates(subset="datetime", keep="last")
                      .set_index("datetime")["raw_close"])
        if cur["datetime"].duplicated().any():
            n_dup = int(cur["datetime"].duplicated().sum())
            print(f"[WARN] 既有分区含 {n_dup} 个重复日期，继承取最后一条")
        mapped = truth["datetime"].map(by_date)
        missing = truth.loc[mapped.isna(), "datetime"]
        if len(missing):
            print(f"[FAIL] --keep-raw-close：真值 {len(truth)} 行中 "
                  f"{len(missing)} 个日期在既有分区中不存在"
                  f"（首个 {missing.iloc[0].date()}，"
                  f"末个 {missing.iloc[-1].date()}）—— "
                  f"禁止填 NaN / 禁止回退到 adj 复制品，中止（零写盘）。"
                  f"请检查真值拉取区间是否与既有分区交易日一致")
            return 1
        truth = truth.copy()
        truth["raw_close"] = mapped.astype(float).values
        print(f"[RAW-CLOSE] {sym0}: 从既有分区逐行继承 raw_close "
              f"{len(truth)} 行（跳过 enrich_raw_close，唯一正确列零损伤）")
    elif args.skip_nominal:
        print(f"[NOMINAL] {sym0}: 已跳过（--skip-nominal），raw_close 仍为 adj 复制品")
    else:
        fetcher = TqsdkRawFetcher() if args.tqsdk_raw else None
        if args.tqsdk_raw:
            print(f"[RAW-CLOSE] {sym0}: 备源=天勤 TqsdkSource（替代 sina 深拉名义价）")
        truth, notes = p6_4.enrich_raw_close(truth, sym0, fetcher=fetcher)
        for note in notes:
            tag = "[NOMINAL]" if "回填成功" in note else "[WARN][NOMINAL]"
            print(f"{tag} {sym0}: {note}")

    # 3) 既有分区前置预检（整年替换语义下，真值缺口 = 脏行残留，禁止）
    drop_dates: list = []
    if replacing:
        col_diff_t = sorted(set(truth.columns) - set(cur.columns))
        col_diff_c = sorted(set(cur.columns) - set(truth.columns))
        if col_diff_t or col_diff_c:
            print(f"[FAIL] 列集合不一致：仅真值有 {col_diff_t[:6]}，"
                  f"仅分区有 {col_diff_c[:6]} —— 拒绝替换")
            return 1
        uncovered = sorted(set(cur["datetime"]) - set(truth["datetime"]))
        cur_eff = cur
        if uncovered:
            if not args.drop_uncovered:
                print(f"[FAIL] 既有分区 {len(cur)} 行中 {len(uncovered)} 个日期"
                      f"未被真值覆盖（首个 {uncovered[0].date()}）—— "
                      f"替换将残留脏行，中止（零写盘）。"
                      f"请检查真值拉取区间/品种是否完整；"
                      f"若确认是节假日幽灵行，可加 --drop-uncovered 走三重校验剔除")
                return 1
            ref_cal, used = load_reference_calendar(args.data_root, args.year)
            if not used:
                print(f"[FAIL] --drop-uncovered：{args.year} 年在 15 个健康品种中"
                      f"找不到任何参考分区，参考日历为空会让第 2 条校验恒真"
                      f"—— 兜底锁失守，中止（零写盘）")
                return 1
            rows = judge_uncovered(cur, truth, ref_cal)
            print(f"[DROP-CHECK] 待删日期 {len(rows)} 个（参考日历："
                  f"{len(used)}/15 个健康品种 {args.year} 年并集，"
                  f"{len(ref_cal)} 个交易日）")
            print(f"[DROP-CHECK] {'#':>3}  {'date':<12}{'①真值未覆盖':<13}"
                  f"{'②非交易日':<12}{'③k==1(观察)':<14}  "
                  f"{'O/H/L/C':<34}{'k':<12}")
            for i, r in enumerate(rows, 1):
                ohlc = (f"{r['open']:.1f}/{r['high']:.1f}/"
                        f"{r['low']:.1f}/{r['close']:.1f}")
                print(f"[DROP-CHECK] {i:>3}  {str(r['date'].date()):<12}"
                      f"{'✅' if r['c1_not_in_truth'] else '❌':<13}"
                      f"{'✅' if r['c2_not_in_refcal'] else '❌':<12}"
                      f"{'✅' if r['c3_k_is_one'] else '❌(不拦截)':<14}  "
                      f"{ohlc:<34}{r['k']:<12.9f}")
            # 硬判据只有 ①②；③ 已于 2026-08-30 降级为观察项（见模块文档）
            bad = [r for r in rows
                   if not (r["c1_not_in_truth"] and r["c2_not_in_refcal"])]
            n_c3_miss = sum(1 for r in rows if not r["c3_k_is_one"])
            if bad:
                print(f"[FAIL] --drop-uncovered：{len(bad)}/{len(rows)} 个待删日期"
                      f"未通过硬校验（首个 {bad[0]['date'].date()}）—— "
                      f"任一硬判据不中即中止（零写盘）。"
                      f"校验①不中 = 该日期其实在真值中，属逻辑错误；"
                      f"校验②不中 = 该日期可能是真实交易日，禁止删除")
                return 1
            if n_c3_miss:
                print(f"[DROP-CHECK] 注意：{n_c3_miss}/{len(rows)} 个待删日期"
                      f"的 k ≠ 1.0（观察项③未中），按 2026-08-30 裁决"
                      f"**不拦截**，仍照常删除 —— 若此数字异常偏大，"
                      f"说明污染形态与本轮不同，请人工复核后再放行")
            drop_dates = [r["date"] for r in rows]
            cur_eff = cur[~cur["datetime"].isin(drop_dates)]
            print(f"[DROP-CHECK] {sym0}/{args.year}: 硬校验①②全中 ✅，"
                  f"将剔除 {len(drop_dates)} 个幽灵行（分区 {len(cur)} → "
                  f"{len(cur_eff)} 行后再替换）")

        n_replaced = len(set(cur_eff["datetime"]) & set(truth["datetime"]))
        n_appended = len(truth) - n_replaced
        # 走到这里，cur_eff 的每个日期都必被真值覆盖（未覆盖的已在上面剔除或中止）
        full_cover = not (set(cur_eff["datetime"]) - set(truth["datetime"]))
        print(f"[PRECHECK] 既有分区 {len(cur)} 行"
              + (f"（剔除 {len(drop_dates)} 幽灵行后 {len(cur_eff)} 行）"
                 if drop_dates else "")
              + f"：将替换 {n_replaced} 行 + 追加 {n_appended} 行"
              + (f"（真值{'全覆盖 ✅' if full_cover else '未覆盖 ⛔'}）"))

    # 4) 重建（missing 路径；对既有分区 = 同日期逐行替换 + 新日期追加）
    item = RebuildNeeded(symbol=sym0, freq="1d", year=args.year,
                         kind="missing", dates=[], detail={
                             "driver": "scripts/p11_truth_rebuild.py",
                             "persisted": str(persisted),
                         })
    if not args.apply:
        action = "整年替换" if replacing else "新建"
        print(f"[DRY-RUN] 将{action} {sym0}/1d/{args.year}.parquet "
              f"({len(truth)} 行)"
              + (f"、剔除 {len(drop_dates)} 个幽灵行" if drop_dates else "")
              + (f" 并清除 _MISSING_{args.year}.json" if not replacing else "")
              + "；预览:")
        print(truth.head(3).to_string(index=False))
        return 0

    res = rebuild_partition(Path(args.data_root), item, truth,
                            drop_dates=drop_dates or None)
    print(f"[REBUILD] {res.symbol}/{res.year}({res.kind}) status={res.status} "
          f"rows {res.rows_before}→{res.rows_after} "
          f"appended={len(res.appended)} replaced={len(res.replaced)} "
          f"dropped={len(res.dropped)} "
          f"marker_cleared={res.sidecar_cleared}"
          + (f" reason={res.reason}" if res.reason else ""))
    if res.status != "OK":
        print(f"[FAIL] 重建未完成：{res.reason}")
        return 1

    # 5) 重建后验证：行数 / 洞告警消除 / 年度边界连续性（前后年通用）
    lake = DataLake(root=args.data_root)
    bf = lake.load_processed(sym0, "1d", warn=False)
    dts = bf.df.index.get_level_values("datetime")
    print(f"[VERIFY] {sym0} 全量 {len(bf.df)} 行，{dts.min().date()} ~ {dts.max().date()}")

    notes = lake.quality_notes(sym0, "1d")
    holes = [n for n in notes if n.kind == "hole"]
    print(f"[VERIFY] quality_notes hole 数: {len(holes)}"
          + (f"（仍剩: {[n.year for n in holes]}）" if holes else "（目标洞已消除）"))

    adj = bf.df["adj_close"].reset_index(drop=True)
    dt = pd.Series(dts)
    y_prev = args.year - 1
    y_next = args.year + 1
    prev_end = adj[dt.dt.year == y_prev].iloc[-1] if (dt.dt.year == y_prev).any() else None
    y_first = adj[dt.dt.year == args.year].iloc[0]
    y_last = adj[dt.dt.year == args.year].iloc[-1]
    next_first = adj[dt.dt.year == y_next].iloc[0] if (dt.dt.year == y_next).any() else None
    for label, a, b in ((f"{y_prev}→{args.year}", prev_end, y_first),
                        (f"{args.year}→{y_next}", y_last, next_first)):
        if a is None or b is None or not a or not b:
            print(f"[VERIFY] 边界 {label}: 数据不足，跳过")
            continue
        jump = abs(b / a - 1)
        flag = "OK" if jump <= 0.10 else "🔴 可疑假跳变（>10%，需人工复核）"
        print(f"[VERIFY] 边界 {label}: adj 跳变 {jump:.4%} —— {flag}")

    print(f"[OK] P0-11 完成：{sym0}/{args.year} 真值重建"
          + ("（整年替换）" if replacing else "（新建分区）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
