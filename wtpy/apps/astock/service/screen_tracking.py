# -*- coding: utf-8 -*-
"""入选股票次周跟踪服务（阶段 2，契约 §3/§4/§5）。

职责：对某周**已发布快照**的入选名单，计算跟踪自然周的逐日双口径收益
（信号收盘基准=理论口径 / 跟踪周首日开盘基准=成交假设口径）、规则级
聚合统计（胜率/覆盖率按口径分别记录）与基准（沪深300）超额，落成
不可变 track 产物 + 可变任务状态 + current 指针。

关键语义（docs/plans/auto-screen-track/contract.md 为权威全文）：

- 入场价**重读**：快照里保存的 close 仅作原始展示（入选日的原始价格
  证据），绝不参与收益计算——qfq 面的复权基准随补数/因子修订变化，
  用快照旧 close 除以重读新 close 会制造虚假收益（契约 §4 复权安全）。
- 涨停判定用**未复权价**：涨停价=prev_close*(1+pct) 后四舍五入到分，
  只有未复权价格序列才与该规则自洽；qfq 序列做过 a*px+b 仿射变换，
  乘出的"涨停价"无意义。判定元数据不足 → fill=unknown，绝不默认 ok。
- exec 口径分母剔除：limit_up_unbuyable/no_bar/unknown 的开盘成交假设
  不可成交或不可信 → ret_close_exec=null 且不进该口径分母（胜率/覆盖率
  不被不可成交样本污染）；信号口径价格完整即可计算。
- 固定读版本：计算开始捕获 data_version（OverlayState + 基准面元数据），
  批读结束从权威状态**重新读取**比对签名；不一致 → data_version_changed
  中止（产物不落盘），由 CLI 重试一次（契约 §4）。
- 产物不可变：track_{snapshot_id}_{revision}.json；同 revision 已存在 →
  校验内容并复用，绝不覆盖。pending→complete 等状态变化只改任务状态
  文件（state_{task_key}.json），不产生新产物身份（契约 §5）。
- 基准缺失：股票收益先落盘（completion=blocked_benchmark，excess=null），
  基准补齐后基准版本变化 → 新 revision 只补基准+超额。
"""

from __future__ import annotations

import calendar as _calendar
import json
import logging
from datetime import date as _date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..config import AStockConfig
from . import screen_contract as sc
from . import screen_snapshots as ss

logger = logging.getLogger(__name__)

# 收益算法版本：只关心"怎么算收益"的算法变更（口径/涨停判定/聚合公式）
# 时 bump——已有产物一字不动，新 revision 并列保留。与"结构怎么存"的
# TRACKING_SCHEMA_VERSION 分离（契约 §5）。
TRACKING_ALGO_VERSION = "1"
# schema v2（2026-09-15）：行内新增 name（结算时解析的股票名称）与
# close_week_end（窗末收盘价，UI「周五收盘价」列的直接数据源）。
# 加字段属"结构怎么存"变更 → bump schema（revision 变化 → 旧 v1 产物
# 被补偿机制重算升级，契约 §5 的新 revision 并列语义）。
TRACKING_SCHEMA_VERSION = "2"

# 基准：沪深300。契约 §3 固定基准口径；指数面数据源与单票 tushare_qfq
# 面的指数解析路径一致（SSE.IDX.000300 只会命中指数记录，不会撞同码
# 股票 000001.SZ——bagua_query.resolve_symbol 的 qualified 优先保证）。
BENCHMARK_CODE = "SSE.IDX.000300"
BENCHMARK_NAME = "沪深300"

# QFQ 面（L1 前复权）与未复权面（涨停判定）。常量集中定义避免散落
# magic string；数据面 key 语义与 bagua_query.normalize_adjust_mode 一致。
PRICE_PLANE_QFQ = "tushare_qfq"
PRICE_PLANE_RAW = "raw"

DEFAULT_DATA_MAX_DATE = 0  # max_date 无可得时的哨兵：窗口视为未结束

# ---------------------------------------------------------------------------
# 数据读取层：单批窗口 + 双价格面
# ---------------------------------------------------------------------------

# 每票一个 dict：trade_date/open/high/low/close（numpy 数组或等长序列，
# 与 DatasetRepository.load_bar_arrays 的返回结构一致）。
_SymbolArrays = Dict[str, Any]
# loader 返回 (per_symbol_arrays, meta)；meta 携带该面的 dataset_id 等
# 数据身份（供 revision 绑定），缺数据时 arrays 为空 dict、meta 含原因。
PlaneBatchLoader = Callable[
    [Sequence[str]], Tuple[Dict[str, _SymbolArrays], Dict[str, Any]]
]


def _capture_data_version(cfg: AStockConfig) -> Dict[str, Any]:
    """捕获股票行情面的当前 data_version（契约 §4：复用 OverlayState）。

    用 _get_plane_session（内部 load_overlay_state）取权威状态并转
    契约字典；文件不存在/未启用时得到全默认值的版本（注入 loader 的
    测试不落盘也照常工作）。基准面版本由调用方另记 key="benchmark"。
    """
    try:
        from .bagua_query import _get_plane_session

        session = _get_plane_session(cfg, PRICE_PLANE_QFQ)
        # session 不暴露 state：从同一权威文件重新读（load_overlay_state
        # 有 stat 缓存，重复读不增加 I/O）。
        from ..data.delta_store import load_overlay_state

        state = load_overlay_state(cfg.market_data_root)
        return sc.data_version_from_overlay_state(state)
    except FileNotFoundError:
        # 无 delta 目录的部署（blob 快照模式）：用会话仓库的实际数据面
        # 身份（formal_l1 dataset_id）兜底，保证"数据面变 → revision 变"。
        try:
            from .bagua_query import _get_plane_session

            session = _get_plane_session(cfg, PRICE_PLANE_QFQ)
        except Exception:  # noqa: BLE001 — 数据面完全不可用时留给调用方报错
            return {}
        l1 = getattr(session, "formal_l1_id", None) or ""
        return {"base_dataset_id": str(l1)} if l1 else {}
    except Exception as e:  # noqa: BLE001 — 版本捕获失败 fail-closed 而非静默
        raise RuntimeError(f"data_version 捕获失败: {e}") from e


def _default_bar_loader(
    cfg: AStockConfig,
    codes: Sequence[str],
    *,
    start_date: Optional[int],
    end_date: Optional[int],
) -> Dict[str, _SymbolArrays]:
    """正式 L1(qfq) 面批量读（每票一次窗口：信号周+跟踪周）。"""
    if not codes:
        return {}
    session = _get_plane_session_cached(cfg)
    return _batch_read(session, codes, start_date=start_date, end_date=end_date)


def _default_raw_loader(
    cfg: AStockConfig,
    codes: Sequence[str],
    *,
    start_date: Optional[int],
    end_date: Optional[int],
) -> Dict[str, _SymbolArrays]:
    """未复权面批量读（涨停判定专用，同窗口一次读齐）。"""
    if not codes:
        return {}
    from .bagua_query import _get_plane_session

    session = _get_plane_session(cfg, PRICE_PLANE_RAW)
    return _batch_read(session, codes, start_date=start_date, end_date=end_date)


def _get_plane_session_cached(cfg: AStockConfig):
    """正式 L1(qfq) 面会话（TTL 会话复用，避免重建全仓索引）。"""
    from .bagua_query import _get_plane_session

    return _get_plane_session(cfg, PRICE_PLANE_QFQ)


def _batch_read(
    session,
    codes: Sequence[str],
    *,
    start_date: Optional[int],
    end_date: Optional[int],
) -> Dict[str, _SymbolArrays]:
    """grouped-by-dataset 批量读（仿 signal_returns._load_bars_batch）。

    每票的数据面只解析一次，同一 dataset 的全部票一次 load_bar_arrays
    读齐；读窗口传 [start_date, end_date] 让仓库做列裁剪。
    """
    out: Dict[str, _SymbolArrays] = {}
    resolved: List[Any] = []
    groups: Dict[str, Tuple[Any, List[str]]] = {}
    for code in codes:
        try:
            res = session.resolve_symbol(code)
        except Exception as e:  # noqa: BLE001 — 解析失败 → 该票 no_bar（缺数据如实标注）
            logger.warning("screen_tracking 解析失败 %s: %s", code, e)
            continue
        ds = res.manifest.dataset_id
        entry = groups.setdefault(ds, (res.manifest, []))
        if res.record.symbol not in entry[1]:
            entry[1].append(res.record.symbol)
        resolved.append(res)
    arrays_by_ds: Dict[str, Dict[str, _SymbolArrays]] = {}
    for ds_id, (manifest, symbols) in groups.items():
        try:
            arrays_by_ds[ds_id] = session.repo.load_bar_arrays(
                dataset_id=ds_id, symbols=symbols,
                start_date=start_date, end_date=end_date,
            )
        except Exception as e:  # noqa: BLE001 — 一个 dataset 失败不拖垮整个批次
            logger.warning("screen_tracking 批量加载失败 %s: %s", ds_id, e)
            arrays_by_ds[ds_id] = {}
    for res in resolved:
        arr = arrays_by_ds.get(res.manifest.dataset_id, {}).get(res.record.symbol)
        if arr:
            out[res.std_code] = arr
    return out


def _default_benchmark_loader(
    cfg: AStockConfig,
    *,
    start_date: Optional[int],
    end_date: Optional[int],
) -> Dict[str, _SymbolArrays]:
    """基准（沪深300）从指数面读：raw 面会话解析指数。

    指数/ETF 行情在 tushare_none_1d_* 面（source=tushare, adj=none），
    对应 PRICE_PLANE_RAW；qfq 面只含个股前复权数据，不含指数。
    """
    from .bagua_query import _get_plane_session

    session = _get_plane_session(cfg, PRICE_PLANE_RAW)
    return _batch_read(
        session, [BENCHMARK_CODE], start_date=start_date, end_date=end_date
    )


def _slice_arrays(
    arr: Optional[_SymbolArrays],
    start: int,
    end: int,
) -> List[Dict[str, float]]:
    """把单票数组裁剪成窗口内按日升序的 dict 行（date/open/high/low/close）。

    升序是自然周窗口语义的前提（窗口首日=最小日期）；仓库可能返回
    降序（Tushare 新日期在前），这里统一翻正。
    """
    if not arr:
        return []
    import numpy as np

    td = arr.get("trade_date")
    if td is None or len(td) == 0:
        return []
    td = np.asarray(td)
    order = np.argsort(td)  # 类型统一：某些数据源 trade_date 为字符串
    td = td[order]
    if len(td) > 1 and int(td[0]) > int(td[-1]):
        td = td[::-1]
    lo = int(np.searchsorted(td, int(start), side="left"))
    hi = int(np.searchsorted(td, int(end), side="right"))
    if hi <= lo:
        return []
    cols = {}
    for k in ("open", "high", "low", "close"):
        v = arr.get(k)
        if v is None:
            return []
        v = np.asarray(v, dtype=float)[order]
        if len(td) > 1 and int(td[0]) > int(td[-1]):
            v = v[::-1]
        cols[k] = v
    out: List[Dict[str, float]] = []
    for i in range(lo, hi):
        out.append(
            {
                "date": int(td[i]),
                "open": float(cols["open"][i]),
                "high": float(cols["high"][i]),
                "low": float(cols["low"][i]),
                "close": float(cols["close"][i]),
            }
        )
    return out


def _arr_max_date(arr: Optional[_SymbolArrays]) -> int:
    """单票数组最大交易日（无数据 → 0）。"""
    if not arr:
        return DEFAULT_DATA_MAX_DATE
    import numpy as np

    td = arr.get("trade_date")
    if td is None or len(td) == 0:
        return DEFAULT_DATA_MAX_DATE
    return int(np.max(np.asarray(td)))


def surface_max_date(
    cfg: AStockConfig, bar_loader: Optional[PlaneBatchLoader] = None
) -> int:
    """数据面最新可得交易日：qfq 面正式 L1 的最大日期。

    窗口是否结束（should_recompute/是否 pending）只看数据面 max_date，
    与单票数据完整性无关（个股缺行 → no_bar/missing，不是整周没结算）。

    审查 🔴-1 修正：聚合从 min(last_dates) 改为 min(cutoff, max(last_dates))。
    原 min 会把"最长期停牌股的最后一根 K 线日期"当全市场水位——生产环境
    停牌股必然存在，导致 surface_max 恒为陈旧日期、补偿/回填全线失效、
    空仓周永久 pending。正确语义："数据实际到达"= min(cutoff 防推导越界,
    max(last_date) 证明面已覆盖到该日)——市场已覆盖到周五，只是个别票停牌。
    """
    try:
        session = _get_plane_session_cached(cfg)
        l1 = getattr(session, "formal_l1_id", None)
        if l1:
            from ..data.dataset_store import DatasetStore

            store = DatasetStore(cfg.market_data_root)
            m = store.load_manifest(l1, deep_copy=False)
            if m is not None and m.status == "ready":
                cutoff = int(m.data_cutoff_date or 0)
                last_dates = [
                    int(r.last_date) for r in m.symbols
                    if r.last_date
                ]
                # max(last_dates)=0 → 面上没有任何 bar → 0（窗口视为未结束）
                if not last_dates:
                    return DEFAULT_DATA_MAX_DATE
                covered = max(last_dates)
                # cutoff 可能被 derive 推到请求日之后，取 min 防止把
                # "数据其实没到"误判为"窗口已结束"（fail-closed 到 pending）
                if cutoff > 0:
                    return min(cutoff, covered)
                return covered
    except Exception as e:  # noqa: BLE001 — 推导失败 → 0（窗口视为未结束）
        logger.warning("screen_tracking surface_max_date 推导失败: %s", e)
    return DEFAULT_DATA_MAX_DATE


# ---------------------------------------------------------------------------
# 涨停判定（未复权价）
# ---------------------------------------------------------------------------


def _limit_fill_status(
    std_code: str,
    raw_rows: List[Dict[str, float]],
) -> str:
    """首日成交性判定（契约 §1）：ok/limit_up_unbuyable/no_bar/unknown。

    为什么用未复权价：涨停价 = prev_close*(1+pct) 四舍五入到分，这条规则
    只在未复权价格序列上自洽；qfq 序列经过 a*px+b 仿射变换，用复权价
    乘出的"涨停价"没有意义。前置 close 缺失（上市首日/窗口截断）→
    元数据不足 → unknown，绝不默认 ok。
    """
    if not raw_rows:
        return sc.FILL_NO_BAR
    first = raw_rows[0]
    prev_close = 0.0
    if len(raw_rows) >= 2:
        prev_close = float(raw_rows[1].get("close") or 0.0)
    if prev_close <= 0:
        # 无前收（一字判定无从谈起）：按 limit_rules 的边界语义是元数据
        # 不足 → unknown；理论开盘价可另展示但绝不进 exec 汇总分母。
        return sc.FILL_UNKNOWN
    from ..data.limit_rules import DefaultAShareLimitRule, LimitContext, infer_board

    ctx = LimitContext(
        std_code=std_code,
        date=int(first["date"]),
        prev_close=prev_close,
        open=float(first["open"]),
        high=float(first["high"]),
        low=float(first["low"]),
        close=float(first["close"]),
        board=infer_board(std_code),
    )
    if DefaultAShareLimitRule().is_limit_up_unbuyable(ctx):
        return sc.FILL_LIMIT_UP_UNBUYABLE
    return sc.FILL_OK


# ---------------------------------------------------------------------------
# 逐票双口径收益
# ---------------------------------------------------------------------------


def _weekday_of(d: int) -> str:
    return _calendar.day_name[_date(d // 10000, (d // 100) % 100, d % 100).weekday()]


def _drawdown_close(closes: List[float]) -> Optional[float]:
    """基于收盘序列的峰谷回撤（日 OHLC 无法确定同日高低先后，按收盘算）。"""
    if not closes:
        return None
    peak = closes[0]
    max_dd = 0.0
    for c in closes:
        peak = max(peak, c)
        if peak > 0:
            max_dd = min(max_dd, c / peak - 1.0)
    return max_dd if max_dd < 0 else 0.0


def _compute_ticket_returns(
    *,
    code: str,
    rule_id: str,
    signal_date: int,
    track_first_day: int,
    qfq_rows: List[Dict[str, float]],
    raw_rows: List[Dict[str, float]],
) -> Dict[str, Any]:
    """单票逐日双口径收益 + 成交性判定（契约 §3）。

    入场价全部来自重读的 qfq bars：entry_close_signal=信号日收盘（重读），
    entry_open_week=跟踪周首日开盘。首日判定用**日历首日**（不是第一根
    出现的 bar）：首日无 K 线 → no_bar（停牌与缺数据不区分，如实标注），
    该票不进 exec 分母；停牌几日后复牌的票 sig 口径照算（信号日收盘
    锚点完整），复牌首根 bar 的开盘只进 theoretical_open_ret 展示，
    绝不冒充"首日开盘"。
    """
    sig_close = None
    for r in qfq_rows:
        if int(r["date"]) == int(signal_date):
            sig_close = float(r["close"])
            break
    week_rows = [r for r in qfq_rows if int(r["date"]) > int(signal_date)]

    if sig_close is None or not week_rows:
        # 信号日收盘或跟踪周首根 K 线缺失：无法建立任何一个口径的入场锚
        return {
            "code": code,
            "rule_id": rule_id,
            "name": None,                              # 结算层回填（缺名留空）
            "entry_close_signal": None,
            "entry_open_week": None,
            "close_week_end": None,                    # 窗末收盘价（无行情 → None）
            "fill_status": sc.FILL_NO_BAR,
            "daily": [],
            "max_gain_sig": None,
            "max_gain_sig_date": None,
            "max_gain_exec": None,
            "max_gain_exec_date": None,
            "min_low_ret_sig": None,
            "drawdown_close_sig": None,
            "ret_close_sig": None,
            "ret_close_exec": None,
            "theoretical_open_ret": None,
            "bench_ret_sig": None,
            "bench_ret_exec": None,
            "excess_sig": None,
            "excess_exec": None,
            "status": "no_bar",
        }

    first = week_rows[0]
    first_d = int(first["date"])
    # 首日语义 = 跟踪周**日历首日**：日历首日无 K 线（停牌/缺数据）→
    # exec 口径 no_bar（entry_open 缺，不进 exec 分母）；但 sig 口径照算
    # ——信号日收盘锚完整、周内复牌后的收盘序列完整（契约 §3：价格完整
    # 即可计算，审查 🔴-2 修正：不得整票剔出 sig 分母）。复牌首根 bar 的
    # 开盘只进 theoretical_open_ret 展示，绝不冒充"首日开盘"。
    if first_d != int(track_first_day):
        reopen_open = float(first["open"])
        entry_open = None  # 无首日开盘锚：exec 全链 None
        fill = sc.FILL_NO_BAR
        sig_basis_open = reopen_open  # 理论展示口径用复牌首根开盘
    else:
        entry_open = float(first["open"])
        # 成交性判定需要 raw 面两根 K 线：跟踪首日 + 它的前一根（前收）。
        # 前 K 线通常是信号日收盘（周五收盘后周一开盘），但若窗口截断/数据
        # 缺行，按日期序取首日之前最近一根，不假设连续。
        fill = sc.FILL_UNKNOWN  # raw 数据不足时保持 unknown（不默认 ok）
        sig_basis_open = entry_open
        raw_in_week = [r for r in raw_rows if int(r["date"]) > int(signal_date)]
        if raw_in_week:
            raw_on_first = [r for r in raw_in_week if int(r["date"]) == first_d]
            raw_before = [r for r in raw_rows if int(r["date"]) < first_d]
            if raw_on_first and raw_before:
                fill = _limit_fill_status(code, [raw_on_first[0], raw_before[-1]])

    daily: List[Dict[str, Any]] = []
    max_gain_sig: Optional[float] = None
    max_gain_sig_date: Optional[int] = None
    max_gain_exec: Optional[float] = None
    max_gain_exec_date: Optional[int] = None
    min_low_ret: Optional[float] = None
    closes: List[float] = []
    last_close: Optional[float] = None
    for r in week_rows:
        c = float(r["close"])
        closes.append(c)
        last_close = c
        ret_sig = (c / sig_close - 1.0) if sig_close > 0 else None
        # entry_open 可为 None（首日停牌分支）：exec 序列整列 None
        ret_exec = (
            (c / entry_open - 1.0)
            if entry_open is not None and entry_open > 0 else None
        )
        hi = float(r["high"])
        ret_high_sig = (hi / sig_close - 1.0) if sig_close > 0 else None
        daily.append(
            {
                "date": int(r["date"]),
                "close": c,
                "ret_vs_signal_close": (round(ret_sig, 6) if ret_sig is not None else None),
                "ret_vs_week_open": (round(ret_exec, 6) if ret_exec is not None else None),
                "high": hi,
                "ret_high_vs_signal": (round(ret_high_sig, 6) if ret_high_sig is not None else None),
            }
        )
        # 见顶日：并列最高取首个日期（契约 §3 描述统计语义）
        if ret_sig is not None and (max_gain_sig is None or ret_sig > max_gain_sig):
            max_gain_sig = ret_sig
            max_gain_sig_date = int(r["date"])
        if ret_exec is not None and (max_gain_exec is None or ret_exec > max_gain_exec):
            max_gain_exec = ret_exec
            max_gain_exec_date = int(r["date"])
        lo = float(r["low"])
        low_ret = (lo / sig_close - 1.0) if sig_close > 0 else None
        if low_ret is not None and (min_low_ret is None or low_ret < min_low_ret):
            min_low_ret = low_ret  # 最低相对入场收益：可为正值（低点高于入场价）

    ret_close_sig = (
        (last_close / sig_close - 1.0) if last_close is not None and sig_close > 0 else None
    )
    # exec 口径收益只在成交性可信（fill=ok）时计算：limit_up_unbuyable 的
    # 首日开盘价买不进、unknown 的判定不可信，混入会污染"已过滤成交
    # 限制"的汇总语义。理论开盘收益单独展示（不进分母）——停牌复牌票
    # 用复牌首根开盘（sig_basis_open），绝冒充"首日开盘"。
    if fill == sc.FILL_OK and entry_open is not None and entry_open > 0 and last_close is not None:
        ret_close_exec = last_close / entry_open - 1.0
        theoretical_open_ret = None
    else:
        ret_close_exec = None
        theoretical_open_ret = (
            (last_close / sig_basis_open - 1.0)
            if sig_basis_open is not None and sig_basis_open > 0 and last_close is not None
            else None
        )

    return {
        "code": code,
        "rule_id": rule_id,
        "name": None,                              # 结算层回填（缺名留空）
        "entry_close_signal": sig_close,           # 重读的信号日收盘（qfq）
        "entry_open_week": entry_open,             # 重读的跟踪周首日开盘（qfq）
        # 窗末（周五；节假日短周=当周最后交易日）收盘价：UI「周五收盘价」列
        # 的直接数据源，不再让前端从 daily 末行反推（短周/缺行时更准确）
        "close_week_end": last_close,
        "fill_status": fill,
        "daily": daily,
        "max_gain_sig": max_gain_sig,
        "max_gain_sig_date": max_gain_sig_date,
        "max_gain_exec": max_gain_exec,
        "max_gain_exec_date": max_gain_exec_date,
        "min_low_ret_sig": min_low_ret,
        "drawdown_close_sig": _drawdown_close(closes),
        "ret_close_sig": ret_close_sig,            # 窗末收盘/信号收盘-1
        "ret_close_exec": ret_close_exec,          # 窗末收盘/首日开盘-1（不可成交=null）
        "theoretical_open_ret": theoretical_open_ret,
        "bench_ret_sig": None,
        "bench_ret_exec": None,
        "excess_sig": None,
        "excess_exec": None,
        "status": "ok",
    }


# ---------------------------------------------------------------------------
# 规则级聚合
# ---------------------------------------------------------------------------


def _aggregate_rule(
    rule_id: str,
    rows: List[Dict[str, Any]],
    *,
    benchmark_ready: bool,
) -> Dict[str, Any]:
    """规则级统计（契约 §3 分母）：胜率分母=对应口径 valid 数，空仓→null。"""
    selected = len(rows)
    valid_sig = [
        r for r in rows
        if r["ret_close_sig"] is not None and r["status"] == "ok"
    ]
    valid_exec = [
        r for r in rows
        if r["ret_close_exec"] is not None and r["status"] == "ok"
    ]
    excess_valid = [
        r for r in rows if r.get("excess_sig") is not None and r["status"] == "ok"
    ]

    def _win_rate(vals: List[float]) -> Optional[float]:
        if not vals:
            return None  # 空仓/无有效样本 → null（不是 0）
        return round(sum(1 for v in vals if v > 0) / len(vals), 6)

    def _mean(vals: List[float]) -> Optional[float]:
        return round(sum(vals) / len(vals), 6) if vals else None

    # 见顶日分布：max_gain_sig_date 的星期分布（描述统计，不可推导卖出规则）
    weekday_dist: Dict[str, int] = {}
    for r in valid_sig:
        d = r.get("max_gain_sig_date")
        if d:
            wd = _weekday_of(int(d))
            weekday_dist[wd] = weekday_dist.get(wd, 0) + 1
    # 平均回吐：max_gain_sig - ret_close_sig 的均值（仅 valid_sig 票）
    givebacks = [
        (r["max_gain_sig"] - r["ret_close_sig"])
        for r in valid_sig
        if r.get("max_gain_sig") is not None and r.get("ret_close_sig") is not None
    ]

    return {
        "rule_id": rule_id,
        "selected_count": selected,
        "valid_sig_count": len(valid_sig),
        "valid_exec_count": len(valid_exec),
        "pending_count": sum(1 for r in rows if r["status"] == "pending"),
        "missing_count": sum(1 for r in rows if r["status"] == "no_bar"),
        "unbuyable_count": sum(
            1 for r in rows if r["fill_status"] == sc.FILL_LIMIT_UP_UNBUYABLE
        ),
        "unknown_count": sum(
            1 for r in rows
            if r["fill_status"] == sc.FILL_UNKNOWN and r["status"] == "ok"
        ),
        "win_rate_sig": _win_rate([r["ret_close_sig"] for r in valid_sig]),
        "win_rate_exec": _win_rate([r["ret_close_exec"] for r in valid_exec]),
        # 字段名对齐阶段 3 读取方（api_routes/tracking.py 的周聚合入口）
        "mean_ret_close_sig": _mean([r["ret_close_sig"] for r in valid_sig]),
        "mean_ret_close_exec": _mean([r["ret_close_exec"] for r in valid_exec]),
        "excess_valid_count": len(excess_valid),
        "win_rate_excess": _win_rate([r["excess_sig"] for r in excess_valid]),
        "mean_excess_sig": _mean([r["excess_sig"] for r in excess_valid]),
        "coverage": sc.coverage_by_basis(
            selected, len(valid_sig), len(valid_exec), len(excess_valid)
        ),
        "max_gain_weekday_dist": weekday_dist,
        # 见顶/回吐均值：契约 §10 名（mean_max_gain_sig / mean_giveback_sig）
        # 与阶段 2 早期实现名（mean_giveback）同时给出——契约与既有读取方
        # （导出/前端已分别按两种名字写过兼容读取）都不被破坏。
        "mean_max_gain_sig": _mean([r["max_gain_sig"] for r in valid_sig]),
        "mean_giveback_sig": _mean(givebacks),
        "mean_giveback": _mean(givebacks),
    }


# ---------------------------------------------------------------------------
# 任务身份
# ---------------------------------------------------------------------------


def tracking_task_key(snapshot_id: str) -> str:
    """任务身份 key：同快照的跟踪是同一任务（pending 重试、状态复用）。"""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(snapshot_id))
    return f"track_{safe}"


def week_task_key(week_id: int) -> str:
    """周身份 key：CLI 抢锁失败时的待办 key（track_{week_id}）。

    与 tracking_task_key(snapshot_id) 是两个体系：CLI 在持锁前只知道
    week_id（快照 id 要进锁后才解析），所以锁待办按周记；TaskState 按
    快照记（契约 §5 任务身份）。终态清欠账时两个 key 都清（见
    _clear_terminal_pending），保证手动补跑成功后锁待办不残留。
    """
    return f"track_{int(week_id)}"


def _clear_terminal_pending(
    storage_root: Path, *, task_key: str, week_id: int
) -> None:
    """终态清欠账：锁待办（track_{week_id}）与任务身份 key 都清。"""
    for key in {task_key, week_task_key(week_id)}:
        try:
            sc.clear_pending_job(storage_root, key)
        except Exception as e:  # noqa: BLE001 — 清欠账失败不影响结算结果
            logger.warning("screen_tracking 清欠账失败 %s: %s", key, e)


# ---------------------------------------------------------------------------
# 产物落盘
# ---------------------------------------------------------------------------


def _write_track_product(
    storage_root: Path, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """写不可变 track 产物（O_EXCL）+ current 指针 + 返回复用信息。

    已存在 → **校验内容并复用，绝不覆盖**（pending→complete 的状态变化
    不产生新产物身份）；内容不一致是编码/算法级事故，抛错人工排查。
    """
    root = Path(storage_root)
    sid = str(payload["snapshot_id"])
    rev = str(payload["tracking_revision_id"])
    path = sc.track_path(root, sid, rev)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 — 损坏文件不静默覆盖
            raise RuntimeError(f"track 产物损坏（不覆盖，人工排查）: {path}: {e}")
        # 比较剔除时间戳后的计算内容：同 revision 两次计算结果必须一致
        def _core(d: Dict[str, Any]) -> str:
            c = {k: v for k, v in d.items() if k != "generated_at"}
            return json.dumps(c, ensure_ascii=False, sort_keys=True)

        if _core(existing) != _core(payload):
            raise RuntimeError(
                f"同 tracking_revision_id 但内容不一致: {path}（不覆盖）"
            )
        return {"path": str(path), "reused": True}
    sc.create_snapshot_file_exclusive(path, payload)
    return {"path": str(path), "reused": False}


def _write_current_pointer(
    storage_root: Path, snapshot_id: str, tracking_revision_id: str
) -> Path:
    """current 指针（可变，仅指向；写失败不阻塞产物——可重建）。"""
    root = Path(storage_root)
    p = sc.track_current_path(root, snapshot_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "snapshot_id": snapshot_id,
                    "tracking_revision_id": tracking_revision_id,
                    "schema_version": TRACKING_SCHEMA_VERSION,
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        import os

        os.replace(str(tmp), str(p))
    except Exception as e:  # noqa: BLE001 — 指针可重建，失败只记日志
        logger.warning("track current 指针写入失败: %s", e)
    return p


# ---------------------------------------------------------------------------
# 核心入口
# ---------------------------------------------------------------------------


def compute_weekly_tracking(
    cfg: AStockConfig,
    week_id: int,
    *,
    run_kind: str = "weekly_chain",
    force: bool = False,
    bar_loader: Optional[PlaneBatchLoader] = None,
    raw_loader: Optional[PlaneBatchLoader] = None,
    benchmark_loader: Optional[PlaneBatchLoader] = None,
    calendar=None,
    algo_version: Optional[str] = None,
) -> Dict[str, Any]:
    """对某信号周的发布快照计算次周跟踪（契约 §3/§4/§5）。

    返回 dict 至少含：completion（契约 TRACK_*）、week_id、snapshot_id、
    tracking_revision_id（有产物时）、task_key、每个非终态/异常分支的
    reason。异常不吞：计算错误抛出，由 CLI 按退出码 1 处理。
    """
    algo_version = str(algo_version or TRACKING_ALGO_VERSION)
    storage_root = Path(cfg.storage_root)
    week_id = int(week_id)

    # ① 该周发布快照：统计/跟踪只认 published 指针（契约 §0）
    snap = ss.load_published_snapshot_for_week(cfg, week_id)
    if snap is None:
        return {
            "completion": "no_snapshot",
            "week_id": week_id,
            "reason": "no_published_snapshot",
        }
    snapshot_id = str(snap["snapshot_id"])
    signal_date = int(snap.get("asof") or week_id)
    task_key = tracking_task_key(snapshot_id)

    # ② 日历 + 自然周窗口（契约 §2）
    if calendar is None:
        calendar = _load_calendar_or_none(cfg)
    if calendar is None:
        raise ValueError(
            "交易日历不可用（calendar.json 缺失且无法推导）："
            "跟踪窗口对齐需要日历，请先完成行情数据同步"
        )
    win = sc.natural_week_window(list(calendar.dates), signal_date)
    if win.get("anomaly"):
        # 信号日非周内最后交易日等用法错误：fail-closed，绝不静默换锚
        raise ValueError(f"自然周窗口异常: {win['anomaly']} (signal={signal_date})")
    if win.get("no_trading_week"):
        # 必须区分两种情况（真实数据实测修正，2026-09-15）：
        # ① 日历**尚未覆盖**跟踪周（行情只到信号周）→ 窗口未结束 → pending
        #    （到期重算）；若误判成 no_trading_week（终态）会短路补偿，
        #    该周**永远不再结算**——正常周五链遇行情滞后即踩此坑。
        # ② 日历已覆盖该周但整周无交易日（如春节）→ 合法终态，写 TaskState
        #    短路，补偿不反复重算。
        next_monday = _next_civil_week_monday(signal_date)
        cal_max = int(max(calendar.dates)) if calendar.dates else 0
        if next_monday and cal_max < next_monday:
            return {
                "completion": sc.TRACK_PENDING,
                "week_id": week_id,
                "snapshot_id": snapshot_id,
                "signal_date": signal_date,
                "task_key": task_key,
                "reason": "track_week_not_covered_yet",
                "calendar_max": cal_max,
                "next_week_monday": next_monday,
            }
        state = sc.TaskState(
            task_key=task_key,
            completion=sc.TRACK_NO_TRADING_WEEK,
            attempts=1,
            extra={"week_id": week_id, "signal_date": signal_date},
        )
        sc.save_task_state(storage_root, state)
        # 终态（整周休市）同样清抢锁欠账：语义与 complete 路径一致，
        # 不残留锁待办等退避到期白跑一轮
        _clear_terminal_pending(storage_root, task_key=task_key, week_id=week_id)
        return {
            "completion": sc.TRACK_NO_TRADING_WEEK,
            "week_id": week_id,
            "snapshot_id": snapshot_id,
            "signal_date": signal_date,
            "task_key": task_key,
            "short_week": bool(win.get("short_week")),
            "reason": "track_week_no_trading_days",
        }
    track_week = win["track_week"]
    assert track_week is not None  # no_trading_week 已排除
    track_dates: List[int] = list(win["track_week_dates"])
    week_start, week_end = int(track_week[0]), int(track_week[1])

    # ③ 计划量：每规则×每入选票
    plan: List[Tuple[str, Dict[str, Any]]] = []
    for rule in snap.get("rules") or []:
        rid = str(rule.get("rule_id"))
        for m in rule.get("matched") or []:
            plan.append((rid, {"code": str(m.get("code")), "close": m.get("close")}))
    codes = sorted({p[1]["code"] for p in plan})
    read_start = int(win["signal_week"][0])
    read_end = week_end

    # ④ 固定读版本：计算开始捕获（契约 §4）
    bars_dv = _capture_data_version(cfg)

    # ⑤ 批量读（qfq 收益面 + raw 涨停判定面 + 基准指数面）
    def _read_plane(loader: Optional[PlaneBatchLoader], default_fn):
        """loader 注入返回 (arrays, meta) 元组，默认实现返回 arrays：
        两种签名归一成 (arrays, meta)。"""
        if loader is not None:
            ret = loader(codes)
            if isinstance(ret, tuple):
                return ret[0], ret[1]
            return ret, {}
        return default_fn(cfg, codes, start_date=read_start, end_date=read_end), {}

    try:
        qfq_arrays, _qfq_meta = _read_plane(bar_loader, _default_bar_loader)
        if raw_loader is not None:
            ret = raw_loader(codes)
            raw_arrays = ret[0] if isinstance(ret, tuple) else ret
        else:
            raw_arrays = _default_raw_loader(
                cfg, codes, start_date=read_start, end_date=read_end
            )
    finally:
        # 结束后重读权威状态比对：防批读期间数据面被增量更新（EOD 链并发）
        after_bars_dv = _capture_data_version(cfg)
    if sc.data_version_signature(after_bars_dv) != sc.data_version_signature(bars_dv):
        # 不落产物，CLI 自动重试一次（重试时捕获的就是新版本）
        return {
            "completion": sc.TRACK_DATA_VERSION_CHANGED,
            "week_id": week_id,
            "snapshot_id": snapshot_id,
            "task_key": task_key,
            "reason": "bars_data_version_changed_during_read",
        }

    bench_arrays: Dict[str, _SymbolArrays] = {}
    bench_meta: Dict[str, Any] = {}
    try:
        if benchmark_loader is not None:
            ret = benchmark_loader([BENCHMARK_CODE])
            if isinstance(ret, tuple):
                bench_arrays, bench_meta = ret[0], (ret[1] or {})
            else:
                bench_arrays, bench_meta = ret, {}
        else:
            bench_arrays = _default_benchmark_loader(
                cfg, start_date=read_start, end_date=read_end
            )
    except Exception as e:  # noqa: BLE001 — 基准缺不阻塞股票收益（契约 §3）
        logger.warning("screen_tracking 基准加载失败: %s", e)
        bench_arrays = {}
    bench_row = bench_arrays.get(BENCHMARK_CODE)
    if not bench_row:
        bench_meta = dict(bench_meta)
        bench_meta.setdefault("reason", "benchmark_data_missing")
    bench_sig_close: Optional[float] = None
    bench_first_open: Optional[float] = None
    bench_last_close: Optional[float] = None
    if bench_row:
        rows = _slice_arrays(bench_row, read_start, week_end)
        for r in rows:
            if int(r["date"]) == signal_date:
                bench_sig_close = float(r["close"])
        after_sig = [r for r in rows if int(r["date"]) > int(signal_date)]
        if after_sig:
            bench_first_open = float(after_sig[0]["open"])
            bench_last_close = float(after_sig[-1]["close"])
    # 基准可用 = 两口径锚点齐全 **且精确落在窗口边界**（审查 🔴-3）：
    # after_sig[0]/[-1] 是"窗口内可得的首末根"，不是日历首末日。指数面
    # 滞后（只到周四）或缺首日时，用它算出的 excess 是口径错配的假数字
    # ——必须 blocked（股票收益保留，excess=null），绝不静默产出。
    # reason 落进产物（审查 🟡-4：UI 需要区分缺数/锚不全/边界错配）。
    bench_meta = dict(bench_meta)
    benchmark_ready = False
    if bench_row and after_sig:
        first_d = int(after_sig[0]["date"])
        last_d = int(after_sig[-1]["date"])
        anchors_ok = (
            bench_sig_close is not None
            and bench_first_open is not None
            and bench_last_close is not None
            and first_d == int(week_start)
            and last_d == int(week_end)
        )
        if anchors_ok:
            benchmark_ready = True
        else:
            bench_meta.setdefault(
                "reason",
                "boundary_mismatch" if bench_sig_close is not None
                else "anchors_incomplete",
            )
    bench_ret_sig = (
        (bench_last_close / bench_sig_close - 1.0)
        if benchmark_ready and bench_sig_close
        else None
    )
    bench_ret_exec = (
        (bench_last_close / bench_first_open - 1.0)
        if benchmark_ready and bench_first_open
        else None
    )

    # 基准面版本单独记录 key="benchmark"（契约 §4：指数补数后股票面没变
    # 也必须使超额重算失效 → revision 变化 → 新产物）
    bench_dv: Dict[str, Any] = dict(bench_meta.get("data_version") or {})
    bench_dv.setdefault(
        "benchmark",
        _benchmark_surface_id(cfg, bench_meta) or "unavailable",
    )

    # ⑥ 逐票计算 + 窗口状态
    # 窗口结束只看数据面（注入 loader 的空选周也要能判终态）：默认实现推
    # 正式 L1 max_date，可能拿不到（无数据面）→ 0；注入 loader 时面内
    # bars 的 max_date 也可作证据。两者都拿不到时以"基准可得"兜底推断
    # （基准指数与股票同市场日历，指数到了周尾即窗口已过）。
    surface_max = surface_max_date(cfg)
    arrays_max = max([_arr_max_date(a) for a in qfq_arrays.values()] + [0])
    data_max = max(surface_max, arrays_max)
    if data_max <= 0 and benchmark_ready and bench_last_close is not None:
        # 基准窗末收盘可得：至少指数面已覆盖周尾 → 视为窗口结束
        data_max = week_end
    window_ended = data_max >= week_end
    # 名称：结算时一次解析、按票缓存（同票多规则不重复解析）。来源与
    # bagua 查询同源（stock_names 的 universe/周报/Tushare 元数据缓存链），
    # 解析失败留空串——读取方按缺名如实显示，绝不伪造。名称口径是**结算
    # 时**的当前名称；回填周的历史名称/ST 偏差由回填免责文案覆盖（契约 §9）。
    name_cache: Dict[str, str] = {}

    def _name_of(c: str) -> str:
        if c not in name_cache:
            try:
                # 统一走 normalize_stock_code 取 6 位码：产物 code 可能是
                # SSE.STK.600033 或 SZSE.000003.SZ 两种形态，不能靠 split(".")
                from ..forecast.name_norm import normalize_stock_code
                from .stock_names import resolve_stock_name

                name_cache[c] = str(
                    resolve_stock_name(
                        cfg, normalize_stock_code(c), std_code=str(c)
                    )
                    or ""
                )
            except Exception as e:  # noqa: BLE001 — 缺名不阻塞结算
                logger.warning("screen_tracking 名称解析失败 %s: %s", c, e)
                name_cache[c] = ""
        return name_cache[c]

    rows_out: List[Dict[str, Any]] = []
    for rid, m in plan:
        code = m["code"]
        qfq_rows = _slice_arrays(qfq_arrays.get(code), read_start, week_end)
        raw_rows = _slice_arrays(raw_arrays.get(code), read_start, week_end)
        row = _compute_ticket_returns(
            code=code,
            rule_id=rid,
            signal_date=signal_date,
            track_first_day=week_start,
            qfq_rows=qfq_rows,
            raw_rows=raw_rows,
        )
        row["name"] = _name_of(code)
        if benchmark_ready:
            # 超额=股票收益-基准收益（同口径配对），基准缺 → null
            if row["ret_close_sig"] is not None:
                row["excess_sig"] = row["ret_close_sig"] - bench_ret_sig
            if row["ret_close_exec"] is not None:
                row["excess_exec"] = row["ret_close_exec"] - bench_ret_exec
        row["bench_ret_sig"] = bench_ret_sig
        row["bench_ret_exec"] = bench_ret_exec
        if row["status"] == "ok" and not window_ended:
            row["status"] = "pending"  # 窗口未结束：到期重算（不是 missing）
        rows_out.append(row)

    # ⑦ 规则级聚合
    rule_stats: List[Dict[str, Any]] = []
    by_rule: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows_out:
        by_rule.setdefault(str(r["rule_id"]), []).append(r)
    rule_fps = dict(snap.get("rule_fingerprints") or {})
    for rule in snap.get("rules") or []:
        rid = str(rule.get("rule_id"))
        rows = by_rule.get(rid, [])
        stat = _aggregate_rule(rid, rows, benchmark_ready=benchmark_ready)
        stat["rule_fingerprint"] = rule_fps.get(rid, "")
        stat["sheet"] = rule.get("sheet", "")
        rule_stats.append(stat)

    total_selected = len(rows_out)
    total_valid_sig = sum(s["valid_sig_count"] for s in rule_stats)
    total_valid_exec = sum(s["valid_exec_count"] for s in rule_stats)
    total_excess_valid = sum(s["excess_valid_count"] for s in rule_stats)
    overall_coverage = sc.coverage_by_basis(
        total_selected, total_valid_sig, total_valid_exec, total_excess_valid
    )

    # ⑧ 产物 + revision
    rev = sc.tracking_revision_id(
        snapshot_id, bars_dv, bench_dv, algo_version, TRACKING_SCHEMA_VERSION
    )
    # 完成状态随产物落盘（读取方 UI 用"产物里的状态"而非任务状态文件，
    # 因为 current 指针指向的本 revision 产物是自洽的结算快照）。
    if not window_ended:
        completion_in_product = sc.TRACK_PENDING
    elif not benchmark_ready:
        completion_in_product = sc.TRACK_BLOCKED_BENCHMARK
    else:
        completion_in_product = sc.TRACK_COMPLETE
    payload = {
        "schema_version": TRACKING_SCHEMA_VERSION,
        "tracking_revision_id": rev,
        "algo_version": algo_version,
        "completion": completion_in_product,
        "snapshot_id": snapshot_id,
        "run_kind": run_kind if run_kind in sc.RUN_KINDS else "weekly_chain",
        "week_id": week_id,
        "asof": signal_date,
        "signal_date": signal_date,
        "signal_week": [int(win["signal_week"][0]), int(win["signal_week"][1])],
        "track_week": [week_start, week_end],
        "track_week_dates": track_dates,
        "short_week": bool(win.get("short_week")),
        "window_ended": window_ended,
        "data_version": bars_dv,                 # 契约 §4：产物记录价格面身份
        "benchmark_data_version": bench_dv,
        "entry_price_used": "reread_signal_close_and_week_first_open",
        "price_plane": PRICE_PLANE_QFQ,           # 收益面；涨停判定面在下方
        "limit_up_plane": PRICE_PLANE_RAW,       # 涨停判定用未复权价
        "limit_boundary_note": "limit_rules BOUNDARY_NOTE 适用（未知 ST/板块按主板 10%）",
        "benchmark": {
            "code": BENCHMARK_CODE,
            "name": BENCHMARK_NAME,
            "ret_sig": bench_ret_sig,
            "ret_exec": bench_ret_exec,
            "available": benchmark_ready,
            # 缺数原因落盘（审查 🟡-4）：UI 区分 benchmark_data_missing /
            # anchors_incomplete / boundary_mismatch
            "reason": (bench_meta.get("reason") or None),
        },
        "selected_count": total_selected,
        "coverage": overall_coverage,
        "rules": rule_stats,
        # 规则聚合的稳定别名：阶段 3 读取方（api_routes/tracking.py 的
        # L0/L1 周聚合入口）按 rule_aggregates 取数。与 rules 内聚合是
        # 同一份对象（JSON 序列化各自独立拷贝），双名只为读取方兼容。
        "rule_aggregates": rule_stats,
        "rows": rows_out,
        "generated_at": _now_str(),
    }

    written = _write_track_product(storage_root, payload)
    _write_current_pointer(storage_root, snapshot_id, rev)

    # ⑨ 任务状态（契约 §5：状态变化不产生新产物身份）
    completion = completion_in_product
    state = sc.TaskState(
        task_key=task_key,
        completion=completion,
        attempts=1,
        tracking_revision_id=rev,
        coverage=overall_coverage,
        extra={
            "week_id": week_id,
            "snapshot_id": snapshot_id,
            "algo_version": algo_version,
            "reused_product": bool(written.get("reused")),
        },
    )
    sc.save_task_state(storage_root, state)
    # 终态成功清欠账（此前 heavy-job 抢锁失败留下的待办；锁待办按周记
    # track_{week_id}、TaskState 按快照记——两个 key 都清，避免残留）
    if completion in sc.TRACK_TERMINAL_STATUSES:
        _clear_terminal_pending(
            storage_root, task_key=task_key, week_id=week_id
        )

    return {
        "completion": completion,
        "week_id": week_id,
        "snapshot_id": snapshot_id,
        "signal_date": signal_date,
        "task_key": task_key,
        "tracking_revision_id": rev,
        "window_ended": window_ended,
        "benchmark_available": benchmark_ready,
        "selected_count": total_selected,
        "coverage": overall_coverage,
        "product_path": written["path"],
        "product_reused": bool(written.get("reused")),
        "short_week": bool(win.get("short_week")),
    }


def _benchmark_surface_id(cfg: AStockConfig, bench_meta: Dict[str, Any]) -> str:
    """基准面数据身份（优先 loader meta 的 dataset_id，回退指数面解析）。"""
    ds = str(bench_meta.get("dataset_id") or "")
    if ds:
        return ds
    try:
        from .bagua_query import _get_plane_session

        session = _get_plane_session(cfg, PRICE_PLANE_RAW)
        res = session.resolve_symbol(BENCHMARK_CODE)
        return str(res.manifest.dataset_id)
    except Exception:  # noqa: BLE001 — 基准不可用是 blocked 分支的输入
        return ""


def _next_civil_week_monday(signal_date: int) -> int:
    """信号日所在 ISO 周的下一个 civil 周周一（YYYYMMDD）。

    用于区分"跟踪周整周休市"（calendar 已覆盖该周）与"日历还没到那一周"
    （行情只到信号周 → pending，到期再算）。返回 0 表示日期非法。
    """
    try:
        from datetime import timedelta

        d = _date(
            int(signal_date) // 10000,
            (int(signal_date) // 100) % 100,
            int(signal_date) % 100,
        )
        iso = d.isocalendar()
        monday = _date.fromisocalendar(int(iso[0]), int(iso[1]), 1)
        nxt = monday + timedelta(days=7)
        return nxt.year * 10000 + nxt.month * 100 + nxt.day
    except Exception:  # noqa: BLE001
        return 0


def _load_calendar_or_none(cfg: AStockConfig):
    """日历加载：calendar.json 优先（**须覆盖数据面**），否则从数据集推导。

    与 backtest.py 的三级回退（calendar.json → TDX → 数据集推导）保持同
    思路；跟踪不依赖 TDX，只加数据集推导一层。

    真实数据实测发现（2026-09-15）：仓库里遗留的 ``storage/astock/calendar.json``
    停在 20260717（TDX 时代产物），而数据面已到 20260911——若无条件优先
    它，真实环境所有 track 都会以 signal_date_not_trading_day 失败。
    故 calendar.json 必须**覆盖数据面 cutoff** 才采用，否则落回数据集推导
    （数据集日历是当前且不可变的），并告警提示该文件已过期。

    第二层修正（同日实测）：overlay_v1 架构下 **base 数据集是周期性全量
    快照，近期行情只在 delta**（本机 base 末日 20260814、delta 到 20260911），
    只读 base 推导出的日历同样覆盖不到信号日。故数据集分支改为
    **base 日历 ∪ delta 可见交易日**——两者并集才是 L1 面的真实覆盖。
    """
    cutoff = _data_face_cutoff(cfg)
    try:
        from ..data.calendar import TradeCalendar

        p = Path(cfg.calendar_path)
        if p.exists():
            cal = TradeCalendar.load(p)
            if cal.dates:
                if cutoff <= 0 or int(cal.dates[-1]) >= int(cutoff):
                    return cal
                logger.warning(
                    "screen_tracking calendar.json 已过期（末日 %s < 数据面 %s），"
                    "改用数据集推导日历: %s",
                    cal.dates[-1], cutoff, p,
                )
    except Exception as e:  # noqa: BLE001
        logger.warning("screen_tracking 日历加载失败: %s", e)
    try:
        from ..data.calendar import TradeCalendar, build_calendar_from_dataset
        from ..data.dataset_store import DatasetStore
        from ..data.tushare_product import select_tushare_base

        store = DatasetStore(cfg.market_data_root)
        base = select_tushare_base(store)
        if base is not None:
            cal, _meta = build_calendar_from_dataset(
                store, base.dataset_id,
                cache_dir=Path(cfg.storage_root) / "calendars",
            )
            if cal.dates:
                delta_dates = _delta_visible_trade_dates(cfg)
                if delta_dates:
                    merged = sorted(set(cal.dates) | set(delta_dates))
                    if int(merged[-1]) > int(cal.dates[-1]):
                        logger.info(
                            "screen_tracking 日历并入 delta 交易日（base 末日 %s → %s）",
                            cal.dates[-1], merged[-1],
                        )
                    return TradeCalendar(merged)
                return cal
    except Exception as e:  # noqa: BLE001
        logger.warning("screen_tracking 数据面日历推导失败: %s", e)
    return None


def _delta_visible_trade_dates(cfg: AStockConfig) -> List[int]:
    """delta 存储可见的交易日（overlay_v1 的近期增量；无 overlay → 空）。

    只读 SQL（DISTINCT trade_date），成本毫秒级；失败一律返回空
    （日历退回 base 版本，不因 delta 读失败让跟踪不可用）。
    """
    try:
        from ..data.overlay import OverlayView

        view = OverlayView.from_root(cfg.market_data_root, required=False)
        if view is None or view.delta is None:
            return []
        state = view.overlay
        return list(
            view.delta.visible_trade_dates(
                watermark=int(state.delta_watermark or 0) or None,
                commit_seq=int(state.delta_commit_seq or 0) or None,
            )
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("screen_tracking delta 交易日读取失败（按 base 日历）: %s", e)
        return []


def _data_face_cutoff(cfg: AStockConfig) -> int:
    """数据面 cutoff 日期（轻量：只读 L1 manifest 的标量字段）。

    仅用于"本地日历是否过期"的判定——不取全市场 last_date 的 max（那是
    surface_max_date 的活，成本更高）。读不到 → 0（跳过校验）。
    """
    try:
        from ..data.dataset_store import DatasetStore

        session = _get_plane_session_cached(cfg)
        l1 = getattr(session, "formal_l1_id", None)
        if not l1:
            return 0
        m = DatasetStore(cfg.market_data_root).load_manifest(l1, deep_copy=False)
        if m is None:
            return 0
        return int(m.data_cutoff_date or 0)
    except Exception:  # noqa: BLE001
        return 0


def _now_str() -> str:
    import datetime as _dt

    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 补偿判定（契约 §7：不看"文件是否存在"，看目标完成状态）
# ---------------------------------------------------------------------------


def should_recompute(
    cfg: AStockConfig,
    week_id: int,
    *,
    calendar=None,
    algo_version: Optional[str] = None,
    bar_loader: Optional[PlaneBatchLoader] = None,
    benchmark_loader: Optional[PlaneBatchLoader] = None,
) -> Tuple[bool, str]:
    """该周跟踪是否需要（重）算（契约 §7 补偿触发）。

    触发 = 窗口已结束（数据面 max_date >= 跟踪周末交易日）且满足任一：
    无产物 / 非终态 / revision 不一致（算法/数据版本升级捞回陈旧 complete）。
    no_trading_week 是日历推导的终态 → 永不再算（无产物也 True 被
    TaskState completion=no_trading_week 短路——这就是整周休市要写
    状态记录的原因）。
    """
    algo_version = str(algo_version or TRACKING_ALGO_VERSION)
    storage_root = Path(cfg.storage_root)
    snap = ss.load_published_snapshot_for_week(cfg, int(week_id))
    if snap is None:
        return False, "no_published_snapshot"
    snapshot_id = str(snap["snapshot_id"])
    task_key = tracking_task_key(snapshot_id)
    signal_date = int(snap.get("asof") or int(week_id))

    if calendar is None:
        calendar = _load_calendar_or_none(cfg)
    if calendar is None:
        return False, "calendar_unavailable"
    win = sc.natural_week_window(list(calendar.dates), signal_date)
    if win.get("anomaly"):
        return False, f"window_anomaly:{win['anomaly']}"
    if win.get("no_trading_week"):
        return False, "no_trading_week"
    track_week = win["track_week"]
    assert track_week is not None
    week_end = int(track_week[1])

    # 窗口是否结束：只看数据面覆盖（个股缺行是 missing，不是周没结算）。
    # 注入 loader 时传**该周入选票**（与 compute_weekly_tracking 同参），
    # 空列表会让合成 loader 无从体现 max_date → 误判 window_not_ended。
    plan_codes = sorted(
        {
            str(m.get("code"))
            for r in snap.get("rules") or []
            for m in r.get("matched") or []
        }
    )
    if bar_loader is not None:
        try:
            ret = bar_loader(plan_codes)
            qfq_arrays = ret[0] if isinstance(ret, tuple) else ret
        except Exception:  # noqa: BLE001 — 注入 loader 异常按未结束处理
            qfq_arrays = {}
        data_max = max(
            [_arr_max_date(a) for a in (qfq_arrays or {}).values()] + [0]
        )
        if data_max <= 0:
            data_max = surface_max_date(cfg)
    else:
        data_max = surface_max_date(cfg)
    if data_max < week_end:
        return False, f"window_not_ended(max={data_max},need={week_end})"

    # 目标 revision：当前数据版本+算法版本下的期望产物身份
    bars_dv = _capture_data_version(cfg)
    bench_dv = {"benchmark": _benchmark_surface_id(cfg, {}) or "unavailable"}
    target_rev = sc.tracking_revision_id(
        snapshot_id, bars_dv, bench_dv, algo_version, TRACKING_SCHEMA_VERSION
    )
    state = sc.load_task_state(storage_root, task_key)
    # 终态自愈（真实数据事故修正，2026-09-15）：state 记着 no_trading_week，
    # 但**当前**日历经 natural_week_window 判定该周确实有交易日（本轮已走到
    # 这里，说明上面的 no_trading_week 短路没命中）——当初的终态是误判
    # （写盘时日历稀疏/数据面只覆盖到信号周，出现过"下一周整周无交易日"
    # 的瞬时视图），必须重算。should_complete_further 对 no_trading_week
    # 恒返回 False（契约：日历推导的合法终态），自愈只能在本层拦截——
    # 否则误判的周会被永久锁死，补偿机制再也捞不回来。
    if state is not None and str(state.get("completion") or "") == sc.TRACK_NO_TRADING_WEEK:
        return True, "no_trading_week_recovered"
    if sc.should_complete_further(
        state, window_ended=True, target_revision_id=target_rev
    ):
        if state is None:
            return True, "no_state"
        return True, "not_terminal_or_revision_changed"
    return False, "complete_and_revision_matches"


# ---------------------------------------------------------------------------
# 回填用的周枚举（周历定义，契约 §3：不倒找"有收益的周"）
# ---------------------------------------------------------------------------


def past_signal_weeks(
    calendar, anchor: int, n: int, *, strict_before: bool = False
) -> List[int]:
    """从 anchor 所在信号周起往回取 n 个 week_id（各 ISO 周的最后交易日）。

    anchor 本身不必是周五（节假日周的最后交易日可能是周四）；按 ISO 周
    逐周回退、每周取该 civil 周内最后一个交易日，整周休市自然被跳过
    （不产生 week_id，绝不拿前一周顶替——契约 §0）。

    strict_before=True 时排除 anchor 所在周、从上一周开始取——周五链
    场景：当晚刚发布本周新快照，要结算的是**上一信号周**（本周新名单
    「待下周结算」，契约 §2）；否则会把窗口未结束的本周跑成恒 pending。
    """
    if calendar is None or n <= 0:
        return []
    from datetime import timedelta

    out: List[int] = []
    d = _date(int(anchor) // 10000, (int(anchor) // 100) % 100, int(anchor) % 100)
    dates = sorted(int(x) for x in calendar.dates)
    if strict_before:
        d = d - timedelta(days=7)
    for _ in range(int(n)):
        iso = d.isocalendar()
        week_end = _date.fromisocalendar(int(iso[0]), int(iso[1]), 7)
        week_start = week_end - timedelta(days=6)
        in_week = [
            x for x in dates
            if week_start <= _date(x // 10000, (x // 100) % 100, x % 100) <= week_end
        ]
        if in_week:
            out.append(max(in_week))
        d = d - timedelta(days=7)
    return out


def previous_signal_week(calendar, week_id: int) -> Optional[int]:
    """上一信号周的 week_id（回退 ISO 周取其最后交易日）。

    周五链锚定用：给定本周 week_id 返回上一信号周；上一周整周休市
    （如春节）→ 继续回退到最近有交易日的周；日历耗尽 → None。
    """
    if calendar is None:
        return None
    from datetime import timedelta

    d = _date(int(week_id) // 10000, (int(week_id) // 100) % 100, int(week_id) % 100)
    dates = sorted(int(x) for x in calendar.dates)
    # 最多回退 4 个 ISO 周（足够跨过最长节假日段）
    for _ in range(4):
        d = d - timedelta(days=7)
        iso = d.isocalendar()
        week_end = _date.fromisocalendar(int(iso[0]), int(iso[1]), 7)
        week_start = week_end - timedelta(days=6)
        in_week = [
            x for x in dates
            if week_start <= _date(x // 10000, (x // 100) % 100, x % 100) <= week_end
        ]
        if in_week:
            return max(in_week)
    return None


def latest_signal_week(cfg: AStockConfig, calendar=None) -> int:
    """数据面最新可得交易日所属的信号周（= 周五链下一次要发布的周）。

    「指定规则补算」（子集快照）的护栏依据（契约 §6）：该周及其之后是
    周五链的地盘，子集快照是**部分名单**，占住发布指针会让链的全量快照
    被"已有指针不替换"永久挡住——该周就只剩这几条规则的数据了。

    日历或数据面不可用 → 0（护栏失效）；此时由 publish_decision 的
    "只允许补无指针周"兜底，绝不因护栏算不出来就放行覆盖。
    """
    if calendar is None:
        calendar = _load_calendar_or_none(cfg)
    if calendar is None:
        return 0
    try:
        raw = surface_max_date(cfg)
    except Exception:  # noqa: BLE001 — 推导失败按护栏失效处理（不阻塞调用方）
        return 0
    if not raw or int(raw) <= 0:
        return 0
    weeks = past_signal_weeks(calendar, int(raw), 1)
    return int(weeks[0]) if weeks else 0


def load_current_tracking(
    cfg: AStockConfig, snapshot_id: str
) -> Optional[Dict[str, Any]]:
    """读某快照当前 revision 的 track 产物（UI 数据源；无指针 → None）。"""
    p = sc.track_current_path(Path(cfg.storage_root), str(snapshot_id))
    if not p.exists():
        return None
    try:
        pointer = json.loads(p.read_text(encoding="utf-8"))
        rev = str(pointer.get("tracking_revision_id") or "")
        if not rev:
            return None
        path = sc.track_path(Path(cfg.storage_root), str(snapshot_id), rev)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 指针/产物损坏按无数据返回
        return None
