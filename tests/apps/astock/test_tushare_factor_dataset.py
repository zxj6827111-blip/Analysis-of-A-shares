# -*- coding: utf-8 -*-
"""Gate C phase 1: factor blob storage, factor-manifest fields, token guards.

Offline-only: uses tmp_path DatasetStore and static source inspection.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)

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
