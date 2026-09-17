"""卦象工作台后端服务（PLAN-BAGUA-UX-V1.1）。

三个入口的后端支撑：统一标的检索（instruments）、真实可用日期（options）、
按规则当天筛选（screen）与独立异步任务容器。

设计要点：
- 筛选当天命中：复用 ``indicator_review.run_weekly_review(persist=False)``——
  正式 L1 + Tushare 前复权路径，不读不写 ``review_{asof}.json``（周五链共享
  缓存）；数据面不可用原样 no_go（persist 修复后不会回读旧缓存冒充成功）。
- 显式日期严格校验：非交易日/超出数据覆盖直接拒绝并给出建议日期，禁止
  静默回退（``_normalize_asof`` 的静默收敛只发生在内部默认值上）。
- 组合运算 any/all 在同一有效日期、同一股票范围内进行，按规范标识去重。
- 「未完成评估」：缺数据（当日无 K 线）与计算失败（``failed_codes``）的票
  不进默认命中列表——命中票必须是所选规则全部完成计算的。
- 独立内存任务容器 + 单工作线程 + 有界等待队列（≤5），队列满返回繁忙。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..config import AStockConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 异常（路由层转 HTTP）
# ---------------------------------------------------------------------------


class ScreenError(Exception):
    """筛选入口的确定性业务错误（400 语义）。"""


class ScreenDataUnavailable(ScreenError):
    """正式 L1 数据面不可用（no_go 语义，503）。"""


class ScreenDateNotAvailable(ScreenError):
    """显式日期当天无可用数据（拒绝执行，携带建议日期，禁止静默回退）。"""

    def __init__(self, requested: int, suggested: int, reason: str):
        self.requested = int(requested)
        self.suggested = int(suggested)
        self.reason = reason
        super().__init__(
            f"{reason}：{requested} 当天没有可用数据；最近的可用交易日为 "
            f"{suggested}，请选择后重新筛选"
        )


class ScreenQueueFull(ScreenError):
    """筛选等待队列已满（繁忙语义，429）。"""


# ---------------------------------------------------------------------------
# 统一标的目录（instruments）
# ---------------------------------------------------------------------------

_INSTRUMENT_CATALOG: dict = {"key": None, "stocks": [], "idx_etf": []}
_INSTRUMENT_LOCK = threading.Lock()


def _exchange_of(std_code: str) -> str:
    return std_code.split(".")[0] if "." in std_code else ""


def _stock_pool(cfg: AStockConfig) -> List[str]:
    """与筛选/导出同源的股票票池（universe.json 优先 → Tushare raw 基线）。"""
    from .indicator_review import _resolve_codes

    return _resolve_codes(cfg, None)


def _instrument_catalog(cfg: AStockConfig) -> Dict[str, Any]:
    """构建股票 + 指数/ETF 统一目录（进程内缓存，60s TTL）。

    股票名称来自 stock_names 缓存（TDX infoharbor > 周报快照 > universe.json）；
    指数/ETF 用系统预置 watchlist（当前查询能力的真实边界）。
    """
    import time as _time

    from .index_etf import watchlist
    from .stock_names import ensure_name_cache

    key = str(cfg.storage_root)
    now = _time.time()
    with _INSTRUMENT_LOCK:
        if _INSTRUMENT_CATALOG["key"] == key and now - (_INSTRUMENT_CATALOG.get("ts") or 0) < 60:
            return _INSTRUMENT_CATALOG

    names: Dict[str, str] = {}
    try:
        names = ensure_name_cache(cfg) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("instruments 名称缓存失败（退化为仅代码）: %s", e)

    stocks: List[Dict[str, str]] = []
    try:
        for std in _stock_pool(cfg):
            if ".STK." not in std:
                continue
            code6 = std.split(".")[-1]
            stocks.append(
                {
                    "id": std,
                    "code": code6,
                    "name": names.get(code6, ""),
                    "type": "STK",
                }
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("instruments 股票票池构建失败: %s", e)

    idx_etf: List[Dict[str, str]] = []
    try:
        _type_map = {"index": "IDX", "etf": "ETF", "stock": "STK"}
        for entry in watchlist(cfg, kind="all", include_availability=False):
            std = entry.get("std_code") or ""
            if not std:
                continue
            code6 = std.split(".")[-1]
            idx_etf.append(
                {
                    "id": std,
                    # 六位代码：与股票目录同构，保证 000001 这类歧义输入
                    # 能同时命中 平安银行(STK) 与 上证指数(IDX) 候选
                    "code": code6,
                    "name": str(entry.get("name") or ""),
                    "type": _type_map.get(str(entry.get("type") or "").lower(), "IDX"),
                }
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("instruments 指数/ETF 目录构建失败: %s", e)

    with _INSTRUMENT_LOCK:
        _INSTRUMENT_CATALOG.update(
            {"key": key, "ts": now, "stocks": stocks, "idx_etf": idx_etf}
        )
    return _INSTRUMENT_CATALOG


_CODE_ALIAS_RE = None  # 占位：见 _match_query


def _match_query(items: List[Dict[str, str]], q: str, kinds: Optional[List[str]], limit: int) -> List[Dict[str, str]]:
    """按代码/名称/别名匹配目录项；排序：精确代码 > 代码前缀 > 名称前缀 > 名称包含。

    六位代码歧义（000001 = 平安银行 + 上证指数）天然返回多条候选项。
    """
    t = (q or "").strip().lower()
    t = t.replace("　", "").replace(" ", "")
    # sh600000 / sz000001 / 600000.sh / 600000.sz 前缀别名归一为六位代码 + 交易所提示
    alias_code = ""
    alias_exch = ""
    cleaned = t
    import re as _re

    m = _re.match(r"^(sh|sz|bj)(\d{6})$", t)
    if m:
        alias_code = m.group(2)
        alias_exch = {"sh": "SSE", "sz": "SZSE", "bj": "BSE"}[m.group(1)]
    else:
        m = _re.match(r"^(\d{6})\.(sh|sz|bj)$", t)
        if m:
            alias_code = m.group(1)
            alias_exch = {"sh": "SSE", "sz": "SZSE", "bj": "BSE"}[m.group(2)]
            cleaned = alias_code
        elif _re.match(r"^\d{6}$", t):
            alias_code = t

    def _visible(it: Dict[str, str]) -> bool:
        return not kinds or it["type"] in kinds

    exact: List[Dict[str, str]] = []
    prefix: List[Dict[str, str]] = []
    name_prefix: List[Dict[str, str]] = []
    name_contains: List[Dict[str, str]] = []
    for it in items:
        if not _visible(it):
            continue
        code6 = it["code"]
        name = it["name"]
        name_l = name.lower()
        if alias_code and code6 == alias_code:
            if alias_exch and it["id"].split(".")[0] != alias_exch:
                continue
            exact.append(it)
        elif not t:
            continue
        elif alias_code and code6.startswith(alias_code):
            prefix.append(it)
        elif not alias_code and code6.startswith(t):
            prefix.append(it)
        elif name_l.startswith(t):
            name_prefix.append(it)
        elif t in name_l:
            name_contains.append(it)
    if not t:
        # 空查询不返回全量（页面提示先输入）；指数/ETF 预置小目录仍给入口
        return [it for it in (idx_etf_only(items))][:limit]
    out = exact + prefix + name_prefix + name_contains
    # 规范标识去重
    seen: set = set()
    uniq: List[Dict[str, str]] = []
    for it in out:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        uniq.append(it)
    return uniq[:limit]


def idx_etf_only(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return [it for it in items if it["type"] in ("IDX", "ETF")]


def search_instruments(
    cfg: AStockConfig,
    q: str,
    *,
    limit: int = 20,
    kinds: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """统一名称/代码检索：股票 + 指数 + ETF，返回规范标识（避免六位代码碰撞）。"""
    cat = _instrument_catalog(cfg)
    items = cat["stocks"] + cat["idx_etf"]
    found = _match_query(items, q, kinds, max(1, min(int(limit), 50)))
    exch_label = {"SSE": "沪市", "SZSE": "深市", "BSE": "北交所"}
    type_label = {"STK": "股票", "IDX": "指数", "ETF": "ETF"}
    out: List[Dict[str, Any]] = []
    for it in found:
        exch = _exchange_of(it["id"])
        out.append(
            {
                "id": it["id"],
                "code": it["code"],
                "name": it["name"] or it["code"],
                "type": it["type"],
                "type_label": type_label.get(it["type"], it["type"]),
                "exchange": exch,
                "exchange_label": exch_label.get(exch, exch),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 可用日期与口径（options）
# ---------------------------------------------------------------------------


def _period_suggestions(bars) -> Dict[str, Any]:
    """日线 bars → 日线最新交易日 + 最近一个完整周/月（含 closed 标注）。"""
    from ..data.periods import aggregate_month, aggregate_week

    if not bars:
        return {}
    day_last = int(bars[-1].date)

    def _last_closed(aggregated):
        for pb in reversed(aggregated):
            if pb.closed:
                return pb.to_dict()
        return None

    week_closed = _last_closed(aggregate_week(list(bars)))
    month_closed = _last_closed(aggregate_month(list(bars)))
    week_last = aggregate_week(list(bars))[-1].to_dict() if list(bars) else None
    month_last = aggregate_month(list(bars))[-1].to_dict() if list(bars) else None
    return {
        "day": {"latest": day_last},
        "week": {"latest_closed": week_closed, "latest": week_last},
        "month": {"latest_closed": month_closed, "latest": month_last},
    }


def instrument_options(
    cfg: AStockConfig,
    code: str,
    *,
    adjust: str = "raw",
) -> Dict[str, Any]:
    """按标的返回真实可用日期、建议周期、完整性与可用价格口径。

    日期一律来自该标的在指定口径下的实际行情覆盖，不用电脑日期冒充。
    """
    from .bagua_query import (
        load_day_bars_for_plane,
        normalize_adjust_mode,
        normalize_query_code,
    )

    std_code = normalize_query_code(code)
    if ".IDX." in std_code:
        sym_type = "IDX"
    elif ".ETF." in std_code:
        sym_type = "ETF"
    else:
        sym_type = "STK"

    # 指数/ETF 无复权概念，按未复权
    plane = normalize_adjust_mode(adjust) if sym_type == "STK" else "raw"
    if plane == "tdx_front":
        raise ValueError("tdx_front 已停用：请使用 tushare_qfq 或 raw")

    try:
        bars, meta = load_day_bars_for_plane(cfg, std_code, plane)
    except FileNotFoundError as e:
        raise ValueError(f"标的 {std_code} 在 {plane} 口径下没有可用行情: {e}") from e

    dates = [int(b.date) for b in bars]
    if not dates:
        raise ValueError(f"标的 {std_code} 在 {plane} 口径下没有可用行情")

    # 数据面最新日（全市场口径，来自正式 L1 产品面；缺产品面时退化为标的自身）
    surface_max = 0
    try:
        from .indicator_review import _resolve_formal_surface

        surface, _reason = _resolve_formal_surface(cfg)
        if surface:
            surface_max = int(surface.get("max_date") or 0)
    except Exception:  # noqa: BLE001
        surface_max = 0

    last = max(dates)
    first = min(dates)
    if sym_type == "STK":
        price_options = [
            {"value": "raw", "label": "未复权", "available": True},
            {"value": "tushare_qfq", "label": "Tushare 前复权", "available": True},
        ]
    else:
        price_options = [
            {
                "value": "raw",
                "label": "未复权（当前系统按未复权计算）",
                "available": True,
            }
        ]
    suggestions = _period_suggestions(bars)
    return {
        "id": std_code,
        "symbol_type": sym_type,
        "adjust": plane,
        "first_date": first,
        "last_date": last,
        "surface_max_date": surface_max or last,
        # 覆盖完整到数据面最新日（标的停牌/退市时为 False）
        "up_to_date": bool(surface_max and last >= surface_max),
        "suggested": suggestions,
        "price_options": price_options,
    }


# ---------------------------------------------------------------------------
# 可筛选规则目录（screen/rules）
# ---------------------------------------------------------------------------


def screen_rule_reasons(spec_pub: Dict[str, Any]) -> Optional[str]:
    """规则不可筛选的原因；None 表示可执行。

    本轮只开放：通达信公式、signal 输出、编译 ready、支持日线、
    无 MIN1 依赖（MIN60 有日线代理可执行）。
    """
    if spec_pub.get("archived"):
        return "规则已归档"
    kind = str(spec_pub.get("kind") or "")
    if kind != "tdx_formula":
        return "仅支持通达信公式规则"
    if str(spec_pub.get("output_type") or "") != "signal":
        return "仅支持信号（XG）输出规则"
    status = str(spec_pub.get("compile_status") or "")
    if status != "ready":
        reason = spec_pub.get("failure_reason") or "编译不可用"
        return f"公式编译未就绪（{reason}）"
    periods = [str(p).upper() for p in (spec_pub.get("supported_periods") or [])]
    if "DAY" not in periods:
        return "不支持日线"
    deps = [str(d).upper() for d in (spec_pub.get("dependencies") or [])]
    if "MIN1" in deps:
        return "依赖 1 分钟数据，当前引擎不可执行"
    return None


# ---------------------------------------------------------------------------
# 跟踪模块共用的「当前规则目录」与归并打分（2026-09-15）
#
# 跟踪 L0/L1（api_routes/tracking.py）与跟踪导出（service/track_export.py）
# 都要做两件事：规则删除同步过滤、同指纹多 id 归并成一行。目录来源与
# canonical 打分**必须同源**——两处若各自实现，将来改一处忘另一处，页面
# 与导出就会对同一组选出不同代表，行数对不上账（契约：UI 与导出一致）。
# ---------------------------------------------------------------------------


def current_rule_catalog(cfg) -> Dict[str, Dict[str, Any]]:
    """当前规则目录（规则中心可见 ∪ 周五链预置复核规则）：id -> 判定信息。

    与 list_screen_rules 同数据源（规则中心口径），但以 id 索引直接返回
    name/executable/hidden/source，供跟踪 L0/L1 与跟踪导出共用。
    """
    from .indicator_review import DEFAULT_REVIEW_RULES
    from .rules import RuleService

    rs = RuleService(cfg)
    out: Dict[str, Dict[str, Any]] = {}
    for r in rs.list_rules(include_archived=False, include_hidden=False):
        out[str(r["id"])] = {
            "id": str(r["id"]),
            "name": str(r.get("name") or ""),
            "executable": screen_rule_reasons(r) is None,
            "hidden": bool(r.get("hidden")),
            "source": str(r.get("source") or ""),
        }
    for rid, _sheet in DEFAULT_REVIEW_RULES:
        out.setdefault(
            str(rid),
            {
                "id": str(rid), "name": str(rid), "executable": True,
                "hidden": True, "source": "builtin",
            },
        )
    return out


def pick_canonical_rule_id(
    ids: List[str], catalog: Optional[Dict[str, Dict[str, Any]]]
) -> str:
    """同指纹的多个 rule_id 里选一个代表身份（确定性，避免每次刷新换行）。

    优先级：目录内存在 > 可执行 > 未被隐藏 > 来源（user > builtin > system）
    > id 字典序。catalog 为空（降级）时退化为「来源 > id」，仍保证稳定。
    """

    def score(rid: str):
        info = (catalog or {}).get(rid)
        return (
            0 if info is not None else 1,
            0 if (info and info.get("executable")) else 1,
            0 if (info and not info.get("hidden")) else 1,
            {"user": 0, "builtin": 1, "system": 2}.get(
                str((info or {}).get("source") or ""), 3
            ),
            rid,
        )

    return sorted(ids, key=score)[0]


def list_screen_rules(ctx) -> Dict[str, Any]:
    """筛选规则目录 = 规则中心当前可见的规则 ∪ 周五链预置复核规则。

    与规则中心保持同步（用户要求）：在规则中心删除（系统规则进 hidden 名单 /
    user 规则硬删）或归档的规则不再出现。``DEFAULT_REVIEW_RULES``（735金叉及
    趋势 / 先跌后涨新版5日外）是被 hidden 名单遮蔽的预置指标规则、周五链与
    导出信号 sheet 依赖，按 V1.1 计划「包含可执行的隐藏指标规则」强制纳入，
    hidden 标志仍用于 ⓘ 展示。
    """
    rows = ctx.rules.list_rules(include_archived=False, include_hidden=False)
    by_id = {r["id"]: r for r in rows}
    from .indicator_review import DEFAULT_REVIEW_RULES

    for rid, _sheet in DEFAULT_REVIEW_RULES:
        if rid in by_id:
            continue
        try:
            by_id[rid] = ctx.rules.get_rule(rid, include_formula=False)
        except KeyError:
            continue  # 本机公式目录没有该规则（如 CI fixture 环境）
    # get_rule 不带 hidden 标志；预置规则是否被遮蔽从 hidden 名单补齐（ⓘ 展示用）
    hidden_ids = ctx.rules._load_hidden_ids()
    for r in by_id.values():
        r.setdefault("hidden", r["id"] in hidden_ids)
    out: List[Dict[str, Any]] = []
    for r in by_id.values():
        reason = screen_rule_reasons(r)
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                # 来源与版本：同名规则（如两条「735金叉及趋势」）靠这几项区分，
                # 前端不做按名去重，避免把不同公式合并成一条
                "source": r.get("source", ""),
                "version": r.get("version", ""),
                "description": r.get("description", ""),
                "category": r.get("category", ""),
                "hidden": bool(r.get("hidden")),
                "executable": reason is None,
                "reason": reason or "",
            }
        )
    out.sort(key=lambda x: (not x["executable"], x["hidden"], x["name"]))
    return {"ok": True, "count": len(out), "rules": out}


# ---------------------------------------------------------------------------
# 严格日期解析 + 筛选执行
# ---------------------------------------------------------------------------


def _load_calendar(cfg: AStockConfig):
    cal_path = Path(cfg.calendar_path)
    if not cal_path.exists():
        return None
    try:
        from ..data.calendar import TradeCalendar

        return TradeCalendar.load(cal_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("筛选日历加载失败（按数据面日期处理）: %s", e)
        return None


def _parse_asof(raw: Any) -> int:
    s = str(raw or "").strip().replace("-", "").replace("/", "")
    if not s.isdigit() or len(s) != 8:
        raise ScreenError(f"日期格式无效：{raw!r}（需要 YYYY-MM-DD）")
    return int(s)


def resolve_screen_asof(
    cfg: AStockConfig, requested: Optional[Any] = None
) -> Tuple[int, Dict[str, Any]]:
    """解析筛选基准日：默认取数据面最新交易日；显式日期严格校验。

    返回 (eff_asof, surface_info)。数据面不可用抛 ScreenDataUnavailable；
    显式日期非交易日/超出覆盖抛 ScreenDateNotAvailable（带建议日期）。
    """
    from .indicator_review import _normalize_asof, _resolve_formal_surface

    surface, reason = _resolve_formal_surface(cfg)
    if surface is None:
        raise ScreenDataUnavailable(f"数据面不可用：{reason}")
    surface_max = int(surface.get("max_date") or 0)
    if surface_max <= 0:
        raise ScreenDataUnavailable("数据面不可用：formal_l1_max_date_unknown")
    latest = _normalize_asof(cfg, surface_max, surface_max)
    if requested is None or str(requested).strip() in ("", "latest"):
        return latest, surface
    req = _parse_asof(requested)
    if req > surface_max:
        raise ScreenDateNotAvailable(req, latest, f"超出数据覆盖（最新 {surface_max}）")
    cal = _load_calendar(cfg)
    if cal is not None and cal.dates and req <= cal.dates[-1] and not cal.is_trading_day(req):
        prior = [d for d in cal.dates if d <= req]
        suggested = max(prior) if prior else latest
        raise ScreenDateNotAvailable(req, suggested, "非交易日")
    return req, surface


# ---------------------------------------------------------------------------
# cache-first：快照服务筛选请求（契约 §规则组②）
# ---------------------------------------------------------------------------


def _current_rule_fingerprints(
    cfg: AStockConfig, rule_ids: Sequence[str]
) -> Optional[Dict[str, str]]:
    """请求规则的当前指纹（规则公式已改 → 快照 stale 不得复用）。

    fail-closed 语义（审查 🔴B3 修正）：**逐规则**解析——单个规则解析
    失败（如已被删除）时该规则指纹记 None（判 stale，落回现算），
    绝不让一个 KeyError 使整组校验失效；只有注册表整体不可用才返回
    None（调用方视为无法覆盖，走现算）。
    """
    try:
        from .indicator_review import _spec_fingerprint, user_registry_file

        from ..indicators.registry import IndicatorRegistry

        reg = IndicatorRegistry.bootstrap(
            cfg.indicator_dir,
            cfg.mapping_path,
            user_registry_path=user_registry_file(cfg),
        )
    except Exception:  # noqa: BLE001
        return None  # 注册表整体不可用 → 无法验证 → 不允许快照覆盖
    out: Dict[str, Optional[str]] = {}
    for r in rule_ids:
        try:
            spec = reg.get(str(r))
        except KeyError:
            out[str(r)] = None  # 规则已删除：旧快照对它是陈旧结果
            continue
        out[str(r)] = _spec_fingerprint(spec)
    return out


def try_screen_from_snapshot(
    cfg: AStockConfig,
    *,
    rule_ids: Sequence[str],
    match_mode: str,
    asof: Optional[Any],
    codes: Optional[Sequence[str]],
) -> Optional[Dict[str, Any]]:
    """尝试用已发布快照直接组装筛选结果；不可完整服务时返回 None。

    只读**发布快照**（契约 §0：统计与筛选只认 published 指针）。
    覆盖判定逐规则做（指纹+状态），缺规则/规则失败/公式已改 → None
    落回现算，绝不把 partial 冒充完整结果。
    picked 范围：快照 universe 内按 codes 过滤；越界票逐票 not_in_universe
    （partial），由调用方决定是否接受（当前策略：越界即落回现算，保证
    指定范围的每一只票都有真实评估）。
    """
    from . import screen_snapshots as ss

    rid_list = [str(r).strip() for r in rule_ids if str(r).strip()]
    if not rid_list:
        return None
    # asof 归一化（审查 🔴B2）：与 submit_screen_job 的 params 归一同口径——
    # "latest"/空串 = 缺省语义，绝不能把原始字面量直通 _parse_asof（会 400）
    asof_norm = (
        None if asof is None or str(asof).strip() in ("", "latest") else asof
    )
    try:
        eff_asof, _surface = resolve_screen_asof(cfg, asof_norm)
    except (ScreenDataUnavailable, ScreenDateNotAvailable, ScreenError):
        return None  # 日期/数据面问题交给原路径抛给用户
    snap = ss.load_published_snapshot_for_week(cfg, eff_asof)
    if snap is None:
        return None
    cov = ss.snapshot_covers(
        snap, rid_list, current_rule_fps=_current_rule_fingerprints(cfg, rid_list)
    )
    if cov["uncovered"] or cov["stale"]:
        return None
    code_list = None
    if codes is not None:
        stk, _non_stk, _unknown = _classify_codes(codes)
        code_list = _dedup_keep_order(stk)
        universe = set(snap.get("universe_codes") or [])
        out_of = [c for c in code_list if c not in universe]
        if out_of:
            return None  # 越界票需要真实评估 → 现算
    combined = ss.combine_snapshot_hits(
        snap, rule_ids=rid_list, match_mode=match_mode, codes=code_list
    )
    # 组合语义带回的 incomplete（error/no_data 票）如实透出
    name_map: Dict[str, str] = {}
    if combined["hits"]:
        try:
            from .stock_names import ensure_name_cache

            name_map = ensure_name_cache(cfg) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("快照筛选名称缓存不可用（仅显示代码）: %s", e)
    hits = combined["hits"]
    for e in hits:
        e["name"] = name_map.get(str(e["code"]).split(".")[-1], "")
    result = {
        "status": "ok",
        "source": "cache",
        "snapshot_id": snap.get("snapshot_id"),
        "asof": combined["asof"],
        "requested_asof": _parse_asof(asof_norm) if asof_norm else None,
        "match_mode": match_mode,
        "rule_ids": rid_list,
        # 两条路径字段对齐（前端 wbRenderScreenJob 统一渲染）：
        # 缓存路径没有全局 error_count 概念——逐规则失败已进
        # incomplete_count；scope_size = 快照 universe（all）或指定范围
        "scope_size": (
            len(code_list) if code_list else combined["universe_size"]
        ),
        "codes_scoped": codes is not None,
        "formal_l1_id": snap.get("data_version", {}).get("base_dataset_id"),
        "universe_size": combined["universe_size"],
        "evaluated": max(
            0, combined["universe_size"]
            - int(snap.get("missing_count") or 0)
            - combined["incomplete_count"]
        ),
        "missing_count": int(snap.get("missing_count") or 0),
        "error_count": 0,
        "incomplete_codes": sorted(
            {i["code"] for i in combined["indeterminate"]}
        )[:2000],
        "incomplete_count": combined["incomplete_count"],
        "rules": combined["rules"],
        "hits": hits,
        "matched_count": len(hits),
        "complete": combined["complete"],
        "generated_at": snap.get("generated_at"),
        "duration_sec": 0.0,
    }
    return result


def _dedup_keep_order(items: Sequence[str]) -> List[str]:
    out: List[str] = []
    seen: set = set()
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _classify_codes(
    codes: Optional[Sequence[str]],
) -> Tuple[List[str], List[str], List[str]]:
    """把指定范围输入分成 (A 股股票, 非股票指数/ETF, 无法识别) 三类。

    归一规则与查询侧一致（``normalize_query_code`` 优先，``to_std_code`` 兜底）。
    分类而不是先过滤，是为了让「混合范围」能如实报出被排除的数量与原因；
    早期实现先过滤再交给 run_screen，导致排除明细永远为 0。
    """
    from ..data.universe import to_std_code
    from .bagua_query import normalize_query_code

    stk: List[str] = []
    non_stk: List[str] = []
    unknown: List[str] = []
    for raw in codes or []:
        t = str(raw).strip()
        if not t:
            continue
        std = ""
        try:
            if t.startswith(("SSE.", "SZSE.", "BSE.")):
                std = t
            else:
                std = normalize_query_code(t)
        except Exception:  # noqa: BLE001
            try:
                std = to_std_code(t)
            except Exception:  # noqa: BLE001
                std = ""
        if not std:
            unknown.append(t)
        elif ".STK." in std:
            stk.append(std)
        else:
            non_stk.append(std)
    return stk, non_stk, unknown


def _normalize_codes(codes: Optional[Sequence[str]]) -> List[str]:
    """规范标识归一 + 去重（保持顺序），只保留 A 股股票。"""
    stk, _non_stk, _unknown = _classify_codes(codes)
    out: List[str] = []
    seen: set = set()
    for std in stk:
        if std in seen:
            continue
        seen.add(std)
        out.append(std)
    return out


def run_screen(
    cfg: AStockConfig,
    *,
    rule_ids: Sequence[str],
    match_mode: str = "any",
    asof: Optional[Any] = None,
    codes: Optional[Sequence[str]] = None,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """执行当天筛选：正式 L1 + Tushare 前复权、persist=False（不碰周五链缓存）。

    返回结果 dict（status=ok / no_go）。组合运算在同一 eff_asof、同一
    股票范围内进行；缺数据/计算失败的票列为未完成评估，不进命中列表。
    """
    from . import indicator_review as ir

    if match_mode not in ("any", "all"):
        raise ScreenError(f"match_mode 必须是 any/all，收到 {match_mode!r}")
    rid_list = [str(r).strip() for r in (rule_ids or []) if str(r).strip()]
    if not rid_list:
        raise ScreenError("未选择筛选规则")
    eff_asof, surface = resolve_screen_asof(cfg, asof)
    code_list = None
    excluded_count = 0
    excluded_note = ""
    # codes=None → 全市场；codes=[]（提交了空范围）→ 必须拒绝，
    # 绝不与 None 混同导致静默回退全市场。这里不能用真假值判断。
    if codes is not None:
        stk_all, non_stk, unknown = _classify_codes(codes)
        code_list = _dedup_keep_order(stk_all)  # 已归一，只需去重
        if not code_list:
            raise ScreenError(
                "指定范围内没有可筛选的 A 股股票（指数/ETF 不参与规则筛选）；"
                "请检查范围或改用全部 A 股"
            )
        # 排除明细：指数/ETF 与无法识别的输入分别计数（混合范围也要如实提示）
        unknown_set = set(_dedup_keep_order(unknown))
        non_stk = [x for x in _dedup_keep_order(non_stk) if x not in unknown_set]
        excluded_count = len(non_stk) + len(unknown_set)
        parts = []
        if non_stk:
            parts.append(f"{len(non_stk)} 个标的为指数/ETF，不参与规则筛选")
        if unknown_set:
            parts.append(f"{len(unknown_set)} 个输入无法识别")
        if parts:
            excluded_note = "范围中 " + "；".join(parts) + "，已排除"

    summary = ir.run_weekly_review(
        cfg,
        asof=eff_asof,
        rule_ids=rid_list,
        codes=code_list,
        persist=False,
        on_progress=on_progress,
    )
    base = {
        "asof": eff_asof,
        "requested_asof": _parse_asof(asof) if asof else None,
        "match_mode": match_mode,
        "rule_ids": rid_list,
        "formal_l1_id": surface.get("formal_l1_id"),
        "codes_scoped": codes is not None,
        "scope_size": len(code_list) if code_list else summary.get("universe_size", 0),
    }
    if summary.get("status") != "ok":
        # 数据面不可用 / no_go：原样透出，绝不冒充成功
        return {
            **base,
            "status": summary.get("status", "no_go"),
            "no_go_reason": summary.get("no_go_reason", ""),
            "universe_size": summary.get("universe_size", 0),
            "evaluated": 0,
            "missing_count": 0,
            "error_count": summary.get("error_count", 0),
            "errors": summary.get("errors", []),
            "rules": [],
            "hits": [],
            "matched_count": 0,
        }

    universe_size = int(summary.get("universe_size") or 0)
    missing_count = int(summary.get("missing_count") or 0)
    error_count = int(summary.get("error_count") or 0)
    failed_codes = set(summary.get("failed_codes") or [])

    rules_out = [
        {"rule_id": r["rule_id"], "sheet": r["sheet"], "count": int(r["count"])}
        for r in summary.get("rules", [])
    ]

    # 按票聚合命中规则（sheet 名展示用），规范标识天然去重
    hits_by_code: Dict[str, Dict[str, Any]] = {}
    for r in summary.get("rules", []):
        for m in r.get("matched", []):
            entry = hits_by_code.setdefault(
                m["code"],
                {"code": m["code"], "close": m.get("close"), "hit_rules": []},
            )
            entry["hit_rules"].append(r["sheet"])

    # 未完成评估（缺数据 + 计算失败）不进默认命中列表
    incomplete = sorted(failed_codes)
    hits = [
        e
        for e in hits_by_code.values()
        if e["code"] not in failed_codes
    ]
    if match_mode == "all":
        n_rules = len(summary.get("rules", []))
        hits = [e for e in hits if len(e["hit_rules"]) >= n_rules]
    hits.sort(key=lambda e: e["code"])
    # 附股票名称（与 instruments 目录同源）：筛选结果按名称辨认，页面可搜索
    name_map: Dict[str, str] = {}
    if hits:
        try:
            from .stock_names import ensure_name_cache

            name_map = ensure_name_cache(cfg) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("筛选结果名称缓存不可用（仅显示代码）: %s", e)
    for e in hits:
        e["signal_date"] = eff_asof
        e["name"] = name_map.get(str(e["code"]).split(".")[-1], "")

    evaluated = max(0, universe_size - missing_count - len(failed_codes))
    return {
        **base,
        "status": "ok",
        "universe_size": universe_size,
        "evaluated": evaluated,
        "missing_count": missing_count,
        # 混合范围提示：多少个非股票输入被排除（0=纯股票范围）
        "excluded_count": excluded_count,
        "excluded_note": excluded_note,
        "error_count": error_count,
        "errors": summary.get("errors", []),
        "incomplete_codes": incomplete[:2000],
        "rules": rules_out,
        "hits": hits,
        "matched_count": len(hits),
        "duration_sec": summary.get("duration_sec"),
    }


# ---------------------------------------------------------------------------
# 独立异步任务容器（单工作线程 + 有界等待队列）
# ---------------------------------------------------------------------------

_SCREEN_MAX_WAITING = 5
_SCREEN_JOBS_KEEP = 30


def _screen_scope_summary(
    *, codes: Optional[Sequence[str]], scope: str
) -> str:
    if scope == "all":
        return "全部 A 股"
    n = len(_normalize_codes(codes)) if codes else 0
    return f"指定 {n} 只股票" if n else "指定股票（空）"


def _screen_run_job(ctx, job_id: str, params: Dict[str, Any]) -> None:
    jobs = ctx.bq_screen_jobs
    lock = ctx.bq_screen_lock
    cfg = ctx.cfg

    with lock:
        job = jobs.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["started_at"] = time.time()
        job["message"] = "正在筛选（加载正式 L1 日线并计算规则信号）…"

    def _prog(info: Dict[str, Any]) -> None:
        with lock:
            j = jobs.get(job_id)
            if not j:
                return
            j["progress"] = {
                "done": info.get("done", 0),
                "total": info.get("total", 0),
            }
            j["message"] = info.get("message") or j["message"]

    try:
        result = run_screen(
            cfg,
            rule_ids=params["rule_ids"],
            match_mode=params["match_mode"],
            asof=params["asof"],
            codes=params.get("codes"),
            on_progress=_prog,
        )
        with lock:
            job = jobs.get(job_id)
            if job:
                status = result.get("status")
                if status == "ok":
                    job["status"] = "done"
                    job["message"] = (
                        f"筛选完成：命中 {result.get('matched_count', 0)} 只"
                        f"（未完成评估 缺数据 {result.get('missing_count', 0)}"
                        f" / 失败 {result.get('error_count', 0)}）"
                    )
                else:
                    # no_go 不算计算失败：任务本身完成，但结果明确无数据
                    job["status"] = "done"
                    job["message"] = (
                        f"数据面不可用（{result.get('no_go_reason') or 'no_go'}），"
                        "本次未产出信号"
                    )
                job["finished_at"] = time.time()
                job["result_status"] = status
                job["asof_used"] = result.get("asof")
                job["result"] = result
    except ScreenDateNotAvailable as e:
        with lock:
            job = jobs.get(job_id)
            if job:
                job["status"] = "error"
                job["finished_at"] = time.time()
                job["error"] = str(e)
                job["suggested_asof"] = e.suggested
                job["message"] = f"筛选日期不可用：{e}"
    except Exception as e:  # noqa: BLE001
        with lock:
            job = jobs.get(job_id)
            if job:
                job["status"] = "error"
                job["finished_at"] = time.time()
                job["error"] = str(e)
                job["message"] = f"筛选失败: {e}"


def _screen_worker(ctx) -> None:
    q: "queue.Queue" = ctx.bq_screen_queue
    while True:
        job_id = q.get()
        if job_id is None:
            return
        try:
            with ctx.bq_screen_lock:
                job = ctx.bq_screen_jobs.get(job_id)
                cancelled = bool(job and job.get("cancelled"))
            if not cancelled:
                _screen_run_job(ctx, job_id, ctx.bq_screen_jobs.get(job_id, {}).get("params", {}))
        except Exception as e:  # noqa: BLE001
            logger.exception("screen worker 异常 job=%s: %s", job_id, e)
        finally:
            q.task_done()


def ensure_screen_worker(ctx) -> None:
    """懒启动单个筛选工作线程（幂等；服务重启即失效——任务容器在内存）。"""
    with ctx.bq_screen_lock:
        if ctx.bq_screen_worker_started:
            return
        import queue as _queue

        ctx.bq_screen_queue = _queue.Queue(maxsize=_SCREEN_MAX_WAITING)
        ctx.bq_screen_worker_started = True
    t = threading.Thread(
        target=_screen_worker, args=(ctx,), name="bagua-screen-worker", daemon=True
    )
    t.start()


def submit_screen_job(
    ctx,
    *,
    rule_ids: Sequence[str],
    match_mode: str,
    asof: Optional[Any],
    codes: Optional[Sequence[str]],
    scope: str = "all",
) -> Dict[str, Any]:
    """入队筛选任务；等待队列满抛 ScreenQueueFull（路由层转 429）。

    范围语义（R2-01）：``scope == "all"`` 时任务参数固定保存 ``codes=None``，
    绝不写成 ``[]``——worker 会把 ``[]`` 交给 run_screen，而 run_screen 必须把
    ``[]`` 当作「空指定范围」拒绝，两者混同会导致全市场筛选被误拒。
    指定范围在提交前就校验为空（含全是指数/ETF）并抛出 400，不占用工作线程；
    原始输入（含非股票）原样保存，由 run_screen 负责归一与排除明细统计。
    """
    ensure_screen_worker(ctx)
    if scope == "all":
        stored_codes: Optional[List[str]] = None
    else:
        raw_codes = [str(c).strip() for c in (codes or []) if str(c).strip()]
        if not raw_codes:
            raise ScreenError("指定范围为空：请先选择股票，或改用全部 A 股。")
        if not _normalize_codes(raw_codes):
            raise ScreenError(
                "指定范围内没有可筛选的 A 股股票（指数/ETF 不参与规则筛选）；"
                "请检查范围或改用全部 A 股"
            )
        stored_codes = raw_codes
    job_id = "bqscr_" + uuid.uuid4().hex[:12]
    rec: Dict[str, Any] = {
        "job_id": job_id,
        "kind": "screen",
        "status": "queued",
        "created_at": time.time(),
        "created_hm": time.strftime("%H:%M:%S", time.localtime()),
        "started_at": None,
        "finished_at": None,
        "message": "已排队",
        "params": {
            "rule_ids": list(rule_ids),
            "match_mode": match_mode,
            "asof": (None if asof in (None, "", "latest") else str(asof)),
            "codes": stored_codes,
            "scope": scope,
        },
        "scope_summary": _screen_scope_summary(codes=stored_codes, scope=scope),
        "rules_summary": "、".join(str(r) for r in rule_ids),
        "asof_used": None,
        "result_status": None,
        "progress": None,
        "result": None,
        "error": None,
        "cancelled": False,
    }
    with ctx.bq_screen_lock:
        # 清理已完成的最旧记录，执行中任务绝不清理
        if len(ctx.bq_screen_jobs) >= _SCREEN_JOBS_KEEP:
            finished = sorted(
                (
                    (k, v)
                    for k, v in ctx.bq_screen_jobs.items()
                    if v.get("status") in ("done", "error")
                ),
                key=lambda kv: float(kv[1].get("finished_at") or 0),
            )
            for k, _v in finished[: len(finished) - (_SCREEN_JOBS_KEEP - 10)]:
                ctx.bq_screen_jobs.pop(k, None)
        ctx.bq_screen_jobs[job_id] = rec
    try:
        ctx.bq_screen_queue.put_nowait(job_id)
    except queue.Full:
        with ctx.bq_screen_lock:
            ctx.bq_screen_jobs.pop(job_id, None)
        raise ScreenQueueFull(
            f"筛选任务等待队列已满（{_SCREEN_MAX_WAITING} 个），请稍后再试"
        ) from None
    return {k: v for k, v in rec.items() if k != "result"}


def _screen_public(rec: Dict[str, Any], *, with_result: bool = False) -> Dict[str, Any]:
    out = {k: v for k, v in rec.items() if k not in ("result", "cancelled")}
    if with_result:
        out["result"] = rec.get("result")
    else:
        out["has_result"] = rec.get("result") is not None
    return out


def list_screen_jobs(ctx) -> Dict[str, Any]:
    with ctx.bq_screen_lock:
        items = [_screen_public(j) for j in ctx.bq_screen_jobs.values()]
    items.sort(key=lambda j: float(j.get("created_at") or 0), reverse=True)
    return {"ok": True, "count": len(items), "jobs": items}


def get_screen_job(ctx, job_id: str, *, with_result: bool = False) -> Dict[str, Any]:
    with ctx.bq_screen_lock:
        rec = ctx.bq_screen_jobs.get(job_id)
        if not rec:
            return {}
        return _screen_public(rec, with_result=with_result)
