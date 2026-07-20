"""735 formula provenance confirmation gates."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

import json
from pathlib import Path

import pytest

from wtpy.apps.astock.cli import main
from wtpy.apps.astock.indicators.tn6_importer import (
    confirm_source_pair,
    import_tn6_with_source,
    load_source_map,
    resolve_formula_audit,
)


def _pkg(ind: Path):
    tn6 = ind / "x735.tn6"
    txt = ind / "x735.txt"
    tn6.write_bytes(b"TN6_FAKE_" + b"\x00" * 32)
    txt.write_text("MA7:=MA(C,7);\nXG:C>MA7;\n", encoding="utf-8")
    return tn6, txt


def test_pair_defaults_unconfirmed(tmp_path):
    ind = tmp_path / "ind"
    ind.mkdir()
    storage = tmp_path / "storage"
    tn6, txt = _pkg(ind)
    map_path = storage / "indicators" / "tn6_source_map.json"
    mapping, spec = import_tn6_with_source(tn6, txt, map_path, note="test")
    assert mapping["formula_provenance"] == "user_confirmation_required"
    assert mapping["source_pair_status"] == "paired_unconfirmed"
    assert mapping["formal_backtest_allowed"] is False
    audit = resolve_formula_audit(mapping, package_sha256=mapping["package_sha256"])
    assert audit["formal_backtest_allowed"] is False
    assert audit["research_backtest_allowed"] is True


def test_confirm_then_formal_allowed(tmp_path):
    ind = tmp_path / "ind"
    ind.mkdir()
    storage = tmp_path / "storage"
    tn6, txt = _pkg(ind)
    map_path = storage / "indicators" / "tn6_source_map.json"
    mapping, _ = import_tn6_with_source(tn6, txt, map_path)
    entry = confirm_source_pair(
        map_path, mapping["package_sha256"], confirmed_by="tester", note="unit"
    )
    audit = resolve_formula_audit(entry, package_sha256=mapping["package_sha256"])
    assert audit["formula_provenance"] == "user_provided_human_formula"
    assert audit["formal_backtest_allowed"] is True


def test_sha_change_invalidates_confirmation(tmp_path):
    ind = tmp_path / "ind"
    ind.mkdir()
    storage = tmp_path / "storage"
    tn6, txt = _pkg(ind)
    map_path = storage / "indicators" / "tn6_source_map.json"
    mapping, _ = import_tn6_with_source(tn6, txt, map_path)
    confirm_source_pair(map_path, mapping["package_sha256"], confirmed_by="tester")
    txt.write_text("MA7:=MA(C,7);\nXG:C<MA7;\n", encoding="utf-8")
    entry = load_source_map(map_path)[mapping["package_sha256"]]
    audit = resolve_formula_audit(entry, package_sha256=mapping["package_sha256"])
    assert audit["formal_backtest_allowed"] is False


def test_cli_pair_writes_unconfirmed(tmp_path):
    ind = tmp_path / "ind"
    ind.mkdir()
    storage = tmp_path / "storage"
    _pkg(ind)
    rc = main(["--storage", str(storage), "--indicator-dir", str(ind), "pair-735"])
    assert rc == 0
    data = json.loads(
        (storage / "indicators" / "tn6_source_map.json").read_text(encoding="utf-8")
    )
    ent = next(iter(data.values()))
    assert ent.get("formula_provenance") == "user_confirmation_required"
    assert ent.get("formal_backtest_allowed") is False


def test_resolve_audit_unconfirmed_blocks_formal_flag():
    audit = resolve_formula_audit(
        {
            "package_sha256": "abc",
            "source_sha256": "def",
            "source_file": __file__,
            "formula_provenance": "user_confirmation_required",
            "source_pair_status": "paired_unconfirmed",
            "formal_backtest_allowed": False,
            "research_backtest_allowed": True,
            "confirmation": None,
        },
        package_sha256="abc",
    )
    assert audit["formal_backtest_allowed"] is False
    assert audit["research_backtest_allowed"] is True


def test_missing_source_blocks_formal(tmp_path):
    ind = tmp_path / "ind"
    ind.mkdir()
    storage = tmp_path / "storage"
    tn6, txt = _pkg(ind)
    map_path = storage / "indicators" / "tn6_source_map.json"
    mapping, _ = import_tn6_with_source(tn6, txt, map_path)
    confirm_source_pair(map_path, mapping["package_sha256"], confirmed_by="tester")
    txt.unlink()
    entry = load_source_map(map_path)[mapping["package_sha256"]]
    audit = resolve_formula_audit(entry, package_sha256=mapping["package_sha256"])
    assert audit["formal_backtest_allowed"] is False
    assert audit["source_pair_status"] == "source_missing"


def test_missing_package_blocks_formal(tmp_path):
    ind = tmp_path / "ind"
    ind.mkdir()
    storage = tmp_path / "storage"
    tn6, txt = _pkg(ind)
    map_path = storage / "indicators" / "tn6_source_map.json"
    mapping, _ = import_tn6_with_source(tn6, txt, map_path)
    confirm_source_pair(map_path, mapping["package_sha256"], confirmed_by="tester")
    tn6.unlink()
    entry = load_source_map(map_path)[mapping["package_sha256"]]
    audit = resolve_formula_audit(entry, package_sha256=mapping["package_sha256"])
    assert audit["formal_backtest_allowed"] is False
    assert audit["source_pair_status"] == "package_missing"


def test_confirmation_missing_hashes_blocks_formal(tmp_path):
    ind = tmp_path / "ind"
    ind.mkdir()
    storage = tmp_path / "storage"
    tn6, txt = _pkg(ind)
    map_path = storage / "indicators" / "tn6_source_map.json"
    mapping, _ = import_tn6_with_source(tn6, txt, map_path)
    confirm_source_pair(map_path, mapping["package_sha256"], confirmed_by="tester")
    entry = load_source_map(map_path)[mapping["package_sha256"]]
    entry["confirmation"]["package_sha256"] = None
    entry["confirmation"]["source_sha256"] = None
    audit = resolve_formula_audit(entry, package_sha256=mapping["package_sha256"])
    assert audit["formal_backtest_allowed"] is False
