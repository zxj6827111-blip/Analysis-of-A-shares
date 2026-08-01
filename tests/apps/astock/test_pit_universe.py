"""Gate B4 focused tests: point-in-time universe (offline, synthetic)."""

from __future__ import annotations

import json

import pytest

from wtpy.apps.astock.data.pit_universe import (
    REASON_AFTER_LAST_TRADE,
    REASON_DELISTED,
    REASON_MEMBER,
    REASON_NO_LIST_DATE,
    REASON_NOT_LISTED_YET,
    REASON_UNKNOWN_SYMBOL,
    InstrumentWindow,
    PointInTimeUniverse,
    UniverseImmutabilityError,
)
from wtpy.apps.astock.research.signal_cache import signal_cache_key


def _w(
    canon="SZSE.STK.300104",
    ts_code="300104.SZ",
    list_date=20100812,
    delist_date=20200721,
    last_trade_date=20200720,
    aliases=None,
    **kw,
):
    return InstrumentWindow(
        canonical_symbol=canon,
        ts_code=ts_code,
        exchange=canon.split(".")[0],
        board="chinext",
        name="乐视网",
        list_status="D" if delist_date else "L",
        list_date=list_date,
        delist_date=delist_date,
        last_trade_date=last_trade_date,
        **kw,
        aliases=aliases or [],
    )


@pytest.fixture
def uni():
    windows = [
        _w(),  # LeTV: 20100812..20200720
        _w(
            canon="SSE.STK.600000",
            ts_code="600000.SH",
            list_date=19991110,
            delist_date=None,
            last_trade_date=None,
        ),
        _w(  # BSE migrated with alias: old 839680 -> new 920680
            canon="BSE.STK.920680",
            ts_code="920680.BJ",
            list_date=20211115,
            delist_date=20260105,
            last_trade_date=20251231,
            aliases=["BSE.STK.839680"],
        ),
        _w(  # unknown list_date -> fail closed
            canon="SZSE.STK.000999",
            ts_code="000999.SZ",
            list_date=None,
            delist_date=None,
            last_trade_date=None,
        ),
    ]
    return PointInTimeUniverse.build(windows, cutoff=20260717)


class TestMembership:
    def test_before_list_excluded(self, uni):
        ok, reason = uni.membership_reason("SZSE.STK.300104", 20100811)
        assert not ok and reason == REASON_NOT_LISTED_YET

    def test_list_day_included(self, uni):
        ok, reason = uni.membership_reason("SZSE.STK.300104", 20100812)
        assert ok and reason == REASON_MEMBER

    def test_last_trade_day_included(self, uni):
        assert uni.is_member("SZSE.STK.300104", 20200720)

    def test_after_last_trade_excluded(self, uni):
        ok, reason = uni.membership_reason("SZSE.STK.300104", 20200721)
        assert not ok and reason == REASON_AFTER_LAST_TRADE

    def test_open_ended_listed_member_today(self, uni):
        assert uni.is_member("SSE.STK.600000", 20260717)

    def test_future_stock_cannot_enter_past(self, uni):
        # BSE 920680 listed 20211115 must not exist in 2020
        assert not uni.is_member("BSE.STK.920680", 20200101)

    def test_unknown_symbol_fail_closed(self, uni):
        ok, reason = uni.membership_reason("SSE.STK.999999", 20200101)
        assert not ok and reason == REASON_UNKNOWN_SYMBOL

    def test_no_list_date_fail_closed(self, uni):
        ok, reason = uni.membership_reason("SZSE.STK.000999", 20200101)
        assert not ok and reason == REASON_NO_LIST_DATE

    def test_delist_only_window(self):
        # no last_trade_date -> trade_date < delist_date rule
        u = PointInTimeUniverse.build(
            [_w(delist_date=20200721, last_trade_date=None)], cutoff=20260717
        )
        assert u.is_member("SZSE.STK.300104", 20200720)
        ok, reason = u.membership_reason("SZSE.STK.300104", 20200721)
        assert not ok and reason == REASON_DELISTED


class TestAliasResolution:
    def test_old_bse_code_resolves_to_identity(self, uni):
        w = uni.resolve("BSE.STK.839680")
        assert w is not None and w.canonical_symbol == "BSE.STK.920680"

    def test_alias_membership_uses_identity_window(self, uni):
        assert uni.is_member("BSE.STK.839680", 20220601)
        assert not uni.is_member("BSE.STK.839680", 20260106)

    def test_format_variants_resolve(self, uni):
        assert uni.is_member("300104.SZ", 20150601)
        assert uni.is_member("sz300104", 20150601)
        assert uni.is_member("300104", 20150601)


class TestFilterAndMembers:
    def test_filter_active(self, uni):
        codes = ["SZSE.STK.300104", "SSE.STK.600000", "BSE.STK.920680"]
        assert uni.filter_active(codes, 20150601) == [
            "SZSE.STK.300104",
            "SSE.STK.600000",
        ]

    def test_members_on(self, uni):
        assert set(uni.members_on(20220601)) == {
            "SZSE.STK.300104",
            "SSE.STK.600000",
            "BSE.STK.920680",
        } - {"SZSE.STK.300104"}


class TestPersistence:
    def test_save_load_roundtrip(self, uni, tmp_path):
        p = uni.save(tmp_path)
        loaded = PointInTimeUniverse.load(p)
        assert loaded.universe_dataset_id == uni.universe_dataset_id
        assert loaded.content_sha256 == uni.content_sha256
        assert loaded.is_member("SZSE.STK.300104", 20150601)
        assert loaded.resolve("BSE.STK.839680").canonical_symbol == "BSE.STK.920680"

    def test_content_addressed_id_stable(self, uni):
        rebuilt = PointInTimeUniverse.build(
            list(uni.entries.values()), cutoff=20260717
        )
        assert rebuilt.universe_dataset_id == uni.universe_dataset_id

    def test_different_content_different_id(self, uni):
        changed = list(uni.entries.values())
        changed[0] = _w(list_date=20100813)
        rebuilt = PointInTimeUniverse.build(changed, cutoff=20260717)
        assert rebuilt.universe_dataset_id != uni.universe_dataset_id

    def test_idempotent_save(self, uni, tmp_path):
        p1 = uni.save(tmp_path)
        p2 = uni.save(tmp_path)  # same content -> no-op
        assert p1 == p2

    def test_immutability_guard(self, uni, tmp_path):
        p = uni.save(tmp_path)
        data = json.loads(p.read_text(encoding="utf-8"))
        # simulate an attacker/bug producing different content under same id
        other = PointInTimeUniverse.build(
            [_w(list_date=20100813)], cutoff=20260717
        )
        other.universe_dataset_id = uni.universe_dataset_id
        with pytest.raises(UniverseImmutabilityError):
            other.save(tmp_path)
        assert json.loads(p.read_text(encoding="utf-8")) == data

    def test_corrupt_file_rejected(self, uni, tmp_path):
        p = uni.save(tmp_path)
        data = json.loads(p.read_text(encoding="utf-8"))
        data["entries"][0]["list_date"] = 19990101  # tamper without re-hashing
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError, match="hash mismatch"):
            PointInTimeUniverse.load(p)

    def test_from_root_and_list(self, uni, tmp_path):
        uni.save(tmp_path)
        ids = PointInTimeUniverse.list_universes(tmp_path)
        assert uni.universe_dataset_id in ids
        loaded = PointInTimeUniverse.from_root(tmp_path, uni.universe_dataset_id)
        assert loaded.content_sha256 == uni.content_sha256


class TestVersioning:
    def test_rule_versions_recorded(self, uni):
        d = uni.to_dict()
        assert d["universe_rule_version"] == "pit_universe_rule_v1"
        assert d["instrument_identity_rule_version"] == "identity_rule_v1"
        assert d["schema_version"] == 1

    def test_universe_version_changes_cache_key(self):
        base = dict(
            indicator_ids=["macd"],
            period="DAY",
            start=20200101,
            end=20201231,
            universe_hash="u",
            adjust_mode="asof_forward_qfq",
        )
        k1 = signal_cache_key(**base, universe_version="pit_universe_1d_20260717_aaa:v1")
        k2 = signal_cache_key(**base, universe_version="pit_universe_1d_20260717_bbb:v1")
        k3 = signal_cache_key(**base)
        assert k1 != k2 != k3 and k1 != k3
