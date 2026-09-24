# -*- coding: utf-8 -*-
"""overlay_v1 现役名单快照（eod_universe_latest.json）→ 池解析优先。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.sync_market_data as smd
from wtpy.apps.astock.service import backtest_universe as bu


def _mk_root(tmp_path) -> Path:
    root = tmp_path / "md"
    root.mkdir()
    return root


class _CfgCfg(SimpleNamespace):
    pass


def _cfg(root: Path) -> SimpleNamespace:
    return SimpleNamespace(market_data_root=root, universe_path=root / "nope.json")


def test_write_snapshot_filters_non_stk(tmp_path):
    root = _mk_root(tmp_path)
    store = SimpleNamespace(root=root)
    smd._write_eod_universe_snapshot(
        store, ["SSE.STK.600000", "BSE.STK.920001", "SSE.IDX.000001", "x"]
    )
    raw = json.loads((root / "eod_universe_latest.json").read_text(encoding="utf-8"))
    assert raw["count"] == 2  # 只留股票（指数/垃圾不入池）
    assert set(raw["symbols"]) == {"SSE.STK.600000", "BSE.STK.920001"}


def test_overlay_eod_universe_requires_overlay_enabled(tmp_path, monkeypatch):
    root = _mk_root(tmp_path)
    cfg = _cfg(root)
    assert bu._overlay_eod_universe(cfg) == []  # 非 overlay 仓库 → 空


def _enable_overlay(monkeypatch):
    """pretend overlay enabled（不建真 delta_store）"""
    from wtpy.apps.astock.data import delta_store as ds_mod

    monkeypatch.setattr(
        ds_mod,
        "load_overlay_state",
        lambda _root: SimpleNamespace(enabled=True, delta_store_id="t"),
    )


def test_overlay_eod_universe_fresh_and_fresh_gate(tmp_path, monkeypatch):
    root = _mk_root(tmp_path)
    _enable_overlay(monkeypatch)
    cfg = _cfg(root)
    syms = [f"SSE.STK.{600000 + i}" for i in range(1500)]
    smd._write_eod_universe_snapshot(SimpleNamespace(root=root), syms)
    got = bu._overlay_eod_universe(cfg)
    assert len(got) == 1500

    # 过期（伪造 30 天前）→ 回退空（调用方走原路径）
    p = root / "eod_universe_latest.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["fetched_at"] = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 30 * 86400)
    )
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    assert bu._overlay_eod_universe(cfg) == []


def test_overlay_eod_universe_rejects_fixture_sized_pool(tmp_path, monkeypatch):
    root = _mk_root(tmp_path)
    _enable_overlay(monkeypatch)
    cfg = _cfg(root)
    smd._write_eod_universe_snapshot(
        SimpleNamespace(root=root), ["SSE.STK.600000", "SSE.STK.600004"]
    )
    assert bu._overlay_eod_universe(cfg) == []


def test_select_universe_prefers_overlay_live_list(tmp_path, monkeypatch):
    """overlay 开 + 快照在：宇宙选择以快照为准（含北交所），universe.json 退兜底。"""
    from wtpy.apps.astock.data.universe import AShareUniverse, SymbolInfo

    root = _mk_root(tmp_path)
    _enable_overlay(monkeypatch)
    # legacy universe.json 只有沪深 2 只（TDX 时代遗留）
    uni = AShareUniverse(
        [
            SymbolInfo(raw="600000", std_code="SSE.STK.600000", exchange="SSE", code="600000"),
            SymbolInfo(raw="000001", std_code="SZSE.STK.000001", exchange="SZSE", code="000001"),
        ]
    )
    uni_path = root / "universe.json"
    uni.save(uni_path)
    live = ["SSE.STK.600000"] * 1 + [
        f"BSE.STK.{920000 + i}" for i in range(1500)
    ]
    smd._write_eod_universe_snapshot(SimpleNamespace(root=root), live)
    cfg = SimpleNamespace(market_data_root=root, universe_path=uni_path)
    got = bu.select_universe(cfg, None)
    assert "BSE.STK.920005" in got
    assert len(got) == 1501
