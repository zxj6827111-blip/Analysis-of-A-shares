# -*- coding: utf-8 -*-
"""规则标准化基准回测：profile / submit / performance 前端契约测试。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.api import create_app
from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.service import rule_benchmark as rb
from wtpy.apps.astock.service.db import (
    create_rule_benchmark,
    get_latest_rule_benchmark,
    get_rule_benchmark_by_job,
    list_rule_benchmarks,
    update_rule_benchmark,
)
from wtpy.apps.astock.service.rules import RuleService

MIN60_FORMULA = 'DIF60:="MACD.DIF#MIN60";\nXG:C>0 AND DIF60>0;'


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> "object":
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


class _FakeJob:
    def __init__(self, job_id, status="queued", run_id=None, error=None, progress=None):
        self.job_id = job_id
        self.status = status
        self.run_id = run_id
        self.error = error
        self.progress = progress


class FakeJobStore:
    def __init__(self):
        self.records = {}
        self.submits = []
        self.queue_size = 0
        self.before_submit = None

    def submit(self, req):
        if self.before_submit is not None:
            self.before_submit()
        self.submits.append(req)
        rec = _FakeJob("job_fake_%d" % len(self.submits))
        self.records[rec.job_id] = rec
        return rec

    def get(self, job_id):
        if job_id not in self.records:
            raise KeyError(job_id)
        return self.records[job_id]

    def cancel(self, job_id):
        rec = self.get(job_id)
        rec.status = "cancelled"
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
    return TestClient(app), app, cfg


def _make_rule(cfg, name="基准规则", formula="XG:C>0;"):
    return RuleService(cfg).create_rule(name=name, formula_text=formula)


def _record_run(cfg, rule_id, run_id="run_x", metrics=None, profile=None):
    bench_id = create_rule_benchmark(
        cfg,
        rule_id,
        "job_" + run_id,
        profile if profile is not None else rb.build_benchmark_profile(cfg),
    )
    update_rule_benchmark(cfg, bench_id, run_id=run_id, status="succeeded")
    return bench_id


def _write_sidecar(cfg, data):
    path = Path(cfg.storage_root) / "benchmark_profile.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _metric_summary(n_round_trips, run_id="run_x", status="ok", extra=None):
    metrics = {
        "total_return": 0.25,
        "annual_return": 0.18,
        "max_drawdown": -0.12,
        "win_rate": 0.55,
        "sharpe": 1.4,
        "n_round_trips": n_round_trips,
    }
    metrics.update(extra or {})
    return {
        "run_id": run_id,
        "status": status,
        "metrics": metrics,
        "data_cutoff_date": 20260731,
        "meta": {},
        "repro": {},
    }


def _two_point_equity(run_id):
    return [
        {"date": 20260101, "cash": 1.0, "market_value": 0.0, "equity": 1.0},
        {"date": 20260102, "cash": 1.1, "market_value": 0.0, "equity": 1.1},
    ]


# --- profile ---------------------------------------------------------------


def test_build_benchmark_profile_defaults(cfg, monkeypatch):
    monkeypatch.setattr(rb, "resolve_data_max_date", lambda c: None)
    profile = rb.build_benchmark_profile(cfg)
    assert profile["profile_id"] == "rule_benchmark_v1"
    assert profile["version"] == 1
    assert profile["universe_label"]
    assert profile["codes"] == ["ALL"]
    assert profile["period"] == "DAY"
    assert profile["start"] == 20180101
    assert profile["end"] is None
    assert profile["account_mode"] == "portfolio"
    assert profile["entry_lag"] == 1
    assert profile["hold"] == 1
    assert profile["buy_on"] == "open"
    assert profile["sell_on"] == "close"
    assert profile["signal_weekdays"] is None
    assert profile["buy_weekday"] is None
    assert profile["exit_weekday"] is None
    assert profile["with_bagua"] is False
    assert profile["stop_loss"] is None
    assert profile["take_profit"] is None
    assert profile["engine"] == "full"
    assert profile["artifact_level"] == "full"
    assert profile["data_quality"]
    assert profile["fee_note"]
    assert profile["gua_filter"] == {"enabled": False}
    assert profile["benchmark_index"] is None
    assert profile["sample_gates"] == {"min_round_trips": 30}


def test_profile_end_resolved_and_sidecar_shallow_merge(cfg, monkeypatch):
    monkeypatch.setattr(rb, "resolve_data_max_date", lambda c: 20260731)
    profile = rb.build_benchmark_profile(cfg)
    assert profile["end"] == 20260731

    sidecar = Path(cfg.storage_root) / "benchmark_profile.json"
    sidecar.write_text(
        json.dumps(
            {
                "hold": 5,
                "sample_gates": {"min_round_trips": 2},
                "codes": ["ALL", "SSE.STK.600000"],
            }
        ),
        encoding="utf-8",
    )
    merged = rb.build_benchmark_profile(cfg)
    assert merged["hold"] == 5
    assert merged["sample_gates"]["min_round_trips"] == 2
    assert merged["codes"] == ["ALL", "SSE.STK.600000"]
    assert merged["end"] == 20260731

    sidecar.write_text("{broken json", encoding="utf-8")
    fallback = rb.build_benchmark_profile(cfg)
    assert fallback["hold"] == 1
    assert fallback["sample_gates"] == {"min_round_trips": 30}


def test_resolve_data_max_date_empty_root_returns_none(cfg):
    assert rb.resolve_data_max_date(cfg) is None


def test_profile_sidecar_custom_codes_display_consistent(cfg, monkeypatch):
    monkeypatch.setattr(rb, "resolve_data_max_date", lambda c: None)
    _write_sidecar(cfg, {"codes": ["600000", "000001"]})

    profile = rb.build_benchmark_profile(cfg)
    assert profile["codes"] == ["600000", "000001"]
    assert profile["universe_label"] == "自定义股票池（2 只）"
    assert profile["data_quality"] == "自定义股票池 · 最新就绪数据（非点时宇宙）"

    client, app, _cfg = _client(cfg)
    route = client.get("/api/v1/rules/benchmark-profile").json()["profile"]
    assert route["codes"] == profile["codes"]
    assert route["universe_label"] == profile["universe_label"]
    assert route["data_quality"] == profile["data_quality"]

    fake = FakeJobStore()
    app.state.astock.jobs = fake
    rid = _make_rule(cfg, name="自定义池落库")["id"]
    r = client.post("/api/v1/rules/%s/benchmark" % rid, json={})
    assert r.status_code == 200, r.text
    assert fake.submits[0].codes == ["600000", "000001"]
    stored = get_latest_rule_benchmark(cfg, rid)["profile"]
    assert stored["codes"] == profile["codes"]
    assert stored["universe_label"] == profile["universe_label"]
    assert stored["data_quality"] == profile["data_quality"]

    _record_run(cfg, rid, run_id="run_custom_pool", profile=profile)
    monkeypatch.setattr(
        rb, "load_run_summary", lambda c, run_id: _metric_summary(42, run_id=run_id)
    )
    monkeypatch.setattr(
        rb,
        "load_equity_curve",
        lambda c, run_id, max_points=4000: _two_point_equity(run_id),
    )
    perf = rb.get_rule_performance(_ctx(cfg), rid)
    assert perf["benchmark_profile"]["codes"] == profile["codes"]
    assert perf["benchmark_profile"]["universe_label"] == profile["universe_label"]
    assert perf["benchmark_profile"]["data_quality"] == profile["data_quality"]


def test_profile_sidecar_all_market_keeps_default_display(cfg, monkeypatch):
    monkeypatch.setattr(rb, "resolve_data_max_date", lambda c: None)
    for codes in ([], ["ALL"], ["all"]):
        _write_sidecar(cfg, {"codes": codes})
        profile = rb.build_benchmark_profile(cfg)
        assert profile["codes"] == ["ALL"]
        assert profile["universe_label"] == rb.DEFAULT_BENCHMARK_PROFILE["universe_label"]
        assert profile["data_quality"] == rb.DEFAULT_BENCHMARK_PROFILE["data_quality"]


def test_benchmark_profile_route_not_captured_as_rule_id(cfg, monkeypatch):
    monkeypatch.setattr(rb, "resolve_data_max_date", lambda c: None)
    client, _app, _cfg = _client(cfg)
    r = client.get("/api/v1/rules/benchmark-profile")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "profile" in body
    profile = body["profile"]
    assert profile["period"] == "DAY"
    assert profile["account_mode"] == "portfolio"
    assert profile["gua_filter"] == {"enabled": False}
    assert profile["end"] is None


# --- submit ----------------------------------------------------------------


def test_benchmark_unknown_rule_404(cfg):
    client, _app, _cfg = _client(cfg)
    assert client.post("/api/v1/rules/does_not_exist/benchmark", json={}).status_code == 404
    assert client.get("/api/v1/rules/does_not_exist/performance").status_code == 404


def _spec(**overrides):
    from wtpy.apps.astock.indicators.models import IndicatorSpec

    base = {
        "id": "unit_rule",
        "name": "unit",
        "version": "v1",
        "kind": "tdx_formula",
        "output_type": "signal",
        "supported_periods": ("DAY",),
        "compile_status": "ready",
    }
    base.update(overrides)
    return IndicatorSpec(**base)


def test_validate_rule_gates_min1_and_min60_without_proxy():
    rb.validate_rule_for_benchmark(_spec())
    rb.validate_rule_for_benchmark(
        _spec(dependencies=["MIN60"], parameters={"min60_day_proxy": True})
    )
    assert rb.needs_research_proxy(
        _spec(dependencies=["MIN60"], parameters={"min60_day_proxy": True})
    )
    assert rb.needs_research_proxy(_spec(parameters={"min60_day_proxy": True}))
    assert not rb.needs_research_proxy(_spec())
    # native MIN60 is rejected outright and is never treated as a proxy.
    assert not rb.needs_research_proxy(
        _spec(dependencies=["MIN60"], parameters={"min60_native": True})
    )
    assert not rb.needs_research_proxy(_spec(dependencies=["MIN60"]))

    for bad in (
        _spec(compile_status="invalid"),
        _spec(compile_status="source_required"),
        _spec(output_type="classification"),
        _spec(id="bagua_ohlc"),
        _spec(dependencies=["MIN1"]),
        _spec(dependencies=["MIN60"]),
    ):
        with pytest.raises(ValueError):
            rb.validate_rule_for_benchmark(bad)

    with pytest.raises(ValueError) as ei:
        rb.validate_rule_for_benchmark(
            _spec(dependencies=["MIN60"], parameters={"min60_native": True})
        )
    assert "原生 60 分钟" in str(ei.value)


def test_benchmark_rejects_unbacktestable_rule(cfg):
    client, _app, _cfg = _client(cfg)
    assert client.post("/api/v1/rules/bagua_ohlc/benchmark", json={}).status_code == 400

    (Path(cfg.indicator_dir) / "pkg.tn6").write_bytes(b"tn6-package")
    r = client.post("/api/v1/rules/tn6_pkg/benchmark", json={})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]


def test_benchmark_min60_needs_confirmation_then_submits(cfg):
    client, app, _cfg = _client(cfg)
    rule = _make_rule(cfg, name="m60基准", formula=MIN60_FORMULA)
    rid = rule["id"]
    assert rule["min60_day_proxy"] is True
    assert rule["min60_proxy_note"]

    fake = FakeJobStore()
    app.state.astock.jobs = fake

    r = client.post("/api/v1/rules/%s/benchmark" % rid, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "needs_confirmation"
    assert body["message"]
    assert body["detail"]
    assert fake.submits == []

    r2 = client.post(
        "/api/v1/rules/%s/benchmark" % rid,
        json={"allow_research_proxy": True},
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["job_id"] == "job_fake_1"
    assert body2["research_proxy"] is True
    assert body2["reused"] is False

    submitted = fake.submits[0]
    assert submitted.rule_ids == [rid]
    assert submitted.codes == ["ALL"]
    assert submitted.period == "DAY"
    assert submitted.account_mode == "portfolio"
    assert submitted.buy_on == "open"
    assert submitted.sell_on == "close"
    assert submitted.entry_lag == 1
    assert submitted.hold == 1
    assert submitted.artifact_level == "full"
    assert submitted.with_bagua is False

    bench = get_latest_rule_benchmark(cfg, rid)
    assert bench is not None
    assert bench["job_id"] == "job_fake_1"
    assert bench["status"] == "queued"
    assert bench["profile"]["profile_id"] == "rule_benchmark_v1"
    prof = bench["profile"]
    assert prof["account_mode"] == "portfolio"
    assert prof["buy_on"] == "open" and prof["sell_on"] == "close"

    # 重复提交复用 queued 任务
    r3 = client.post(
        "/api/v1/rules/%s/benchmark" % rid,
        json={"allow_research_proxy": True},
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["job_id"] == "job_fake_1"
    assert r3.json()["reused"] is True
    assert len(fake.submits) == 1


def test_benchmark_plain_rule_submits_without_confirmation(cfg):
    client, app, _cfg = _client(cfg)
    rid = _make_rule(cfg, name="普通基准", formula="XG:C>0;")["id"]
    fake = FakeJobStore()
    app.state.astock.jobs = fake
    r = client.post("/api/v1/rules/%s/benchmark" % rid, json={})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "job_id": "job_fake_1",
        "research_proxy": False,
        "reused": False,
    }
    assert get_rule_benchmark_by_job(cfg, "job_fake_1")["rule_id"] == rid


# --- performance -----------------------------------------------------------


def test_performance_untested_without_record(cfg):
    rid = _make_rule(cfg, name="无记录基准")["id"]
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["has_performance"] is False
    assert out["validity"] == "untested"
    assert out["demo"] is False
    assert out["show_metrics"] is False and out["show_chart"] is False
    assert out["metrics"] == {} and out["equity"] == []
    assert out["benchmark_equity"] == []


def test_performance_route_untested(cfg):
    client, _app, _cfg = _client(cfg)
    rid = _make_rule(cfg, name="HTTP无记录")["id"]
    body = client.get("/api/v1/rules/%s/performance" % rid).json()
    assert body["has_performance"] is False
    assert body["validity"] == "untested"
    assert body["demo"] is False
    assert body["show_metrics"] is False and body["show_chart"] is False
    assert body["metrics"] == {} and body["equity"] == []
    assert body["benchmark_equity"] == []


def test_performance_valid_contract(cfg, monkeypatch):
    rid = _make_rule(cfg, name="有效基准")["id"]
    _record_run(cfg, rid, run_id="run_v")
    monkeypatch.setattr(
        rb, "load_run_summary", lambda c, run_id: _metric_summary(42, run_id=run_id)
    )
    monkeypatch.setattr(
        rb,
        "load_equity_curve",
        lambda c, run_id, max_points=4000: _two_point_equity(run_id),
    )

    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["has_performance"] is True
    assert out["validity"] == "valid"
    assert out["demo"] is False
    assert out["show_metrics"] is True
    assert out["show_chart"] is True
    assert out["source_run_id"] == "run_v"
    assert set(out["metrics"]) == {
        "total_return",
        "annual_return",
        "max_drawdown",
        "win_rate",
        "sharpe",
        "n_round_trips",
    }
    assert out["metrics"]["total_return"] == 0.25
    assert out["metrics"]["max_drawdown"] < 0
    assert out["metrics"]["win_rate"] == 0.55
    assert out["equity"][1]["equity"] == 1.1
    assert out["benchmark_profile"]["end"] == 20260731
    assert "message" not in out
    assert "detail" not in out
    assert isinstance(out["updated_at"], int) and out["updated_at"] > 0
    assert out["benchmark_equity"] == []


def test_performance_route_contract_keys(cfg, monkeypatch):
    client, _app, _cfg = _client(cfg)
    rid = _make_rule(cfg, name="HTTP有效")["id"]
    _record_run(cfg, rid, run_id="run_h")
    monkeypatch.setattr(
        rb, "load_run_summary", lambda c, run_id: _metric_summary(42, run_id=run_id)
    )
    monkeypatch.setattr(
        rb,
        "load_equity_curve",
        lambda c, run_id, max_points=4000: _two_point_equity(run_id),
    )
    data = client.get("/api/v1/rules/%s/performance" % rid).json()
    assert data["validity"] == "valid"
    for key in (
        "has_performance",
        "validity",
        "metrics",
        "equity",
        "benchmark_profile",
        "source_run_id",
        "updated_at",
        "demo",
        "show_metrics",
        "show_chart",
    ):
        assert key in data, key
    assert data["show_metrics"] is True
    assert data["show_chart"] is True


def test_performance_no_trades_branch(cfg, monkeypatch):
    rid = _make_rule(cfg, name="零成交")["id"]
    _record_run(cfg, rid, run_id="run_n")
    summary = _metric_summary(0, run_id="run_n", extra={"win_rate": 0.0})
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: summary)
    monkeypatch.setattr(
        rb, "load_equity_curve", lambda c, run_id, max_points=4000: []
    )
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["validity"] == "no_trades"
    assert out["show_metrics"] is False
    assert out["show_chart"] is False
    assert out["detail"] == "本次基准测试没有产生有效交易"


def test_performance_empty_metrics_is_no_trades(cfg, monkeypatch):
    rid = _make_rule(cfg, name="空指标")["id"]
    _record_run(cfg, rid, run_id="run_e")
    monkeypatch.setattr(
        rb, "load_run_summary", lambda c, run_id: {"status": "ok", "metrics": {}}
    )
    monkeypatch.setattr(
        rb, "load_equity_curve", lambda c, run_id, max_points=4000: []
    )
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["validity"] == "no_trades"
    assert out["detail"]


def test_performance_insufficient_samples_branch(cfg, monkeypatch):
    rid = _make_rule(cfg, name="样本不足")["id"]
    _record_run(cfg, rid, run_id="run_i")
    monkeypatch.setattr(
        rb, "load_run_summary", lambda c, run_id: _metric_summary(5, run_id=run_id)
    )
    monkeypatch.setattr(
        rb,
        "load_equity_curve",
        lambda c, run_id, max_points=4000: _two_point_equity(run_id),
    )
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["validity"] == "insufficient_samples"
    assert out["show_metrics"] is True
    assert out["show_chart"] is True
    assert out["message"]


def test_performance_proxy_data_branch(cfg, monkeypatch):
    rule = _make_rule(cfg, name="代理数据", formula=MIN60_FORMULA)
    rid = rule["id"]
    _record_run(cfg, rid, run_id="run_p")
    monkeypatch.setattr(
        rb, "load_run_summary", lambda c, run_id: _metric_summary(42, run_id=run_id)
    )
    monkeypatch.setattr(
        rb,
        "load_equity_curve",
        lambda c, run_id, max_points=4000: _two_point_equity(run_id),
    )
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["validity"] == "proxy_data"
    assert out["show_metrics"] is True
    assert "60 分钟" in out["message"]


def test_performance_failed_summary_branch(cfg, monkeypatch):
    rid = _make_rule(cfg, name="失败基准")["id"]
    _record_run(cfg, rid, run_id="run_f")
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
    monkeypatch.setattr(
        rb, "load_equity_curve", lambda c, run_id, max_points=4000: []
    )
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["validity"] == "failed"
    assert out["show_metrics"] is False
    assert "全市场无信号" in (out.get("detail") or out.get("message") or "")


def test_performance_missing_run_files_failed(cfg, monkeypatch):
    rid = _make_rule(cfg, name="产物缺失")["id"]
    _record_run(cfg, rid, run_id="run_missing")

    def _boom(c, run_id):
        raise FileNotFoundError(run_id)

    monkeypatch.setattr(rb, "load_run_summary", _boom)
    monkeypatch.setattr(
        rb, "load_equity_curve", lambda c, run_id, max_points=4000: []
    )
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["validity"] == "failed"
    assert out["detail"]


def test_performance_lazy_sync_succeeded_job(cfg, monkeypatch):
    rid = _make_rule(cfg, name="惰性同步")["id"]
    create_rule_benchmark(cfg, rid, "job_ok", None)
    jobs = FakeJobStore()
    jobs.records["job_ok"] = _FakeJob("job_ok", status="succeeded", run_id="run_ok")
    monkeypatch.setattr(
        rb, "load_run_summary", lambda c, run_id: _metric_summary(42, run_id=run_id)
    )
    monkeypatch.setattr(
        rb,
        "load_equity_curve",
        lambda c, run_id, max_points=4000: _two_point_equity(run_id),
    )
    out = rb.get_rule_performance(_ctx(cfg, jobs), rid)
    assert out["validity"] == "valid"
    assert out["source_run_id"] == "run_ok"
    bench = get_latest_rule_benchmark(cfg, rid)
    assert bench["run_id"] == "run_ok"
    assert bench["status"] == "succeeded"


def test_performance_lazy_sync_failed_job(cfg):
    rid = _make_rule(cfg, name="失败任务")["id"]
    create_rule_benchmark(cfg, rid, "job_bad", None)
    jobs = FakeJobStore()
    jobs.records["job_bad"] = _FakeJob(
        "job_bad", status="failed", error="boom at run"
    )
    out = rb.get_rule_performance(_ctx(cfg, jobs), rid)
    assert out["validity"] == "failed"
    assert "boom at run" in (out.get("detail") or "")
    assert get_latest_rule_benchmark(cfg, rid)["status"] == "failed"


def test_performance_job_lost_marks_failed(cfg):
    rid = _make_rule(cfg, name="重启丢失")["id"]
    create_rule_benchmark(cfg, rid, "job_lost", None)
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["validity"] == "failed"
    text = (out.get("detail") or "") + (out.get("message") or "")
    assert "服务重启" in text
    bench = get_latest_rule_benchmark(cfg, rid)
    assert bench["status"] == "failed"
    assert "服务重启" in (bench["error"] or "")


def test_performance_pending_job_reports_untested(cfg):
    for status in ("queued", "running"):
        rid = _make_rule(cfg, name="排队_%s" % status)["id"]
        create_rule_benchmark(cfg, rid, "job_%s" % status, None)
        jobs = FakeJobStore()
        progress = (
            {"pct": 42.0, "message": "信号计算中"} if status == "running" else None
        )
        jobs.records["job_%s" % status] = _FakeJob(
            "job_%s" % status, status=status, progress=progress
        )
        out = rb.get_rule_performance(_ctx(cfg, jobs), rid)
        assert out["has_performance"] is False
        assert out["validity"] == "untested"
        assert out["benchmark_status"] == status
        assert out["benchmark_job_id"] == "job_%s" % status
        assert out["demo"] is False
        assert out["show_metrics"] is False and out["show_chart"] is False
        assert out["metrics"] == {} and out["equity"] == []
        assert out["benchmark_equity"] == []
        assert isinstance(out["updated_at"], int) and out["updated_at"] > 0
        if status == "queued":
            assert "排队" in out["message"]
        else:
            assert "运行中" in out["message"]
            assert "42" in out["message"] and "信号计算中" in out["message"]


def test_performance_queued_message_follows_live_progress(cfg):
    rid = _make_rule(cfg, name="排队进度")["id"]
    create_rule_benchmark(cfg, rid, "job_qprog", None)
    rec = _FakeJob(
        "job_qprog",
        status="queued",
        progress={"message": "等待调度", "queue_position": 3},
    )
    jobs = FakeJobStore()
    jobs.records["job_qprog"] = rec
    first = rb.get_rule_performance(_ctx(cfg, jobs), rid)["message"]
    assert "排队中" in first
    assert "前面还有 2 个任务" in first
    assert "等待调度" in first

    rec.progress = {"message": "即将开始", "queue_position": 1}
    second = rb.get_rule_performance(_ctx(cfg, jobs), rid)["message"]
    assert "即将开始" in second
    assert "前面还有" not in second
    assert second != first


def test_performance_pending_uses_live_job_status_not_db(cfg):
    rid = _make_rule(cfg, name="实时状态")["id"]
    create_rule_benchmark(cfg, rid, "job_live", None)
    jobs = FakeJobStore()
    jobs.records["job_live"] = _FakeJob("job_live", status="running")
    out = rb.get_rule_performance(_ctx(cfg, jobs), rid)
    # DB row still says queued; the payload must reflect the live job.
    assert get_latest_rule_benchmark(cfg, rid)["status"] == "queued"
    assert out["benchmark_status"] == "running"
    assert out["benchmark_job_id"] == "job_live"


def test_performance_succeeded_is_not_pending_and_clears_error(cfg, monkeypatch):
    rid = _make_rule(cfg, name="成功清错")["id"]
    bench_id = create_rule_benchmark(cfg, rid, "job_err", None)
    update_rule_benchmark(cfg, bench_id, status="failed", error="old failure")
    jobs = FakeJobStore()
    jobs.records["job_err"] = _FakeJob("job_err", status="succeeded", run_id="run_ok")
    monkeypatch.setattr(
        rb, "load_run_summary", lambda c, run_id: _metric_summary(42, run_id=run_id)
    )
    monkeypatch.setattr(
        rb,
        "load_equity_curve",
        lambda c, run_id, max_points=4000: _two_point_equity(run_id),
    )
    out = rb.get_rule_performance(_ctx(cfg, jobs), rid)
    assert out["validity"] == "valid"
    assert get_latest_rule_benchmark(cfg, rid)["error"] == ""


def test_performance_alias_uses_canonical_rule_id(cfg, monkeypatch):
    rule = _make_rule(cfg, name="别名基准")
    rid = rule["id"]
    _record_run(cfg, rid, run_id="run_alias")
    monkeypatch.setattr(
        rb, "load_run_summary", lambda c, run_id: _metric_summary(42, run_id=run_id)
    )
    monkeypatch.setattr(
        rb,
        "load_equity_curve",
        lambda c, run_id, max_points=4000: _two_point_equity(run_id),
    )
    # access via display name -> registry resolves to spec.id before DB lookup
    out = rb.get_rule_performance(_ctx(cfg), rule["name"])
    assert out["validity"] == "valid"
    assert out["source_run_id"] == "run_alias"


def test_has_numeric_metrics_ignores_non_numbers():
    assert rb._has_numeric_metrics({"total_return": "0.25"}) is False
    assert rb._has_numeric_metrics({"total_return": None}) is False
    assert rb._has_numeric_metrics({"total_return": True}) is False
    assert rb._has_numeric_metrics({"total_return": 0.0}) is True
    assert rb._has_numeric_metrics({"n_round_trips": 42}) is True


def test_run_cutoff_priority_run_end_then_min_then_cutoff(cfg, monkeypatch):
    rid = _make_rule(cfg, name="cutoff优先")["id"]
    profile = rb.build_benchmark_profile(cfg)
    profile["end"] = 20200101
    _record_run(cfg, rid, "run_cut", profile=profile)

    summary = _metric_summary(42, run_id="run_cut")
    summary["end"] = 20191231
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: summary)
    monkeypatch.setattr(
        rb,
        "load_equity_curve",
        lambda c, run_id, max_points=4000: _two_point_equity(run_id),
    )
    # run summary end wins over the dataset cutoff
    assert rb.get_rule_performance(_ctx(cfg), rid)["benchmark_profile"]["end"] == 20191231

    # no run end: min(requested end, cutoff)
    del summary["end"]
    monkeypatch.setattr(
        rb,
        "load_run_summary",
        lambda c, run_id: {**summary, "data_cutoff_date": 20260731},
    )
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["benchmark_profile"]["end"] == 20200101

    profile2 = rb.build_benchmark_profile(cfg)
    profile2["end"] = None
    _record_run(cfg, rid, "run_cut2", profile=profile2)
    monkeypatch.setattr(
        rb, "load_run_summary", lambda c, run_id: _metric_summary(42, run_id=run_id)
    )
    out2 = rb.get_rule_performance(_ctx(cfg), rid)
    assert out2["benchmark_profile"]["end"] == 20260731


def test_run_cutoff_clamps_run_end_by_dataset_cutoff(cfg, monkeypatch):
    rid = _make_rule(cfg, name="cutoff保守")["id"]
    profile = rb.build_benchmark_profile(cfg)
    profile["end"] = 20201231
    _record_run(cfg, rid, "run_conservative", profile=profile)

    summary = _metric_summary(42, run_id="run_conservative")
    summary["end"] = 20260731  # summary end echoes the request, later than data
    summary["data_cutoff_date"] = 20240105
    monkeypatch.setattr(rb, "load_run_summary", lambda c, run_id: summary)
    monkeypatch.setattr(
        rb,
        "load_equity_curve",
        lambda c, run_id, max_points=4000: _two_point_equity(run_id),
    )
    out = rb.get_rule_performance(_ctx(cfg), rid)
    assert out["benchmark_profile"]["end"] == 20240105

    # run end earlier than the cutoff still wins
    summary["end"] = 20200101
    out2 = rb.get_rule_performance(_ctx(cfg), rid)
    assert out2["benchmark_profile"]["end"] == 20200101


# --- submit hardening ------------------------------------------------------


def test_submit_sidecar_runtime_fields_passthrough(cfg):
    sidecar = Path(cfg.storage_root) / "benchmark_profile.json"
    sidecar.write_text(
        json.dumps(
            {
                "start": 20200101,
                "end": 20201231,
                "entry_lag": 2,
                "hold": 5,
                "buy_on": "close",
                "sell_on": "open",
                "account_mode": "per_symbol",
                "engine": "fast",
                "artifact_level": "summary",
                "signal_weekdays": [1, 3, 5],
                "buy_weekday": 5,
                "exit_weekday": 1,
                "with_bagua": True,
                "codes": ["SSE.STK.600000", "SZSE.STK.000001"],
                "stop_loss": 0.08,
                "take_profit": 0.2,
                "gua_filter": {"enabled": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client, app, _cfg = _client(cfg)
    fake = FakeJobStore()
    app.state.astock.jobs = fake
    rid = _make_rule(cfg, name="边车透传")["id"]

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
    assert req.gua_filter == {"enabled": True}


def test_submit_sidecar_invalid_types_fall_back_to_defaults(cfg):
    sidecar = Path(cfg.storage_root) / "benchmark_profile.json"
    sidecar.write_text(
        json.dumps(
            {
                "entry_lag": "abc",
                "hold": "many",
                "with_bagua": "yes",
                "codes": "ALL",
                "account_mode": "bogus",
                "engine": 3,
                "artifact_level": "everything",
                "signal_weekdays": "not-a-day",
                "buy_weekday": 99,
                "stop_loss": "cheap",
                "gua_filter": "off",
                "sample_gates": {"min_round_trips": "oops"},
                "universe_label": "hacked",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    profile = rb.build_benchmark_profile(cfg)
    assert profile["entry_lag"] == 1 and profile["hold"] == 1
    assert profile["with_bagua"] is False
    assert profile["codes"] == ["ALL"]
    assert profile["account_mode"] == "portfolio"
    assert profile["engine"] == "full" and profile["artifact_level"] == "full"
    assert profile["signal_weekdays"] is None and profile["buy_weekday"] is None
    assert profile["stop_loss"] is None and profile["gua_filter"] == {"enabled": False}
    assert profile["sample_gates"] == {"min_round_trips": 30}
    # display-only fields are not overridable from the sidecar
    assert profile["universe_label"] == rb.DEFAULT_BENCHMARK_PROFILE["universe_label"]

    client, app, _cfg = _client(cfg)
    fake = FakeJobStore()
    app.state.astock.jobs = fake
    rid = _make_rule(cfg, name="边车坏类型")["id"]
    r = client.post("/api/v1/rules/%s/benchmark" % rid, json={})
    assert r.status_code == 200, r.text
    req = fake.submits[0]
    assert req.entry_lag == 1 and req.hold == 1 and req.codes == ["ALL"]


@pytest.mark.parametrize(
    "key,bad",
    [
        ("entry_lag", 0),
        ("entry_lag", -1),
        ("entry_lag", True),
        ("entry_lag", "2"),
        ("hold", 0),
        ("hold", -3),
        ("hold", False),
        ("hold", "5"),
    ],
)
def test_sidecar_int_fields_strict_edges_fall_back(cfg, monkeypatch, key, bad):
    monkeypatch.setattr(rb, "resolve_data_max_date", lambda c: None)
    _write_sidecar(cfg, {key: bad})
    profile = rb.build_benchmark_profile(cfg)
    assert profile[key] == 1


def test_sidecar_int_fields_valid_values_kept(cfg, monkeypatch):
    monkeypatch.setattr(rb, "resolve_data_max_date", lambda c: None)
    _write_sidecar(cfg, {"entry_lag": 3, "hold": 4})
    profile = rb.build_benchmark_profile(cfg)
    assert profile["entry_lag"] == 3 and profile["hold"] == 4


@pytest.mark.parametrize("key", ["buy_weekday", "exit_weekday"])
@pytest.mark.parametrize("bad", [True, False, 0, 8, -1, "3", 3.0, [3]])
def test_sidecar_weekday_strict_edges_fall_back(cfg, monkeypatch, key, bad):
    monkeypatch.setattr(rb, "resolve_data_max_date", lambda c: None)
    _write_sidecar(cfg, {key: bad})
    profile = rb.build_benchmark_profile(cfg)
    assert profile[key] is None


def test_sidecar_weekday_valid_values_kept(cfg, monkeypatch):
    monkeypatch.setattr(rb, "resolve_data_max_date", lambda c: None)
    _write_sidecar(cfg, {"buy_weekday": 5, "exit_weekday": 1})
    profile = rb.build_benchmark_profile(cfg)
    assert profile["buy_weekday"] == 5 and profile["exit_weekday"] == 1


@pytest.mark.parametrize(
    "bad",
    [
        "1,3,5",
        1,
        1.5,
        True,
        [1, True],
        [1, 0],
        [1, 8],
        [1, "3"],
        [1, 2.0],
        [1, 2, 3, 4, 5, 6, 7, 1],
    ],
)
def test_sidecar_signal_weekdays_strict_edges_fall_back(cfg, monkeypatch, bad):
    monkeypatch.setattr(rb, "resolve_data_max_date", lambda c: None)
    _write_sidecar(cfg, {"signal_weekdays": bad})
    profile = rb.build_benchmark_profile(cfg)
    assert profile["signal_weekdays"] is None


def test_sidecar_signal_weekdays_dedupe_sort_and_kept(cfg, monkeypatch):
    monkeypatch.setattr(rb, "resolve_data_max_date", lambda c: None)
    _write_sidecar(cfg, {"signal_weekdays": [5, 1, 3, 3]})
    assert rb.build_benchmark_profile(cfg)["signal_weekdays"] == [1, 3, 5]

    _write_sidecar(cfg, {"signal_weekdays": [1, 2, 3, 4, 5, 6, 7]})
    assert rb.build_benchmark_profile(cfg)["signal_weekdays"] == [1, 2, 3, 4, 5, 6, 7]

    _write_sidecar(cfg, {"signal_weekdays": []})
    assert rb.build_benchmark_profile(cfg)["signal_weekdays"] is None


@pytest.mark.parametrize(
    "bad",
    [
        "600000",
        123,
        [123],
        [""],
        ["   "],
        ["x" * 33],
        [None],
        ["600000"] * 1001,
    ],
)
def test_sidecar_codes_strict_edges_fall_back(cfg, monkeypatch, bad):
    monkeypatch.setattr(rb, "resolve_data_max_date", lambda c: None)
    _write_sidecar(cfg, {"codes": bad})
    profile = rb.build_benchmark_profile(cfg)
    assert profile["codes"] == ["ALL"]
    assert profile["universe_label"] == rb.DEFAULT_BENCHMARK_PROFILE["universe_label"]


def test_sidecar_codes_normalized_and_bounded(cfg, monkeypatch):
    monkeypatch.setattr(rb, "resolve_data_max_date", lambda c: None)
    _write_sidecar(cfg, {"codes": [" 600000 ", "000001"]})
    profile = rb.build_benchmark_profile(cfg)
    assert profile["codes"] == ["600000", "000001"]

    _write_sidecar(cfg, {"codes": ["600000"] * 1000})
    assert len(rb.build_benchmark_profile(cfg)["codes"]) == 1000

    _write_sidecar(cfg, {"codes": ["x" * 32]})
    assert rb.build_benchmark_profile(cfg)["codes"] == ["x" * 32]


def test_submit_concurrent_first_post_creates_single_job(cfg):
    rid = _make_rule(cfg, name="并发首提")["id"]
    fake = FakeJobStore()
    fake.before_submit = lambda: time.sleep(0.05)
    ctx = _ctx(cfg, fake)
    results, errors = [], []
    barrier = threading.Barrier(4)

    def worker():
        try:
            barrier.wait(timeout=10)
            results.append(rb.submit_rule_benchmark(ctx, rid))
        except Exception as e:  # noqa: BLE001
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
    assert sum(1 for r in results if r["reused"]) == 3


def test_submit_db_failure_cancels_orphan_job(cfg, monkeypatch):
    rid = _make_rule(cfg, name="落库失败")["id"]
    fake = FakeJobStore()
    ctx = _ctx(cfg, fake)

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(rb, "create_rule_benchmark", _boom)
    with pytest.raises(RuntimeError, match="db down"):
        rb.submit_rule_benchmark(ctx, rid)
    assert list(fake.records.values())[0].status == "cancelled"
    assert get_latest_rule_benchmark(cfg, rid) is None


def test_submit_rejects_when_queue_full(cfg):
    client, app, _cfg = _client(cfg)
    fake = FakeJobStore()
    fake.queue_size = rb.MAX_QUEUED_BENCHMARKS
    app.state.astock.jobs = fake
    rid = _make_rule(cfg, name="队列已满")["id"]
    r = client.post("/api/v1/rules/%s/benchmark" % rid, json={})
    assert r.status_code == 429, r.text
    assert "队列已满" in r.json()["detail"]
    assert fake.submits == []
    assert get_latest_rule_benchmark(cfg, rid) is None


def test_submit_unconfirmed_tn6_rejected_before_queue(cfg):
    from wtpy.apps.astock.indicators.tn6_importer import (
        file_sha256,
        pair_source,
        save_source_map,
    )

    pkg = Path(cfg.indicator_dir) / "conf_probe.tn6"
    pkg.write_bytes(b"tn6-package")
    src = Path(cfg.indicator_dir) / "conf_probe.txt"
    src.write_text("XG:C>0;\n", encoding="utf-8")
    mapping = {}
    pair_source(mapping, file_sha256(pkg), src, package_file=pkg)
    save_source_map(cfg.mapping_path, mapping)

    client, app, _cfg = _client(cfg)
    fake = FakeJobStore()
    app.state.astock.jobs = fake
    r = client.post("/api/v1/rules/tn6_conf_probe/benchmark", json={})
    assert r.status_code == 400, r.text
    assert "未确认" in r.json()["detail"]
    assert fake.submits == []
    assert get_latest_rule_benchmark(cfg, "tn6_conf_probe") is None


def test_submit_confirmed_tn6_enqueues(cfg):
    from wtpy.apps.astock.indicators.tn6_importer import (
        confirm_source_pair,
        file_sha256,
        pair_source,
        save_source_map,
    )

    pkg = Path(cfg.indicator_dir) / "conf_ok.tn6"
    pkg.write_bytes(b"tn6-package")
    src = Path(cfg.indicator_dir) / "conf_ok.txt"
    src.write_text("XG:C>0;\n", encoding="utf-8")
    sha = file_sha256(pkg)
    mapping = {}
    pair_source(mapping, sha, src, package_file=pkg)
    save_source_map(cfg.mapping_path, mapping)
    confirm_source_pair(cfg.mapping_path, sha, confirmed_by="tester")

    client, app, _cfg = _client(cfg)
    fake = FakeJobStore()
    app.state.astock.jobs = fake
    r = client.post("/api/v1/rules/tn6_conf_ok/benchmark", json={})
    assert r.status_code == 200, r.text
    assert len(fake.submits) == 1


def test_resolve_data_max_date_ttl_cache(cfg, monkeypatch):
    calls = {"n": 0}

    def _fake_uncached(c):
        calls["n"] += 1
        return 20240101

    monkeypatch.setattr(rb, "_resolve_data_max_date_uncached", _fake_uncached)
    assert rb.resolve_data_max_date(cfg) == 20240101
    assert rb.resolve_data_max_date(cfg) == 20240101
    assert calls["n"] == 1
    key = str(cfg.market_data_root)
    rb._DATA_MAX_CACHE[key] = (time.monotonic() - rb._DATA_MAX_CACHE_TTL - 1, 20240101)
    assert rb.resolve_data_max_date(cfg) == 20240101
    assert calls["n"] == 2


# --- db helpers / rule payload ---------------------------------------------


def test_rule_benchmark_db_roundtrip(cfg):
    bench_id = create_rule_benchmark(cfg, "r1", "job_a", {"k": 1})
    assert isinstance(bench_id, int) and bench_id > 0
    row = get_latest_rule_benchmark(cfg, "r1")
    assert row["job_id"] == "job_a"
    assert row["profile"] == {"k": 1}
    assert row["status"] == "queued"

    update_rule_benchmark(cfg, bench_id, run_id="run_a", status="succeeded")
    row = get_latest_rule_benchmark(cfg, "r1")
    assert row["run_id"] == "run_a"
    assert row["status"] == "succeeded"

    create_rule_benchmark(cfg, "r1", "job_b", "{}")
    latest = get_latest_rule_benchmark(cfg, "r1")
    assert latest["job_id"] == "job_b"
    assert get_rule_benchmark_by_job(cfg, "job_a")["run_id"] == "run_a"


def test_rule_public_includes_min60_proxy_note(cfg):
    svc = RuleService(cfg)
    m60 = svc.create_rule(name="代理说明", formula_text=MIN60_FORMULA)
    assert m60["min60_proxy_note"]
    plain = svc.create_rule(name="普通说明", formula_text="XG:C>0;")
    assert plain["min60_proxy_note"] == ""


def test_rule_benchmark_table_created_on_init(cfg):
    from wtpy.apps.astock.service.db import connect, init_db

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
