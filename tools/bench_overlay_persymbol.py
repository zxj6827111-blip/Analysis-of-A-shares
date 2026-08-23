# -*- coding: utf-8 -*-
"""基准：全市场卦象导出逐股票读取路径（overlay_v1 性能回归验收工具）。

三个场景各自使用**独立的 BaguaPlaneSession / MarketDataRepository**，计数器
补丁在每个场景结束后完整还原，杜绝跨场景预热污染：

  A) 回归模拟   : 逐周期 query_bagua + 绕过 View LRU（等价 0f009e9 修复前：
                  每次加载新建 OverlayView → 每股 2 次 delta 全量查询）
  B) 仅 View 缓存: 逐周期 query_bagua（repository LRU 生效 → delta 全市场 1 次，
                  但每股仍解压/物化 2 次）
  C) 完整修复   : _query_bagua_periods_for_code（View LRU + WEEK/MONTH 共享
                  物化 → delta 1 次、每股解压 1 次）

用法：

    python tools/bench_overlay_persymbol.py [样本数]

数据根取 MARKET_DATA_ROOT 环境变量，未设置时回退 E:\\AStockData\\datasets\\market_data。
注意：OS 文件缓存在场景间无法隔离，绝对耗时只看趋势；delta/NPZ 调用次数
是确定性证据。生产验收以服务器 30 股票基准 + 全市场导出耗时为准。
"""

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MARKET_DATA_ROOT", r"E:\AStockData\datasets\market_data")

from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.data.dataset_store import DatasetStore
from wtpy.apps.astock.data.delta_store import DeltaStore
from wtpy.apps.astock.data.overlay import OverlayView
from wtpy.apps.astock.data.repository import MarketDataRepository
from wtpy.apps.astock.service import bagua_query as bq
from wtpy.apps.astock.service.bagua_query import (
    BaguaPlaneSession,
    _query_bagua_periods_for_code,
    query_bagua,
)

ASOF_MAP = {"WEEK": 20260821, "MONTH": 20260731}
PERIODS = ("WEEK", "MONTH")


@contextmanager
def _counted(patch_repo_lru: bool = False):
    """场景级计数器与补丁：进入安装、退出完整还原（含异常路径）。"""
    counts = {"delta_bars": 0, "delta_factors": 0, "npz": 0, "views": 0}
    orig = {
        "bars": DeltaStore.load_all_visible_bars,
        "factors": DeltaStore.load_all_visible_factors,
        "npz": DatasetStore.load_bars,
        "view": MarketDataRepository._overlay_view_for_manifest,
        "for_manifest": OverlayView.for_manifest.__func__,
    }

    def _bars(self, watermark, **kw):
        counts["delta_bars"] += 1
        return orig["bars"](self, watermark, **kw)

    def _factors(self, watermark, **kw):
        counts["delta_factors"] += 1
        return orig["factors"](self, watermark, **kw)

    def _npz(self, sha):
        counts["npz"] += 1
        return orig["npz"](self, sha)

    # 统计 OverlayView 的**真实创建**次数（而非 repository 入口调用次数）
    def _for_manifest(cls, store, manifest):
        counts["views"] += 1
        return orig["for_manifest"](cls, store, manifest)

    DeltaStore.load_all_visible_bars = _bars
    DeltaStore.load_all_visible_factors = _factors
    DatasetStore.load_bars = _npz
    OverlayView.for_manifest = classmethod(_for_manifest)
    if patch_repo_lru:
        # 回归模拟：绕过 LRU，每次调用直接新建 OverlayView（0f009e9 行为）
        def _no_cache(self, manifest):
            return OverlayView.for_manifest(self._store, manifest)

        MarketDataRepository._overlay_view_for_manifest = _no_cache
    try:
        yield counts
    finally:
        DeltaStore.load_all_visible_bars = orig["bars"]
        DeltaStore.load_all_visible_factors = orig["factors"]
        DatasetStore.load_bars = orig["npz"]
        OverlayView.for_manifest = classmethod(orig["for_manifest"])
        MarketDataRepository._overlay_view_for_manifest = orig["view"]


def _fresh_session(cfg) -> BaguaPlaneSession:
    """场景独立 session：清空模块级 TTL 缓存后新建，避免复用预热实例。"""
    bq._session_cache.clear()
    return BaguaPlaneSession(cfg, "raw")


def _pools(cfg, n):
    stocks = bq._resolve_batch_codes(cfg, None, all_stocks=True)
    etfs = bq._enumerate_export_etf_pool(cfg)
    if n and n > 0:
        stocks = stocks[:n]
        etfs = etfs[: max(0, n // 4)]
    return stocks, etfs


def _run_pool(cfg, session, calc, pool, runner):
    ok = err = 0
    for code in pool:
        if runner == "per_period":
            row = {}
            for per in PERIODS:
                try:
                    row[per] = query_bagua(
                        cfg, code=code, date=ASOF_MAP[per],
                        period=per, adjust="raw",
                        session=session, calc=calc,
                    )
                except Exception:
                    row[per] = None
        else:
            # 与 per_period 分支对称：单标的整体失败计为失败行，不中断基准
            try:
                row = _query_bagua_periods_for_code(
                    cfg, code=code, asof=ASOF_MAP["WEEK"],
                    periods=list(PERIODS), adjust="raw",
                    session=session, calc=calc, asof_map=ASOF_MAP,
                )
            except Exception:
                row = {per: None for per in PERIODS}
        if all(v is not None and v.get("ok") for v in row.values()):
            ok += 1
        else:
            err += 1
    return ok, err


def main(n: int = 100) -> None:
    cfg = get_default_config()
    calc = bq.BaguaCalculator.from_json(cfg.bagua_json)
    stocks, etfs = _pools(cfg, n)
    pool = stocks + etfs
    total_stocks, total_etfs = (
        len(bq._resolve_batch_codes(cfg, None, all_stocks=True)),
        len(bq._enumerate_export_etf_pool(cfg)),
    )
    total = total_stocks + total_etfs
    print(f"样本池: 股票 {len(stocks)} + ETF {len(etfs)} = {len(pool)}"
          f"（全市场 {total_stocks} + {total_etfs} = {total}）")

    results = {}
    scenarios = (
        ("A) 回归模拟: 逐周期+每次新建View", "per_period", True),
        ("B) 仅View缓存: 逐周期", "per_period", False),
        ("C) 完整修复: View缓存+共享物化", "shared", False),
    )
    for label, runner, patch_lru in scenarios:
        session = _fresh_session(cfg)
        with _counted(patch_repo_lru=patch_lru) as counts:
            t0 = time.perf_counter()
            ok_s, err_s = _run_pool(cfg, session, calc, stocks, runner)
            dt_stocks = time.perf_counter() - t0
            t0 = time.perf_counter()
            ok_e, err_e = _run_pool(cfg, session, calc, etfs, runner)
            dt_etfs = time.perf_counter() - t0
        # 分类平均 + 按真实池规模加权外推（n//4 的抽样比例 ≠ 生产比例）
        ms_stocks = dt_stocks / max(1, len(stocks)) * 1000
        ms_etfs = dt_etfs / max(1, len(etfs)) * 1000 if etfs else 0.0
        weighted_min = (
            ms_stocks * total_stocks + ms_etfs * total_etfs
        ) / 1000 / 60
        results[label] = weighted_min
        print(
            f"{label}:\n"
            f"    股票 {len(stocks)} 只: 成功 {ok_s} / 失败 {err_s}, "
            f"{ms_stocks:6.1f} ms/只\n"
            f"    ETF   {len(etfs)} 只: 成功 {ok_e} / 失败 {err_e}, "
            f"{ms_etfs:6.1f} ms/只\n"
            f"    delta_bars={counts['delta_bars']}  "
            f"delta_factors={counts['delta_factors']}  npz={counts['npz']}  "
            f"view创建={counts['views']}\n"
            f"    加权全市场外推（{total_stocks} 股票 + {total_etfs} ETF）: "
            f"{weighted_min:.1f} 分钟"
        )

    # 一致性：C 路径与逐周期路径结果必须逐字段一致（独立 session，无补丁）
    session = _fresh_session(cfg)
    sample = pool[: min(20, len(pool))]
    mism = 0
    for code in sample:
        a = {
            per: query_bagua(cfg, code=code, date=ASOF_MAP[per], period=per,
                             adjust="raw", session=session, calc=calc)
            for per in PERIODS
        }
        b = _query_bagua_periods_for_code(
            cfg, code=code, asof=ASOF_MAP["WEEK"], periods=list(PERIODS),
            adjust="raw", session=session, calc=calc, asof_map=ASOF_MAP,
        )
        if a != b:
            mism += 1
            print(f"  [MISMATCH] {code}")
    print(f"一致性抽样: {len(sample)} 只中字段不一致 {mism} 只")

    for label, weighted_min in results.items():
        print(f"加权全市场外推 [{label}]: {weighted_min:.1f} 分钟")
    print("注：OS 文件缓存在场景间无法隔离，上述绝对耗时仅用于趋势对比；"
          "delta/NPZ 调用次数为确定性证据，生产验收以服务器完整池实测为准。")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 100)
