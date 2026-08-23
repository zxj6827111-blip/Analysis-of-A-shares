# -*- coding: utf-8 -*-
"""OverlayView 缓存生命周期与多周期单次物化的专项测试。

背景回归（0f009e9）：_load_virtual_bars 从仓库级复用视图改为每次调用
OverlayView.for_manifest()，实例级 delta/pool/factor 缓存随之失效，
全市场卦象导出退化为每股一次 DuckDB 全量扫描（58.89 分钟 / 7572 标的）。

验收映射：
  #2 逐股票 load_bars：同一 manifest 只建一个 view、一次 raw delta 查询
  #3 QFQ 路径：raw delta 与 factor delta 各至多一次查询
  #4 并发首次加载：8 线程同 manifest 查询次数仍为 1
  #5 LRU 上限 4，淘汰最旧项
  #6 新旧 watermark manifest 同时缓存时各自重放各自数据
  #7 预热后 EOD 写入不被阻塞（无长持读连接/连接池）
  #8 WEEK/MONTH 相同 resolution 只物化一次；不同 resolution 物化两次
  #9 快路径与逐周期路径结果逐字段一致
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from wtpy.apps.astock.data.dataset_store import DatasetStore
from wtpy.apps.astock.data.delta_store import DeltaStore
from wtpy.apps.astock.data.overlay import OverlayView
from wtpy.apps.astock.data.repository import MarketDataRepository

from .conftest import (
    OVERLAY_BASE_DATES,
    _mk_overlay_bar as _mk_bar,
    commit_eod_delta,
)

JSON_PATH = (
    Path(__file__).resolve().parents[3]
    / "wtpy"
    / "apps"
    / "astock"
    / "bagua"
    / "bagua_384.json"
)

SYMBOLS = ["SSE.STK.600000", "SZSE.STK.000001", "SSE.STK.601088"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _count_delta_queries(monkeypatch):
    """Patch DeltaStore 全量可见面加载并返回计数器。"""
    calls = {"bars": 0, "factors": 0}
    orig_bars = DeltaStore.load_all_visible_bars
    orig_factors = DeltaStore.load_all_visible_factors

    def _bars(self, watermark, **kw):
        calls["bars"] += 1
        return orig_bars(self, watermark, **kw)

    def _factors(self, watermark, **kw):
        calls["factors"] += 1
        return orig_factors(self, watermark, **kw)

    monkeypatch.setattr(DeltaStore, "load_all_visible_bars", _bars)
    monkeypatch.setattr(DeltaStore, "load_all_visible_factors", _factors)
    return calls


def _count_view_creations(monkeypatch):
    """Patch OverlayView.for_manifest 统计 manifest view 创建次数。"""
    counter = {"views": 0}
    orig = OverlayView.for_manifest.__func__

    def _for_manifest(cls, store, manifest):
        counter["views"] += 1
        return orig(cls, store, manifest)

    monkeypatch.setattr(OverlayView, "for_manifest", classmethod(_for_manifest))
    return counter


def _rows(dates_close: dict) -> dict:
    out = {}
    for sym, pairs in dates_close.items():
        out[sym] = [
            (d, c - 0.1, c + 0.2, c - 0.2, c, 1000.0, 100000.0)
            for d, c in pairs
        ]
    return out


def _session_warehouse(root, *, etf_snapshot_count: int = 1):
    """长历史 overlay 仓库（通过 BaguaPlaneSession 的孤儿窗口门槛）。

    conftest 的 warehouse 每符号仅 6 行、跨度 7 天，会被
    MIN_ORPHAN_MEDIAN_ROWS=120 / SPAN_DAYS=60 过滤，session 无法索引；
    这里生成 ~140 个自然日的长历史版本。拓扑贴近生产：股票 base 数据集
    与 ETF 独立 tushare/none 数据集分开（生产中 L2 composite 不含 ETF）。

    ``etf_snapshot_count`` 生成多个 ETF 快照代次（旧快照窗口截短、
    created_at 更早），模拟生产中逐日快照导致"最近 cutoff"点时选择
    在不同查询日命中不同代次的场景。
    """
    from wtpy.apps.astock.data.dataset_store import (
        DatasetManifest,
        SymbolRecord,
    )
    from wtpy.apps.astock.data.delta_store import OverlayState, save_overlay_state
    import datetime as _dt

    d0 = _dt.date(2023, 10, 1)
    dates = [
        int((d0 + _dt.timedelta(days=i)).strftime("%Y%m%d"))
        for i in range(140)
    ]
    store = DatasetStore(root)

    def _store_dataset(dataset_id, specs, *, dataset_type="bars", adjustment="none",
                       created_at="2024-02-18T18:00:00", window=None):
        recs = {}
        span = window if window is not None else dates
        for sym, base in specs.items():
            if dataset_type == "bars":
                bars = [
                    _mk_bar(sym, d, base + 0.01 * i)
                    for i, d in enumerate(span)
                ]
                sha = store.store_bars(sym, bars)
                first, last, row_count = span[0], span[-1], len(bars)
            else:  # factor
                sha = store.store_factors(sym, [20230101, 20240101], [1.0, 1.5])
                first, last, row_count = 20230101, 20240101, 2
            recs[sym] = SymbolRecord(
                symbol=sym, blob_sha256=sha, first_date=first,
                last_date=last, row_count=row_count, quality="ok",
            )
        m = DatasetManifest(
            dataset_id=dataset_id, source="tushare",
            adjustment=adjustment, period="1d", dataset_type=dataset_type,
            data_cutoff_date=span[-1], snapshot_date=span[-1],
            provider_version="test", status="ready", created_at=created_at,
        )
        m.symbols = list(recs.values())
        m.symbol_count = len(recs)
        m.row_count = sum(r.row_count for r in recs.values())
        m.expected_symbol_count = len(recs)
        m.imported_symbol_count = len(recs)
        m.coverage_ratio = 1.0
        store.publish(m)
        return m

    stock_specs = {"SSE.STK.600000": 10.0, "SZSE.STK.000001": 5.0}
    # 159096/561830：跨市场裸代码串号回归样本（见 test_bare_etf_codes_never_cross_resolve）
    etf_specs = {
        "SSE.ETF.510300": 4.0,
        "SZSE.ETF.159915": 2.5,
        "SSE.ETF.561830": 1.2,
        "SZSE.ETF.159096": 0.9,
    }
    m = _store_dataset("tushare_none_1d_base_long", stock_specs)
    # 独立 ETF 数据集（与生产拓扑一致：ETF 不进股票 composite）。
    # 多快照时旧代次窗口截短、created_at 更早，模拟逐日快照积累。
    for snap in range(etf_snapshot_count):
        age = etf_snapshot_count - 1 - snap  # 0 = 最新
        _store_dataset(
            f"tushare_none_1d_etf_long_{snap}", etf_specs,
            created_at=f"2024-02-18T18:00:{age:02d}",
            window=dates[: len(dates) - 3 * age] if age else None,
        )
    fac = _store_dataset(
        "tushare_adjfactor_1d_base_long",
        {**stock_specs, **etf_specs},
        dataset_type="factor", adjustment="adj_factor",
        created_at="2024-02-18T18:05:00",
    )

    save_overlay_state(
        root,
        OverlayState(
            enabled=True,
            base_dataset_id=m.dataset_id,
            base_manifest_sha256=m.manifest_sha256,
            factor_base_dataset_id=fac.dataset_id,
            factor_base_manifest_sha256=fac.manifest_sha256,
            delta_watermark=dates[-1],
            factor_watermark=dates[-1],
        ),
    )
    return store, dates


# ---------------------------------------------------------------------------
# repository 层：manifest view LRU + 单次 delta 查询
# ---------------------------------------------------------------------------


class TestManifestViewCache:
    def test_persymbol_loads_share_one_view_and_one_delta_query(
        self, warehouse, monkeypatch
    ):
        """验收 #2：逐股票 load_bars 多 symbol 多轮只建 1 个 view、1 次 delta 查询。"""
        commit_eod_delta(
            warehouse,
            cutoff=20240109,
            rows=_rows({SYMBOLS[0]: [(20240109, 10.8)]}),
            batch_suffix="eod1",
        )
        repo = MarketDataRepository(warehouse)
        l2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        views = _count_view_creations(monkeypatch)
        calls = _count_delta_queries(monkeypatch)
        tails = []
        for _round in range(2):  # 两轮模拟 WEEK+MONTH 双周期重复加载
            for sym in SYMBOLS:
                bars = repo.load_bars(dataset_id=l2.dataset_id, symbol=sym)
                assert bars, sym
                tails.append(bars[-1].trade_date)
        assert views["views"] == 1, "逐股票循环不得重建 manifest view"
        assert calls["bars"] == 1, "delta 全量面只应查询一次"

    def test_qfq_persymbol_single_raw_and_factor_query(
        self, warehouse, monkeypatch
    ):
        """验收 #3：QFQ 逐股票加载 raw/factor delta 各至多一次查询。"""
        commit_eod_delta(
            warehouse,
            cutoff=20240109,
            rows=_rows({SYMBOLS[0]: [(20240109, 10.8)]}),
            factor_rows={"SSE.STK.600000": [(20240109, 1.5)]},
            batch_suffix="eodq",
        )
        repo = MarketDataRepository(warehouse)
        l1 = repo.resolve_latest_ready(
            source="internal",
            adjustment="composite_tushare_factor_qfq",
            period="1d",
        )
        calls = _count_delta_queries(monkeypatch)
        for sym in SYMBOLS:
            for _ in range(2):
                bars = repo.load_bars(dataset_id=l1.dataset_id, symbol=sym)
                assert bars, sym
        assert calls["bars"] <= 1
        assert calls["factors"] <= 1

    def test_concurrent_first_load_runs_delta_query_once(
        self, warehouse, monkeypatch
    ):
        """验收 #4：并发首次加载查询次数为 1 且结果一致。"""
        commit_eod_delta(
            warehouse,
            cutoff=20240109,
            rows=_rows({SYMBOLS[0]: [(20240109, 10.8)]}),
            batch_suffix="eodc",
        )
        repo = MarketDataRepository(warehouse)
        l2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        calls = _count_delta_queries(monkeypatch)
        start = threading.Barrier(8)
        results: list = []
        errors: list = []

        def _worker():
            try:
                start.wait()
                row = {}
                for sym in SYMBOLS:
                    bars = repo.load_bars(dataset_id=l2.dataset_id, symbol=sym)
                    row[sym] = (len(bars), int(bars[-1].trade_date))
                results.append(row)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not errors, errors
        assert len(results) == 8
        assert all(r == results[0] for r in results), "并发读取结果不一致"
        assert calls["bars"] == 1, "single-flight 失效：delta 被重复查询"

    def test_lru_eviction_bound(self, tmp_path, monkeypatch):
        """验收 #5：manifest view 缓存上限 4，淘汰最旧项。"""
        from wtpy.apps.astock.data.repository import _MANIFEST_VIEW_CACHE_MAX

        assert _MANIFEST_VIEW_CACHE_MAX == 4
        repo = MarketDataRepository(DatasetStore(tmp_path))

        stubs = []

        def _fake_for_manifest(cls, store, manifest):
            v = SimpleNamespace(pinned=manifest.dataset_id)
            stubs.append(v)
            return v

        monkeypatch.setattr(
            OverlayView, "for_manifest", classmethod(_fake_for_manifest)
        )

        def _fake_manifest(i):
            return SimpleNamespace(
                dataset_id=f"virt_{i}", manifest_sha256=f"sha{i}", symbols=[]
            )

        for i in range(6):
            repo._overlay_view_for_manifest(_fake_manifest(i))
        keys = list(repo._manifest_views.keys())
        assert len(keys) == _MANIFEST_VIEW_CACHE_MAX == len(repo._manifest_views)
        assert ("virt_0", "sha0") not in keys and ("virt_1", "sha1") not in keys
        assert keys[-1] == ("virt_5", "sha5")
        # 再次访问未淘汰项应命中缓存（不新建）
        n_before = len(stubs)
        repo._overlay_view_for_manifest(_fake_manifest(4))
        assert len(stubs) == n_before

    def test_dual_generation_replay_with_cached_views(
        self, warehouse, monkeypatch
    ):
        """验收 #6：新旧 watermark manifest 同时缓存时各自重放各自数据。"""
        repo = MarketDataRepository(warehouse)
        l_wm1 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        old_bars = repo.load_bars(
            dataset_id=l_wm1.dataset_id, symbol=SYMBOLS[0]
        )
        assert old_bars[-1].trade_date == OVERLAY_BASE_DATES[-1]

        commit_eod_delta(
            warehouse,
            cutoff=20240110,
            rows=_rows({SYMBOLS[0]: [(20240110, 11.0)]}),
            batch_suffix="eod2",
        )
        # 新代次解析出新的虚拟 manifest id（watermark 入 id）
        l_wm2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        assert l_wm2.dataset_id != l_wm1.dataset_id
        new_bars = repo.load_bars(
            dataset_id=l_wm2.dataset_id, symbol=SYMBOLS[0]
        )
        assert new_bars[-1].trade_date == 20240110
        # 旧 manifest 命中的是 pinned 到旧 watermark 的缓存视图：
        # 必须继续重放旧数据，不得看到新 delta 行
        replay_old = repo.load_bars(
            dataset_id=l_wm1.dataset_id, symbol=SYMBOLS[0]
        )
        assert replay_old[-1].trade_date == OVERLAY_BASE_DATES[-1]
        assert len(repo._manifest_views) >= 2

    def test_record_fast_path_matches_variant_lookup(self, warehouse):
        """load_record_bars 与 load_bars(symbol=...) 结果完全一致。"""
        commit_eod_delta(
            warehouse,
            cutoff=20240109,
            rows=_rows({SYMBOLS[1]: [(20240109, 5.4)]}),
            batch_suffix="eodr",
        )
        repo = MarketDataRepository(warehouse)
        l2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        for sym in SYMBOLS:
            via_symbol = repo.load_bars(dataset_id=l2.dataset_id, symbol=sym)
            record = repo._find_symbol_record(l2, sym)
            via_record = repo.load_record_bars(
                manifest=l2, record=record
            )
            assert [
                (b.trade_date, b.close) for b in via_symbol
            ] == [(b.trade_date, b.close) for b in via_record]

    def test_eod_writer_not_blocked_after_warm_reads(self, warehouse):
        """验收 #7（进程内部分）：预热读取后同进程 EOD 写入必须成功。

        本测试只覆盖同进程场景：预热 raw/QFQ 全部读取路径后提交一笔增量，
        并断言 DeltaStore 上没有连接池残留属性。真正的跨进程写锁行为
        （Windows 上常驻 read-only 连接阻止 EOD 子进程读写打开）由
        test_overlay_delta.py 的
        test_read_query_does_not_leave_cross_process_writer_lock
        以真实子进程覆盖，两者互补。
        """
        commit_eod_delta(
            warehouse,
            cutoff=20240109,
            rows=_rows({SYMBOLS[0]: [(20240109, 10.8)]}),
            factor_rows={"SSE.STK.600000": [(20240109, 1.5)]},
            batch_suffix="warm",
        )
        repo = MarketDataRepository(warehouse)
        for adjustment in ("composite_none", "composite_tushare_factor_qfq"):
            ds = repo.resolve_latest_ready(
                source="internal", adjustment=adjustment, period="1d"
            )
            for sym in SYMBOLS:
                assert repo.load_bars(dataset_id=ds.dataset_id, symbol=sym)
        # 连接池不得复活
        assert not hasattr(DeltaStore, "_read_conn_pool")
        assert not hasattr(DeltaStore, "_release_read_conn")
        # 写入成功即证明没有长期 read-only 连接占用文件锁
        batch = commit_eod_delta(
            warehouse,
            cutoff=20240112,
            rows=_rows({SYMBOLS[0]: [(20240112, 11.2)]}),
            batch_suffix="afterwarm",
        )
        assert batch["new_rows"] >= 1


# ---------------------------------------------------------------------------
# service 层：多周期单次物化
# ---------------------------------------------------------------------------


class _FakeResolution:
    def __init__(self, key: tuple):
        self._key = key

    @property
    def materialize_key(self) -> tuple:
        return self._key


class _FakeSession:
    """duck-type BaguaPlaneSession 的 resolve/materialize/build_meta 三步。"""

    def __init__(self, key_by_asof: bool):
        self.key_by_asof = key_by_asof
        self.materialized: list = []

    def resolve_symbol(self, std_code, *, asof=None):
        key = ("k", asof) if self.key_by_asof else ("k", None)
        return _FakeResolution(key)

    def materialize_symbol(self, resolution):
        from wtpy.apps.astock.data.tdx_reader import DayBar

        self.materialized.append(resolution.materialize_key)
        # 覆盖 20240131 与 20240216 附近的日线，供周/月线切片
        return [
            DayBar(date=d, open=10.0, high=10.5, low=9.5, close=10.2,
                   amount=100000.0, volume=1000.0)
            for d in (20240125, 20240126, 20240129, 20240130, 20240131,
                      20240201, 20240205, 20240208, 20240213, 20240214,
                      20240215, 20240216)
        ]

    def build_meta(self, resolution):
        return {
            "dataset_source": "internal",
            "dataset_adjustment": "composite_none",
            "dataset_id": "virt_x",
            "legacy_fallback": False,
        }


class TestMultiperiodSingleMaterialize:
    def _cfg(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MARKET_DATA_ROOT", str(tmp_path))
        from wtpy.apps.astock.config import get_default_config

        return get_default_config()

    def _calc(self):
        from wtpy.apps.astock.bagua.calculator import BaguaCalculator

        if not JSON_PATH.exists():
            pytest.skip("bagua_384.json missing")
        return BaguaCalculator.from_json(JSON_PATH)

    def test_same_key_materializes_once(self, monkeypatch, tmp_path):
        """验收 #8a：WEEK/MONTH 同一物化键只物化一次。"""
        from wtpy.apps.astock.service.bagua_query import (
            _query_bagua_periods_for_code,
        )

        cfg = self._cfg(monkeypatch, tmp_path)
        sess = _FakeSession(key_by_asof=False)
        out = _query_bagua_periods_for_code(
            cfg,
            code="600000",
            asof=20240216,
            periods=["WEEK", "MONTH"],
            adjust="raw",
            session=sess,
            calc=self._calc(),
            asof_map={"WEEK": 20240216, "MONTH": 20240131},
        )
        assert set(out) == {"WEEK", "MONTH"}
        assert all(v.get("ok") for v in out.values()), out
        assert len(sess.materialized) == 1

    def test_different_keys_materialize_twice(self, monkeypatch, tmp_path):
        """验收 #8b：不同 manifest/record 保持两次物化。"""
        from wtpy.apps.astock.service.bagua_query import (
            _query_bagua_periods_for_code,
        )

        cfg = self._cfg(monkeypatch, tmp_path)
        sess = _FakeSession(key_by_asof=True)  # 键含 asof → 两周期必不同
        _query_bagua_periods_for_code(
            cfg,
            code="600000",
            asof=20240216,
            periods=["WEEK", "MONTH"],
            adjust="raw",
            session=sess,
            calc=self._calc(),
            asof_map={"WEEK": 20240216, "MONTH": 20240131},
        )
        assert len(sess.materialized) == 2
        assert sess.materialized[0] != sess.materialized[1]

    def test_result_field_parity_vs_perperiod_path(
        self, monkeypatch, tmp_path
    ):
        """验收 #9：快路径结果与逐周期 query_bagua 逐字段一致。"""
        from wtpy.apps.astock.service.bagua_query import (
            BaguaPlaneSession,
            _query_bagua_periods_for_code,
            query_bagua,
        )

        store, dates = _session_warehouse(tmp_path)
        view = OverlayView(store=DatasetStore(tmp_path))
        l2m = view.l2_virtual_manifest()
        cfg = self._cfg(monkeypatch, tmp_path)
        calc = self._calc()
        sess = BaguaPlaneSession(cfg, "raw")
        # 把持久化的 L2 虚拟面设为正式产品面，走 overlay_v1 物化分支
        sess.formal_l2_id = l2m.dataset_id
        sess._build_index()

        week_asof = dates[-1]
        month_asof = 20240131
        asof_map = {"WEEK": week_asof, "MONTH": month_asof}
        expected = {
            per: query_bagua(
                cfg,
                code="600000",
                date=asof_map[per],
                period=per,
                adjust="raw",
                session=sess,
                calc=calc,
            )
            for per in ("WEEK", "MONTH")
        }
        actual = _query_bagua_periods_for_code(
            cfg,
            code="600000",
            asof=week_asof,
            periods=["WEEK", "MONTH"],
            adjust="raw",
            session=sess,
            calc=calc,
            asof_map=asof_map,
        )
        assert actual == expected
        # 快路径确实生效：两次解析命中同一 manifest/record
        r_week = sess.resolve_symbol("SSE.STK.600000", asof=week_asof)
        r_month = sess.resolve_symbol("SSE.STK.600000", asof=month_asof)
        assert r_week.materialize_key == r_month.materialize_key

    def test_fastpath_failure_falls_back_with_debug_log(
        self, monkeypatch, tmp_path, caplog
    ):
        """快路径内部异常必须回退到逐周期路径，且留下 debug 级别日志。"""
        import logging as _logging

        from wtpy.apps.astock.service.bagua_query import (
            _query_bagua_periods_for_code,
            query_bagua,
        )

        class BoomSession:
            def resolve_symbol(self, std_code, *, asof=None):
                raise RuntimeError("mid-flight boom")

        cfg = self._cfg(monkeypatch, tmp_path)
        calc = self._calc()
        with caplog.at_level(_logging.DEBUG, logger="wtpy.apps.astock.service.bagua_query"):
            out = _query_bagua_periods_for_code(
                cfg,
                code="600000",
                asof=20240216,
                periods=["WEEK"],
                adjust="raw",
                session=BoomSession(),
                calc=calc,
                asof_map={"WEEK": 20240216},
            )
        # 回退路径产出错误行（BoomSession 无 load_symbol，与基线一致）
        assert out["WEEK"]["ok"] is False
        assert any(
            "mid-flight boom" in rec.message and rec.levelno == _logging.DEBUG
            for rec in caplog.records
        ), "快路径回退未留 debug 日志"
        # 对照：单周期入口对同一 session 抛 AttributeError（无 load_symbol），
        # 多周期入口将其转为 ok=False 错误行而非向上抛
        with pytest.raises(AttributeError):
            query_bagua(
                cfg,
                code="600000",
                date="2024-02-16",
                period="WEEK",
                adjust="raw",
                session=BoomSession(),
                calc=calc,
            )

    def test_qfq_stock_parity_and_single_materialize(
        self, monkeypatch, tmp_path
    ):
        """QFQ 股票快路径：与逐周期路径逐字段一致，且 WEEK/MONTH 只物化一次。"""
        from wtpy.apps.astock.service.bagua_query import (
            BaguaPlaneSession,
            _query_bagua_periods_for_code,
            query_bagua,
        )

        _store, dates = _session_warehouse(tmp_path)
        view = OverlayView(store=DatasetStore(tmp_path))
        l1m = view.l1_virtual_manifest()
        cfg = self._cfg(monkeypatch, tmp_path)
        calc = self._calc()
        sess = BaguaPlaneSession(cfg, "tushare_qfq")
        # 把持久化的 L1 虚拟面设为正式产品面，走 overlay_v1 QFQ 派生分支
        sess.formal_l1_id = l1m.dataset_id
        sess._build_index()

        week_asof = dates[-1]
        month_asof = 20240131
        asof_map = {"WEEK": week_asof, "MONTH": month_asof}
        expected = {
            per: query_bagua(
                cfg, code="600000", date=asof_map[per], period=per,
                adjust="tushare_qfq", session=sess, calc=calc,
            )
            for per in ("WEEK", "MONTH")
        }

        calls = {"n": 0}
        orig_materialize = sess.materialize_symbol

        def _counting(res):
            calls["n"] += 1
            return orig_materialize(res)

        sess.materialize_symbol = _counting
        actual = _query_bagua_periods_for_code(
            cfg, code="600000", asof=week_asof, periods=["WEEK", "MONTH"],
            adjust="tushare_qfq", session=sess, calc=calc, asof_map=asof_map,
        )
        assert actual == expected
        assert calls["n"] == 1, "QFQ 快路径未共享物化"
        r_week = sess.resolve_symbol("SSE.STK.600000", asof=week_asof)
        assert getattr(r_week.manifest, "view_type", "") == "l1_virtual_qfq"

    def test_etf_fastpath_parity_and_single_materialize(
        self, monkeypatch, tmp_path
    ):
        """ETF 快路径（独立 ETF 数据集、raw 面）：与逐周期入口逐字段一致，
        且 WEEK/MONTH 只物化一次（生产拓扑中 ETF 不在股票 composite 内）。"""
        from wtpy.apps.astock.service.bagua_query import (
            BaguaPlaneSession,
            _query_bagua_periods_for_code,
            query_bagua,
        )

        _store, dates = _session_warehouse(tmp_path)
        cfg = self._cfg(monkeypatch, tmp_path)
        calc = self._calc()
        week_asof = dates[-1]
        month_asof = 20240131
        asof_map = {"WEEK": week_asof, "MONTH": month_asof}

        calls = {"n": 0}
        orig_materialize = BaguaPlaneSession.materialize_symbol

        def _counting(self, res):
            calls["n"] += 1
            return orig_materialize(self, res)

        monkeypatch.setattr(
            BaguaPlaneSession, "materialize_symbol", _counting
        )
        sess = BaguaPlaneSession(cfg, "raw")
        actual = _query_bagua_periods_for_code(
            cfg, code="510300", asof=week_asof, periods=["WEEK", "MONTH"],
            adjust="tushare_qfq",  # 请求 qfq，ETF 恒按 raw 计算
            session=sess, calc=calc, asof_map=asof_map,
        )
        assert calls["n"] == 1, "ETF 快路径未共享物化"
        expected = {
            per: query_bagua(
                cfg, code="510300", date=asof_map[per], period=per,
                adjust="tushare_qfq", session=sess, calc=calc,
            )
            for per in ("WEEK", "MONTH")
        }
        assert actual == expected

    def test_etf_pinned_surface_single_materialize(
        self, monkeypatch, tmp_path
    ):
        """多快照代次下锚点面钉定：WEEK/MONTH 共享最新代次、只物化一次。

        生产中逐日快照使"最近 cutoff"点时选择在不同查询日命中不同代次
        （MONTH 锚点更近落旧快照）→ 同一 ETF 物化两次。钉定后全部周期
        共享锚点面；价格与卦象和逐周期路径一致，仅非锚点周期的数据集
        溯源字段（adjust_meta.dataset_id 等）按设计指向新面。
        """
        from wtpy.apps.astock.service.bagua_query import (
            BaguaPlaneSession,
            _query_bagua_periods_for_code,
            query_bagua,
        )

        _session_warehouse(tmp_path, etf_snapshot_count=2)
        cfg = self._cfg(monkeypatch, tmp_path)
        calc = self._calc()
        sess = BaguaPlaneSession(cfg, "raw")
        week_asof, month_asof = 20240218, 20240131
        asof_map = {"WEEK": week_asof, "MONTH": month_asof}

        calls = {"n": 0}
        orig_materialize = BaguaPlaneSession.materialize_symbol

        def _counting(self, res):
            calls["n"] += 1
            return orig_materialize(self, res)

        monkeypatch.setattr(
            BaguaPlaneSession, "materialize_symbol", _counting
        )
        actual = _query_bagua_periods_for_code(
            cfg, code="510300", asof=week_asof, periods=["WEEK", "MONTH"],
            adjust="raw", session=sess, calc=calc, asof_map=asof_map,
        )
        assert all(v.get("ok") for v in actual.values())
        assert calls["n"] == 1, "锚点钉定后仍多次物化"
        # 两个周期共享同一最新快照
        ds_ids = {
            v["adjust_meta"]["dataset_id"] for v in actual.values()
        }
        assert len(ds_ids) == 1
        assert ds_ids.pop().startswith("tushare_none_1d_etf_long_1")
        # 价格与卦象和逐周期路径一致（溯源字段允许不同，见 docstring）
        slow_sess = BaguaPlaneSession(cfg, "raw")
        for per in ("WEEK", "MONTH"):
            slow = query_bagua(
                cfg, code="510300", date=asof_map[per], period=per,
                adjust="raw", session=slow_sess, calc=calc,
            )
            assert actual[per]["bar"] == slow["bar"]
            assert actual[per]["bagua"] == slow["bagua"]
            assert actual[per]["summary"] == slow["summary"]

    def test_etf_legacy_shared_load_for_uncovered_periods(
        self, monkeypatch, tmp_path
    ):
        """仓库面不覆盖更早周期时：legacy/TDX 只加载一次、供所有未覆盖周期组装。"""
        from wtpy.apps.astock.data.dataset_store import (
            DatasetManifest,
            SymbolRecord,
        )
        from wtpy.apps.astock.data.tdx_reader import DayBar
        from wtpy.apps.astock.service.bagua_query import (
            BaguaPlaneSession,
            _query_bagua_periods_for_code,
        )

        store, dates = _session_warehouse(tmp_path)
        # 晚上市 ETF：仅在仓库最后 8 天有数据（first > MONTH 查询日）
        late_sym = "SSE.ETF.513100"
        late_dates = dates[-8:]
        bars = [_mk_bar(late_sym, d, 3.0) for d in late_dates]
        sha = store.store_bars(late_sym, bars)
        rec = SymbolRecord(
            symbol=late_sym, blob_sha256=sha, first_date=late_dates[0],
            last_date=late_dates[-1], row_count=len(bars), quality="ok",
        )
        m = DatasetManifest(
            dataset_id="tushare_none_1d_etf_late", source="tushare",
            adjustment="none", period="1d",
            data_cutoff_date=late_dates[-1], snapshot_date=late_dates[-1],
            provider_version="test", status="ready",
            created_at="2024-02-18T18:10:00",
        )
        m.symbols = [rec]
        m.symbol_count = 1
        m.row_count = len(bars)
        m.expected_symbol_count = 1
        m.imported_symbol_count = 1
        m.coverage_ratio = 1.0
        store.publish(m)

        cfg = self._cfg(monkeypatch, tmp_path)
        calc = self._calc()

        legacy_calls = {"n": 0}

        def _fake_legacy(cfg_, std):
            import datetime as _dt

            legacy_calls["n"] += 1
            d0 = _dt.date(2024, 1, 1)
            return [
                DayBar(
                    date=int((d0 + _dt.timedelta(days=i)).strftime("%Y%m%d")),
                    open=3.0, high=3.2, low=2.9, close=3.1,
                    amount=90000.0, volume=900.0,
                )
                for i in range(49)  # 20240101..20240218 连续自然日
            ]

        from wtpy.apps.astock.service import bagua_query as _bq

        monkeypatch.setattr(_bq, "load_index_etf_day_bars", _fake_legacy)

        week_asof, month_asof = 20240218, 20240131
        sess = BaguaPlaneSession(cfg, "raw")
        out = _query_bagua_periods_for_code(
            cfg, code="513100", asof=week_asof, periods=["WEEK", "MONTH"],
            adjust="raw", session=sess, calc=calc,
            asof_map={"WEEK": week_asof, "MONTH": month_asof},
        )
        assert all(v.get("ok") for v in out.values()), out
        # WEEK 命中仓库面；MONTH 仓库不覆盖 → legacy 兜底，且只加载一次
        assert out["WEEK"]["adjust_meta"]["model"] == "warehouse"
        assert out["MONTH"]["adjust_meta"]["model"] == "legacy_day_file"
        assert out["MONTH"]["adjust_meta"]["legacy_fallback"] is True
        assert legacy_calls["n"] == 1, "legacy 后备被逐周期重复加载"

    def test_bare_etf_codes_never_cross_resolve(self, monkeypatch, tmp_path):
        """回归：561830（沪 ETF）与 159096（深 ETF）裸代码不得互相串号。

        历史缺陷：561830 曾被展示成 159096。仓库同时存在两只 ETF 时，
        裸 6 位代码查询必须各自解析到正确市场/品种，normalize 与分类
        规则也不得把 LOF 段误判为 ETF。
        """
        from wtpy.apps.astock.service.bagua_query import BaguaPlaneSession
        from wtpy.apps.astock.service.index_etf import (
            classify_symbol,
            to_index_etf_std_code,
        )

        # normalize / 分类层：裸代码归属正确的市场，且不产生对方形态
        assert classify_symbol("561830") == "etf"
        assert classify_symbol("159096") == "etf"
        assert to_index_etf_std_code("561830") == "SSE.ETF.561830"
        assert to_index_etf_std_code("159096") == "SZSE.ETF.159096"

        _session_warehouse(tmp_path)
        cfg = self._cfg(monkeypatch, tmp_path)
        sess = BaguaPlaneSession(cfg, "raw")
        for bare, expected in (
            ("561830", "SSE.ETF.561830"),
            ("159096", "SZSE.ETF.159096"),
            ("510300", "SSE.ETF.510300"),
            ("159915", "SZSE.ETF.159915"),
        ):
            bars, meta = sess.load_symbol(bare, asof=20240218)
            assert bars, bare
            ds_id = meta.get("dataset_id") or ""
            assert ds_id.startswith("tushare_none_1d_etf_long"), ds_id
            res = sess.resolve_symbol(bare, asof=20240218)
            assert res.record.symbol == expected, (bare, res.record.symbol)


def test_pool_record_defined_exactly_once():
    """AST 钉子：OverlayView._pool_record 在模块内只允许定义一次。

    历史事故：helpers 区残留过旧的线性扫描版 _pool_record，Python 类体
    后定义覆盖前定义，使 O(1) 索引实现静默失效——每符号导出仍线性遍历
    全池约 5000 条。静态计数能在 CI 阶段拦住同类"重复定义覆盖"。
    """
    import ast

    import wtpy.apps.astock.data.overlay as overlay_mod

    src = Path(overlay_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    defs = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_pool_record"
    ]
    assert len(defs) == 1, (
        f"_pool_record 定义了 {len(defs)} 次——后定义会覆盖 O(1) 索引实现"
    )


def test_pool_record_semantics_match_linear_scan():
    """O(1) 实现的查找语义必须与旧线性扫描完全等价。

    命中精确符号返回同一记录对象、未命中返回 None、空池返回 None、
    缓存未填充时经 _pool_records 自动填充——四种情况都不允许因为
    索引化而改变行为。
    """
    recs = [
        SimpleNamespace(symbol=f"SSE.STK.{600000 + i}") for i in range(5)
    ]
    index = {r.symbol: r for r in recs}

    def _view(recs_, index_):
        view = OverlayView.__new__(OverlayView)
        view._cache_lock = threading.Lock()
        view._pool_state_key = lambda: "k"
        view._pool_records_cache = {"k": list(recs_)}
        view._pool_index_cache = {"k": dict(index_)}
        return view

    # 命中：返回索引中的同一对象；未命中：None
    view = _view(recs, index)
    assert view._pool_record(recs[2].symbol) is recs[2]
    assert view._pool_record("SZSE.ETF.159915") is None

    # 空池 -> None（走线性兜底扫描，不得抛异常）
    empty = _view([], {})
    empty._pool_records = lambda: []
    assert empty._pool_record("SSE.STK.600000") is None

    # 索引缓存缺失 -> 经 _pool_records 填充后仍能命中
    cold = OverlayView.__new__(OverlayView)
    cold._cache_lock = threading.Lock()
    cold._pool_state_key = lambda: "k"
    cold._pool_records_cache = {}
    cold._pool_index_cache = {}

    def _fill():
        cold._pool_records_cache["k"] = list(recs)
        cold._pool_index_cache["k"] = dict(index)
        return recs

    cold._pool_records = _fill
    assert cold._pool_record(recs[0].symbol) is recs[0]


def test_no_duplicate_method_definitions_in_data_service():
    """通用钉子：data/service 包内任何类的任何方法不得重复定义。

    _pool_record 事故的推广防护：Python 类体后定义静默覆盖前定义，
    编译期无任何告警——重复定义会让被覆盖的实现（通常是性能优化）
    静默失效。全包扫描一次 ~20 个模块，毫秒级成本。
    """
    import ast
    from collections import Counter

    import wtpy.apps.astock.data.overlay as overlay_mod

    pkg_root = Path(overlay_mod.__file__).resolve().parent.parent / "data"
    svc_root = pkg_root.parent / "service"
    scanned = 0
    for py in sorted(list(pkg_root.glob("*.py")) + list(svc_root.glob("*.py"))):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            scanned += 1
            names = [
                n.name for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            dup = {k: v for k, v in Counter(names).items() if v > 1}
            assert not dup, f"{py.name} 类 {node.name} 重复定义: {dup}"
    assert scanned > 50, f"扫描类数异常偏少({scanned})，测试可能失效"
