# -*- coding: utf-8 -*-
"""Forward returns of a run's picks (信号周涨幅).

Covers the shipped service path: signals.csv parsing / de-duplication, the
close-to-close forward window, pending + no-bar accounting, cache reuse and
invalidation, and the API route's error mapping.
"""

from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.service.signal_returns import (
    compute_signal_forward_returns,
    read_signal_picks,
)

_SIGNAL_FIELDS = [
    "std_code", "date", "period", "indicator_id", "value", "is_dwm",
    "bagua_full_name", "bagua_yao_name", "bagua_gua_order", "bagua_judgement",
    "bagua_core_gang", "bagua_state_id", "bagua_action_signal", "bagua_biangua",
    "bagua_rule_version",
]

# 7 trading days: 0102 0103 0104 0105 0108 0109 0110
_DATES = [20240102, 20240103, 20240104, 20240105, 20240108, 20240109, 20240110]


@pytest.fixture()
def cfg(tmp_path: Path) -> AStockConfig:
    c = AStockConfig()
    c.output_root = tmp_path / "outputs"
    c.output_root.mkdir(parents=True, exist_ok=True)
    # 缓存写在 storage_root 下：必须隔离到 tmp，不能落到仓库真实 storage 里
    c.storage_root = tmp_path / "storage"
    return c


def _cache_file(cfg: AStockConfig, run_id: str, horizon: int) -> Path:
    return (Path(cfg.storage_root) / "cache" / "signal_returns" / run_id
            / f"signal_returns_h{horizon}_tushare_qfq.json")


def _bars(closes) -> list:
    assert len(closes) <= len(_DATES)
    return [
        DayBar(date=_DATES[i], open=c, high=c, low=c, close=c, amount=0.0, volume=0.0)
        for i, c in enumerate(closes)
    ]


def _write_signals(run_dir: Path, rows) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "signals.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=_SIGNAL_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in _SIGNAL_FIELDS})


def _loader(mapping, calls=None):
    def _load(code):
        if calls is not None:
            calls.append(code)
        if code not in mapping:
            raise FileNotFoundError(code)
        return mapping[code], {"dataset_id": "ds_test", "dataset_adjustment": "qfq"}

    return _load


def test_forward_returns_and_summary(cfg: AStockConfig):
    """After 5 trading days: 10 -> 15 (+50%), 11 -> 16 (+45.45%)."""
    run_dir = Path(cfg.output_root) / "bt_sr_1"
    _write_signals(run_dir, [
        {"std_code": "600000.SH", "date": 20240102, "period": "DAY", "indicator_id": "rule_a"},
        {"std_code": "000001.SZ", "date": 20240103, "period": "DAY", "indicator_id": "rule_b"},
    ])
    bars = _bars([10, 11, 12, 13, 14, 15, 16])
    out = compute_signal_forward_returns(
        cfg, "bt_sr_1", horizon=5, with_names=False,
        bar_loader=_loader({"600000.SH": bars, "000001.SZ": bars}),
    )

    rows = {r["std_code"]: r for r in out["rows"]}
    assert rows["600000.SH"]["status"] == "ok"
    assert rows["600000.SH"]["entry_close"] == 10.0
    assert rows["600000.SH"]["exit_close"] == 15.0
    assert rows["600000.SH"]["exit_date"] == 20240109
    assert rows["600000.SH"]["ret"] == pytest.approx(0.5)

    assert rows["000001.SZ"]["entry_close"] == 11.0
    assert rows["000001.SZ"]["exit_close"] == 16.0
    assert rows["000001.SZ"]["ret"] == pytest.approx(16 / 11 - 1)

    s = out["summary"]
    assert s["n_total"] == 2 and s["n_ok"] == 2
    assert s["n_pending"] == 0 and s["n_no_bar"] == 0
    assert s["mean"] == pytest.approx((0.5 + 16 / 11 - 1) / 2)
    assert s["median"] == pytest.approx((0.5 + 16 / 11 - 1) / 2)
    assert s["win_rate"] == 1.0
    assert s["max"] == pytest.approx(0.5)
    assert out["horizon"] == 5 and out["plane"] == "tushare_qfq"
    assert out["plane_meta"]["dataset_id"] == "ds_test"


def test_pending_and_no_bar_excluded_from_stats(cfg: AStockConfig):
    """Too few forward bars -> pending; signal date absent from bars -> no_bar."""
    run_dir = Path(cfg.output_root) / "bt_sr_2"
    _write_signals(run_dir, [
        {"std_code": "600000.SH", "date": 20240102, "period": "DAY", "indicator_id": "r"},
        {"std_code": "600000.SH", "date": 20240109, "period": "DAY", "indicator_id": "r"},
        {"std_code": "600000.SH", "date": 20231229, "period": "DAY", "indicator_id": "r"},
    ])
    bars = _bars([10, 11, 12, 13, 14, 15, 16])
    out = compute_signal_forward_returns(
        cfg, "bt_sr_2", horizon=5, with_names=False,
        bar_loader=_loader({"600000.SH": bars}),
    )

    by_date = {r["signal_date"]: r for r in out["rows"]}
    assert by_date[20240102]["status"] == "ok"
    assert by_date[20240109]["status"] == "pending"
    assert by_date[20240109]["ret"] is None
    assert by_date[20240109]["entry_close"] == 15.0
    assert by_date[20231229]["status"] == "no_bar"
    assert by_date[20231229]["entry_close"] is None

    s = out["summary"]
    assert s["n_total"] == 3 and s["n_ok"] == 1
    assert s["n_pending"] == 1 and s["n_no_bar"] == 1
    assert s["mean"] == pytest.approx(0.5)


def test_same_code_date_multiple_rules_merged(cfg: AStockConfig):
    """One pick per (code, date): the same stock picked twice is not double-counted."""
    run_dir = Path(cfg.output_root) / "bt_sr_3"
    _write_signals(run_dir, [
        {"std_code": "600000.SH", "date": 20240102, "indicator_id": "rule_a"},
        {"std_code": "600000.SH", "date": 20240102, "indicator_id": "rule_b"},
        {"std_code": "600000.SH", "date": 20240103, "indicator_id": "rule_a"},
    ])
    picks = read_signal_picks(run_dir)
    assert len(picks) == 2
    assert picks[0]["indicator_ids"] == ["rule_a", "rule_b"]

    out = compute_signal_forward_returns(
        cfg, "bt_sr_3", horizon=5, with_names=False,
        bar_loader=_loader({"600000.SH": _bars([10, 11, 12, 13, 14, 15, 16])}),
    )
    assert out["summary"]["n_total"] == 2


def test_cache_reused_then_invalidated(cfg: AStockConfig):
    """Second call serves the cached payload; a changed signals.csv recomputes."""
    run_dir = Path(cfg.output_root) / "bt_sr_4"
    _write_signals(run_dir, [
        {"std_code": "600000.SH", "date": 20240102, "indicator_id": "r"},
    ])
    bars = _bars([10, 11, 12, 13, 14, 15, 16])
    load = _loader({"600000.SH": bars})

    first = compute_signal_forward_returns(
        cfg, "bt_sr_4", horizon=5, with_names=False, bar_loader=load
    )
    assert first.get("reused") is None
    cache_p = _cache_file(cfg, "bt_sr_4", 5)
    assert cache_p.exists()
    # 缓存不得写进 run 目录：历史 run 的产物清单是不可变证据
    assert not (run_dir / "signal_returns_h5_tushare_qfq.json").exists()

    second = compute_signal_forward_returns(
        cfg, "bt_sr_4", horizon=5, with_names=False, bar_loader=load
    )
    assert second.get("reused") is True
    assert second["summary"]["n_ok"] == 1

    # signals.csv grows -> fingerprint mismatch -> recompute, not a stale hit
    _write_signals(run_dir, [
        {"std_code": "600000.SH", "date": 20240102, "indicator_id": "r"},
        {"std_code": "600000.SH", "date": 20240103, "indicator_id": "r"},
    ])
    third = compute_signal_forward_returns(
        cfg, "bt_sr_4", horizon=5, with_names=False, bar_loader=load
    )
    assert third.get("reused") is None
    assert third["summary"]["n_total"] == 2


def test_different_horizon_uses_separate_cache(cfg: AStockConfig):
    run_dir = Path(cfg.output_root) / "bt_sr_5"
    _write_signals(run_dir, [{"std_code": "600000.SH", "date": 20240102, "indicator_id": "r"}])
    load = _loader({"600000.SH": _bars([10, 11, 12, 13, 14, 15, 16])})

    compute_signal_forward_returns(cfg, "bt_sr_5", horizon=5, with_names=False, bar_loader=load)
    out3 = compute_signal_forward_returns(
        cfg, "bt_sr_5", horizon=3, with_names=False, bar_loader=load
    )
    # 0102 -> idx3 (20240105, close 13)
    assert out3["rows"][0]["exit_close"] == 13.0
    assert out3.get("reused") is None
    assert _cache_file(cfg, "bt_sr_5", 3).exists()
    assert _cache_file(cfg, "bt_sr_5", 5).exists()


def test_missing_run_and_missing_signals_raise(cfg: AStockConfig):
    with pytest.raises(FileNotFoundError):
        compute_signal_forward_returns(cfg, "nope", with_names=False, bar_loader=_loader({}))

    run_dir = Path(cfg.output_root) / "bt_sr_6"
    run_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(FileNotFoundError):
        compute_signal_forward_returns(cfg, "bt_sr_6", with_names=False, bar_loader=_loader({}))


def test_no_data_when_symbol_has_no_bars(cfg: AStockConfig):
    """A symbol with no bars at all (delisted/unavailable) is not the same as a
    suspension on the signal date, and one bad symbol must not fail the table."""
    run_dir = Path(cfg.output_root) / "bt_sr_8"
    _write_signals(run_dir, [
        {"std_code": "600000.SH", "date": 20240102, "indicator_id": "r"},
        {"std_code": "600001.SH", "date": 20240102, "indicator_id": "r"},
    ])
    out = compute_signal_forward_returns(
        cfg, "bt_sr_8", horizon=5, with_names=False,
        bar_loader=_loader({"600000.SH": _bars([10, 11, 12, 13, 14, 15, 16])}),
    )
    rows = {r["std_code"]: r for r in out["rows"]}
    assert rows["600000.SH"]["status"] == "ok"
    assert rows["600001.SH"]["status"] == "no_data"
    assert out["summary"]["n_ok"] == 1 and out["summary"]["n_no_data"] == 1


def test_api_route_error_mapping(cfg: AStockConfig):
    from fastapi import HTTPException

    from wtpy.apps.astock.api_routes.backtests import api_run_signal_returns

    ctx = SimpleNamespace(cfg=cfg)
    with pytest.raises(HTTPException) as missing:
        api_run_signal_returns("nope", horizon=5, plane="tushare_qfq", force=False, ctx=ctx)
    assert missing.value.status_code == 404

    run_dir = Path(cfg.output_root) / "bt_sr_7"
    _write_signals(run_dir, [{"std_code": "600000.SH", "date": 20240102, "indicator_id": "r"}])
    with pytest.raises(HTTPException) as bad_plane:
        api_run_signal_returns("bt_sr_7", horizon=5, plane="tdx_front", force=False, ctx=ctx)
    assert bad_plane.value.status_code == 400

    with pytest.raises(HTTPException) as bad_horizon:
        api_run_signal_returns("bt_sr_7", horizon=0, plane="tushare_qfq", force=False, ctx=ctx)
    assert bad_horizon.value.status_code == 400
