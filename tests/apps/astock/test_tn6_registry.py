"""TN6 import / registry tests."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401
import hashlib
from pathlib import Path

import pytest

from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.indicators.registry import IndicatorRegistry
from wtpy.apps.astock.indicators.tn6_importer import (
    build_specs_from_indicator_dir,
    file_sha256,
    import_tn6_with_source,
    scan_tn6_dir,
)


def test_scan_and_dedupe():
    cfg = get_default_config()
    ind = cfg.indicator_dir
    if not ind.exists():
        pytest.skip("指标 dir missing")
    pkgs = scan_tn6_dir(ind)
    assert len(pkgs) == 4
    shas = [p.sha256 for p in pkgs]
    # three dual-increase share hash
    assert shas.count(shas[1]) >= 2 or len(set(shas)) == 2


def test_registry_bootstrap_source_required():
    cfg = get_default_config()
    if not cfg.indicator_dir.exists():
        pytest.skip("指标 dir missing")
    reg = IndicatorRegistry.bootstrap(cfg.indicator_dir, cfg.mapping_path)
    specs = {s.name: s for s in reg.list()}
    # bagua present
    reg.get("bagua_ohlc")
    # 735: with explicit project map may be ready; without map must be source_required
    found = [s for s in reg.list() if "735" in s.name or "735" in (s.aliases or [""])[0]]
    assert found, "735 package should be registered"
    if found[0].source_file:
        assert found[0].compile_status == "ready"
        assert found[0].backtestable
    else:
        assert found[0].compile_status == "source_required"
        assert not found[0].backtestable

    # three dual packages -> one content version with 3 aliases
    dual = [s for s in reg.list() if s.package_sha256 and s.aliases and len(s.aliases) >= 2]
    assert dual, "expected deduped dual-increase content"
    assert len(dual[0].aliases) == 3

    # txt formulas registered separately
    txts = [s for s in reg.list() if s.id.startswith("txt_")]
    assert len(txts) >= 3
    # MIN60 formulas are research-backtestable via day-line MACD proxy
    min60 = [s for s in txts if "MIN60" in (s.dependencies or [])]
    assert min60
    assert all(s.compile_status == "ready" for s in min60), [s.failure_reason for s in min60]
    assert all(s.backtestable for s in min60)
    assert all((s.parameters or {}).get("min60_day_proxy") for s in min60)


def test_explicit_pair_only(tmp_path):
    cfg = get_default_config()
    ind = cfg.indicator_dir
    tn6 = list(ind.glob("*735*.tn6"))
    txt = list(ind.glob("*V5*.txt"))
    if not tn6 or not txt:
        pytest.skip("files missing")
    # pairing 735 with V5 is allowed as explicit (user choice) — just test mechanism
    map_path = tmp_path / "map.json"
    mapping, spec = import_tn6_with_source(tn6[0], txt[0], map_path, note="test pair")
    assert mapping["source_sha256"]
    assert spec.package_sha256 == file_sha256(tn6[0])
    # without map, still source_required for 735 alone
    specs = build_specs_from_indicator_dir(ind, mapping={})
    s735 = [s for s in specs if "735" in s.name][0]
    assert s735.compile_status == "source_required"
