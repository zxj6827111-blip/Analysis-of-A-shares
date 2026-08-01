"""Gate B1 focused tests: historical universe identity + merge + PIT rules."""

from __future__ import annotations

import inspect

import pytest

from wtpy.apps.astock.data import historical_universe as hu
from wtpy.apps.astock.data.universe import to_std_code


class TestTsCodeConversion:
    def test_roundtrip_sh(self):
        assert hu.ts_code_to_canonical("600001.SH") == "SSE.STK.600001"
        assert hu.canonical_to_ts_code("SSE.STK.600001") == "600001.SH"

    def test_roundtrip_sz(self):
        assert hu.ts_code_to_canonical("300104.SZ") == "SZSE.STK.300104"
        assert hu.canonical_to_ts_code("SZSE.STK.300104") == "300104.SZ"

    def test_roundtrip_bj(self):
        assert hu.ts_code_to_canonical("430047.BJ") == "BSE.STK.430047"
        assert hu.canonical_to_ts_code("BSE.STK.430047") == "430047.BJ"
        assert hu.ts_code_to_canonical("920001.BJ") == "BSE.STK.920001"

    def test_consistent_with_universe_to_std_code(self):
        # ts_code path must agree with the project's existing normalizer
        assert hu.ts_code_to_canonical("600000.SH") == to_std_code("sh600000")
        assert hu.ts_code_to_canonical("000001.SZ") == to_std_code("sz000001")
        assert hu.ts_code_to_canonical("430047.BJ") == to_std_code("bj430047")

    def test_malformed_raises(self):
        for bad in ("600000", "600000.XX", "SSE.STK.600000", "", "60000.SH"):
            with pytest.raises(ValueError):
                hu.ts_code_to_canonical(bad)
        for bad in ("600000.SH", "SSE.600000", "NYSE.STK.600000", ""):
            with pytest.raises(ValueError):
                hu.canonical_to_ts_code(bad)


class TestClassifyInstrument:
    @pytest.mark.parametrize(
        "code,exch,expected",
        [
            ("600001", "SSE", hu.A_SHARE),
            ("688001", "SSE", hu.A_SHARE),
            ("900901", "SSE", hu.B_SHARE),
            ("510300", "SSE", hu.ETF),
            ("501000", "SSE", hu.LOF),
            ("000001", "SSE", hu.INDEX),
            ("110038", "SSE", hu.CONVERTIBLE_BOND),
            ("019547", "SSE", hu.BOND),
            ("000001", "SZSE", hu.A_SHARE),
            ("002680", "SZSE", hu.A_SHARE),
            ("300104", "SZSE", hu.A_SHARE),
            ("200011", "SZSE", hu.B_SHARE),
            ("159915", "SZSE", hu.ETF),
            ("161725", "SZSE", hu.LOF),
            ("399006", "SZSE", hu.INDEX),
            ("123001", "SZSE", hu.CONVERTIBLE_BOND),
            ("430047", "BSE", hu.A_SHARE),
            ("833171", "BSE", hu.A_SHARE),
            ("920001", "BSE", hu.A_SHARE),
            ("999999", "BSE", hu.OTHER),
        ],
    )
    def test_classification(self, code, exch, expected):
        assert hu.classify_instrument(code, exch) == expected

    def test_non_numeric_is_other(self):
        assert hu.classify_instrument("ABCDEF", "SSE") == hu.OTHER


class TestMergeListStatus:
    def _rec(self, ts_code, status, list_date=20000101, delist_date=None, name="x"):
        return hu.ListStatusRecord(
            ts_code=ts_code,
            name=name,
            list_status=status,
            list_date=list_date,
            delist_date=delist_date,
        )

    def test_simple_merge_no_conflict(self):
        merged = hu.merge_list_status(
            [self._rec("600001.SH", "D", delist_date=20100114)]
        )
        m = merged["600001.SH"]
        assert m.list_status == "D"
        assert m.canonical_symbol == "SSE.STK.600001"
        assert m.delist_date == 20100114
        assert not m.conflicts

    def test_duplicate_status_conflict_d_wins(self):
        merged = hu.merge_list_status(
            [
                self._rec("600001.SH", "L"),
                self._rec("600001.SH", "D", delist_date=20100114),
            ]
        )
        m = merged["600001.SH"]
        assert m.list_status == "D"
        assert any(c.startswith("duplicate_status") for c in m.conflicts)

    def test_p_overrides_l_but_not_d(self):
        merged = hu.merge_list_status(
            [
                self._rec("000001.SZ", "P"),
                self._rec("000001.SZ", "L"),
            ]
        )
        assert merged["000001.SZ"].list_status == "P"
        merged2 = hu.merge_list_status(
            [
                self._rec("000002.SZ", "D", delist_date=20191127),
                self._rec("000002.SZ", "P"),
            ]
        )
        assert merged2["000002.SZ"].list_status == "D"

    def test_list_date_mismatch_recorded(self):
        merged = hu.merge_list_status(
            [
                self._rec("600001.SH", "L", list_date=20000101),
                self._rec("600001.SH", "D", list_date=19990101, delist_date=20100114),
            ]
        )
        m = merged["600001.SH"]
        assert any("list_date_mismatch" in c for c in m.conflicts)

    def test_name_mismatch_recorded(self):
        merged = hu.merge_list_status(
            [
                self._rec("300104.SZ", "L", name="乐视网"),
                self._rec("300104.SZ", "D", name="乐视退", delist_date=20200721),
            ]
        )
        assert any("name_mismatch" in c for c in merged["300104.SZ"].conflicts)


class TestPointInTime:
    def test_before_list_date_excluded(self):
        assert not hu.is_member_point_in_time(20091231, 20100101, None)

    def test_on_list_date_included(self):
        assert hu.is_member_point_in_time(20100101, 20100101, None)

    def test_after_delist_excluded(self):
        assert not hu.is_member_point_in_time(20200721, 20100430, 20200721)
        assert not hu.is_member_point_in_time(20200722, 20100430, 20200721)

    def test_day_before_delist_included(self):
        assert hu.is_member_point_in_time(20200720, 20100430, 20200721)

    def test_last_trade_date_takes_precedence(self):
        # tradable through last_trade_date inclusive even if delist later
        assert hu.is_member_point_in_time(
            20200720, 20100430, 20200728, last_trade_date=20200720
        )
        assert not hu.is_member_point_in_time(
            20200721, 20100430, 20200728, last_trade_date=20200720
        )

    def test_unknown_list_date_fail_closed(self):
        assert not hu.is_member_point_in_time(20200101, None, None)

    def test_open_ended_listed(self):
        assert hu.is_member_point_in_time(20260717, 20000101, None)

    def test_annual_counts_year_boundary(self):
        idents = [
            hu.MergedIdentity(
                ts_code="600001.SH",
                canonical_symbol="SSE.STK.600001",
                exchange="SSE",
                name="a",
                list_status="D",
                list_date=20000101,
                delist_date=20100114,
            ),
            hu.MergedIdentity(
                ts_code="300104.SZ",
                canonical_symbol="SZSE.STK.300104",
                exchange="SZSE",
                name="b",
                list_status="D",
                list_date=20100812,
                delist_date=20200721,
            ),
            # non-A-share must not be counted
            hu.MergedIdentity(
                ts_code="900901.SH",
                canonical_symbol="SSE.STK.900901",
                exchange="SSE",
                name="bshare",
                list_status="L",
                list_date=19920101,
                delist_date=None,
            ),
        ]
        rows = hu.annual_membership_counts(idents, [2009, 2010, 2019, 2020])
        by_year = {r["year"]: r for r in rows}
        assert by_year[2009]["member_count"] == 1  # only 600001
        assert by_year[2010]["member_count"] == 1  # 600001 delisted, 300104 listed
        assert by_year[2019]["member_count"] == 1  # only 300104
        assert by_year[2020]["member_count"] == 0  # both gone
        assert by_year[2010]["szse"] == 1

    def test_annual_counts_carry_rule_version(self):
        rows = hu.annual_membership_counts([], [2000])
        assert rows[0]["universe_rule_version"] == hu.UNIVERSE_RULE_VERSION


class TestNoTokenLeak:
    def test_module_has_no_token_material(self):
        src = inspect.getsource(hu)
        lowered = src.lower()
        assert "set_token(" not in src
        assert "api_key" not in lowered
        # no long hex/base64 literals that could be a credential
        import re

        assert not re.search(r"['\"][0-9a-f]{32,}['\"]", lowered)

    def test_module_is_offline(self):
        src = inspect.getsource(hu)
        for banned in ("import requests", "import tushare", "urlopen", "http://", "https://"):
            assert banned not in src
