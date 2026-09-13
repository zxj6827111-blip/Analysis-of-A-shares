# -*- coding: utf-8 -*-
"""通达信原生兼容改造配套环节测试（GPT6 两轮复核的反例全覆盖）。

覆盖：混合规则名称加载（any 触发）、过期刷新失败不用旧名、缓存错误
切片+回放+旧缓存重算、复核指纹校验（规则/股票池/名称快照）、导出
stale_rules、combine=all 失败规则兜底、保存回读。
全部 CI-safe（tmp_path 沙箱 + monkeypatch，不触网）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.service import indicator_review as ir
from wtpy.apps.astock.service.backtest import run_backtest
from wtpy.apps.astock.service.rules import RuleService
from wtpy.apps.astock.service import stock_names as sn
from wtpy.apps.astock.research.signal_cache import (
    get_or_compute_signals,
    load_signal_cache,
    save_signal_cache,
    _load_blob,
)
from wtpy.apps.astock.study import SignalEvent

FORMULA_NL = "XG:NOT(NAMELIKE('ST'));\n"
FORMULA_PLAIN = "XG:C>REF(C,1);\n"


class _Spec:
    """最小 spec 桩：只提供 formula_text（ensure_stock_names_for 只看这个）。"""

    def __init__(self, formula_text, spec_id="t"):
        self.formula_text = formula_text
        self.id = spec_id


# ---------------- B: 名称快照 ----------------

def test_mixed_rules_trigger_name_load(tmp_path, monkeypatch):
    """混合规则（普通+NAMELIKE）不得跳过名称加载（any，不是 all）。"""
    from wtpy.apps.astock.service import bagua_query as bq

    called = {"n": 0}

    def fake_ensure_fresh(cfg, codes, max_age_days=7, force_refresh=False):
        called["n"] += 1
        return {"600000": "浦发银行"}, {"600000": "2026-09-13"}

    monkeypatch.setattr(bq, "ensure_fresh_symbol_names", fake_ensure_fresh)
    cfg = get_default_config(storage_root=tmp_path / "s", indicator_dir=tmp_path / "i")
    # 全部普通规则：零加载
    m1, sid1 = sn.ensure_stock_names_for(cfg, ["600000"], [_Spec(FORMULA_PLAIN)])
    assert m1 == {} and sid1 == ""
    # 混合：任一 NAMELIKE 即加载
    m2, sid2 = sn.ensure_stock_names_for(
        cfg, ["600000"], [_Spec(FORMULA_PLAIN), _Spec(FORMULA_NL)]
    )
    assert called["n"] == 1
    assert m2 == {"600000": "浦发银行"}
    assert sid2 != ""


def test_stale_names_not_used_when_refresh_fails(tmp_path, monkeypatch):
    """刷新失败时过期名称不参与计算：返回 map 缺该票 → 运行期报错可见。"""
    from wtpy.apps.astock.service import bagua_query as bq

    monkeypatch.setattr(
        bq, "_fetch_symbol_meta_from_tushare",
        lambda cfg: (_ for _ in ()).throw(RuntimeError("network down")),
    )
    monkeypatch.setattr(
        bq, "_rizhu_list_dates_cache_path", lambda: tmp_path / "meta.json"
    )
    monkeypatch.setattr(bq, "_SYMBOL_META_CACHE", {})
    # 进程级节流状态复位，保证本用例真的会尝试刷新
    monkeypatch.setattr(bq, "_LAST_FORCE_NAME_REFRESH_TS", 0.0)
    # 旧缓存：有名称但 per-code 年龄 > 7 天（过期）
    (tmp_path / "meta.json").write_text(
        json.dumps({
            "schema_version": 3,
            "fetched_at": "2026-08-01",
            "stocks": {"600000": 19991110},
            "etfs": {},
            "stock_names": {"600000": "浦发银行"},
            "etf_names": {},
            "per_code_fetched": {"600000": "2026-08-01"},
        }),
        encoding="utf-8",
    )
    # tdx_root 指向不存在路径：TDX/周报/universe 源全部封闭（CI 与本机均稳定）
    cfg = get_default_config(
        storage_root=tmp_path / "s",
        indicator_dir=tmp_path / "i",
        tdx_root=tmp_path / "no_tdx",
        forecast_root=tmp_path / "no_fc",
        forecast_weekly_dir=tmp_path / "no_fc_weekly",
    )
    m, _sid = sn.ensure_stock_names_for(cfg, ["600000"], [_Spec(FORMULA_NL)])
    assert m == {}  # 过期名称不返回 → 调用方报错可见


def test_name_snapshot_content_fingerprint(tmp_path, monkeypatch):
    """名称内容不变 → 指纹不变；戴帽 → 指纹变（缓存失效依据）。"""
    from wtpy.apps.astock.service import bagua_query as bq

    state = {"name": "戴帽", "age": "2026-09-13"}

    def fake_ensure_fresh(cfg, codes, max_age_days=7, force_refresh=False):
        return ({c: state["name"] for c in codes}, {c: state["age"] for c in codes})

    monkeypatch.setattr(bq, "ensure_fresh_symbol_names", fake_ensure_fresh)
    cfg = get_default_config(
        storage_root=tmp_path / "s",
        indicator_dir=tmp_path / "i",
        tdx_root=tmp_path / "no_tdx",
        forecast_root=tmp_path / "no_fc",
        forecast_weekly_dir=tmp_path / "no_fc_weekly",
    )
    specs = [_Spec(FORMULA_NL)]
    _m1, sid1 = sn.ensure_stock_names_for(cfg, ["600000"], specs)
    _m2, sid2 = sn.ensure_stock_names_for(cfg, ["600000"], specs)
    assert sid1 == sid2  # 内容一致 → 键稳定
    state["name"] = "ST戴帽"
    _m3, sid3 = sn.ensure_stock_names_for(cfg, ["600000"], specs)
    assert sid3 != sid1  # 戴帽 → 指纹变


# ---------------- B2: 名称源逐文件时效（门与加载同路径） ----------------

def _touch_days_old(path: Path, days: float) -> None:
    import os
    import time as _t

    ts = _t.time() - days * 86400
    os.utime(path, (ts, ts))


def test_universe_loader_per_file_freshness(tmp_path):
    """universe.json：传 max_age_seconds 时过期文件整体不参与；默认不过滤。"""
    cfg = get_default_config(storage_root=tmp_path / "s", indicator_dir=tmp_path / "i")
    upath = Path(cfg.storage_root) / "universe.json"
    upath.parent.mkdir(parents=True, exist_ok=True)
    upath.write_text(
        json.dumps({"symbols": [{"code": "600000", "name": "浦发银行"}]}),
        encoding="utf-8",
    )
    _touch_days_old(upath, 30)
    fresh = sn.NAME_FRESH_DAYS * 86400.0
    assert sn._load_from_universe(cfg, max_age_seconds=fresh) == {}
    # 默认（None）不过滤：通用名称解析路径行为不变
    assert sn._load_from_universe(cfg) == {"600000": "浦发银行"}


def test_tdx_loader_per_file_freshness(tmp_path):
    """TDX 候选逐文件时效：首个文件过期被跳过、取新鲜后补文件；全过期则空。"""
    tdx = tmp_path / "tdx"
    (tdx / "T0002" / "hq_cache").mkdir(parents=True)
    (tdx / "hq_cache").mkdir(parents=True)
    f1 = tdx / "T0002" / "hq_cache" / "infoharbor_ex.code"
    f2 = tdx / "hq_cache" / "infoharbor_ex.code"
    f1.write_text("600000|ST老名\n", encoding="gbk")
    f2.write_text("600000|浦发银行\n", encoding="gbk")
    _touch_days_old(f1, 30)  # f1 过期、f2 新鲜
    fresh = sn.NAME_FRESH_DAYS * 86400.0
    cfg = get_default_config(
        storage_root=tmp_path / "s", indicator_dir=tmp_path / "i", tdx_root=tdx
    )
    # 过期的 f1 被跳过 → 取新鲜的 f2（不再被首个文件拖死）
    assert sn._load_from_tdx_infoharbor(cfg, max_age_seconds=fresh) == {
        "600000": "浦发银行"
    }
    # 默认不过滤：首个产出文件（f1）生效，即使它过期
    assert sn._load_from_tdx_infoharbor(cfg) == {"600000": "ST老名"}
    # 全部过期 → 整源不参与
    _touch_days_old(f2, 30)
    assert sn._load_from_tdx_infoharbor(cfg, max_age_seconds=fresh) == {}


def test_weekly_loader_per_file_freshness(tmp_path):
    """周报快照逐文件时效：过期 active 周文件跳过、新鲜目录补上；
    只存在于过期文件的 code 不返回；默认路径 active 周优先（first-wins）。"""
    weekly = tmp_path / "weekly"
    old_dir = weekly / "snapshots" / "20260101"
    new_dir = weekly / "snapshots" / "20260201"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    (weekly / "index.json").write_text(
        json.dumps({"active_week_key": "20260101"}), encoding="utf-8"
    )
    old_f = old_dir / "stocks.jsonl"
    new_f = new_dir / "stocks.jsonl"
    old_f.write_text(
        json.dumps({"code6": "600000", "name": "ST老名"}, ensure_ascii=False) + "\n"
        + json.dumps({"code6": "000001", "name": "旧平安"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    new_f.write_text(
        json.dumps({"code6": "600000", "name": "浦发银行"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _touch_days_old(old_f, 30)  # 仅 old 过期
    fresh = sn.NAME_FRESH_DAYS * 86400.0
    cfg = get_default_config(
        storage_root=tmp_path / "s",
        indicator_dir=tmp_path / "i",
        forecast_weekly_dir=weekly,
    )
    got = sn._load_from_forecast_weekly(cfg, max_age_seconds=fresh)
    # 过期 active 周文件被跳过：600000 取新鲜目录的新名；仅旧文件有的 000001 不返回
    assert got == {"600000": "浦发银行"}
    # 默认不过滤：active 周优先（first-wins），旧名照常返回
    got_all = sn._load_from_forecast_weekly(cfg)
    assert got_all == {"600000": "ST老名", "000001": "旧平安"}


# ---------------- C: 信号缓存错误切片 ----------------

def test_signal_cache_saves_and_replays_errors(tmp_path):
    """错误切片：只缓存信号计算新增错误，回放不重复追加既有错误。"""
    cfg = get_default_config(storage_root=tmp_path / "s")
    cfg.storage_root.mkdir(parents=True, exist_ok=True)
    events = [SignalEvent(std_code="SSE.STK.600000", date=20260828, period="DAY", indicator_id="t")]

    def make_compute(extra_pre, new_err):
        def _compute():
            errors_ref.extend(extra_pre)          # 既有错误（行情加载等）
            if new_err:
                errors_ref.append(new_err)         # 本次信号计算新增
            return events
        return _compute

    errors_ref = [
        {"code": "*", "indicator": "coverage", "error": "SOFT_DROP 5 只"},
    ]
    sig_err = {"code": "SSE.STK.600001", "indicator": "t", "error": "NAMELIKE requires stock name context"}
    ev, hit = get_or_compute_signals(
        "k1", make_compute([], sig_err), cfg=cfg, errors_ref=errors_ref
    )
    assert hit is False
    assert errors_ref == [
        {"code": "*", "indicator": "coverage", "error": "SOFT_DROP 5 只"},
        sig_err,
    ]
    blob = _load_blob("k1", cfg=cfg)
    assert blob is not None
    assert blob["signal_errors"] == [sig_err]  # 只存切片，不含 coverage

    # 第二次（缓存命中）：回放 signal_errors，既有错误不被覆盖/重复
    errors_ref2 = [
        {"code": "*", "indicator": "coverage", "error": "SOFT_DROP 5 只"},
    ]
    ev2, hit2 = get_or_compute_signals("k1", lambda: events, cfg=cfg, errors_ref=errors_ref2)
    assert hit2 is True
    assert errors_ref2 == [
        {"code": "*", "indicator": "coverage", "error": "SOFT_DROP 5 只"},
        sig_err,  # 缓存错误回放 → 每次运行都可见
    ]


def test_signal_cache_old_schema_recomputes(tmp_path):
    """旧 schema（v1 无 signal_errors）→ 不可验证 → 重算（不当作无错误）。"""
    from wtpy.apps.astock.research.signal_cache import CACHE_SCHEMA, _path_for

    cfg = get_default_config(storage_root=tmp_path / "s")
    path = _path_for("k_old", cfg=cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema": "signal_cache_v1",
            "key": "k_old",
            "saved_at": 1,
            "n_events": 0,
            "meta": {},
            "events": [],
        }),
        encoding="utf-8",
    )
    assert load_signal_cache("k_old", cfg=cfg) is None  # 旧 schema 拒绝
    called = {"n": 0}

    def _compute():
        called["n"] += 1
        return [SignalEvent(std_code="SSE.STK.600000", date=20260828, period="DAY", indicator_id="t")]

    ev, hit = get_or_compute_signals("k_old", _compute, cfg=cfg)
    assert hit is False and called["n"] == 1  # 重算


# ---------------- D: 复核指纹 ----------------

def _write_review(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _mk_review(rid="user_demo", fp="abc123", status="ok", with_fp=True, **extra):
    payload = {
        "asof": 20260828,
        "generated_at": "2026-08-28 18:30:00",
        "status": status,
        "no_go_reason": "",
        "formal_l1_id": "ds_l1",
        "universe_size": 1,
        "scanned": 1,
        "missing_count": 0,
        "error_count": 0,
        "errors": [],
        "rules": [{"rule_id": rid, "sheet": "demo", "count": 0, "matched": []}],
        "duration_sec": 0.1,
    }
    if with_fp:
        payload.update({
            ir._FP_RULE: {rid: fp},
            ir._FP_UNIVERSE: "u1",
            ir._FP_NAME: "n1",
            ir._FP_SURFACE: "ds_l1:20260828:20260828",
        })
    payload.update(extra)
    return payload


def test_fingerprints_match_all_keys_required():
    cur = {ir._FP_RULE: {"a": "1"}, ir._FP_UNIVERSE: "u", ir._FP_NAME: "n", ir._FP_SURFACE: "s"}
    assert ir._fingerprints_match(dict(cur), cur) is True
    # 缺任一键 = 无法验证 = 不匹配
    for key in (ir._FP_RULE, ir._FP_UNIVERSE, ir._FP_NAME, ir._FP_SURFACE):
        partial = {k: v for k, v in cur.items() if k != key}
        assert ir._fingerprints_match(partial, cur) is False
    # 值不同 = 不匹配
    assert ir._fingerprints_match({**cur, ir._FP_NAME: "n2"}, cur) is False


def test_export_rejects_missing_fingerprint_review(tmp_path):
    """旧文件缺规则指纹 → 无法验证 → (None, stale_rules) 不端旧 sheet。"""
    cfg = get_default_config(storage_root=tmp_path / "s", indicator_dir=tmp_path / "i")
    _write_review(ir.review_output_path(cfg, 20260828), _mk_review(with_fp=False))
    review, note = ir.load_review_for_export(cfg, 20260828)
    assert review is None
    assert "stale_rules" in note


def _bootstrap_registry(cfg):
    """与 run_weekly_review 一致的注册表构造（带 user registry）。"""
    from wtpy.apps.astock.indicators.registry import IndicatorRegistry

    return IndicatorRegistry.bootstrap(
        cfg.indicator_dir,
        cfg.mapping_path,
        user_registry_path=ir.user_registry_file(cfg),
    )


def test_export_rejects_changed_rule(tmp_path):
    """规则公式更新 → 复核结果过期 → 拒绝（走即时计算）。"""
    cfg = get_default_config(storage_root=tmp_path / "s", indicator_dir=tmp_path / "i")
    (tmp_path / "i").mkdir(exist_ok=True)
    svc = RuleService(cfg)
    created = svc.create_rule(name="演示", formula_text=FORMULA_PLAIN)
    rid = created["id"]
    spec_fp = ir._spec_fingerprint(_bootstrap_registry(cfg).get(rid))
    _write_review(
        ir.review_output_path(cfg, 20260828),
        _mk_review(rid=rid, fp=spec_fp),
    )
    review, note = ir.load_review_for_export(cfg, 20260828)
    assert review is not None and note == ""  # 一致 → 可用

    # 规则更新（公式整体重写为不同公式）后指纹不匹配
    svc.update_rule(rid, name="演示", formula_text="MA5:=MA(C,5);\nXG:C>MA5;\n")
    review2, note2 = ir.load_review_for_export(cfg, 20260828)
    assert review2 is None and "stale_rules" in note2


def test_review_reuse_fingerprint_mismatch_recomputes(tmp_path, monkeypatch):
    """幂等复用指纹不匹配 → 重算（规则变了旧结果不冒充）。"""
    cfg = get_default_config(storage_root=tmp_path / "s", indicator_dir=tmp_path / "i")
    (tmp_path / "i").mkdir(exist_ok=True)
    svc = RuleService(cfg)
    created = svc.create_rule(name="演示复核", formula_text=FORMULA_PLAIN)
    rid = created["id"]

    # 伪造旧 ok 结果（指纹对不上）
    _write_review(
        ir.review_output_path(cfg, 20260828),
        _mk_review(rid=rid, fp="deadbeef"),
    )
    calls = {"load": 0}

    def _fake_loader(code, asof):
        calls["load"] += 1
        raise FileNotFoundError(code)

    summary = ir.run_weekly_review(
        cfg,
        asof=20260828,
        rule_ids=[rid],
        codes=["SSE.STK.600000"],
        bar_loader=_fake_loader,
        surface_resolver=lambda _cfg: ({"formal_l1_id": "ds", "max_date": 20260828}, ""),
    )
    assert summary.get("reused") is not True  # 未复用 → 真重算（load 被调）
    assert calls["load"] >= 1
    assert summary[ir._FP_RULE].get(rid) is not None  # 新结果带指纹


def test_review_reuse_fingerprint_match_hits(tmp_path):
    """指纹一致 → 幂等命中（不重复扫描）。"""
    cfg = get_default_config(storage_root=tmp_path / "s", indicator_dir=tmp_path / "i")
    (tmp_path / "i").mkdir(exist_ok=True)
    svc = RuleService(cfg)
    created = svc.create_rule(name="演示复核2", formula_text=FORMULA_PLAIN)
    rid = created["id"]

    calls = {"load": 0}

    def _fake_loader(code, asof):
        calls["load"] += 1
        raise FileNotFoundError(code)

    kw = dict(
        asof=20260828,
        rule_ids=[rid],
        codes=["SSE.STK.600000"],
        bar_loader=_fake_loader,
        surface_resolver=lambda _cfg: ({"formal_l1_id": "ds", "max_date": 20260828}, ""),
    )
    s1 = ir.run_weekly_review(cfg, **kw)
    assert calls["load"] == 1
    s2 = ir.run_weekly_review(cfg, **kw)
    assert calls["load"] == 1  # 幂等命中，不再扫描
    assert s2.get("reused") is True
    assert s2[ir._FP_RULE][rid] == ir._spec_fingerprint(_bootstrap_registry(cfg).get(rid))


# ---------------- 端到端：run_backtest 真实执行 NAMELIKE 规则 ----------------

def _bt_sandbox(tmp_path):
    """沙箱回测环境：两只票的 csv 日线 + 一条 NAMELIKE 规则。"""
    from wtpy.apps.astock.service.backtest import BacktestRequest

    storage, ind = tmp_path / "bt_st", tmp_path / "bt_ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    for mk, c6 in (("SSE", "600000"), ("SZSE", "000001")):
        d = storage / "csv" / "day" / mk
        d.mkdir(parents=True, exist_ok=True)
        lines = ["date,open,high,low,close,amount,volume"]
        for i in range(1, 60):
            dt = 20260600 + i
            o, c = 10.0, 10.0 + i * 0.05
            lines.append(
                f"{dt},{o:.2f},{(c + 0.2):.2f},{(o - 0.1):.2f},{c:.2f},1000000,100000"
            )
        (d / f"{c6}.csv").write_text("\n".join(lines), encoding="utf-8")
    svc = RuleService(cfg)
    created = svc.create_rule(
        name="nl_bt", formula_text="去风险:=NOT(NAMELIKE('ST'));\nXG:C>OPEN AND 去风险;\n"
    )
    return cfg, created["id"], BacktestRequest


def test_no_go_path_reuses_ok_only_when_fingerprints_match(tmp_path):
    """数据面不可用时的旧结果复用，必须指纹一致（GPT6 约束的行为写死）。

    - 指纹一致（规则/池/名称快照未变）→ 保留旧 ok 结果（导出不丢 sheet）
    - 规则公式变了 → 不再返回旧 ok matched，返回 no_go
    """
    cfg = get_default_config(storage_root=tmp_path / "s", indicator_dir=tmp_path / "i")
    (tmp_path / "i").mkdir(exist_ok=True)
    svc = RuleService(cfg)
    rid = svc.create_rule(name="no_go_复用", formula_text=FORMULA_PLAIN)["id"]

    ok_surface = lambda _cfg: ({"formal_l1_id": "ds", "max_date": 20260828}, "")

    def _fail_loader(code, asof):
        raise FileNotFoundError(code)

    # 先产出一份带指纹的 ok 结果
    first = ir.run_weekly_review(
        cfg, asof=20260828, rule_ids=[rid], codes=["SSE.STK.600000"],
        bar_loader=_fail_loader, surface_resolver=ok_surface,
    )
    assert first["status"] == "ok"
    assert first[ir._FP_RULE].get(rid)

    bad_surface = lambda _cfg: (None, "no_formal_l1_product")

    # ① 指纹一致 → 复用旧 ok（不是 no_go）
    again = ir.run_weekly_review(
        cfg, asof=20260828, rule_ids=[rid], codes=["SSE.STK.600000"],
        bar_loader=_fail_loader, surface_resolver=bad_surface,
    )
    assert again["status"] == "ok"
    assert again.get("reused") is True

    # ② 规则公式更新 → 指纹不匹配 → 不得返回旧 ok，落 no_go
    svc.update_rule(rid, name="no_go_复用", formula_text="MA3:=MA(C,3);\nXG:C>MA3;\n")
    after = ir.run_weekly_review(
        cfg, asof=20260828, rule_ids=[rid], codes=["SSE.STK.600000"],
        bar_loader=_fail_loader, surface_resolver=bad_surface,
    )
    assert after["status"] == "no_go"
    assert after.get("reused") is None


def test_backtest_namelike_filters_st_and_cache_key_tracks_name(tmp_path, monkeypatch):
    """端到端：run_backtest 内 NAMELIKE 真实生效 + 缓存键随名称内容变化。

    ① 两票普通名 → 有信号、0 错误、未命中缓存（首跑）
    ② 其中一票改名 ST → 该票信号被剔除、错误仍为 0、**未命中缓存**（键随名称内容变）
    ③ 同名重跑 → **命中缓存**（名称未变则键稳定，昂贵计算复用）
    """
    import wtpy.apps.astock.service.stock_names as sn
    from wtpy.apps.astock.forecast.name_norm import normalize_stock_code

    cfg, rid, BacktestRequest = _bt_sandbox(tmp_path)

    state = {"600000": "普通甲", "000001": "普通乙"}

    def fake_names(cfg_, codes, specs):
        pool = {}
        for c in codes:
            c6 = normalize_stock_code(c)
            if c6 in state:
                pool[c6] = state[c6]
        return pool, "fp_" + ",".join(f"{k}={v}" for k, v in sorted(pool.items()))

    monkeypatch.setattr(sn, "ensure_stock_names_for", fake_names)

    def _run(use_cache=True):
        return run_backtest(
            cfg,
            BacktestRequest(
                rule_ids=[rid],
                codes=["600000", "000001"],
                start=20260601,
                end=20260630,
                research_unadjusted=True,
                use_signal_cache=use_cache,
                artifact_level="summary",
            ),
        )

    r1 = _run()
    assert r1["status"] == "research_unadjusted"
    assert r1["n_events"] > 0
    assert not (r1.get("errors_sample") or []), "两票都有名称，不应有错误"
    assert r1["signal_cache_hit"] is False
    n_both = r1["n_events"]

    state["000001"] = "ST测试乙"  # 戴帽
    r2 = _run()
    assert not (r2.get("errors_sample") or []), "ST 过滤是正常剔除，不报错"
    assert 0 < r2["n_events"] < n_both, "ST 票信号应被剔除、另一票保留"
    assert r2["signal_cache_hit"] is False, "名称内容变→缓存键变→必须重算（旧缓存不得放行）"

    r3 = _run()
    assert r3["signal_cache_hit"] is True, "名称未变→键稳定→应命中缓存"
    assert r3["n_events"] == r2["n_events"]


def test_backtest_namelike_missing_name_reports_error_not_signal(tmp_path, monkeypatch):
    """端到端：名称缺失 → 该票该规则进 errors 且不产信号（报错可见策略）。"""
    import wtpy.apps.astock.service.stock_names as sn

    cfg, rid, BacktestRequest = _bt_sandbox(tmp_path)
    # 名称快照只提供一票 → 另一票缺名
    monkeypatch.setattr(
        sn, "ensure_stock_names_for", lambda cfg_, codes, specs: ({"600000": "普通甲"}, "fp1")
    )
    res = run_backtest(
        cfg,
        BacktestRequest(
            rule_ids=[rid],
            codes=["600000", "000001"],
            start=20260601,
            end=20260630,
            research_unadjusted=True,
            use_signal_cache=False,
            artifact_level="summary",
        ),
    )
    errs = res.get("errors_sample") or []
    assert errs, "缺名称必须留下可见错误"
    assert any("NAMELIKE" in (e.get("error") or "") for e in errs)
    assert any("000001" in str(e.get("code") or "") for e in errs)
    # 有名称的票照常出信号（未被连坐）
    assert res["n_events"] > 0


def test_backtest_combine_all_skips_code_when_one_rule_fails(tmp_path, monkeypatch):
    """端到端：combine=all 下任一参与规则失败 → 该票不产生组合信号（记错误）。"""
    import wtpy.apps.astock.service.stock_names as sn

    cfg, rid_namelike, BacktestRequest = _bt_sandbox(tmp_path)
    svc = RuleService(cfg)
    rid_plain = svc.create_rule(name="plain_bt", formula_text="XG:C>OPEN;\n")["id"]
    # NAMELIKE 规则拿不到名称 → 该规则对每票都失败
    monkeypatch.setattr(sn, "ensure_stock_names_for", lambda cfg_, codes, specs: ({}, ""))

    base = dict(
        codes=["600000", "000001"],
        start=20260601,
        end=20260630,
        research_unadjusted=True,
        use_signal_cache=False,
        artifact_level="summary",
    )
    # 单跑普通规则：正常出信号（证明行情本身有信号）
    solo = run_backtest(cfg, BacktestRequest(rule_ids=[rid_plain], **base))
    assert solo["n_events"] > 0

    # combine=all + 含失败规则：不得产生组合信号
    combined = run_backtest(
        cfg, BacktestRequest(rule_ids=[rid_plain, rid_namelike], combine="all", **base)
    )
    assert combined["n_events"] == 0, "任一参与规则失败时不得产组合信号"
    errs = combined.get("errors_sample") or []
    assert any("组合信号未产生" in (e.get("error") or "") for e in errs), errs



# ---------------- 研究执行路径的缓存键（name_snapshot 维度） ----------------

def test_research_executor_key_tracks_name_snapshot():
    """research/executor 的信号键与过滤键都必须随名称快照变化。

    NAMELIKE 规则的信号取决于股票名称；若该路径漏掉名称维度，戴帽后
    会复用旧名称算出的信号（与 service/backtest 同口径修复）。
    """
    from types import SimpleNamespace

    from wtpy.apps.astock.research.executor import (
        build_filter_key_from_request,
        build_signal_key_from_request,
    )

    req = SimpleNamespace(
        period="DAY", rule_ids=["r1"], start=20260101, end=20260630,
        research_unadjusted=True, combine=None,
    )
    k_plain = build_signal_key_from_request(req, universe_hash="u")
    k_name_a = build_signal_key_from_request(req, universe_hash="u", name_snapshot_id="fpA")
    k_name_b = build_signal_key_from_request(req, universe_hash="u", name_snapshot_id="fpB")

    assert k_plain != k_name_a, "含 NAMELIKE 时键必须带上名称维度"
    assert k_name_a != k_name_b, "名称内容变→键必须变"

    # 过滤键以信号键为输入 → 传递性感知名称
    f_a = build_filter_key_from_request(req, signal_key=k_name_a)
    f_b = build_filter_key_from_request(req, signal_key=k_name_b)
    assert f_a != f_b

def test_create_rule_preserves_single_quotes_and_strips_outer_ws(tmp_path):
    """原始通达信公式（含单引号 NAMELIKE）保存成功；回读 strip 后逐字一致，
    内部单引号完整保留（_sanitize_rule_text 只清洗 name/description）。"""
    storage = tmp_path / "storage"
    (storage / "indicators").mkdir(parents=True)
    ind = tmp_path / "empty_ind"
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    svc = RuleService(cfg)

    formula = (
        "  MA5:=MA(C,5);\n"
        "去风险:=NOT(NAMELIKE('ST') OR NAMELIKE('*ST') OR DYNAINFO(4)=0);\n"
        "XG:去风险 AND C>MA5;\n  "
    )
    v = svc.validate_formula(formula)
    assert v["ok"] is True and v["has_xg"] is True
    created = svc.create_rule(name="单引号保存", formula_text=formula)
    assert created["backtestable"] is True
    got = svc.get_rule(created["id"])
    assert got["formula_text"] == formula.strip()
    assert "NAMELIKE('ST')" in got["formula_text"]  # 内部单引号保留
