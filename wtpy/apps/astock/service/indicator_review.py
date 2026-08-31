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
    max_date = int(pair.cutoff or pair.l1_max_date or 0)
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
) -> Dict[str, Any]:
    """全市场复核两条指标公式在 asof 当日的 XG 命中，产出 review_{asof}.json。

    返回摘要 dict（同落盘 JSON）。``bar_loader`` / ``surface_resolver`` 为测试
    注入点，默认走正式 L1 产品面 + BaguaPlaneSession 加载。
    """
    t0 = time.time()
    sheet_of = dict(DEFAULT_REVIEW_RULES)
    rules = list(DEFAULT_REVIEW_RULES)
    if rule_ids:
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
        # 若把 ok 冲成 no_go，导出侧会白白丢掉两个 sheet
        if summary["asof"]:
            path = review_output_path(cfg, summary["asof"])
            if path.exists() and not force:
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    existing = None
                if existing is not None and existing.get("status") == "ok":
                    existing["reused"] = True
                    logger.info(
                        "indicator_review 表面不可用但已有 ok 结果，保留不覆盖: %s",
                        path,
                    )
                    return existing
            _atomic_write_json(path, summary)
        return summary

    eff_asof = _normalize_asof(cfg, asof, int(surface["max_date"]))
    out_path = review_output_path(cfg, eff_asof)
    if out_path.exists() and not force:
        try:
            cached = json.loads(out_path.read_text(encoding="utf-8"))
            cached["reused"] = True
            logger.info("indicator_review 幂等命中: %s", out_path)
            return cached
        except Exception as e:  # noqa: BLE001
            logger.warning("indicator_review 缓存损坏（重算）: %s", e)

    # 规则 spec：编译不可用直接抛错（CLI 非零退出），不产出半截结果
    from ..indicators.registry import IndicatorRegistry

    reg = IndicatorRegistry.bootstrap(cfg.indicator_dir, cfg.mapping_path)
    specs: List[Tuple[str, str, Any]] = []
    for rid, sheet in rules:
        spec = reg.get(rid)  # KeyError 即规则不存在
        if spec.compile_status != "ready":
            raise RuntimeError(f"规则 {rid} 编译状态 {spec.compile_status}: {spec.failure_reason}")
        specs.append((rid, sheet, spec))

    universe = _resolve_codes(cfg, codes)
    load = bar_loader or _default_bar_loader_factory(cfg)

    matched_by_rule: Dict[str, List[Dict[str, Any]]] = {rid: [] for rid, _s, _sp in specs}
    errors: List[Dict[str, str]] = []
    error_count = 0
    scanned = 0
    n = len(universe)

    for i, code in enumerate(universe):
        try:
            bars, _meta = load(code, eff_asof)
            if not bars or int(bars[-1].date) != eff_asof:
                # asof 当日无 K 线（停牌/退市/未上市）：无信号可言，跳过不计错
                scanned += 1
            else:
                bars_dict = bars_dict_from_day(bars)
                dates_arr = bars_dict["date"]
                close = float(bars[-1].close)
                for rid, _sheet, spec in specs:
                    sig, err = compute_indicator_signal(spec, bars_dict)
                    if err:
                        error_count += 1
                        if len(errors) < _MAX_ERROR_RECORDS:
                            errors.append({"code": code, "rule": rid, "error": err})
                        continue
                    hit_dates = signal_dates(dates_arr, sig)
                    if hit_dates and hit_dates[-1] == eff_asof:
                        matched_by_rule[rid].append({"code": code, "close": close})
                scanned += 1
        except Exception as e:  # noqa: BLE001
            error_count += 1
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
        "universe_size": n,
        "scanned": scanned,
        "error_count": error_count,
        "errors": errors,
        "rules": [
            {"rule_id": rid, "sheet": sheet, "count": len(matched_by_rule[rid]),
             "matched": matched_by_rule[rid]}
            for rid, sheet, _spec in specs
        ],
        "duration_sec": round(time.time() - t0, 1),
    }
    _atomic_write_json(out_path, summary)
    logger.info(
        "indicator_review 完成 asof=%s scanned=%d 命中 %s 错误 %d 用时 %.1fs",
        eff_asof, scanned,
        {r["sheet"]: r["count"] for r in summary["rules"]},
        error_count, summary["duration_sec"],
    )
    return summary


def load_review_for_export(
    cfg: AStockConfig,
    asof: int,
    *,
    max_age_days: int = REVIEW_MAX_AGE_DAYS,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """导出侧读取复核结果。返回 (review|None, note)。

    优先精确匹配 ``review_{asof}.json``；缺失时回看 ``max_age_days`` 天内
    最近一次复核（周五链产出、周末/下周初导出仍带 sheet）。
    note 为空串表示正常；否则为 missing / no_go:<原因> / stale:<asof'> 等。
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
    return review, f"fallback:使用 {latest} 复核（导出日 {asof} 无当日复核）"


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
