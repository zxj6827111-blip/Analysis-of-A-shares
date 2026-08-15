"""Backtest service: multi-rule signals + PortfolioBacktester + reports."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from ..bagua.calculator import BaguaCalculator
from ..bagua.filter_rules import (
    DEFAULT_BAGUA_FILTER_MODE,
    DEFAULT_RULE_VERSION,
    GuaFilter,
    filter_events_by_bagua_mode,
    filter_events_by_gua_filter,
    gua_filter_history_summary,
    gua_filter_natural_language,
    mode_label as bagua_mode_label,
)
from ..config import AStockConfig, get_default_config
from ..data.adjustments import build_factor_series, factor_manifest_sha, formal_adjustment_ready
from ..data.calendar import TradeCalendar
from ..data.catalog import selected_universe_sha
from ..data.data_store import DataStore
from ..data.tdx_reader import TdxDayReader
from ..data.minline_reader import (
    MIN_VENDOR_60MIN_MAX_DATE,
    load_min60_daybars,
    load_min60_daybars_any,
    min60_bars_to_arrays,
)
from ..indicators.registry import IndicatorRegistry
from ..indicators.tn6_importer import load_source_map, resolve_formula_audit
from ..strategy import (
    PortfolioBacktester,
    filter_events_by_signal_weekdays,
    parse_price_session,
    parse_signal_weekdays,
    parse_single_weekday,
)
from ..study import (
    SignalEvent,
    attach_bagua,
    bars_dict_from_day,
    bars_dict_from_period,
    build_period_bars,
    combine_signals,
    compute_indicator_signal,
    compute_v5_dwm_resonance,
    day_bars_to_standard_qfq,
    day_bars_to_point_in_time_adjusted,
    day_bars_for_signals,
    day_bars_for_signals_affine,
    signal_dates,
)
from .rules import RuleService

from .backtest_universe import (  # noqa: F401
    DEMO_CODES,
    FULL_MARKET_TOKENS,
    _astock_code_sha,
    _is_full_market_token,
    select_universe,
)
from .backtest_request import (  # noqa: F401
    BacktestRequest,
    research_fingerprint_fields_from_request,
)

class BacktestService:
    def __init__(self, cfg: Optional[AStockConfig] = None):
        self.cfg = cfg or get_default_config()
        self.cfg.ensure_dirs()
        self.rules = RuleService(self.cfg)

    def run(
        self,
        req: BacktestRequest,
        *,
        progress_cb: Optional[Any] = None,
    ) -> Dict[str, Any]:
        return run_backtest(self.cfg, req, rules=self.rules, progress_cb=progress_cb)


def resolve_market_data_bindings(
    cfg: AStockConfig,
    req: BacktestRequest,
    codes: Sequence[str],
    *,
    check_symbol_coverage: bool = True,
) -> Dict[str, Any]:
    """Resolve + validate repo-mode dataset bindings BEFORE any run exists.

    Gate C D1: explicit dataset_id must match manifest source/adjustment/
    period/role/lineage — mismatches raise DatasetBindingError (HTTP 4xx),
    no run row, no SQLite write, no cache write, no fallback.
    Gate C D3 (product soft mode): requested codes not covered by the
    signal or execution dataset are **dropped from the run pool** with an
    explicit ``coverage_excluded`` list + warning — never treated as zero
    signal, never silently ignored. If every code is uncovered, still hard
    fail (empty pool).

    Mutates req: signal_adjustment (defaulted), dataset_id,
    execution_dataset_id, execution_adjustment and signal lineage fields.
    """
    from ..data.dataset_store import DatasetStore
    from ..data.repository import MarketDataRepository, DatasetNotFoundError
    from ..data.dataset_binding import (
        DatasetBindingError,
        classify_symbol_coverage,
        execution_resolve_candidates,
        manifest_symbol_index,
        signal_resolve_candidates,
        validate_execution_dataset_binding,
        validate_signal_dataset_binding,
    )

    _signal_data_source = getattr(req, "signal_data_source", None)
    _signal_adjustment = getattr(req, "signal_adjustment", None)
    _dataset_id = getattr(req, "dataset_id", None)
    # Product UI vocabulary: tdxquant / tushare / raw (+ legacy internal)
    if _signal_data_source == "raw":
        if not _signal_adjustment:
            _signal_adjustment = "none"
            req.signal_adjustment = "none"
        req.research_unadjusted = True
    elif not _signal_adjustment:
        from ..data.providers.base import DataSource, SIGNAL_SOURCE_ADJUSTMENT
        _src_enum = {
            "tdxquant": DataSource.TDXQUANT,
            "tushare": DataSource.TUSHARE,
            "internal": DataSource.INTERNAL,
        }.get(_signal_data_source)
        if _src_enum and _src_enum in SIGNAL_SOURCE_ADJUSTMENT:
            _signal_adjustment = SIGNAL_SOURCE_ADJUSTMENT[_src_enum].value
            # product "tushare前复权" prefers official qfq; derived is fallback
            if _signal_data_source == "tushare":
                _signal_adjustment = "qfq"
            elif _signal_data_source == "internal":
                _signal_adjustment = "tushare_factor_qfq"
            req.signal_adjustment = _signal_adjustment

    repo = MarketDataRepository(DatasetStore(cfg.market_data_root))

    def _resolve_signal_candidates():
        """Ordered (source, adjustment) pairs for product signal sources."""
        return signal_resolve_candidates(_signal_data_source, _signal_adjustment)

    def _product_family_pairs():
        """Pairs accepted for product UI keys (tushare/raw may span warehouse sources)."""
        return set(_resolve_signal_candidates())

    if _dataset_id:
        # Product keys (tushare/raw) may already be pinned to a fallback dataset
        # (e.g. tushare → internal/tushare_factor_qfq). Validate by family, not
        # literal source equality — otherwise job re-bind fails after API pre-resolve.
        _cands = _product_family_pairs()
        _is_product_family = _signal_data_source in ("tushare", "raw", "internal", "tdxquant")
        _allow_raw_signal = (_signal_data_source == "raw") or (
            (_signal_adjustment or "") in ("none", "composite_none")
        )
        if _is_product_family:
            try:
                _ds = repo.get_dataset(_dataset_id)
            except DatasetNotFoundError:
                raise DatasetBindingError(
                    "DATASET_NOT_FOUND",
                    f"Dataset not found: {_dataset_id}",
                    http_status=404,
                    dataset_id=_dataset_id,
                    requested_source=_signal_data_source,
                    requested_adjustment=_signal_adjustment,
                    remediation="dataset_id 不存在；请重新选择信号源或同步数据",
                ) from None
            _pair = (_ds.source, _ds.adjustment)
            if _pair not in _cands:
                raise DatasetBindingError(
                    "DATASET_BINDING_MISMATCH",
                    f"dataset {_dataset_id} source={_ds.source} adjustment="
                    f"{_ds.adjustment} is not in product family "
                    f"{_signal_data_source} (allowed={sorted(_cands)})",
                    http_status=400,
                    dataset_id=_dataset_id,
                    requested_source=_signal_data_source,
                    requested_adjustment=_signal_adjustment,
                    manifest_source=_ds.source,
                    manifest_adjustment=_ds.adjustment,
                    remediation="该 dataset 不属于所选信号源族；请清空 dataset_id 让系统重选，或改选匹配源",
                )
            # Re-validate role/ready/blobs with warehouse source (not product key)
            _ds = validate_signal_dataset_binding(
                repo,
                _dataset_id,
                source=_ds.source,
                adjustment=_ds.adjustment,
                period="1d",
                allow_raw_signal=_allow_raw_signal or (_ds.adjustment in ("none", "composite_none")),
            )
        else:
            _ds = validate_signal_dataset_binding(
                repo,
                _dataset_id,
                source=_signal_data_source,
                adjustment=_signal_adjustment,
                period="1d",
                allow_raw_signal=_allow_raw_signal,
            )
        _resolved_dataset_id = _dataset_id
        req.signal_adjustment = _ds.adjustment
        _signal_adjustment = _ds.adjustment
    else:
        _ds = None
        _resolved_dataset_id = None
        _last_err = None
        for _cand_src, _cand_adj in _resolve_signal_candidates():
            try:
                _ds = repo.resolve_latest_ready(
                    source=_cand_src,
                    adjustment=_cand_adj,
                    period="1d",
                )
                _resolved_dataset_id = _ds.dataset_id
                # Keep request source as product key; pin actual adjustment
                req.signal_adjustment = _ds.adjustment
                _signal_adjustment = _ds.adjustment
                break
            except DatasetNotFoundError as _e:
                _last_err = _e
                continue
        if _ds is None:
            raise DatasetBindingError(
                "DATASET_NOT_FOUND",
                f"No ready dataset for signal source={_signal_data_source} "
                f"adjustment={_signal_adjustment}. Run sync first.",
                http_status=404,
                requested_source=_signal_data_source,
                requested_adjustment=_signal_adjustment,
                remediation="先在数据仓库同步对应信号数据（通达信前复权 / Tushare前复权 / 未复权）",
            ) from _last_err
    req.dataset_id = _resolved_dataset_id
    # Gate C lineage: expose derived-dataset parents for cache isolation
    # and result traceability (empty for non-derived signal datasets).
    req.signal_raw_parent_dataset_id = getattr(_ds, "raw_dataset_id", "") or ""
    req.signal_factor_parent_dataset_id = getattr(_ds, "factor_dataset_id", "") or ""
    req.signal_formula_version = getattr(_ds, "formula_version", "") or ""
    req.signal_anchor_policy = getattr(_ds, "anchor_policy", "") or ""

    _execution_dataset_id = getattr(req, "execution_dataset_id", None)
    if _execution_dataset_id:
        _exec_ds = validate_execution_dataset_binding(
            repo,
            _execution_dataset_id,
            source=getattr(req, "execution_data_source", None),
            period="1d",
        )
    else:
        _exec_source = getattr(req, "execution_data_source", None) or "internal"

        _exec_ds = None
        _last_exec_err = None
        for _es, _ea in execution_resolve_candidates(_exec_source):
            try:
                _exec_ds = repo.resolve_latest_ready(
                    source=_es, adjustment=_ea, period="1d",
                )
                break
            except DatasetNotFoundError as _e:
                _last_exec_err = _e
        if _exec_ds is None:
            _tried = "、".join(
                "%s/%s" % (_es, _ea)
                for _es, _ea in execution_resolve_candidates(_exec_source)
            )
            raise DatasetBindingError(
                "DATASET_NOT_FOUND",
                f"No ready execution dataset for source={_exec_source} "
                f"(tried: {_tried}). "
                f"Run: python scripts/sync_market_data.py --source tushare --mode "
                f"incremental then reconcile the formal L2",
                http_status=404,
                requested_source=_exec_source,
                requested_adjustment="none",
                remediation="先同步 Tushare 数据并协调正式 L2（internal/composite_none），或显式指定 execution_dataset_id",
            ) from _last_exec_err
        _execution_dataset_id = _exec_ds.dataset_id
        # keep request source aligned with the dataset actually resolved
        try:
            req.execution_data_source = _exec_ds.source
        except Exception:
            pass
    req.execution_dataset_id = _execution_dataset_id
    req.execution_adjustment = _exec_ds.adjustment

    # Gate C D7: resolve the corporate-action factor dataset (no Baostock in
    # repo mode). Derived signal datasets pin their factor parent; others use
    # the latest ready tushare/adj_factor dataset. Missing -> None (per-code
    # series become dataset_missing => explicit fail-closed downstream).
    _factor_manifest = None
    _factor_ds_id = getattr(_ds, "factor_dataset_id", "") or ""
    if _factor_ds_id:
        try:
            _factor_manifest = repo.get_dataset(_factor_ds_id)
        except DatasetNotFoundError:
            raise DatasetBindingError(
                "DATASET_LINEAGE_BROKEN",
                f"signal dataset {_resolved_dataset_id} factor parent "
                f"{_factor_ds_id} is missing from the data root",
                http_status=422,
                dataset_id=_resolved_dataset_id,
                remediation="派生数据集的因子父集缺失；数据根不完整",
            ) from None
    else:
        try:
            _factor_manifest = repo.resolve_latest_ready(
                source="tushare", adjustment="adj_factor", period="1d"
            )
            _factor_ds_id = _factor_manifest.dataset_id
        except DatasetNotFoundError:
            _factor_manifest = None
            _factor_ds_id = ""
    req.ca_factor_dataset_id = _factor_ds_id or None

    # Gate B4: resolve + validate the point-in-time universe binding.
    _pit_universe = None
    _universe_id = getattr(req, "universe_dataset_id", None)
    if _universe_id:
        from ..data.pit_universe import PointInTimeUniverse

        try:
            _pit_universe = PointInTimeUniverse.from_root(
                cfg.market_data_root, _universe_id
            )
        except FileNotFoundError:
            raise DatasetBindingError(
                "UNIVERSE_NOT_FOUND",
                f"point-in-time universe not found: {_universe_id}",
                http_status=404,
                dataset_id=_universe_id,
                remediation="检查 universe_dataset_id 拼写，或先构建点时宇宙",
            ) from None
        except ValueError as _ue:
            raise DatasetBindingError(
                "UNIVERSE_CORRUPT",
                f"point-in-time universe unreadable: {_universe_id}: {_ue}",
                http_status=422,
                dataset_id=_universe_id,
                remediation="universe 文件内容哈希不一致（损坏）；禁止继续，请检查数据根",
            ) from None
        req.universe_rule_version = _pit_universe.universe_rule_version

    # Gate B7: survivorship-safe chain lineage on the request. The supplement
    # factor parent comes from the composite QFQ manifest; execution parents
    # from the composite execution manifest; baseline_generation classifies
    # the run for comparability (never silently mixed with legacy results).
    _sig_prov = getattr(_ds, "provenance", None) or {}
    req.signal_supplement_factor_dataset_id = (
        _sig_prov.get("supplement_factor_dataset_id") or None
    )
    # Gate B8 fix: the L3 corporate-action gate must also see the supplement
    # factor parent (delisted stocks' factors live there, not in the main
    # factor dataset). Missing supplement parent = broken lineage, fail closed.
    _supp_factor_manifest = None
    if req.signal_supplement_factor_dataset_id:
        try:
            _supp_factor_manifest = repo.get_dataset(
                req.signal_supplement_factor_dataset_id
            )
        except DatasetNotFoundError:
            raise DatasetBindingError(
                "DATASET_LINEAGE_BROKEN",
                f"signal dataset {_resolved_dataset_id} supplement factor "
                f"parent {req.signal_supplement_factor_dataset_id} is missing "
                f"from the data root",
                http_status=422,
                dataset_id=_resolved_dataset_id,
                remediation="补数因子父集缺失；数据根不完整，禁止继续",
            ) from None
    _exec_prov = getattr(_exec_ds, "provenance", None) or {}
    _exec_parents = _exec_prov.get("parents")
    if isinstance(_exec_parents, list):
        req.execution_parent_dataset_ids = [
            str(p.get("dataset_id"))
            for p in _exec_parents
            if isinstance(p, dict) and p.get("dataset_id")
        ] or None
    req.data_cutoff_date = (
        getattr(_exec_ds, "data_cutoff_date", None)
        or getattr(_ds, "data_cutoff_date", None)
    )
    _supp_parent_id = ""
    if isinstance(_exec_parents, list):
        for _p in _exec_parents:
            if isinstance(_p, dict) and _p.get("role") == "supplement":
                _supp_parent_id = str(_p.get("dataset_id") or "")
    _exec_sym_prov = _exec_prov.get("symbol_provenance") or {}
    if _supp_parent_id and isinstance(_exec_sym_prov, dict):
        req.execution_supplement_symbols = sorted(
            s for s, v in _exec_sym_prov.items() if v == _supp_parent_id
        ) or None
    req.baseline_generation = (
        "survivorship_safe"
        if (
            _ds.adjustment == "composite_tushare_factor_qfq"
            and _exec_ds.adjustment == "composite_none"
            and _pit_universe is not None
        )
        else "legacy"
    )

    coverage_excluded: List[dict] = []
    codes_kept: List[str] = list(codes)
    if check_symbol_coverage and codes:
        _sig_idx = manifest_symbol_index(_ds)
        _exec_idx = manifest_symbol_index(_exec_ds)
        for c in codes:
            sc = classify_symbol_coverage(_sig_idx, c)
            if sc != "ok":
                coverage_excluded.append({"symbol": c, "reason": f"signal_{sc}"})
                continue
            ec = classify_symbol_coverage(_exec_idx, c)
            if ec != "ok":
                coverage_excluded.append({"symbol": c, "reason": f"execution_{ec}"})
        if coverage_excluded:
            excl = {x["symbol"] for x in coverage_excluded}
            codes_kept = [c for c in codes if c not in excl]
            # Empty pool still hard-fails — nothing left to backtest.
            if not codes_kept:
                raise DatasetBindingError(
                    "SYMBOL_NOT_COVERED",
                    f"全部 {len(codes)} 只请求股票均未被锁定数据集覆盖 "
                    f"(signal={_resolved_dataset_id}, execution={_execution_dataset_id}); "
                    f"first: {coverage_excluded[0]['symbol']} "
                    f"({coverage_excluded[0]['reason']})",
                    dataset_id=_resolved_dataset_id,
                    requested_source=_signal_data_source,
                    requested_adjustment=req.signal_adjustment,
                    manifest_source=_ds.source,
                    manifest_adjustment=_ds.adjustment,
                    remediation=(
                        "股票池与信号/执行数据集无交集；请换信号源、补同步，"
                        "或缩小/更换股票池"
                    ),
                    extra={
                        "excluded_count": len(coverage_excluded),
                        "excluded": coverage_excluded[:50],
                        "requested_count": len(codes),
                        "kept_count": 0,
                    },
                )
    return {
        "repo": repo,
        "signal_manifest": _ds,
        "execution_manifest": _exec_ds,
        "signal_dataset_id": _resolved_dataset_id,
        "execution_dataset_id": _execution_dataset_id,
        "factor_manifest": _factor_manifest,
        "factor_dataset_id": _factor_ds_id or None,
        "supplement_factor_manifest": _supp_factor_manifest,
        "pit_universe": _pit_universe,
        "coverage_excluded": coverage_excluded,
        "codes_kept": codes_kept,
        "codes_requested_count": len(codes),
    }


def run_backtest(
    cfg: AStockConfig,
    req: BacktestRequest,
    *,
    rules: Optional[RuleService] = None,
    progress_cb: Optional[Any] = None,
) -> Dict[str, Any]:
    """Execute portfolio backtest; returns summary dict (also writes outputs)."""
    cfg.ensure_dirs()
    rules = rules or RuleService(cfg)
    reg = rules.load_full_registry()
    store = DataStore(cfg.storage_root)
    period = (req.period or "DAY").upper()
    if req.dwm:
        period = "DWM"
    if period in ("M60", "60", "60M", "H1", "MIN_60"):
        period = "MIN60"
    hold = int(req.hold or 1)
    entry_lag = int(req.entry_lag or 1)
    if entry_lag < 1:
        raise ValueError("entry_lag must be >= 1")
    signal_weekdays = parse_signal_weekdays(getattr(req, "signal_weekdays", None))
    buy_on = parse_price_session(getattr(req, "buy_on", None), default="open")
    sell_on = parse_price_session(getattr(req, "sell_on", None), default="open")
    buy_weekday = parse_single_weekday(getattr(req, "buy_weekday", None))
    exit_weekday = parse_single_weekday(getattr(req, "exit_weekday", None))
    codes = select_universe(cfg, req.codes)

    def _progress(payload: dict) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(payload)
        except InterruptedError:
            # Cooperative cancel from JobStore — re-raise to stop the run.
            raise
        except Exception:
            pass

    n_codes = len(codes)
    _progress({
        "phase": "prepare",
        "pct": 2.0,
        "current": 0,
        "total": n_codes,
        "message": "准备回测，股票池 %d 只" % n_codes,
        "code": None,
    })

    try:
        specs = [reg.get(i) for i in req.rule_ids]
    except KeyError as e:
        raise ValueError(f"unknown rule: {e}") from e
    research_unadj = bool(req.research_unadjusted)
    min60_research_note = False
    if period == "MIN60":
        min60_research_note = True

    start = int(req.start) if req.start else None
    end = int(req.end) if req.end else None

    trade_specs = [s for s in specs if s.id != "bagua_ohlc" and s.output_type == "signal"]
    if not trade_specs:
        raise ValueError("No tradeable signal indicators selected.")

    for s in trade_specs:
        if s.compile_status == "source_required":
            raise ValueError(f"blocked source_required: {s.id}")
        if s.compile_status == "invalid":
            raise ValueError(f"blocked invalid: {s.id}: {s.failure_reason}")
        if s.compile_status == "unsupported":
            raise ValueError(f"blocked unsupported: {s.id}: {s.failure_reason}")
        if "MIN1" in (s.dependencies or []):
            raise ValueError(f"blocked MIN1 (no minute history): {s.id}")
        if "MIN60" in (s.dependencies or []) and not (
            getattr(s, "uses_min60_day_proxy", False)
            or (s.parameters or {}).get("min60_day_proxy")
        ):
            raise ValueError(
                f"blocked MIN60 without day proxy: {s.id}: {s.failure_reason}"
            )

    events: List[SignalEvent] = []
    raw_map: Dict[str, Any] = {}  # execution + valuation
    adj_map: Dict[str, Any] = {}  # point_in_time_adjusted research refs (legacy name)
    standard_qfq_map: Dict[str, Any] = {}  # default signal bars
    period_raw_map: Dict[str, Any] = {}  # L2 period bars (audit / future use)

    _signal_data_source = getattr(req, "signal_data_source", None)
    _signal_adjustment = getattr(req, "signal_adjustment", None)
    _dataset_id = getattr(req, "dataset_id", None)
    _weekly_bar_mode = getattr(req, "weekly_bar_mode", None) or "local_aggregate"
    _use_repository_l1 = _signal_data_source in (
        "tdxquant", "tushare", "internal", "raw",
    )
    if _signal_data_source == "raw":
        req.research_unadjusted = True
        research_unadj = True
        if not _signal_adjustment:
            _signal_adjustment = "none"
            req.signal_adjustment = "none"

    if _use_repository_l1 and not _signal_adjustment:
        from ..data.providers.base import DataSource, SIGNAL_SOURCE_ADJUSTMENT
        _src_enum = {
            "tdxquant": DataSource.TDXQUANT,
            "tushare": DataSource.TUSHARE,
            "internal": DataSource.INTERNAL,
        }.get(_signal_data_source)
        if _signal_data_source == "tushare":
            _signal_adjustment = "qfq"
            req.signal_adjustment = "qfq"
        elif _signal_data_source == "internal":
            _signal_adjustment = "tushare_factor_qfq"
            req.signal_adjustment = "tushare_factor_qfq"
        elif _src_enum and _src_enum in SIGNAL_SOURCE_ADJUSTMENT:
            _signal_adjustment = SIGNAL_SOURCE_ADJUSTMENT[_src_enum].value
            req.signal_adjustment = _signal_adjustment

    _repo = None
    _resolved_dataset_id = None
    _execution_dataset_id = getattr(req, "execution_dataset_id", None)
    _factor_manifest = None
    _factor_symbol_index = None
    _supp_factor_manifest = None
    _supp_factor_symbol_index = None
    _pit_universe = None
    _coverage_excluded: List[dict] = []
    _codes_requested_count = len(codes)
    if _use_repository_l1:
        _binding = resolve_market_data_bindings(cfg, req, codes)
        _repo = _binding["repo"]
        _ds = _binding["signal_manifest"]
        _exec_ds = _binding["execution_manifest"]
        _resolved_dataset_id = _binding["signal_dataset_id"]
        _execution_dataset_id = _binding["execution_dataset_id"]
        _signal_adjustment = req.signal_adjustment
        _factor_manifest = _binding["factor_manifest"]
        _supp_factor_manifest = _binding.get("supplement_factor_manifest")
        _pit_universe = _binding.get("pit_universe")
        _coverage_excluded = list(_binding.get("coverage_excluded") or [])
        _codes_requested_count = int(
            _binding.get("codes_requested_count") or len(codes)
        )
        # Soft-drop uncovered symbols; never run them as silent zero-signal.
        if _binding.get("codes_kept") is not None:
            codes = list(_binding["codes_kept"])
            n_codes = len(codes)
        if _factor_manifest is not None:
            from ..data.dataset_binding import manifest_symbol_index
            _factor_symbol_index = manifest_symbol_index(_factor_manifest)
        if _supp_factor_manifest is not None:
            from ..data.dataset_binding import manifest_symbol_index
            _supp_factor_symbol_index = manifest_symbol_index(_supp_factor_manifest)
        if _coverage_excluded:
            _n_drop = len(_coverage_excluded)
            _progress({
                "phase": "prepare",
                "pct": 3.0,
                "current": 0,
                "total": n_codes,
                "message": (
                    "股票池覆盖：已剔除 %d/%d 只未进信号/执行集的股票，"
                    "剩余 %d 只继续回测"
                    % (_n_drop, _codes_requested_count, n_codes)
                ),
                "code": None,
                "coverage_excluded_count": _n_drop,
            })
            # Persist for repro / summary (soft-drop, not silent zero-signal).
            try:
                req.coverage_excluded = list(_coverage_excluded)
                req.coverage_excluded_count = _n_drop
                req.codes_requested_count = _codes_requested_count
                req.codes_kept_count = n_codes
            except Exception:
                pass

    period_signal_map: Dict[str, Any] = {}  # L1 period bars for indicators + bagua
    factor_series = []
    errors: List[dict] = []
    # L3 explicit corporate-action ledger is cache-only during backtests.
    # Network access remains isolated to scripts/sync_ca_events.py.
    # Hoisted before the per-symbol loop: the factor-series snap anchors to
    # these ledger dates (Tushare adj_factor micro-drift absorption).
    ca_events_by_code: Dict[str, List[Any]] = {}
    ca_meta: Dict[str, Any] = {}
    ca_dates_by_code: Dict[str, set] = {}
    ca_root = Path(cfg.market_data_root) / "ca_events"
    try:
        from ..data.tushare_ca_fetcher import (
            cached_events_metadata,
            load_cached_events_for_universe,
        )

        ca_events_by_code = load_cached_events_for_universe(
            codes,
            start_date=start,
            end_date=end,
            root=ca_root,
        )
        ca_meta = cached_events_metadata(
            ca_events_by_code,
            root=ca_root,
            requested_codes=codes,
        )
        ca_dates_by_code = {
            code: {
                int(getattr(e, "date", 0))
                for e in evs
                if int(getattr(e, "date", 0) or 0) > 0
            }
            for code, evs in ca_events_by_code.items()
        }
    except Exception as exc:
        # Missing/corrupt ledger never disables the residual factor gate.
        ca_events_by_code = {}
        ca_dates_by_code = {}
        ca_meta = {
            "cache_root": str(ca_root),
            "event_count": 0,
            "event_symbol_count": 0,
            "load_error": str(exc),
        }
        errors.append(
            {
                "code": "*",
                "indicator": "corporate_action",
                "error": f"CA_CACHE_LOAD_FAILED: {exc}; fallback=fail_closed",
                "level": "warning",
            }
        )
    if _coverage_excluded:
        _sample = ", ".join(
            x.get("symbol", "") for x in _coverage_excluded[:8]
        )
        if len(_coverage_excluded) > 8:
            _sample += ", …"
        errors.append({
            "code": "*",
            "indicator": "coverage",
            "error": (
                "SYMBOL_COVERAGE_SOFT_DROP: 已从股票池剔除 %d/%d 只"
                "（信号或执行数据集无覆盖）；剩余 %d 只继续。"
                "样例: %s"
                % (
                    len(_coverage_excluded),
                    _codes_requested_count,
                    n_codes,
                    _sample,
                )
            ),
            "excluded": _coverage_excluded[:50],
            "level": "warning",
        })
    combine = req.combine
    signal_cache_hit = False
    filter_cache_hit = False
    execution_cache_hit = False
    use_signal_cache = bool(getattr(req, "use_signal_cache", False))

    def _min60_bars_for_code(code, start_, end_):
        """60-min bars: warehouse dataset first, vendor CSV/.lc1 fallback.

        Preference order:
          1. minute_vendor/60m ready dataset in the warehouse (the imported
             blob+manifest form — this is the production surface).
          2. Vendor CSV archives directly (research/dev when the import has
             not been run yet).
          3. .lc1 binary minute files (last resort).
        Returns (bars, source); source == "csv_truncated" means only CSV
        covers the range and it stops at MIN_VENDOR_60MIN_MAX_DATE — the
        caller must reject (never silently truncate).
        """
        if _repo is not None:
            try:
                from ..data.repository import DatasetNotFoundError as _M60NF
                _m60_ds = _repo.resolve_latest_ready(
                    source="minute_vendor", adjustment="none", period="60m"
                )
                if _m60_ds is not None:
                    _mbars = _repo.load_bars(
                        dataset_id=_m60_ds.dataset_id,
                        symbol=code,
                        start_date=start_,
                        end_date=end_,
                    )
                    if _mbars:
                        from ..data.tdx_reader import DayBar as _MinDayBar
                        bars = [
                            _MinDayBar(
                                date=b.trade_date,
                                open=b.open,
                                high=b.high,
                                low=b.low,
                                close=b.close,
                                amount=b.amount,
                                volume=b.volume,
                                reserved=b.trade_date // 100,
                            )
                            for b in _mbars
                        ]
                        return bars, "warehouse"
            except Exception:
                pass  # fall through to CSV / .lc1
        bars, src = load_min60_daybars_any(
            cfg.tdx_root,
            cfg.minute_vendor_root,
            code,
            start=start_,
            end=end_,
        )
        if src == "vendor_csv" and end_ and int(end_) > MIN_VENDOR_60MIN_MAX_DATE:
            lc1 = load_min60_daybars(cfg.tdx_root, code, start=start_, end=end_)
            if lc1:
                return lc1, "tdx_lc1"
            return None, "csv_truncated"
        return bars, src

    def _apply_min60_qfq(m60, fs, end_):
        """Scale raw 60-min bars by the day-aligned factor series (qfq).

        Same math as the composite derivation: ratio(t) = factor_asof(t) /
        anchor_factor (last factor on or before the run end). Bars whose day
        has no factor keep the anchor ratio. With no usable factor series the
        bars stay unadjusted (identical to the pre-change behaviour).
        """
        if not m60 or fs is None:
            return m60
        dates = [int(d) for d in (getattr(fs, "dates", None) or [])]
        factors = getattr(fs, "factors", None) or []
        if not dates or not factors or len(dates) != len(factors):
            return m60
        import bisect as _bisect
        idx = _bisect.bisect_right(dates, int(end_ or 0)) - 1
        if idx < 0:
            return m60
        anchor = float(factors[idx])
        if anchor <= 0:
            return m60
        from ..data.tdx_reader import DayBar as _MinDayBar
        fmap = {d: float(f) for d, f in zip(dates, factors) if float(f) > 0}
        out = []
        for b in m60:
            fac = fmap.get(int(b.reserved)) if b.reserved else None
            ratio = (fac if fac else anchor) / anchor
            out.append(
                _MinDayBar(
                    date=b.date,
                    open=b.open * ratio,
                    high=b.high * ratio,
                    low=b.low * ratio,
                    close=b.close * ratio,
                    amount=b.amount,
                    volume=b.volume,
                    reserved=b.reserved,
                )
            )
        return out

    def _load_maps_and_maybe_signals(*, compute_signals: bool) -> List[SignalEvent]:
        """Always fill raw/adj/period maps + factor_series; optionally emit events."""
        local_events: List[SignalEvent] = []
        for idx, code in enumerate(codes):
            # signal phase occupies 5% ~ 85%
            if n_codes > 0:
                pct = 5.0 + 80.0 * (idx / float(n_codes))
            else:
                pct = 5.0
            if idx == 0 or (idx + 1) % 5 == 0 or (idx + 1) == n_codes:
                _progress({
                    "phase": "signals" if compute_signals else "bars",
                    "pct": round(pct, 2),
                    "current": idx + 1,
                    "total": n_codes,
                    "message": (
                        "计算信号 %d/%d" % (idx + 1, n_codes)
                        if compute_signals
                        else "加载行情 %d/%d" % (idx + 1, n_codes)
                    ),
                    "code": code,
                })
            if _use_repository_l1 and _repo is not None and _execution_dataset_id:
                from ..data.tdx_reader import DayBar as _ExecDayBar
                from ..data.repository import DatasetNotFoundError as _ExecDSNotFound
                exec_bars = _repo.load_bars(
                    dataset_id=_execution_dataset_id,
                    symbol=code,
                    start_date=start,
                    end_date=end,
                )
                day_raw = [
                    _ExecDayBar(
                        date=b.trade_date,
                        open=b.open,
                        high=b.high,
                        low=b.low,
                        close=b.close,
                        amount=b.amount,
                        volume=b.volume,
                    )
                    for b in exec_bars
                ]
                if len(day_raw) > 1 and day_raw[0].date > day_raw[-1].date:
                    day_raw.reverse()
            else:
                try:
                    day_raw = store.load_symbol(code)
                except FileNotFoundError:
                    reader = TdxDayReader(cfg.tdx_root)
                    raw = ("sh" if code.startswith("SSE") else "sz") + code.split(".")[-1]
                    day_raw, _ = reader.read(raw)
            raw_map[code] = day_raw
            dates = [b.date for b in day_raw]

            if _use_repository_l1 and _repo is not None and _resolved_dataset_id:
                from ..data.tdx_reader import DayBar as _DayBar
                repo_bars = _repo.load_bars(
                    dataset_id=_resolved_dataset_id,
                    symbol=code,
                    start_date=start,
                    end_date=end,
                )
                day_for_ind = [
                    _DayBar(
                        date=b.trade_date,
                        open=b.open,
                        high=b.high,
                        low=b.low,
                        close=b.close,
                        amount=b.amount,
                        volume=b.volume,
                    )
                    for b in repo_bars
                ]
                if len(day_for_ind) > 1 and day_for_ind[0].date > day_for_ind[-1].date:
                    day_for_ind.reverse()
                standard_qfq_map[code] = day_for_ind
                # Gate C D3: symbol has a blob but no rows inside [start,end]
                # -> explicit no_data_in_range, never a silent zero-signal.
                if not day_for_ind:
                    errors.append({
                        "code": code,
                        "indicator": "*",
                        "error": "no_data_in_range(signal dataset %s)" % _resolved_dataset_id,
                    })
                if not day_raw:
                    errors.append({
                        "code": code,
                        "indicator": "*",
                        "error": "no_data_in_range(execution dataset %s)" % _execution_dataset_id,
                    })
                # Gate C D7: corporate-action factors come from the LOCKED
                # adj_factor dataset — no Baostock in repository mode.
                # Gate B8: mirror the composite derivation's
                # factor_resolution_v1 (exact_main > exact_supplement >
                # alias_main > alias_supplement). Delisted stocks' factors
                # live in the supplement dataset; BSE pre-migration codes
                # resolve via the PIT universe alias to the 920 canonical.
                # Every tier is a locked dataset — still fully offline and
                # fail-closed when none covers the symbol.
                from ..data.adjustments import build_factor_series_from_dataset
                if _factor_manifest is not None or _supp_factor_manifest is not None:
                    _attempts = []
                    if _factor_manifest is not None:
                        _attempts.append(
                            (_factor_manifest, _factor_symbol_index, None))
                    if _supp_factor_manifest is not None:
                        _attempts.append(
                            (_supp_factor_manifest, _supp_factor_symbol_index, None))
                    if _pit_universe is not None:
                        _w = _pit_universe.resolve(code)
                        _canon = getattr(_w, "canonical_symbol", None) if _w else None
                        if _canon and _canon != code:
                            if _factor_manifest is not None:
                                _attempts.append(
                                    (_factor_manifest, _factor_symbol_index, _canon))
                            if _supp_factor_manifest is not None:
                                _attempts.append(
                                    (_supp_factor_manifest,
                                     _supp_factor_symbol_index, _canon))
                    series = None
                    for _fm, _fidx, _lookup in _attempts:
                        series = build_factor_series_from_dataset(
                            getattr(_repo, "_store"),
                            _fm,
                            code,
                            dates,
                            symbol_index=_fidx,
                            lookup_symbol=_lookup,
                            known_event_dates=ca_dates_by_code.get(code),
                        )
                        if series.quality == "complete":
                            break
                else:
                    from ..data.adjustments import FactorSeries as _FS
                    from ..data.adjustments import identity_factors as _idf
                    series = _FS(
                        std_code=code,
                        dates=[int(d) for d in dates],
                        factors=_idf(len(dates)).tolist(),
                        source="dataset_missing",
                        source_detail="no ready tushare/adj_factor dataset in data root",
                        quality="incomplete",
                        sha256="",
                    )
                factor_series.append(series)
            else:
                from ..data.dataset_store import DatasetStore as _LegacyDS
                series = build_factor_series(
                    code, dates, adj_root=cfg.adj_root, prefer_baostock=True,
                    store=_LegacyDS(cfg.market_data_root),
                )
                factor_series.append(series)
                import numpy as np

                fac = np.array(series.factors, dtype=float)
                day_pit = day_bars_to_point_in_time_adjusted(day_raw, fac)
                adj_map[code] = day_pit
                _asof_sig = end if end else (day_raw[-1].date if day_raw else None)

                from ..data.affine_adjust import build_affine_series
                affine = build_affine_series(code, dates, adj_root=cfg.adj_root)
                if affine.quality == "complete" and not affine.is_identity:
                    day_for_ind = day_bars_for_signals_affine(
                        day_raw,
                        affine,
                        research_unadjusted=research_unadj,
                        signal_adjust="asof_forward_qfq",
                        asof_date=_asof_sig,
                    )
                    standard_qfq_map[code] = day_bars_for_signals_affine(
                        day_raw,
                        affine,
                        research_unadjusted=False,
                        signal_adjust="standard_qfq",
                    )
                else:
                    day_for_ind = day_bars_for_signals(
                        day_raw,
                        fac,
                        research_unadjusted=research_unadj,
                        signal_adjust="asof_forward_qfq",
                        asof_date=_asof_sig,
                        dates=dates,
                    )
                    standard_qfq_map[code] = day_bars_to_standard_qfq(day_raw, fac)
            asof = day_raw[-1].date if day_raw else None

            if not compute_signals:
                # Fill period maps for bagua (L1 signal bars) when signal cache hits.
                if period == "DWM":
                    period_raw_map[code] = day_raw
                    period_signal_map[code] = day_for_ind
                elif period == "MIN60":
                    m60, _msrc = _min60_bars_for_code(code, start, end)
                    period_raw_map[code] = m60 or []
                    period_signal_map[code] = m60 or []
                    if _msrc == "csv_truncated":
                        errors.append({
                            "code": code,
                            "indicator": "*",
                            "error": (
                                f"分钟CSV截止{MIN_VENDOR_60MIN_MAX_DATE}且无.lc1覆盖，"
                                f"请求end={end}超出（不静默截断）"
                            ),
                        })
                else:
                    period_raw_map[code] = build_period_bars(day_raw, period, asof=asof, weekly_bar_mode=_weekly_bar_mode)
                    period_signal_map[code] = build_period_bars(
                        day_for_ind, period, asof=asof, weekly_bar_mode=_weekly_bar_mode
                    )
                continue

            local_events.extend(
                _events_for_code(code, day_raw, day_for_ind, asof,
                                 fs=factor_series[idx] if idx < len(factor_series) else None)
            )
        return local_events

    def _events_for_code(code, day_raw, day_for_ind, asof, fs=None) -> List[SignalEvent]:
        """Indicator signals for one code from already-built bar lanes."""
        local_events: List[SignalEvent] = []
        if period == "DWM":
            base = trade_specs[0]
            w_bars = build_period_bars(day_for_ind, "WEEK", asof=asof, weekly_bar_mode=_weekly_bar_mode)
            m_bars = build_period_bars(day_for_ind, "MONTH", asof=asof)
            d_dict = bars_dict_from_day(day_for_ind)
            w_dict = bars_dict_from_period(w_bars)
            m_dict = bars_dict_from_period(m_bars)
            ds, e1 = compute_indicator_signal(base, d_dict)
            ws, e2 = compute_indicator_signal(base, w_dict)
            ms, e3 = compute_indicator_signal(base, m_dict)
            if ds is None or ws is None or ms is None:
                errors.append({"code": code, "dwm_errors": [e1, e2, e3]})
                return local_events
            res = compute_v5_dwm_resonance(day_for_ind, ds, w_bars, ws, m_bars, ms)
            period_raw_map[code] = day_raw
            period_signal_map[code] = day_for_ind
            for d in signal_dates(d_dict["date"], res):
                if start and d < start:
                    continue
                if end and d > end:
                    continue
                local_events.append(SignalEvent(code, d, "DWM", f"{base.id}_dwm", is_dwm=True))
            return local_events

        if period == "MIN60":
            m60 = period_raw_map.get(code)
            if m60 is None:
                m60, _msrc = _min60_bars_for_code(code, start, end)
                if _msrc == "csv_truncated":
                    errors.append({
                        "code": code,
                        "indicator": "*",
                        "error": (
                            f"分钟CSV截止{MIN_VENDOR_60MIN_MAX_DATE}且无.lc1覆盖，"
                            f"请求end={end}超出（不静默截断）"
                        ),
                    })
                    return local_events
                period_raw_map[code] = m60 or []
                period_signal_map[code] = m60 or []
            elif code not in period_signal_map:
                period_signal_map[code] = m60 or []
            if not m60:
                errors.append({"code": code, "indicator": "*", "error": "无60分钟线数据(.lc1)"})
                return local_events
            m60 = _apply_min60_qfq(m60, fs, end)
            bars = min60_bars_to_arrays(m60)
            trade_dates = bars.get("trade_date")
            # 按日去重策略（显式约定）：同一交易日内多个 60 分钟信号（同一
            # 股票）在下方 SignalEvent 构造时都以真实交易日 d_out 聚合为同一天，
            # strategy_engine 的 sig_map 按 (date, code) 去重 → 同日只买一次
            # （"当天首次命中触发，当日执行"）。这不是静默丢信号——4 个 60min
            # bar 各自产生的信号全部进入 n_events，仅在成交侧按日合并。
        else:
            p_bars_ind = build_period_bars(day_for_ind, period, asof=asof, weekly_bar_mode=_weekly_bar_mode)
            p_bars_raw = build_period_bars(day_raw, period, asof=asof, weekly_bar_mode=_weekly_bar_mode)
            period_raw_map[code] = p_bars_raw
            period_signal_map[code] = p_bars_ind
            bars = bars_dict_from_day(p_bars_ind) if period == "DAY" else bars_dict_from_period(p_bars_ind)
            trade_dates = None
        sigs = []
        for spec in trade_specs:
            sig, err = compute_indicator_signal(
                spec, bars,
                minute_mode=(period == "MIN60"),
            )
            if err:
                errors.append({"code": code, "indicator": spec.id, "error": err})
                continue
            sigs.append(sig)
            if not combine:
                date_arr = bars["date"]
                for i, d in enumerate(date_arr):
                    try:
                        on = int(sig[i]) != 0 and not (
                            isinstance(sig[i], float) and __import__("math").isnan(sig[i])
                        )
                    except Exception:
                        on = bool(sig[i])
                    if not on:
                        continue
                    if trade_dates is not None and i < len(trade_dates):
                        d_out = int(trade_dates[i])
                    else:
                        d_out = int(d)
                    if start and d_out < start:
                        continue
                    if end and d_out > end:
                        continue
                    local_events.append(SignalEvent(code, d_out, period, spec.id))
        if combine and sigs:
            combined = sigs[0] if len(sigs) == 1 else combine_signals(sigs, mode=combine)
            date_arr = bars["date"]
            for i, d in enumerate(date_arr):
                try:
                    on = int(combined[i]) != 0
                except Exception:
                    on = bool(combined[i])
                if not on:
                    continue
                if trade_dates is not None and i < len(trade_dates):
                    d_out = int(trade_dates[i])
                else:
                    d_out = int(d)
                if start and d_out < start:
                    continue
                if end and d_out > end:
                    continue
                local_events.append(SignalEvent(code, d_out, period, f"combine_{combine}"))
        return local_events

    def _compute_events_from_loaded_maps() -> List[SignalEvent]:
        """Compute signals using maps already filled by a prior bars/factors load."""
        local_events: List[SignalEvent] = []
        for idx, code in enumerate(codes):
            if n_codes > 0:
                pct = 5.0 + 80.0 * (idx / float(n_codes))
            else:
                pct = 5.0
            if idx == 0 or (idx + 1) % 5 == 0 or (idx + 1) == n_codes:
                _progress({
                    "phase": "signals",
                    "pct": round(pct, 2),
                    "current": idx + 1,
                    "total": n_codes,
                    "message": "计算信号 %d/%d" % (idx + 1, n_codes),
                    "code": code,
                })
            day_raw = raw_map.get(code) or []
            if research_unadj:
                day_for_ind = day_raw
            else:
                day_for_ind = standard_qfq_map.get(code) or day_raw
            asof = day_raw[-1].date if day_raw else None
            local_events.extend(_events_for_code(code, day_raw, day_for_ind, asof,
                                                 fs=factor_series[idx] if idx < len(factor_series) else None))
        return local_events

    def _make_signal_cache_key(factor_manifest: str) -> str:
        from ..research.signal_cache import signal_cache_key

        _ind_src = "|".join(
            sorted(
                "%s:%s" % (s.id, getattr(s, "source_sha256", None) or "")
                for s in trade_specs
            )
        )
        return signal_cache_key(
            indicator_ids=[s.id for s in trade_specs],
            indicator_source_hash=_ind_src,
            period=period,
            start=start,
            end=end,
            universe_hash=selected_universe_sha(codes),
            adjust_mode=("research_unadjusted" if research_unadj else "asof_forward_qfq"),
            factor_manifest_sha=factor_manifest or "",
            combine=combine,
            data_source=getattr(req, "signal_data_source", None) or "",
            adjustment=getattr(req, "signal_adjustment", None) or "",
            dataset_id=getattr(req, "dataset_id", None) or "",
            weekly_bar_mode=getattr(req, "weekly_bar_mode", None) or "local_aggregate",
            anchor_date=getattr(req, "end", None),
            execution_data_source=getattr(req, "execution_data_source", None) or "internal",
            execution_dataset_id=getattr(req, "execution_dataset_id", None) or "",
            raw_parent_dataset_id=getattr(req, "signal_raw_parent_dataset_id", None) or "",
            factor_parent_dataset_id=getattr(req, "signal_factor_parent_dataset_id", None) or "",
            formula_version=getattr(req, "signal_formula_version", None) or "",
            anchor_policy=getattr(req, "signal_anchor_policy", None) or "",
            universe_version=(
                f"{req.universe_dataset_id}:{req.universe_rule_version or ''}"
                if getattr(req, "universe_dataset_id", None)
                else None
            ),
        )

    if use_signal_cache:
        try:
            from ..research.signal_cache import get_or_compute_signals

            # Load bars/factors first so the cache key pins factor_manifest_sha.
            # Without this, standard_qfq signals can change after CA updates while
            # the disk cache still returns stale events.
            _load_maps_and_maybe_signals(compute_signals=False)
            _factor_manifest = factor_manifest_sha(factor_series)
            _sig_key = _make_signal_cache_key(_factor_manifest)

            events, signal_cache_hit = get_or_compute_signals(
                _sig_key,
                _compute_events_from_loaded_maps,
                cfg=cfg,
                use_cache=True,
                meta={
                    "period": period,
                    "n_codes": n_codes,
                    "rule_ids": [s.id for s in trade_specs],
                    "factor_manifest_sha": _factor_manifest,
                },
            )
            if signal_cache_hit:
                _progress({
                    "phase": "signals",
                    "pct": 85.0,
                    "current": n_codes,
                    "total": n_codes,
                    "message": "信号缓存命中 %d 条" % len(events),
                    "code": None,
                    "signal_cache_hit": True,
                })
        except Exception as _cache_err:
            # Fail open: recompute without cache
            signal_cache_hit = False
            if not raw_map:
                events = _load_maps_and_maybe_signals(compute_signals=True)
            else:
                events = _compute_events_from_loaded_maps()
            errors.append({"code": "*", "indicator": "signal_cache", "error": str(_cache_err)[:200]})
    else:
        events = _load_maps_and_maybe_signals(compute_signals=True)

    # Gate B4: point-in-time universe filter — a signal only survives if the
    # stock was listed and still tradable on the signal date. Applied on both
    # the cache-hit and recompute paths (the cache key carries the universe
    # version, so caches are isolated per universe anyway).
    pit_meta: Dict[str, Any] = {}
    if _pit_universe is not None:
        _n_before_pit = len(events)
        _pit_excluded: Dict[str, int] = {}
        _kept = []
        for _ev in events:
            _ok, _reason = _pit_universe.membership_reason(_ev.std_code, _ev.date)
            if _ok:
                _kept.append(_ev)
            else:
                _pit_excluded[_reason] = _pit_excluded.get(_reason, 0) + 1
        events = _kept
        pit_meta = {
            "universe_dataset_id": _pit_universe.universe_dataset_id,
            "universe_rule_version": _pit_universe.universe_rule_version,
            "instrument_identity_rule_version": _pit_universe.identity_rule_version,
            "universe_content_sha256": _pit_universe.content_sha256,
            "n_events_before_pit": _n_before_pit,
            "n_events_after_pit": len(events),
            "excluded_by_reason": _pit_excluded,
        }

    # Gate B5: resolve the delisted-position exit policy. Active only with a
    # point-in-time universe (it provides the bar-derived terminal dates).
    _delist_policy = None
    _delist_terminal_dates: Dict[str, int] = {}
    delist_meta: Dict[str, Any] = {}
    _req_delist_scenario = getattr(req, "delist_exit_scenario", None)
    if _pit_universe is not None:
        from ..delist_policy import normalize_delist_policy

        _delist_policy, _dp_notes = normalize_delist_policy(
            _req_delist_scenario,
            getattr(req, "delist_recovery_discount", None),
        )
        req.delist_exit_scenario = _delist_policy.scenario
        req.delist_recovery_discount = _delist_policy.recovery_discount
        req.delist_exit_rule_version = _delist_policy.rule_version
        for _canon, _w in _pit_universe.entries.items():
            if _w.last_trade_date:
                _delist_terminal_dates[_canon] = int(_w.last_trade_date)
                for _alias in _w.aliases:
                    _delist_terminal_dates[_alias] = int(_w.last_trade_date)
        delist_meta = {
            **_delist_policy.to_meta(),
            "delist_terminal_dates_count": len(_delist_terminal_dates),
        }
        for _note in _dp_notes:
            errors.append({"code": "*", "indicator": "delist_policy", "error": _note})
    elif _req_delist_scenario:
        raise ValueError(
            "delist_exit_scenario requires universe_dataset_id: terminal "
            "dates come from the point-in-time universe (no silent fallback)"
        )

    # Weekday filter BEFORE bagua: "周五信号 + 最佳3爻" = Friday tech signals that pass gua.
    n_events_raw_signals = len(events)
    n_events_after_weekday = n_events_raw_signals
    if signal_weekdays:
        events = filter_events_by_signal_weekdays(events, signal_weekdays)
        n_events_after_weekday = len(events)

    _progress({
        "phase": "factors",
        "pct": 86.0,
        "current": n_codes,
        "total": n_codes,
        "message": "校验复权因子",
        "code": None,
    })

    formal_ok, adj_msg = formal_adjustment_ready(factor_series)
    _progress({
        "phase": "factors",
        "pct": 86.2,
        "current": n_codes,
        "total": n_codes,
        "message": "复权因子校验完成" + ("（正式通过）" if formal_ok else "（未完整）"),
        "code": None,
    })
    if not formal_ok and not research_unadj:
        # Persist a failed run so the UI history is not empty and the message
        # is inspectable later. Still No-Go (no portfolio metrics).
        run_id = req.run_id or f"bt_nogo_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        out_dir = Path(cfg.output_root) / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "run_id": run_id,
            "status": "no_go",
            "reason": adj_msg,
            "error": adj_msg,
            "indicator_ids": [s.id for s in trade_specs],
            "hold": hold,
            "entry_lag": entry_lag,
            "signal_weekdays": signal_weekdays,
            "buy_on": buy_on,
            "sell_on": sell_on,
            "buy_weekday": buy_weekday,
            "exit_weekday": exit_weekday,
            "schedule_mode": (
                "weekday"
                if (buy_weekday is not None or exit_weekday is not None)
                else "tn"
            ),
            "period": period,
            "start": start,
            "end": end,
            "selected_codes_count": len(codes),
            "request": req.to_dict(),
            "n_signals": len(events),
            **(research_fingerprint_fields_from_request(req) or {}),
            "hint": (
                "复权因子不完整，正式回测已拒绝。"
                "可勾选「研究未复权」重跑，或先修复 adjustments 缓存后重试正式模式。"
            ),
            "metrics": {},
        }
        from ..backtest_summary import build_backtest_summary

        meta["analysis_summary"] = build_backtest_summary(
            {}, status="no_go", context=meta, reason=adj_msg
        )
        (out_dir / "run_meta.json").write_text(
            __import__("json").dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Gate C D5: persistence failures must not be swallowed.
        from .runs import append_run_index

        append_run_index(
            cfg,
            {
                    "run_id": run_id,
                    "status": "no_go",
                    "created_at": int(time.time()),
                    "indicator_ids": [s.id for s in trade_specs],
                    "hold": hold,
                    "entry_lag": entry_lag,
                    "buy_weekday": buy_weekday,
                    "exit_weekday": exit_weekday,
                    "buy_on": buy_on,
                    "sell_on": sell_on,
                    "signal_weekdays": signal_weekdays,
                    "schedule_mode": meta["schedule_mode"],
                    "period": period,
                    "metrics": None,
                    "error": adj_msg[:500],
                    "signal_data_source": getattr(req, "signal_data_source", None),
                    "signal_adjustment": getattr(req, "signal_adjustment", None),
                    "dataset_id": getattr(req, "dataset_id", None),
                    "weekly_bar_mode": getattr(req, "weekly_bar_mode", None) or "local_aggregate",
                    "execution_data_source": getattr(req, "execution_data_source", None) or "internal",
                    "execution_dataset_id": getattr(req, "execution_dataset_id", None),
                    "execution_adjustment": getattr(req, "execution_adjustment", None),
                    "raw_dataset_id": getattr(req, "signal_raw_parent_dataset_id", None),
                    "factor_dataset_id": getattr(req, "signal_factor_parent_dataset_id", None),
                    "signal_formula_version": getattr(req, "signal_formula_version", None),
            },
        )
        _progress({
            "phase": "failed",
            "pct": 86.0,
            "current": n_codes,
            "total": n_codes,
            "message": (adj_msg or "")[:200],
            "code": None,
            "run_id": run_id,
        })
        return {
            "status": "no_go",
            "reason": adj_msg,
            "run_id": run_id,
            "error": adj_msg,
            "hint": meta["hint"],
            "analysis_summary": meta["analysis_summary"],
        }

    mapping = load_source_map(cfg.mapping_path)
    formula_audits: Dict[str, Any] = {}
    allow_unconfirmed = bool(req.research_unconfirmed_formula)
    for s in trade_specs:
        entry = None
        if s.package_sha256 and s.package_sha256 in mapping:
            entry = dict(mapping[s.package_sha256])
            if s.package_file:
                entry["package_file"] = str(Path(s.package_file).resolve())
        elif s.source_sha256:
            entry = {
                "package_sha256": None,
                "source_sha256": s.source_sha256,
                "source_file": s.source_file,
                "formula_provenance": "txt_self_source",
                "source_pair_status": "txt_only",
                "formal_backtest_allowed": True,
                "research_backtest_allowed": True,
                "confirmation": {
                    "package_sha256": None,
                    "source_sha256": s.source_sha256,
                    "confirmed_by": "txt_self",
                    "confirmation_method": "txt_only",
                    "note": "standalone txt formula",
                    "schema_version": 1,
                },
            }
        audit = resolve_formula_audit(entry, package_sha256=s.package_sha256)
        if entry and entry.get("source_pair_status") == "txt_only" and s.compile_status == "ready":
            audit = {
                "formula_provenance": "txt_self_source",
                "source_pair_status": "txt_only",
                "formal_backtest_allowed": True,
                "research_backtest_allowed": True,
            }
        formula_audits[s.id] = audit
        if s.package_sha256 and not audit.get("formal_backtest_allowed", False):
            if not allow_unconfirmed:
                return {
                    "status": "rejected_unconfirmed_formula",
                    "indicator": s.id,
                    "audit": audit,
                    "error": "unconfirmed formula; use research_unconfirmed_formula or confirm source",
                    "run_id": None,
                }

    gf = GuaFilter.from_dict(getattr(req, "gua_filter", None))
    try:
        from .gua import rule_version as _gua_rule_ver, hexagram_name_map, state_label_map

        if not gf.rule_version:
            gf.rule_version = _gua_rule_ver(cfg)
    except Exception:
        if not gf.rule_version:
            gf.rule_version = DEFAULT_RULE_VERSION

    bagua_enabled = (
        any(s.id == "bagua_ohlc" for s in specs)
        or bool(req.with_bagua)
        or gf.is_active()
    )
    bagua_filter_mode = None
    bagua_n_before = len(events)
    bagua_n_after = len(events)
    gua_filter_meta = None
    filter_cache_hit = False
    _filter_key = None
    # Phase-3: filter-layer cache (gua / bagua mode). Signal weekdays still applied in engine.
    # Product: bagua always uses WEEK hexagram covering the signal date.
    # Price plane selectable: raw | tdx_front | tushare_qfq.
    _bagua_period = "WEEK"
    _bagua_plane_raw = (getattr(req, "bagua_price_plane", None) or "raw").strip().lower()
    if _bagua_plane_raw in ("tdx_front", "tdxquant_front", "通达信前复权"):
        _bagua_plane = "tdx_front"
    elif _bagua_plane_raw in ("tushare_qfq", "ts_qfq", "tushare前复权"):
        _bagua_plane = "tushare_qfq"
    else:
        _bagua_plane = "raw"
    req.bagua_period = _bagua_period
    req.bagua_price_plane = _bagua_plane

    # Track bagua plane fidelity: never claim adjusted plane when raw was used.
    bagua_plane_fallback_codes: List[str] = []
    bagua_plane_effective = _bagua_plane
    bagua_plane_fallback_count = 0

    if bagua_enabled:
        def _bagua_plane_matches_signal(plane: str) -> bool:
            """True when bagua plane is the same product family as L1 signal bars.

            Only then may we reuse ``standard_qfq_map`` / signal day bars and
            skip a second full-A warehouse scan (freeze root cause).
            """
            src = (getattr(req, "signal_data_source", None) or "").strip()
            adj = (getattr(req, "signal_adjustment", None) or "").strip()
            if plane == "tdx_front":
                return src == "tdxquant" and adj in ("", "front")
            if plane == "tushare_qfq":
                if src == "tushare" and adj in ("", "qfq"):
                    return True
                if src == "internal" and adj in (
                    "tushare_factor_qfq",
                    "composite_tushare_factor_qfq",
                    "qfq",
                    "asof_qfq",
                ):
                    return True
                # Product tushare key may already be bound to derived QFQ dataset.
                if src == "tushare" and adj in (
                    "tushare_factor_qfq",
                    "composite_tushare_factor_qfq",
                ):
                    return True
            return False

        def _bagua_codes_from_events(evs) -> List[str]:
            """Unique symbols that still have signals — bagua only needs these.

            Product flow: rule scans full universe → events; bagua filters those
            events. Never re-walk all ``codes`` just to prepare OHLC for stocks
            with zero signals (was the full-A bagua-plane freeze).
            """
            seen = {}
            out_codes: List[str] = []
            for ev in evs or []:
                c = getattr(ev, "std_code", None) or getattr(ev, "code", None)
                if not c or c in seen:
                    continue
                seen[c] = 1
                out_codes.append(str(c))
            return out_codes

        def _bagua_day_bars_from_signal_maps(code: str):
            """Prefer in-memory L1 day bars already loaded for signals."""
            bars = standard_qfq_map.get(code)
            if bars:
                return bars
            # DAY period map may already hold day signal bars.
            pmap = period_signal_map.get(code)
            if pmap and len(pmap) > 0:
                b0 = pmap[0]
                # Period bars have start_date/end_date; day bars only date.
                if not hasattr(b0, "start_date"):
                    return pmap
            return None

        def _bagua_bars_map_for_plane(
            plane: str, needed_codes: Optional[Sequence[str]] = None
        ) -> Dict[str, Any]:
            """Day bars for weekly bagua attach; attach_bagua re-aggs to WEEK.

            Only ``needed_codes`` (symbols present on signal events) are prepared.
            Full universe ``codes`` is intentionally NOT used here.

            Adjusted planes (tdx_front / tushare_qfq) fail closed per code:
            missing/unloadable bars are omitted (no silent raw substitution).
            Raw plane subsets execution raw map for signal symbols only.

            Same-family signal+plane: reuse in-memory signal day bars.
            Cross-family: one BaguaPlaneSession indexes manifests once.
            """
            want = [str(c) for c in (needed_codes or []) if c]
            if not want:
                return {}

            if plane == "raw":
                # Subset only — prepare_bars_for_bagua would otherwise weekly-agg
                # every stock in period_raw_map (full A) for no benefit.
                out_raw: Dict[str, Any] = {}
                for c in want:
                    if c in period_raw_map and period_raw_map[c]:
                        out_raw[c] = period_raw_map[c]
                    else:
                        bagua_plane_fallback_codes.append(c)
                return out_raw

            out: Dict[str, Any] = {}
            _n = max(1, len(want))
            reuse = _bagua_plane_matches_signal(plane) and bool(standard_qfq_map)
            _progress({
                "phase": "bagua_plane",
                "pct": 86.5,
                "current": 0,
                "total": _n,
                "message": (
                    "信号票卦象面 %s（%d 只有信号，复用K线）" % (plane, _n)
                    if reuse
                    else "信号票卦象面 %s（%d 只有信号）" % (plane, _n)
                ),
                "code": None,
                "bagua_plane_reuse": bool(reuse),
                "bagua_signal_symbols": _n,
            })

            session = None
            if not reuse:
                from .bagua_query import (
                    BaguaPlaneSession,
                    SourceDisabledError,
                    load_day_bars_for_plane as _load_plane,
                )
                try:
                    session = BaguaPlaneSession(cfg, plane)
                except (FileNotFoundError, SourceDisabledError) as _se:
                    # Unbuildable sessions and disabled planes (legacy
                    # tdx_front experiments rerun under the Tushare-only
                    # policy) degrade per code like per-symbol load failures
                    # instead of aborting the whole rerun.
                    for _c in want:
                        bagua_plane_fallback_codes.append(_c)
                        errors.append({
                            "code": _c,
                            "indicator": "*",
                            "error": "bagua_plane_load_failed(%s): %s" % (plane, _se),
                        })
                    return out
            else:
                from .bagua_query import load_day_bars_for_plane as _load_plane  # noqa: F401

            _reused = 0
            _loaded = 0
            for _bi, code in enumerate(want):
                try:
                    bars = None
                    if reuse:
                        bars = _bagua_day_bars_from_signal_maps(code)
                        if bars:
                            _reused += 1
                    if not bars:
                        if session is not None:
                            bars, _meta = session.load_symbol(code, asof=end)
                            if start is not None or end is not None:
                                lo = int(start) if start is not None else 0
                                hi = int(end) if end is not None else 10**9
                                bars = [b for b in bars if lo <= int(b.date) <= hi]
                        else:
                            bars, _meta = _load_plane(
                                cfg, code, plane, start=start, end=end
                            )
                        if bars:
                            _loaded += 1
                    if bars:
                        out[code] = bars
                    else:
                        bagua_plane_fallback_codes.append(code)
                except (OSError, ValueError, KeyError, TypeError, FileNotFoundError) as _be:
                    bagua_plane_fallback_codes.append(code)
                    errors.append({
                        "code": code,
                        "indicator": "*",
                        "error": "bagua_plane_load_failed(%s): %s" % (plane, _be),
                    })
                if _bi == 0 or (_bi + 1) % 10 == 0 or (_bi + 1) == _n:
                    _pct = 86.5 + 1.2 * ((_bi + 1) / float(_n))
                    _progress({
                        "phase": "bagua_plane",
                        "pct": round(min(87.7, _pct), 2),
                        "current": _bi + 1,
                        "total": _n,
                        "message": (
                            "信号票卦象 %s %d/%d（复用 %d · 读盘 %d）"
                            % (plane, _bi + 1, _n, _reused, _loaded)
                        ),
                        "code": code,
                        "bagua_plane_reuse": bool(reuse),
                        "bagua_plane_reused": _reused,
                        "bagua_plane_loaded": _loaded,
                        "bagua_signal_symbols": _n,
                    })
            return out

        def _apply_bagua_filters(src_events):
            nonlocal bagua_filter_mode, gua_filter_meta, bagua_n_before, bagua_n_after
            nonlocal bagua_plane_effective, bagua_plane_fallback_count
            evs = list(src_events)
            calc = BaguaCalculator.from_json(cfg.bagua_json)
            # Only symbols that still have signals need OHLC for hexagram attach.
            _needed = _bagua_codes_from_events(evs)
            _bars_map = _bagua_bars_map_for_plane(_bagua_plane, _needed)
            bagua_plane_fallback_count = len(bagua_plane_fallback_codes)
            bagua_plane_effective = _bagua_plane
            _progress({
                "phase": "bagua_attach",
                "pct": 87.8,
                "current": len(_needed),
                "total": max(1, len(_needed)),
                "message": "计算周卦并过滤（%d 条信号 · %d 只票）" % (len(evs), len(_needed)),
                "code": None,
                "n_signals": len(evs),
                "bagua_signal_symbols": len(_needed),
            })
            attach_bagua(
                evs,
                _bars_map,
                calc,
                bagua_period=_bagua_period,
                price_plane=_bagua_plane,
            )
            bagua_n_before = len(evs)
            if gf.is_active():
                try:
                    from ..bagua.filter_rules import compute_bagua_metrics
                    bagua_metrics_pre = compute_bagua_metrics(evs, gf)
                except Exception:
                    bagua_metrics_pre = None
                evs = filter_events_by_gua_filter(evs, gf)
                bagua_n_after = len(evs)
                bagua_filter_mode = f"gua_filter:{gf.selection_mode}"
                try:
                    names = hexagram_name_map(cfg)
                    labels = state_label_map(cfg)
                except Exception:
                    names, labels = {}, {}
                gua_filter_meta = {
                    **gf.to_dict(),
                    "natural_language": gua_filter_natural_language(gf, hexagram_names=names),
                    "history_summary": gua_filter_history_summary(
                        gf, hexagram_names=names, state_labels=labels
                    ),
                    "n_signals_before": bagua_n_before,
                    "n_signals_after": bagua_n_after,
                    "retention_rate": (
                        (bagua_n_after / bagua_n_before) if bagua_n_before else 0.0
                    ),
                }
                if bagua_metrics_pre is not None:
                    gua_filter_meta["bagua_metrics"] = bagua_metrics_pre
            else:
                bagua_filter_mode = (req.bagua_filter_mode or DEFAULT_BAGUA_FILTER_MODE).strip()
                evs = filter_events_by_bagua_mode(evs, bagua_filter_mode)
                bagua_n_after = len(evs)
            return evs

        if use_signal_cache:
            try:
                from ..research.filter_cache import filter_cache_key, get_or_compute_filtered

                # Prefer raw signal cache key if available
                _sk = locals().get("_sig_key")
                if not _sk:
                    from ..research.signal_cache import signal_cache_key as _sck
                    _ind_src2 = "|".join(
                        sorted(
                            "%s:%s" % (s.id, getattr(s, "source_sha256", None) or "")
                            for s in trade_specs
                        )
                    )
                    _sk = _sck(
                        indicator_ids=[s.id for s in trade_specs],
                        indicator_source_hash=_ind_src2,
                        period=period,
                        start=start,
                        end=end,
                        universe_hash=selected_universe_sha(codes),
                        adjust_mode=("research_unadjusted" if research_unadj else "asof_forward_qfq"),
                        factor_manifest_sha=factor_manifest_sha(factor_series),
                        combine=combine,
                    )
                _filter_key = filter_cache_key(
                    signal_cache_key=str(_sk),
                    signal_weekdays=signal_weekdays,
                    gua_rule_version=getattr(gf, "rule_version", None),
                    gua_filter=gf.to_dict() if gf else None,
                    with_bagua=bool(req.with_bagua) or bagua_enabled,
                    bagua_filter_mode=getattr(req, "bagua_filter_mode", None),
                    extra={
                        "bagua_period": _bagua_period,
                        "bagua_price_plane": _bagua_plane,
                        "bagua_ohlc_source": "week_bars_from_%s_days" % _bagua_plane,
                    },
                )
                # Cache stores post-filter events; bagua attach needs period_signal_map on miss.
                # On hit, bagua fields are already on events from prior save.
                # Filter keys inherit signal_cache_key (adjust_mode + factor_manifest).
                events, filter_cache_hit = get_or_compute_filtered(
                    _filter_key,
                    lambda: _apply_bagua_filters(events),
                    cfg=cfg,
                    use_cache=True,
                    meta={"layer": "bagua_filter"},
                )
                bagua_n_after = len(events)
                if filter_cache_hit:
                    bagua_n_before = bagua_n_before or bagua_n_after
                    if gf.is_active():
                        bagua_filter_mode = bagua_filter_mode or f"gua_filter:{gf.selection_mode}"
                        if gua_filter_meta is None:
                            gua_filter_meta = {
                                **gf.to_dict(),
                                "n_signals_before": bagua_n_before,
                                "n_signals_after": bagua_n_after,
                                "retention_rate": (
                                    (bagua_n_after / bagua_n_before) if bagua_n_before else 0.0
                                ),
                                "from_filter_cache": True,
                            }
                    else:
                        bagua_filter_mode = (
                            bagua_filter_mode
                            or (req.bagua_filter_mode or DEFAULT_BAGUA_FILTER_MODE).strip()
                        )
                msg = (
                    ("过滤缓存命中 · " if filter_cache_hit else "")
                    + (
                        f"卦象过滤：{bagua_n_before} → {bagua_n_after} 条信号"
                        if gf.is_active()
                        else f"八卦过滤：{bagua_n_before} → {bagua_n_after} 条信号"
                    )
                )
            except Exception as _fc_err:
                filter_cache_hit = False
                events = _apply_bagua_filters(events)
                errors.append(
                    {"code": "*", "indicator": "filter_cache", "error": str(_fc_err)[:200]}
                )
                msg = f"卦象/八卦过滤：{bagua_n_before} → {bagua_n_after} 条信号"
        else:
            events = _apply_bagua_filters(events)
            msg = (
                f"卦象过滤·{gf.selection_mode}：{bagua_n_before} → {bagua_n_after} 条信号"
                if gf.is_active()
                else (
                    f"八卦过滤·{bagua_mode_label(bagua_filter_mode)}："
                    f"{bagua_n_before} → {bagua_n_after} 条信号"
                )
            )
        _progress({
            "phase": "bagua_filter",
            "pct": 88.0,
            "current": n_codes,
            "total": n_codes,
            "message": msg,
            "code": None,
            "n_signals": bagua_n_after,
            "n_signals_before_bagua": bagua_n_before,
            "filter_cache_hit": filter_cache_hit,
        })

    # Gate C D6: repo/dataset mode derives the trading calendar from the
    # LOCKED execution dataset (full historical range, versioned + hashed).
    # The legacy calendar.json (2016+) remains isolated to legacy mode only.
    calendar_meta: Dict[str, Any] = {}
    if _use_repository_l1 and _repo is not None and _execution_dataset_id:
        from ..data.calendar import build_calendar_from_dataset

        cal, calendar_meta = build_calendar_from_dataset(
            getattr(_repo, "_store"),
            _execution_dataset_id,
            cache_dir=Path(cfg.storage_root) / "calendars",
        )
    else:
        try:
            cal = TradeCalendar.load(cfg.calendar_path)
            calendar_meta = {
                "calendar_source": "legacy_calendar_json",
                "calendar_path": str(cfg.calendar_path),
            }
        except Exception:
            try:
                cal = TradeCalendar.from_tdx(cfg.tdx_root)
                calendar_meta = {
                    "calendar_source": "legacy_tdx_index",
                    "calendar_path": str(cfg.tdx_root),
                }
            except Exception:
                # 无 TDX 部署且无 calendar.json:回退数据集推导日历,
                # 与 repo 模式同一来源;仍失败则置空(不崩溃)。
                cal = None
                calendar_meta = {"calendar_source": "unavailable"}
        if cal is not None and cal.dates:
            calendar_meta["calendar_first"] = int(cal.dates[0])
            calendar_meta["calendar_last"] = int(cal.dates[-1])
            calendar_meta["calendar_count"] = len(cal.dates)

    # Gate C D6 §5: signals earlier than the calendar's first day must be
    # EXCLUDED explicitly — never squeezed onto the first trading day.
    # Repo mode only; legacy mode keeps its historical behavior (isolated).
    if _use_repository_l1 and cal is not None and cal.dates and events:
        _cal_first = int(cal.dates[0])
        _n_before = sum(1 for e in events if int(getattr(e, "date", 0)) < _cal_first)
        if _n_before:
            events = [e for e in events if int(getattr(e, "date", 0)) >= _cal_first]
            calendar_meta["n_signals_before_calendar_excluded"] = _n_before
            errors.append({
                "code": "*",
                "indicator": "calendar",
                "error": (
                    f"before_execution_calendar: {_n_before} signals earlier than "
                    f"execution-calendar first day {_cal_first} excluded "
                    f"(no execution data; not clustered onto the first day)"
                ),
            })

    # L3 explicit corporate-action ledger (ca_events_by_code / ca_meta) was
    # loaded before the per-symbol loop above — the factor-series snap there
    # anchors to the same ledger dates. Cache-only during backtests; network
    # access remains isolated to scripts/sync_ca_events.py.

    run_id = req.run_id or f"bt_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    unconfirmed_run = bool(
        allow_unconfirmed
        and any(
            a.get("source_pair_status")
            in (
                "paired_unconfirmed",
                "stale_source_hash",
                "source_missing",
                "package_missing",
                "stale_package_hash",
            )
            for a in formula_audits.values()
        )
    )
    # four-lane portfolio + artifacts (schedule/price/bagua/cache via context)
    engine = (getattr(req, "engine", None) or "full").strip().lower()
    holiday_policy = getattr(req, "holiday_policy", None) or "next_trading_day"
    artifact_level = getattr(req, "artifact_level", None) or "full"
    try:
        from ..research.artifacts import apply_artifact_policy, normalize_artifact_level

        artifact_level = normalize_artifact_level(artifact_level, default="full")
        art_flags = apply_artifact_policy(level=artifact_level)
    except Exception:
        art_flags = {
            "write_signals": True,
            "write_fills": True,
            "write_excel": True,
            "write_equity": True,
            "write_meta": True,
        }

    from .backtest_context import (
        BacktestRunContext,
        BaguaState,
        CacheState,
        PriceModes,
        ScheduleParams,
        run_portfolio_and_finalize,
    )

    ctx = BacktestRunContext(
        cfg=cfg,
        req=req,
        codes=codes,
        schedule=ScheduleParams(
            period=period,
            hold=hold,
            entry_lag=entry_lag,
            buy_on=buy_on,
            sell_on=sell_on,
            buy_weekday=buy_weekday,
            exit_weekday=exit_weekday,
            signal_weekdays=signal_weekdays,
            holiday_policy=holiday_policy,
            start=start,
            end=end,
        ),
        price=PriceModes(
            research_unadj=research_unadj,
            formal_ok=formal_ok,
            adj_msg=adj_msg,
        ),
        bagua=BaguaState(
            enabled=bagua_enabled,
            filter_mode=bagua_filter_mode,
            gua_filter_meta=gua_filter_meta,
            gf=gf,
            n_before=bagua_n_before,
            n_after=bagua_n_after,
            plane_requested=_bagua_plane,
            plane_effective=bagua_plane_effective if bagua_enabled else _bagua_plane,
            plane_missing_count=int(bagua_plane_fallback_count or 0),
            plane_missing_codes=list(bagua_plane_fallback_codes[:50]),
        ),
        cache=CacheState(
            signal_hit=signal_cache_hit,
            filter_hit=filter_cache_hit,
            execution_hit=execution_cache_hit,
            use_signal_cache=use_signal_cache,
        ),
        combine=combine,
        engine=engine,
        artifact_level=artifact_level,
        art_flags=art_flags,
        run_id=run_id,
        trade_specs=trade_specs,
        formula_audits=formula_audits,
        factor_series=factor_series,
        ca_events_by_code=ca_events_by_code,
        ca_meta=ca_meta,
        unconfirmed_run=unconfirmed_run,
        n_events_raw_signals=n_events_raw_signals,
        n_events_after_weekday=n_events_after_weekday,
        errors=errors,
        calendar_meta=calendar_meta,
        pit_meta=pit_meta,
        delist_policy=_delist_policy,
        delist_terminal_dates=_delist_terminal_dates,
        delist_meta=delist_meta,
    )
    return run_portfolio_and_finalize(
        ctx,
        cal=cal,
        events=events,
        raw_map=raw_map,
        adj_map=adj_map,
        standard_qfq_map=standard_qfq_map,
        progress=_progress,
    )
