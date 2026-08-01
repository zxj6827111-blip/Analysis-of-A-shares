# -*- coding: utf-8 -*-
"""Unit tests for the vendor universe builder and generic code classification.

Pure unit tests: NEVER touch real vendor data on disk.  All archive input is
built as miniature year ZIP fixtures under pytest's tmp_path.
"""

from __future__ import annotations

import csv
import hashlib
import zipfile
from pathlib import Path

import pytest

from wtpy.apps.astock.data.universe import is_ashare_code
from wtpy.apps.astock.data.vendor_universe import (
    ROW_FIELDS,
    VendorUniverse,
    build_vendor_universe,
    classify_vendor_code,
)

# Real vendor CSV header (verbatim column names).
VENDOR_COLUMNS = (
    "code,datetime,open,high,low,close,pre_close,change,pct_chg,volume,amount,"
    "turnover,turnover_free,volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,dv_yield,dv_ttm,"
    "total_share,float_share,free_share,total_mv,circ_mv"
)


# ---------------------------------------------------------------------------
# classify_vendor_code: generic segment rules
# ---------------------------------------------------------------------------

class TestClassifyVendorCode:
    @pytest.mark.parametrize(
        "code,exchange,itype,board",
        [
            ("600000.SH", "SSE", "stock", "sse_main"),
            ("605111.SH", "SSE", "stock", "sse_main"),
            ("688001.SH", "SSE", "stock", "star"),
            ("900901.SH", "SSE", "b_share", ""),
            ("510300.SH", "SSE", "fund_etf", ""),
            ("000001.SH", "SSE", "index", ""),
            ("110011.SH", "SSE", "bond", ""),
            ("000001.SZ", "SZSE", "stock", "szse_main"),
            ("002027.SZ", "SZSE", "stock", "szse_main"),
            ("300750.SZ", "SZSE", "stock", "chinext"),
            ("301111.SZ", "SZSE", "stock", "chinext"),
            ("302132.SZ", "SZSE", "stock", "chinext"),  # new 302 segment, generic 30x rule
            ("305999.SZ", "SZSE", "stock", "chinext"),  # future 30x segment
            ("200011.SZ", "SZSE", "b_share", ""),
            ("159915.SZ", "SZSE", "fund_etf", ""),
            ("399001.SZ", "SZSE", "index", ""),
            ("430047.BJ", "BSE", "stock", "bse"),
            ("832000.BJ", "BSE", "stock", "bse"),
            ("920001.BJ", "BSE", "stock", "bse"),
        ],
    )
    def test_segment_classification(self, code, exchange, itype, board):
        result = classify_vendor_code(code)
        assert result["exchange"] == exchange
        assert result["instrument_type"] == itype
        assert result["board"] == board

    def test_exchange_code_mismatch_is_other(self):
        result = classify_vendor_code("600000.SZ")
        assert result["exchange"] == "SZSE"
        assert result["instrument_type"] == "other"
        assert result["board"] == ""

    def test_unidentified_segment(self):
        result = classify_vendor_code("777777.SH")
        assert result["exchange"] == "SSE"
        assert result["instrument_type"] == "unidentified"
        assert result["board"] == ""

    @pytest.mark.parametrize("bad", ["", "600000", "FOO.SH", "60000.SH", "600000.XX", "abc"])
    def test_non_vendor_form_is_unknown(self, bad):
        result = classify_vendor_code(bad)
        assert result["exchange"] == "UNKNOWN"
        assert result["instrument_type"] == "unidentified"
        assert result["board"] == ""


# ---------------------------------------------------------------------------
# is_ashare_code: generic 30x ChiNext segment
# ---------------------------------------------------------------------------

class TestIsAshareCode30x:
    def test_302_segment_accepted(self):
        assert is_ashare_code("sz302132") is True
        assert is_ashare_code("302132") is True

    def test_30x_generic_segment_accepted(self):
        assert is_ashare_code("305999") is True
        assert is_ashare_code("sz309000") is True

    def test_300_301_still_accepted(self):
        assert is_ashare_code("sz300750") is True
        assert is_ashare_code("301111") is True

    def test_funds_and_indices_still_rejected(self):
        assert is_ashare_code("159915") is False
        assert is_ashare_code("sz399001") is False
        assert is_ashare_code("sh510300") is False
        assert is_ashare_code("bj430047") is False


# ---------------------------------------------------------------------------
# build_vendor_universe with tmp ZIP fixtures
# ---------------------------------------------------------------------------

def _csv_text(code: str, dates, *, total_share: str = "", float_share: str = "") -> str:
    """Build vendor-shaped CSV text (real column names, utf-8-sig at zip time)."""
    lines = [VENDOR_COLUMNS]
    for d in dates:
        lines.append(
            f"{code},{d},9.90,10.50,9.80,10.10,9.85,0.25,2.54,10000,50000,"
            f"1.20,1.50,0.90,15.0,14.2,1.60,2.10,2.00,1.10,1.20,"
            f"{total_share},{float_share},,,"
        )
    return "\n".join(lines) + "\n"


def _write_year_zip(root: Path, year: int, files: dict) -> Path:
    zpath = root / f"{year}.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for code, text in files.items():
            zf.writestr(f"{year}/{code}.csv", text.encode("utf-8-sig"))
    return zpath


class _FakeProvider:
    """Duck-typed stand-in exposing the LocalVendorProvider accessor pair."""

    def __init__(self, year_zips: dict):
        self._year_zips = {int(y): Path(p) for y, p in year_zips.items()}

    def available_years(self):
        return sorted(self._year_zips.keys())

    def year_zip_path(self, year):
        return self._year_zips.get(int(year))


@pytest.fixture()
def vendor_provider(tmp_path) -> _FakeProvider:
    """Three year archives (2023-2025) with:
      * 600000.SH  main-board stock, equity metadata present, all years
      * 510300.SH  ETF, no equity values, all years
      * 000001.SZ  stock, equity ok, absent from the latest year (2025)
      * 430047.BJ  BSE stock, equity ok, all years
      * 603999.SH  stock-like segment but NO equity metadata in any year
    """
    year_zips = {}
    for year in (2023, 2024, 2025):
        files = {
            "600000.SH": _csv_text(
                "600000.SH",
                [f"{year}-01-05", f"{year}-06-05"],
                total_share="1500000.0",
                float_share="1400000.0",
            ),
            "510300.SH": _csv_text("510300.SH", [f"{year}-03-01"]),
            "430047.BJ": _csv_text(
                "430047.BJ", [f"{year}-04-01"], total_share="52000.5"
            ),
            "603999.SH": _csv_text("603999.SH", [f"{year}-05-01"]),
        }
        if year != 2025:
            files["000001.SZ"] = _csv_text(
                "000001.SZ", [f"{year}-02-01"], float_share="19405918.19"
            )
        year_zips[year] = _write_year_zip(tmp_path, year, files)
    return _FakeProvider(year_zips)


REQUIRED_SUMMARY_KEYS = {
    "total_union",
    "snapshot_2024_a_share",
    "snapshot_2024_bse",
    "snapshot_latest_total",
    "historical_a_share",
    "historical_bse",
    "non_a_count",
    "excluded_count",
    "included_count",
    "absent_from_latest_count",
    "suspected_delisted_count",
    "unidentified_count",
    "latest_year",
}


class TestBuildVendorUniverse:
    def test_inclusion_and_exclusion(self, vendor_provider):
        uni = build_vendor_universe(vendor_provider, with_metadata=True)
        rows = {r["symbol"]: r for r in uni.rows}
        assert set(rows) == {
            "600000.SH",
            "510300.SH",
            "000001.SZ",
            "430047.BJ",
            "603999.SH",
        }

        main = rows["600000.SH"]
        assert main["inclusion_status"] == "included"
        assert main["canonical_symbol"] == "SSE.STK.600000"
        assert main["board"] == "sse_main"
        assert main["exclusion_reason"] == ""
        assert main["metadata_source"] == "vendor_csv+code_segment_rules"

        fund = rows["510300.SH"]
        assert fund["inclusion_status"] == "excluded"
        assert fund["instrument_type"] == "fund_etf"
        assert fund["exclusion_reason"] == "non_stock_instrument"

        no_equity = rows["603999.SH"]
        assert no_equity["instrument_type"] == "stock"
        assert no_equity["inclusion_status"] == "excluded"
        assert no_equity["exclusion_reason"] == "equity_metadata_missing"

        bse = rows["430047.BJ"]
        assert bse["inclusion_status"] == "included"
        assert bse["board"] == "bse"
        assert bse["canonical_symbol"] == "BSE.STK.430047"

    def test_seen_dates_years_and_row_counts(self, vendor_provider):
        uni = build_vendor_universe(vendor_provider, with_metadata=True)
        rows = {r["symbol"]: r for r in uni.rows}

        main = rows["600000.SH"]
        assert main["first_seen_date"] == "2023-01-05"
        assert main["last_seen_date"] == "2025-06-05"
        assert main["first_seen_year"] == 2023
        assert main["last_seen_year"] == 2025
        assert main["source_archive_count"] == 3
        assert main["source_row_count"] == 6  # 2 rows x 3 archives, no dedup

        gone = rows["000001.SZ"]
        assert gone["first_seen_year"] == 2023
        assert gone["last_seen_year"] == 2024
        assert gone["source_archive_count"] == 2
        assert gone["source_row_count"] == 2

    def test_latest_year_presence_and_survivorship(self, vendor_provider):
        uni = build_vendor_universe(vendor_provider, with_metadata=True)
        rows = {r["symbol"]: r for r in uni.rows}

        assert rows["600000.SH"]["present_in_latest_year"] is True
        assert rows["600000.SH"]["known_delisted"] == "unknown"
        assert rows["600000.SH"]["survivorship_risk"] == "baseline"

        gone = rows["000001.SZ"]
        assert gone["present_in_latest_year"] is False
        assert gone["known_delisted"] == "suspected_absent_latest_year"
        assert gone["survivorship_risk"] == "high"
        # absence from the latest archive must NOT exclude the stock
        assert gone["inclusion_status"] == "included"

    def test_summary_keys_and_counts(self, vendor_provider):
        uni = build_vendor_universe(vendor_provider, with_metadata=True)
        s = uni.summary
        assert REQUIRED_SUMMARY_KEYS <= set(s.keys())

        assert s["latest_year"] == 2025
        assert s["total_union"] == 5
        assert s["included_count"] == 3
        assert s["excluded_count"] == 2
        assert s["historical_a_share"] == 2  # 600000.SH + 000001.SZ
        assert s["historical_bse"] == 1
        assert s["snapshot_2024_a_share"] == 2
        assert s["snapshot_2024_bse"] == 1
        assert s["snapshot_latest_total"] == 2  # 600000.SH + 430047.BJ
        assert s["non_a_count"] == 1  # the ETF
        assert s["absent_from_latest_count"] == 1
        assert s["suspected_delisted_count"] == 1
        assert s["unidentified_count"] == 0

    def test_included_symbols_and_stable_hash(self, vendor_provider):
        uni = build_vendor_universe(vendor_provider, with_metadata=True)
        expected = ["BSE.STK.430047", "SSE.STK.600000", "SZSE.STK.000001"]
        assert uni.included_symbols == expected

        expected_hash = hashlib.sha256(",".join(expected).encode("utf-8")).hexdigest()
        assert uni.universe_hash == expected_hash

        # rebuild from the same archives -> identical hash
        again = build_vendor_universe(vendor_provider, with_metadata=True)
        assert again.universe_hash == uni.universe_hash

    def test_write_csv_columns(self, vendor_provider, tmp_path):
        uni = build_vendor_universe(vendor_provider, with_metadata=True)
        out = tmp_path / "out" / "vendor_universe.csv"
        uni.write_csv(out)

        with out.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            assert reader.fieldnames == list(ROW_FIELDS)
            read_rows = list(reader)
        assert len(read_rows) == len(uni.rows) == 5
        assert read_rows[0]["metadata_source"] == "vendor_csv+code_segment_rules"

    def test_without_metadata_fast_preview(self, vendor_provider):
        uni = build_vendor_universe(vendor_provider, with_metadata=False)
        rows = {r["symbol"]: r for r in uni.rows}

        # namelist-only mode: no dates / row counts, archive presence intact
        main = rows["600000.SH"]
        assert main["first_seen_date"] == ""
        assert main["last_seen_date"] == ""
        assert main["source_row_count"] == 0
        assert main["source_archive_count"] == 3
        assert main["first_seen_year"] == 2023
        assert main["last_seen_year"] == 2025
        assert main["inclusion_status"] == "included"

        # equity confirmation is skipped -> segment-only inclusion
        assert rows["603999.SH"]["inclusion_status"] == "included"
        # non-stock instruments still excluded by segment rules
        assert rows["510300.SH"]["inclusion_status"] == "excluded"
        # latest-year absence still derived from namelist presence
        assert rows["000001.SZ"]["present_in_latest_year"] is False
        assert rows["000001.SZ"]["survivorship_risk"] == "high"

    def test_progress_callback(self, vendor_provider):
        calls = []
        build_vendor_universe(
            vendor_provider,
            with_metadata=True,
            progress=lambda year, done, total: calls.append((year, done, total)),
        )
        assert calls == [(2023, 1, 3), (2024, 2, 3), (2025, 3, 3)]

    def test_row_field_order_matches_schema(self, vendor_provider):
        uni = build_vendor_universe(vendor_provider, with_metadata=True)
        for row in uni.rows:
            assert tuple(row.keys()) == ROW_FIELDS
