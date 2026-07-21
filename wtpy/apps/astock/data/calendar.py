"""Trading calendar derived from index daily bars (not expired holiday files)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import List, Optional, Sequence

from .tdx_reader import parse_day_file


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
        # binary-ish via linear since list is not huge
        for d in self.dates:
            if d > date:
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
        """First trading day on ISO weekday (1=Mon..7=Sun).

        strict=True: strictly after after_date (default for buy/exit after signal/entry).
        strict=False: on or after after_date.
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
