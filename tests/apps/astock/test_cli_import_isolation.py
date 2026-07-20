"""Real CLI import-data must not overwrite global catalog when --codes is set."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

import json
import struct
from pathlib import Path

from wtpy.apps.astock.cli import main
from wtpy.apps.astock.data.catalog import (
    file_sha_or_empty,
    rebuild_catalog_from_storage,
    selected_universe_sha,
)


def _write_day(path: Path, date: int = 20200102, open_=10.0, high=11.0, low=9.0, close=10.0):
    """Write one TDX 32-byte .day record (prices in fen)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = struct.pack(
        "<IIIIIfII",
        int(date),
        int(round(open_ * 100)),
        int(round(high * 100)),
        int(round(low * 100)),
        int(round(close * 100)),
        1.0,
        1000,
        0,
    )
    path.write_bytes(rec)


def _mini_tdx(root: Path, codes=("sh600000", "sz000001")):
    for code in codes:
        exch = code[:2]
        _write_day(root / "vipdoc" / exch / "lday" / f"{code}.day")
    # index for calendar optional
    _write_day(root / "vipdoc" / "sh" / "lday" / "sh000001.day")


def test_cli_codes_import_preserves_global_catalog(tmp_path):
    tdx = tmp_path / "tdx"
    storage = tmp_path / "storage"
    _mini_tdx(tdx, ("sh600000", "sz000001", "sh600519"))
    # full import first (no codes) builds global catalog
    rc = main(
        [
            "--storage",
            str(storage),
            "--tdx-root",
            str(tdx),
            "import-data",
            "--skip-dsb",
            "--skip-factors",
        ]
    )
    assert rc == 0
    g_m = file_sha_or_empty(storage / "manifest.json")
    g_u = file_sha_or_empty(storage / "universe.json")
    man = json.loads((storage / "manifest.json").read_text(encoding="utf-8"))
    assert man["count"] >= 2

    # selection import
    rc = main(
        [
            "--storage",
            str(storage),
            "--tdx-root",
            str(tdx),
            "import-data",
            "--codes",
            "sh600000",
            "--skip-dsb",
            "--skip-factors",
        ]
    )
    assert rc == 0
    assert file_sha_or_empty(storage / "manifest.json") == g_m
    assert file_sha_or_empty(storage / "universe.json") == g_u
    sels = list((storage / "selections").glob("import_sel_*.json"))
    assert sels
    sel = json.loads(sels[-1].read_text(encoding="utf-8"))
    assert sel.get("selected_codes_count", sel.get("count")) == 1
    assert sel["selected_universe_sha"] == selected_universe_sha(sel["codes"])


def test_limit_import_does_not_overwrite_global(tmp_path):
    tdx = tmp_path / "tdx"
    storage = tmp_path / "storage"
    _mini_tdx(tdx, ("sh600000", "sz000001", "sh600519"))
    main(
        [
            "--storage",
            str(storage),
            "--tdx-root",
            str(tdx),
            "import-data",
            "--skip-dsb",
            "--skip-factors",
        ]
    )
    g_m = file_sha_or_empty(storage / "manifest.json")
    rc = main(
        [
            "--storage",
            str(storage),
            "--tdx-root",
            str(tdx),
            "import-data",
            "--limit",
            "1",
            "--skip-dsb",
            "--skip-factors",
        ]
    )
    assert rc == 0
    assert file_sha_or_empty(storage / "manifest.json") == g_m


def test_rebuild_catalog_reports_csv_npz_pairing(tmp_path):
    storage = tmp_path / "storage"
    # csv+npz pair
    d = storage / "csv" / "day" / "SSE"
    d.mkdir(parents=True)
    (d / "600000.csv").write_text(
        "date,open,high,low,close,amount,volume\n20200102,1,1,1,1,1,1\n",
        encoding="utf-8",
    )
    nd = storage / "npz" / "day" / "SSE"
    nd.mkdir(parents=True)
    import numpy as np

    np.savez_compressed(
        nd / "600000.npz",
        date=np.array([20200102]),
        open=np.array([1.0]),
        high=np.array([1.0]),
        low=np.array([1.0]),
        close=np.array([1.0]),
        amount=np.array([1.0]),
        volume=np.array([1.0]),
    )
    # csv only
    (d / "600519.csv").write_text(
        "date,open,high,low,close,amount,volume\n20200102,1,1,1,1,1,1\n",
        encoding="utf-8",
    )
    # npz only
    np.savez_compressed(
        nd / "601318.npz",
        date=np.array([20200102]),
        open=np.array([1.0]),
        high=np.array([1.0]),
        low=np.array([1.0]),
        close=np.array([1.0]),
        amount=np.array([1.0]),
        volume=np.array([1.0]),
    )
    r = rebuild_catalog_from_storage(storage)
    assert r["csv_count"] == 2
    assert r["npz_count"] == 2
    assert r["paired_count"] == 1
    assert r["csv_only"] == 1
    assert r["npz_only"] == 1
    assert r["manifest_count"] == 3
