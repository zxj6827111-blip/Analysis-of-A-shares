# -*- coding: utf-8 -*-
"""Gate B8: factor_resolution_v1 in the L3 corporate-action gate.

The composite QFQ derivation resolves adjustment factors as
exact_main > exact_supplement > alias_main > alias_supplement (aliases are
BSE pre-migration codes mapped through the PIT universe). The backtest-time
L3 gate and the experiment eligibility preflight must mirror that rule —
otherwise delisted stocks (factors only in the supplement dataset) and BSE
old-code series (factors under the 920 canonical) fail dataset_missing and
the run is No-Go. Offline-only.
"""
from __future__ import annotations

import pytest

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.data.adjustments import (
    build_factor_series_from_dataset,
    formal_adjustment_ready,
)
from wtpy.apps.astock.data.dataset_binding import DatasetBindingError
from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from wtpy.apps.astock.data.pit_universe import InstrumentWindow, PointInTimeUniverse
from wtpy.apps.astock.data.providers.base import MarketBar
from wtpy.apps.astock.data.repository import MarketDataRepository
from wtpy.apps.astock.service.backtest import resolve_market_data_bindings
from wtpy.apps.astock.service.backtest_request import BacktestRequest

DATES = [20000104, 20080115, 20101012, 20151231, 20160104, 20200608, 20260717]

SYM_SURV = "SSE.STK.600000"       # covered by the MAIN factor dataset
SYM_DELISTED = "SSE.STK.600001"   # covered ONLY by the SUPPLEMENT factor dataset
SYM_ALIAS_OLD = "BSE.STK.430001"  # factors live under the 920 canonical (MAIN)
SYM_ALIAS_NEW = "BSE.STK.920001"
SYM_NO_FACTOR = "SZSE.STK.000002"  # in signal/exec datasets, in NO factor set

MAIN_FACTOR_ID = "tushare_adjfactor_1d_b8main"
SUP_FACTOR_ID = "tushare_adjfactor_1d_b8sup"
SIGNAL_ID = "internal_ctsfqfq_1d_b8t1"
EXEC_ID = "internal_composite_none_1d_b8t1"


def _bars(symbol, dates, source, adjustment, base=10.0):
    return [
        MarketBar(
            symbol=symbol, trade_date=d, period="1d",
            open=base + i, high=base + i + 1, low=base + i - 1,
            close=base + i + 0.5, volume=1000.0, amount=10000.0,
            source=source, adjustment=adjustment,
        )
        for i, d in enumerate(dates)
    ]


def _publish_bars(store, dataset_id, source, adjustment, symbols, **extra):
    recs = []
    total = 0
    for sym in symbols:
        sha = store.store_bars(sym, _bars(sym, DATES, source, adjustment))
        recs.append(SymbolRecord(
            symbol=sym, blob_sha256=sha, row_count=len(DATES),
            first_date=DATES[0], last_date=DATES[-1], quality="ok"))
        total += len(DATES)
    m = DatasetManifest(
        dataset_id=dataset_id, source=source, adjustment=adjustment,
        period="1d", status="building", symbols=recs,
        symbol_count=len(recs), row_count=total, **extra,
    )
    store.publish(m)
    return m


def _publish_factors(store, dataset_id, sym_events):
    """sym_events: {symbol: (dates, factors)}"""
    recs = []
    for sym, (dts, facs) in sym_events.items():
        sha = store.store_factors(sym, dts, facs)
        recs.append(SymbolRecord(
            symbol=sym, blob_sha256=sha, row_count=len(dts), quality="ok"))
    m = DatasetManifest(
        dataset_id=dataset_id, source="tushare", adjustment="adj_factor",
        period="1d", status="building", dataset_type="factor",
        symbols=recs, symbol_count=len(recs),
        row_count=sum(len(d) for d, _ in sym_events.values()),
    )
    store.publish(m)
    return m


@pytest.fixture
def sandbox(tmp_path):
    cfg = AStockConfig(
        storage_root=str(tmp_path / "storage"),
        output_root=str(tmp_path / "output"),
        tdx_root=str(tmp_path / "tdx"),
    )
    cfg.ensure_dirs()
    store = DatasetStore(cfg.market_data_root)

    _publish_factors(store, MAIN_FACTOR_ID, {
        SYM_SURV: ([20000104, 20160104], [1.0, 1.25]),
        SYM_ALIAS_NEW: ([20000104], [1.0]),
    })
    _publish_factors(store, SUP_FACTOR_ID, {
        SYM_DELISTED: ([20000104, 20080115], [1.0, 1.1]),
    })
    syms = [SYM_SURV, SYM_DELISTED, SYM_ALIAS_OLD, SYM_NO_FACTOR]
    _publish_bars(store, EXEC_ID, "internal", "composite_none", syms)
    _publish_bars(
        store, SIGNAL_ID, "internal", "composite_tushare_factor_qfq", syms,
        raw_dataset_id=EXEC_ID,
        factor_dataset_id=MAIN_FACTOR_ID,
        provenance={"supplement_factor_dataset_id": SUP_FACTOR_ID},
    )

    pit = PointInTimeUniverse.build(
        [
            InstrumentWindow(
                canonical_symbol=SYM_SURV, ts_code="600000.SH", exchange="SSE",
                board="sse_main", name="surv", list_status="L",
                list_date=20000101, delist_date=None, last_trade_date=None,
            ),
            InstrumentWindow(
                canonical_symbol=SYM_DELISTED, ts_code="600001.SH",
                exchange="SSE", board="sse_main", name="delisted",
                list_status="D", list_date=20000101, delist_date=20101013,
                last_trade_date=20101012,
            ),
            InstrumentWindow(
                canonical_symbol=SYM_ALIAS_NEW, ts_code="920001.BJ",
                exchange="BSE", board="bse", name="alias", list_status="L",
                list_date=20000101, delist_date=None, last_trade_date=None,
                aliases=[SYM_ALIAS_OLD],
            ),
            InstrumentWindow(
                canonical_symbol=SYM_NO_FACTOR, ts_code="000002.SZ",
                exchange="SZSE", board="szse_main", name="nofac",
                list_status="L", list_date=20000101, delist_date=None,
                last_trade_date=None,
            ),
        ],
        cutoff=20260717,
    )
    pit.save(cfg.market_data_root)
    return cfg, store, pit


# ------------------------------------------------- lookup_symbol (alias tier)


class TestLookupSymbolAliasTier:
    def test_alias_lookup_resolves_and_labels(self, sandbox):
        cfg, store, pit = sandbox
        repo = MarketDataRepository(store)
        fm = repo.get_dataset(MAIN_FACTOR_ID)
        s = build_factor_series_from_dataset(
            store, fm, SYM_ALIAS_OLD, DATES, lookup_symbol=SYM_ALIAS_NEW
        )
        assert s.quality == "complete"
        assert s.source == "dataset"
        assert s.std_code == SYM_ALIAS_OLD
        assert f"alias:{SYM_ALIAS_NEW}" in s.source_detail
        ok, _ = formal_adjustment_ready([s])
        assert ok

    def test_without_lookup_still_fails_closed(self, sandbox):
        cfg, store, pit = sandbox
        repo = MarketDataRepository(store)
        fm = repo.get_dataset(MAIN_FACTOR_ID)
        s = build_factor_series_from_dataset(store, fm, SYM_ALIAS_OLD, DATES)
        assert s.quality == "incomplete"
        assert s.source == "dataset_missing"
        ok, _ = formal_adjustment_ready([s])
        assert not ok

    def test_supplement_dataset_covers_delisted(self, sandbox):
        cfg, store, pit = sandbox
        repo = MarketDataRepository(store)
        main = repo.get_dataset(MAIN_FACTOR_ID)
        sup = repo.get_dataset(SUP_FACTOR_ID)
        s_main = build_factor_series_from_dataset(store, main, SYM_DELISTED, DATES)
        assert s_main.quality == "incomplete"
        s_sup = build_factor_series_from_dataset(store, sup, SYM_DELISTED, DATES)
        assert s_sup.quality == "complete"
        assert SUP_FACTOR_ID in s_sup.source_detail


# ------------------------------------------------- binding passthrough


class TestBindingSupplementFactorManifest:
    def _req(self, universe_id):
        return BacktestRequest(
            rule_ids=["r"], period="DAY", hold=5,
            codes=[SYM_SURV, SYM_DELISTED],
            start=20000101, end=20260717,
            signal_data_source="internal",
            signal_adjustment="composite_tushare_factor_qfq",
            dataset_id=SIGNAL_ID,
            execution_data_source="internal",
            execution_dataset_id=EXEC_ID,
            universe_dataset_id=universe_id,
        )

    def test_binding_exposes_supplement_manifest(self, sandbox):
        cfg, store, pit = sandbox
        req = self._req(pit.universe_dataset_id)
        binding = resolve_market_data_bindings(
            cfg, req, req.codes, check_symbol_coverage=False
        )
        supp = binding["supplement_factor_manifest"]
        assert supp is not None
        assert supp.dataset_id == SUP_FACTOR_ID
        assert req.signal_supplement_factor_dataset_id == SUP_FACTOR_ID

    def test_missing_supplement_parent_fails_closed(self, sandbox, tmp_path):
        cfg, store, pit = sandbox
        broken_id = "internal_ctsfqfq_1d_b8broken"
        _publish_bars(
            store, broken_id, "internal", "composite_tushare_factor_qfq",
            [SYM_SURV],
            raw_dataset_id=EXEC_ID,
            factor_dataset_id=MAIN_FACTOR_ID,
            provenance={
                "supplement_factor_dataset_id": "tushare_adjfactor_1d_gone"
            },
        )
        req = self._req(pit.universe_dataset_id)
        req.dataset_id = broken_id
        req.codes = [SYM_SURV]
        with pytest.raises(DatasetBindingError) as ei:
            resolve_market_data_bindings(
                cfg, req, req.codes, check_symbol_coverage=False
            )
        assert ei.value.code == "DATASET_LINEAGE_BROKEN"


# ------------------------------------------------- experiment eligibility


class TestExperimentEligibilityFactorTiers:
    def test_supplement_and_alias_symbols_stay_eligible(self, sandbox):
        cfg, store, pit = sandbox
        from wtpy.apps.astock.service.experiments import (
            _resolve_variant_datasets_and_common_universe,
        )

        _resolved, _exec, info = _resolve_variant_datasets_and_common_universe(
            cfg,
            [{
                "signal_data_source": "internal",
                "signal_adjustment": "composite_tushare_factor_qfq",
                "dataset_id": SIGNAL_ID,
            }],
            requested_codes=[SYM_SURV, SYM_DELISTED, SYM_ALIAS_OLD,
                             SYM_NO_FACTOR],
            execution_data_source="internal",
            execution_dataset_id=EXEC_ID,
            requested_end=None,
            universe_dataset_id=pit.universe_dataset_id,
        )
        eligible = set(info["eligible_codes"])
        assert SYM_SURV in eligible
        assert SYM_DELISTED in eligible, "supplement-tier symbol must be eligible"
        assert SYM_ALIAS_OLD in eligible, "alias-tier symbol must be eligible"
        assert SYM_NO_FACTOR not in eligible
        excl = {e["symbol"]: e["reason"] for e in info["exclusions"]}
        assert "ca_factor_" in excl[SYM_NO_FACTOR]
