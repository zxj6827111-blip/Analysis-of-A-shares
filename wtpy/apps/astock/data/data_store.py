"""Local storage for imported A-share bars (CSV + optional DSB + parquet-like numpy)."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .tdx_reader import DayBar, bars_to_arrays, parse_day_file
from .universe import is_ashare_code, to_std_code
from .adjustments import build_factor_series

logger = logging.getLogger(__name__)


from .io_util import atomic_write_text, atomic_write_json


@dataclass
class FileManifest:
    raw: str
    std_code: str
    source_path: str
    source_sha256: str
    n_records: int
    first_date: Optional[int]
    last_date: Optional[int]
    n_issues: int
    status: str
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def bars_to_csv(path: Path, bars: Sequence[DayBar]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["date,open,high,low,close,amount,volume"]
    for b in bars:
        lines.append(
            f"{b.date},{b.open:.2f},{b.high:.2f},{b.low:.2f},{b.close:.2f},{b.amount},{b.volume}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_bars_csv(path: Path) -> List[DayBar]:
    path = Path(path)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    bars: List[DayBar] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split(",")
        bars.append(
            DayBar(
                date=int(parts[0]),
                open=float(parts[1]),
                high=float(parts[2]),
                low=float(parts[3]),
                close=float(parts[4]),
                amount=float(parts[5]),
                volume=float(parts[6]),
            )
        )
    return bars


def save_arrays_npz(path: Path, arrays: Dict[str, np.ndarray]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **arrays)


def load_arrays_npz(path: Path) -> Dict[str, np.ndarray]:
    data = np.load(path)
    return {k: data[k] for k in data.files}


def try_store_dsb(
    bar_file: Path,
    bars: Sequence[DayBar],
) -> Tuple[bool, str]:
    """Best-effort DSB write via WonderTrader helper; non-fatal on failure."""
    try:
        from ctypes import POINTER

        from wtpy.WtCoreDefs import WTSBarStruct
        from wtpy.wrapper.WtDtHelper import WtDataHelper
    except Exception as e:  # pragma: no cover
        return False, f"wt_helper_import_failed:{e}"

    if not bars:
        return False, "empty"

    try:
        helper = WtDataHelper()
        arr = (WTSBarStruct * len(bars))()
        for i, b in enumerate(bars):
            arr[i].date = int(b.date)
            arr[i].reserve = 0
            # day bar time convention in WT often date as time base
            arr[i].time = int(b.date)
            arr[i].open = float(b.open)
            arr[i].high = float(b.high)
            arr[i].low = float(b.low)
            arr[i].close = float(b.close)
            arr[i].settle = 0.0
            arr[i].money = float(b.amount)
            arr[i].vol = float(b.volume)
            arr[i].hold = 0.0
            arr[i].diff = 0.0
        bar_file = Path(bar_file)
        bar_file.parent.mkdir(parents=True, exist_ok=True)
        ok = helper.store_bars(str(bar_file), arr, len(bars), "d")
        return bool(ok), "ok" if ok else "store_bars_false"
    except Exception as e:
        return False, f"store_bars_error:{e}"


class DataStore:
    def __init__(self, storage_root: Path):
        self.root = Path(storage_root)
        self.csv_root = self.root / "csv" / "day"
        self.npz_root = self.root / "npz" / "day"
        self.dsb_root = self.root / "his" / "day"
        self.manifest_path = self.root / "manifest.json"

    def paths_for(self, std_code: str) -> Dict[str, Path]:
        exch, _, code = std_code.split(".")
        return {
            "csv": self.csv_root / exch / f"{code}.csv",
            "npz": self.npz_root / exch / f"{code}.npz",
            "dsb": self.dsb_root / exch / f"STK{code}.dsb",
        }

    def import_day_file(
        self,
        source: Path,
        *,
        write_dsb: bool = True,
        fetch_factors: bool = True,
    ) -> FileManifest:
        source = Path(source)
        raw = source.stem
        if not is_ashare_code(raw):
            return FileManifest(
                raw=raw,
                std_code="",
                source_path=str(source),
                source_sha256=sha256_file(source) if source.exists() else "",
                n_records=0,
                first_date=None,
                last_date=None,
                n_issues=0,
                status="skipped_non_ashare",
            )
        std = to_std_code(raw)
        digest = sha256_file(source)
        try:
            bars, issues = parse_day_file(source)
        except Exception as e:
            return FileManifest(
                raw=raw,
                std_code=std,
                source_path=str(source),
                source_sha256=digest,
                n_records=0,
                first_date=None,
                last_date=None,
                n_issues=1,
                status="error",
                message=str(e),
            )
        paths = self.paths_for(std)
        bars_to_csv(paths["csv"], bars)
        save_arrays_npz(paths["npz"], bars_to_arrays(bars))
        # raw bars only in csv/npz; factors stored separately (no future leak on align)
        factor_msg = "factors_skipped"
        if fetch_factors:
            try:
                dates = [b.date for b in bars]
                adj_root = self.root / "adjustments"
                series = build_factor_series(
                    std,
                    dates,
                    adj_root=adj_root,
                    prefer_baostock=True,
                    force_identity=False,
                    refresh=True,
                )
                factor_msg = f"factor_source={series.source};factor_sha={series.sha256[:12]}"
            except Exception as e:
                factor_msg = f"factor_error:{e}"
        dsb_msg = ""
        if write_dsb:
            ok, dsb_msg = try_store_dsb(paths["dsb"], bars)
            if not ok:
                logger.info("DSB store skipped/failed for %s: %s", std, dsb_msg)
        return FileManifest(
            raw=raw,
            std_code=std,
            source_path=str(source),
            source_sha256=digest,
            n_records=len(bars),
            first_date=bars[0].date if bars else None,
            last_date=bars[-1].date if bars else None,
            n_issues=len(issues),
            status="ok",
            message=(dsb_msg + ";" + factor_msg) if dsb_msg else factor_msg,
        )

    def load_symbol(self, std_code: str) -> List[DayBar]:
        paths = self.paths_for(std_code)
        if paths["csv"].exists():
            return load_bars_csv(paths["csv"])
        if paths["npz"].exists():
            arr = load_arrays_npz(paths["npz"])
            bars = []
            for i in range(len(arr["date"])):
                bars.append(
                    DayBar(
                        date=int(arr["date"][i]),
                        open=float(arr["open"][i]),
                        high=float(arr["high"][i]),
                        low=float(arr["low"][i]),
                        close=float(arr["close"][i]),
                        amount=float(arr["amount"][i]),
                        volume=float(arr["volume"][i]),
                    )
                )
            return bars
        raise FileNotFoundError(std_code)

    def load_arrays(self, std_code: str) -> Dict[str, np.ndarray]:
        paths = self.paths_for(std_code)
        if paths["npz"].exists():
            return load_arrays_npz(paths["npz"])
        bars = self.load_symbol(std_code)
        return bars_to_arrays(bars)

    def save_manifest(self, items: List[FileManifest], *, path: Optional[Path] = None) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = Path(path or self.manifest_path)
        payload = {
            "count": len(items),
            "ok": sum(1 for x in items if x.status == "ok"),
            "items": [x.to_dict() for x in items],
            "schema_version": 2,
        }
        atomic_write_json(path, payload)
        return path
