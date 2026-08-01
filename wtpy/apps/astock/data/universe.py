"""Shanghai/Shenzhen A-share universe filtering."""

from __future__ import annotations

import json
from .io_util import atomic_write_json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set


def is_ashare_code(raw: str) -> bool:
    """Return True if code is Shanghai/Shenzhen A-share (exclude BJ, B, fund, index, bond).

    Accepted:
      - SSE A: 600/601/603/605 xxxxxx
      - SSE STAR: 688/689
      - SZSE main: 000/001
      - SZSE SME: 002/003
      - SZSE ChiNext: 300-309 (generic 30x segment, covers 302/305/... additions)
    Rejected:
      - BJ 8xxxxx / 4xxxxx
      - B shares 900/200
      - funds/ETFs 5xxxxx / 15xxxx / 16xxxx / 18xxxx
      - indices 000xxx on SH (sh000*), sz399*
      - bonds etc.
    """
    code = raw.lower().replace(".day", "")
    if code.startswith(("sh", "sz", "bj")):
        prefix = code[:2]
        num = code[2:]
    else:
        prefix = ""
        num = code
    if not re.fullmatch(r"\d{6}", num):
        return False
    if prefix == "bj" or num.startswith(("4", "8")):
        return False
    # indices
    if prefix == "sh" and num.startswith("000"):
        return False
    if prefix == "sz" and num.startswith("399"):
        return False
    # B shares
    if num.startswith(("900", "200")):
        return False
    # funds / ETFs / LOF
    if num.startswith(("5", "15", "16", "18", "1")) and not (
        num[:2] == "30"
        or num.startswith(
            ("000", "001", "002", "003", "600", "601", "603", "605", "688", "689")
        )
    ):
        # careful: sz1xxxxx may be funds; sz000 is main board
        if num.startswith(("15", "16", "18", "50", "51", "52", "56", "58")):
            return False
        if prefix == "sh" and num.startswith("5"):
            return False
    # positive allow-list (ChiNext is the generic 30x segment: 300-309)
    if num[:2] == "30" or num.startswith(
        ("600", "601", "603", "605", "688", "689", "000", "001", "002", "003")
    ):
        # sh000 is index already excluded
        if prefix == "sh" and num.startswith("000"):
            return False
        return True
    return False


def is_bse_code(raw: str) -> bool:
    """True for Beijing Stock Exchange A-shares.

    BSE code segments: 43xxxx / 83xxxx / 87xxxx (historical) and 920xxx
    (post-migration canonical range). SSE 9xxxxx B-shares are 900xxx and do
    NOT overlap 920xxx.
    """
    code = raw.lower().replace(".day", "")
    if code.startswith("bj"):
        num = code[2:]
    else:
        num = code
    if not re.fullmatch(r"\d{6}", num):
        return False
    return num.startswith(("4", "8")) or num.startswith("92")


def to_std_code(raw: str) -> str:
    """Convert sh600000 / sz000001 / bj430047 / 920001 (and canonical
    SSE.STK.* / SZSE.STK.* / BSE.STK.* passthrough) to canonical form.

    BSE segments: 43/83/87 (historical) and 920 (post-migration). Bare
    92xxxx maps to BSE — SSE B-shares are 900xxx and never 92xxxx.
    """
    s = str(raw).strip()
    if s.upper().startswith(("SSE.", "SZSE.", "BSE.")):
        parts = s.split(".")
        if len(parts) == 3 and parts[2].isdigit():
            return f"{parts[0].upper()}.{parts[1].upper()}.{parts[2]}"
        raise ValueError(f"bad code: {raw}")
    code = s.lower().replace(".day", "")
    if code.startswith("sh"):
        num = code[2:]
        return f"SSE.STK.{num}"
    if code.startswith("sz"):
        num = code[2:]
        return f"SZSE.STK.{num}"
    if code.startswith("bj"):
        num = code[2:]
        return f"BSE.STK.{num}"
    if code.isdigit() and len(code) == 6:
        if code.startswith("92"):
            return f"BSE.STK.{code}"
        if code.startswith(("5", "6", "9")):
            return f"SSE.STK.{code}"
        if code.startswith(("4", "8")):
            return f"BSE.STK.{code}"
        return f"SZSE.STK.{code}"
    raise ValueError(f"bad code: {raw}")


def exchange_of(std_code: str) -> str:
    return std_code.split(".")[0]


def numeric_code(std_code: str) -> str:
    return std_code.split(".")[-1]


@dataclass
class SymbolInfo:
    raw: str
    std_code: str
    exchange: str
    code: str
    name: str = ""
    product: str = "STK"
    list_date: Optional[int] = None
    delist_date: Optional[int] = None
    status: str = "listed"
    source: str = "tdx_local"
    first_market_date: Optional[int] = None
    last_market_date: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


class AShareUniverse:
    def __init__(self, symbols: Optional[List[SymbolInfo]] = None):
        self.symbols: List[SymbolInfo] = list(symbols or [])

    def __len__(self) -> int:
        return len(self.symbols)

    def codes(self) -> List[str]:
        return [s.std_code for s in self.symbols]

    def filter_codes(self, codes: Iterable[str]) -> List[str]:
        allow: Set[str] = set(self.codes())
        return [c for c in codes if c in allow]

    @classmethod
    def from_tdx_dirs(
        cls,
        sh_dir: Path,
        sz_dir: Path,
        *,
        include_bj: bool = False,
        bj_dir: Optional[Path] = None,
        names: Optional[Dict[str, str]] = None,
        include_delisted: bool = False,
    ) -> "AShareUniverse":
        names = names or {}
        symbols: List[SymbolInfo] = []
        for d in (sh_dir, sz_dir):
            if not d or not Path(d).exists():
                continue
            for p in sorted(Path(d).glob("*.day")):
                raw = p.stem
                if not is_ashare_code(raw):
                    continue
                std = to_std_code(raw)
                exch, _, code = std.split(".")
                symbols.append(
                    SymbolInfo(
                        raw=raw,
                        std_code=std,
                        exchange=exch,
                        code=code,
                        name=names.get(std, names.get(code, "")),
                    )
                )
        if include_bj and bj_dir and Path(bj_dir).exists():
            for p in sorted(Path(bj_dir).glob("*.day")):
                raw = p.stem
                if not is_bse_code(raw):
                    continue
                std = to_std_code(raw)
                exch, _, code = std.split(".")
                symbols.append(
                    SymbolInfo(
                        raw=raw,
                        std_code=std,
                        exchange=exch,
                        code=code,
                        name=names.get(std, names.get(code, "")),
                    )
                )
        return cls(symbols)

    @classmethod
    def from_tushare_basic(
        cls,
        entries: List,
        *,
        include_bse: bool = False,
        include_delisted: bool = False,
    ) -> "AShareUniverse":
        """Build universe from Tushare stock_basic entries (UniverseEntry list)."""
        symbols: List[SymbolInfo] = []
        for e in entries:
            if not include_bse and getattr(e, "exchange", "") == "BSE":
                continue
            if not include_delisted and getattr(e, "status", "listed") == "delisted":
                continue
            std_code = getattr(e, "symbol", "")
            parts = std_code.split(".")
            exch = parts[0] if len(parts) >= 1 else ""
            code = parts[-1] if parts else ""
            symbols.append(
                SymbolInfo(
                    raw=code,
                    std_code=std_code,
                    exchange=exch,
                    code=code,
                    name=getattr(e, "name", ""),
                    list_date=getattr(e, "list_date", None),
                    delist_date=getattr(e, "delist_date", None),
                    status=getattr(e, "status", "listed"),
                    source=getattr(e, "source", "tushare"),
                )
            )
        return cls(symbols)

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        has_bse = any(s.exchange == "BSE" for s in self.symbols)
        has_delisted = any(s.status == "delisted" for s in self.symbols)
        data = {
            "count": len(self.symbols),
            "exclude_bj": not has_bse,
            "include_delisted": has_delisted,
            "symbols": [s.to_dict() for s in self.symbols],
            "survivor_bias_warning": (
                "" if has_delisted else
                "Local TDX pool may lack delisted names; results have survivor bias."
            ),
            "schema_version": 3,
        }
        atomic_write_json(path, data)

    @classmethod
    def load(cls, path: Path) -> "AShareUniverse":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        symbols = []
        allowed = set(SymbolInfo.__dataclass_fields__.keys())
        for s in data.get("symbols", []):
            symbols.append(SymbolInfo(**{k: v for k, v in s.items() if k in allowed}))
        return cls(symbols)
