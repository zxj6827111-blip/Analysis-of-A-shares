# -*- coding: utf-8 -*-
"""Research experiment parameter-space models (JSON-friendly dataclasses)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union


# ISO weekday 1=Mon .. 7=Sun. None (or empty list in some APIs) = all days.
WeekdayOption = Optional[List[int]]


@dataclass
class AxisSpec:
    """Named axis of discrete options for cartesian expansion."""

    name: str
    options: List[Any] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ParameterSpace:
    """Independent research axes for experiment grid expansion.

    Each list field is an *axis* of options. Expansion takes the cartesian
    product across axes that have at least one option.

    Buy/sell modes are option dicts:
      - T+N path: ``{"entry_lag": int, "buy_on": "open"|"close"}``
      - weekday path: ``{"buy_weekday": int, "buy_on": "open"|"close"}``
      - sell T+N: ``{"hold": int, "sell_on": "open"|"close"}``
      - sell weekday: ``{"exit_weekday": int, "sell_on": "open"|"close"}``

    Gua options are either preset keys (str) or full gua_filter dicts.
    """

    rule_ids: List[str] = field(default_factory=list)
    period: str = "DAY"
    # Each option: list of ISO weekdays, or None = all trading weekdays
    signal_weekdays: List[WeekdayOption] = field(default_factory=lambda: [None])
    buy_modes: List[Dict[str, Any]] = field(default_factory=list)
    sell_modes: List[Dict[str, Any]] = field(default_factory=list)
    # Preset keys ("none", "best3", ...) and/or inline gua_filter payloads
    gua_keys: List[str] = field(default_factory=lambda: ["none"])
    gua_filters: List[Optional[Dict[str, Any]]] = field(default_factory=list)
    stop_loss_list: List[Optional[float]] = field(default_factory=lambda: [None])
    take_profit_list: List[Optional[float]] = field(default_factory=lambda: [None])
    holiday_policy: str = "next_trading_day"
    codes: Optional[List[str]] = None
    start: Optional[int] = None
    end: Optional[int] = None
    account_mode: str = "portfolio"
    research_unadjusted: bool = False
    # Free-form extra axes: name -> list of option values
    extra_axes: Dict[str, List[Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ParameterSpace":
        if not data:
            return cls()
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)

    def axis_sizes(self) -> Dict[str, int]:
        """Cardinality of each expandable axis (minimum 1 for empty product safety)."""
        sizes = {
            "rule_ids": max(1, len(self.rule_ids or [])),
            "signal_weekdays": max(1, len(self.signal_weekdays or [None])),
            "buy_modes": max(1, len(self.buy_modes or [])),
            "sell_modes": max(1, len(self.sell_modes or [])),
            "gua": max(1, len(self.gua_keys or []) + len(self.gua_filters or [])),
            "stop_loss": max(1, len(self.stop_loss_list if self.stop_loss_list is not None else [None])),
            "take_profit": max(
                1, len(self.take_profit_list if self.take_profit_list is not None else [None])
            ),
        }
        for name, opts in (self.extra_axes or {}).items():
            sizes[f"extra:{name}"] = max(1, len(opts or []))
        return sizes

    def theoretical_count(self) -> int:
        n = 1
        for v in self.axis_sizes().values():
            n *= v
        return n


def as_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses / nested structures for JSON."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return {k: as_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [as_jsonable(x) for x in obj]
    return obj
