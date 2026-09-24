# -*- coding: utf-8 -*-
"""EOD「每周最后一个交易日」调度（eod_schedule + eod_sync_decide 新模式）。"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wtpy.apps.astock.api import eod_sync_day_gate, eod_sync_decide
from wtpy.apps.astock.service import eod_schedule as sch


# --------------------------------------------------------------------------
# 周界判定（用 2026 中秋周做真实路径：2026-09-25 周五为中秋假期）
# --------------------------------------------------------------------------

# 覆盖中秋+国庆的迷你日历（开/休市与 trade_cal 事实一致）
_MINI_OPEN_DAYS = [
    20260921, 20260922, 20260923, 20260924,  # 周一~周四开市（周五 9/25 中秋休市）
    20260928, 20260929, 20260930,            # 国庆前三天开市
    20261008, 20261009,                      # 10/5~10/7 休市，10/8(四) 起开市
    20261012, 20261013                       # 覆盖下周（保证本周判定有据可依）
]


def test_week_last_trading_day_midautumn_week_is_thursday():
    # 2026-09-21(一) 当周最后交易日 = 9-24(四)，不是 9-25(五，中秋休市)
    assert sch.week_last_trading_day(_MINI_OPEN_DAYS, 20260921) == 20260924
    assert sch.week_last_trading_day(_MINI_OPEN_DAYS, 20260924) == 20260924
    assert sch.week_last_trading_day(_MINI_OPEN_DAYS, 20260925) == 20260924


def test_is_last_trading_day_of_week_verdicts():
    assert sch.is_last_trading_day_of_week(_MINI_OPEN_DAYS, 20260924) is True
    assert sch.is_last_trading_day_of_week(_MINI_OPEN_DAYS, 20260923) is False


def test_week_whole_holiday_has_no_trigger_day():
    # 国庆整周（2026-10-05~09 周一至周日里 10/5~7 全休，10/8 起在下一周）
    # 构造：把 10/8、10/9 从日历里去掉，则 10/5~10/11 这一周没有任何交易日
    days = [d for d in _MINI_OPEN_DAYS if d not in (20261008, 20261009)]
    assert sch.week_last_trading_day(days, 20261007) is None
    assert sch.is_last_trading_day_of_week(days, 20261007) is False


def test_calendar_not_covering_week_end_is_undecidable():
    # 日历只到 9/24（不覆盖本周日 9/27）→ None，调用方应退化固定周几
    short = [d for d in _MINI_OPEN_DAYS if d <= 20260924]
    assert sch.is_last_trading_day_of_week(short, 20260921) is None
    assert sch.is_last_trading_day_of_week(short, 20260924) is None


# --------------------------------------------------------------------------
# eod_sync_decide：last_trading_day 模式
# --------------------------------------------------------------------------


def _wd(d: int) -> _dt.datetime:
    """YYYYMMDD -> 当天 18:40（已过 18:30 触发时点）。"""
    return _dt.datetime(d // 10000, (d // 100) % 100, d % 100, 18, 40)


def test_decide_triggers_on_week_last_trading_day_not_friday():
    # 周四 2026-09-24 = 中秋前最后交易日：触发（旧固定周五口径下不会触发）
    trigger, reason, today = eod_sync_decide(
        lag=2, now=_wd(20260924), schedule_mode="last_trading_day",
        trading_days=_MINI_OPEN_DAYS,
    )
    assert trigger is True
    assert today == _dt.date(2026, 9, 24)
    assert "本周最后交易日" in reason


def test_decide_skips_friday_when_holiday_shifts_week_end():
    # 中秋周五 9/25 休市：不触发——旧口径会在周五空跑（无数据）
    trigger, reason, _ = eod_sync_decide(
        lag=5, now=_wd(20260925), schedule_mode="last_trading_day",
        trading_days=_MINI_OPEN_DAYS,
    )
    assert trigger is False
    assert "非本周最后交易日" in reason


def test_decide_skips_non_trigger_weekday():
    trigger, reason, _ = eod_sync_decide(
        lag=5, now=_wd(20260923), schedule_mode="last_trading_day",
        trading_days=_MINI_OPEN_DAYS,
    )
    assert trigger is False
    assert "20260924" in reason


def test_decide_falls_back_to_weekday_when_calendar_missing_or_stale():
    # 无日历：退化为固定周几（周五 9/18 才触发，周四 9/17 不触发）
    thu = _wd(20260917)  # 2026-09-17 周四
    trigger, reason, _ = eod_sync_decide(
        lag=2, now=thu, schedule_mode="last_trading_day", trading_days=None,
    )
    assert trigger is False
    friday = _wd(20260918)
    trigger, reason, _ = eod_sync_decide(
        lag=2, now=friday, schedule_mode="last_trading_day", trading_days=None,
    )
    assert trigger is True
    assert "周历日" in reason

    # 日历覆盖不到本周日（9/18 当周周日=9/20，日历只到 9/15）→ 同样退化
    stale = [20260914, 20260915]
    assert stale[-1] < 20260920
    trigger, reason, _ = eod_sync_decide(
        lag=2, now=friday, schedule_mode="last_trading_day", trading_days=stale,
    )
    assert trigger is True
    # 但周四不触发（退化后仍固定周五）
    trigger, reason, _ = eod_sync_decide(
        lag=2, now=thu, schedule_mode="last_trading_day", trading_days=stale,
    )
    assert trigger is False


def test_decide_weekday_mode_ignores_calendar():
    # schedule_mode=weekday 时即便日历说周四是末交易日，也只在 sync_weekday 触发
    trigger, reason, _ = eod_sync_decide(
        lag=5, now=_wd(20260924), schedule_mode="weekday", sync_weekday=4,
        trading_days=_MINI_OPEN_DAYS,
    )
    assert trigger is False
    trigger, reason, _ = eod_sync_decide(
        lag=5, now=_wd(20260925), schedule_mode="weekday", sync_weekday=4,
        trading_days=_MINI_OPEN_DAYS,
    )
    assert trigger is True


def test_day_gate_before_time_blocks_trigger_day():
    # 触发日但未到 18:30
    trigger, reason, _ = eod_sync_decide(
        lag=5, now=_dt.datetime(2026, 9, 24, 18, 20),
        schedule_mode="last_trading_day", trading_days=_MINI_OPEN_DAYS,
    )
    assert trigger is False
    assert "未到自动同步时间" in reason


def test_backward_compatible_defaults_equal_weekday_friday():
    # 不传新模式参数：行为与旧版逐字节一致（周五触发、周内其他天不触发）
    trigger, reason, today = eod_sync_decide(
        lag=2, now=_dt.datetime(2026, 8, 14, 18, 40),
        sync_time="18:30", sync_weekday=4, min_lag=1,
    )
    assert trigger is True and today == _dt.date(2026, 8, 14)
    trigger, reason, _ = eod_sync_decide(
        lag=2, now=_dt.datetime(2026, 8, 13, 18, 40), sync_weekday=4,
    )
    assert trigger is False


# --------------------------------------------------------------------------
# 循环候选日（工作日都醒来，日历门在 _check 内低成本判定）
# --------------------------------------------------------------------------


def test_next_candidate_at_skips_weekends():
    fri = _dt.datetime(2026, 9, 18, 18, 40)  # 周五已过 sync_time
    nxt = sch.next_candidate_at(fri, "18:30", weekdays={0, 1, 2, 3, 4})
    assert nxt == _dt.datetime(2026, 9, 21, 18, 30)  # 下一个周一 18:30
    mon_before = _dt.datetime(2026, 9, 21, 18, 29)
    nxt = sch.next_candidate_at(mon_before, "18:30", weekdays={0, 1, 2, 3, 4})
    assert nxt == _dt.datetime(2026, 9, 21, 18, 30)


def test_normalize_schedule_mode_aliases_and_default():
    assert sch.normalize_schedule_mode("last_trading_day") == "last_trading_day"
    assert sch.normalize_schedule_mode("WEEKDAY") == "weekday"
    assert sch.normalize_schedule_mode(None) == "last_trading_day"
    assert sch.normalize_schedule_mode("bogus") == "last_trading_day"


# --------------------------------------------------------------------------
# 前瞻日历缓存（读写/刷新/退化）
# --------------------------------------------------------------------------


def test_forward_calendar_roundtrip(tmp_path):
    p = sch.save_forward_open_dates(tmp_path, [20260924, 20260923, 20260923])
    assert p.name == "trade_cal_forward.json"
    assert sch.load_forward_open_dates(tmp_path) == [20260923, 20260924]
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["schema"] == sch.FORWARD_CALENDAR_VERSION
    assert raw["years"] == [2026]


def test_forward_calendar_missing_or_corrupt_returns_none(tmp_path):
    assert sch.load_forward_open_dates(tmp_path) is None
    (tmp_path / "trade_cal_forward.json").write_text("{bad json", encoding="utf-8")
    assert sch.load_forward_open_dates(tmp_path) is None
    # 空开市日也视为无缓存（交易所每周必有交易日，空=没拉到）
    sch.save_forward_open_dates(tmp_path, [])
    assert sch.load_forward_open_dates(tmp_path) is None


def test_refresh_forward_calendar_skips_when_fresh_and_covering(
    tmp_path, monkeypatch
):
    now = _dt.datetime(2026, 9, 23, 12, 0)
    sch.save_forward_open_dates(tmp_path, [20260923, 20260924, 20261030])

    def _boom(**_kw):
        raise AssertionError("不该联网")

    monkeypatch.setattr(sch, "fetch_forward_open_dates", _boom)
    assert sch.refresh_forward_calendar(tmp_path, now=now) is True


def test_refresh_forward_calendar_fetches_when_stale(tmp_path, monkeypatch):
    now = _dt.datetime(2026, 9, 23, 12, 0)
    # 陈旧缓存（fetched_at 是 30 天前）
    sch.save_forward_open_dates(
        tmp_path, [20260824, 20260825, 20260826],
        fetched_at="2026-08-24 00:00:00",
    )
    called = {}

    def _fake_fetch(*, token=None, start=None, end=None):
        called["args"] = (start, end)
        return _MINI_OPEN_DAYS

    monkeypatch.setattr(sch, "fetch_forward_open_dates", _fake_fetch)
    assert sch.refresh_forward_calendar(tmp_path, now=now) is True
    assert called["args"][0] <= 20260923 <= called["args"][1]
    assert sch.load_forward_open_dates(tmp_path) == _MINI_OPEN_DAYS


def test_refresh_forward_calendar_failure_keeps_old_cache(tmp_path, monkeypatch):
    sch.save_forward_open_dates(
        tmp_path, _MINI_OPEN_DAYS, fetched_at="2026-08-01 00:00:00"
    )

    def _boom(**_kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(sch, "fetch_forward_open_dates", _boom)
    # 拉取失败：旧缓存继续可用（不丢判定依据），并返回 True
    assert sch.refresh_forward_calendar(tmp_path, now=_dt.datetime(2026, 9, 23)) is True
    assert sch.load_forward_open_dates(tmp_path) == _MINI_OPEN_DAYS


def test_refresh_forward_calendar_failure_without_cache_is_not_usable(
    tmp_path, monkeypatch
):
    def _boom(**_kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(sch, "fetch_forward_open_dates", _boom)
    assert sch.refresh_forward_calendar(tmp_path) is False
