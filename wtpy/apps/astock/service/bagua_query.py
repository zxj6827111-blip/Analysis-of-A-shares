# -*- coding: utf-8 -*-
"""Single-stock hexagram (卦象) query by code + date + period.

Uses the same OHLC digit-sum algorithm as backtest bagua attach:
  upper = open digit sum mod 8
  lower = close digit sum mod 8
  yao   = (high + low) digit sum mod 6

Price plane selectable:
  - raw / unadjusted: L2 unadjusted OHLC (historical default for this query tool)
  - standard_qfq / qfq: ordinary forward-adjust to snapshot end
  - asof_forward_qfq / asof: 时点动态前复权 anchored at query date (L1 formal)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from ..bagua.calculator import BaguaCalculator
from ..config import AStockConfig
from ..data.adjustments import build_factor_series
from ..data.data_store import DataStore
from ..data.tdx_reader import DayBar, TdxDayReader
from ..data.universe import to_std_code
from ..study import (
    build_period_bars,
    day_bars_for_signals,
    day_bars_for_signals_affine,
    day_bars_to_standard_qfq,
)
from ..data.affine_adjust import build_affine_series
from .stock_names import display_code_with_name, resolve_stock_name


def _parse_ymd(value: Union[str, int, None]) -> int:
    if value is None or value == "":
        raise ValueError("date is required (YYYY-MM-DD or YYYYMMDD)")
    if isinstance(value, int):
        d = value
        if d < 19900101 or d > 21001231:
            raise ValueError(f"invalid date: {value}")
        return d
    s = str(value).strip().replace("/", "-")
    if "-" in s:
        parts = s.split("-")
        if len(parts) != 3:
            raise ValueError(f"invalid date: {value}")
        y, m, day = int(parts[0]), int(parts[1]), int(parts[2])
        d = y * 10000 + m * 100 + day
    else:
        digits = "".join(ch for ch in s if ch.isdigit())
        if len(digits) != 8:
            raise ValueError(f"invalid date: {value}")
        d = int(digits)
    if d < 19900101 or d > 21001231:
        raise ValueError(f"invalid date: {value}")
    return d


def normalize_period(period: Optional[str]) -> str:
    p = (period or "DAY").strip().upper()
    if p in ("DAY", "D", "1D", "日", "日线", "按日"):
        return "DAY"
    if p in ("WEEK", "W", "1W", "周", "周线", "按周"):
        return "WEEK"
    if p in ("MONTH", "M", "1M", "月", "月线", "按月"):
        return "MONTH"
    raise ValueError("period must be DAY, WEEK, or MONTH")


def normalize_adjust_mode(mode: Optional[str]) -> str:
    """Return canonical: raw | standard_qfq | asof_forward_qfq."""
    m = (mode or "raw").strip().lower()
    if m in ("raw", "unadjusted", "none", "未复权", "不复权"):
        return "raw"
    if m in ("standard_qfq", "qfq", "ordinary_qfq", "forward", "前复权", "普通前复权"):
        return "standard_qfq"
    if m in (
        "asof_forward_qfq",
        "asof",
        "asof_qfq",
        "dynamic_qfq",
        "pit_forward",
        "时点前复权",
        "动态前复权",
    ):
        return "asof_forward_qfq"
    raise ValueError(
        "adjust must be raw | standard_qfq (前复权) | asof_forward_qfq (时点前复权)"
    )


def normalize_query_code(raw: str) -> str:
    """Accept 600000 / sh600000 / SSE.STK.600000 -> WonderTrader std code."""
    t = (raw or "").strip()
    if not t:
        raise ValueError("code is required")
    t = t.split()[0].split("　")[0]
    if t.startswith("SSE.") or t.startswith("SZSE."):
        return t
    return to_std_code(t)


def display_code(std_code: str) -> str:
    if std_code.startswith("SSE.STK."):
        return "sh" + std_code.split(".")[-1]
    if std_code.startswith("SZSE.STK."):
        return "sz" + std_code.split(".")[-1]
    return std_code


def load_day_bars(cfg: AStockConfig, std_code: str) -> List[DayBar]:
    store = DataStore(cfg.storage_root)
    try:
        return store.load_symbol(std_code)
    except FileNotFoundError:
        pass
    reader = TdxDayReader(cfg.tdx_root)
    raw = ("sh" if std_code.startswith("SSE") else "sz") + std_code.split(".")[-1]
    bars, _ = reader.read(raw)
    if not bars:
        raise FileNotFoundError(f"no bars for {std_code}")
    return list(bars)


def _find_day_bar(bars: Sequence[DayBar], asof: int) -> Tuple[DayBar, bool]:
    if not bars:
        raise FileNotFoundError("empty bar series")
    by_date = {int(b.date): b for b in bars}
    if asof in by_date:
        return by_date[asof], True
    candidates = [b for b in bars if int(b.date) <= asof]
    if not candidates:
        first = bars[0]
        raise FileNotFoundError(
            f"no bar on/before {asof}; first available {first.date}"
        )
    return candidates[-1], False


def _find_period_bar(
    day_bars: Sequence[DayBar],
    period: str,
    asof: int,
) -> Tuple[Any, bool]:
    p_bars = build_period_bars(day_bars, period, asof=asof, include_open=True)
    if not p_bars:
        raise FileNotFoundError(f"no {period} bar for {asof}")
    for pb in reversed(p_bars):
        start = int(getattr(pb, "start_date", pb.date))
        end = int(getattr(pb, "end_date", pb.date))
        if start <= asof <= end:
            exact = end == asof or bool(getattr(pb, "closed", True))
            return pb, exact
    before = [pb for pb in p_bars if int(getattr(pb, "end_date", pb.date)) <= asof]
    if not before:
        return p_bars[0], False
    return before[-1], False


def _adjust_day_bars(
    cfg: AStockConfig,
    std_code: str,
    day_raw: Sequence[DayBar],
    adjust: str,
    asof: int,
) -> Tuple[List[DayBar], Dict[str, Any]]:
    """Return (day bars in chosen price plane, meta)."""
    meta: Dict[str, Any] = {
        "adjust": adjust,
        "price_plane": "L2_trade_price" if adjust == "raw" else "L1_signal_price",
    }
    if adjust == "raw" or not day_raw:
        meta["price_format"] = "unadjusted, 2 decimal places"
        return list(day_raw), meta

    dates = [int(b.date) for b in day_raw]

    affine = build_affine_series(std_code, dates, adj_root=cfg.adj_root)
    if affine.quality == "complete" and not affine.is_identity:
        out = day_bars_for_signals_affine(
            day_raw,
            affine,
            research_unadjusted=False,
            signal_adjust=adjust,
            asof_date=asof,
        )
        meta["factor_source"] = affine.source
        meta["factor_quality"] = affine.quality
        meta["factor_manifest_sha"] = affine.sha256
        meta["model"] = "affine"
        if adjust == "standard_qfq":
            meta["price_format"] = "standard_qfq affine (a*raw+b), 2 decimal places"
            meta["signal_adjust"] = "standard_qfq"
        else:
            meta["price_format"] = "asof_forward_qfq affine (a*raw+b), 2 decimal places"
            meta["signal_adjust"] = "asof_forward_qfq"
            meta["asof_date"] = asof
        return out, meta

    series = build_factor_series(
        std_code, dates, adj_root=cfg.adj_root, prefer_baostock=True
    )
    fac = np.array(series.factors, dtype=float)
    meta["factor_source"] = series.source
    meta["factor_quality"] = series.quality
    meta["factor_manifest_sha"] = series.sha256
    meta["model"] = "multiplicative_fallback"

    if adjust == "standard_qfq":
        out = day_bars_to_standard_qfq(day_raw, fac)
        meta["price_format"] = "standard_qfq (factor_t/snapshot_end), 2 decimal places"
        meta["signal_adjust"] = "standard_qfq"
    else:
        out = day_bars_for_signals(
            day_raw,
            fac,
            research_unadjusted=False,
            signal_adjust="asof_forward_qfq",
            asof_date=asof,
            dates=dates,
        )
        meta["price_format"] = (
            "asof_forward_qfq (factor_t/factor_asof at query date), 2 decimal places"
        )
        meta["signal_adjust"] = "asof_forward_qfq"
        meta["asof_date"] = asof
    return out, meta


def query_bagua(
    cfg: AStockConfig,
    *,
    code: str,
    date: Union[str, int],
    period: str = "DAY",
    adjust: str = "raw",
) -> Dict[str, Any]:
    """Query hexagram for one stock at a given date and period.

    adjust: raw | standard_qfq | asof_forward_qfq
    """
    std = normalize_query_code(code)
    asof = _parse_ymd(date)
    per = normalize_period(period)
    adj = normalize_adjust_mode(adjust)

    if not cfg.bagua_json:
        raise FileNotFoundError("bagua knowledge json not configured")
    calc = BaguaCalculator.from_json(cfg.bagua_json)

    day_raw = load_day_bars(cfg, std)
    if not day_raw:
        raise FileNotFoundError(f"no market data for {display_code(std)}")

    day_bars, adj_meta = _adjust_day_bars(cfg, std, day_raw, adj, asof)

    if per == "DAY":
        bar, exact = _find_day_bar(day_bars, asof)
        bar_meta = {
            "date": int(bar.date),
            "start_date": int(bar.date),
            "end_date": int(bar.date),
            "n_days": 1,
            "closed": True,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
        }
        o, h, l, c = bar.open, bar.high, bar.low, bar.close
    else:
        pb, exact = _find_period_bar(day_bars, per, asof)
        bar_meta = {
            "date": int(pb.date),
            "start_date": int(getattr(pb, "start_date", pb.date)),
            "end_date": int(getattr(pb, "end_date", pb.date)),
            "n_days": int(getattr(pb, "n_days", 1)),
            "closed": bool(getattr(pb, "closed", True)),
            "open": float(pb.open),
            "high": float(pb.high),
            "low": float(pb.low),
            "close": float(pb.close),
        }
        o, h, l, c = pb.open, pb.high, pb.low, pb.close

    result = calc.calculate(open_price=o, high_price=h, low_price=l, close_price=c)
    bagua = result.to_dict()

    notes: List[str] = []
    if adj == "raw":
        notes.append(
            "算法：开盘定上卦(mod8)、收盘定下卦(mod8)、最高+最低定动爻(mod6)；价格未复权两位小数后逐位求和。"
        )
    elif adj == "standard_qfq":
        notes.append(
            "算法同未复权；价格为普通前复权(standard_qfq，锚点=因子快照末端)，与盘面通达信风格接近。"
        )
    else:
        notes.append(
            "算法同未复权；价格为时点动态前复权(asof_forward_qfq)，锚点=查询日及以前可知因子，与回测 L1 信号默认一致。"
        )
    if not exact and per == "DAY":
        notes.append(f"请求日期 {asof} 非交易日或无日线，已使用最近交易日 {bar_meta['date']}。")
    if per != "DAY" and not bar_meta.get("closed", True):
        notes.append(f"该{('周' if per == 'WEEK' else '月')}K 尚未收官，卦象可能随后续交易日变化。")
    if per == "DAY" and int(bar_meta["date"]) != asof:
        notes.append(f"实际使用日线日期：{bar_meta['date']}。")

    stock_name = resolve_stock_name(cfg, display_code(std), std_code=std)

    return {
        "ok": True,
        "code": display_code(std),
        "name": stock_name,
        "display": display_code_with_name(display_code(std), stock_name),
        "std_code": std,
        "query_date": asof,
        "period": per,
        "adjust": adj,
        "price_plane": adj_meta.get("price_plane"),
        "bar_date_exact": exact if per == "DAY" else (int(bar_meta["end_date"]) == asof),
        "bar": bar_meta,
        "bagua": bagua,
        "algorithm": {
            "open_to_upper": "digit_sum(open) mod 8 (0→8)",
            "close_to_lower": "digit_sum(close) mod 8 (0→8)",
            "hl_to_yao": "digit_sum(high)+digit_sum(low) mod 6 (0→6)",
            "price_format": adj_meta.get("price_format"),
            "adjust": adj,
        },
        "adjust_meta": adj_meta,
        "notes": notes,
        "summary": {
            "full_name": bagua.get("full_name") or bagua.get("gua_name") or "",
            "yao_name": bagua.get("yao_name") or bagua.get("line_name") or "",
            "state_id": bagua.get("state_id") or "",
            "action_signal": bagua.get("action_signal") or "",
            "market_judgement": bagua.get("market_judgement")
            or bagua.get("market_summary")
            or "",
            "upper": f"{bagua.get('upper_alias') or bagua.get('upper_name') or ''}"
            f"({bagua.get('upper_id')})",
            "lower": f"{bagua.get('lower_alias') or bagua.get('lower_name') or ''}"
            f"({bagua.get('lower_id')})",
            "yao_order": bagua.get("yao_order"),
        },
    }
