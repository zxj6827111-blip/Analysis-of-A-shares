"""Forward-return stats for the stocks picked by a backtest run.

A run's ``signals.csv`` lists every pick the rules produced (stock code +
signal date). Trade-level metrics cannot answer "these picks, how much did
they rise in the following week" — a run with ``hold=1`` or a fixed exit
weekday closes positions long before a week has passed, and the same stock
may be re-picked across dates without a clean per-pick return.

This module re-reads the picks and measures the forward-adjusted close-to-close
return over the next ``horizon`` trading days (default 5 = one trading week),
aggregating mean / median / win-rate / pending counts.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..config import AStockConfig
from ..data.tdx_reader import DayBar

logger = logging.getLogger(__name__)

DEFAULT_HORIZON = 5
DEFAULT_PLANE = "tushare_qfq"
MAX_HORIZON = 60

# (bars, meta) — meta carries the plane's dataset lineage for the UI.
BarLoader = Callable[[str], Tuple[Sequence[DayBar], Dict[str, Any]]]


def _run_dir(cfg: AStockConfig, run_id: str) -> Path:
    return Path(cfg.output_root) / str(run_id)


def _cache_path(cfg: AStockConfig, run_id: str, horizon: int, plane: str) -> Path:
    """Derived-result cache, deliberately kept OUT of the run folder.

    Run output folders are immutable evidence (artifact lists, cross-run
    comparison, export); a viewer-time cache must never turn up as a new
    artifact of an already-finished run.
    """
    return (
        Path(cfg.storage_root)
        / "cache"
        / "signal_returns"
        / str(run_id)
        / f"signal_returns_h{int(horizon)}_{plane}.json"
    )


def normalize_plane(plane: str) -> str:
    """Canonicalize the price plane; only raw / tushare_qfq are supported."""
    from .bagua_query import normalize_adjust_mode

    key = normalize_adjust_mode(plane)
    if key not in ("raw", "tushare_qfq"):
        raise ValueError(
            f"unsupported plane: {plane!r}（仅支持 raw / tushare_qfq）"
        )
    return key


def read_signal_picks(out_dir: Path) -> List[Dict[str, Any]]:
    """Read ``signals.csv`` into one row per (code, signal date).

    The same stock picked by several rules on one date is a single pick for
    this measure, so indicator ids are merged instead of duplicated.
    """
    path = out_dir / "signals.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"signals.csv not found in run {out_dir.name}：该回测未保存信号明细"
        )
    merged: Dict[Tuple[str, int], Dict[str, Any]] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for raw in csv.DictReader(f):
            code = (raw.get("std_code") or "").strip()
            if not code:
                continue
            try:
                date = int(str(raw.get("date") or "").strip())
            except ValueError:
                continue
            if date <= 0:
                continue
            row = merged.get((code, date))
            if row is None:
                row = {
                    "std_code": code,
                    "signal_date": date,
                    "indicator_ids": [],
                    "period": (raw.get("period") or "").strip(),
                    "bagua_full_name": (raw.get("bagua_full_name") or "").strip(),
                    "bagua_yao_name": (raw.get("bagua_yao_name") or "").strip(),
                    "bagua_action_signal": (raw.get("bagua_action_signal") or "").strip(),
                }
                merged[(code, date)] = row
            iid = (raw.get("indicator_id") or "").strip()
            if iid and iid not in row["indicator_ids"]:
                row["indicator_ids"].append(iid)
    return sorted(merged.values(), key=lambda r: (r["signal_date"], r["std_code"]))


def _series_from_arrays(arr: Dict[str, Any]):
    """(dates, closes) for one symbol — only what the forward window needs.

    Materializing a DayBar per bar costs ~2.7us, i.e. ~16s for a full-market
    run; the measure only needs the two columns, so keep them as arrays.
    """
    dates = arr.get("trade_date")
    if dates is None or len(dates) == 0:
        return None, None
    closes = arr.get("close")
    if closes is None or len(closes) != len(dates):
        return None, None
    if len(dates) > 1 and int(dates[0]) > int(dates[-1]):
        dates = dates[::-1]
        closes = closes[::-1]
    return dates, closes


def _load_bars_batch(cfg: AStockConfig, plane: str, codes: Sequence[str]):
    """Resolve every pick once, then read bars per dataset in bulk.

    The per-symbol path costs ~60ms (blob read + decompress), so a full-market
    run of thousands of picks would take minutes. Overlay datasets expose a
    DuckDB batch reader (~18x faster), so picks are grouped by the dataset they
    resolve to and read in one call per dataset. Resolution and the returned
    meta come from the same ``BaguaPlaneSession`` the per-symbol path uses.

    Returns ``{code: (dates, closes, meta)}``.
    """
    from .bagua_query import BaguaPlaneSession

    session = BaguaPlaneSession(cfg, plane)
    out: Dict[str, Tuple[Any, Any, Dict[str, Any]]] = {}
    resolved: List[Any] = []
    groups: Dict[str, Tuple[Any, List[str]]] = {}
    for code in codes:
        try:
            res = session.resolve_symbol(code)
        except Exception as e:  # noqa: BLE001 — unresolved symbol -> no_data row
            logger.warning("signal_returns 解析失败 %s: %s", code, e)
            out[code] = (None, None, {})
            continue
        manifest = res.manifest
        symbol = res.record.symbol
        entry = groups.get(manifest.dataset_id)
        if entry is None:
            entry = (manifest, [])
            groups[manifest.dataset_id] = entry
        if symbol not in entry[1]:
            entry[1].append(symbol)
        resolved.append(res)

    arrays_by_ds: Dict[str, Dict[str, Any]] = {}
    for ds_id, (_manifest, symbols) in groups.items():
        try:
            arrays_by_ds[ds_id] = session.repo.load_bar_arrays(
                dataset_id=ds_id, symbols=symbols
            )
        except Exception as e:  # noqa: BLE001 — one dataset must not kill the table
            logger.warning("signal_returns 批量加载失败 %s: %s", ds_id, e)
            arrays_by_ds[ds_id] = {}

    for res in resolved:
        arr = arrays_by_ds.get(res.manifest.dataset_id, {}).get(res.record.symbol)
        dates, closes = _series_from_arrays(arr or {})
        out[res.std_code] = (dates, closes, session.build_meta(res))
    return out


def _locate(dates, target: int) -> Optional[int]:
    """Index of an exact date match, or None. Accepts ndarray or sequence."""
    if dates is None:
        return None
    if isinstance(dates, np.ndarray):
        i = int(np.searchsorted(dates, target))
        if 0 <= i < dates.size and int(dates[i]) == target:
            return i
        return None
    try:
        return dates.index(target)  # type: ignore[attr-defined]
    except ValueError:
        return None



def _summarize(rows: Sequence[Dict[str, Any]], horizon: int) -> Dict[str, Any]:
    """Aggregate the valid rows; pending / no-bar picks are counted, not dropped."""
    rets = np.array(
        [r["ret"] for r in rows if r.get("status") == "ok" and r.get("ret") is not None],
        dtype=np.float64,
    )
    summary: Dict[str, Any] = {
        "horizon": int(horizon),
        "n_total": len(rows),
        "n_ok": int(rets.size),
        "n_pending": sum(1 for r in rows if r.get("status") == "pending"),
        "n_no_bar": sum(1 for r in rows if r.get("status") == "no_bar"),
        "n_no_data": sum(1 for r in rows if r.get("status") == "no_data"),
        "mean": None,
        "median": None,
        "win_rate": None,
        "max": None,
        "min": None,
        "p25": None,
        "p75": None,
        "std": None,
    }
    if rets.size:
        summary.update(
            mean=float(np.mean(rets)),
            median=float(np.median(rets)),
            win_rate=float(np.mean(rets > 0)),
            max=float(np.max(rets)),
            min=float(np.min(rets)),
            p25=float(np.percentile(rets, 25)),
            p75=float(np.percentile(rets, 75)),
            std=float(np.std(rets, ddof=0)),
        )
    return summary


def compute_signal_forward_returns(
    cfg: AStockConfig,
    run_id: str,
    *,
    horizon: int = DEFAULT_HORIZON,
    plane: str = DEFAULT_PLANE,
    force: bool = False,
    bar_loader: Optional[BarLoader] = None,
    with_names: bool = True,
) -> Dict[str, Any]:
    """Forward close-to-close returns for every pick in a backtest run.

    Entry is the signal-date close, exit the close ``horizon`` trading days
    later, on the (default) forward-adjusted plane so ex-dividend gaps are not
    read as losses. Picks without enough forward bars (data end / suspension)
    are reported with ``status="pending"`` and excluded from the statistics.

    Results are cached next to the run as ``signal_returns_h{h}_{plane}.json``;
    the cache is invalidated when ``signals.csv`` changes.
    """
    horizon = int(horizon)
    if horizon < 1 or horizon > MAX_HORIZON:
        raise ValueError(f"horizon must be in [1, {MAX_HORIZON}]")
    plane = normalize_plane(plane)

    out_dir = _run_dir(cfg, run_id)
    if not out_dir.is_dir():
        raise FileNotFoundError(f"run not found: {run_id}")

    sig_path = out_dir / "signals.csv"
    if not sig_path.exists():
        raise FileNotFoundError(
            f"signals.csv not found in run {run_id}：该回测未保存信号明细"
        )
    sig_stat = sig_path.stat()
    fingerprint = {
        "signals_mtime_ns": sig_stat.st_mtime_ns,
        "signals_size": sig_stat.st_size,
    }

    cache_p = _cache_path(cfg, run_id, horizon, plane)
    if cache_p.exists() and not force:
        try:
            cached = json.loads(cache_p.read_text(encoding="utf-8"))
            if all(cached.get(k) == v for k, v in fingerprint.items()):
                cached["reused"] = True
                return cached
        except Exception as e:  # noqa: BLE001 — corrupted cache just recomputes
            logger.warning("signal_returns 缓存损坏（重算）: %s", e)

    picks = read_signal_picks(out_dir)
    codes = sorted({p["std_code"] for p in picks})
    if bar_loader is not None:
        def _get(code: str):
            """Injected loader: DayBar sequences -> (dates, closes, meta)."""
            try:
                bars, meta = bar_loader(code)
            except FileNotFoundError:
                return None, None, {}
            except Exception as e:  # noqa: BLE001 — one bad symbol must not fail the table
                logger.warning("signal_returns 加载失败 %s: %s", code, e)
                return None, None, {}
            if not bars:
                return None, None, meta or {}
            dates = [int(b.date) for b in bars]
            closes = [float(b.close) for b in bars]
            if len(dates) > 1 and dates[0] > dates[-1]:
                dates.reverse()
                closes.reverse()
            return dates, closes, meta or {}
    else:
        loaded = _load_bars_batch(cfg, plane, codes)

        def _get(code: str):
            return loaded.get(code) or (None, None, {})

    name_cache: Dict[str, str] = {}
    rows: List[Dict[str, Any]] = []
    plane_meta: Dict[str, Any] = {}
    for pick in picks:
        code = pick["std_code"]
        dates, closes, meta = _get(code)
        if meta and not plane_meta:
            plane_meta = {
                k: meta.get(k)
                for k in (
                    "dataset_id",
                    "dataset_source",
                    "dataset_adjustment",
                    "legacy_fallback",
                    "bootstrap_fallback",
                )
                if k in meta
            }

        row = dict(pick)
        row["plane"] = plane
        n = 0 if dates is None else len(dates)
        i = _locate(dates, int(pick["signal_date"]))
        if n == 0:
            # 整只股票没有可用行情（未上市/退市/数据集缺失），与「当天无K线」区分
            row.update(entry_close=None, exit_close=None, exit_date=None,
                       ret=None, status="no_data")
        elif i is None:
            row.update(entry_close=None, exit_close=None, exit_date=None,
                       ret=None, status="no_bar")
        elif i + horizon >= n:
            row.update(entry_close=float(closes[i]), exit_close=None,
                       exit_date=None, ret=None, status="pending")
        else:
            entry = float(closes[i])
            exit_close = float(closes[i + horizon])
            exit_date = int(dates[i + horizon])
            if entry <= 0:
                row.update(entry_close=entry, exit_close=exit_close,
                           exit_date=exit_date, ret=None, status="bad_price")
            else:
                row.update(entry_close=entry, exit_close=exit_close,
                           exit_date=exit_date, ret=exit_close / entry - 1.0,
                           status="ok")
        if with_names:
            if code not in name_cache:
                try:
                    from .stock_names import resolve_stock_name

                    name_cache[code] = resolve_stock_name(cfg, code)
                except Exception:  # noqa: BLE001
                    name_cache[code] = ""
            row["name"] = name_cache[code]
        rows.append(row)

    payload: Dict[str, Any] = {
        "run_id": str(run_id),
        "horizon": horizon,
        "plane": plane,
        "plane_meta": plane_meta,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": _summarize(rows, horizon),
        "rows": rows,
        **fingerprint,
    }
    try:
        cache_p.parent.mkdir(parents=True, exist_ok=True)
        cache_p.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001 — cache write is best-effort
        logger.warning("signal_returns 缓存写入失败: %s", e)
    return payload
