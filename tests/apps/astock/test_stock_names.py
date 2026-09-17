# -*- coding: utf-8 -*-
"""名称解析（stock_names）单测：Tushare 元数据缓存兜底 + 展示名补齐。

重点覆盖 Tushare-only 部署（无通达信 infoharbor、无 universe.json、无周报
快照）——线上跟踪页名称列整列「—」正是这条路径缺失导致的。测试一律自建
名称源并显式关闭本机 TDX：默认 ``tdx_root`` 指向本机 ``D:\\通达信``，不隔离
的话断言会随跑测试的机器变化（CI 与本地结论不一致）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.service import stock_names as sn


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir(parents=True)
    ind.mkdir(parents=True)
    c = get_default_config(
        storage_root=storage, indicator_dir=ind, output_root=tmp_path / "out"
    )
    monkeypatch.setattr(c, "tdx_root", None)
    monkeypatch.setattr(c, "forecast_root", storage / "forecast")
    # 进程级名称缓存清零，避免其它用例的加载结果串味
    monkeypatch.setattr(sn, "_cache", {})
    monkeypatch.setattr(sn, "_loaded_for", None)
    return c


def _write_symbol_meta(storage: Path, *, stock_names, etf_names=None) -> None:
    (storage / "rizhu_list_dates.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "fetched_at": "2026-09-12",
                "stocks": {},
                "etfs": {},
                "stock_names": stock_names,
                "etf_names": etf_names or {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_universe(storage: Path, symbols) -> None:
    (storage / "universe.json").write_text(
        json.dumps({"symbols": symbols}, ensure_ascii=False), encoding="utf-8"
    )


class TestTushareCacheFallback:
    def test_cache_is_the_only_source_without_local_imports(self, cfg):
        """无 TDX / universe / 周报快照时，Tushare 元数据缓存是唯一名称源。"""
        _write_symbol_meta(
            Path(cfg.storage_root),
            stock_names={"000001": "平安银行", "600033": "福建高速"},
        )
        assert sn.resolve_stock_name(cfg, "000001") == "平安银行"
        assert sn.resolve_stock_name(cfg, "SSE.STK.600033") == "福建高速"

    def test_missing_code_stays_empty(self, cfg):
        """缓存里没有的代码 → 空串（不拿代码冒充名称）。"""
        _write_symbol_meta(Path(cfg.storage_root), stock_names={"000001": "平安银行"})
        assert sn.resolve_stock_name(cfg, "300750") == ""

    def test_local_sources_win_over_tushare_cache(self, cfg):
        """本地导入产物优先：缓存**只补缺口**，不覆盖 universe/TDX/周报的名称。"""
        storage = Path(cfg.storage_root)
        _write_symbol_meta(storage, stock_names={"000001": "Tushare名", "000002": "仅缓存"})
        _write_universe(storage, [{"code": "000001", "name": "本地名"}])
        assert sn.resolve_stock_name(cfg, "000001") == "本地名"
        # 本地没有的代码仍由缓存兜底
        assert sn.resolve_stock_name(cfg, "000002") == "仅缓存"

    def test_etf_names_used_when_stock_name_absent(self, cfg):
        """ETF 代码走 etf_names；同名冲突时股票口径优先。"""
        _write_symbol_meta(
            Path(cfg.storage_root),
            stock_names={"000001": "平安银行"},
            etf_names={"510300": "沪深300ETF", "000001": "指数占位"},
        )
        assert sn.resolve_stock_name(cfg, "510300") == "沪深300ETF"
        assert sn.resolve_stock_name(cfg, "000001") == "平安银行"

    def test_cache_file_change_invalidates_name_cache(self, cfg):
        """缓存文件内容变化后重新加载（指纹把它算进去，不吃旧缓存）。"""
        storage = Path(cfg.storage_root)
        _write_symbol_meta(storage, stock_names={"000001": "平安银行"})
        assert sn.resolve_stock_name(cfg, "000001") == "平安银行"
        _write_symbol_meta(storage, stock_names={"000001": "平安银行", "000002": "万科A"})
        assert sn.resolve_stock_name(cfg, "000002") == "万科A"

    def test_broken_cache_file_is_tolerated(self, cfg):
        """缓存文件损坏 → 返回空名（读取路径不因脏文件报错）。"""
        (Path(cfg.storage_root) / "rizhu_list_dates.json").write_text(
            "{ not json", encoding="utf-8"
        )
        assert sn.resolve_stock_name(cfg, "000001") == ""


class TestFillMissingNames:
    def test_fills_only_empty_rows(self, cfg):
        """补齐只动空 name 的行，已有名字（可能与当前源不同）不被改写。"""
        _write_symbol_meta(Path(cfg.storage_root), stock_names={"000001": "平安银行"})
        rows = [
            {"code": "SZSE.000001.SZ", "name": ""},          # 空串待补
            {"code": "SZSE.000001.SZ"},                       # 无字段待补
            {"code": "SZSE.000009.SZ", "name": "历史名"},      # 已有名 → 不动
        ]
        assert sn.fill_missing_names(cfg, rows) == 2
        assert rows[0]["name"] == "平安银行"
        assert rows[1]["name"] == "平安银行"
        assert rows[2]["name"] == "历史名"

    def test_no_source_keeps_rows_untouched(self, cfg):
        """没有任何名称源 → 一行不补，返回 0（缺名如实留空）。"""
        rows = [{"code": "SZSE.000001.SZ", "name": ""}]
        assert sn.fill_missing_names(cfg, rows) == 0
        assert rows[0]["name"] == ""

    def test_code_without_name_is_not_fabricated(self, cfg):
        """解析不到就保持原样：绝不把代码写进名称列。"""
        _write_symbol_meta(Path(cfg.storage_root), stock_names={"000001": "平安银行"})
        rows = [{"code": "SZSE.000009.SZ", "name": ""}]
        assert sn.fill_missing_names(cfg, rows) == 0
        assert rows[0]["name"] == ""
