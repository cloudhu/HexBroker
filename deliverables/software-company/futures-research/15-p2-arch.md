# P2 双治理 + 落地（六方案 P2 项）· 架构师增量设计 + 任务分解

> 输入：`14-p2-prd.md`（PM 需求池 P2-1~P2-4 + Q1~Q5）
> 约束：无证据不翻转 / 549 测试全绿 / 红线零顶层重依赖 / 默认零行为变更

## 0. TL;DR

新增**纯增量治理控制平面** `hexbroker/governance/`，不触碰任何既有交易逻辑。三件套（寄存器/联锁/账本）+ 一份 Yaml + 启动自检接线。

**Q 裁决（采纳 PRD）**
- Q1 → **fail-safe**：联锁拒绝 = `log.warning` + 强制 `shadow`，不崩溃、不阻断启动。
- Q2 → 账本落盘 `data/governance/calibration_ledger.json`。
- Q3 → **不合并** `paper.yaml` 的 `symbols.mode`（合约级 vs 策略级维度不同）；本批次仅策略级方案治理。
- Q4 → 1B/2B 缺 WR/n **不阻塞**（status 已 NOT_PASS，联锁天然拦截；账本标 `pending_qa`）。
- Q5 → 本批次**仅静态登记 + 启动自检**；运行时动态降级留后续批次（避免热路径侵入）。

## 1. 模块边界与依赖

```
hexbroker/governance/
  __init__.py      # 导出 SchemeStatus/Scheme/SchemeRegistry/assert_safe_to_activate/
                   #   resolve_scheme_mode/CalibrationRecord/CalibrationLedger
  scheme.py        # SchemeStatus(Enum) / Scheme(dataclass) / SchemeRegistry(load+verify_all)
  interlock.py     # MisOpenBlocked / ResolvedMode / assert_safe_to_activate / resolve_scheme_mode
  ledger.py        # CalibrationRecord / CalibrationLedger(load/save/append/get, 原子写)

configs/scheme_governance.yaml   # 6 方案定案（status + data_root_cause + gate_metrics + calib_ref）

tests/test_scheme_governance.py  # 单元：联锁4类reason + fail-safe + 账本往返/追加 + 寄存器verify_all

接线点：paper_trading_main.py（startup 自检）— 仅 WARNING + 强制 shadow，零侵入 tick
```

**零依赖约束**：`governance/` 仅用标准库（`dataclasses`/`enum`/`json`/`pathlib`/`datetime`）+ 项目既有 `utils.logging`。**禁止**引入 `vnpy_ctp`/`mlflow`/`qlib` 等；如确需（本批次不需）一律函数内 `try import`。

## 2. 数据模型

```python
# scheme.py
class SchemeStatus(str, Enum):
    PASS = "PASS"                 # 过双闸门，可晋升 LIVE（须有校准记录）
    SHADOW_ONLY = "SHADOW_ONLY"   # 仅影子/模拟，不可实盘
    DEPRECATED = "DEPRECATED"     # 弃用
    NOT_PASS = "NOT_PASS"         # 未过门禁，维持影子

@dataclass
class Scheme:
    scheme_id: str
    name: str
    status: SchemeStatus
    data_root_cause: str
    gate_metrics: dict            # 可选：{wr, n, maxdd, net_pnl_vs_cost, pbo, dsr}
    calibration_ref: str | None   # 指向 ledger 中记录 id

# ledger.py
@dataclass
class CalibrationRecord:
    scheme_id: str
    status: SchemeStatus
    verdict: str                  # "PASS" / "NOT_PASS" / "DEPRECATED"
    gate1_dir_acc: float | None   # 闸门1 方向准确率
    gate2_oos_cost: bool | None   # 闸门2 OOS计成本达标
    gate2_pbo: float | None       # PBO < 0.5
    gate2_dsr: float | None       # DSR > 0
    n: int | None
    maxdd: float | None
    net_pnl_vs_cost: float | None
    data_root_cause: str
    lead_verdict: str
    calibrated_at: str            # ISO8601
    source_doc: str
    pending_qa: bool = False      # 1B/2B 缺 WR/n 标记
```

## 3. 联锁语义（interlock.py）

```python
class MisOpenBlocked(Exception):
    def __init__(self, scheme_id, reason: str): ...

class ResolvedMode(str, Enum):
    LIVE = "live"
    SHADOW = "shadow"

def assert_safe_to_activate(scheme_id, registry) -> None:
    s = registry.get(scheme_id)
    if s is None:
        raise MisOpenBlocked(scheme_id, "unknown_scheme")
    if s.status != SchemeStatus.PASS:
        raise MisOpenBlocked(scheme_id, _reason_of(s.status))  # not_pass/deprecated/shadow_only
    if not registry.has_calibration(scheme_id):
        raise MisOpenBlocked(scheme_id, "no_calibration")

def resolve_scheme_mode(requested_mode, scheme_id, registry) -> tuple[ResolvedMode, str | None]:
    if requested_mode in ("live", "trade"):
        try:
            assert_safe_to_activate(scheme_id, registry)
            return ResolvedMode.LIVE, None
        except MisOpenBlocked as e:
            return ResolvedMode.SHADOW, e.reason   # fail-safe
    return ResolvedMode.SHADOW, None
```

**默认 fail-safe（R3）**：`SchemeRegistry.get` 对未知方案返回 `None` → `unknown_scheme` 拦截 → shadow。

## 4. 账本原子写（ledger.py）

复用项目既有安全写约定（`.tmp` + `os.replace`，与 `paper/broker.py` snapshot 同款）：
```python
def save(self, path):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(self._to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)   # 原子
```
追加：`append(record)` 更新 `current[scheme_id]` + 留 `history` 列表（append-only 留痕）。

## 5. 启动自检接线（paper_trading_main.py）

```python
# startup，仅 WARNING，零侵入 tick
registry = SchemeRegistry.load("configs/scheme_governance.yaml")
live, shadow = [], []
for sid in registry.ids():
    mode, reason = resolve_scheme_mode("live", sid, registry)
    if mode is ResolvedMode.LIVE:
        live.append(sid)
    else:
        shadow.append(sid)
        if reason:
            log.warning("[GOV] 方案 %s 未过治理联锁(%s)，强制 SHADOW_ONLY", sid, reason)
log.info("[GOV] 治理自检：可实盘=%s，强制影子=%s", live, shadow)
```
> 接线**仅决议启动期方案 mode**，不改信号/风控/撮合任何数值路径（R15）。

## 6. 任务分解（T01~T04）

| Task | 对应 PRD | 内容 | 风险 | 批次 |
|------|----------|------|------|------|
| **T01** | P2-3 | `scheme.py`：SchemeStatus/Scheme/SchemeRegistry(load+verify_all) + `configs/scheme_governance.yaml`（6方案定案） | 低（纯增量） | 批次一 |
| **T02** | P2-2 | `ledger.py`：CalibrationRecord/CalibrationLedger(load/save/append/get, 原子写) + 用权威数据预填 6 方案记录 | 低 | 批次一 |
| **T03** | P2-1 | `interlock.py`：MisOpenBlocked/ResolvedMode/assert_safe_to_activate/resolve_scheme_mode + `__init__.py` 导出 | 低 | 批次一 |
| **T04** | P2-4 | `paper_trading_main.py` startup 自检接线（WARNING+强制shadow，零侵入） | 低 | 批次二 |

**依赖**：T01 → T02（账本 ref 由 T01 寄存器消费）；T01+T02 → T03（联锁消费寄存器+账本）；T01+T02+T03 → T04（接线消费三件套）。批次一 T01‖T02 后 T03；批次二 T04。

**验收门禁（本批次内部）**：
- 549 测试全绿（0 回归）；红线审计零顶层重依赖。
- 默认零变更：`git diff` 既有交易/信号/风控代码为空（仅 `governance/` 新增 + `paper_trading_main.py` 追加自检块 + 配置）。
- 新测试 `test_scheme_governance.py` 覆盖：联锁 4 类 reason + 未知方案 fail-safe + 账本往返/追加幂等 + 寄存器 verify_all 对 NOT_PASS 要求校准记录。

## 7. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 接线误改 tick 行为 | 接线仅 startup 日志+决议，绝不进入 `_tick` 主循环；diff 复核 |
| 账本并发写损坏 | 原子写 `.tmp`+`replace`；单进程启动期写，无并发 |
| Yaml 与账本不一致 | `verify_all()` 启动期校验：非 SHADOW_ONLY 须有 calib_ref |
| 红线违反（误引重依赖） | `governance/` 仅标准库；CI（P1-10）已对新增模块 strict |

## 8. 不做（Out of Scope）

- 运行时动态降级（Q5 留后续批次）。
- 与 `paper.yaml symbols.mode` 合并（Q3）。
- 真实 CTP/SimNow 接线（P1-5 已延后）。
- 六方案终裁归档文档（独立批次，非本 P2）。
