# -*- coding: utf-8 -*-
"""Gate C cache isolation: derived-dataset lineage must partition caches.

signal_cache_key / execution_cache_key must change whenever the raw parent,
factor parent, formula version or anchor policy changes — and stay stable
(and legacy-compatible) otherwise.
"""
from __future__ import annotations

import dataclasses

from wtpy.apps.astock.research.execution_cache import execution_cache_key
from wtpy.apps.astock.research.signal_cache import signal_cache_key
from wtpy.apps.astock.service.backtest_request import BacktestRequest


def _base_signal_kwargs():
    return dict(
        indicator_ids=["ind_735"],
        indicator_source_hash="srchash",
        period="DAY",
        start=20240101,
        end=20240701,
        universe_hash="univ_hash",
        adjust_mode="qfq",
        data_source="internal",
        adjustment="tushare_factor_qfq",
        dataset_id="internal_tsfqfq_1d_20240112_abc",
    )


LINEAGE = dict(
    raw_parent_dataset_id="localvendor_none_1d_raw001",
    factor_parent_dataset_id="tushare_adjfactor_1d_fac001",
    formula_version="tsqfq_v1",
    anchor_policy="last_factor_on_or_before_cutoff",
)


class TestSignalCacheLineageIsolation:
    def test_factor_parent_changes_key(self):
        k1 = signal_cache_key(**_base_signal_kwargs(), **LINEAGE)
        k2 = signal_cache_key(
            **_base_signal_kwargs(),
            **{**LINEAGE, "factor_parent_dataset_id": "tushare_adjfactor_1d_fac002"},
        )
        assert k1 != k2

    def test_raw_parent_changes_key(self):
        k1 = signal_cache_key(**_base_signal_kwargs(), **LINEAGE)
        k2 = signal_cache_key(
            **_base_signal_kwargs(),
            **{**LINEAGE, "raw_parent_dataset_id": "localvendor_none_1d_raw002"},
        )
        assert k1 != k2

    def test_formula_version_changes_key(self):
        k1 = signal_cache_key(**_base_signal_kwargs(), **LINEAGE)
        k2 = signal_cache_key(
            **_base_signal_kwargs(), **{**LINEAGE, "formula_version": "tsqfq_v2"}
        )
        assert k1 != k2

    def test_anchor_policy_changes_key(self):
        k1 = signal_cache_key(**_base_signal_kwargs(), **LINEAGE)
        k2 = signal_cache_key(
            **_base_signal_kwargs(),
            **{**LINEAGE, "anchor_policy": "first_factor_after_cutoff"},
        )
        assert k1 != k2

    def test_identical_lineage_same_key(self):
        k1 = signal_cache_key(**_base_signal_kwargs(), **LINEAGE)
        k2 = signal_cache_key(**_base_signal_kwargs(), **dict(LINEAGE))
        assert k1 == k2

    def test_legacy_call_equals_empty_lineage(self):
        # Backward compatibility: omitting the new kwargs must produce the
        # same key as passing empty strings explicitly.
        k_legacy = signal_cache_key(**_base_signal_kwargs())
        k_empty = signal_cache_key(
            **_base_signal_kwargs(),
            raw_parent_dataset_id="",
            factor_parent_dataset_id="",
            formula_version="",
            anchor_policy="",
        )
        assert k_legacy == k_empty


class TestExecutionCacheLineageIsolation:
    def _base_payload(self):
        return {
            "engine": "full",
            "rule_ids": ["rule_a"],
            "period": "DAY",
            "hold": 5,
            "start": 20240101,
            "end": 20240701,
            "universe": "univ_hash",
            "signal_adjustment": "tushare_factor_qfq",
            "raw_parent_dataset_id": "localvendor_none_1d_raw001",
            "factor_parent_dataset_id": "tushare_adjfactor_1d_fac001",
            "formula_version": "tsqfq_v1",
            "anchor_policy": "last_factor_on_or_before_cutoff",
        }

    def test_adding_factor_parent_changes_key(self):
        base = self._base_payload()
        without = {k: v for k, v in base.items()
                   if k != "factor_parent_dataset_id"}
        assert execution_cache_key(base) != execution_cache_key(without)

    def test_changed_factor_parent_changes_key(self):
        p1 = self._base_payload()
        p2 = {**p1, "factor_parent_dataset_id": "tushare_adjfactor_1d_fac002"}
        assert execution_cache_key(p1) != execution_cache_key(p2)

    def test_same_payload_stable_key(self):
        p = self._base_payload()
        k1 = execution_cache_key(p)
        k2 = execution_cache_key(dict(p))
        assert k1 == k2
        assert isinstance(k1, str) and len(k1) == 32


class TestBacktestRequestLineageFields:
    def test_new_fields_exist_default_none(self):
        req = BacktestRequest(rule_ids=["ind_735"])
        assert req.signal_raw_parent_dataset_id is None
        assert req.signal_factor_parent_dataset_id is None
        assert req.signal_formula_version is None
        assert req.signal_anchor_policy is None

    def test_serialization_includes_lineage_fields(self):
        req = BacktestRequest(
            rule_ids=["ind_735"],
            signal_raw_parent_dataset_id="raw_x",
            signal_factor_parent_dataset_id="fac_y",
            signal_formula_version="tsqfq_v1",
            signal_anchor_policy="last_factor_on_or_before_cutoff",
        )
        d = req.to_dict() if hasattr(req, "to_dict") else dataclasses.asdict(req)
        for key in (
            "signal_raw_parent_dataset_id",
            "signal_factor_parent_dataset_id",
            "signal_formula_version",
            "signal_anchor_policy",
        ):
            assert key in d, f"missing {key} in serialized request"
        assert d["signal_raw_parent_dataset_id"] == "raw_x"
        assert d["signal_factor_parent_dataset_id"] == "fac_y"
        assert d["signal_formula_version"] == "tsqfq_v1"
        assert d["signal_anchor_policy"] == "last_factor_on_or_before_cutoff"
