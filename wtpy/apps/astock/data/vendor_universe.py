"""Vendor security universe builder with generic code-segment classification.

Builds the full-union security universe from the purchased vendor's yearly
daily-K ZIP archives (``2000.zip`` .. latest, each containing
``{YEAR}/{CODE}.{EXCH}.csv`` members) and classifies every security with
GENERIC code-segment rules plus per-row equity metadata confirmation.

Design constraints (hard rules):
  * No per-symbol special cases: every rule is a code-segment range or a
    metadata predicate.  New segments (e.g. SZSE 302xxx ChiNext additions)
    are covered by the generic 30x range, never by enumerating symbols.
  * No hardcoded universe sizes: every summary number is computed from the
    archives actually present.
  * Streaming only: ZIP members are parsed row-by-row; only per-security
    aggregate dicts are kept in memory.

The ``provider`` passed to :func:`build_vendor_universe` is duck-typed and
only needs two methods (``LocalVendorProvider`` satisfies both):
  * ``available_years() -> List[int]``
  * ``year_zip_path(year) -> Optional[Path]``
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

__all__ = [
    "ROW_FIELDS",
    "STOCK_BOARDS",
    "VendorUniverse",
    "build_vendor_universe",
    "classify_vendor_code",
]

#: Fixed row schema; also the exact column order of :meth:`VendorUniverse.write_csv`.
ROW_FIELDS: Tuple[str, ...] = (
    "symbol",
    "canonical_symbol",
    "exchange",
    "board",
    "instrument_type",
    "first_seen_date",
    "last_seen_date",
    "first_seen_year",
    "last_seen_year",
    "present_in_latest_year",
    "source_archive_count",
    "source_row_count",
    "inclusion_status",
    "inclusion_reason",
    "exclusion_reason",
    "known_delisted",
    "survivorship_risk",
    "metadata_source",
)

#: Stock boards eligible for inclusion (A-shares incl. BSE).
STOCK_BOARDS = frozenset({"sse_main", "star", "szse_main", "chinext", "bse"})

#: Fixed provenance tag for every row.
METADATA_SOURCE = "vendor_csv+code_segment_rules"

#: Reference year used by the fixed ``snapshot_2024_*`` summary keys.  The
#: *counts* are always computed dynamically from archive presence; only the
#: reference year of the snapshot schema is fixed.
SNAPSHOT_REFERENCE_YEAR = 2024

_SUFFIX_TO_EXCHANGE = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}

_VENDOR_CODE_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")


def _parse_vendor_code(vendor_code: str) -> Optional[Tuple[str, str]]:
    """Parse ``600000.SH`` -> ``("600000", "SH")``; ``None`` if not vendor form."""
    if not isinstance(vendor_code, str):
        return None
    m = _VENDOR_CODE_RE.match(vendor_code.strip().upper())
    if not m:
        return None
    return m.group(1), m.group(2)


def _classify_segment(num: str, exchange: str) -> Tuple[str, str]:
    """Classify a 6-digit code under ONE exchange's generic segment rules.

    Returns ``(instrument_type, board)``; ``board`` is only meaningful for
    ``instrument_type == "stock"``.  All ranges are generic segments — never
    single-symbol special cases.
    """
    p1, p2, p3 = num[:1], num[:2], num[:3]
    if exchange == "SSE":
        if "600" <= p3 <= "605":
            return "stock", "sse_main"
        if p3 in ("688", "689"):
            return "stock", "star"
        if p3 == "900":
            return "b_share", ""
        if p1 == "5":
            return "fund_etf", ""
        if p3 == "000":
            return "index", ""
        if p1 in ("1", "2"):
            return "bond", ""
        return "unidentified", ""
    if exchange == "SZSE":
        if "000" <= p3 <= "004":
            return "stock", "szse_main"
        if p2 == "30":
            # Generic ChiNext segment 300000-309999.  Newly opened segments
            # (302xxx, 305xxx, ...) are covered by this range rule.
            return "stock", "chinext"
        if p3 == "200":
            return "b_share", ""
        if p2 in ("15", "16", "18"):
            return "fund_etf", ""
        if p3 == "399":
            return "index", ""
        if p1 == "1":
            # 1-prefixed and not 15/16/18 fund segments -> bond segment.
            return "bond", ""
        return "unidentified", ""
    if exchange == "BSE":
        if p1 in ("4", "8") or p2 == "92":
            return "stock", "bse"
        return "unidentified", ""
    return "unidentified", ""


def classify_vendor_code(vendor_code: str) -> dict:
    """Classify a vendor code (``600000.SH`` style) by generic segment rules.

    Returns ``{"exchange": str, "board": str, "instrument_type": str}`` where
      * ``exchange``: ``SSE | SZSE | BSE | UNKNOWN``
      * ``instrument_type``: ``stock | b_share | fund_etf | index | bond |
        other | unidentified``  (``other`` == exchange/code-segment mismatch,
        e.g. ``600000.SZ``)
      * ``board`` (meaningful only for stocks): ``sse_main | star |
        szse_main | chinext | bse | ""``

    Anything not of the form ``6 digits + .SH/.SZ/.BJ`` yields
    ``exchange="UNKNOWN"`` / ``instrument_type="unidentified"``.
    """
    parsed = _parse_vendor_code(vendor_code)
    if parsed is None:
        return {"exchange": "UNKNOWN", "board": "", "instrument_type": "unidentified"}
    num, suffix = parsed
    exchange = _SUFFIX_TO_EXCHANGE[suffix]
    itype, board = _classify_segment(num, exchange)
    if itype == "unidentified":
        # Segment unknown on the declared exchange: if the segment clearly
        # belongs to ANOTHER exchange, this is an exchange/code mismatch.
        for other_exchange in _SUFFIX_TO_EXCHANGE.values():
            if other_exchange == exchange:
                continue
            other_itype, _ = _classify_segment(num, other_exchange)
            if other_itype != "unidentified":
                return {"exchange": exchange, "board": "", "instrument_type": "other"}
    return {
        "exchange": exchange,
        "board": board if itype == "stock" else "",
        "instrument_type": itype,
    }


class _Agg:
    """Per-security streaming aggregate (the only state kept in memory)."""

    __slots__ = ("years", "row_count", "min_date", "max_date", "equity_ok")

    def __init__(self) -> None:
        self.years: set = set()
        self.row_count: int = 0
        self.min_date: Optional[str] = None
        self.max_date: Optional[str] = None
        self.equity_ok: bool = False


def _row_equity_ok(row: dict) -> bool:
    """True when total_share or float_share parses to a float > 0."""
    for field in ("total_share", "float_share"):
        val = row.get(field)
        if val is None:
            continue
        try:
            if float(val) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _iter_csv_members(zf: zipfile.ZipFile):
    """Yield ``(member_name, agg_key)`` for every real CSV member."""
    for name in zf.namelist():
        if "__MACOSX" in name:
            continue
        if not name.lower().endswith(".csv"):
            continue
        fname = name.rsplit("/", 1)[-1]
        stem = fname[:-4]  # strip ".csv"
        parsed = _parse_vendor_code(stem)
        key = f"{parsed[0]}.{parsed[1]}" if parsed else stem
        yield name, key


def _scan_zip_namelist(zf: zipfile.ZipFile, year: int, aggs: Dict[str, _Agg]) -> None:
    """Fast mode: member presence only, no content is read."""
    for _name, key in _iter_csv_members(zf):
        agg = aggs.get(key)
        if agg is None:
            agg = aggs[key] = _Agg()
        agg.years.add(year)


def _scan_zip_with_metadata(zf: zipfile.ZipFile, year: int, aggs: Dict[str, _Agg]) -> None:
    """Streaming scan of one year ZIP: per-member row counts, date range and
    equity metadata check (first valid row of each member)."""
    for name, key in _iter_csv_members(zf):
        agg = aggs.get(key)
        if agg is None:
            agg = aggs[key] = _Agg()
        agg.years.add(year)
        with zf.open(name, "r") as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
            reader = csv.DictReader(text)
            first_valid_seen = False
            for row in reader:
                date_str = (row.get("datetime") or "").strip()
                if not date_str:
                    continue
                agg.row_count += 1
                if agg.min_date is None or date_str < agg.min_date:
                    agg.min_date = date_str
                if agg.max_date is None or date_str > agg.max_date:
                    agg.max_date = date_str
                if not first_valid_seen:
                    first_valid_seen = True
                    if not agg.equity_ok and _row_equity_ok(row):
                        agg.equity_ok = True


def _build_rows(
    aggs: Dict[str, _Agg],
    latest_year: Optional[int],
    with_metadata: bool,
) -> List[dict]:
    rows: List[dict] = []
    for key in sorted(aggs.keys()):
        agg = aggs[key]
        cls = classify_vendor_code(key)
        exchange = cls["exchange"]
        board = cls["board"]
        itype = cls["instrument_type"]
        parsed = _parse_vendor_code(key)
        # Canonical symbols use the STK product namespace; only stock rows
        # get one so non-stock instruments can never leak a fake STK symbol.
        canonical = (
            f"{exchange}.STK.{parsed[0]}" if (parsed and itype == "stock") else ""
        )
        years_sorted = sorted(agg.years)
        present_latest = latest_year is not None and latest_year in agg.years

        if itype == "stock" and board in STOCK_BOARDS:
            if with_metadata and not agg.equity_ok:
                # Segment looks like a stock but no archive ever carried
                # positive equity metadata -> refuse inclusion (generic rule).
                status, inc_reason, exc_reason = "excluded", "", "equity_metadata_missing"
            else:
                status = "included"
                inc_reason = (
                    "stock_segment+equity_metadata_confirmed"
                    if with_metadata
                    else "stock_segment_rules_only"
                )
                exc_reason = ""
        elif itype == "other":
            status, inc_reason, exc_reason = "excluded", "", "exchange_code_mismatch"
        elif itype == "unidentified":
            status, inc_reason, exc_reason = "excluded", "", "unidentified_segment"
        else:  # b_share / fund_etf / index / bond
            status, inc_reason, exc_reason = "excluded", "", "non_stock_instrument"

        # The vendor archives carry no delist metadata; absence from the
        # latest year archive is only a *suspicion*, never a confirmation.
        known_delisted = "unknown" if present_latest else "suspected_absent_latest_year"
        survivorship_risk = "baseline" if present_latest else "high"

        rows.append(
            {
                "symbol": key,
                "canonical_symbol": canonical,
                "exchange": exchange,
                "board": board,
                "instrument_type": itype,
                "first_seen_date": agg.min_date or "",
                "last_seen_date": agg.max_date or "",
                "first_seen_year": years_sorted[0] if years_sorted else "",
                "last_seen_year": years_sorted[-1] if years_sorted else "",
                "present_in_latest_year": present_latest,
                "source_archive_count": len(agg.years),
                # Sum over ALL year archives, duplicates NOT deduplicated
                # (documented aggregation convention).
                "source_row_count": agg.row_count,
                "inclusion_status": status,
                "inclusion_reason": inc_reason,
                "exclusion_reason": exc_reason,
                "known_delisted": known_delisted,
                "survivorship_risk": survivorship_risk,
                "metadata_source": METADATA_SOURCE,
            }
        )
    return rows


def _build_summary(
    rows: List[dict],
    aggs: Dict[str, _Agg],
    latest_year: Optional[int],
) -> dict:
    """All values computed dynamically from the scanned archives.

    Conventions:
      * ``snapshot_2024_*`` / ``historical_*`` / ``snapshot_latest_total``
        count INCLUDED stocks only (a_share = SSE+SZSE, bse = BSE).
      * ``non_a_count``: securities classified as a non-A instrument
        (b_share/fund_etf/index/bond/other); unidentified counted separately.
      * ``absent_from_latest_count``: ALL securities absent from the latest
        year archive; ``suspected_delisted_count``: INCLUDED stocks absent
        from the latest year archive (the survivorship-relevant number).
    """
    included_rows = [r for r in rows if r["inclusion_status"] == "included"]

    def _in_snapshot_year(row: dict) -> bool:
        return SNAPSHOT_REFERENCE_YEAR in aggs[row["symbol"]].years

    return {
        "total_union": len(rows),
        "snapshot_2024_a_share": sum(
            1
            for r in included_rows
            if r["exchange"] in ("SSE", "SZSE") and _in_snapshot_year(r)
        ),
        "snapshot_2024_bse": sum(
            1 for r in included_rows if r["exchange"] == "BSE" and _in_snapshot_year(r)
        ),
        "snapshot_latest_total": sum(
            1 for r in included_rows if r["present_in_latest_year"]
        ),
        "historical_a_share": sum(
            1 for r in included_rows if r["exchange"] in ("SSE", "SZSE")
        ),
        "historical_bse": sum(1 for r in included_rows if r["exchange"] == "BSE"),
        "non_a_count": sum(
            1
            for r in rows
            if r["instrument_type"] in ("b_share", "fund_etf", "index", "bond", "other")
        ),
        "excluded_count": len(rows) - len(included_rows),
        "included_count": len(included_rows),
        "absent_from_latest_count": sum(
            1 for r in rows if not r["present_in_latest_year"]
        ),
        "suspected_delisted_count": sum(
            1
            for r in included_rows
            if r["known_delisted"] == "suspected_absent_latest_year"
        ),
        "unidentified_count": sum(
            1 for r in rows if r["instrument_type"] == "unidentified"
        ),
        "latest_year": latest_year,
    }


class VendorUniverse:
    """Result of :func:`build_vendor_universe`.

    Attributes:
      rows: one dict per security, keys/order exactly :data:`ROW_FIELDS`.
      included_symbols: sorted canonical symbols of ``inclusion_status ==
        "included"`` rows.
      universe_hash: ``sha256(",".join(sorted(included_symbols)))``.
      summary: dynamic statistics (see :func:`_build_summary`).
    """

    def __init__(
        self,
        rows: List[dict],
        included_symbols: List[str],
        universe_hash: str,
        summary: dict,
    ) -> None:
        self.rows = rows
        self.included_symbols = included_symbols
        self.universe_hash = universe_hash
        self.summary = summary

    def write_csv(self, path) -> None:
        """Write all rows as utf-8-sig CSV, columns exactly :data:`ROW_FIELDS`."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(ROW_FIELDS))
            writer.writeheader()
            for row in self.rows:
                writer.writerow(row)


def build_vendor_universe(
    provider,
    *,
    with_metadata: bool = True,
    progress: Optional[Callable[[int, int, int], None]] = None,
) -> "VendorUniverse":
    """Build the vendor security universe from all year ZIP archives.

    Args:
      provider: duck-typed archive provider exposing ``available_years()``
        and ``year_zip_path(year)`` (e.g. ``LocalVendorProvider``).
      with_metadata: when True (default) each ZIP member is streamed with
        ``csv.DictReader`` to aggregate row counts, first/last trade dates
        and the equity-metadata confirmation; inclusion then additionally
        requires equity confirmation.  When False only ``namelist()`` is
        used (fast preview): ``first/last_seen_date`` are empty,
        ``source_row_count`` is 0 and inclusion degrades to pure
        code-segment rules.
      progress: optional callable ``progress(year, done, total)`` invoked
        after each year archive is scanned.
    """
    years = sorted(int(y) for y in provider.available_years())
    latest_year: Optional[int] = years[-1] if years else None
    aggs: Dict[str, _Agg] = {}

    for idx, year in enumerate(years):
        zpath = provider.year_zip_path(year)
        if zpath is None:
            continue
        with zipfile.ZipFile(zpath, "r") as zf:
            if with_metadata:
                _scan_zip_with_metadata(zf, year, aggs)
            else:
                _scan_zip_namelist(zf, year, aggs)
        if progress is not None:
            progress(year, idx + 1, len(years))

    rows = _build_rows(aggs, latest_year, with_metadata)
    included_symbols = sorted(
        r["canonical_symbol"] for r in rows if r["inclusion_status"] == "included"
    )
    universe_hash = hashlib.sha256(
        ",".join(sorted(included_symbols)).encode("utf-8")
    ).hexdigest()
    summary = _build_summary(rows, aggs, latest_year)
    return VendorUniverse(rows, included_symbols, universe_hash, summary)
