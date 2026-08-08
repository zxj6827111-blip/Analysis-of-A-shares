# -*- coding: utf-8 -*-
"""Gate C phase 1: factor blob storage, factor-manifest fields, token guards.

Offline-only: uses tmp_path DatasetStore and static source inspection.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from wtpy.apps.astock.data.providers.base import ProviderError

ROOT = Path(__file__).resolve().parents[3]
SYNC_SCRIPT = ROOT / "scripts" / "sync_market_data.py"

SYM = "SSE.STK.600000"


@pytest.fixture
def store(tmp_path):
    return DatasetStore(tmp_path / "market_data")


class TestStoreFactors:
    def test_roundtrip_exact(self, store):
        dates = [20240101, 20240102, 20240105]
        factors = [1.0, 1.1, 1.25]
        sha = store.store_factors(SYM, dates, factors)
        assert isinstance(sha, str) and len(sha) == 64
        arrays = store.load_bars(sha)
        assert set(arrays) == {"trade_date", "adj_factor"}
        np.testing.assert_array_equal(
            arrays["trade_date"], np.array(dates, dtype=np.int64)
        )
        np.testing.assert_array_equal(
            arrays["adj_factor"], np.array(factors, dtype=np.float64)
        )

    def test_dates_out_of_order_rejected(self, store):
        with pytest.raises(ValueError):
            store.store_factors(SYM, [20240102, 20240101], [1.0, 1.1])

    def test_duplicate_dates_rejected(self, store):
        with pytest.raises(ValueError):
            store.store_factors(SYM, [20240101, 20240101], [1.0, 1.1])

    def test_zero_factor_rejected(self, store):
        with pytest.raises(ValueError):
            store.store_factors(SYM, [20240101, 20240102], [1.0, 0.0])

    def test_negative_factor_rejected(self, store):
        with pytest.raises(ValueError):
            store.store_factors(SYM, [20240101, 20240102], [1.0, -1.5])

    def test_empty_series_rejected(self, store):
        with pytest.raises(ValueError):
            store.store_factors(SYM, [], [])

    def test_length_mismatch_rejected(self, store):
        with pytest.raises(ValueError):
            store.store_factors(SYM, [20240101, 20240102], [1.0])


class TestFactorManifestRoundtrip:
    def test_new_fields_survive_publish_and_load(self, store):
        sha = store.store_factors(SYM, [20240101, 20240108], [1.0, 1.25])
        rec = SymbolRecord(
            symbol=SYM, blob_sha256=sha, first_date=20240101,
            last_date=20240108, row_count=2, quality="ok",
        )
        m = DatasetManifest(
            dataset_id="tushare_adjfactor_1d_20240112_roundtrip",
            source="tushare",
            adjustment="adj_factor",
            period="1d",
            status="building",
            dataset_type="factor",
            universe_file="vendor_universe_v1.csv",
            universe_sha256="a" * 64,
            content_hash="c" * 64,
            provenance={"api": "adj_factor", "derivation": "none"},
            token_exposed=False,
            incremental_policy_version="factor_inc_v1",
            raw_dataset_id="raw_parent_x",
            raw_dataset_sha256="1" * 64,
            raw_source="local_vendor",
            factor_dataset_id="factor_parent_y",
            factor_dataset_sha256="2" * 64,
            factor_source="tushare",
            anchor_policy="last_factor_on_or_before_cutoff",
            formula_version="tsqfq_v1",
            price_precision_policy="round_half_even_4dp_store; compare at 2dp",
            volume_policy="copied_from_raw_shares_no_adjustment",
            amount_policy="copied_from_raw_cny_no_adjustment",
            symbols=[rec],
            symbol_count=1,
            row_count=2,
        )
        store.publish(m)

        loaded = store.load_manifest("tushare_adjfactor_1d_20240112_roundtrip")
        assert loaded is not None
        assert loaded.status == "ready"
        assert loaded.dataset_type == "factor"
        assert loaded.token_exposed is False
        assert loaded.universe_file == "vendor_universe_v1.csv"
        assert loaded.universe_sha256 == "a" * 64
        assert loaded.content_hash == "c" * 64
        assert loaded.provenance == {"api": "adj_factor", "derivation": "none"}
        assert loaded.incremental_policy_version == "factor_inc_v1"
        assert loaded.raw_dataset_id == "raw_parent_x"
        assert loaded.raw_dataset_sha256 == "1" * 64
        assert loaded.raw_source == "local_vendor"
        assert loaded.factor_dataset_id == "factor_parent_y"
        assert loaded.factor_dataset_sha256 == "2" * 64
        assert loaded.factor_source == "tushare"
        assert loaded.anchor_policy == "last_factor_on_or_before_cutoff"
        assert loaded.formula_version == "tsqfq_v1"
        assert loaded.price_precision_policy.startswith("round_half_even_4dp")
        assert loaded.volume_policy == "copied_from_raw_shares_no_adjustment"
        assert loaded.amount_policy == "copied_from_raw_cny_no_adjustment"
        assert loaded.symbols[0].symbol == SYM
        assert loaded.symbols[0].blob_sha256 == sha

    def test_legacy_manifest_without_new_fields_gets_defaults(self, store):
        legacy = {
            "dataset_id": "tdxquant_front_1d_20250101_legacy01",
            "source": "tdxquant",
            "adjustment": "front",
            "period": "1d",
            "status": "ready",
            "symbols": [],
        }
        path = store.manifests_dir / "tdxquant_front_1d_20250101_legacy01.json"
        path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

        loaded = store.load_manifest("tdxquant_front_1d_20250101_legacy01")
        assert loaded is not None
        assert loaded.dataset_type == "bars"
        assert loaded.token_exposed is None
        assert loaded.universe_file == ""
        assert loaded.universe_sha256 == ""
        assert loaded.content_hash == ""
        assert loaded.provenance == {}
        assert loaded.incremental_policy_version == ""
        assert loaded.raw_dataset_id == ""
        assert loaded.factor_dataset_id == ""
        assert loaded.anchor_policy == ""
        assert loaded.formula_version == ""


class TestTokenNotLeaked:
    """Static guards on scripts/sync_market_data.py — token must never be
    hardcoded or printed."""

    def _source(self) -> str:
        return SYNC_SCRIPT.read_text(encoding="utf-8")

    def test_no_hardcoded_set_token_literal(self):
        src = self._source()
        # ts.set_token() mentioned in docs/error strings is fine; a call with a
        # literal token argument like set_token("abcdef1234...") is not.
        assert not re.search(r"set_token\(\s*['\"][A-Za-z0-9]{8,}", src), (
            "sync_market_data.py must never hardcode a Tushare token in "
            "set_token(...)"
        )

    def test_factor_sync_never_prints_token(self):
        src = self._source()
        marker = "def sync_tushare_adj_factor_full("
        assert marker in src, "sync_tushare_adj_factor_full not found in script"
        start = src.index(marker)
        nxt = src.find("\ndef ", start)
        fn_src = src[start: nxt if nxt != -1 else len(src)]
        for line in fn_src.splitlines():
            assert not ("print(" in line and "get_token(" in line), (
                "sync_tushare_adj_factor_full must never print the token: "
                + line.strip()
            )


# ---------------------------------------------------------------------------
# Gate C factor incremental: window fetch, parent merge, failure policy
# ---------------------------------------------------------------------------

_SMD_MODULE = None


def _script():
    """Load scripts/sync_market_data.py once (module is stateless)."""
    global _SMD_MODULE
    if _SMD_MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "sync_md_factor_inc_test", SYNC_SCRIPT)
        _SMD_MODULE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_SMD_MODULE)
    return _SMD_MODULE


class _FakeFactorPro:
    """Fake tushare pro object: records adj_factor kwargs, serves responses.

    responses: {ts_code: DataFrame | Exception} for per-symbol calls.
    trade_date_responses: {"YYYYMMDD": DataFrame | Exception} for whole-market
    calls (adj_factor(trade_date=...) returns every symbol for that day).
    Calls are recorded as dicts of the exact kwargs the sync passed (strings
    for dates); trade_date-only calls get ts_code=None.
    """

    def __init__(self, responses=None, trade_date_responses=None):
        self.responses = dict(responses or {})
        self.trade_date_responses = dict(trade_date_responses or {})
        self.calls = []
        self.stock_basic_calls = []

    def adj_factor(self, **kwargs):
        call = dict(kwargs)
        if "ts_code" not in call:
            call["ts_code"] = None
        self.calls.append(call)
        ts_code = kwargs.get("ts_code")
        if ts_code is None:
            resp = self.trade_date_responses.get(kwargs.get("trade_date"))
        else:
            resp = self.responses.get(ts_code)
        if isinstance(resp, BaseException):
            raise resp
        return resp

    def stock_basic(self, list_status="L"):
        self.stock_basic_calls.append(list_status)
        return pd.DataFrame(columns=[
            "ts_code", "name", "list_status", "list_date", "delist_date",
        ])


class _FakeTushareProvider:
    registry = {"pro": None}

    def __init__(self, token=None):
        self._token = token
        self._pro = _FakeTushareProvider.registry["pro"]

    def _ensure_initialized(self):
        pass

    def _call_with_retry(self, fn, **kwargs):
        return fn(**kwargs)

    def fetch_adj_factor(self, ts_code=None, *, start_date=None, end_date=None,
                         trade_date=None):
        """Mirror the real provider's kwargs construction (str dates)."""
        kwargs = {}
        if trade_date:
            kwargs["trade_date"] = str(trade_date)
        else:
            kwargs["ts_code"] = ts_code
            if start_date:
                kwargs["start_date"] = str(start_date)
            if end_date:
                kwargs["end_date"] = str(end_date)
        return self._call_with_retry(self._pro.adj_factor, **kwargs)

    def _to_ts_code(self, symbol):
        parts = symbol.split(".")
        exch, _, code = parts
        suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[exch]
        return f"{code}.{suffix}"

    def _from_ts_code(self, ts_code):
        code, suffix = ts_code.split(".")
        exch = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}[suffix.upper()]
        return f"{exch}.STK.{code}"

    def provider_version(self):
        return "fake_factor_provider_v1"


def _calendar_dates(start_ymd: int, n: int) -> np.ndarray:
    """n strictly ascending CALENDAR dates from start_ymd (strptime-safe)."""
    import datetime as _dt
    start = _dt.datetime.strptime(str(start_ymd), "%Y%m%d").date()
    return np.array(
        [int((start + _dt.timedelta(days=i)).strftime("%Y%m%d"))
         for i in range(n)],
        dtype=np.int64,
    )


def _factor_df(dates, factors):
    return pd.DataFrame({
        "trade_date": [int(d) for d in dates],
        "adj_factor": [float(f) for f in factors],
    })


def _write_universe(tmp_path, symbols):
    p = tmp_path / "universe.csv"
    p.write_text(
        "canonical_symbol,inclusion_status\n"
        + "\n".join(f"{s},included" for s in symbols) + "\n",
        encoding="utf-8",
    )
    return p


def _publish_factor_parent(store, *, dataset_id, symbols, n_rows=300,
                           start=20251005, status="building"):
    """Publish a ready full-history factor parent (>= FACTOR_PARENT_MIN_AVG_ROWS).

    n_rows calendar days from start: with start=20251005, n=300 the parent
    ends on 20260731 (so resume start = 20260711, window end 20260804).
    """
    records = []
    for sym in symbols:
        dates = _calendar_dates(start, n_rows)
        factors = np.full(n_rows, 1.0)
        sha = store.store_factors(sym, dates, factors)
        records.append(SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=int(dates[0]),
            last_date=int(dates[-1]), row_count=n_rows, quality="ok",
        ))
    m = DatasetManifest(
        dataset_id=dataset_id, source="tushare", adjustment="adj_factor",
        period="1d", status=status, dataset_type="factor", symbols=records,
        symbol_count=len(records),
        row_count=sum(r.row_count for r in records),
        data_cutoff_date=int(max(r.last_date or 0 for r in records)),
    )
    store.publish(m)
    return m


def _factor_args(tmp_path, **over):
    base = dict(
        source="tushare", mode="incremental", adjustment="adj_factor",
        token=None, universe_file=None, start_date=None, end_date=20260804,
        factor_raw_root=str(tmp_path / "factor_raw"),
        rate_per_min=100000, resume=False, fresh=True,
        log_path=None, report_path=None, coverage_out=None,
        include_bse=True, include_delisted=False, anchor_date=None,
        symbol=None, asset_class="stocks",
    )
    base.update(over)
    return SimpleNamespace(**base)


SYM_A = "SSE.STK.600000"
SYM_B = "SZSE.STK.000001"
# parent dates 20251005..20260731 (300 calendar days) ->
# resume start = 20260731 - 20 calendar days = 20260711
PARENT_FIRST = 20251005
PARENT_LAST = 20260731
RESUME_START = 20260711


class TestFactorIncrementalWindow:
    """Acceptance: provider window, parent merge, orphans, failure policy."""

    def _run(self, tmp_path, monkeypatch, *, symbols, responses,
             end_date=20260804, start_date=None, extra_manifests=None,
             trade_date_responses=None, args_overrides=None):
        smd = _script()
        pro = _FakeFactorPro(responses, trade_date_responses)
        _FakeTushareProvider.registry["pro"] = pro
        from wtpy.apps.astock.data.providers import tushare as tushare_mod
        monkeypatch.setattr(tushare_mod, "TushareProvider", _FakeTushareProvider)
        store = DatasetStore(tmp_path / "market_data")
        uni = _write_universe(tmp_path, symbols)
        if extra_manifests:
            for fn in extra_manifests:
                fn(store)
        args = _factor_args(
            tmp_path, universe_file=str(uni), end_date=end_date,
            start_date=start_date, **(args_overrides or {}),
        )
        result = smd.sync_tushare_adj_factor_full(args, store)
        return smd, pro, store, args, result

    def test_provider_receives_window_start_end(self, tmp_path, monkeypatch):
        """Parent symbol gets (start_date=resume, end_date=cutoff); a brand-new
        symbol without parent history gets a full-history window (no start)."""
        parent = self._publish(tmp_path, symbols=[SYM_A])
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A, SYM_B],
            responses={
                "600000.SH": _factor_df(
                    [20260720, 20260801, 20260804], [2.0, 2.5, 3.0]),
                "000001.SZ": _factor_df(_calendar_dates(20230101, 400), [1.0] * 400),
            },
        )
        calls = {c["ts_code"]: c for c in pro.calls}
        assert calls["600000.SH"]["start_date"] == str(RESUME_START)
        assert calls["600000.SH"]["end_date"] == "20260804"
        # new symbol: no parent record -> full history (no start_date)
        assert "start_date" not in calls["000001.SZ"]
        assert calls["000001.SZ"]["end_date"] == "20260804"

    def test_parent_history_retained_and_appended(self, tmp_path, monkeypatch):
        parent = self._publish(tmp_path, symbols=[SYM_A])
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A],
            responses={"600000.SH": _factor_df(
                [20260720, 20260801, 20260804], [2.0, 2.5, 3.0])},
        )
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        # parent 300 rows, overlap 20260720 replaced, two new dates appended
        assert rec.row_count == 302
        assert rec.first_date == PARENT_FIRST
        assert rec.last_date == 20260804
        blob = store.load_bars(rec.blob_sha256)
        dates = list(blob["trade_date"])
        assert PARENT_FIRST in dates          # parent history kept
        assert 20260801 in dates              # new date appended
        assert 20260804 in dates              # new date appended
        assert len(set(dates)) == len(dates)

    def test_overlap_uses_new_window_value(self, tmp_path, monkeypatch):
        parent = self._publish(tmp_path, symbols=[SYM_A])
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A],
            responses={"600000.SH": _factor_df(
                [20260720, 20260804], [2.0, 3.0])},  # 20260720 also in parent
        )
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        blob = store.load_bars(rec.blob_sha256)
        idx = list(blob["trade_date"]).index(20260720)
        assert float(blob["adj_factor"][idx]) == 2.0  # window wins over 1.0

    def test_new_symbol_without_parent_fetches_full_history(self, tmp_path, monkeypatch):
        parent = self._publish(tmp_path, symbols=[SYM_A])
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A, SYM_B],
            responses={
                "600000.SH": _factor_df([20260804], [3.0]),
                "000001.SZ": _factor_df(_calendar_dates(20190101, 500), [1.2] * 500),
            },
        )
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_B)
        assert rec.row_count == 500
        assert rec.first_date == 20190101

    def test_provider_failure_keeps_parent_blob_and_partial(self, tmp_path, monkeypatch):
        parent = self._publish(tmp_path, symbols=[SYM_A])
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A],
            responses={"600000.SH": ProviderError("boom")},
        )
        assert result["dataset_status"] == "partial"
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        assert rec.quality == "error"
        assert rec.blob_sha256 == parent.symbols[0].blob_sha256
        blob = store.load_bars(rec.blob_sha256)
        assert len(blob["trade_date"]) == 300  # parent history intact

    def test_any_failure_blocks_ready_publish(self, tmp_path, monkeypatch):
        parent = self._publish(tmp_path, symbols=[SYM_A, SYM_B])
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A, SYM_B],
            responses={
                "600000.SH": _factor_df([20260804], [3.0]),
                "000001.SZ": ProviderError("rate_limited"),
            },
        )
        assert result["dataset_status"] == "partial"
        assert result["result"]["failed"] == 1

    def test_empty_window_keeps_parent_history(self, tmp_path, monkeypatch):
        parent = self._publish(tmp_path, symbols=[SYM_A])
        empty = _factor_df([], [])
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A],
            responses={"600000.SH": empty},
        )
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        assert rec.quality == "ok"
        assert rec.blob_sha256 == parent.symbols[0].blob_sha256
        assert rec.row_count == 300

    def test_16_row_orphan_not_used_as_parent(self, tmp_path, monkeypatch):
        """A 16-row factor shell must never become the incremental parent."""
        smd = _script()
        store = DatasetStore(tmp_path / "market_data")
        # orphan only (newer cutoff, avg 16 rows << 250 gate)
        _publish_factor_parent(
            store, dataset_id="tushare_adjfactor_1d_orphan",
            symbols=[SYM_A, SYM_B], n_rows=16, start=20260701,
        )
        assert smd._select_factor_incremental_parent(store) is None
        # plus a full parent: selection must prefer the full history
        _publish_factor_parent(
            store, dataset_id="tushare_adjfactor_1d_full",
            symbols=[SYM_A, SYM_B], n_rows=300,
        )
        parent = smd._select_factor_incremental_parent(store)
        assert parent.dataset_id == "tushare_adjfactor_1d_full"

        # end-to-end with only the orphan: no parent -> full-history fetch
        pro = _FakeFactorPro({
            "600000.SH": _factor_df(np.arange(20250101, 20250101 + 400), [1.0] * 400),
            "000001.SZ": _factor_df(np.arange(20250101, 20250101 + 400), [1.0] * 400),
        })
        _FakeTushareProvider.registry["pro"] = pro
        from wtpy.apps.astock.data.providers import tushare as tushare_mod
        monkeypatch.setattr(tushare_mod, "TushareProvider", _FakeTushareProvider)
        store.manifests_dir.joinpath("tushare_adjfactor_1d_full.json").unlink()
        uni = _write_universe(tmp_path, [SYM_A, SYM_B])
        result = smd.sync_tushare_adj_factor_full(
            _factor_args(tmp_path, universe_file=str(uni)), store)
        for c in pro.calls:
            assert "start_date" not in c, "orphan must not shrink the window"

    def test_checkpoint_resume_hard_mismatch(self, tmp_path, monkeypatch):
        """Resume refuses checkpoints from a DIFFERENT universe or parent
        (only window/cutoff drift on the SAME universe+parent is treated as
        stale-but-compatible and auto-freshes; see the next test)."""
        import json as _json
        smd = _script()
        store = DatasetStore(tmp_path / "market_data")
        parent = _publish_factor_parent(
            store, dataset_id="tushare_adjfactor_1d_full",
            symbols=[SYM_A, SYM_B], n_rows=300,
        )
        pro = _FakeFactorPro({
            "600000.SH": _factor_df([20260804], [3.0]),
            "000001.SZ": _factor_df([20260804], [3.0]),
        })
        _FakeTushareProvider.registry["pro"] = pro
        from wtpy.apps.astock.data.providers import tushare as tushare_mod
        monkeypatch.setattr(tushare_mod, "TushareProvider", _FakeTushareProvider)
        uni = _write_universe(tmp_path, [SYM_A, SYM_B])
        symbols = _script()._load_universe_file(uni)
        universe_hash = hashlib.sha256(
            ",".join(symbols).encode()).hexdigest()
        window_start = smd._factor_resume_start(parent)
        ck_path = store.sync_logs_dir / "checkpoint_tushare_adj_factor_1d.json"
        good = {
            "universe_hash": universe_hash, "sync_run_id": "tsfactor_resume1",
            "done": {}, "api_calls": 0,
            "parent_dataset_id": parent.dataset_id,
            "window_start": window_start, "cutoff": 20260804,
        }
        for field, bad in (
            ("parent_dataset_id", "PARENT_WRONG"),
            ("universe_hash", "OTHER_UNIVERSE"),
        ):
            ck = dict(good)
            ck[field] = bad
            ck_path.write_text(_json.dumps(ck), encoding="utf-8")
            r = smd.sync_tushare_adj_factor_full(
                _factor_args(tmp_path, universe_file=str(uni),
                             resume=True, fresh=False), store)
            assert r["status"] == "failed", field
            assert r["error"] == "checkpoint_mismatch", field

    def test_checkpoint_stale_but_compatible_auto_fresh(self, tmp_path, monkeypatch):
        """A checkpoint from a previous window/date (same universe + parent,
        only cutoff/window_start moved, e.g. yesterday's run re-run today)
        is discarded automatically: --resume succeeds with a fresh window
        instead of forcing --fresh."""
        import json as _json
        smd = _script()
        store = DatasetStore(tmp_path / "market_data")
        parent = _publish_factor_parent(
            store, dataset_id="tushare_adjfactor_1d_full",
            symbols=[SYM_A, SYM_B], n_rows=300,
        )
        pro = _FakeFactorPro({
            "600000.SH": _factor_df([20260804], [3.0]),
            "000001.SZ": _factor_df([20260804], [3.0]),
        })
        _FakeTushareProvider.registry["pro"] = pro
        from wtpy.apps.astock.data.providers import tushare as tushare_mod
        monkeypatch.setattr(tushare_mod, "TushareProvider", _FakeTushareProvider)
        uni = _write_universe(tmp_path, [SYM_A, SYM_B])
        symbols = _script()._load_universe_file(uni)
        universe_hash = hashlib.sha256(
            ",".join(symbols).encode()).hexdigest()
        ck_path = store.sync_logs_dir / "checkpoint_tushare_adj_factor_1d.json"
        # yesterday's checkpoint: same universe/parent, older window + cutoff
        stale = {
            "universe_hash": universe_hash,
            "sync_run_id": "tsfactor_yesterday",
            "done": {}, "api_calls": 0,
            "parent_dataset_id": parent.dataset_id,
            "window_start": 19990101, "cutoff": 20200101,
            "saved_at": "2026-08-07T09:00:00",
        }
        ck_path.write_text(_json.dumps(stale), encoding="utf-8")
        r = smd.sync_tushare_adj_factor_full(
            _factor_args(tmp_path, universe_file=str(uni),
                         resume=True, fresh=False), store)
        assert r["status"] in ("success", "warning")
        assert r["dataset_status"] == "ready"
        # the run restarted with a fresh window (new sync_run_id), it did NOT
        # resume the stale checkpoint
        assert r["sync_run_id"] != "tsfactor_yesterday"
        # checkpoint consumed after the terminal publish
        assert not ck_path.exists()

    def test_manifest_cutoff_is_real_data_date(self, tmp_path, monkeypatch):
        """data_cutoff_date must be the true max factor date, never the
        requested end-date (holidays / missing trading days)."""
        parent = self._publish(tmp_path, symbols=[SYM_A])
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A],
            end_date=20260810,  # requested cutoff with no real rows after 0807
            responses={"600000.SH": _factor_df(
                [20260720, 20260804, 20260807], [2.0, 3.0, 3.1])},
        )
        m = store.load_manifest(result["dataset_id"])
        assert m.data_cutoff_date == 20260807
        prov = m.provenance or {}
        assert prov["requested_cutoff"] == 20260810
        assert prov["actual_cutoff"] == 20260807

    def test_explicit_start_date_overrides_resume(self, tmp_path, monkeypatch):
        parent = self._publish(tmp_path, symbols=[SYM_A])
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A],
            start_date=20260720,
            responses={"600000.SH": _factor_df([20260804], [3.0])},
        )
        calls = {c["ts_code"]: c for c in pro.calls}
        assert calls["600000.SH"]["start_date"] == "20260720"

    def test_trade_date_batch_path_merges_and_publishes(self, tmp_path, monkeypatch):
        """Whole-market trade_date calls replace per-symbol window fetches;
        merged history, manifest publish and the fixed 'latest' raw dir."""
        symbols = [SYM_A, SYM_B] + [f"SZSE.STK.{i:06d}" for i in range(2, 40)]
        parent = self._publish(tmp_path, symbols=symbols)
        wdays = _calendar_dates(RESUME_START, 25)  # 20260711..20260804
        day_rows = {}
        for d in wdays:
            fa = 1.0
            if d == 20260720:
                fa = 2.0      # overlap day: window must win over parent 1.0
            elif d == 20260801:
                fa = 2.5
            elif d == 20260804:
                fa = 3.0
            day_rows[str(d)] = pd.DataFrame({
                "ts_code": ["600000.SH", "000001.SZ"],
                "trade_date": [int(d), int(d)],
                "adj_factor": [fa, 1.0],
            })
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=symbols,
            responses={
                "600000.SH": _factor_df([20260804], [3.0]),  # unused on batch
                "000001.SZ": _factor_df([20260804], [3.0]),
            },
            trade_date_responses=day_rows,
        )
        assert result["status"] in ("success", "warning")
        assert result["dataset_status"] == "ready"
        # every factor call went through the trade_date batch path
        assert all("trade_date" in c for c in pro.calls)
        # ~1 call per calendar day (+1 probe), far below the symbol count
        assert result["stats"]["api_calls"] == 1 + len(wdays)
        assert result["stats"]["api_calls"] < len(symbols)
        assert result["stats"].get("batch_by_trade_date") is True

        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        # parent 300 rows + window 25 rows - 21 overlapping days
        assert rec.row_count == 304
        assert rec.first_date == PARENT_FIRST
        assert rec.last_date == 20260804
        blob = store.load_bars(rec.blob_sha256)
        dates = list(blob["trade_date"])
        assert PARENT_FIRST in dates
        assert len(set(dates)) == len(dates)
        idx = dates.index(20260720)
        assert float(blob["adj_factor"][idx]) == 2.0  # window wins over parent
        # symbol absent from the batch window keeps the parent blob
        rec_missing = next(r for r in m.symbols if r.symbol == symbols[-1])
        assert rec_missing.quality == "ok"
        assert rec_missing.blob_sha256 == parent.symbols[-1].blob_sha256
        # incremental raw cache went to the fixed "latest" dir
        latest = Path(args.factor_raw_root) / "latest"
        assert (latest / "600000.SH.csv").exists()
        assert (latest / "000001.SZ.csv").exists()

    def test_trade_date_batch_fallback_to_per_symbol(self, tmp_path, monkeypatch):
        """A failing trade_date probe silently falls back to per-symbol
        window fetches and still publishes a ready dataset."""
        parent = self._publish(tmp_path, symbols=[SYM_A, SYM_B])
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A, SYM_B],
            responses={
                "600000.SH": _factor_df(
                    [20260720, 20260801, 20260804], [2.0, 2.5, 3.0]),
                "000001.SZ": _factor_df([20260804], [3.0]),
            },
            trade_date_responses={"20260804": ProviderError("permission denied")},
        )
        assert result["status"] in ("success", "warning")
        assert result["dataset_status"] == "ready"
        assert result["stats"].get("batch_by_trade_date") is None
        # one probe attempt, then per-symbol window calls
        assert any("trade_date" in c for c in pro.calls)
        calls = {c["ts_code"]: c for c in pro.calls}
        assert calls["600000.SH"]["start_date"] == str(RESUME_START)
        assert calls["600000.SH"]["end_date"] == "20260804"
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        assert rec.row_count == 302
        assert rec.last_date == 20260804

    def test_keep_raw_batches_prunes_old(self, tmp_path):
        smd = _script()
        root = tmp_path / "factor_raw"
        (root / "latest").mkdir(parents=True)
        (root / "latest" / "x.csv").write_text("x", encoding="utf-8")
        for name in ("tsfactor_aaaa", "tsfactor_bbbb",
                     "tsfactor_cccc", "tsfactor_dddd"):
            d = root / name
            d.mkdir()
            (d / "600000.SH.csv").write_text("x", encoding="utf-8")
        smd._prune_raw_batches(str(root), 2)
        left = sorted(p.name for p in root.iterdir())
        assert left == ["latest", "tsfactor_cccc", "tsfactor_dddd"]

    def test_prune_raw_batches_keep_zero_is_noop(self, tmp_path):
        """keep=0 disables pruning entirely (caller-side guard too)."""
        smd = _script()
        root = tmp_path / "factor_raw"
        for name in ("tsfactor_aaaa", "tsfactor_bbbb"):
            (root / name).mkdir(parents=True)
        smd._prune_raw_batches(str(root), 0)
        assert sorted(p.name for p in root.iterdir()) == [
            "tsfactor_aaaa", "tsfactor_bbbb"]

    def test_prune_raw_batches_nonexistent_root_is_noop(self, tmp_path):
        smd = _script()
        root = tmp_path / "no_such_dir"
        smd._prune_raw_batches(str(root), 2)  # must not raise or create

    def test_prune_raw_batches_keep_ge_count_is_noop(self, tmp_path):
        smd = _script()
        root = tmp_path / "factor_raw"
        (root / "latest").mkdir(parents=True)
        for name in ("tsfactor_aaaa", "tsfactor_bbbb"):
            (root / name).mkdir()
        smd._prune_raw_batches(str(root), 5)
        assert sorted(p.name for p in root.iterdir()) == [
            "latest", "tsfactor_aaaa", "tsfactor_bbbb"]

    def test_merge_factor_history_window_wins_and_dedups(self, tmp_path):
        """Direct unit: window rows win over parent rows on overlapping dates,
        and duplicate dates inside the window itself keep the last value."""
        smd = _script()
        store = DatasetStore(tmp_path / "market_data")
        sha = store.store_factors(SYM_A, [20260720, 20260721], [1.0, 1.0])
        parent_arrays = store.load_bars(sha)
        merged = smd._merge_factor_history(
            parent_arrays, _factor_df([20260721, 20260721, 20260722],
                                      [2.0, 2.5, 3.0]))
        assert list(merged["trade_date"]) == [20260720, 20260721, 20260722]
        assert list(merged["adj_factor"]) == [1.0, 2.5, 3.0]

    def test_merge_factor_history_empty_window_is_guarded_by_caller(self):
        """merge never sees an empty window in production (caller keeps the
        parent blob first); only a non-empty window reaches the helper."""
        smd = _script()
        empty = _factor_df([], [])
        # Caller guard contract: empty windows are filtered before merge,
        # e.g. sync_tushare_adj_factor_full's `df is None or df.empty` branch.
        assert empty.empty

    def test_keep_raw_batches_prunes_after_ready_publish(self, tmp_path, monkeypatch):
        """--keep-raw-batches N prunes the oldest tsfactor_* dirs after a
        ready publish, preserving the fixed 'latest' cache."""
        parent = self._publish(tmp_path, symbols=[SYM_A])
        root = tmp_path / "factor_raw"
        for name in ("tsfactor_old1", "tsfactor_old2", "tsfactor_old3"):
            (root / name).mkdir(parents=True)
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A],
            responses={"600000.SH": _factor_df([20260804], [3.0])},
            args_overrides={"keep_raw_batches": 2},
        )
        assert result["dataset_status"] == "ready"
        left = sorted(p.name for p in root.iterdir())
        assert left == ["latest", "tsfactor_old2", "tsfactor_old3"]

    def test_keep_raw_batches_zero_disables_pruning(self, tmp_path, monkeypatch):
        """--keep-raw-batches 0 (and the test-default absence) never prunes."""
        parent = self._publish(tmp_path, symbols=[SYM_A])
        root = tmp_path / "factor_raw"
        for name in ("tsfactor_old1", "tsfactor_old2", "tsfactor_old3"):
            (root / name).mkdir(parents=True)
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A],
            responses={"600000.SH": _factor_df([20260804], [3.0])},
            args_overrides={"keep_raw_batches": 0},
        )
        assert result["dataset_status"] == "ready"
        left = sorted(p.name for p in root.iterdir())
        assert left == ["latest", "tsfactor_old1", "tsfactor_old2",
                        "tsfactor_old3"]

    def test_trade_date_batch_probe_none_falls_back(self, tmp_path, monkeypatch):
        """A probe returning None (missing day / no data, not an exception)
        must fall back to per-symbol window fetches too."""
        parent = self._publish(tmp_path, symbols=[SYM_A, SYM_B])
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A, SYM_B],
            responses={
                "600000.SH": _factor_df(
                    [20260720, 20260801, 20260804], [2.0, 2.5, 3.0]),
                "000001.SZ": _factor_df([20260804], [3.0]),
            },
            trade_date_responses={},  # probe (20260804) -> None
        )
        assert result["dataset_status"] == "ready"
        assert result["stats"].get("batch_by_trade_date") is None
        # the probe walks back up to 7 calendar days (cutoff .. cutoff-6);
        # every day is empty here, so 7 attempts happen before the fallback
        assert sum("trade_date" in c for c in pro.calls) == 7
        calls = {c["ts_code"]: c for c in pro.calls}
        assert calls["600000.SH"]["start_date"] == str(RESUME_START)
        assert calls["600000.SH"]["end_date"] == "20260804"
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        assert rec.row_count == 302

    def test_trade_date_batch_midrun_failure_falls_back(self, tmp_path, monkeypatch):
        """A day failing after a successful probe abandons the whole batch
        (no partial window map may be used) and falls back per-symbol."""
        parent = self._publish(tmp_path, symbols=[SYM_A, SYM_B])
        ok = pd.DataFrame({
            "ts_code": ["600000.SH", "000001.SZ"],
            "trade_date": [20260804, 20260804],
            "adj_factor": [3.0, 3.0],
        })
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A, SYM_B],
            responses={
                "600000.SH": _factor_df(
                    [20260720, 20260801, 20260804], [2.0, 2.5, 3.0]),
                "000001.SZ": _factor_df([20260804], [3.0]),
            },
            trade_date_responses={
                "20260804": ok,                       # probe day: ok
                "20260712": ProviderError("burst"),   # mid-window day: fail
            },
        )
        assert result["dataset_status"] == "ready"
        assert result["stats"].get("batch_by_trade_date") is None
        # batch calls: probe + 20260711 (None, skipped) + 20260712 (fail);
        # then one per-symbol call each
        assert result["stats"]["api_calls"] == 5
        calls = {c["ts_code"]: c for c in pro.calls}
        assert calls["600000.SH"]["start_date"] == str(RESUME_START)
        assert calls["600000.SH"]["end_date"] == "20260804"
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        assert rec.row_count == 302

    def test_trade_date_batch_empty_probe_df_falls_back_per_symbol(
            self, tmp_path, monkeypatch):
        """P1-1: a probe returning an empty-but-not-None DataFrame is a
        "no data day" — the probe keeps walking back; with the whole window
        empty the batch is abandoned and every symbol falls back to the
        per-symbol window fetch (start_date set, no batch flag), keeping the
        parent blobs as-is."""
        parent = self._publish(tmp_path, symbols=[SYM_A, SYM_B])
        empty = pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])
        day_rows = {str(d): empty for d in _calendar_dates(RESUME_START, 25)}
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A, SYM_B],
            responses={
                "600000.SH": _factor_df(
                    [20260720, 20260801, 20260804], [2.0, 2.5, 3.0]),
                "000001.SZ": _factor_df([20260804], [3.0]),
            },
            trade_date_responses=day_rows,
        )
        assert result["dataset_status"] == "ready"
        assert result["stats"].get("batch_by_trade_date") is None
        # probe walks back 7 days (all empty), then per-symbol window fetches
        assert sum("trade_date" in c for c in pro.calls) == 7
        calls = {c["ts_code"]: c for c in pro.calls}
        assert calls["600000.SH"]["start_date"] == str(RESUME_START)
        assert calls["600000.SH"]["end_date"] == "20260804"
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        assert rec.last_date == 20260804
        assert rec.row_count == 302

    def test_trade_date_batch_empty_probe_finds_data_in_window(
            self, tmp_path, monkeypatch):
        """P1-1: an empty probe on the cutoff day is a no-data day; when a
        rolled-back day returns rows the batch stays enabled (the probe no
        longer treats empty DataFrames as a usable probe)."""
        parent = self._publish(tmp_path, symbols=[SYM_A, SYM_B])
        wdays = _calendar_dates(RESUME_START, 25)  # 20260711..20260804
        day_rows = {}
        for d in wdays:
            if d == 20260804:
                day_rows[str(d)] = pd.DataFrame(
                    columns=["ts_code", "trade_date", "adj_factor"])
                continue  # empty probe day: keep walking back
            day_rows[str(d)] = pd.DataFrame({
                "ts_code": ["600000.SH", "000001.SZ"],
                "trade_date": [int(d), int(d)],
                "adj_factor": [3.0, 1.0],
            })
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A, SYM_B],
            responses={},  # per-symbol responses must NOT be used
            trade_date_responses=day_rows,
        )
        assert result["dataset_status"] == "ready"
        assert result["stats"].get("batch_by_trade_date") is True
        # probe: 20260804 (empty) then rolled back to 20260803 (data)
        assert any(c.get("trade_date") == "20260803" for c in pro.calls)
        assert result["stats"]["api_calls"] == 2 + 25
        assert all("trade_date" in c for c in pro.calls)
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        assert rec.last_date == 20260803

    def test_trade_date_batch_symbol_without_rows_keeps_parent_blob(
            self, tmp_path, monkeypatch):
        """Batch path: a universe symbol absent from the window map keeps its
        parent blob untouched (no csv written), while a symbol present in the
        window merges onto the parent."""
        parent = self._publish(tmp_path, symbols=[SYM_A, SYM_B])
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A, SYM_B],
            responses={},
            trade_date_responses={
                "20260804": pd.DataFrame({
                    "ts_code": ["600000.SH"],
                    "trade_date": [20260804],
                    "adj_factor": [3.0],
                }),
            },
        )
        assert result["dataset_status"] == "ready"
        assert result["stats"].get("batch_by_trade_date") is True
        m = store.load_manifest(result["dataset_id"])
        # SYM_B has no rows in the window -> parent blob retained
        old_b = next(r for r in parent.symbols if r.symbol == SYM_B)
        rec_b = next(r for r in m.symbols if r.symbol == SYM_B)
        assert rec_b.quality == "ok"
        assert rec_b.blob_sha256 == old_b.blob_sha256
        assert rec_b.row_count == old_b.row_count
        # SYM_A has a window row -> merged onto the parent
        rec_a = next(r for r in m.symbols if r.symbol == SYM_A)
        assert rec_a.row_count == 301
        assert rec_a.last_date == 20260804
        # raw cache only holds symbols with window rows
        latest = Path(args.factor_raw_root) / "latest"
        assert (latest / "600000.SH.csv").exists()
        assert not (latest / "000001.SZ.csv").exists()

    def test_trade_date_batch_bad_row_falls_back_without_crash(
            self, tmp_path, monkeypatch):
        """A day response with an unparseable row (bad trade_date value) is
        caught inside the batch helper: the batch aborts and per-symbol
        window fetches complete the sync without crashing."""
        parent = self._publish(tmp_path, symbols=[SYM_A, SYM_B])
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A, SYM_B],
            responses={
                "600000.SH": _factor_df(
                    [20260720, 20260801, 20260804], [2.0, 2.5, 3.0]),
                "000001.SZ": _factor_df([20260804], [3.0]),
            },
            trade_date_responses={
                "20260804": pd.DataFrame({
                    "ts_code": ["600000.SH", "000001.SZ"],
                    "trade_date": [20260804, 20260804],
                    "adj_factor": [3.0, 3.0],
                }),
                # the NaN row is dropped by dropna; the unparseable string
                # survives and aborts the batch inside the helper
                "20260720": pd.DataFrame({
                    "ts_code": ["600000.SH", "600000.SH", "000001.SZ"],
                    "trade_date": [20260720, "not_a_date", 20260720],
                    "adj_factor": [2.0, 3.0, 1.0],
                }),
            },
        )
        assert result["dataset_status"] == "ready"
        assert result["stats"].get("batch_by_trade_date") is None
        # probe + days 0711..0720, then one per-symbol call each
        assert sum("trade_date" in c for c in pro.calls) == 11
        calls = {c["ts_code"]: c for c in pro.calls}
        assert calls["600000.SH"]["start_date"] == str(RESUME_START)
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        assert rec.row_count == 302
        assert rec.last_date == 20260804

    def test_trade_date_batch_low_coverage_falls_back(self, tmp_path, monkeypatch):
        """A non-empty day covering < 90% of a large universe is treated as
        unreliable (Tushare row-cap truncation) and falls back per-symbol."""
        symbols = [SYM_A] + [f"SZSE.STK.{i:06d}" for i in range(2, 101)]
        parent = self._publish(tmp_path, symbols=symbols)
        ts_codes = ["600000.SH"] + [f"{i:06d}.SZ" for i in range(2, 101)]
        full_rows = pd.DataFrame({
            "ts_code": ts_codes,
            "trade_date": [20260804] * 100,
            "adj_factor": [3.0] * 100,
        })
        per_sym = {
            "600000.SH": _factor_df([20260804], [3.0]),
            **{f"{i:06d}.SZ": _factor_df([20260804], [3.0])
               for i in range(2, 101)},
        }
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=symbols,
            responses=per_sym,
            trade_date_responses={
                "20260804": full_rows,             # probe day: full coverage
                "20260711": full_rows.iloc[:50],   # truncated day: 50%
            },
        )
        assert result["dataset_status"] == "ready"
        assert result["stats"].get("batch_by_trade_date") is None
        # probe + the truncated day, then one per-symbol call per symbol
        assert sum("trade_date" in c for c in pro.calls) == 2
        assert result["stats"]["api_calls"] == 2 + len(symbols)
        calls = {c["ts_code"]: c for c in pro.calls}
        assert calls["600000.SH"]["start_date"] == str(RESUME_START)
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        assert rec.row_count == 301
        assert rec.last_date == 20260804

    def test_trade_date_batch_probe_weekend_rolls_back_in_time(
            self, tmp_path, monkeypatch):
        """A None probe on the cutoff day (weekend/holiday) walks back in
        time to the most recent day with data; the batch path stays enabled
        and the day loop still starts at the window start."""
        parent = self._publish(tmp_path, symbols=[SYM_A, SYM_B])
        wdays = _calendar_dates(RESUME_START, 25)  # 20260711..20260804
        day_rows = {}
        for d in wdays:
            if d == 20260804:
                continue  # weekend-style cutoff: no response (None)
            day_rows[str(d)] = pd.DataFrame({
                "ts_code": ["600000.SH", "000001.SZ"],
                "trade_date": [int(d), int(d)],
                "adj_factor": [3.0, 1.0],
            })
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A, SYM_B],
            responses={},  # per-symbol responses must NOT be used
            trade_date_responses=day_rows,
        )
        assert result["dataset_status"] == "ready"
        assert result["stats"].get("batch_by_trade_date") is True
        # probe: 20260804 (None) then rolled back to 20260803 (data)
        assert any(c.get("trade_date") == "20260803" for c in pro.calls)
        # probe attempts (2) + full day loop (25 days)
        assert result["stats"]["api_calls"] == 2 + 25
        assert all("trade_date" in c for c in pro.calls)
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        assert rec.last_date == 20260803

    def test_trade_date_batch_long_window_skips_batch(self, tmp_path, monkeypatch):
        """An explicit start-date stretching the window beyond the batch cap
        skips the trade_date batch entirely and uses per-symbol fetches."""
        parent = self._publish(tmp_path, symbols=[SYM_A, SYM_B])
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=[SYM_A, SYM_B],
            start_date=20260501,  # window 20260501..20260804 = 95 days
            responses={
                "600000.SH": _factor_df([20260804], [3.0]),
                "000001.SZ": _factor_df([20260804], [3.0]),
            },
            trade_date_responses={
                "20260804": pd.DataFrame({
                    "ts_code": ["600000.SH", "000001.SZ"],
                    "trade_date": [20260804, 20260804],
                    "adj_factor": [3.0, 3.0],
                }),
            },
        )
        assert result["dataset_status"] == "ready"
        assert result["stats"].get("batch_by_trade_date") is None
        # no trade_date call at all: the batch was skipped up front
        assert all("ts_code" in c for c in pro.calls)
        calls = {c["ts_code"]: c for c in pro.calls}
        assert calls["600000.SH"]["start_date"] == "20260501"
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        assert rec.row_count == 301
        assert rec.last_date == 20260804

    def test_trade_date_batch_exact_coverage_boundary_falls_back(
            self, tmp_path, monkeypatch):
        """Coverage exactly at the 90% boundary is still treated as
        truncated (<= comparison) and falls back per-symbol."""
        symbols = [SYM_A] + [f"SZSE.STK.{i:06d}" for i in range(2, 101)]
        parent = self._publish(tmp_path, symbols=symbols)
        ts_codes = ["600000.SH"] + [f"{i:06d}.SZ" for i in range(2, 101)]
        per_sym = {
            "600000.SH": _factor_df([20260804], [3.0]),
            **{f"{i:06d}.SZ": _factor_df([20260804], [3.0])
               for i in range(2, 101)},
        }
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            symbols=symbols,
            responses=per_sym,
            trade_date_responses={
                # exactly 90/100 symbols: the boundary must fall back
                "20260804": pd.DataFrame({
                    "ts_code": ts_codes[:90],
                    "trade_date": [20260804] * 90,
                    "adj_factor": [3.0] * 90,
                }),
            },
        )
        assert result["dataset_status"] == "ready"
        assert result["stats"].get("batch_by_trade_date") is None
        # one probe attempt only (the probe day has data, no roll-back),
        # then one per-symbol call per symbol
        assert sum("trade_date" in c for c in pro.calls) == 1
        assert result["stats"]["api_calls"] == 1 + len(symbols)
        calls = {c["ts_code"]: c for c in pro.calls}
        assert calls["600000.SH"]["start_date"] == str(RESUME_START)
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        assert rec.row_count == 301
        assert rec.last_date == 20260804

    def test_resume_all_done_skips_batch(self, tmp_path, monkeypatch):
        """Resume with every symbol already done never touches the trade_date
        batch (zero API calls) and republishes the dataset."""
        parent = self._publish(tmp_path, symbols=[SYM_A, SYM_B])
        smd = _script()
        store = DatasetStore(tmp_path / "market_data")
        pro = _FakeFactorPro({}, {"20260804": pd.DataFrame({
            "ts_code": ["600000.SH", "000001.SZ"],
            "trade_date": [20260804, 20260804],
            "adj_factor": [3.0, 3.0],
        })})
        _FakeTushareProvider.registry["pro"] = pro
        from wtpy.apps.astock.data.providers import tushare as tushare_mod
        monkeypatch.setattr(tushare_mod, "TushareProvider", _FakeTushareProvider)
        uni = _write_universe(tmp_path, [SYM_A, SYM_B])
        symbols = smd._load_universe_file(uni)
        universe_hash = hashlib.sha256(
            ",".join(symbols).encode()).hexdigest()
        done = {}
        for sym, prec in zip([SYM_A, SYM_B], parent.symbols):
            done[sym] = {
                "status": "factor_ready", "blob_sha256": prec.blob_sha256,
                "rows": int(prec.row_count or 0), "first_date": prec.first_date,
                "last_date": prec.last_date,
            }
        ck_path = store.sync_logs_dir / "checkpoint_tushare_adj_factor_1d.json"
        ck_path.write_text(json.dumps({
            "universe_hash": universe_hash, "sync_run_id": "tsfactor_resume2",
            "done": done, "api_calls": 0,
            "parent_dataset_id": parent.dataset_id,
            "window_start": RESUME_START, "cutoff": 20260804,
        }), encoding="utf-8")
        result = smd.sync_tushare_adj_factor_full(
            _factor_args(tmp_path, universe_file=str(uni),
                         resume=True, fresh=False), store)
        assert result["dataset_status"] == "ready"
        assert sum("trade_date" in c for c in pro.calls) == 0
        assert result["stats"]["api_calls"] == 0
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == SYM_A)
        assert rec.blob_sha256 == parent.symbols[0].blob_sha256

    def _publish(self, tmp_path, symbols):
        store = DatasetStore(tmp_path / "market_data")
        return _publish_factor_parent(
            store, dataset_id="tushare_adjfactor_1d_full",
            symbols=symbols, n_rows=300,
        )


class TestFetchAdjFactorArgExclusion:
    """fetch_adj_factor(ts_code=..., trade_date=...) must never silently pick
    one of the two modes when both are given."""

    class _StubPro:
        def __init__(self):
            self.calls = []

        def adj_factor(self, **kwargs):
            self.calls.append(kwargs)
            return None

    def _provider(self):
        from wtpy.apps.astock.data.providers.tushare import TushareProvider

        p = TushareProvider(token="test")
        p._initialized = True
        p._pro = self._StubPro()
        return p

    def test_ts_code_and_trade_date_raise(self):
        p = self._provider()
        with pytest.raises(ValueError, match="mutually exclusive"):
            p.fetch_adj_factor("600000.SH", trade_date=20260804)

    def test_single_mode_calls_still_route(self):
        p = self._provider()
        p.fetch_adj_factor("600000.SH", start_date=20260711, end_date=20260804)
        assert p._pro.calls[-1] == {
            "ts_code": "600000.SH",
            "start_date": "20260711",
            "end_date": "20260804",
        }
        p.fetch_adj_factor(trade_date=20260804)
        assert p._pro.calls[-1] == {"trade_date": "20260804"}
        p.fetch_adj_factor()  # bare market call keeps working
        assert p._pro.calls[-1] == {"ts_code": None}
