"""Catalog rebuild and stable SHA helpers for global vs selected universes."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .data_store import sha256_file
from .io_util import atomic_write_json
from .universe import AShareUniverse, SymbolInfo


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def selected_universe_sha(codes: Sequence[str]) -> str:
    """Stable SHA over sorted unique std codes."""
    norm = sorted({c.strip() for c in codes if c and str(c).strip()})
    payload = "\n".join(norm) + ("\n" if norm else "")
    return sha256_text(payload)


def file_sha_or_empty(path: Path) -> str:
    path = Path(path)
    if not path.exists():
        return ""
    return sha256_file(path)


def rebuild_catalog_from_storage(
    storage_root: Path,
    *,
    tdx_root: Optional[Path] = None,
) -> dict:
    """Rebuild global manifest.json + universe.json from on-disk CSV/NPZ (+ optional TDX).

    Does not download data or recompute adjustment factors.
    Enumerates both CSV and NPZ; reports pairing anomalies.
    """
    storage_root = Path(storage_root)
    csv_root = storage_root / "csv" / "day"
    npz_root = storage_root / "npz" / "day"
    manifest_path = storage_root / "manifest.json"
    universe_path = storage_root / "universe.json"

    old_manifest_sha = file_sha_or_empty(manifest_path)
    old_universe_sha = file_sha_or_empty(universe_path)

    found: Dict[str, dict] = {}
    csv_count = 0
    npz_count = 0
    for exch_dir in ("SSE", "SZSE"):
        cdir = csv_root / exch_dir
        ndir = npz_root / exch_dir
        if cdir.exists():
            for pth in sorted(cdir.glob("*.csv")):
                csv_count += 1
                code = pth.stem
                std = f"{exch_dir}.STK.{code}"
                rec = found.setdefault(
                    std,
                    {
                        "std_code": std,
                        "exchange": exch_dir,
                        "code": code,
                        "csv": None,
                        "npz": None,
                    },
                )
                rec["csv"] = str(pth)
        if ndir.exists():
            for pth in sorted(ndir.glob("*.npz")):
                npz_count += 1
                code = pth.stem
                std = f"{exch_dir}.STK.{code}"
                rec = found.setdefault(
                    std,
                    {
                        "std_code": std,
                        "exchange": exch_dir,
                        "code": code,
                        "csv": None,
                        "npz": None,
                    },
                )
                rec["npz"] = str(pth)

    items: List[dict] = []
    symbols: List[SymbolInfo] = []
    anomalies = 0
    csv_only = 0
    npz_only = 0
    paired = 0
    invalid = 0

    for std, info in sorted(found.items()):
        has_csv = bool(info.get("csv") and Path(info["csv"]).exists())
        has_npz = bool(info.get("npz") and Path(info["npz"]).exists())
        if has_csv and has_npz:
            paired += 1
            status = "ok"
            message = "reindexed_csv_npz"
        elif has_csv and not has_npz:
            csv_only += 1
            anomalies += 1
            status = "csv_only"
            message = "npz_missing"
        elif has_npz and not has_csv:
            npz_only += 1
            anomalies += 1
            status = "npz_only"
            message = "csv_missing"
        else:
            invalid += 1
            anomalies += 1
            status = "invalid"
            message = "no_files"

        first = last = None
        n_rec = 0
        if has_csv:
            try:
                lines = Path(info["csv"]).read_text(encoding="utf-8").strip().splitlines()
                body = [ln for ln in lines[1:] if ln.strip()]
                n_rec = len(body)
                if body:
                    first = int(body[0].split(",")[0])
                    last = int(body[-1].split(",")[0])
                if n_rec == 0:
                    anomalies += 1
                    status = "error"
                    message = "empty_csv"
                    invalid += 1
            except Exception as e:  # noqa: BLE001
                status = "error"
                message = f"reindex_csv_error:{e}"
                anomalies += 1
                invalid += 1
                n_rec = 0

        source_path = None
        source_sha = None
        if tdx_root:
            num = info["code"]
            raw = ("sh" if info["exchange"] == "SSE" else "sz") + num
            day = Path(tdx_root) / "vipdoc" / raw[:2] / "lday" / f"{raw}.day"
            if day.exists():
                source_path = str(day)
                try:
                    source_sha = sha256_file(day)
                except Exception:
                    source_sha = None
                    anomalies += 1

        # Include CSV-present or NPZ-only symbols in catalog (explicit status)
        items.append(
            {
                "raw": (("sh" if info["exchange"] == "SSE" else "sz") + info["code"]),
                "std_code": std,
                "source_path": source_path,
                "source_sha256": source_sha,
                "n_records": n_rec,
                "first_date": first,
                "last_date": last,
                "n_issues": 0 if status in ("ok", "csv_only", "npz_only") else 1,
                "status": status,
                "message": message,
                "csv_path": info.get("csv"),
                "npz_path": info.get("npz"),
            }
        )
        symbols.append(
            SymbolInfo(
                raw=items[-1]["raw"],
                std_code=std,
                exchange=info["exchange"],
                code=info["code"],
                name="",
            )
        )

    payload_m = {
        "count": len(items),
        "ok": sum(1 for x in items if x["status"] == "ok"),
        "items": items,
        "schema_version": 3,
        "reindexed": True,
        "csv_count": csv_count,
        "npz_count": npz_count,
        "paired_count": paired,
        "csv_only": csv_only,
        "npz_only": npz_only,
        "invalid_count": invalid,
        "anomalies": anomalies,
        "note": "Rebuilt from local CSV/NPZ; factors not recomputed.",
    }
    atomic_write_json(manifest_path, payload_m)

    uni = AShareUniverse(symbols)
    uni.save(universe_path)

    return {
        "old_manifest_sha": old_manifest_sha,
        "old_universe_sha": old_universe_sha,
        "new_manifest_sha": file_sha_or_empty(manifest_path),
        "new_universe_sha": file_sha_or_empty(universe_path),
        "manifest_count": len(items),
        "universe_count": len(symbols),
        "csv_count": csv_count,
        "npz_count": npz_count,
        "paired_count": paired,
        "csv_only": csv_only,
        "npz_only": npz_only,
        "invalid_count": invalid,
        "anomalies": anomalies,
        "manifest_path": str(manifest_path),
        "universe_path": str(universe_path),
    }
