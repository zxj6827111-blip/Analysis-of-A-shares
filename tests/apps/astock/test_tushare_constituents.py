# -*- coding: utf-8 -*-
"""Unit tests for the Tushare constituents provider (index_weight + benchmark map).

Covers the curated keyword table, normalization, member parsing, snapshot
freshness, multi-code fallback, cache format compatibility and the no-token
TDX fallback path in ``index_etf.resolve_constituents``. No network calls are
made: providers are constructed with a fake token and Tushare calls are
monkeypatched where needed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from wtpy.apps.astock.data.tushare_constituents import (
    TushareConstituentsError,
    TushareConstituentsProvider,
    _BENCHMARK_KEYWORDS,
    _keyword_match,
    _norm_name,
)
from wtpy.apps.astock.service import index_etf as ie
from wtpy.apps.astock.service import stock_names


# ---------------------------------------------------------------------------
# Curated keyword table
# ---------------------------------------------------------------------------


def test_keyword_match_curated_table_entries():
    """Every curated entry must match its own benchmark-style text."""
    for key, code in _BENCHMARK_KEYWORDS:
        # benchmark 文本以指数名开头(常见格式:名称 + 指数/收益率 ×100%)
        bench = f"{key}指数×100%"
        hit = _keyword_match(bench)
        assert hit is not None, f"curated key not matched: {key}"
        assert hit[0] == code, f"{key} -> {hit[0]}, expected {code}"


def test_keyword_match_longest_key_wins():
    """Longer keys must win over shorter ones sharing a prefix."""
    # 中证全指证券公司(7字)优先于 中证银行 之类短词
    assert _keyword_match("中证全指证券公司指数×100%")[0] == "399975.SZ"
    assert _keyword_match("中证银行指数×100%")[0] == "399986.SZ"
    # 上证科创板50成份 不被 上证综合 截胡
    assert _keyword_match("上证科创板50成份指数收益率×100%")[0] == "000688.SH"


def test_keyword_match_no_false_positive():
    assert _keyword_match("") is None
    assert _keyword_match("一年定期存款利率(税后)+1.2%") is None
    assert _keyword_match("MSCI中国A50互联互通人民币") is None


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_norm_name():
    assert _norm_name("创业板指数P×100%") == "创业板"
    assert _norm_name("沪深300指数×100%") == "沪深300"
    assert _norm_name("中证酒指数收益率×100%") == "中证酒"
    assert _norm_name("上证科创板50成份指数收益率×100%") == "上证科创板50成份"
    assert _norm_name("中证新能源汽车指数收益率×100%") == "中证新能源汽车"
    # 混合基准:比例与存款段被剥离
    assert _norm_name("活期存款利率(税后)×5%+中证小盘500指数×95%") == (
        "活期存款利率+中证小盘500"
    )


# ---------------------------------------------------------------------------
# Member parsing
# ---------------------------------------------------------------------------


def test_weight_members():
    snap = [
        {"con_code": "300750.SZ"},   # 带交易所后缀
        {"con_code": "600519.SH"},   # 带交易所后缀
        {"con_code": "000001"},      # 裸 6 位 -> SZSE
        {"con_code": "688981"},      # 科创板 -> SSE
        {"con_code": "bad"},         # 非法
        {"con_code": "12345"},       # 长度不足
        {},                          # 缺字段
    ]
    out = ie._weight_members(snap)
    assert out == [
        (0, "300750"),
        (1, "600519"),
        (0, "000001"),
        (1, "688981"),
    ]
    assert ie._weight_members(None) == []
    assert ie._weight_members([]) == []


# ---------------------------------------------------------------------------
# Snapshot freshness
# ---------------------------------------------------------------------------


def test_snapshot_fresh_boundaries():
    f = TushareConstituentsProvider._snapshot_fresh
    # 40 天以内视为新鲜(月末发布窗口)
    assert f(20260731, 20260909) is True
    # 超过 40 天视为过期,触发重新回溯
    assert f(20260731, 20260910) is False
    assert f(20260731, 20260915) is False
    # 同日 / 未来快照日期
    assert f(20260731, 20260731) is True
    # asof 为空:不判断新鲜度(保持缓存)
    assert f(20260731, None) is True
    # 非法日期格式:保守返回新鲜
    assert f("not-a-date", 20260812) is True


# ---------------------------------------------------------------------------
# Multi-code fallback (fetch_index_constituents_multi)
# ---------------------------------------------------------------------------


class _FakeProviderForMulti:
    """Stands in for TushareConstituentsProvider: first candidate has no
    index_weight data, second one does (创业板 395004 vs 399006 scenario)."""

    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)
        self._calls = []

    def fetch_index_constituents(self, code, *, asof=None, cache_dir=None):
        self._calls.append(code)
        if code == "395004.SZ":
            raise TushareConstituentsError("index_weight 无数据: 395004.SZ")
        return 20260731, [{"con_code": f"{code[:2]}0001.SZ"}]


def test_fetch_index_constituents_multi_falls_back(monkeypatch, tmp_path):
    fake = _FakeProviderForMulti(tmp_path)
    monkeypatch.setattr(
        TushareConstituentsProvider,
        "fetch_index_constituents",
        fake.fetch_index_constituents,
    )
    prov = TushareConstituentsProvider(
        token="fake-token", cache_dir=tmp_path
    )
    code, date, snap = prov.fetch_index_constituents_multi(
        ["395004.SZ", "399006.SZ"]
    )
    assert code == "399006.SZ"
    assert date == 20260731
    assert snap and snap[0]["con_code"].endswith(".SZ")
    assert fake._calls == ["395004.SZ", "399006.SZ"]


def test_fetch_index_constituents_multi_all_fail(monkeypatch, tmp_path):
    fake = _FakeProviderForMulti(tmp_path)
    monkeypatch.setattr(
        TushareConstituentsProvider,
        "fetch_index_constituents",
        fake.fetch_index_constituents,
    )
    prov = TushareConstituentsProvider(
        token="fake-token", cache_dir=tmp_path
    )
    with pytest.raises(TushareConstituentsError):
        prov.fetch_index_constituents_multi(["395004.SZ"])
    # 空候选列表直接失败,不崩溃
    with pytest.raises(TushareConstituentsError):
        prov.fetch_index_constituents_multi([])


# ---------------------------------------------------------------------------
# Cache format compatibility (old [code, key] vs new [key, [codes]])
# ---------------------------------------------------------------------------


def _write_track_map_cache(cache_dir, data):
    p = Path(cache_dir) / "constituents_etf_track_map.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_track_map_cache_old_format(tmp_path):
    """旧格式 {etf: [code, key]} 必须被兼容读取为 (key, [code])。"""
    _write_track_map_cache(
        tmp_path,
        {"510300.SH": ["000300.SH", "沪深300"]},
    )
    prov = TushareConstituentsProvider(token="fake-token", cache_dir=tmp_path)
    m = prov.fetch_etf_track_map()
    assert m["510300.SH"] == ("沪深300", ["000300.SH"])


def test_track_map_cache_new_format(tmp_path):
    """新格式 {etf: [key, [codes]]} 正常读取。"""
    _write_track_map_cache(
        tmp_path,
        {"510300.SH": ["沪深300", ["000300.SH", "399300.SZ"]]},
    )
    prov = TushareConstituentsProvider(token="fake-token", cache_dir=tmp_path)
    m = prov.fetch_etf_track_map()
    assert m["510300.SH"] == ("沪深300", ["000300.SH", "399300.SZ"])


def test_should_force_refresh(tmp_path):
    prov = TushareConstituentsProvider(token="fake-token", cache_dir=tmp_path)
    # 无缓存文件 -> 允许强制刷新
    assert prov.should_force_refresh() is True
    # 刚写入的缓存 -> 当天不重复刷新(避免每个未映射 ETF 都全量拉 fund_basic)
    _write_track_map_cache(tmp_path, {"A.SH": ["x", ["000300.SH"]]})
    assert prov.should_force_refresh() is False


# ---------------------------------------------------------------------------
# index_etf.resolve_constituents integration (no token -> TDX fallback)
# ---------------------------------------------------------------------------


def _write_fake_tdx(tmp_path, spec_lines=None, block_lines=None):
    hq = tmp_path / "T0002" / "hq_cache"
    hq.mkdir(parents=True, exist_ok=True)
    if spec_lines is not None:
        (hq / "specetfdata.txt").write_text("\n".join(spec_lines), encoding="gbk")
    if block_lines is not None:
        (hq / "infoharbor_block.dat").write_text("\n".join(block_lines), encoding="gbk")
    return _make_cfg(tdx_root=tmp_path)


def _make_cfg(**overrides):
    base = dict(
        bagua_json=Path("."),
        storage_root=Path("."),
        market_data_root=Path("."),
        forecast_root=Path("."),
        forecast_weekly_dir=Path("."),
        universe_path=Path("."),
        adj_root=Path("."),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_stock_names(cfg, disp, std_code=""):
    return {"sh600000": "浦发银行", "sz000001": "平安银行"}.get(disp, "")


def test_resolve_index_constituents_no_token_tdx_fallback(tmp_path, monkeypatch):
    """无 TUSHARE_TOKEN 时指数成分走 TDX 兜底,并携带 tushare_error。"""
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setattr(stock_names, "resolve_stock_name", _fake_stock_names)
    cfg = _write_fake_tdx(
        tmp_path,
        block_lines=[
            "#ZS_沪深300,300,000300,20050408,20260512,,",
            "0#000001,1#600000,1#600519",
        ],
    )
    out = ie.resolve_constituents(cfg, "SSE.IDX.000300")
    assert out["ok"] is True
    assert out["count"] == 3
    assert out["source"] == "tdx_infoharbor_block:ZS_沪深300"
    # Tushare 失败原因保留在独立字段,不污染 note(与 TDX 路径成功一致)
    assert out["note"] == ""
    assert "TUSHARE_TOKEN" in (out["tushare_error"] or "")


def test_resolve_etf_constituents_no_token_tdx_fallback(tmp_path, monkeypatch):
    """无 TUSHARE_TOKEN 时 ETF 成分走 specetfdata + 板块文件兜底。"""
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setattr(stock_names, "resolve_stock_name", _fake_stock_names)
    cfg = _write_fake_tdx(
        tmp_path,
        spec_lines=["1,510300,000300,1,800300,20260512,"],
        block_lines=[
            "#ZS_沪深300,300,000300,20050408,20260512,,",
            "0#000001,1#600000",
        ],
    )
    out = ie.resolve_constituents(cfg, "SSE.ETF.510300")
    assert out["ok"] is True
    assert out["tracked_index"] == "000300"
    assert out["count"] == 2
    assert "tushare:fund_basic" not in out["source"]
    assert "TUSHARE_TOKEN" in (out["tushare_error"] or "")


# ---------------------------------------------------------------------------
# resolve_constituents Tushare success path (fake provider, no network)
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, *, token=None, cache_dir=None):
        pass

    def fetch_etf_track_map(self, force=False):
        return {
            "510300.SH": ("沪深300", ["000300.SH"]),
            "159915.SZ": ("创业板", ["395004.SZ", "399006.SZ"]),
        }

    def fetch_index_constituents(self, index_code, *, asof=None, cache_dir=None):
        assert index_code in ("000300.SH", "399006.SZ")
        return 20260731, [
            {"con_code": "600519.SH"},
            {"con_code": "300750.SZ"},
        ]

    def fetch_index_constituents_multi(self, index_codes, *, asof=None, cache_dir=None):
        for code in index_codes:
            if code in ("000300.SH", "399006.SZ"):
                return code, 20260731, [
                    {"con_code": "600519.SH"},
                    {"con_code": "300750.SZ"},
                ]
        raise TushareConstituentsError(f"no data: {index_codes}")

    def should_force_refresh(self):
        return False


def test_resolve_constituents_tushare_success(monkeypatch):
    """Tushare 路径成功:返回成分、source 标注 tushare 数据源、无 TDX 依赖。"""
    monkeypatch.setattr(ie, "_constituents_provider", lambda cfg: _FakeProvider())
    monkeypatch.setattr(stock_names, "resolve_stock_name", _fake_stock_names)
    cfg = _make_cfg()

    out = ie.resolve_constituents(cfg, "SSE.ETF.510300")
    assert out["ok"] is True
    assert out["tracked_index"] == "000300"
    assert out["tracked_index_name"] == "沪深300"
    assert out["count"] == 2
    assert out["source"].startswith("tushare:fund_basic.benchmark")
    assert "index_weight@20260731" in out["source"]
    assert out["tushare_error"] is None

    out = ie.resolve_constituents(cfg, "SZSE.ETF.159915")
    assert out["tracked_index"] == "399006"
    assert out["count"] == 2


def test_resolve_index_constituents_tushare_success(monkeypatch):
    monkeypatch.setattr(ie, "_constituents_provider", lambda cfg: _FakeProvider())
    monkeypatch.setattr(stock_names, "resolve_stock_name", _fake_stock_names)
    cfg = _make_cfg()

    out = ie.resolve_constituents(cfg, "SSE.IDX.000300", limit=1)
    assert out["count"] == 2  # limit 只截断展示
    assert out["source"] == "tushare:index_weight@20260731"
    assert len(out["constituents"]) == 1
