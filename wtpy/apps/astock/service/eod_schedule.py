# -*- coding: utf-8 -*-
"""EOD 自动同步的「每周最后一个交易日」调度。

为什么需要（2026-09 中秋场景）：固定周五触发在节前一周会错过——例如
2026-09-25（周五，中秋节）休市，本周最后交易日是 9-24（周四），数据整个
假期都不新鲜。新口径：每周在**该周最后一个交易日**收盘后自动更新。

判定依据是 Tushare ``trade_cal`` 前瞻日历：交易所会提前发布全年开/休市
安排，因此可以提前知道本周五是否休市。沪深北节假日一致，SSE 日历即全市场
口径。日历按年缓存在 ``storage/astock/trade_cal_forward.json``（schema v1）。

日历不可判定时（缓存缺失、过期或不覆盖本周日）一律**退化回固定周几**
（``ASTOCK_EOD_SYNC_WEEKDAY``，默认周五）——绝不静默停更；待日历刷新后
自动恢复周界口径。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path
from typing import Any, List, Optional, Sequence

logger = logging.getLogger(__name__)

SCHEDULE_LAST_TRADING_DAY = "last_trading_day"
SCHEDULE_WEEKDAY = "weekday"

#: 日历缓存可读的最小年份覆盖：本年。年底一周会顺带拉次年，见 refresh。
FORWARD_CALENDAR_VERSION = 1

#: 缓存默认最大龄期（自然日）；超过即联网刷新（交易所临时调整节假日时
#: 一周内会被吸收）。
DEFAULT_CALENDAR_MAX_AGE_DAYS = 7

#: 前视窗口：至少覆盖到下周日的所有判定（一周判定只需看本周日；多取一段
#: 以降低刷新频率，Tushare 单次调用即全年）。
DEFAULT_CALENDAR_LOOKBACK_DAYS = 15
DEFAULT_CALENDAR_FORWARD_DAYS = 365


def normalize_schedule_mode(value: Any, *, default: str = SCHEDULE_LAST_TRADING_DAY) -> str:
    """归一化调度模式：last_trading_day（默认）| weekday（旧固定周几）。"""
    if value is None:
        return default
    v = str(value).strip().lower()
    aliases = {
        "last_trading_day": SCHEDULE_LAST_TRADING_DAY,
        "last-trading-day": SCHEDULE_LAST_TRADING_DAY,
        "week_last_trading_day": SCHEDULE_LAST_TRADING_DAY,
        "trading_day": SCHEDULE_LAST_TRADING_DAY,
        "weekly_last_trading_day": SCHEDULE_LAST_TRADING_DAY,
        "weekday": SCHEDULE_WEEKDAY,
        "weekly_weekday": SCHEDULE_WEEKDAY,
        "weekly": SCHEDULE_WEEKDAY,
    }
    return aliases.get(v, default)


def forward_calendar_path(storage_root: Path) -> Path:
    """前瞻交易日历缓存路径（与 eod_sync_state.json 同目录，覆盖单仓库部署）。"""
    return Path(storage_root) / "trade_cal_forward.json"


def load_forward_open_dates(storage_root: Path) -> Optional[List[int]]:
    """读取缓存的开市日序列（YYYYMMDD 升序）；缺失/损坏/版本不符 → None。

    返回 None 而非空列表：空结果与"未取到"必须可区分，前者不该再视为
    有效缓存（交易所每周都有交易日）。
    """
    try:
        p = forward_calendar_path(storage_root)
        if not p.exists():
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
        if int(raw.get("schema") or 0) != FORWARD_CALENDAR_VERSION:
            return None
        dates = raw.get("open_dates")
        if not isinstance(dates, list) or not dates:
            return None
        out = sorted({int(d) for d in dates})
        return out or None
    except Exception:  # noqa: BLE001 — 缓存损坏与缺失同等处理（重建）
        return None


def forward_calendar_fetched_at(storage_root: Path) -> Optional[str]:
    """缓存的拉取时间戳（ISO 字符串）；不可读 → None。"""
    try:
        p = forward_calendar_path(storage_root)
        if not p.exists():
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
        ts = str(raw.get("fetched_at") or "").strip()
        return ts or None
    except Exception:  # noqa: BLE001
        return None


def save_forward_open_dates(
    storage_root: Path,
    open_dates: Sequence[int],
    *,
    fetched_at: Optional[str] = None,
) -> Path:
    """原子写入前瞻日历缓存（schema v1）。

    升序去重 + 强制 int 化：调用方传入任何 iterable 都落成稳定内容，
    便于内容比对（tests 断言幂等）。
    """
    from ..data.io_util import atomic_write_json

    dates = sorted({int(d) for d in open_dates})
    years = sorted({int(d) // 10000 for d in dates})
    payload = {
        "schema": FORWARD_CALENDAR_VERSION,
        "exchange": "SSE",
        "fetched_at": fetched_at or _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "years": years,
        "count": len(dates),
        "open_dates": dates,
    }
    path = forward_calendar_path(storage_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)
    return path


def fetch_forward_open_dates(
    *,
    token: Optional[str] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
) -> List[int]:
    """经 TushareProvider 拉取 SSE 交易日历的开市日（升序）。

    provider 统一承担限速/重试/超时；本函数不做结果校验，空结果原样返回
    （调用方决定是不可用还是真的没有交易日——贴年内必有）。
    """
    from ..data.providers.tushare import TushareProvider

    provider = TushareProvider(token=token)
    return provider.fetch_trade_calendar(start_date=start, end_date=end)


def refresh_forward_calendar(
    cfg_storage_root: Path,
    *,
    token: Optional[str] = None,
    max_age_days: int = DEFAULT_CALENDAR_MAX_AGE_DAYS,
    now: Optional[_dt.datetime] = None,
) -> bool:
    """必要时刷新前瞻日历缓存，返回「缓存此刻是否可用」。

    只在缓存过期**或**覆盖不到下周日时才联网（避免每周一次 EOD 定时线程
    频繁打 API）；拉取失败/结果为空时保留旧缓存并返回 False，调用方据此
    退化到固定周几规则。
    """
    now = now or _dt.datetime.now()
    existing = load_forward_open_dates(cfg_storage_root)
    if existing is not None:
        fetched = forward_calendar_fetched_at(cfg_storage_root)
        fresh_enough = False
        if fetched:
            try:
                age = now - _dt.datetime.strptime(fetched, "%Y-%m-%d %H:%M:%S")
                fresh_enough = age <= _dt.timedelta(days=max(1, int(max_age_days)))
            except (TypeError, ValueError):
                fresh_enough = False
        # 覆盖检查：必须能看到下周日之后，否则下周的周界判定会退化——
        # 有效期临近尾声的缓存提前刷新，让「下一周」的判定同样有据可依。
        horizon_end = int(
            (now.date() + _dt.timedelta(days=14)).strftime("%Y%m%d")
        )
        covers = existing[-1] >= horizon_end
        if fresh_enough and covers:
            return True
    try:
        start = int(
            (now.date() - _dt.timedelta(days=DEFAULT_CALENDAR_LOOKBACK_DAYS))
            .strftime("%Y%m%d")
        )
        end = int(
            (now.date() + _dt.timedelta(days=DEFAULT_CALENDAR_FORWARD_DAYS))
            .strftime("%Y%m%d")
        )
        dates = fetch_forward_open_dates(token=token, start=start, end=end)
    except Exception as e:  # noqa: BLE001 — 网络/限流/缺 token：降级路径处理
        logger.warning("前瞻交易日历刷新失败（沿用旧缓存/退化规则）: %s", e)
        return existing is not None
    if not dates:
        logger.warning("前瞻交易日历拉取返回空（沿用旧缓存/退化规则）")
        return existing is not None
    save_forward_open_dates(cfg_storage_root, dates)
    return True


def _to_date(d: int) -> _dt.date:
    return _dt.date(int(d) // 10000, (int(d) // 100) % 100, int(d) % 100)


def _week_bounds(d: _dt.date) -> tuple:
    """(周一, 周日) civil 区间。"""
    iso_y, iso_w, _ = d.isocalendar()
    week_start = _dt.date.fromisocalendar(iso_y, iso_w, 1)
    return week_start, week_start + _dt.timedelta(days=6)


def week_last_trading_day(
    open_dates: Optional[Sequence[int]], day: int
) -> Optional[int]:
    """``day`` 所在 ISO 周的最后一个交易日。

    返回 None 的三种情形：日历为空、日历覆盖不到本周日（不可判定）、
    本周全周休市（无最后交易日）。需要区分时用 is_last_trading_day_of_week。
    覆盖判据：最新开市日 ≥ 本周日即可——交易所会连续发布整年日历，本周
    休市安排在覆盖范围内即视为完整。
    """
    if not open_dates:
        return None
    week_start, week_end = _week_bounds(_to_date(int(day)))
    if int(open_dates[-1]) < int(week_end.strftime("%Y%m%d")):
        return None
    in_week = [
        int(d)
        for d in open_dates
        if week_start <= _to_date(int(d)) <= week_end
    ]
    return in_week[-1] if in_week else None


def is_last_trading_day_of_week(
    open_dates: Optional[Sequence[int]], day: int
) -> Optional[bool]:
    """``day`` 是否其所在周的最后一个交易日。

    True = 是；False = 不是（自然日错开，或本周全周休市）；None = 日历
    覆盖不到本周日，不可判定（调用方退化到固定周几规则）。
    """
    if not open_dates:
        return None
    week_start, week_end = _week_bounds(_to_date(int(day)))
    if int(open_dates[-1]) < int(week_end.strftime("%Y%m%d")):
        return None
    in_week = [
        int(d)
        for d in open_dates
        if week_start <= _to_date(int(d)) <= week_end
    ]
    if not in_week:
        return False
    return int(day) == in_week[-1]


def schedule_mode_label(mode: str, sync_weekday: int = 4, sync_time: str = "18:30") -> str:
    """给 UI/状态文件的人类可读文案。"""
    if mode == SCHEDULE_LAST_TRADING_DAY:
        return f"每周最后一个交易日 {sync_time}"
    wd = {0: "一", 1: "二", 2: "三", 3: "四", 4: "五", 5: "六", 6: "日"}.get(
        int(sync_weekday), str(sync_weekday)
    )
    return f"每周{wd} {sync_time}"


def next_candidate_at(now: _dt.datetime, hhmm: str, *, weekdays: set) -> _dt.datetime:
    """``now`` 之后下一个属于 ``weekdays``（0=周一）指定日期 hh:mm 的时刻。"""
    try:
        hour, minute = int(hhmm[:2]), int(hhmm[3:5])
    except (ValueError, IndexError):
        hour, minute = 18, 30
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += _dt.timedelta(days=1)
    while target.weekday() not in weekdays:
        target += _dt.timedelta(days=1)
    return target
