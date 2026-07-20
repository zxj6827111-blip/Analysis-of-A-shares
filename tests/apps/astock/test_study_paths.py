"""Forward path MFE/MAE study tests."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.study import event_path_stats


def test_event_path_next_open_and_mfe_mae():
    bars = [
        DayBar(20240102, 10, 10.5, 9.5, 10, 1, 1),  # signal
        DayBar(20240103, 11, 13, 10, 12, 1, 1),  # entry open 11; high 13
        DayBar(20240104, 12, 14, 11, 13, 1, 1),  # path
        DayBar(20240105, 12.5, 13, 12, 12.5, 1, 1),  # exit open for hold=2 -> entry+2
    ]
    # signal index 0, hold=2 => entry i=1, exit i=3
    evs = event_path_stats(bars, [0], hold_periods=2, period="DAY")
    assert len(evs) == 1
    e = evs[0]
    assert e["entry_date"] == 20240103
    assert e["exit_date"] == 20240105
    assert abs(e["ret"] - (12.5 / 11 - 1)) < 1e-9
    # path highs entry..exit-1 = 13,14
    assert abs(e["mfe"] - (14 / 11 - 1)) < 1e-9
    assert abs(e["mae"] - (10 / 11 - 1)) < 1e-9
