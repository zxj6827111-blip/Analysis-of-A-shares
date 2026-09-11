# -*- coding: utf-8 -*-
"""独立验证：规则标准化基准回测（rule_benchmark v1）。

本模块不改业务代码，只从外部契约与边界条件出发复核：
- 路由注册顺序（静态子路径不被 /{rule_id} 吞掉）
- profile 字段/end 解析/sidecar 白名单与坏值回退
- POST 确认、复用、失败重跑、落库、队列容量、tn6 审计、并发串行化
- performance validity 状态机与判定顺序、pending(queued/running) 契约、数值口径
- 真实后端 JSON 喂给 index_v3 的 renderRulePerformancePanel（node 桩）
- DB 老库自动建表、重复/并发提交

第二轮（coder 修复后）为设计内行为，不再保留 xfail/已知问题标注。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.api import create_app
from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.indicators.models import IndicatorSpec
from wtpy.apps.astock.service import rule_benchmark as rb
from wtpy.apps.astock.service.db import (
    connect,
    create_rule_benchmark,
    get_latest_rule_benchmark,
    init_db,
    list_rule_benchmarks,
    update_rule_benchmark,
)
from wtpy.apps.astock.service.rules import RuleService

ROOT = Path(__file__).resolve().parents[3]
V3 = ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index_v3.html"
HARNESS = Path(__file__).resolve().parent / "v3_rule_performance_harness.js"

MIN60_FORMULA = 'DIF60:="MACD.DIF#MIN60";\nXG:C>0 AND DIF60>0;'


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
    storage = tmp_path / "storage"
    indicators = tmp_path / "indicators"
    outputs = tmp_path / "outputs"
    storage.mkdir(parents=True)
    indicators.mkdir(parents=True)
    outputs.mkdir(parents=True)
    return get_default_config(
        storage_root=storage,
        indicator_dir=indicators,
        output_root=outputs,
    )


class FakeJob:
    def __init__(self, job_id, status="queued", run_id=None, error=None, progress=None):
        self.job_id = job_id
        self.status = status
        self.run_id = run_id
        self.error = error
        self.progress = progress


class FakeJobStore:
    """Records submits; queue_snapshot can be forced full for 429 tests."""

    def __init__(self):
        self.records: dict = {}
        self.submits: list = []
        self.before_submit = None
        self.queue_size = 0
        self.cancelled: list = []

    def submit(self, req):
        if self.before_submit is not None:
            self.before_submit()
        self.submits.append(req)
        rec = FakeJob("job_fake_%d" % len(self.submits))
        self.records[rec.job_id] = rec
        return rec

    def get(self, job_id):
        if job_id not in self.records:
            raise KeyError(job_id)
        return self.records[job_id]

    def cancel(self, job_id):
        rec = self.get(job_id)
        rec.status = "cancelled"
        self.cancelled.append(job_id)
        return rec

    def queue_snapshot(self):
        n_queued = self.queue_size or sum(
            1 for r in self.records.values() if r.status == "queued"
        )
        return {"n_queued": n_queued, "n_running": 0}


def _ctx(cfg, jobs=None):
    return SimpleNamespace(
        cfg=cfg,
        rules=RuleService(cfg),
        jobs=jobs or FakeJobStore(),
        bt_svc=None,
    )


def _client(cfg):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = create_app(cfg)
    return TestClient(app), app


def _make_rule(cfg, name="验证规则", formula="XG:C>0;"):
    return RuleService(cfg).create_rule(name=name, formula_text=formula)


def _spec(**overrides):
    base = {
        "id": "verify_rule",
        "name": "verify",
        "version": "v1",
        "kind": "tdx_formula",
        "output_type": "signal",
        "supported_periods": ("DAY",),
        "compile_status": "ready",
        "parameters": {},
    }
    base.update(overrides)
    return IndicatorSpec(**base)


def _summary(n_round_trips, run_id, status="ok", data_cutoff=20260731):
    return {
        "run_id": run_id,
        "status": status,
        "metrics": {
            "total_return": 0.25,
            "annual_return": 0.18,
            "max_drawdown": -0.12,
            "win_rate": 0.55,
            "sharpe": 1.4,
            "n_round_trips": n_round_trips,
        },
        "data_cutoff_date": data_cutoff,
        "meta": {},
        "repro": {},
    }


def _equity_two(run_id):
    return [
        {"date": 20260101, "cash": 1.0, "market_value": 0.0, "equity": 1.0},
        {"date": 20260102, "cash": 1.1, "market_value": 0.0, "equity": 1.1},
    ]


def _record_run(cfg, rule_id, run_id, profile=None):
    bench_id = create_rule_benchmark(
        cfg,
        rule_id,
        "job_" + run_id,
        profile if profile is not None else rb.build_benchmark_profile(cfg),
    )
    update_rule_benchmark(cfg, bench_id, run_id=run_id, status="succeeded")
    return bench_id


# ===========================================================================
# a) route ordering / static paths
# ===========================================================================


def test_static_rule_routes_registered_before_dynamic_rule_id(cfg):
    client, app = _client(cfg)
    app.state.astock.jobs = FakeJobStore()

    paths = [getattr(r, "path", "") for r in app.routes]
    assert "/api/v1/rules/benchmark-profile" in paths
    assert "/api/v1/rules/{rule_id}" in paths
    assert paths.index("/api/v1/rules/benchmark-profile") < paths.index(
        "/api/v1/rules/{rule_id}"
    )

    r = client.get("/api/v1/rules/benchmark-profile")
    assert r.status_code == 200, r.text
    assert isinstance(r.json().get("profile"), dict)

    cats = client.get("/api/v1/rules/categories")
    assert cats.status_code == 200 and "categories" in cats.json()

    added = client.post("/api/v1/rules/categories", json={"name": "验证分类"})
    assert added.status_code == 200 and "验证分类" in added.json()["categories"]

    imp = client.post(
        "/api/v1/rules/import",
        json={"filename": "route_probe.txt", "content": "XG:C>0;\n"},
    )
    assert imp.status_code == 200, imp.text
    rid = imp.json()["id"]
    assert rid.startswith("txt_")

    # dynamic route must still work and unknown ids stay 404
    got = client.get("/api/v1/rules/%s" % rid)
    assert got.status_code == 200 and got.json()["id"] == rid
    assert client.get("/api/v1/rules/no_such_rule_zz").status_code == 404

    batch = client.post("/api/v1/rules/batch-validate", json={"ids": [rid]})
    assert batch.status_code == 200


# ===========================================================================
# b) profile
# ===========================================================================


def test_profile_contract_and_end_none_without_data(cfg):
    assert not Path(cfg.market_data_root).exists()
    profile = rb.build_benchmark_profile(cfg)

    assert set(rb.DEFAULT_BENCHMARK_PROFILE).issubset(set(profile))
    assert profile["profile_id"] == "rule_benchmark_v1"
    assert profile["version"] == 1
    assert profile["codes"] == ["ALL"]
    assert profile["period"] == "DAY"
    assert profile["start"] == 20180101
    assert profile["end"] is None
    assert profile["account_mode"] == "portfolio"
    assert profile["entry_lag"] == 1 and profile["hold"] == 1
    assert profile["buy_on"] == "open" and profile["sell_on"] == "close"
    assert profile["engine"] == "full" and profile["artifact_level"] == "full"
    assert profile["gua_filter"] == {"enabled": False}
    assert profile["benchmark_index"] is None
    assert profile["sample_gates"] == {"min_round_trips": 30}
    assert "佣金" in profile["fee_note"] and "印花税" in profile["fee_note"]


def test_profile_end_resolves_from_ready_dataset_cutoff(cfg, monkeypatch, tmp_path):
    from tests.apps.astock.conftest import build_overlay_warehouse

    wh = tmp_path / "wh"
    wh.mkdir()
    build_overlay_warehouse(wh)
    monkeypatch.setenv("MARKET_DATA_ROOT", str(wh))

    assert rb.resolve_data_max_date(cfg) == 20240108
    assert rb.build_benchmark_profile(cfg)["end"] == 20240108


def test_resolve_data_max_date_scans_symbol_last_dates(cfg, monkeypatch, tmp_path):
    root = tmp_path / "md_scan"
    root.mkdir()
    monkeypatch.setenv("MARKET_DATA_ROOT", str(root))

    import wtpy.apps.astock.data.repository as repo_mod

    class _Repo:
        def __init__(self, store):
            pass

        def list_datasets(self):
            return [
                SimpleNamespace(
                    status="failed", data_cutoff_date=0, symbols=[], period="1d"
                ),
                SimpleNamespace(
                    status="partial",
                    data_cutoff_date=0,
                    period="1d",
                    symbols=[
                        SimpleNamespace(last_date=20200202),
                        SimpleNamespace(last_date=20200101),
                    ],
                ),
                # minute dataset with a much later cutoff must be filtered out
                SimpleNamespace(
                    status="ready",
                    data_cutoff_date=20260731,
                    period="60m",
                    symbols=[SimpleNamespace(last_date=20260731)],
                ),
            ]

    monkeypatch.setattr(repo_mod, "MarketDataRepository", _Repo)
    assert rb.resolve_data_max_date(cfg) == 20200202


def test_resolve_data_max_date_ttl_cache(cfg, monkeypatch, tmp_path):
    # unique market_data_root key so the module-level cache cannot leak in
    root = tmp_path / "md_ttl"
    root.mkdir()
    monkeypatch.setenv("MARKET_DATA_ROOT", str(root))
    calls = {"n": 0}

    def _fake_uncached(c):
        calls["n"] += 1
        return 20240101

    monkeypatch.setattr(rb, "_resolve_data_max_date_uncached", _fake_uncached)
    assert rb.resolve_data_max_date(cfg) == 20240101
    assert rb.resolve_data_max_date(cfg) == 20240101
    assert calls["n"] == 1

    rb._DATA_MAX_CACHE[str(cfg.market_data_root)] = (
        time.monotonic() - rb._DATA_MAX_CACHE_TTL - 1,
        None,
    )
    assert rb.resolve_data_max_date(cfg) == 20240101
    assert calls["n"] == 2


def test_resolve_data_max_date_degrades_silently(cfg, monkeypatch, tmp_path):
    root = tmp_path / "broken_md"
    root.mkdir()
    monkeypatch.setenv("MARKET_DATA_ROOT", str(root))

    import wtpy.apps.astock.data.dataset_store as ds_mod

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("store unavailable")

    monkeypatch.setattr(ds_mod, "DatasetStore", _Boom)
    assert rb.resolve_data_max_date(cfg) is None


def test_profile_sidecar_shallow_override_and_corrupt_fallback(cfg):
    sidecar = Path(cfg.storage_root) / "benchmark_profile.json"

    sidecar.write_text(
        json.dumps(
            {
                "end": 20200101,
                "hold": 5,
                "codes": ["SSE.STK.600000"],
                "sample_gates": {"min_round_trips": 2, "note": "shallow"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    profile = rb.build_benchmark_profile(cfg)
    assert profile["end"] == 20200101
    assert profile["hold"] == 5
    assert profile["codes"] == ["SSE.STK.600000"]
    # whitelist override replaces the default nested dict wholesale
    assert profile["sample_gates"] == {"min_round_trips": 2, "note": "shallow"}

    sidecar.write_text("{not valid json", encoding="utf-8")
    fallback = rb.build_benchmark_profile(cfg)
    assert fallback["codes"] == ["ALL"] and fallback["hold"] == 1
    assert fallback["sample_gates"] == {"min_round_trips": 30}

    sidecar.write_text("[1, 2, 3]", encoding="utf-8")
    fallback2 = rb.build_benchmark_profile(cfg)
    assert fallback2["codes"] == ["ALL"] and fallback2["end"] is None


def test_profile_sidecar_bad_types_are_dropped_not_500(cfg):
    """修复点 5：坏类型逐字段回退默认；展示字段不接受覆盖。"""
    sidecar = Path(cfg.storage_root) / "benchmark_profile.json"
    sidecar.write_text(
        json.dumps(
            {
                "entry_lag": "abc",
                "hold": "many",
                "with_bagua": "yes",
                "codes": ["SSE.STK.600000", 123],
                "account_mode": "bogus",
                "engine": 3,
                "artifact_level": "everything",
                "signal_weekdays": "not-a-day",
                "buy_weekday": 99,
                "start": -5,
                "buy_on": "   ",
                "sell_on": "midnight",
                "stop_loss": None,
                "take_profit": float("nan"),
                "gua_filter": "off",
                "sample_gates": ["not", "dict"],
                "universe_label": "hacked",
                "data_quality": "hacked",
                "benchmark_index": "SSE.INDX.000300",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    profile = rb.build_benchmark_profile(cfg)  # must not raise
    assert profile["entry_lag"] == 1 and profile["hold"] == 1
    assert profile["with_bagua"] is False and profile["codes"] == ["ALL"]
    assert profile["account_mode"] == "portfolio"
    assert profile["engine"] == "full" and profile["artifact_level"] == "full"
    assert profile["signal_weekdays"] is None
    assert profile["buy_weekday"] is None and profile["exit_weekday"] is None
    assert profile["buy_on"] == "open" and profile["sell_on"] == "close"
    assert profile["stop_loss"] is None and profile["take_profit"] is None
    assert profile["gua_filter"] == {"enabled": False}
    assert profile["sample_gates"] == {"min_round_trips": 30}
    # display-only fields stay default
    assert profile["universe_label"] == rb.DEFAULT_BENCHMARK_PROFILE["universe_label"]
    assert profile["data_quality"] == rb.DEFAULT_BENCHMARK_PROFILE["data_quality"]
    assert profile["benchmark_index"] is None

    # the API layer must still serve a 200 profile with the same fallbacks
    client, app = _client(cfg)
    app.state.astock.jobs = FakeJobStore()
    r = client.get("/api/v1/rules/benchmark-profile")
    assert r.status_code == 200, r.text
    assert r.json()["profile"]["account_mode"] == "portfolio"

    # sample_gates 为坏 dict 值：min_round_trips 回退默认、其余键保留
    sidecar.write_text(
        json.dumps(
            {
                "sample_gates": {"min_round_trips": "oops", "note": "kept"},
                "entry_lag": 0,  # 第三轮收紧：0 非法 -> 回退默认 1
            }
        ),
        encoding="utf-8",
    )
    profile2 = rb.build_benchmark_profile(cfg)
    assert profile2["sample_gates"] == {"min_round_trips": 30, "note": "kept"}
    assert profile2["entry_lag"] == 1


def test_profile_sidecar_boundary_tightening(cfg):
    """第三轮边界收紧：int≥1、weekday 1..7、signal_weekdays 去重排序、codes 上限。"""
    sidecar = Path(cfg.storage_root) / "benchmark_profile.json"

    def _write(payload):
        sidecar.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return rb.build_benchmark_profile(cfg)

    # entry_lag / hold: bool / 0 / 负数 / 字符串全部回退默认 1
    for bad in (True, False, 0, -1, "2", 2.0):
        p = _write({"entry_lag": bad})
        assert p["entry_lag"] == 1, bad
        p = _write({"hold": bad})
        assert p["hold"] == 1, bad
    p = _write({"entry_lag": 3, "hold": 4})
    assert p["entry_lag"] == 3 and p["hold"] == 4

    # buy/exit weekday: 1..7 且拒 bool
    for bad in (0, 8, -1, True, False, "5", 5.0):
        assert _write({"buy_weekday": bad})["buy_weekday"] is None, bad
        assert _write({"exit_weekday": bad})["exit_weekday"] is None, bad
    p = _write({"buy_weekday": 5, "exit_weekday": 7})
    assert p["buy_weekday"] == 5 and p["exit_weekday"] == 7

    # signal_weekdays: 去重排序；非法元素整体回退 None
    assert _write({"signal_weekdays": [5, 1, 3, 3]})["signal_weekdays"] == [1, 3, 5]
    assert _write({"signal_weekdays": []})["signal_weekdays"] is None
    assert _write({"signal_weekdays": [1, 2, 3, 4, 5, 6, 7]})[
        "signal_weekdays"
    ] == [1, 2, 3, 4, 5, 6, 7]
    for bad in ([0], [8], [True], ["1"], [1, 3, 3, 3, 3, 3, 3, 3], "1,3"):
        assert _write({"signal_weekdays": bad})["signal_weekdays"] is None, bad

    # codes: ["all"] 归一为 ["ALL"]；单项 ≤32；数量 ≤1000
    assert _write({"codes": ["all"]})["codes"] == ["ALL"]
    assert _write({"codes": ["a" * 32]})["codes"] == ["a" * 32]
    assert _write({"codes": ["a" * 33]})["codes"] == ["ALL"]
    assert _write({"codes": ["X" + str(i) for i in range(1001)]})["codes"] == ["ALL"]
    assert _write({"codes": ["SSE.STK.600000", "SZSE.STK.000001"]})["codes"] == [
        "SSE.STK.600000",
        "SZSE.STK.000001",
    ]
    assert _write({"codes": ["ALL", "SSE.STK.600000"]})["codes"] == [
        "ALL",
        "SSE.STK.600000",
    ]


# ===========================================================================
# c) POST submit
# ===========================================================================


def test_post_unknown_rule_404_and_unbacktestable_400(cfg):
    client, app = _client(cfg)
    app.state.astock.jobs = FakeJobStore()

    assert (
        client.post("/api/v1/rules/nope_zz/benchmark", json={}).status_code == 404
    )
    assert client.get("/api/v1/rules/nope_zz/performance").status_code == 404

    r = client.post("/api/v1/rules/bagua_ohlc/benchmark", json={})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]


def test_validate_rule_gates_and_proxy_helpers():
    rb.validate_rule_for_benchmark(_spec())
    rb.validate_rule_for_benchmark(
        _spec(dependencies=["MIN60"], parameters={"min60_day_proxy": True})
    )

    for bad, needle in (
        (_spec(compile_status="source_required"), "不可回测"),
        (_spec(output_type="series"), "可交易信号"),
        (_spec(id="bagua_ohlc"), "可交易信号"),
        (_spec(dependencies=["MIN1"]), "MIN1"),
        (_spec(dependencies=["MIN60"]), "MIN60"),
        (
            _spec(dependencies=["MIN60"], parameters={"min60_native": True}),
            "原生 60 分钟",
        ),
    ):
        with pytest.raises(ValueError) as ei:
            rb.validate_rule_for_benchmark(bad)
        assert needle in str(ei.value)

    assert rb.needs_research_proxy(
        _spec(dependencies=["MIN60"], parameters={"min60_day_proxy": True})
    )
    assert rb.needs_research_proxy(_spec(parameters={"min60_day_proxy": True}))
    assert not rb.needs_research_proxy(_spec())
    # native MIN60 / plain MIN60 are never treated as a research proxy
    assert not rb.needs_research_proxy(
        _spec(dependencies=["MIN60"], parameters={"min60_native": True})
    )
    assert not rb.needs_research_proxy(_spec(dependencies=["MIN60"]))

    assert (
        rb._proxy_detail(
            _spec(parameters={"min60_proxy_note": "自述"}, failure_reason="ignored")
        )
        == "自述"
    )
    assert rb._proxy_detail(_spec(failure_reason="代理原因")) == "代理原因"
    assert rb._proxy_detail(_spec()) == rb.PROXY_DETAIL_FALLBACK


def test_native_min60_rule_rejected_with_400(cfg):
    """修复点 2：原生 MIN60 在 validate 阶段 400，不进入代理确认/提交。"""
    native = _spec(
        id="native_m60_probe",
        name="native_m60_probe",
        dependencies=["MIN60"],
        parameters={"min60_native": True},
    )

    class _Registry:
        def get(self, rule_id):
            if str(rule_id) in ("native_m60_probe", native.name):
                return native
            raise KeyError(rule_id)

    class _Rules:
        def load_full_registry(self):
            return _Registry()

    client, app = _client(cfg)
    fake = FakeJobStore()
    app.state.astock.jobs = fake
    app.state.astock.rules = _Rules()

    r = client.post("/api/v1/rules/native_m60_probe/benchmark", json={})
    assert r.status_code == 400, r.text
    assert "原生 60 分钟" in r.json()["detail"]
    assert fake.submits == []
    # even with proxy confirmation it must stay rejected
    r2 = client.post(
        "/api/v1/rules/native_m60_probe/benchmark",
        json={"allow_research_proxy": True},
    )
    assert r2.status_code == 400
    assert fake.submits == []


def test_min60_native_exposed_in_rules_list_and_detail(cfg):
    """修复点 4：真实 minute_vendor 仓库下 min60_native 字段出现在列表与详情。"""
    from wtpy.apps.astock.data.dataset_store import DatasetManifest, DatasetStore
    from wtpy.apps.astock.indicators.tn6_importer import (
        file_sha256,
        pair_source,
        save_source_map,
    )
    from wtpy.apps.astock.service.rules import rule_to_public

    # unit: rule_to_public always exposes the boolean field
    assert rule_to_public(_spec(parameters={"min60_native": True}))["min60_native"] is True
    assert rule_to_public(_spec())["min60_native"] is False

    # a ready minute_vendor/60m dataset flips #MIN60 formulas to native mode
    md_root = Path(cfg.market_data_root)
    md_root.mkdir(parents=True, exist_ok=True)
    store = DatasetStore(md_root)
    manifest = DatasetManifest(
        dataset_id="minute_vendor_60m_probe",
        source="minute_vendor",
        adjustment="none",
        period="60m",
        snapshot_date=20240101,
        data_cutoff_date=20240101,
        provider_version="verify",
        status="ready",
        created_at="2024-01-01T00:00:00",
    )
    manifest.symbol_count = 1
    manifest.row_count = 10
    store.save_manifest(manifest)

    pkg = Path(cfg.indicator_dir) / "native60_probe.tn6"
    pkg.write_bytes(b"native-60m-package")
    src = Path(cfg.indicator_dir) / "native60_probe.txt"
    src.write_text(MIN60_FORMULA, encoding="utf-8")
    mapping = {}
    pair_source(mapping, file_sha256(pkg), src, package_file=pkg)
    save_source_map(cfg.mapping_path, mapping)

    rid = "tn6_native60_probe"
    svc = RuleService(cfg)
    rows = {r["id"]: r for r in svc.list_rules()}
    assert rid in rows
    assert rows[rid]["min60_native"] is True
    assert rows[rid]["min60_day_proxy"] is False
    assert svc.get_rule(rid)["min60_native"] is True

    client, app = _client(cfg)
    fake = FakeJobStore()
    app.state.astock.jobs = fake

    listing = {r["id"]: r for r in client.get("/api/v1/rules").json()}
    assert listing[rid]["min60_native"] is True
    detail = client.get("/api/v1/rules/%s" % rid).json()
    assert detail["min60_native"] is True

    r = client.post("/api/v1/rules/%s/benchmark" % rid, json={})
    assert r.status_code == 400, r.text
    assert "原生 60 分钟" in r.json()["detail"]
    assert fake.submits == []


def test_post_min60_needs_confirmation_then_submits_and_persists(cfg):
    client, app = _client(cfg)
    fake = FakeJobStore()
    app.state.astock.jobs = fake
    rule = _make_rule(cfg, "验证M60", MIN60_FORMULA)
    rid = rule["id"]

    first = client.post("/api/v1/rules/%s/benchmark" % rid, json={})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["mode"] == "needs_confirmation"
    assert "60 分钟" in body["message"] and body["detail"]
    assert fake.submits == []
    assert get_latest_rule_benchmark(cfg, rid) is None

    second = client.post(
        "/api/v1/rules/%s/benchmark" % rid, json={"allow_research_proxy": True}
    )
    assert second.status_code == 200, second.text
    payload = second.json()
    assert payload["research_proxy"] is True
    assert payload["reused"] is False
    assert payload["job_id"]

    req = fake.submits[0]
    assert req.rule_ids == [rid] and req.codes == ["ALL"] and req.period == "DAY"
    assert req.account_mode == "portfolio"
    assert req.buy_on == "open" and req.sell_on == "close"
    assert req.entry_lag == 1 and req.hold == 1
    assert req.with_bagua is False

    bench = get_latest_rule_benchmark(cfg, rid)
    assert bench["job_id"] == payload["job_id"]
    assert bench["status"] == "queued"
    assert bench["profile"]["profile_id"] == "rule_benchmark_v1"

    third = client.post(
        "/api/v1/rules/%s/benchmark" % rid, json={"allow_research_proxy": True}
    )
    assert third.json()["reused"] is True
    assert len(fake.submits) == 1
    assert len(list_rule_benchmarks(cfg, rid)) == 1


def test_post_reuse_matrix_and_retry_after_failure(cfg):
    client, app = _client(cfg)
    fake = FakeJobStore()
    app.state.astock.jobs = fake
    rid = _make_rule(cfg, "验证复用")["id"]

    r1 = client.post("/api/v1/rules/%s/benchmark" % rid, json={}).json()
    job1 = r1["job_id"]
    assert r1["reused"] is False

    # queued -> reuse
    assert client.post("/api/v1/rules/%s/benchmark" % rid, json={}).json()["reused"]
    # running -> reuse
    fake.records[job1].status = "running"
    assert client.post("/api/v1/rules/%s/benchmark" % rid, json={}).json()["reused"]
    # succeeded -> no reuse, new job
    fake.records[job1].status = "succeeded"
    fake.records[job1].run_id = "run_done"
    r2 = client.post("/api/v1/rules/%s/benchmark" % rid, json={}).json()
    assert r2["reused"] is False and r2["job_id"] != job1
    job2 = r2["job_id"]

    # failed old record -> retry allowed (new job)
    update_rule_benchmark(
        cfg, get_latest_rule_benchmark(cfg, rid)["id"], status="failed", error="old"
    )
    fake.records[job2].status = "failed"
    fake.records[job2].error = "boom"
    r3 = client.post("/api/v1/rules/%s/benchmark" % rid, json={}).json()
    assert r3["reused"] is False and r3["job_id"] not in (job1, job2)

    # lost in-memory job (service restart) -> new job, not a crash
    job3 = r3["job_id"]
    fake.records.pop(job3)
    r4 = client.post("/api/v1/rules/%s/benchmark" % rid, json={}).json()
    assert r4["reused"] is False and r4["job_id"] != job3
    assert len(fake.submits) == 4
    assert len(list_rule_benchmarks(cfg, rid)) == 4


def test_post_sidecar_runtime_fields_passthrough(cfg):
    """修复点 5：sidecar 白名单字段经校验后透传到 BacktestRequest。"""
    sidecar = Path(cfg.storage_root) / "benchmark_profile.json"
    sidecar.write_text(
        json.dumps(
            {
                "start": 20200101,
                "end": 20201231,
                "entry_lag": 2,
                "hold": 5,
                "account_mode": "per_symbol",
                "buy_on": "close",
                "sell_on": "open",
                "engine": "fast",
                "artifact_level": "summary",
                "signal_weekdays": [1, 3, 5],
                "buy_weekday": 5,
                "exit_weekday": 1,
                "codes": ["SSE.STK.600000", "SZSE.STK.000001"],
                "with_bagua": True,
                "stop_loss": 0.08,
                "take_profit": 0.2,
                "gua_filter": {"enabled": True, "mode": "best3"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client, app = _client(cfg)
    fake = FakeJobStore()
    app.state.astock.jobs = fake
    rid = _make_rule(cfg, "验证边车透传")["id"]

    r = client.post("/api/v1/rules/%s/benchmark" % rid, json={})
    assert r.status_code == 200, r.text
    req = fake.submits[0]
    assert req.codes == ["SSE.STK.600000", "SZSE.STK.000001"]
    assert req.start == 20200101 and req.end == 20201231
    assert req.entry_lag == 2 and req.hold == 5
    assert req.buy_on == "close" and req.sell_on == "open"
    assert req.account_mode == "per_symbol"
    assert req.engine == "fast" and req.artifact_level == "summary"
    assert req.signal_weekdays == [1, 3, 5]
    assert req.buy_weekday == 5 and req.exit_weekday == 1
    assert req.with_bagua is True
    assert req.stop_loss == 0.08 and req.take_profit == 0.2
    assert req.gua_filter == {"enabled": True, "mode": "best3"}

    # the persisted benchmark profile matches what was submitted
    bench_profile = get_latest_rule_benchmark(cfg, rid)["profile"]
    assert bench_profile["codes"] == ["SSE.STK.600000", "SZSE.STK.000001"]
    assert bench_profile["account_mode"] == "per_symbol"
    assert bench_profile["with_bagua"] is True

    # gua_filter must be copied, not aliased: mutating the sidecar file later
    # must not retroactively change the stored profile payload
    sidecar.write_text(json.dumps({"gua_filter": {"enabled": False}}), encoding="utf-8")
    assert req.gua_filter.get("enabled") is True


def test_custom_codes_universe_display_consistent(cfg, monkeypatch):
    """修复点 1：自定义股票池的展示字段在 GET profile / 落库快照 / performance 三处一致。"""
    codes = ["SSE.STK.600000", "SZSE.STK.000001"]
    sidecar = Path(cfg.storage_root) / "benchmark_profile.json"
    sidecar.write_text(json.dumps({"codes": codes}), encoding="utf-8")

    custom_label = "自定义股票池（2 只）"
    custom_quality = "自定义股票池 · 最新就绪数据（非点时宇宙）"

    profile = rb.build_benchmark_profile(cfg)
    assert profile["codes"] == codes
    assert profile["universe_label"] == custom_label
    assert profile["data_quality"] == custom_quality

    client, app = _client(cfg)
    fake = FakeJobStore()
    app.state.astock.jobs = fake

    # 1) GET /benchmark-profile
    route_profile = client.get("/api/v1/rules/benchmark-profile").json()["profile"]
    assert route_profile["universe_label"] == custom_label
    assert route_profile["data_quality"] == custom_quality

    # 2) POST -> request codes + persisted snapshot
    rid = _make_rule(cfg, "验证自定义池")["id"]
    r = client.post("/api/v1/rules/%s/benchmark" % rid, json={})
    assert r.status_code == 200, r.text
    assert fake.submits[0].codes == codes
    bench = get_latest_rule_benchmark(cfg, rid)
    assert bench["profile"]["universe_label"] == custom_label
    assert bench["profile"]["data_quality"] == custom_quality
    assert bench["profile"]["codes"] == codes

    # 3) performance payload
    update_rule_benchmark(cfg, bench["id"], run_id="run_uni", status="succeeded")
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: _summary(42, run_id))
    monkeypatch.setattr(
        rb, "load_equity_curve", lambda c, run_id, max_points=4000: _equity_two(run_id)
    )
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["validity"] == "valid"
    assert out["benchmark_profile"]["universe_label"] == custom_label
    assert out["benchmark_profile"]["data_quality"] == custom_quality
    assert out["benchmark_profile"]["codes"] == codes

    # ["ALL"] (or normalized ["all"]) keeps the全市场 label
    sidecar.write_text(json.dumps({"codes": ["all"]}), encoding="utf-8")
    all_profile = rb.build_benchmark_profile(cfg)
    assert all_profile["codes"] == ["ALL"]
    assert all_profile["universe_label"] == rb.DEFAULT_BENCHMARK_PROFILE["universe_label"]
    assert all_profile["data_quality"] == rb.DEFAULT_BENCHMARK_PROFILE["data_quality"]


# ===========================================================================
# d) performance state machine
# ===========================================================================


def test_perf_untested_exact_contract(cfg):
    """修复点 7：untested 也返回显式空指标/空净值字段。"""
    rid = _make_rule(cfg, "验证无记录")["id"]
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out == {
        "has_performance": False,
        "validity": "untested",
        "demo": False,
        "show_metrics": False,
        "show_chart": False,
        "metrics": {},
        "equity": [],
        "benchmark_equity": [],
    }


def test_perf_pending_queued_and_running_contract(cfg):
    """修复点 1：queued/running 返回 untested + benchmark_status，而不是 failed。"""
    for status, progress, expect in (
        ("queued", None, "排队"),
        ("running", None, "运行中"),
        (
            "running",
            {"pct": 42.0, "message": "信号计算中"},
            "运行中",
        ),
    ):
        rid = _make_rule(cfg, "验证待机_%s_%s" % (status, expect))["id"]
        create_rule_benchmark(cfg, rid, "job_%s" % status, None)
        jobs = FakeJobStore()
        jobs.records["job_%s" % status] = FakeJob(
            "job_%s" % status, status=status, progress=progress
        )
        out = rb.get_rule_performance(_ctx(cfg, jobs), rid)

        assert out["has_performance"] is False
        assert out["validity"] == "untested"
        assert out["benchmark_status"] == status
        assert out["benchmark_job_id"] == "job_%s" % status
        assert expect in out["message"]
        assert out["demo"] is False
        assert out["show_metrics"] is False and out["show_chart"] is False
        assert out["metrics"] == {} and out["equity"] == []
        assert out["benchmark_equity"] == []
        assert isinstance(out["updated_at"], int) and out["updated_at"] > 0
        assert "failed" != out["validity"]
        if progress:
            assert "42" in out["message"] and "信号计算中" in out["message"]


def test_perf_pending_reports_live_status_not_stale_db(cfg):
    """pending 契约必须反映 job 实时状态（DB 行可能仍是 queued）。"""
    rid = _make_rule(cfg, "验证实时状态")["id"]
    create_rule_benchmark(cfg, rid, "job_live", None)  # DB status = queued
    jobs = FakeJobStore()
    jobs.records["job_live"] = FakeJob("job_live", status="running")
    out = rb.get_rule_performance(_ctx(cfg, jobs), rid)
    assert get_latest_rule_benchmark(cfg, rid)["status"] == "queued"
    assert out["benchmark_status"] == "running"
    assert out["benchmark_job_id"] == "job_live"
    assert "运行中" in out["message"]


def test_perf_pending_message_updates_with_progress(cfg):
    """修复点 1：progress 变化时 message 实时更新。"""
    rid = _make_rule(cfg, "验证进度实时")["id"]
    create_rule_benchmark(cfg, rid, "job_prog", None)
    rec = FakeJob(
        "job_prog",
        status="running",
        progress={"pct": 10.0, "message": "拉取行情"},
    )
    jobs = FakeJobStore()
    jobs.records["job_prog"] = rec
    first = rb.get_rule_performance(_ctx(cfg, jobs), rid)["message"]
    assert "10" in first and "拉取行情" in first

    rec.progress = {"pct": 80.0, "message": "撮合中"}
    second = rb.get_rule_performance(_ctx(cfg, jobs), rid)["message"]
    assert "80" in second and "撮合中" in second
    assert second != first


def test_perf_pending_queued_message_uses_live_progress(cfg):
    """修复点 3：queued message 使用实时 queue_position/message。"""
    rid = _make_rule(cfg, "验证排队进度")["id"]
    create_rule_benchmark(cfg, rid, "job_qmsg", None)
    rec = FakeJob(
        "job_qmsg",
        status="queued",
        progress={"queue_position": 5, "message": "排队中（前面还有 4 个任务，并行 1/6）"},
    )
    jobs = FakeJobStore()
    jobs.records["job_qmsg"] = rec

    first = rb.get_rule_performance(_ctx(cfg, jobs), rid)["message"]
    assert "前面还有 4 个任务" in first

    rec.progress = {"queue_position": 1, "message": "排队中，即将开始（并行槽位空闲）"}
    second = rb.get_rule_performance(_ctx(cfg, jobs), rid)["message"]
    assert "即将开始" in second and second != first

    # no progress -> stable fallback (not an empty "（）")
    rec.progress = None
    fallback = rb.get_rule_performance(_ctx(cfg, jobs), rid)["message"]
    assert "排队中" in fallback and fallback.endswith("请稍后刷新")

    # note without a usable position is still surfaced
    rec.progress = {"queue_position": None, "message": "自定义排队说明"}
    assert "自定义排队说明" in rb.get_rule_performance(_ctx(cfg, jobs), rid)["message"]


def test_perf_valid_contract_and_ratio_passthrough(cfg, monkeypatch):
    rid = _make_rule(cfg, "验证有效")["id"]
    _record_run(cfg, rid, "run_valid")
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: _summary(42, run_id))
    monkeypatch.setattr(
        rb, "load_equity_curve", lambda c, run_id, max_points=4000: _equity_two(run_id)
    )
    out = rb.get_rule_performance(_ctx(cfg), rid)

    assert out["has_performance"] is True and out["validity"] == "valid"
    assert out["demo"] is False
    assert out["show_metrics"] is True and out["show_chart"] is True
    assert out["source_run_id"] == "run_valid"
    assert isinstance(out["updated_at"], int) and out["updated_at"] > 0
    assert out["benchmark_profile"]["end"] == 20260731
    assert set(out["metrics"]) == {
        "total_return",
        "annual_return",
        "max_drawdown",
        "win_rate",
        "sharpe",
        "n_round_trips",
    }
    # ratios stay ratios in JSON (no x100), drawdown stays negative
    assert out["metrics"]["total_return"] == 0.25
    assert out["metrics"]["win_rate"] == 0.55
    assert out["metrics"]["max_drawdown"] == -0.12
    assert out["metrics"]["n_round_trips"] == 42
    assert out["benchmark_equity"] == []
    assert "message" not in out and "detail" not in out


def test_perf_missing_metric_keys_still_viewable(cfg, monkeypatch):
    rid = _make_rule(cfg, "验证指标缺失")["id"]
    _record_run(cfg, rid, "run_sparse")
    monkeypatch.setattr(
        rb,
        "load_run_summary",
        lambda c, run_id: {"status": "ok", "metrics": {"n_round_trips": 42}},
    )
    monkeypatch.setattr(
        rb,
        "load_equity_curve",
        lambda c, run_id, max_points=4000: _equity_two(run_id),
    )
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["validity"] == "valid"
    assert out["metrics"]["win_rate"] is None
    assert out["show_metrics"] is True


def test_perf_validity_order_failed_then_no_trades_then_proxy(cfg, monkeypatch):
    # failed wins over valid metrics (summary status gate)
    rid_f = _make_rule(cfg, "验证顺序失败")["id"]
    _record_run(cfg, rid_f, "run_fail_order")
    monkeypatch.setattr(
        rb,
        "load_run_summary",
        lambda c, run_id: _summary(99, run_id, status="no_go"),
    )
    monkeypatch.setattr(
        rb, "load_equity_curve", lambda c, run_id, max_points=4000: _equity_two(run_id)
    )
    out_f = rb.get_rule_performance(_ctx(cfg), rid_f)
    assert out_f["validity"] == "failed"

    # no_trades wins over proxy_data (0 trades on a MIN60 rule)
    rule_p = _make_rule(cfg, "验证顺序代理", MIN60_FORMULA)
    _record_run(cfg, rule_p["id"], "run_proxy_zero")
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: _summary(0, run_id))
    out_p = rb.get_rule_performance(_ctx(cfg), rule_p["id"])
    assert out_p["validity"] == "no_trades"


def test_perf_no_trades_boundaries_and_negative(cfg, monkeypatch):
    rid = _make_rule(cfg, "验证零成交")["id"]
    for n in (0, -3, None):
        _record_run(cfg, rid, "run_nt_%s" % n)
        metrics = {"n_round_trips": n}
        monkeypatch.setattr(
            rb,
            "load_run_summary",
            lambda c, run_id, _m=metrics: {"status": "ok", "metrics": _m},
        )
        monkeypatch.setattr(
            rb, "load_equity_curve", lambda c, run_id, max_points=4000: []
        )
        out = rb.get_rule_performance(_ctx(cfg), rid)
        assert out["validity"] == "no_trades", n
        assert out["show_metrics"] is False and out["show_chart"] is False
        assert out["detail"] == "本次基准测试没有产生有效交易"
        assert out["benchmark_equity"] == []


def test_perf_non_numeric_n_round_trips_is_no_trades(cfg, monkeypatch):
    rid = _make_rule(cfg, "验证非数值笔数")["id"]
    _record_run(cfg, rid, "run_bad_n")
    monkeypatch.setattr(
        rb,
        "load_run_summary",
        lambda c, run_id: {
            "status": "ok",
            "metrics": {"n_round_trips": "abc", "total_return": 0.1},
        },
    )
    monkeypatch.setattr(rb, "load_equity_curve", lambda c, run_id, max_points=4000: [])
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["validity"] == "no_trades"


def test_run_cutoff_unparseable_dates_do_not_change_profile(cfg, monkeypatch):
    rid = _make_rule(cfg, "验证cutoff异常")["id"]
    profile = rb.build_benchmark_profile(cfg)
    profile["end"] = 20200101
    _record_run(cfg, rid, "run_bad_cutoff", profile=profile)

    bad_summary = {
        "status": "ok",
        "metrics": {
            "total_return": 0.1,
            "annual_return": 0.1,
            "max_drawdown": -0.1,
            "win_rate": 0.5,
            "sharpe": 1.0,
            "n_round_trips": 42,
        },
        "data_cutoff_date": "not-a-number",
        "meta": {"data_cutoff_date": object()},
        "repro": {"data_cutoff_date": ["nope"]},
        "end": "also-bad",
    }
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: bad_summary)
    monkeypatch.setattr(
        rb, "load_equity_curve", lambda c, run_id, max_points=4000: _equity_two(run_id)
    )
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["validity"] == "valid"
    assert out["benchmark_profile"]["end"] == 20200101


def test_run_cutoff_priority_run_end_then_min_then_cutoff(cfg, monkeypatch):
    """修复点 6：min(run end, cutoff) > min(请求 end, cutoff) > cutoff。"""
    rid = _make_rule(cfg, "验证cutoff覆盖")["id"]
    profile = rb.build_benchmark_profile(cfg)
    profile["end"] = 20200101
    _record_run(cfg, rid, "run_end_override", profile=profile)
    monkeypatch.setattr(
        rb, "load_equity_curve", lambda c, run_id, max_points=4000: _equity_two(run_id)
    )

    # 1) run end earlier than cutoff: run end wins
    summary = _summary(42, "run_end_override")
    summary["end"] = 20191231
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: summary)
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["benchmark_profile"]["end"] == 20191231

    # 1b) run end beyond available data: clamped by dataset cutoff
    summary1b = _summary(42, "run_end_override")
    summary1b["end"] = 20991231
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: summary1b)
    out1b = rb.get_rule_performance(_ctx(cfg), rid)
    assert out1b["benchmark_profile"]["end"] == 20260731

    # 2) no run end: min(requested 20200101, cutoff 20260731) = requested
    summary2 = _summary(42, "run_end_override")
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: summary2)
    out2 = rb.get_rule_performance(_ctx(cfg), rid)
    assert out2["benchmark_profile"]["end"] == 20200101

    # 3) cutoff earlier than request: min wins
    summary3 = _summary(42, "run_end_override", data_cutoff=20191201)
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: summary3)
    out3 = rb.get_rule_performance(_ctx(cfg), rid)
    assert out3["benchmark_profile"]["end"] == 20191201

    # 4) request end None: dataset cutoff becomes the reported end
    profile2 = rb.build_benchmark_profile(cfg)
    profile2["end"] = None
    _record_run(cfg, rid, "run_cut4", profile=profile2)
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: _summary(42, run_id))
    out4 = rb.get_rule_performance(_ctx(cfg), rid)
    assert out4["benchmark_profile"]["end"] == 20260731


def test_run_cutoff_falls_back_to_summary_end(cfg, monkeypatch):
    rid = _make_rule(cfg, "验证cutoff回退")["id"]
    _record_run(cfg, rid, "run_end_only")
    summary = _summary(42, "run_end_only")
    summary.pop("data_cutoff_date")
    summary["end"] = 20200101
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: summary)
    monkeypatch.setattr(
        rb, "load_equity_curve", lambda c, run_id, max_points=4000: _equity_two(run_id)
    )
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["benchmark_profile"]["end"] == 20200101


def test_perf_min_round_trips_boundary_30_and_profile_gate(cfg, monkeypatch):
    rid = _make_rule(cfg, "验证门槛")["id"]
    _record_run(cfg, rid, "run_gate")
    monkeypatch.setattr(
        rb, "load_equity_curve", lambda c, run_id, max_points=4000: _equity_two(run_id)
    )
    for n, expected in ((29, "insufficient_samples"), (30, "valid"), (31, "valid")):
        monkeypatch.setattr(
            rb, "load_run_summary", lambda c, run_id, _n=n: _summary(_n, run_id)
        )
        out = rb.get_rule_performance(_ctx(cfg), rid)
        assert out["validity"] == expected, n

    # profile sample gate overrides the default 30
    rid2 = _make_rule(cfg, "验证门槛覆盖")["id"]
    profile = rb.build_benchmark_profile(cfg)
    profile["sample_gates"] = {"min_round_trips": 2}
    _record_run(cfg, rid2, "run_gate2", profile=profile)
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: _summary(5, run_id))
    out2 = rb.get_rule_performance(_ctx(cfg), rid2)
    assert out2["validity"] == "valid"

    # gate invalid -> default 30
    rid3 = _make_rule(cfg, "验证门槛异常")["id"]
    profile3 = rb.build_benchmark_profile(cfg)
    profile3["sample_gates"] = {"min_round_trips": "oops"}
    _record_run(cfg, rid3, "run_gate3", profile=profile3)
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: _summary(5, run_id))
    assert rb.get_rule_performance(_ctx(cfg), rid3)["validity"] == "insufficient_samples"

    # gate not a dict -> default 30
    rid4 = _make_rule(cfg, "验证门槛非字典")["id"]
    profile4 = rb.build_benchmark_profile(cfg)
    profile4["sample_gates"] = "oops"
    _record_run(cfg, rid4, "run_gate4", profile=profile4)
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: _summary(5, run_id))
    assert rb.get_rule_performance(_ctx(cfg), rid4)["validity"] == "insufficient_samples"


def test_perf_proxy_data_takes_precedence_over_insufficient(cfg, monkeypatch):
    rule = _make_rule(cfg, "验证代理优先", MIN60_FORMULA)
    _record_run(cfg, rule["id"], "run_proxy_low")
    monkeypatch.setattr(
        rb, "load_run_summary", lambda c, run_id: _summary(5, run_id)
    )
    monkeypatch.setattr(
        rb, "load_equity_curve", lambda c, run_id, max_points=4000: _equity_two(run_id)
    )
    out = rb.get_rule_performance(_ctx(cfg), rule["id"])
    assert out["validity"] == "proxy_data"
    assert out["show_metrics"] is True
    assert "60 分钟" in (out.get("message") or "")


def test_perf_run_artifacts_missing_failed_with_source_run_id(cfg, monkeypatch):
    rid = _make_rule(cfg, "验证产物缺失")["id"]
    _record_run(cfg, rid, "run_missing")

    def _boom(c, run_id):
        raise FileNotFoundError(run_id)

    monkeypatch.setattr(rb, "load_run_summary", _boom)
    monkeypatch.setattr(rb, "load_equity_curve", lambda c, run_id, max_points=4000: [])
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["validity"] == "failed"
    assert out["source_run_id"] == "run_missing"
    assert out["show_metrics"] is False and out["show_chart"] is False
    assert out["benchmark_equity"] == []
    assert "不可读" in out["message"]


def test_perf_summary_no_go_detail_propagates(cfg, monkeypatch):
    rid = _make_rule(cfg, "验证失败原因")["id"]
    _record_run(cfg, rid, "run_nogo")
    monkeypatch.setattr(
        rb,
        "load_run_summary",
        lambda c, run_id: {
            "status": "no_go",
            "metrics": {},
            "meta": {"reason": "全市场无信号"},
            "repro": {},
        },
    )
    monkeypatch.setattr(rb, "load_equity_curve", lambda c, run_id, max_points=4000: [])
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["validity"] == "failed"
    assert "全市场无信号" in (out.get("detail") or "")


def test_perf_lazy_sync_job_states(cfg):
    # succeeded with run_id -> valid + persisted
    rid_ok = _make_rule(cfg, "验证同步成功")["id"]
    create_rule_benchmark(cfg, rid_ok, "job_ok", None)
    jobs_ok = FakeJobStore()
    jobs_ok.records["job_ok"] = FakeJob("job_ok", status="succeeded", run_id="run_ok")

    import unittest.mock as mock

    with mock.patch.object(
        rb, "load_run_summary", lambda c, run_id: _summary(42, run_id)
    ), mock.patch.object(
        rb, "load_equity_curve", lambda c, run_id, max_points=4000: _equity_two(run_id)
    ):
        out = rb.get_rule_performance(_ctx(cfg, jobs_ok), rid_ok)
    assert out["validity"] == "valid" and out["source_run_id"] == "run_ok"
    row = get_latest_rule_benchmark(cfg, rid_ok)
    assert row["run_id"] == "run_ok" and row["status"] == "succeeded"

    # failed job -> failed + error persisted
    rid_bad = _make_rule(cfg, "验证同步失败")["id"]
    create_rule_benchmark(cfg, rid_bad, "job_bad", None)
    jobs_bad = FakeJobStore()
    jobs_bad.records["job_bad"] = FakeJob("job_bad", status="failed", error="boom")
    out_bad = rb.get_rule_performance(_ctx(cfg, jobs_bad), rid_bad)
    assert out_bad["validity"] == "failed" and "boom" in out_bad.get("detail", "")
    assert get_latest_rule_benchmark(cfg, rid_bad)["status"] == "failed"

    # cancelled job -> failed with 用户取消
    rid_cancel = _make_rule(cfg, "验证同步取消")["id"]
    create_rule_benchmark(cfg, rid_cancel, "job_cancel", None)
    jobs_cancel = FakeJobStore()
    jobs_cancel.records["job_cancel"] = FakeJob("job_cancel", status="cancelled")
    out_cancel = rb.get_rule_performance(_ctx(cfg, jobs_cancel), rid_cancel)
    assert out_cancel["validity"] == "failed"
    assert "用户取消" in (out_cancel.get("detail") or "")
    assert get_latest_rule_benchmark(cfg, rid_cancel)["status"] == "failed"


def test_perf_succeeded_job_without_run_id_reports_failure(cfg):
    rid = _make_rule(cfg, "验证无run_id")["id"]
    create_rule_benchmark(cfg, rid, "job_norun", None)
    jobs = FakeJobStore()
    jobs.records["job_norun"] = FakeJob("job_norun", status="succeeded", run_id=None)
    out = rb.get_rule_performance(_ctx(cfg, jobs), rid)
    assert out["validity"] == "failed"
    assert "未产生结果" in (out.get("detail") or "")
    assert get_latest_rule_benchmark(cfg, rid)["status"] == "succeeded"


def test_perf_missing_job_but_succeeded_status_reports_no_result(cfg):
    # DB says succeeded but the in-memory job vanished without a run_id: the
    # KeyError sync must not flip it to the restart message; the record simply
    # reports that no result was produced.
    rid = _make_rule(cfg, "验证丢失成功态")["id"]
    bench_id = create_rule_benchmark(cfg, rid, "job_gone_success", None)
    update_rule_benchmark(cfg, bench_id, status="succeeded")
    out = rb.get_rule_performance(_ctx(cfg, FakeJobStore()), rid)
    assert out["validity"] == "failed"
    assert "未产生结果" in (out.get("detail") or "")
    assert "服务重启" not in ((out.get("message") or "") + (out.get("detail") or ""))


def test_sync_from_job_empty_job_id_is_noop(cfg):
    bench = {"id": 1, "job_id": "", "status": "queued", "updated_at": 123}
    assert rb._sync_from_job(_ctx(cfg), bench) is bench


def test_perf_job_keyerror_marks_service_restart_failure(cfg):
    rid = _make_rule(cfg, "验证服务重启")["id"]
    create_rule_benchmark(cfg, rid, "job_lost", None)
    out = rb.get_rule_performance(_ctx(cfg, FakeJobStore()), rid)
    assert out["validity"] == "failed"
    text = (out.get("message") or "") + (out.get("detail") or "")
    assert "服务重启" in text
    row = get_latest_rule_benchmark(cfg, rid)
    assert row["status"] == "failed" and "服务重启" in (row["error"] or "")


def test_perf_generic_jobstore_error_does_not_crash(cfg):
    rid = _make_rule(cfg, "验证存储异常")["id"]
    create_rule_benchmark(cfg, rid, "job_io", None)

    class ExplodingJobs:
        def get(self, job_id):
            raise RuntimeError("io failure")

        def submit(self, req):  # pragma: no cover - not used here
            raise AssertionError("submit should not be called")

    out = rb.get_rule_performance(_ctx(cfg, ExplodingJobs()), rid)
    assert out["has_performance"] is True
    assert out["validity"] == "failed"
    assert "状态不可用" in (out.get("detail") or "")


def test_perf_equity_gate_requires_two_positive_finite_points(cfg, monkeypatch):
    rid = _make_rule(cfg, "验证净值门槛")["id"]

    cases = (
        ([{"equity": 1.0}, {"equity": 0.0}], False),
        ([{"equity": -1.0}, {"equity": 1.2}], False),
        ([{"equity": "nan"}, {"equity": 1.2}], False),
        ([{"equity": 1.0}, {"equity": "1.2"}], True),
        ([{"not_equity": 1.0}, {"equity": 1.2}], False),
        ([1.0, {"equity": 1.2}], False),
        ([1.0, {"equity": 1.2}, {"equity": 1.3}], True),
        ("garbage-not-a-list", False),
    )
    for n, (equity, expected) in enumerate(cases):
        run_id = "run_eq_%d" % n
        create_rule_benchmark(cfg, rid, "job_" + run_id, None)
        update_rule_benchmark(
            cfg, get_latest_rule_benchmark(cfg, rid)["id"], run_id=run_id, status="succeeded"
        )
        monkeypatch.setattr(
            rb, "load_run_summary", lambda c, run_id: _summary(42, run_id)
        )
        monkeypatch.setattr(
            rb,
            "load_equity_curve",
            lambda c, run_id, max_points=4000, _e=equity: _e if isinstance(_e, list) else [],
        )
        out = rb.get_rule_performance(_ctx(cfg), rid)
        assert out["validity"] == "valid"
        assert out["show_chart"] is expected, (n, equity)


# ===========================================================================
# e) min60_proxy_note / rule payload
# ===========================================================================


def test_rule_public_min60_proxy_note_lifecycle(cfg):
    svc = RuleService(cfg)
    m60 = svc.create_rule(name="验证代理说明", formula_text=MIN60_FORMULA)
    assert m60["min60_day_proxy"] is True
    assert m60["min60_proxy_note"]

    plain = svc.create_rule(name="验证普通说明", formula_text="XG:C>0;")
    assert plain["min60_proxy_note"] == ""

    updated = svc.update_rule(m60["id"], formula_text="XG:C>0;")
    assert updated["min60_proxy_note"] == ""
    assert updated["min60_day_proxy"] is False


# ===========================================================================
# f) frontend contract: feed real backend JSON into the shipped panel
# ===========================================================================


def test_frontend_panel_consumes_real_backend_payloads(cfg, monkeypatch, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    assert V3.is_file() and HARNESS.is_file()

    summaries = {
        "run_js_v": _summary(42, "run_js_v"),
        "run_js_no": _summary(0, "run_js_no"),
        "run_js_ins": _summary(5, "run_js_ins"),
        "run_js_p": _summary(42, "run_js_p"),
    }
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: summaries[run_id])
    monkeypatch.setattr(
        rb,
        "load_equity_curve",
        lambda c, run_id, max_points=4000: (
            [] if run_id == "run_js_no" else _equity_two(run_id)
        ),
    )

    ctx = _ctx(cfg)
    cases = []

    rid_u = _make_rule(cfg, "js未测试")["id"]
    cases.append(
        {
            "name": "real-untested",
            "data": rb.get_rule_performance(ctx, rid_u),
            "expect": ["暂无基准回测数据"],
            "forbid": ["加载失败", "测试失败"],
        }
    )

    rid_v = _make_rule(cfg, "js有效")["id"]
    _record_run(cfg, rid_v, "run_js_v")
    cases.append(
        {
            "name": "real-valid",
            "data": rb.get_rule_performance(ctx, rid_v),
            "expect": ["有效绩效", "polyline", "查看完整回测"],
            "forbid": ["加载失败", "测试失败"],
        }
    )

    rid_n = _make_rule(cfg, "js零成交")["id"]
    _record_run(cfg, rid_n, "run_js_no")
    cases.append(
        {
            "name": "real-no-trades",
            "data": rb.get_rule_performance(ctx, rid_n),
            "expect": ["样本无效"],
            "forbid": ["加载失败"],
        }
    )

    rid_i = _make_rule(cfg, "js样本不足")["id"]
    _record_run(cfg, rid_i, "run_js_ins")
    cases.append(
        {
            "name": "real-insufficient",
            "data": rb.get_rule_performance(ctx, rid_i),
            "expect": ["样本不足"],
            "forbid": ["加载失败"],
        }
    )

    rid_p = _make_rule(cfg, "js代理", MIN60_FORMULA)["id"]
    _record_run(cfg, rid_p, "run_js_p")
    cases.append(
        {
            "name": "real-proxy",
            "data": rb.get_rule_performance(ctx, rid_p),
            "expect": ["代理数据"],
            "forbid": ["加载失败"],
        }
    )

    rid_f = _make_rule(cfg, "js失败")["id"]
    _record_run(cfg, rid_f, "run_js_missing")
    cases.append(
        {
            "name": "real-failed",
            "data": rb.get_rule_performance(ctx, rid_f),
            "expect": ["测试失败"],
            "forbid": ["加载失败"],
        }
    )

    rid_q = _make_rule(cfg, "js运行中")["id"]
    create_rule_benchmark(cfg, rid_q, "job_js_q", None)
    jobs = FakeJobStore()
    jobs.records["job_js_q"] = FakeJob(
        "job_js_q", status="running", progress={"pct": 37.0, "message": "信号计算中"}
    )
    running = rb.get_rule_performance(_ctx(cfg, jobs), rid_q)
    assert running["validity"] == "untested"
    assert running["benchmark_status"] == "running"
    cases.append(
        {
            "name": "real-running",
            "data": running,
            "expect": ["运行中", "刷新状态", "信号计算中", "37"],
            "forbid": ["加载失败", "测试失败"],
        }
    )

    rid_queue = _make_rule(cfg, "js排队中")["id"]
    create_rule_benchmark(cfg, rid_queue, "job_js_q2", None)
    jobs2 = FakeJobStore()
    jobs2.records["job_js_q2"] = FakeJob("job_js_q2", status="queued")
    queued = rb.get_rule_performance(_ctx(cfg, jobs2), rid_queue)
    assert queued["validity"] == "untested"
    assert queued["benchmark_status"] == "queued"
    cases.append(
        {
            "name": "real-queued",
            "data": queued,
            "expect": ["排队中", "刷新状态"],
            "forbid": ["加载失败", "测试失败"],
        }
    )

    payload_file = tmp_path / "perf_payloads.json"
    payload_file.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

    proc = subprocess.run(
        [node, str(HARNESS), str(V3), str(payload_file)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert proc.stdout.count("PASS js-contract") == len(cases) + 1  # + error marker


def test_frontend_panel_builtin_matrix_when_node_available():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, str(HARNESS), str(V3)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    for marker in (
        "PASS js-contract pending-running",
        "PASS js-contract pending-queued",
        "PASS js-contract custom-codes-meta",
        # third-round frontend lifecycle scenarios
        "PASS js-scenario drawer-closed-no-autorefresh",
        "PASS js-scenario autorefresh-silent-when-open",
        "PASS js-scenario drawer-close-clears-timer",
        "PASS js-scenario native-min60-no-post",
        "PASS js-scenario plain-rule-posts",
    ):
        assert marker in proc.stdout, marker


def test_frontend_drawer_lifecycle_static_guards():
    """第三轮前端修复的静态核对（node 场景之外的兜底结构断言）。"""
    html = V3.read_text(encoding="utf-8")
    # drawer open flag is driven by renderRuleDetail
    assert "AppState.ruleDrawerOpen = !!rule;" in html
    # close branch clears the auto-refresh timer
    assert "if (perfBox && perfBox.__perfAutoTimer) {" in html
    assert "clearTimeout(perfBox.__perfAutoTimer);" in html
    # auto-refresh callback and benchmark job poll both gate on drawer state
    assert (
        "if (!AppState.ruleDrawerOpen || !AppState.activeRule "
        "|| AppState.activeRule.id !== rule.id) return;" in html
    )
    # 8s auto refresh is silent (no loading flash)
    assert "loadRulePerformance(rule, { silent: true })" in html
    assert "if (!opts.silent) renderRulePerformancePanel(rule, { loading: true });" in html
    # native MIN60 pre-check in runRuleBenchmark happens before the POST
    native_idx = html.index("if (rule.min60_native) {")
    assert native_idx > 0
    post_idx = html.index("/benchmark", native_idx)
    assert native_idx < post_idx
    assert "该规则依赖原生 60 分钟数据，暂不支持基准回测" in html


# ===========================================================================
# g) DB: legacy auto-create, repeat/concurrent submit
# ===========================================================================


def test_legacy_db_without_rule_benchmark_table_gets_it_on_init(cfg):
    from wtpy.apps.astock.service import db as dbmod

    path = dbmod.db_path(cfg)
    conn = sqlite3.connect(str(path))
    conn.executescript(dbmod.SCHEMA_SQL)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
        (str(dbmod._SCHEMA_VERSION),),
    )
    conn.commit()
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }
    conn.close()
    assert "rule_benchmarks" not in tables
    assert "idx_rule_benchmarks_rule" not in tables

    init_db(cfg)
    conn = connect(cfg)
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "rule_benchmarks" in names
    assert "idx_rule_benchmarks_rule" in names

    bench_id = create_rule_benchmark(cfg, "legacy_rule", "job_legacy", {"k": 1})
    assert bench_id > 0
    assert get_latest_rule_benchmark(cfg, "legacy_rule")["profile"] == {"k": 1}


def test_update_rule_benchmark_coalesce_keeps_existing_values(cfg):
    bench_id = create_rule_benchmark(cfg, "coalesce_rule", "job_c", {"x": 1})
    update_rule_benchmark(cfg, bench_id, run_id="run_c", status="succeeded")
    update_rule_benchmark(cfg, bench_id, error="late warning")
    row = get_latest_rule_benchmark(cfg, "coalesce_rule")
    assert row["job_id"] == "job_c"
    assert row["run_id"] == "run_c"
    assert row["status"] == "succeeded"
    assert row["error"] == "late warning"
    assert row["profile"] == {"x": 1}


def test_concurrent_first_submit_creates_single_job(cfg):
    """修复点 3：4 线程同时首提，模块级锁保证 1 job / 1 记录 / 其余复用。

    与旧 P2 复现不同：不在 submit 内部放 barrier（那要求两个线程同时进入
    submit，与串行化互斥），只在 worker 进入前对齐起点并加宽 submit 窗口。
    """
    rid = _make_rule(cfg, "验证并发首提")["id"]
    fake = FakeJobStore()
    fake.before_submit = lambda: time.sleep(0.05)  # widen the old race window
    ctx = _ctx(cfg, fake)
    barrier = threading.Barrier(4)
    results, errors = [], []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait(timeout=10)
            out = rb.submit_rule_benchmark(ctx, rid)
            with lock:
                results.append(out)
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    assert len(results) == 4
    assert len(fake.submits) == 1, "concurrent first submit enqueued %d jobs" % len(
        fake.submits
    )
    assert len(list_rule_benchmarks(cfg, rid)) == 1
    assert sum(1 for r in results if r.get("reused")) == 3
    assert sum(1 for r in results if not r.get("reused")) == 1


def test_submit_db_failure_cancels_orphan_job(cfg, monkeypatch):
    """修复点 3：落库失败必须补偿取消已提交 job，不留下孤儿任务。"""
    rid = _make_rule(cfg, "验证落库补偿")["id"]
    fake = FakeJobStore()
    ctx = _ctx(cfg, fake)

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(rb, "create_rule_benchmark", _boom)
    with pytest.raises(RuntimeError, match="db down"):
        rb.submit_rule_benchmark(ctx, rid)

    assert len(fake.submits) == 1
    assert fake.cancelled == ["job_fake_1"]
    assert fake.records["job_fake_1"].status == "cancelled"
    assert get_latest_rule_benchmark(cfg, rid) is None


# ===========================================================================
# h) second-round hardening (queue / audit / alias)
# ===========================================================================


def test_queue_full_boundary_429_and_reuse_bypass(cfg):
    """修复点 10：n_queued>=50 → 429；已有活跃任务的复用先于容量检查。"""
    client, app = _client(cfg)
    fake = FakeJobStore()
    app.state.astock.jobs = fake
    rid = _make_rule(cfg, "验证队列容量")["id"]

    fake.queue_size = rb.MAX_QUEUED_BENCHMARKS - 1  # 49 -> allowed
    r49 = client.post("/api/v1/rules/%s/benchmark" % rid, json={})
    assert r49.status_code == 200, r49.text
    job_id = r49.json()["job_id"]
    assert fake.records[job_id].status == "queued"

    # reuse of the live queued job is checked before capacity -> still 200
    fake.queue_size = rb.MAX_QUEUED_BENCHMARKS
    reuse = client.post("/api/v1/rules/%s/benchmark" % rid, json={})
    assert reuse.status_code == 200, reuse.text
    assert reuse.json()["reused"] is True
    assert reuse.json()["job_id"] == job_id
    assert len(fake.submits) == 1

    # a fresh rule is rejected with 429 and nothing is enqueued
    rid2 = _make_rule(cfg, "验证队列满拒绝")["id"]
    r429 = client.post("/api/v1/rules/%s/benchmark" % rid2, json={})
    assert r429.status_code == 429, r429.text
    assert "队列已满" in r429.json()["detail"]
    assert len(fake.submits) == 1
    assert get_latest_rule_benchmark(cfg, rid2) is None

    # service-level type check for the dedicated exception
    ctx = _ctx(cfg, fake)
    with pytest.raises(rb.BenchmarkQueueFullError):
        rb.submit_rule_benchmark(ctx, rid2)


def test_tn6_unconfirmed_rejected_400_then_confirmed_enqueues(cfg):
    """修复点 8：tn6 package 未确认公式在入队前 400；确认后可提交。"""
    from wtpy.apps.astock.indicators.tn6_importer import (
        confirm_source_pair,
        file_sha256,
        load_source_map,
        pair_source,
        save_source_map,
    )

    pkg = Path(cfg.indicator_dir) / "audit_probe.tn6"
    pkg.write_bytes(b"audit-package")
    src = Path(cfg.indicator_dir) / "audit_probe.txt"
    src.write_text("XG:C>0;\n", encoding="utf-8")
    sha = file_sha256(pkg)
    mapping = {}
    pair_source(mapping, sha, src, package_file=pkg)
    save_source_map(cfg.mapping_path, mapping)

    client, app = _client(cfg)
    fake = FakeJobStore()
    app.state.astock.jobs = fake

    r = client.post("/api/v1/rules/tn6_audit_probe/benchmark", json={})
    assert r.status_code == 400, r.text
    assert "未确认" in r.json()["detail"]
    assert fake.submits == []
    assert get_latest_rule_benchmark(cfg, "tn6_audit_probe") is None

    confirm_source_pair(cfg.mapping_path, sha, confirmed_by="verifier")
    assert (
        load_source_map(cfg.mapping_path)[sha]["source_pair_status"]
        == "paired_confirmed"
    )
    r2 = client.post("/api/v1/rules/tn6_audit_probe/benchmark", json={})
    assert r2.status_code == 200, r2.text
    assert len(fake.submits) == 1


def test_perf_alias_resolves_to_canonical_rule_id(cfg, monkeypatch):
    """修复点 4：按显示名/别名访问 performance 也能命中同一条记录。"""
    from urllib.parse import quote

    rule = _make_rule(cfg, "验证别名命中")
    _record_run(cfg, rule["id"], "run_alias")
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: _summary(42, run_id))
    monkeypatch.setattr(
        rb, "load_equity_curve", lambda c, run_id, max_points=4000: _equity_two(run_id)
    )

    out = rb.get_rule_performance(_ctx(cfg), rule["name"])
    assert out["validity"] == "valid"
    assert out["source_run_id"] == "run_alias"

    client, app = _client(cfg)
    app.state.astock.jobs = FakeJobStore()
    body = client.get(
        "/api/v1/rules/%s/performance" % quote(str(rule["name"]), safe="")
    ).json()
    assert body["validity"] == "valid"
    assert body["source_run_id"] == "run_alias"
