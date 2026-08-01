"""Point-in-time universe — Gate B4.

A versioned, immutable, auditable answer to "which stocks existed and were
tradable on trade_date X?". Built from the B1 reference universe (Tushare
stock_basic L/D merged) with bar-derived last_trade_date from the B2/B3
datasets. Stored as JSON under <market_data_root>/universes/<id>.json.

Membership rule (UNIVERSE_RULE_VERSION in historical_universe):
  list_date <= trade_date AND
    trade_date <= last_trade_date   (when bar-derived last trade is known)
    trade_date <  delist_date       (fallback when only delist_date known)
  unknown symbols and symbols without list_date are NEVER members
  (fail closed — a stock we cannot date must not appear in history).

BSE 2025 code migration: pre-migration codes (43/83/87 segments) are aliases
of the post-migration 920 identity; both resolve to one instrument window.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .historical_universe import (
    INSTRUMENT_IDENTITY_RULE_VERSION,
    UNIVERSE_RULE_VERSION,
    is_member_point_in_time,
)
from .io_util import atomic_write_json
from .repository import MarketDataRepository

PIT_UNIVERSE_SCHEMA_VERSION = 1

# membership_reason codes (audit contract)
REASON_MEMBER = "member"
REASON_NOT_LISTED_YET = "not_listed_yet"
REASON_AFTER_LAST_TRADE = "after_last_trade_date"
REASON_DELISTED = "delisted"
REASON_UNKNOWN_SYMBOL = "unknown_symbol"
REASON_NO_LIST_DATE = "no_list_date_fail_closed"


@dataclass
class InstrumentWindow:
    canonical_symbol: str
    ts_code: str
    exchange: str
    board: str
    name: str
    list_status: str
    list_date: Optional[int]
    delist_date: Optional[int]
    last_trade_date: Optional[int]
    last_trade_date_source: str = ""
    aliases: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class UniverseImmutabilityError(RuntimeError):
    """Attempted to overwrite an existing universe file with different content."""


class PointInTimeUniverse:
    def __init__(
        self,
        entries: Dict[str, InstrumentWindow],
        *,
        universe_dataset_id: str,
        content_sha256: str,
        built_from: Optional[dict] = None,
        created_at: str = "",
    ):
        self.entries = entries
        self.universe_dataset_id = universe_dataset_id
        self.content_sha256 = content_sha256
        self.universe_rule_version = UNIVERSE_RULE_VERSION
        self.identity_rule_version = INSTRUMENT_IDENTITY_RULE_VERSION
        self.built_from = built_from or {}
        self.created_at = created_at
        self._alias_index: Dict[str, str] = {}
        for canon, w in entries.items():
            for alias in w.aliases:
                self._alias_index[alias] = canon

    # ---------- construction ----------

    @staticmethod
    def _content_sha(entries: Dict[str, InstrumentWindow]) -> str:
        canonical = json.dumps(
            {
                "schema": PIT_UNIVERSE_SCHEMA_VERSION,
                "rule": UNIVERSE_RULE_VERSION,
                "identity_rule": INSTRUMENT_IDENTITY_RULE_VERSION,
                "entries": [entries[k].to_dict() for k in sorted(entries)],
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def build(
        cls,
        windows: Iterable[InstrumentWindow],
        *,
        cutoff: int,
        built_from: Optional[dict] = None,
    ) -> "PointInTimeUniverse":
        entries = {w.canonical_symbol: w for w in windows}
        sha = cls._content_sha(entries)
        uid = f"pit_universe_1d_{cutoff}_{sha[:12]}"
        return cls(
            entries,
            universe_dataset_id=uid,
            content_sha256=sha,
            built_from=built_from,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )

    # ---------- persistence ----------

    @staticmethod
    def universe_dir(market_data_root: Path | str) -> Path:
        return Path(market_data_root) / "universes"

    def save(self, market_data_root: Path | str) -> Path:
        d = self.universe_dir(market_data_root)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{self.universe_dataset_id}.json"
        payload = self.to_dict()
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("content_sha256") == self.content_sha256:
                return path  # idempotent no-op
            raise UniverseImmutabilityError(
                f"universe file exists with different content: {path}"
            )
        atomic_write_json(path, payload)
        return path

    def to_dict(self) -> dict:
        return {
            "schema_version": PIT_UNIVERSE_SCHEMA_VERSION,
            "universe_dataset_id": self.universe_dataset_id,
            "universe_rule_version": self.universe_rule_version,
            "instrument_identity_rule_version": self.identity_rule_version,
            "content_sha256": self.content_sha256,
            "created_at": self.created_at,
            "built_from": self.built_from,
            "entry_count": len(self.entries),
            "entries": [self.entries[k].to_dict() for k in sorted(self.entries)],
        }

    @classmethod
    def load(cls, path: Path | str) -> "PointInTimeUniverse":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = {
            e["canonical_symbol"]: InstrumentWindow(**e) for e in data["entries"]
        }
        sha = cls._content_sha(entries)
        if sha != data.get("content_sha256"):
            raise ValueError(
                f"universe content hash mismatch (corrupt file?): {path}"
            )
        return cls(
            entries,
            universe_dataset_id=data["universe_dataset_id"],
            content_sha256=sha,
            built_from=data.get("built_from"),
            created_at=data.get("created_at", ""),
        )

    @classmethod
    def from_root(
        cls, market_data_root: Path | str, universe_dataset_id: str
    ) -> "PointInTimeUniverse":
        path = cls.universe_dir(market_data_root) / f"{universe_dataset_id}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"universe dataset not found: {universe_dataset_id}"
            )
        return cls.load(path)

    @staticmethod
    def list_universes(market_data_root: Path | str) -> List[str]:
        d = PointInTimeUniverse.universe_dir(market_data_root)
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.json"))

    # ---------- queries ----------

    def resolve(self, symbol: str) -> Optional[InstrumentWindow]:
        """Resolve canonical / alias / format-variant symbol to its window."""
        for variant in MarketDataRepository._symbol_variants(symbol):
            if variant in self.entries:
                return self.entries[variant]
            if variant in self._alias_index:
                return self.entries[self._alias_index[variant]]
        return None

    def membership_reason(self, symbol: str, trade_date: int) -> Tuple[bool, str]:
        w = self.resolve(symbol)
        if w is None:
            return False, REASON_UNKNOWN_SYMBOL
        if not w.list_date:
            return False, REASON_NO_LIST_DATE
        if trade_date < w.list_date:
            return False, REASON_NOT_LISTED_YET
        member = is_member_point_in_time(
            trade_date, w.list_date, w.delist_date, w.last_trade_date
        )
        if member:
            return True, REASON_MEMBER
        if w.last_trade_date is not None and trade_date > w.last_trade_date:
            return False, REASON_AFTER_LAST_TRADE
        return False, REASON_DELISTED

    def is_member(self, symbol: str, trade_date: int) -> bool:
        return self.membership_reason(symbol, trade_date)[0]

    def filter_active(
        self, symbols: Sequence[str], trade_date: int
    ) -> List[str]:
        return [s for s in symbols if self.is_member(s, trade_date)]

    def members_on(self, trade_date: int) -> List[str]:
        return [
            canon
            for canon, w in self.entries.items()
            if is_member_point_in_time(
                trade_date, w.list_date, w.delist_date, w.last_trade_date
            )
        ]
