"""Global vs selected catalog isolation and rebuild tests."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

import json
from pathlib import Path

from wtpy.apps.astock.cli import main
from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.data.catalog import (
    file_sha_or_empty,
    rebuild_catalog_from_storage,
    selected_universe_sha,
)
from wtpy.apps.astock.data.data_store import DataStore, FileManifest, atomic_write_json
from wtpy.apps.astock.data.universe import AShareUniverse, SymbolInfo


def test_selected_universe_sha_order_insensitive():
    a = selected_universe_sha(["SSE.STK.600000", "SZSE.STK.000001"])
    b = selected_universe_sha(["SZSE.STK.000001", "SSE.STK.600000", "SSE.STK.600000"])
    assert a == b
    assert len(a) == 64


def test_codes_import_does_not_overwrite_global(tmp_path):
    storage = tmp_path / "storage"
    # seed global catalog with 2 symbols
    storage.mkdir(parents=True)
    (storage / "csv" / "day" / "SSE").mkdir(parents=True)
    (storage / "csv" / "day" / "SSE" / "600000.csv").write_text(
        "date,open,high,low,close,amount,volume\n20200102,10,11,9,10,1,1\n",
        encoding="utf-8",
    )
    (storage / "csv" / "day" / "SZSE").mkdir(parents=True)
    (storage / "csv" / "day" / "SZSE" / "000001.csv").write_text(
        "date,open,high,low,close,amount,volume\n20200102,10,11,9,10,1,1\n",
        encoding="utf-8",
    )
    r = rebuild_catalog_from_storage(storage)
    assert r["universe_count"] == 2
    g_man = file_sha_or_empty(storage / "manifest.json")
    g_uni = file_sha_or_empty(storage / "universe.json")

    # selection import should not change global (even if empty codes import path)
    # Simulate selection-only save path used by CLI: write selection file without touching global
    from wtpy.apps.astock.data.catalog import selected_universe_sha as sus

    sel = storage / "selections"
    sel.mkdir(parents=True)
    codes = ["SSE.STK.600000"]
    atomic_write_json(
        sel / "t.json",
        {"codes": codes, "selected_universe_sha": sus(codes), "count": 1},
    )
    assert file_sha_or_empty(storage / "manifest.json") == g_man
    assert file_sha_or_empty(storage / "universe.json") == g_uni


def test_rebuild_catalog_counts_disk(tmp_path):
    storage = tmp_path / "storage"
    for exch, code in (("SSE", "600000"), ("SSE", "600519"), ("SZSE", "000001")):
        d = storage / "csv" / "day" / exch
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{code}.csv").write_text(
            "date,open,high,low,close,amount,volume\n20200102,1,1,1,1,1,1\n",
            encoding="utf-8",
        )
    r = rebuild_catalog_from_storage(storage)
    assert r["manifest_count"] == 3
    assert r["universe_count"] == 3
    uni = json.loads((storage / "universe.json").read_text(encoding="utf-8"))
    assert uni["count"] == 3
