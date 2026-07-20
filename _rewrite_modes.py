from pathlib import Path
# Make CLI formula mode tests not hang: skip baostock by using identity + research path carefully
# For unconfirmed adjusted test, if factors not ready skip is ok
# Fix test_cli_formula_modes to avoid refresh baostock login storms
p = Path("tests/apps/astock/test_cli_formula_modes.py")
t = p.read_text(encoding="utf-8")
# replace baostock refresh with prefer_baostock False + force not used; use skip always for adjusted if no cached factors
# Simpler: mark baostock-dependent tests with timeout skip and keep unit audit tests strong
# Rewrite unconfirmed adjusted test to mock formal_adjustment_ready via writing complete baostock quality file without network

new = r'''"""CLI formula research vs price mode independence; meta fields first-write."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

import json
import struct
from pathlib import Path

import pytest

from wtpy.apps.astock.cli import main
from wtpy.apps.astock.data.adjustments import FactorSeries, formal_adjustment_ready, save_factors
from wtpy.apps.astock.indicators.tn6_importer import (
    confirm_source_pair,
    load_source_map,
)


def _write_history_csv(path: Path, n: int = 80):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["date,open,high,low,close,amount,volume"]
    for i in range(n):
        d = 20200102 + i
        # keep simple monotonic dates even if not real calendar
        lines.append(f"{d},10,11,9,10,1,1000")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [20200102 + i for i in range(n)]


def _write_day_file(path: Path, n: int = 80):
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = b""
    for i in range(n):
        d = 20200102 + i
        blob += struct.pack("<IIIIIfII", d, 1000, 1100, 900, 1000, 1.0, 1000, 0)
    path.write_bytes(blob)


def _setup(tmp_path: Path):
    tdx = tmp_path / "tdx"
    storage = tmp_path / "storage"
    ind = tmp_path / "ind"
    ind.mkdir()
    _write_day_file(tdx / "vipdoc" / "sh" / "lday" / "sh600000.day")
    _write_day_file(tdx / "vipdoc" / "sh" / "lday" / "sh000001.day")
    (ind / "x735.tn6").write_bytes(b"TN6X" + b"\x00" * 40)
    (ind / "x735.txt").write_text("MA7:=MA(C,7);\nMA35:=MA(C,35);\nXG:CROSS(MA7,MA35);\n", encoding="utf-8")
    main = __import__("wtpy.apps.astock.cli", fromlist=["main"]).main
    assert main(["--storage", str(storage), "--tdx-root", str(tdx), "import-data", "--skip-dsb", "--skip-factors"]) == 0
    assert main(["--storage", str(storage), "--indicator-dir", str(ind), "pair-735"]) == 0
    # write synthetic complete factors (no network)
    dates = _write_history_csv(storage / "csv" / "day" / "SSE" / "600000.csv")
    # also need npz optional
    factors = {d: 1.0 + (i * 0.0) for i, d in enumerate(dates)}
    # store as baostock complete quality file used by load path
    from wtpy.apps.astock.data.adjustments import build_factor_series, FactorSeries, align_factors_to_dates
    import numpy as np
    # create FactorSeries complete manually
    arr = [1.0] * len(dates)
    series = FactorSeries(
        std_code="SSE.STK.600000",
        dates=dates,
        factors=arr,
        source="baostock",
        source_detail="test_fixture",
        event_dates=[dates[0]],
        event_factors=[1.0],
        prehistory_factor=1.0,
        quality="complete",
        sha256="test",
    )
    adj_path = storage / "adjustments" / "SSE_STK_600000.json"
    adj_path.parent.mkdir(parents=True, exist_ok=True)
    adj_path.write_text(
        json.dumps({
            "std_code": series.std_code,
            "source": "baostock",
            "source_detail": "test_fixture",
            "quality": "complete",
            "prehistory_factor": 1.0,
            "event_dates": series.event_dates,
            "event_factors": series.event_factors,
            "dates": series.dates,
            "factors": series.factors,
            "sha256": "test",
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ok, msg = formal_adjustment_ready([series])
    assert ok, msg
    return storage, ind, dates


def test_unconfirmed_without_flag_rejected(tmp_path):
    storage, ind, dates = _setup(tmp_path)
    from wtpy.apps.astock.cli import main
    rc = main([
        "--storage", str(storage), "--indicator-dir", str(ind),
        "backtest", "--indicator", "tn6_x735", "--period", "DAY", "--hold", "1",
        "--codes", "sh600000", "--start", str(dates[10]), "--end", str(dates[-1]),
        "--run-id", "reject_formal",
    ])
    assert rc == 4


def test_unconfirmed_research_adjusted_without_raw(tmp_path):
    storage, ind, dates = _setup(tmp_path)
    from wtpy.apps.astock.cli import main
    from wtpy.apps.astock.config import get_default_config
    rc = main([
        "--storage", str(storage), "--indicator-dir", str(ind),
        "backtest", "--indicator", "tn6_x735", "--period", "DAY", "--hold", "1",
        "--codes", "sh600000", "--start", str(dates[10]), "--end", str(dates[-1]),
        "--research-unconfirmed-formula",
        "--run-id", "research_adj_unconfirmed",
    ])
    assert rc == 0
    out = get_default_config(storage_root=storage).output_root / "research_adj_unconfirmed"
    if not (out / "run_meta.json").exists():
        out = Path("outputs/astock/research_adj_unconfirmed")
    meta = json.loads((out / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "research_unconfirmed_formula"
    assert meta["repro"]["price_mode"] == "adjusted"


def test_confirmed_formal_ok_meta_fields(tmp_path):
    storage, ind, dates = _setup(tmp_path)
    map_path = storage / "indicators" / "tn6_source_map.json"
    mapping = load_source_map(map_path)
    pkg = next(iter(mapping))
    confirm_source_pair(map_path, pkg, confirmed_by="tester", note="unit")
    from wtpy.apps.astock.cli import main
    from wtpy.apps.astock.config import get_default_config
    rc = main([
        "--storage", str(storage), "--indicator-dir", str(ind),
        "backtest", "--indicator", "tn6_x735", "--period", "DAY", "--hold", "1",
        "--codes", "sh600000", "--start", str(dates[10]), "--end", str(dates[-1]),
        "--run-id", "formal_confirmed",
    ])
    assert rc == 0
    out = get_default_config(storage_root=storage).output_root / "formal_confirmed"
    if not (out / "run_meta.json").exists():
        out = Path("outputs/astock/formal_confirmed")
    meta = json.loads((out / "run_meta.json").read_text(encoding="utf-8"))
    repro = meta["repro"]
    assert meta["status"] == "ok"
    assert repro["price_mode"] == "adjusted"
    assert repro["formula_provenance"] == "user_provided_human_formula"
    assert repro["source_pair_status"] == "paired_confirmed"
    assert repro["formal_backtest_allowed"] is True
    assert repro.get("confirmed_by")
    assert repro.get("package_sha256")
    assert repro.get("source_sha256")
    assert repro.get("global_manifest_sha")
    assert repro.get("selected_universe_sha")
    assert (out / "summary.xlsx").exists()
    assert not (out / "summary.xlsx.failed").exists()
'''
Path("tests/apps/astock/test_cli_formula_modes.py").write_text(new, encoding="utf-8")
print("rewrote formula mode tests offline")
