"""CLI pair-735 default glob resolution (no hardcoded Chinese names)."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

import json
from pathlib import Path

import pytest

from wtpy.apps.astock.cli import main
from wtpy.apps.astock.indicators.tn6_importer import file_sha256


def _write_minimal_tn6(path: Path) -> None:
    path.write_bytes(b"TN6_TEST_PACKAGE_FOR_TEST_ONLY_" + b"\x00" * 64)


def _write_formula(path: Path) -> None:
    path.write_text(
        "MA7:=MA(C,7);\nMA35:=MA(C,35);\nXG:CROSS(MA7,MA35);\n",
        encoding="utf-8",
    )


def test_pair_735_cli_unique_glob(tmp_path):
    ind = tmp_path / "ind"
    ind.mkdir()
    storage = tmp_path / "storage"
    tn6 = ind / "demo735_pkg.tn6"
    txt = ind / "demo735_src.txt"
    _write_minimal_tn6(tn6)
    _write_formula(txt)
    map_path = storage / "indicators" / "tn6_source_map.json"
    rc = main(
        [
            "--storage",
            str(storage),
            "--indicator-dir",
            str(ind),
            "pair-735",
        ]
    )
    assert rc == 0
    assert map_path.exists()
    data = json.loads(map_path.read_text(encoding="utf-8"))
    assert file_sha256(tn6) in data
    entry = data[file_sha256(tn6)]
    assert entry["source_sha256"] == file_sha256(txt)
    assert Path(entry["source_file"]).resolve() == txt.resolve()


def test_pair_735_cli_multiple_txt_rejects(tmp_path):
    ind = tmp_path / "ind"
    ind.mkdir()
    storage = tmp_path / "storage"
    _write_minimal_tn6(ind / "a735.tn6")
    _write_formula(ind / "a735_a.txt")
    _write_formula(ind / "a735_b.txt")
    rc = main(["--storage", str(storage), "--indicator-dir", str(ind), "pair-735"])
    assert rc == 1


def test_pair_735_cli_missing_txt_rejects(tmp_path):
    ind = tmp_path / "ind"
    ind.mkdir()
    storage = tmp_path / "storage"
    _write_minimal_tn6(ind / "only735.tn6")
    rc = main(["--storage", str(storage), "--indicator-dir", str(ind), "pair-735"])
    assert rc == 1


def test_cli_rejects_illegal_stop_loss():
    with pytest.raises(SystemExit) as ei:
        main(["backtest", "--indicator", "x", "--stop-loss", "-0.03"])
    assert ei.value.code != 0


def test_cli_rejects_stop_loss_zero_and_one_and_tp_over():
    for flag, bad in (("--stop-loss", "0"), ("--stop-loss", "1"), ("--take-profit", "1.5")):
        with pytest.raises(SystemExit) as ei:
            main(["backtest", "--indicator", "x", flag, bad])
        assert ei.value.code != 0
