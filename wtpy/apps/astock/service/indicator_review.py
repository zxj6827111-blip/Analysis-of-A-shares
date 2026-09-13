"""周五 EOD 链后的全市场指标复核（735 金叉及趋势 / 先跌后涨新版 5 日外）。

设计要点：
- 数据面与回测/导出一致：正式 L1（tushare_qfq）产品面，经 ``BaguaPlaneSession``
  一次索引、逐票加载（5200+ 只避免每票全仓扫描）。
- 命中定义：通达信公式 XG 信号在复核日（asof）当日为真。
- 幂等：结果按 ``storage/astock/indicator_review/review_{asof}.json`` 落盘，
  已存在且非 force 直接返回摘要；导出侧只读该 JSON，不重复计算。
- no_go 门控：正式 L1 产品面（复权因子派生产物）不存在时不产出信号——
  这是 formal_adjustment_ready「复权因子未就绪即 No-Go」口径在 qfq 数据面
  上的等价物（L1 面本身即 raw×adj_factor 的乘积，无正式 L1 即无正式复权）。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..config import AStockConfig
from ..data.tdx_reader import DayBar
from ..study import bars_dict_from_day, compute_indicator_signal, signal_dates

logger = logging.getLogger(__name__)

# (规则 ID, 导出 sheet 名)。规则来自 指标/ 目录（txt_ 前缀），
# 在 UI 规则列表中隐藏（hidden_rule_ids），但注册表可正常解析。
DEFAULT_REVIEW_RULES: List[Tuple[str, str]] = [
    ("txt_735金叉及趋势", "735"),
    ("txt_先跌后涨新版5日外", "5日外"),
]

# 导出侧允许回看的最大复核龄期（自然日）：周五链产出后周末/下周初导出
# 仍应带上两个 sheet；超过龄期视为过期（missing），避免陈旧信号误导。
REVIEW_MAX_AGE_DAYS = 7

_PROGRESS_EVERY = 250
_MAX_ERROR_RECORDS = 50


def review_output_path(cfg: AStockConfig, asof: int) -> Path:
    """复核结果 JSON 路径（导出侧复用同一函数定位文件）。"""
    return Path(cfg.storage_root) / "indicator_review" / f"review_{int(asof)}.json"


# 复核结果指纹键（GPT6 复核约束：规则/股票池/名称快照/行情面任一变化
# 即视为不同结果，不得互相复用）。
_FP_RULE = "rule_fingerprints"
_FP_UNIVERSE = "universe_fingerprint"
_FP_NAME = "name_snapshot_id"
_FP_SURFACE = "review_surface"


def _spec_fingerprint(spec) -> str:
    """规则内容指纹：优先 source_sha256，缺失时退化 formula_text 哈希。"""
    sha = getattr(spec, "source_sha256", None) or ""
    if sha:
        return str(sha)[:16]
    text = getattr(spec, "formula_text", "") or ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _review_fingerprint(
    specs: Sequence[Tuple[str, str, Any]],
    universe: Sequence[str],
    name_snapshot_id: str,
    surface: Dict[str, Any],
    eff_asof: int,
) -> Dict[str, Any]:
    rule_fps = {rid: _spec_fingerprint(spec) for rid, _sheet, spec in specs}
    universe_fp = hashlib.sha256(
        ",".join(sorted(set(universe))).encode("utf-8")
    ).hexdigest()[:16]
    return {
        _FP_RULE: rule_fps,
        _FP_UNIVERSE: universe_fp,
        _FP_NAME: name_snapshot_id or "",
        _FP_SURFACE: f"{surface.get('formal_l1_id') or ''}:{eff_asof}:{surface.get('max_date') or ''}",
    }


def _fingerprints_match(existing: Dict[str, Any], current: Dict[str, Any]) -> bool:
    """旧文件缺任一指纹键 → 无法验证 → 不匹配（不默认复用）。"""
    for key in (_FP_RULE, _FP_UNIVERSE, _FP_NAME, _FP_SURFACE):
        if key not in existing or existing.get(key) != current.get(key):
            return False
    return True


def _resolve_rules_for_fingerprint(
    cfg: AStockConfig, rules: Sequence[Tuple[str, str]]
) -> Optional[List[Tuple[str, str, Any]]]:
    """解析规则 spec 供指纹计算；任何失败返回 None（视为不可验证）。"""
    try:
        from ..indicators.registry import IndicatorRegistry

        reg = IndicatorRegistry.bootstrap(
            cfg.indicator_dir,
            cfg.mapping_path,
            user_registry_path=user_registry_file(cfg),
        )
        out: List[Tuple[str, str, Any]] = []
        for rid, sheet in rules:
            spec = reg.get(rid)
            if spec.compile_status != "ready":
                return None
            out.append((rid, sheet, spec))
        return out
    except Exception:  # noqa: BLE001
        return None


def _no_go_reuse_fingerprints_ok(
    cfg: AStockConfig,
    rules: Sequence[Tuple[str, str]],
    codes: Optional[Sequence[str]],
    existing: Dict[str, Any],
) -> bool:
    """数据面不可用时旧 ok 结果的复用校验：规则/股票池/名称快照都一致才可复用。

    任何一项不可验证（解析失败/旧文件缺字段）都返回 False——不把旧结果
    冒充当前规则/池的结果。
    """
    specs = _resolve_rules_for_fingerprint(cfg, rules)
    if specs is None:
        return False
    cur_rules = {rid: _spec_fingerprint(spec) for rid, _s, spec in specs}
    if existing.get(_FP_RULE) != cur_rules:
        return False
    try:
        universe = _resolve_codes(cfg, codes)
    except Exception:  # noqa: BLE001
        return False
    universe_fp = hashlib.sha256(
        ",".join(sorted(set(universe))).encode("utf-8")
    ).hexdigest()[:16]
    if existing.get(_FP_UNIVERSE) != universe_fp:
        return False
    try:
        from .stock_names import ensure_stock_names_for

        _map, name_snapshot_id = ensure_stock_names_for(
            cfg, universe, [sp for _r, _s, sp in specs]
        )
    except Exception:  # noqa: BLE001
        return False
    if existing.get(_FP_NAME) != (name_snapshot_id or ""):
        return False
    return True


def _resolve_formal_surface(cfg: AStockConfig) -> Tuple[Optional[Dict[str, Any]], str]:
    """解析正式 L1 产品面。返回 (surface_info|None, no_go_reason)。

    surface_info: {formal_l1_id, max_date}，max_date 为数据面最新可得交易日。
    """
    from ..data.dataset_store import DatasetStore
    from ..data.tushare_product import resolve_active_tushare_product_pair

    md_root = getattr(cfg, "market_data_root", None)
    if not md_root or not Path(md_root).exists():
        return None, f"market_data_root 不存在: {md_root}"
    try:
        store = DatasetStore(md_root)
        pair = resolve_active_tushare_product_pair(store, deep_copy=False)
    except Exception as e:  # noqa: BLE001
        return None, f"formal_product_pair_resolve_failed: {e}"
    if pair is None or not pair.l1_dataset_id:
        return None, "no_formal_l1_product（复权因子未就绪，正式 L1 产品面缺失）"
    # 真实行情最后日优先于 cutoff：cutoff 可能被 derive 到请求日/今天，
    # 超前于数据面实际最后一根 K 线，按它扫描会全市场无命中。
    max_date = int(pair.l1_max_date or pair.cutoff or 0)
    if max_date <= 0:
        return None, "formal_l1_max_date_unknown"
    return {"formal_l1_id": pair.l1_dataset_id, "max_date": max_date}, ""


def _default_bar_loader_factory(cfg: AStockConfig) -> Callable[[str, int], Tuple[List[DayBar], Dict[str, Any]]]:
    """构建基于 BaguaPlaneSession 的逐票加载器（session 只索引一次仓库）。"""
    from .bagua_query import BaguaPlaneSession, load_day_bars_for_plane

    holder: Dict[str, Any] = {}

    def _load(code: str, asof: int) -> Tuple[List[DayBar], Dict[str, Any]]:
        if "session" not in holder:
            holder["session"] = BaguaPlaneSession(cfg, "tushare_qfq")
        # asof 选数据集版本锚点，end=asof 截断未来 K 线（防信号用到复核日之后的数据）
        bars, meta = load_day_bars_for_plane(
            cfg, code, "tushare_qfq", asof=asof, end=asof, session=holder["session"]
        )
        return bars, meta

    return _load


def _normalize_asof(cfg: AStockConfig, asof: Optional[int], surface_max: int) -> int:
    """asof 收敛：默认取数据面最新交易日；显式传入时不超过数据面；
    日历可用且 asof 在日历覆盖范围内时回退到最近交易日（周末/节假日触发）。"""
    out = int(surface_max if asof is None else min(int(asof), int(surface_max)))
    cal_path = Path(cfg.calendar_path)
    if cal_path.exists():
        try:
            from ..data.calendar import TradeCalendar

            cal = TradeCalendar.load(cal_path)
            if cal.dates and out <= cal.dates[-1]:
                prior = [d for d in cal.dates if d <= out]
                if prior:
                    out = max(prior)
        except Exception as e:  # noqa: BLE001
            logger.warning("indicator_review 日历归一失败（按原 asof）: %s", e)
    return out


def resolve_review_asof(cfg: AStockConfig, requested_asof: int) -> Tuple[int, str]:
    """信号基准日收敛到真实数据面，返回 (实际使用的基准日, note)。

    - 请求日 > 数据面最后日：回退到数据面最后日再经 ``_normalize_asof``
      落到交易日，note 说明回退原因（导出侧写入 meta/响应头）；
    - 请求日 <= 数据面：按请求日归一（周末/节假日回退最近交易日），note 空串；
    - 数据面不可用（无正式 L1）：原样返回请求日与空 note，保持旧行为，
      由调用方走 no_go / 即时计算路径。
    """
    requested = int(requested_asof)
    surface, _reason = _resolve_formal_surface(cfg)
    if surface is None:
        return requested, ""
    surface_max = int(surface.get("max_date") or 0)
    if surface_max <= 0:
        return requested, ""
    if requested > surface_max:
        eff = _normalize_asof(cfg, surface_max, surface_max)
        note = (
            f"fallback_date:请求 {requested} 超出数据覆盖 {surface_max}，"
            f"信号按 {eff} 计算"
        )
        return eff, note
    return _normalize_asof(cfg, requested, surface_max), ""


def _resolve_codes(cfg: AStockConfig, codes: Optional[Sequence[str]]) -> List[str]:
    """票池：显式 codes（str 逗号分隔或序列）否则与导出 stock-all 同源。

    同源解析 = universe.json 优先，缺失时走 Tushare raw 基线派生
    （backtest_universe._universe_from_data_root）——Tushare-only 部署从不
    产生 universe.json，只认文件会让每周复核在服务器上永远失败。但绝不
    接受空池或 DEMO_CODES 兜底（复核语义要求全市场口径，不静默降级），
    且与导出池同源保证「命中 ∩ stock_pool」交集有意义。
    """
    from ..data.universe import AShareUniverse, to_std_code

    if codes:
        if isinstance(codes, str):
            parts = [c.strip() for c in codes.split(",") if c.strip()]
        else:
            parts = [str(c).strip() for c in codes if str(c).strip()]
        out: List[str] = []
        for c in parts:
            if c.startswith(("SSE.", "SZSE.", "BSE.")):
                out.append(c)
            else:
                out.append(to_std_code(c))
        return out
    upath = Path(cfg.universe_path)
    if upath.exists():
        pool = AShareUniverse.load(upath).codes()
        source = str(upath)
    else:
        from .backtest_universe import DEMO_CODES, _universe_from_data_root

        pool = _universe_from_data_root(cfg)
        source = "tushare_raw_baseline"
        if not pool or list(pool) == list(DEMO_CODES):
            pool = []
    if not pool:
        raise FileNotFoundError(
            f"无法确定全市场票池：{upath} 不存在且数据仓库无可用 Tushare "
            f"raw 基线（复核要求全市场口径，不静默降级到 demo 票）"
        )
    logger.info("indicator_review 票池 %d 只（来源 %s）", len(pool), source)
    return pool


_SHEET_ILLEGAL_CHARS = set('[]:*?/\\')
# Excel 保留 sheet 名（历史追踪用 History）与工作簿自有 sheet，冲突时回退
# index-all 是导出侧内置的大盘指数表：Excel sheet 名大小写不敏感，漏收会让
# 名为 "Index-All" 的规则与工作簿自带 index-all 撞名，文件被 Excel 判为损坏
_SHEET_RESERVED_NAMES = {"meta", "stock-all", "index-all", "etf-all", "history"}
# Excel 会把以这些字符开头的单元格当公式；sheet 名同样禁用其起始
_SHEET_FORMULA_LEAD = ("=", "+", "-", "@")


def _bad_sheet_char(ch: str) -> bool:
    """Excel/XML 1.0 非法字符：C0、\\x7f、C1、U+FFFE/U+FFFF 与孤立代理。"""
    code = ord(ch)
    return (
        ch in _SHEET_ILLEGAL_CHARS
        or code < 32
        or 0x7F <= code <= 0x9F
        or code in (0xFFFE, 0xFFFF)
        or 0xD800 <= code <= 0xDFFF
    )


def _sanitize_sheet_name(name: str, rule_id: str) -> str:
    """把规则名收敛为合法且安全的 Excel sheet 名：过滤非法/控制字符
    （C0/C1、\\x7f、U+FFFE/U+FFFF、孤立代理）、处理首尾单引号、公式起始
    字符替换为 ``_``、限 31 字符；空名/保留字冲突时回退 rule_id，回退后
    仍非法/保留则用 ``rule_<hash>`` 固定安全名，保证任何输入都不抛。
    非默认规则（如 user_*）做 sheet 名不可读，优先用规则显示名。"""

    def _clean(value: str) -> str:
        return "".join(
            "_" if _bad_sheet_char(ch) else ch for ch in str(value)
        ).strip().strip("'").strip()

    cleaned = _clean(name)
    if cleaned[:1] in _SHEET_FORMULA_LEAD:
        cleaned = "_" + cleaned[1:]
    if len(cleaned) > 31:
        cleaned = cleaned[:31].rstrip("_").strip()
    if not cleaned or cleaned.lower() in _SHEET_RESERVED_NAMES:
        fallback = _clean(rule_id)
        if fallback[:1] in _SHEET_FORMULA_LEAD:
            fallback = "_" + fallback[1:]
        fallback = fallback[:31]
        if fallback and fallback.lower() not in _SHEET_RESERVED_NAMES:
            return fallback
        digest = hashlib.sha1(
            str(rule_id).encode("utf-8", "replace")
        ).hexdigest()[:8]
        return f"rule_{digest}"
    return cleaned


def user_registry_file(cfg: AStockConfig) -> Path:
    """用户规则注册表路径（RuleService 写入、复核/导出只读合并）。"""
    return Path(cfg.storage_root) / "indicators" / "user_registry.json"


def resolve_rule_sheet_names(
    cfg: AStockConfig, rule_ids: Sequence[str]
) -> Dict[str, str]:
    """解析规则的占位 sheet 名：DEFAULT_REVIEW_RULES 短名 > user_registry.json /
    系统注册表显示名 > rule_id；结果已过 ``_sanitize_sheet_name``。

    仅用于即时计算整体失败（error/no_go）时的空 sheet 兜底命名，只读用户
    注册表文件，不触发 RuleService 的全量扫描/写 registry.json 副作用。
    """
    out: Dict[str, str] = {}
    default_sheets = dict(DEFAULT_REVIEW_RULES)
    for rid in rule_ids:
        if rid in default_sheets:
            out[rid] = _sanitize_sheet_name(default_sheets[rid], rid)
    pending = [rid for rid in rule_ids if rid not in out]
    if not pending:
        return out

    names: Dict[str, str] = {}
    try:
        upath = user_registry_file(cfg)
        if upath.exists():
            from ..indicators.registry import IndicatorRegistry

            for s in IndicatorRegistry.load(upath).list():
                names.setdefault(s.id, s.name)
    except Exception as e:  # noqa: BLE001
        logger.warning("indicator_review 用户注册表读取失败（占位名回退）: %s", e)

    pending = [rid for rid in pending if rid not in names]
    indicator_dir = getattr(cfg, "indicator_dir", None)
    mapping_path = getattr(cfg, "mapping_path", None)
    if pending and indicator_dir and mapping_path:
        try:
            from ..indicators.registry import IndicatorRegistry

            reg = IndicatorRegistry.bootstrap(indicator_dir, mapping_path)
            for s in reg.list():
                names.setdefault(s.id, s.name)
        except Exception as e:  # noqa: BLE001
            logger.warning("indicator_review 系统注册表扫描失败（占位名回退）: %s", e)

    for rid in rule_ids:
        if rid not in out:
            out[rid] = _sanitize_sheet_name(names.get(rid) or rid, rid)
    return out


def run_weekly_review(
    cfg: AStockConfig,
    asof: Optional[int] = None,
    *,
    rule_ids: Optional[Sequence[str]] = None,
    codes: Optional[Sequence[str]] = None,
    force: bool = False,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    bar_loader: Optional[Callable[[str, int], Tuple[List[DayBar], Dict[str, Any]]]] = None,
    surface_resolver: Optional[Callable[[AStockConfig], Tuple[Optional[Dict[str, Any]], str]]] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """全市场复核指标公式在 asof 当日的 XG 命中，产出 review_{asof}.json。

    返回摘要 dict（同落盘 JSON）。``bar_loader`` / ``surface_resolver`` 为测试
    注入点，默认走正式 L1 产品面 + BaguaPlaneSession 加载。
    ``persist=False`` 供导出侧即时计算自定义规则：不读也不写
    review_{asof}.json（该文件是周五链与默认导出的共享数据源，
    不得被临时计算覆盖），仅返回结果。
    """
    t0 = time.time()
    sheet_of = dict(DEFAULT_REVIEW_RULES)
    rules = list(DEFAULT_REVIEW_RULES)
    if rule_ids:
        # 非默认规则优先用规则显示名做 sheet 名（user_* ID 不可读）；
        # 显示名在 spec 解析后再 sanitize（非法字符/31 字符/保留字回退）。
        # 显式回传的默认规则 ID 先映射回短名，确保 no_go 摘要与 ok 路径一致。
        rules = [(rid, sheet_of.get(rid, rid)) for rid in rule_ids]

    resolve_surface = surface_resolver or _resolve_formal_surface
    surface, no_go_reason = resolve_surface(cfg)

    if surface is None:
        # 复权因子/正式 L1 未就绪：不扫描、不产出信号，落 no_go 供导出侧识别
        summary = {
            "asof": int(asof) if asof else 0,
            "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "no_go",
            "no_go_reason": no_go_reason,
            "universe_size": 0,
            "scanned": 0,
            "error_count": 0,
            "errors": [],
            "rules": [
                {"rule_id": rid, "sheet": sheet, "count": 0, "matched": []}
                for rid, sheet in rules
            ],
            "duration_sec": round(time.time() - t0, 1),
        }
        # no_go 也落盘（asof 未知时用 0 占位无意义——仅在 asof 可确定时写文件）
        # 但不得覆盖已有 ok 结果：产品面指针短暂不可用时，无 force 的重跑
        # 若把 ok 冲成 no_go，导出侧会白白丢掉两个 sheet。
        # 复用旧 ok 前必须指纹校验（规则/股票池/名称快照一致才可信）；
        # 不匹配时不再返回旧 ok matched，写 no_go 覆盖（GPT6 复核约束）。
        # persist=False（导出/筛选的临时即时计算）下缓存读/写全部跳过：
        # 数据面不可用必须原样返回 no_go，不得把已有周报缓存冒充本次结果。
        if summary["asof"]:
            path = review_output_path(cfg, summary["asof"])
            if persist and path.exists() and not force:
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    existing = None
                if (
                    existing is not None
                    and existing.get("status") == "ok"
                    and _no_go_reuse_fingerprints_ok(cfg, rules, codes, existing)
                ):
                    existing["reused"] = True
                    logger.info(
                        "indicator_review 表面不可用但已有 ok 结果（指纹一致），保留不覆盖: %s",
                        path,
                    )
                    return existing
                if existing is not None and existing.get("status") == "ok":
                    logger.warning(
                        "indicator_review 表面不可用且旧 ok 指纹不匹配（规则/池/名称快照已变），"
                        "不返回旧 matched，写 no_go: %s",
                        path,
                    )
            if persist:
                _atomic_write_json(path, summary)
        return summary

    eff_asof = _normalize_asof(cfg, asof, int(surface["max_date"]))
    out_path = review_output_path(cfg, eff_asof)

    # 规则 spec：编译不可用直接抛错（CLI 非零退出），不产出半截结果。
    # 解析提前到幂等判断之前——复用校验需要规则指纹。
    from ..indicators.registry import IndicatorRegistry

    reg = IndicatorRegistry.bootstrap(
        cfg.indicator_dir,
        cfg.mapping_path,
        user_registry_path=user_registry_file(cfg),
    )
    specs: List[Tuple[str, str, Any]] = []
    for rid, sheet in rules:
        spec = reg.get(rid)  # KeyError 即规则不存在
        if spec.compile_status != "ready":
            raise RuntimeError(f"规则 {rid} 编译状态 {spec.compile_status}: {spec.failure_reason}")
        # 默认两条保持既有 sheet 名（显式传 rule_ids 时 rules 内是完整 ID，
        # 必须回查 sheet_of 取短名）；其余用显示名 sanitize（见 _sanitize_sheet_name）
        eff_sheet = sheet_of[rid] if rid in sheet_of else _sanitize_sheet_name(spec.name, rid)
        specs.append((rid, eff_sheet, spec))

    universe = _resolve_codes(cfg, codes)

    # NAMELIKE 名称快照：任一规则用到即解析（缺名称的票运行时报错可见）。
    from .stock_names import ensure_stock_names_for

    stock_name_map, name_snapshot_id = ensure_stock_names_for(
        cfg, universe, [sp for _r, _s, sp in specs]
    )

    current_fp = _review_fingerprint(specs, universe, name_snapshot_id, surface, eff_asof)

    # 幂等复用必须指纹一致（规则/股票池/名称快照/行情面）；旧文件缺指纹
    # 或任一项不匹配 → 无法验证/规则已变 → 重算，不把旧结果冒充本次结果。
    if persist and out_path.exists() and not force:
        try:
            cached = json.loads(out_path.read_text(encoding="utf-8"))
            if cached.get("status") == "ok" and _fingerprints_match(cached, current_fp):
                cached["reused"] = True
                logger.info("indicator_review 幂等命中（指纹一致）: %s", out_path)
                return cached
            logger.warning(
                "indicator_review 旧结果指纹不匹配（规则/池/名称快照已变或无法验证），重算: %s",
                out_path,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("indicator_review 缓存损坏（重算）: %s", e)

    load = bar_loader or _default_bar_loader_factory(cfg)

    matched_by_rule: Dict[str, List[Dict[str, Any]]] = {rid: [] for rid, _s, _sp in specs}
    errors: List[Dict[str, str]] = []
    error_count = 0
    scanned = 0
    # 质量统计（PLAN-BAGUA-UX-V1.1 筛选侧「未完成评估」依据）：
    # missing_count = asof 当日无 K 线（停牌/退市/未上市）；failed_codes =
    # 加载或计算失败的去重票集合（不受 errors 截断影响，供命中完整性过滤）
    missing_count = 0
    failed_codes: set = set()
    n = len(universe)

    from ..forecast.name_norm import normalize_stock_code

    for i, code in enumerate(universe):
        try:
            bars, _meta = load(code, eff_asof)
            if not bars or int(bars[-1].date) != eff_asof:
                # asof 当日无 K 线（停牌/退市/未上市）：无信号可言，跳过不计错
                scanned += 1
                missing_count += 1
            else:
                bars_dict = bars_dict_from_day(bars)
                dates_arr = bars_dict["date"]
                close = float(bars[-1].close)
                stock_name = stock_name_map.get(normalize_stock_code(code), "")
                for rid, _sheet, spec in specs:
                    sig, err = compute_indicator_signal(spec, bars_dict, stock_name=stock_name)
                    if err:
                        error_count += 1
                        failed_codes.add(code)
                        if len(errors) < _MAX_ERROR_RECORDS:
                            errors.append({"code": code, "rule": rid, "error": err})
                        continue
                    hit_dates = signal_dates(dates_arr, sig)
                    if hit_dates and hit_dates[-1] == eff_asof:
                        matched_by_rule[rid].append({"code": code, "close": close})
                scanned += 1
        except Exception as e:  # noqa: BLE001
            error_count += 1
            failed_codes.add(code)
            if len(errors) < _MAX_ERROR_RECORDS:
                errors.append({"code": code, "rule": "*", "error": str(e)})
        done = i + 1
        if done == 1 or done % _PROGRESS_EVERY == 0 or done == n:
            msg = f"指标复核 {done}/{n}（asof={eff_asof}）"
            logger.info(msg)
            if on_progress:
                try:
                    on_progress({"done": done, "total": n, "message": msg})
                except Exception:  # noqa: BLE001
                    pass

    summary = {
        "asof": eff_asof,
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "ok",
        "no_go_reason": "",
        "formal_l1_id": surface.get("formal_l1_id"),
        # 复核结果指纹：导出/复用侧校验规则版本/股票池/名称快照/行情面，
        # 任一变化不得互相复用（旧文件缺这些键 = 无法验证 = 不复用）。
        **current_fp,
        "universe_size": n,
        "scanned": scanned,
        "missing_count": missing_count,
        "error_count": error_count,
        "errors": errors,
        "failed_codes": sorted(failed_codes)[:2000],
        "rules": [
            {"rule_id": rid, "sheet": sheet, "count": len(matched_by_rule[rid]),
             "matched": matched_by_rule[rid]}
            for rid, sheet, _spec in specs
        ],
        "duration_sec": round(time.time() - t0, 1),
    }
    if persist:
        _atomic_write_json(out_path, summary)
    logger.info(
        "indicator_review 完成 asof=%s scanned=%d 命中 %s 错误 %d 用时 %.1fs",
        eff_asof, scanned,
        {r["sheet"]: r["count"] for r in summary["rules"]},
        error_count, summary["duration_sec"],
    )
    return summary


def _export_rules_stale(
    cfg: AStockConfig, review: Dict[str, Any]
) -> Optional[str]:
    """导出侧规则指纹校验：文件内规则集与当前注册表逐条比对。

    - 旧文件缺 rule_fingerprints → 无法验证 → stale（不默认匹配）
    - 规则缺失（规则被删）或指纹变化（公式更新）→ stale
    - 返回 None = 一致可用；返回 str = stale 原因
    """
    fps = review.get(_FP_RULE)
    if not isinstance(fps, dict) or not fps:
        return "stale_rules:复核文件缺规则指纹（旧版结果，无法验证当前规则版本）"
    try:
        from ..indicators.registry import IndicatorRegistry

        reg = IndicatorRegistry.bootstrap(
            cfg.indicator_dir,
            cfg.mapping_path,
            user_registry_path=user_registry_file(cfg),
        )
    except Exception:  # noqa: BLE001
        return "stale_rules:规则注册表解析失败，无法验证复核结果版本"
    for rid, fp in fps.items():
        try:
            spec = reg.get(str(rid))
        except KeyError:
            return f"stale_rules:规则 {rid} 已不存在（复核时的规则被删除）"
        if _spec_fingerprint(spec) != fp:
            return f"stale_rules:规则 {rid} 公式已更新（复核时版本与当前不一致）"
    return None


def load_review_for_export(
    cfg: AStockConfig,
    asof: int,
    *,
    max_age_days: int = REVIEW_MAX_AGE_DAYS,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """导出侧读取复核结果。返回 (review|None, note)。

    优先精确匹配 ``review_{asof}.json``；缺失时回看 ``max_age_days`` 天内
    最近一次复核（周五链产出、周末/下周初导出仍带 sheet）。
    note 为空串表示正常；否则为 missing / no_go:<原因> / stale:<asof'> /
    stale_rules:<原因> 等。规则指纹不匹配返回 (None, stale_rules:...)，
    导出侧按 missing 语义走即时计算，不端出旧 sheet。
    """
    asof = int(asof)
    review_dir = Path(cfg.storage_root) / "indicator_review"

    def _load(path: Path) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    exact = _load(review_dir / f"review_{asof}.json")
    if exact is not None:
        if int(exact.get("asof") or 0) != asof:
            return None, f"corrupt:文件内 asof={exact.get('asof')} 与文件名不符"
        if exact.get("status") != "ok":
            return exact, f"no_go:{exact.get('no_go_reason') or 'unknown'}"
        stale_reason = _export_rules_stale(cfg, exact)
        if stale_reason:
            return None, stale_reason
        return exact, ""

    # 回看：目录内 asof' <= asof 的最新复核，龄期不超过 max_age_days
    candidates: List[int] = []
    if review_dir.exists():
        for p in review_dir.glob("review_*.json"):
            stem = p.stem  # review_YYYYMMDD
            digits = stem.split("_")[-1]
            if digits.isdigit() and len(digits) == 8:
                d = int(digits)
                if d <= asof:
                    candidates.append(d)
    if not candidates:
        return None, "missing:未找到复核文件（等待周五链完成或 CLI review-weekly 补跑）"
    latest = max(candidates)
    try:
        age = (
            _dt.datetime.strptime(str(asof), "%Y%m%d").date()
            - _dt.datetime.strptime(str(latest), "%Y%m%d").date()
        ).days
    except ValueError:
        return None, f"missing:复核文件名日期无效 {latest}"
    if age > int(max_age_days):
        return None, f"stale:最近复核 {latest} 已过期（{age} 天 > {max_age_days} 天）"
    review = _load(review_dir / f"review_{latest}.json")
    if review is None:
        return None, f"missing:复核文件 review_{latest}.json 读取失败"
    if int(review.get("asof") or 0) != latest:
        return None, f"corrupt:文件内 asof={review.get('asof')} 与文件名不符"
    if review.get("status") != "ok":
        return review, f"no_go:{review.get('no_go_reason') or 'unknown'}"
    stale_reason = _export_rules_stale(cfg, review)
    if stale_reason:
        return None, f"{stale_reason}（复核 {latest}）"
    return review, f"fallback:使用 {latest} 复核（导出日 {asof} 无当日复核）"


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
