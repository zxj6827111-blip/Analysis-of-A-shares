"""LocalVendorProvider – reads purchased daily K-line data from ZIP archives.

Data format (confirmed by sampling):
  - ZIP per year: 2000.zip ... 2026.zip
  - Inside: {YEAR}/{CODE}.{EXCHANGE}.csv (e.g. 2024/000001.SZ.csv)
  - Encoding: utf-8-sig
  - Columns: code,datetime,open,high,low,close,pre_close,change,pct_chg,
             volume,amount,turnover,turnover_free,volume_ratio,pe,pe_ttm,
             pb,ps,ps_ttm,dv_yield,dv_ttm,total_share,float_share,
             free_share,total_mv,circ_mv
  - Date format: YYYY-MM-DD
  - Code format: 600000.SH / 000001.SZ / 688699.SH
  - Volume unit: 手 (lots of 100 shares) -> standardized to 股 (shares), x100
  - Amount unit: 千元 (1000 CNY) -> standardized to 元 (CNY), x1000
    (empirically verified 2026-07-26: VWAP=amount*1000/(volume*100) falls inside
    the daily low/high range for 400/400 sampled rows; the 万元 unit hypothesis
    matched 0 rows. Do NOT change back to 万元.)
  - Data is UNADJUSTED (raw) — verified against tdx_local prices

This provider is ONLY used during sync. Never called during backtest.
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Set

from .base import (
    AdjustmentMode,
    BarPeriod,
    DataNotDownloaded,
    DataSource,
    InvalidSymbol,
    MarketBar,
    MarketDataRequest,
    ProviderCapabilities,
    ProviderUnavailable,
    UniverseEntry,
)


def _code_to_canonical(code: str) -> str:
    """Convert vendor code (600000.SH) to canonical (SSE.STK.600000)."""
    parts = code.split(".")
    if len(parts) == 2:
        num, suffix = parts
        exch = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix.upper())
        if exch:
            return f"{exch}.STK.{num}"
    raise InvalidSymbol(f"Cannot convert vendor code to canonical: {code}")


def _canonical_to_vendor_code(symbol: str) -> str:
    """Convert canonical (SSE.STK.600000) to vendor code (600000.SH)."""
    parts = symbol.split(".")
    if len(parts) == 3:
        exch, _, code = parts
        suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exch)
        if suffix:
            return f"{code}.{suffix}"
    if len(parts) == 2:
        code, suffix = parts
        if suffix.upper() in ("SH", "SZ", "BJ"):
            return f"{code}.{suffix.upper()}"
    raise InvalidSymbol(f"Cannot convert to vendor code: {symbol}")


def _validate_symbol_exchange(symbol: str) -> None:
    """Reject symbols where code doesn't match exchange."""
    parts = symbol.split(".")
    if len(parts) != 3:
        raise InvalidSymbol(f"Not canonical format: {symbol}")
    exch, _, code = parts
    if not code.isdigit() or len(code) != 6:
        raise InvalidSymbol(f"Invalid code: {symbol}")
    if exch == "SSE" and not code.startswith(("5", "6", "9")):
        raise InvalidSymbol(
            f"Exchange mismatch: {code} is not Shanghai (SSE). "
            f"Shanghai codes start with 5/6/9."
        )
    if exch == "SZSE" and code.startswith(("5", "6", "9")):
        raise InvalidSymbol(
            f"Exchange mismatch: {code} is not Shenzhen (SZSE). "
            f"Shenzhen codes start with 0/1/2/3."
        )
    if exch == "BSE" and not code.startswith(("4", "8", "92")):
        raise InvalidSymbol(
            f"Exchange mismatch: {code} is not Beijing (BSE). "
            f"Beijing codes start with 4/8/92."
        )


class LocalVendorProvider:
    """Reads unadjusted daily bars from purchased vendor ZIP archives."""

    def __init__(self, incoming_root: Path | str):
        self._root = Path(incoming_root)
        self._daily_dir: Optional[Path] = None
        self._year_zips: Dict[int, Path] = {}
        self._scan_daily_dir()

    def _scan_daily_dir(self) -> None:
        """Find the daily K-line directory and index year ZIPs."""
        for d in self._root.rglob("*"):
            if d.is_dir():
                zips = list(d.glob("2*.zip"))
                year_zips = {}
                for z in zips:
                    stem = z.stem.replace("(1)", "").strip()
                    if stem.isdigit() and len(stem) == 4:
                        year_zips[int(stem)] = z
                if len(year_zips) >= 10:
                    self._daily_dir = d
                    self._year_zips = year_zips
                    return

    def health_check(self) -> bool:
        return self._daily_dir is not None and len(self._year_zips) > 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source=DataSource.LOCAL_VENDOR,
            adjustments=[AdjustmentMode.NONE],
            periods=[BarPeriod.DAY],
            supports_batch=False,
            max_batch_size=1,
            requires_client_online=False,
            supports_universe=True,
            supports_delisted=True,
            supports_bse=True,
        )

    def available_years(self) -> List[int]:
        return sorted(self._year_zips.keys())

    def year_zip_path(self, year: int) -> Optional[Path]:
        """Public accessor: filesystem path of the chosen ZIP for a year."""
        return self._year_zips.get(int(year))

    def list_symbols_in_year(self, year: int) -> List[str]:
        """List all canonical symbols available in a given year ZIP."""
        zpath = self._year_zips.get(year)
        if not zpath:
            return []
        symbols = []
        with zipfile.ZipFile(zpath, "r") as zf:
            for name in zf.namelist():
                if name.endswith(".csv") and "__MACOSX" not in name:
                    fname = Path(name).stem
                    try:
                        symbols.append(_code_to_canonical(fname))
                    except InvalidSymbol:
                        continue
        return sorted(symbols)

    def fetch_bars(self, request: MarketDataRequest) -> List[MarketBar]:
        if request.adjustment != AdjustmentMode.NONE:
            raise InvalidSymbol(
                f"LocalVendorProvider only supports adjustment=none, got {request.adjustment}"
            )
        if request.period != BarPeriod.DAY:
            raise InvalidSymbol(
                f"LocalVendorProvider only supports period=1d, got {request.period}"
            )

        bars: List[MarketBar] = []
        for symbol in request.symbols:
            _validate_symbol_exchange(symbol)
            vendor_code = _canonical_to_vendor_code(symbol)
            symbol_bars = self._read_symbol_all_years(
                vendor_code, symbol, request.start_date, request.end_date
            )
            if not symbol_bars:
                raise DataNotDownloaded(
                    f"No data for {symbol} in vendor archives"
                )
            bars.extend(symbol_bars)
        return bars

    def fetch_bars_zipfirst(
        self,
        symbols: List[str],
        start_date: Optional[int] = None,
        end_date: Optional[int] = None,
    ) -> Dict[str, List[MarketBar]]:
        """ZIP-first batch read: open each year ZIP once, iterate all CSVs inside.

        Returns {canonical_symbol: [MarketBar, ...]} for all requested symbols.
        This is O(27 ZIP opens) instead of O(symbols × 27 ZIP opens).
        """
        for s in symbols:
            _validate_symbol_exchange(s)

        vendor_to_canonical: Dict[str, str] = {}
        for s in symbols:
            vc = _canonical_to_vendor_code(s)
            vendor_to_canonical[vc] = s

        target_fnames: Set[str] = {f"{vc}.csv" for vc in vendor_to_canonical}

        result: Dict[str, Dict[int, MarketBar]] = {s: {} for s in symbols}

        for year in sorted(self._year_zips.keys()):
            zpath = self._year_zips[year]
            year_str = str(year)
            with zipfile.ZipFile(zpath, "r") as zf:
                for name in zf.namelist():
                    if "__MACOSX" in name or not name.endswith(".csv"):
                        continue
                    fname = Path(name).name
                    if fname not in target_fnames:
                        continue
                    vendor_code = fname[:-4]
                    canonical = vendor_to_canonical.get(vendor_code)
                    if canonical is None:
                        continue
                    raw = zf.read(name)
                    bars = self._parse_csv(raw, canonical, start_date, end_date)
                    for b in bars:
                        result[canonical][b.trade_date] = b

        out: Dict[str, List[MarketBar]] = {}
        for s in symbols:
            date_map = result[s]
            out[s] = [date_map[d] for d in sorted(date_map.keys())]
        return out

    def _read_symbol_all_years(
        self,
        vendor_code: str,
        canonical: str,
        start_date: Optional[int],
        end_date: Optional[int],
    ) -> List[MarketBar]:
        """Read a single symbol across all year ZIPs (legacy path for small requests)."""
        bars_by_date: Dict[int, MarketBar] = {}
        fname = f"{vendor_code}.csv"

        for year in sorted(self._year_zips.keys()):
            zpath = self._year_zips[year]
            year_str = str(year)
            possible_paths = [
                f"{year_str}/{fname}",
                fname,
            ]
            with zipfile.ZipFile(zpath, "r") as zf:
                for p in possible_paths:
                    if p in zf.namelist():
                        raw = zf.read(p)
                        bars = self._parse_csv(raw, canonical, start_date, end_date)
                        for b in bars:
                            bars_by_date[b.trade_date] = b
                        break

        return [bars_by_date[d] for d in sorted(bars_by_date.keys())]

    def _parse_csv(
        self,
        raw: bytes,
        canonical: str,
        start_date: Optional[int],
        end_date: Optional[int],
    ) -> List[MarketBar]:
        """Parse a vendor CSV file into MarketBars.

        Unit conversion (vendor → project standard):
          volume: 手(lots) → 股(shares), ×100
          amount: 千元(1000 CNY) → 元(CNY), ×1000
        """
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        bars: List[MarketBar] = []

        for row in reader:
            try:
                date_str = row.get("datetime", "").strip()
                if not date_str:
                    continue
                trade_date = int(date_str.replace("-", ""))

                if start_date and trade_date < start_date:
                    continue
                if end_date and trade_date > end_date:
                    continue

                o = float(row["open"])
                h = float(row["high"])
                l = float(row["low"])
                c = float(row["close"])
                v = float(row.get("volume") or 0) * 100.0
                a = float(row.get("amount") or 0) * 1000.0

                if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                    continue

                bars.append(
                    MarketBar(
                        symbol=canonical,
                        trade_date=trade_date,
                        period="1d",
                        open=o,
                        high=h,
                        low=l,
                        close=c,
                        volume=v,
                        amount=a,
                        source=DataSource.LOCAL_VENDOR.value,
                        adjustment=AdjustmentMode.NONE.value,
                        provider_version=self.provider_version(),
                    )
                )
            except (ValueError, KeyError):
                continue

        return bars

    def fetch_universe(
        self,
        *,
        include_delisted: bool = False,
        include_bse: bool = False,
    ) -> List[UniverseEntry]:
        """List all symbols as historical union across all year ZIPs.

        Delisted inference: a symbol present in year N but absent from the
        latest year is marked status='delisted'. Symbols present in the
        latest year are 'listed'.
        """
        if not self._year_zips:
            return []
        all_symbols: Set[str] = set()
        latest_year = max(self._year_zips.keys())
        latest_symbols: Set[str] = set()

        for year in sorted(self._year_zips.keys()):
            syms = self.list_symbols_in_year(year)
            all_symbols.update(syms)
            if year == latest_year:
                latest_symbols.update(syms)

        entries = []
        for s in sorted(all_symbols):
            parts = s.split(".")
            exch = parts[0] if parts else ""
            status = "listed" if s in latest_symbols else "delisted"
            entries.append(
                UniverseEntry(
                    symbol=s,
                    name="",
                    exchange=exch,
                    status=status,
                    source=DataSource.LOCAL_VENDOR.value,
                )
            )
        return entries

    def provider_version(self) -> str:
        return "local_vendor_v1"
