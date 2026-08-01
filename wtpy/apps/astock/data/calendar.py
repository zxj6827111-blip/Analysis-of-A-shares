"""Trading calendar derived from index daily bars (not expired holiday files)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .tdx_reader import parse_day_file

# Holiday / non-session resolution when a planned calendar date is not a trading day.
HOLIDAY_POLICY_NEXT = "next_trading_day"
HOLIDAY_POLICY_PREV = "previous_trading_day"
HOLIDAY_POLICY_SKIP = "skip_trade"
HOLIDAY_POLICY_EXACT = "exact_weekday_only"

DEFAULT_HOLIDAY_POLICY = HOLIDAY_POLICY_NEXT

KNOWN_HOLIDAY_POLICIES = (
    HOLIDAY_POLICY_NEXT,
    HOLIDAY_POLICY_PREV,
    HOLIDAY_POLICY_SKIP,
    HOLIDAY_POLICY_EXACT,
)


def normalize_holiday_policy(value: Optional[str], *, default: str = DEFAULT_HOLIDAY_POLICY) -> str:
    if value is None or str(value).strip() == "":
        return default
    p = str(value).strip().lower()
    aliases = {
        "next": HOLIDAY_POLICY_NEXT,
        "forward": HOLIDAY_POLICY_NEXT,
        "roll_forward": HOLIDAY_POLICY_NEXT,
        "prev": HOLIDAY_POLICY_PREV,
        "previous": HOLIDAY_POLICY_PREV,
        "roll_back": HOLIDAY_POLICY_PREV,
        "skip": HOLIDAY_POLICY_SKIP,
        "cancel": HOLIDAY_POLICY_SKIP,
        "exact": HOLIDAY_POLICY_EXACT,
        "exact_only": HOLIDAY_POLICY_EXACT,
    }
    p = aliases.get(p, p)
    if p not in KNOWN_HOLIDAY_POLICIES:
        raise ValueError(
            "holiday_policy must be one of %s, got %r" % (KNOWN_HOLIDAY_POLICIES, value)
        )
    return p


def yyyymmdd_to_date(d: int) -> _date:
    d = int(d)
    return _date(d // 10000, (d // 100) % 100, d % 100)


def date_to_yyyymmdd(d: _date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def first_calendar_date_on_weekday(
    after_date: int, weekday: int, *, strict: bool = True
) -> int:
    """First civil calendar date with ISO weekday (1=Mon..7=Sun).

    Unlike trading-day search, this returns the *planned* weekday even if that
    civil day is a market holiday (so holiday_policy can shift it).
    """
    weekday = int(weekday)
    if weekday < 1 or weekday > 7:
        raise ValueError("weekday must be 1..7, got %s" % weekday)
    d = yyyymmdd_to_date(int(after_date))
    if strict:
        d = d + timedelta(days=1)
    # at most 7 steps to hit target weekday
    for _ in range(8):
        if d.isoweekday() == weekday:
            return date_to_yyyymmdd(d)
        d = d + timedelta(days=1)
    raise RuntimeError("unreachable weekday search")


@dataclass
class TradeCalendar:
    dates: List[int]  # sorted YYYYMMDD

    def __post_init__(self) -> None:
        self.dates = sorted(set(int(d) for d in self.dates))
        self._index = {d: i for i, d in enumerate(self.dates)}

    def __len__(self) -> int:
        return len(self.dates)

    def is_trading_day(self, date: int) -> bool:
        return int(date) in self._index

    def next_trading_day(self, date: int) -> Optional[int]:
        """First trading day strictly after date."""
        date = int(date)
        for d in self.dates:
            if d > date:
                return d
        return None

    def first_trading_day_on_or_after(self, date: int) -> Optional[int]:
        """First trading day with d >= date."""
        date = int(date)
        if date in self._index:
            return date
        for d in self.dates:
            if d >= date:
                return d
        return None

    def nth_trading_day_after(self, date: int, n: int) -> Optional[int]:
        """Nth trading day strictly after date (n >= 1). n=1 equals next_trading_day."""
        n = int(n)
        if n < 1:
            raise ValueError("n must be >= 1, got %s" % n)
        d = int(date)
        out = None
        for _ in range(n):
            out = self.next_trading_day(d)
            if out is None:
                return None
            d = out
        return out

    def next_weekday_trading_day(
        self, after_date: int, weekday: int, *, strict: bool = True
    ) -> Optional[int]:
        """First *trading* day on ISO weekday (1=Mon..7=Sun).

        Legacy helper: skips non-trading sessions and may jump to the next week
        if the target weekday is a holiday. Prefer
        :meth:`resolve_weekday_session` with an explicit holiday_policy.
        """
        after_date = int(after_date)
        weekday = int(weekday)
        if weekday < 1 or weekday > 7:
            raise ValueError("weekday must be 1..7, got %s" % weekday)
        for d in self.dates:
            if strict:
                if d <= after_date:
                    continue
            else:
                if d < after_date:
                    continue
            y, m, day = d // 10000, (d // 100) % 100, d % 100
            if _date(y, m, day).isoweekday() == weekday:
                return d
        return None

    def apply_holiday_policy(
        self, planned: int, policy: str
    ) -> Optional[int]:
        """Map a planned civil/trading date to an actual trading day.

        Returns None when the trade should be cancelled (skip / exact fail).
        """
        planned = int(planned)
        policy = normalize_holiday_policy(policy)
        if self.is_trading_day(planned):
            return planned
        if policy == HOLIDAY_POLICY_NEXT:
            return self.first_trading_day_on_or_after(planned)
        if policy == HOLIDAY_POLICY_PREV:
            return self.prev_trading_day(planned)
        if policy in (HOLIDAY_POLICY_SKIP, HOLIDAY_POLICY_EXACT):
            return None
        raise ValueError("unknown holiday_policy: %r" % policy)

    def resolve_weekday_session(
        self,
        after_date: int,
        weekday: int,
        *,
        strict: bool = True,
        holiday_policy: str = DEFAULT_HOLIDAY_POLICY,
    ) -> Optional[Tuple[int, int, int]]:
        """Resolve weekday anchor to (planned_date, actual_trading_date, shift_days).

        *planned_date* is the civil calendar weekday target (may be a holiday).
        *actual* is the trading day after applying ``holiday_policy``.
        *shift_days* is the count of civil days from planned to actual (0 if same).

        Returns None if cancelled by policy or calendar exhausted.
        """
        policy = normalize_holiday_policy(holiday_policy)
        planned = first_calendar_date_on_weekday(int(after_date), int(weekday), strict=strict)
        if policy == HOLIDAY_POLICY_EXACT:
            if not self.is_trading_day(planned):
                return None
            return planned, planned, 0
        actual = self.apply_holiday_policy(planned, policy)
        if actual is None:
            return None
        # If policy rolled and actual falls on/before after_date when strict, try next week once.
        if strict and actual <= int(after_date):
            # move planned one week forward and re-apply
            planned2 = date_to_yyyymmdd(yyyymmdd_to_date(planned) + timedelta(days=7))
            if policy == HOLIDAY_POLICY_EXACT and not self.is_trading_day(planned2):
                return None
            actual2 = self.apply_holiday_policy(planned2, policy)
            if actual2 is None or actual2 <= int(after_date):
                return None
            planned, actual = planned2, actual2
        shift = (yyyymmdd_to_date(actual) - yyyymmdd_to_date(planned)).days
        return planned, actual, int(shift)

    def prev_trading_day(self, date: int) -> Optional[int]:
        date = int(date)
        prev = None
        for d in self.dates:
            if d >= date:
                break
            prev = d
        return prev

    def session_end(self, date: int) -> bool:
        return self.is_trading_day(date)

    def range(self, start: int, end: int) -> List[int]:
        return [d for d in self.dates if start <= d <= end]

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "source": "sh000001.day index trading days",
                    "count": len(self.dates),
                    "first": self.dates[0] if self.dates else None,
                    "last": self.dates[-1] if self.dates else None,
                    "dates": self.dates,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "TradeCalendar":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(list(data["dates"]))

    @classmethod
    def from_index_day_file(cls, path: Path | str) -> "TradeCalendar":
        bars, _ = parse_day_file(path)
        return cls([b.date for b in bars])

    @classmethod
    def from_tdx(cls, tdx_root: Path | str = r"D:\通达信") -> "TradeCalendar":
        path = Path(tdx_root) / "vipdoc" / "sh" / "lday" / "sh000001.day"
        if not path.exists():
            raise FileNotFoundError(f"index day file not found: {path}")
        return cls.from_index_day_file(path)


# ---- Gate C D6: dataset-derived trading calendar --------------------------
# Repo/dataset mode must NOT depend on the legacy 2016+ calendar.json, on
# Baostock, on D:\通达信 files or on the machine clock. The market calendar
# is derived from the locked execution dataset: the union of trade_date over
# every symbol blob covers the dataset's full range (a day is a trading day
# iff at least one listed stock traded). The result is versioned per
# dataset_id + content sha256 and cached; datasets are immutable, so the
# cached calendar is immutable too.

DATASET_CALENDAR_VERSION = 1


def dataset_calendar_cache_path(cache_dir: Path, dataset_id: str) -> Path:
    return Path(cache_dir) / f"calendar_{dataset_id}.json"


def build_calendar_from_dataset(
    store: "Any",
    dataset_id: str,
    *,
    cache_dir: Optional[Path] = None,
) -> Tuple["TradeCalendar", dict]:
    """Build (or load cached) TradeCalendar from an execution dataset.

    Returns (calendar, meta) where meta carries calendar_source /
    calendar_dataset_id / calendar_sha256 / first / last / count for run
    traceability and cache keying.
    """
    import hashlib as _hashlib

    import numpy as _np

    if cache_dir is not None:
        cpath = dataset_calendar_cache_path(cache_dir, dataset_id)
        if cpath.exists():
            try:
                data = json.loads(cpath.read_text(encoding="utf-8"))
                if (
                    data.get("calendar_version") == DATASET_CALENDAR_VERSION
                    and data.get("dataset_id") == dataset_id
                    and isinstance(data.get("dates"), list)
                    and data.get("dates")
                ):
                    cal = TradeCalendar(list(data["dates"]))
                    meta = {
                        "calendar_source": "execution_dataset",
                        "calendar_dataset_id": dataset_id,
                        "calendar_sha256": data.get("sha256", ""),
                        "calendar_first": cal.dates[0],
                        "calendar_last": cal.dates[-1],
                        "calendar_count": len(cal.dates),
                        "calendar_cache": str(cpath),
                    }
                    return cal, meta
            except Exception:
                pass  # unreadable cache -> rebuild below

    manifest = store.load_manifest(dataset_id)
    if manifest is None:
        raise FileNotFoundError(f"dataset not found for calendar: {dataset_id}")
    all_dates: set = set()
    for rec in manifest.symbols:
        if not rec.blob_sha256:
            continue
        arrays = store.load_bars(rec.blob_sha256)
        td = arrays.get("trade_date")
        if td is None or len(td) == 0:
            continue
        all_dates.update(int(x) for x in _np.unique(td))
    if not all_dates:
        raise ValueError(f"dataset {dataset_id} has no trade dates for calendar")
    dates = sorted(all_dates)
    sha = _hashlib.sha256(
        json.dumps(dates, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    cal = TradeCalendar(dates)
    meta = {
        "calendar_source": "execution_dataset",
        "calendar_dataset_id": dataset_id,
        "calendar_sha256": sha,
        "calendar_first": dates[0],
        "calendar_last": dates[-1],
        "calendar_count": len(dates),
    }
    if cache_dir is not None:
        cpath = dataset_calendar_cache_path(cache_dir, dataset_id)
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cpath.write_text(
            json.dumps(
                {
                    "calendar_version": DATASET_CALENDAR_VERSION,
                    "dataset_id": dataset_id,
                    "sha256": sha,
                    "count": len(dates),
                    "first": dates[0],
                    "last": dates[-1],
                    "dates": dates,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        meta["calendar_cache"] = str(cpath)
    return cal, meta
