"""Integration: small universe import, signals, backtest, bagua study."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

import json
from pathlib import Path

import pytest

from wtpy.apps.astock.cli import main
from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.indicators.registry import IndicatorRegistry


def _tdx_ok() -> bool:
    return (get_default_config().tdx_root / "vipdoc" / "sh" / "lday" / "sh600000.day").exists()


@pytest.mark.skipif(not _tdx_ok(), reason="local TDX required")
def test_e2e_small_pool(tmp_path):
    storage = tmp_path / "storage"
    rc = main([
        "--storage", str(storage),
        "import-data",
        "--codes", "sh600000,sz000001,sh600519,sz000002",
        "--skip-dsb",
        "--skip-factors",
    ])
    assert rc == 0
    # selection import must not create/overwrite global catalog; selection snapshot is enough
    assert list((storage / "selections").glob("import_sel_*.json")) or (storage / "manifest.json").exists()

    cfg = get_default_config(storage_root=storage)
    reg = IndicatorRegistry.bootstrap(cfg.indicator_dir, cfg.mapping_path)
    v5 = [s for s in reg.list() if s.compile_status == "ready" and s.id.startswith("txt_")]
    assert v5
    v5_id = v5[0].id
    s735 = [s for s in reg.list() if "735" in s.name][0]
    # after explicit human pairing, 735 is ready; dual-increase remains source_required without map
    dual = [s for s in reg.list() if s.compile_status == "source_required"]
    assert dual, "expected at least one source_required package (e.g. dual-increase)"
    blocked = dual[0]

    rc = main([
        "--storage", str(storage),
        "backtest",
        "--indicator", v5_id,
        "--period", "DAY",
        "--hold", "1",
        "--codes", "sh600000,sz000001",
        "--start", "20200101",
        "--end", "20201231",
        "--research-unadjusted",
        "--run-id", "e2e_bt",
    ])
    assert rc == 0
    out = cfg.output_root / "e2e_bt"
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    meta = json.loads((out / "run_meta.json").read_text(encoding="utf-8"))
    assert "total_return" in metrics
    assert meta.get("status") == "research_unadjusted"
    # dual_price_v1: research_unadjusted still executes raw; price_mode is dual_price_v1
    _pm = meta.get("repro", {}).get("price_mode")
    assert _pm in ("raw", "dual_price_v1")
    assert meta.get("repro", {}).get("execution_price_mode", "raw") == "raw"
    assert meta.get("repro", {}).get("signal_price_mode") in ("raw", None)
    # hold is top-level / repro (AppConfig snapshot in config has no strategy hold)
    assert meta.get("hold") == 1 or meta.get("repro", {}).get("hold") == 1
    if (out / "signals.csv").exists():
        rows = (out / "signals.csv").read_text(encoding="utf-8-sig").strip().splitlines()[1:]
        for row in rows:
            d = int(row.split(",")[1])
            assert 20200101 <= d <= 20201231

    rc = main([
        "--storage", str(storage),
        "backtest",
        "--indicator", v5_id,
        "--period", "DWM",
        "--hold", "3",
        "--codes", "sh600000,sz000001",
        "--start", "20200101",
        "--end", "20210630",
        "--research-unadjusted",
        "--run-id", "e2e_dwm",
    ])
    assert rc == 0
    assert (cfg.output_root / "e2e_dwm" / "metrics.json").exists()

    rc = main([
        "--storage", str(storage),
        "backtest",
        "--indicator", blocked.id,
        "--period", "DAY",
        "--codes", "sh600000",
        "--research-unadjusted",
        "--run-id", "should_fail",
    ])
    assert rc == 2

    rc = main([
        "--storage", str(storage),
        "bagua-study",
        "--period", "WEEK",
        "--codes", "sh600000",
        "--run-id", "e2e_bagua",
    ])
    assert rc == 0
    assert (cfg.output_root / "e2e_bagua" / "bagua_stats.csv").exists()
