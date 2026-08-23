"""Historical reference universe — identity normalization and merge rules.

Gate B1 builds an authoritative 2000-2026 A-share reference universe from
Tushare stock_basic (L/D/P), local_vendor bars, Tushare adj_factor and
TdxQuant front datasets. This module holds the pure (offline) logic:
ts_code <-> canonical conversion, instrument classification, list-status
merge and point-in-time membership. Network fetching lives in audit/sync
scripts, never here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

INSTRUMENT_IDENTITY_RULE_VERSION = "identity_rule_v1"
UNIVERSE_RULE_VERSION = "pit_universe_rule_v1"

# instrument_type values
A_SHARE = "a_share"
B_SHARE = "b_share"
ETF = "etf"
LOF = "lof"
FUND_OTHER = "fund_other"
INDEX = "index"
BOND = "bond"
CONVERTIBLE_BOND = "convertible_bond"
OTHER = "other"

_TS_SUFFIX_TO_EXCH = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}
_EXCH_TO_TS_SUFFIX = {v: k for k, v in _TS_SUFFIX_TO_EXCH.items()}


def ts_code_to_canonical(ts_code: str) -> str:
    """600000.SH -> SSE.STK.600000 (raises on malformed input)."""
    s = str(ts_code).strip()
    m = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", s, re.IGNORECASE)
    if not m:
        raise ValueError(f"bad ts_code: {ts_code!r}")
    code, suffix = m.group(1), m.group(2).upper()
    return f"{_TS_SUFFIX_TO_EXCH[suffix]}.STK.{code}"


def canonical_to_ts_code(symbol: str) -> str:
    """SSE.STK.600000 -> 600000.SH (raises on malformed input)."""
    s = str(symbol).strip()
    m = re.fullmatch(r"(SSE|SZSE|BSE)\.[A-Z]+\.(\d{6})", s, re.IGNORECASE)
    if not m:
        raise ValueError(f"bad canonical symbol: {symbol!r}")
    exch, code = m.group(1).upper(), m.group(2)
    return f"{code}.{_EXCH_TO_TS_SUFFIX[exch]}"


def classify_instrument(code: str, exchange: str) -> str:
    """Classify a 6-digit code on a given exchange (SSE/SZSE/BSE).

    Rule version: INSTRUMENT_IDENTITY_RULE_VERSION. Non-stock instruments are
    kept (with exclusion reasons) — never silently dropped by callers.
    """
    if not re.fullmatch(r"\d{6}", str(code)):
        return OTHER
    exchange = exchange.upper()
    if exchange == "SSE":
        if code.startswith(("600", "601", "603", "605", "688", "689")):
            return A_SHARE
        if code.startswith("900"):
            return B_SHARE
        # 沪市 ETF：51/52/56/58 传统段 + 530/551 新段（服务器实测已有
        # 真实行情，如 530060/530300/551060/551900）；550 保持非 ETF。
        if code.startswith(("51", "52", "530", "551", "56", "58")):
            return ETF
        if code.startswith(("501", "502", "506")):
            return LOF
        if code.startswith(("500", "505", "550")):
            return FUND_OTHER
        if code.startswith("000"):
            return INDEX
        if code.startswith(("110", "111", "113", "118")):
            return CONVERTIBLE_BOND
        if code.startswith(("01", "02", "10", "12", "13", "20")):
            return BOND
        return OTHER
    if exchange == "SZSE":
        if code.startswith(("000", "001", "002", "003", "004")) or code[:2] == "30":
            return A_SHARE
        if code.startswith("200"):
            return B_SHARE
        # 深市 ETF：159（传统段）+ 158（新段）。16xxxx=LOF、其余 15/18=
        # 其他场内基金，不得标为 ETF。
        if code.startswith(("158", "159")):
            return ETF
        if code.startswith(("16", "15")):
            return LOF if code.startswith("16") else FUND_OTHER
        if code.startswith("18"):
            return FUND_OTHER
        if code.startswith("399"):
            return INDEX
        if code.startswith(("123", "127", "128")):
            return CONVERTIBLE_BOND
        if code.startswith(("10", "11", "12", "13")):
            return BOND
        return OTHER
    if exchange == "BSE":
        if code.startswith(("43", "83", "87", "920")):
            return A_SHARE
        return OTHER
    return OTHER


def board_of(code: str, exchange: str) -> str:
    """Board segmentation used for reporting (not a trading rule)."""
    exchange = exchange.upper()
    if exchange == "SSE":
        if code.startswith(("688", "689")):
            return "star"
        if code.startswith(("600", "601", "603", "605")):
            return "sse_main"
    elif exchange == "SZSE":
        if code[:2] == "30":
            return "chinext"
        if code.startswith(("002", "003", "004")):
            return "szse_sme_legacy"
        if code.startswith(("000", "001")):
            return "szse_main"
    elif exchange == "BSE":
        return "bse"
    return "other"


@dataclass
class ListStatusRecord:
    """One stock_basic row (already normalized)."""

    ts_code: str
    name: str
    list_status: str  # L | D | P
    list_date: Optional[int]
    delist_date: Optional[int]
    market: str = ""
    exchange: str = ""


@dataclass
class MergedIdentity:
    ts_code: str
    canonical_symbol: str
    exchange: str
    name: str
    list_status: str
    list_date: Optional[int]
    delist_date: Optional[int]
    market: str = ""
    status_sources: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)


# Merge priority when the same ts_code appears in multiple stock_basic
# list_status pulls: a D record is terminal truth, P overrides L.
_STATUS_PRIORITY = {"D": 3, "P": 2, "L": 1}


def merge_list_status(records: Iterable[ListStatusRecord]) -> Dict[str, MergedIdentity]:
    """Merge L/D/P stock_basic pulls into one identity per ts_code.

    Conflicts (same ts_code in several statuses, or diverging list_date /
    name across pulls) are recorded on the merged row, never dropped.
    """
    merged: Dict[str, MergedIdentity] = {}
    for rec in records:
        exch = _TS_SUFFIX_TO_EXCH.get(rec.ts_code.split(".")[-1].upper(), "")
        if rec.ts_code not in merged:
            merged[rec.ts_code] = MergedIdentity(
                ts_code=rec.ts_code,
                canonical_symbol=ts_code_to_canonical(rec.ts_code),
                exchange=exch,
                name=rec.name,
                list_status=rec.list_status,
                list_date=rec.list_date,
                delist_date=rec.delist_date,
                market=rec.market,
                status_sources=[rec.list_status],
            )
            continue
        m = merged[rec.ts_code]
        m.status_sources.append(rec.list_status)
        m.conflicts.append(
            f"duplicate_status:{'+'.join(sorted(set(m.status_sources)))}"
        )
        if _STATUS_PRIORITY[rec.list_status] > _STATUS_PRIORITY[m.list_status]:
            m.list_status = rec.list_status
        if rec.list_date and m.list_date and rec.list_date != m.list_date:
            m.conflicts.append(f"list_date_mismatch:{m.list_date}!={rec.list_date}")
        if rec.list_date and not m.list_date:
            m.list_date = rec.list_date
        if rec.delist_date:
            if m.delist_date and rec.delist_date != m.delist_date:
                m.conflicts.append(
                    f"delist_date_mismatch:{m.delist_date}!={rec.delist_date}"
                )
            m.delist_date = m.delist_date or rec.delist_date
        if rec.name and rec.name != m.name:
            m.conflicts.append(f"name_mismatch:{m.name}!={rec.name}")
    return merged


def is_member_point_in_time(
    trade_date: int,
    list_date: Optional[int],
    delist_date: Optional[int],
    last_trade_date: Optional[int] = None,
) -> bool:
    """Point-in-time membership (UNIVERSE_RULE_VERSION).

    Rule: list_date <= trade_date AND trade_date <= effective_end where
    effective_end = last_trade_date if known, else (delist_date - 1 day is
    approximated as trade_date < delist_date), else open-ended.

    A stock with unknown list_date is never a member (fail closed: a stock
    we cannot date must not appear in history).
    """
    if not list_date or trade_date < list_date:
        return False
    if last_trade_date is not None:
        return trade_date <= last_trade_date
    if delist_date is not None:
        return trade_date < delist_date
    return True


def annual_membership_counts(
    identities: Iterable[MergedIdentity],
    years: Iterable[int],
    *,
    instrument_filter: Optional[str] = A_SHARE,
) -> List[Dict]:
    """Year-end membership counts per year (reporting aid, rule-versioned)."""
    rows = []
    idents = list(identities)
    for year in years:
        year_end = year * 10000 + 1231
        count = 0
        by_exch: Dict[str, int] = {}
        for m in idents:
            code = m.ts_code.split(".")[0]
            if instrument_filter and classify_instrument(code, m.exchange) != instrument_filter:
                continue
            if is_member_point_in_time(year_end, m.list_date, m.delist_date):
                count += 1
                by_exch[m.exchange] = by_exch.get(m.exchange, 0) + 1
        rows.append(
            {
                "year": year,
                "as_of": year_end,
                "member_count": count,
                "sse": by_exch.get("SSE", 0),
                "szse": by_exch.get("SZSE", 0),
                "bse": by_exch.get("BSE", 0),
                "universe_rule_version": UNIVERSE_RULE_VERSION,
            }
        )
    return rows
