# -*- coding: utf-8 -*-
"""Offline tests for Tushare corporate-action event ingestion."""

from __future__ import annotations

import json

import pytest

from wtpy.apps.astock.ca_ledger import CA_CASH_DIVIDEND, CA_SHARE_RATIO
from wtpy.apps.astock.data import tushare_ca_fetcher as ca_fetcher


@pytest.mark.parametrize(
    ("standard_code", "ts_code"),
    [
        ("SSE.STK.600000", "600000.SH"),
        ("SZSE.STK.000001", "000001.SZ"),
        ("BSE.STK.430047", "430047.BJ"),
    ],
)
def test_code_conversion_round_trip(standard_code, ts_code):
    assert ca_fetcher.standard_to_tushare_code(standard_code) == ts_code
    assert ca_fetcher.tushare_to_standard_code(ts_code) == standard_code


def test_dividend_rows_build_explicit_events_and_filter():
    rows = [
        {
            "ts_code": "600000.SH",
            "div_proc": "\u5b9e\u65bd",
            "ex_date": "20240115",
            "cash_div": 0.1,
            "stk_div": 0.2,
            "stk_bo_rate": 0.2,
            "stk_co_rate": 0.3,
        },
        {
            "ts_code": "600000.SH",
            "div_proc": "planned",
            "ex_date": "20240116",
            "cash_div": 0.5,
            "stk_div": 0.5,
        },
        {
            "ts_code": "600000.SH",
            "div_proc": "implemented",
            "ex_date": "20230101",
            "cash_div": 0.8,
            "stk_div": 0.8,
        },
        {
            "ts_code": "600000.SH",
            "div_proc": "",
            "ex_date": "20240117",
            "cash_div": 0.7,
            "stk_div": 0.7,
        },
    ]

    events = ca_fetcher.dividend_rows_to_events(
        rows,
        start_date=20240101,
        end_date=20241231,
    )

    assert len(events) == 2
    by_type = {event.event_type: event for event in events}
    assert by_type[CA_CASH_DIVIDEND].cash_per_share == pytest.approx(0.1)
    assert by_type[CA_SHARE_RATIO].share_multiplier == pytest.approx(1.2)
    assert {event.std_code for event in events} == {"SSE.STK.600000"}
    assert {event.date for event in events} == {20240115}


def test_fetch_filters_locally_and_uses_supported_api_parameters(tmp_path):
    calls = []

    class FakeProvider:
        def dividend(self, **kwargs):
            calls.append(kwargs)
            return [
                {
                    "ts_code": "600000.SH",
                    "div_proc": "implemented",
                    "ex_date": "20240115",
                    "cash_div": 0.1,
                },
                {
                    "ts_code": "600000.SH",
                    "div_proc": "implemented",
                    "ex_date": "20250115",
                    "cash_div": 0.2,
                },
            ]

    ca_fetcher.TUSHARE_CA_CACHE.clear()
    events = ca_fetcher.fetch_dividend_events(
        "SSE.STK.600000",
        start_date=20240101,
        end_date=20241231,
        root=tmp_path,
        refresh=True,
        pro=FakeProvider(),
    )

    assert len(events) == 1
    assert events[0].date == 20240115
    assert calls == [
        {
            "ts_code": "600000.SH",
            "fields": ca_fetcher.DIVIDEND_FIELDS,
        }
    ]
    assert "start_date" not in calls[0]
    assert "end_date" not in calls[0]


def test_legacy_tushare_code_cache_is_normalized(tmp_path):
    payload = [
        {
            "std_code": "600000.SH",
            "date": 20240115,
            "event_type": CA_CASH_DIVIDEND,
            "share_multiplier": 1.0,
            "cash_per_share": 0.1,
            "note": "legacy",
            "source": "tushare_dividend",
            "meta": {},
        }
    ]
    (tmp_path / "600000.SH.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    events = ca_fetcher.load_cached_dividend_events(
        "SSE.STK.600000",
        root=tmp_path,
    )

    assert len(events) == 1
    assert events[0].std_code == "SSE.STK.600000"


def test_cached_events_metadata_records_selected_ledger(tmp_path):
    events = {
        "SSE.STK.600000": [
            ca_fetcher.CorporateActionEvent(
                std_code="SSE.STK.600000",
                date=20240115,
                event_type=CA_CASH_DIVIDEND,
                cash_per_share=0.1,
                source="tushare_dividend",
            )
        ]
    }
    (tmp_path / "_meta.json").write_text(
        json.dumps(
            {
                "last_sync_at": "2026-07-30 12:00:00",
                "last_sync_mode": "full",
                "as_of_date": 20260730,
                "ex_date_range": "19900101~20260730",
                "success": 1,
                "failed": 0,
                "nonempty_symbols": 1,
                "event_count": 1,
            }
        ),
        encoding="utf-8",
    )

    meta = ca_fetcher.cached_events_metadata(
        events,
        root=tmp_path,
        requested_codes=["SSE.STK.600000", "SZSE.STK.000001"],
    )

    assert meta["requested_symbol_count"] == 2
    assert meta["event_symbol_count"] == 1
    assert meta["event_count"] == 1
    assert meta["sync_failed"] == 0
    assert meta["sync_as_of_date"] == 20260730
    assert meta["sync_ex_date_range"] == "19900101~20260730"
    assert len(meta["event_manifest_sha256"]) == 64
    assert len(meta["cache_meta_sha256"]) == 64