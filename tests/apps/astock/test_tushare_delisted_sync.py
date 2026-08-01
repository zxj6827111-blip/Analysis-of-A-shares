"""Gate B2 focused tests: pagination / validation / unit transform (offline)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from wtpy.apps.astock.data import tushare_delisted_sync as tds


def _frame(dates, close=10.0, vol=100.0, amount=101.0):
    n = len(dates)
    return pd.DataFrame(
        {
            "ts_code": ["600001.SH"] * n,
            "trade_date": [str(d) for d in dates],
            "open": [close] * n,
            "high": [close * 1.01] * n,
            "low": [close * 0.99] * n,
            "close": [close] * n,
            "vol": [vol] * n,
            "amount": [amount] * n,
        }
    )


class TestPagination:
    def test_single_page(self):
        calls = []

        def fetch(ts_code, end_date):
            calls.append(end_date)
            return _frame([20200103, 20200102, 20200101])

        df, pages = tds.fetch_daily_paginated(fetch, "600001.SH", page_rows=6000)
        assert pages == 1
        assert list(df["trade_date"]) == [20200101, 20200102, 20200103]
        assert calls == [""]

    def test_multi_page_walks_past_cap(self):
        # page size 3: first call returns newest 3, second the older 2
        pages_data = {
            "": _frame([20200105, 20200104, 20200103]),
            "20200102": _frame([20200102, 20200101]),
        }

        def fetch(ts_code, end_date):
            return pages_data.get(end_date, pd.DataFrame())

        df, pages = tds.fetch_daily_paginated(fetch, "600001.SH", page_rows=3)
        assert pages == 2
        assert list(df["trade_date"]) == [
            20200101,
            20200102,
            20200103,
            20200104,
            20200105,
        ]

    def test_overlap_dedup(self):
        all_days = [20200101, 20200102, 20200103, 20200104, 20200105]

        def fetch(ts_code, end_date):
            # API quirk stub: second page overlaps the first (includes 0103)
            if end_date == "":
                return _frame([20200105, 20200104, 20200103])
            return _frame([20200103, 20200102, 20200101])

        df, _ = tds.fetch_daily_paginated(fetch, "600001.SH", page_rows=3)
        assert list(df["trade_date"]) == all_days
        assert df["trade_date"].duplicated().sum() == 0

    def test_no_progress_page_stops(self):
        def fetch(ts_code, end_date):
            return _frame([20200103, 20200102, 20200101])  # same page forever

        df, pages = tds.fetch_daily_paginated(fetch, "600001.SH", page_rows=3)
        assert pages == 1  # identical second page detected as no-progress
        assert list(df["trade_date"]) == [20200101, 20200102, 20200103]

    def test_empty_first_page_is_no_data(self):
        df, pages = tds.fetch_daily_paginated(
            lambda ts_code, end_date: pd.DataFrame(), "600001.SH"
        )
        assert df.empty and pages == 0

    def test_runaway_pagination_guarded(self):
        state = {"base": 20200401}

        def fetch(ts_code, end_date):
            # always-progressing full pages that never end
            base = state["base"]
            state["base"] -= 100
            return _frame([base + 3, base + 2, base + 1])

        with pytest.raises(RuntimeError, match="pagination exceeded"):
            tds.fetch_daily_paginated(fetch, "600001.SH", page_rows=3, max_pages=4)


class TestValidation:
    def test_clean_frame_ok(self):
        r = tds.validate_daily_frame(_frame([20200101, 20200102]))
        assert r.ok and r.row_count == 2
        assert r.first_date == 20200101 and r.last_date == 20200102

    def test_empty_rejected(self):
        r = tds.validate_daily_frame(pd.DataFrame())
        assert not r.ok and r.reasons == ["empty"]

    def test_duplicate_dates_rejected(self):
        df = pd.concat([_frame([20200101]), _frame([20200101])], ignore_index=True)
        r = tds.validate_daily_frame(df)
        assert not r.ok
        assert any("duplicate_trade_dates" in x for x in r.reasons)

    def test_ohlc_invalid_rejected(self):
        df = _frame([20200101])
        df.loc[0, "high"] = 5.0  # below low/open/close
        r = tds.validate_daily_frame(df)
        assert not r.ok
        assert any("ohlc_invalid" in x for x in r.reasons)

    def test_nonpositive_close_rejected(self):
        df = _frame([20200101])
        df.loc[0, "close"] = 0.0
        r = tds.validate_daily_frame(df)
        assert not r.ok
        assert any("nonpositive_close" in x for x in r.reasons)

    def test_negative_volume_rejected(self):
        df = _frame([20200101])
        df.loc[0, "vol"] = -1.0
        r = tds.validate_daily_frame(df)
        assert not r.ok
        assert any("negative_volume" in x for x in r.reasons)

    def test_malformed_date_rejected(self):
        df = _frame([20200101])
        df.loc[0, "trade_date"] = "18991231"
        r = tds.validate_daily_frame(df)
        assert not r.ok
        assert any("malformed_dates" in x for x in r.reasons)


class TestUnitTransform:
    def test_vol_hand_to_shares_amount_kcny_to_cny(self):
        arrays = tds.transform_to_bar_arrays(
            _frame([20200101], close=10.0, vol=123.0, amount=456.0)
        )
        assert arrays["volume"][0] == pytest.approx(12300.0)
        assert arrays["amount"][0] == pytest.approx(456000.0)
        assert arrays["close"][0] == pytest.approx(10.0)
        assert arrays["trade_date"].dtype == np.int64

    def test_nan_volume_becomes_zero(self):
        df = _frame([20200101])
        df.loc[0, "vol"] = float("nan")
        arrays = tds.transform_to_bar_arrays(df)
        assert arrays["volume"][0] == 0.0

    def test_unit_ratio_consistency(self):
        # implied price = amount(元) / volume(股) must approximate close
        df = _frame([20200101], close=10.0, vol=1000.0, amount=1000.0)
        arrays = tds.transform_to_bar_arrays(df)
        implied = arrays["amount"][0] / arrays["volume"][0]
        assert implied == pytest.approx(10.0)


class TestCutoffClip:
    def test_clip(self):
        df = _frame([20260716, 20260717, 20260718])
        out = tds.clip_to_cutoff(df, 20260717)
        assert list(pd.to_numeric(out["trade_date"])) == [20260716, 20260717]


class TestErrorClassification:
    @pytest.mark.parametrize(
        "msg,expected",
        [
            ("抱歉，您没有访问该接口的权限", "permission_denied"),
            ("您的积分不足", "permission_denied"),
            ("token不对", "auth_failed"),
            ("每分钟最多访问该接口500次 limit", "rate_limited"),
            ("Read timed out", "timeout"),
            ("Connection aborted", "network_error"),
            ("some other failure", "api_failed"),
        ],
    )
    def test_classification(self, msg, expected):
        assert tds.classify_sync_error(RuntimeError(msg)) == expected

    def test_scrub_secret(self):
        s = tds.scrub_secret("token=abcdef0123456789abcdef0123456789 failed")
        assert "abcdef0123456789" not in s
        assert "<scrubbed>" in s


class TestOhlcRepair:
    def test_close_below_low_repaired(self):
        # real case shape: 000015.SZ 19921016 close < low
        df = _frame([20200101])
        df.loc[0, ["open", "high", "low", "close"]] = [17.35, 17.70, 17.35, 17.10]
        out, dates = tds.repair_ohlc_envelope(df)
        assert dates == [20200101]
        assert out.loc[0, "low"] == pytest.approx(17.10)
        assert out.loc[0, "high"] == pytest.approx(17.70)
        assert tds.validate_daily_frame(out).ok

    def test_open_above_high_repaired(self):
        df = _frame([20200101])
        df.loc[0, ["open", "high", "low", "close"]] = [18.0, 17.5, 17.0, 17.2]
        out, dates = tds.repair_ohlc_envelope(df)
        assert dates == [20200101]
        assert out.loc[0, "high"] == pytest.approx(18.0)

    def test_clean_rows_untouched(self):
        df = _frame([20200101, 20200102])
        out, dates = tds.repair_ohlc_envelope(df)
        assert dates == []
        pd.testing.assert_frame_equal(out, df)

    def test_close_preserved_never_altered(self):
        df = _frame([20200101])
        df.loc[0, ["open", "high", "low", "close"]] = [17.35, 17.70, 17.35, 17.10]
        out, _ = tds.repair_ohlc_envelope(df)
        assert out.loc[0, "close"] == pytest.approx(17.10)

    def test_repair_ratio_constant_sane(self):
        assert 0 < tds.OHLC_REPAIR_MAX_RATIO <= 0.01


class TestStatusMachine:
    def test_status_values_complete(self):
        assert set(tds.ALL_STATUSES) == {
            "pending",
            "downloading",
            "downloaded",
            "validated",
            "no_data",
            "failed",
            "published",
        }
