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
      - SZSE ChiNext: 300/301
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
    if num.startswith(("5", "15", "16", "18", "1")) and not num.startswith(
        ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689")
    ):
        # careful: sz1xxxxx may be funds; sz000 is main board
        if num.startswith(("15", "16", "18", "50", "51", "52", "56", "58")):
            return False
        if prefix == "sh" and num.startswith("5"):
            return False
    # positive allow-list
    if num.startswith(
        ("600", "601", "603", "605", "688", "689", "000", "001", "002", "003", "300", "301")
    ):
        # sh000 is index already excluded
        if prefix == "sh" and num.startswith("000"):
            return False
        return True
    return False


def to_std_code(raw: str) -> str:
    """Convert sh600000 / sz000001 to SSE.STK.600000 / SZSE.STK.000001."""
    code = raw.lower().replace(".day", "")
    if code.startswith("sh"):
        num = code[2:]
        return f"SSE.STK.{num}"
    if code.startswith("sz"):
        num = code[2:]
        return f"SZSE.STK.{num}"
    if code.isdigit() and len(code) == 6:
        if code.startswith(("5", "6", "9")):
            return f"SSE.STK.{code}"
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
            # intentionally unused in v1; kept for completeness
            pass
        return cls(symbols)

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "count": len(self.symbols),
            "exclude_bj": True,
            "symbols": [s.to_dict() for s in self.symbols],
            "survivor_bias_warning": (
                "Local TDX pool may lack delisted names; results have survivor bias."
            ),
            "schema_version": 2,
        }
        atomic_write_json(path, data)

    @classmethod
    def load(cls, path: Path) -> "AShareUniverse":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        symbols = [SymbolInfo(**s) for s in data.get("symbols", [])]
        return cls(symbols)
