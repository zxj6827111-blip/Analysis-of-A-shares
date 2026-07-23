# -*- coding: utf-8 -*-
"""Phase-3 micro-benchmark: signal cache + fast vs full (synthetic, no TDX)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import List

# Allow `python scripts/bench_phase3_cache_fast.py` from repo root without install.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.research.fast_engine import run_fast_backtest
from wtpy.apps.astock.research.signal_cache import get_or_compute_signals, signal_cache_key
from wtpy.apps.astock.study import SignalEvent



def _bars(n: int = 60) -> dict:
    code = "SSE.STK.600000"
    # synthetic weekdays from 20240102
    dates = []
    d = 20240102
    while len(dates) < n:
        dates.append(d)
        # rough +1 day without calendar libs
        y, m, day = d // 10000, (d // 100) % 100, d % 100
        day += 1
        if day > 28:
            day = 1
            m += 1
            if m > 12:
                m = 1
                y += 1
        d = y * 10000 + m * 100 + day
    bars = {
        code: [
            DayBar(dt, 10 + i * 0.01, 11, 9, 10.5, 1, 1000) for i, dt in enumerate(dates)
        ]
    }
    return code, dates, bars


def main() -> None:
    code, dates, bars = _bars(80)
    cal = TradeCalendar(dates)
    events: List[SignalEvent] = [
        SignalEvent(code, dates[i], "DAY", "bench") for i in range(5, 50, 5)
    ]

    t0 = time.perf_counter()
    r1 = run_fast_backtest(events, bars, cal, hold=1, entry_lag=1)
    t_fast = time.perf_counter() - t0

    # signal cache micro
    cfg = AStockConfig()
    cfg.storage_root = Path("storage") / "astock_bench_tmp"
    cfg.ensure_dirs()
    key = signal_cache_key(
        indicator_ids=["bench"],
        period="DAY",
        start=dates[0],
        end=dates[-1],
        universe_hash="bench",
        adjust_mode="adjusted",
    )
    n = {"c": 0}

    def compute():
        n["c"] += 1
        time.sleep(0.05)  # pretend indicator cost
        return list(events)

    t0 = time.perf_counter()
    get_or_compute_signals(key, compute, cfg=cfg, use_cache=True)
    t_miss = time.perf_counter() - t0
    t0 = time.perf_counter()
    get_or_compute_signals(key, compute, cfg=cfg, use_cache=True)
    t_hit = time.perf_counter() - t0

    print("=== Phase-3 micro benchmark (synthetic) ===")
    print(f"fast_engine: trades={r1.n_trades} elapsed={t_fast*1000:.1f} ms")
    print(f"signal_cache miss: {t_miss*1000:.1f} ms (compute_calls={n['c']})")
    print(f"signal_cache hit:  {t_hit*1000:.1f} ms (compute_calls={n['c']})")
    print(f"cache speedup (miss/hit): {t_miss / max(t_hit, 1e-9):.1f}x")


if __name__ == "__main__":
    main()
