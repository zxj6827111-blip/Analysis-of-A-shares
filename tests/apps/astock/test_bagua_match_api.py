# -*- coding: utf-8 -*-
"""API-layer tests for same-hexagram / same-day-pillar routes."""

from __future__ import annotations

import time
from pathlib import Path


def test_same_gua_should_async():
    from wtpy.apps.astock.api_routes.bagua import _bq_same_gua_should_async

    assert _bq_same_gua_should_async(None) is True
    assert _bq_same_gua_should_async(["600000"]) is False
    assert _bq_same_gua_should_async([str(i) for i in range(51)]) is True


def test_export_job_thread_passes_ctx(monkeypatch):
    """Regression: export worker thread must receive (ctx, job_id, params).
    A missing ctx makes the thread raise TypeError on start and the job
    stays queued forever (full-market export appeared stuck)."""
    import threading
    from types import SimpleNamespace

    from wtpy.apps.astock.api_routes import bagua as br

    captured = {}
    class FakeThread:
        def __init__(self, *a, **k):
            captured["args"] = k.get("args")
        def start(self):
            pass

    monkeypatch.setattr(br._bq_threading, "Thread", FakeThread)
    jobs = {}
    lock = threading.Lock()
    ctx = SimpleNamespace(cfg=object(), bq_export_jobs=jobs, bq_export_lock=lock)

    br._bq_start_export_job(
        ctx,
        date="2024-01-03",
        periods=["WEEK", "MONTH"],
        adjust="raw",
        codes=None,
        all_stocks=True,
        limit=None,
    )
    args = captured.get("args")
    assert args is not None
    assert len(args) == 3, f"worker args must be (ctx, job_id, params), got {len(args)}"
    assert args[0] is ctx
    assert isinstance(args[1], str)
    assert isinstance(args[2], dict)


def test_same_gua_start_reuses_active_job(monkeypatch):
    """Same params while a job is queued/running -> reuse, never duplicate."""
    import threading
    from types import SimpleNamespace

    from wtpy.apps.astock.api_routes import bagua as br

    jobs = {}
    lock = threading.Lock()
    ctx = SimpleNamespace(cfg=object(), bq_export_jobs=jobs, bq_export_lock=lock)
    # do not actually spawn worker threads in the test
    monkeypatch.setattr(
        br._bq_threading,
        "Thread",
        lambda *_a, **_k: SimpleNamespace(start=lambda: None),
    )

    kw = dict(code="600000", date="2024-01-03", period="DAY", adjust="raw", scope=None, limit=None)
    r1 = br._bq_start_same_gua_job(ctx, **kw)
    r2 = br._bq_start_same_gua_job(ctx, **kw)
    assert r1["job_id"] == r2["job_id"]
    assert r2.get("reused") is True
    assert r2.get("status") in ("queued", "running")

    # different parameters -> brand new job
    r3 = br._bq_start_same_gua_job(ctx, **{**kw, "code": "000001"})
    assert r3["job_id"] != r1["job_id"]
    assert r3.get("reused") is None

    # once finished, the slot is reusable again
    with lock:
        jobs[r1["job_id"]]["status"] = "done"
    r4 = br._bq_start_same_gua_job(ctx, **kw)
    assert r4["job_id"] != r1["job_id"]
    assert r4.get("reused") is None


def test_same_gua_async_job_flow(tmp_path: Path):
    import pytest

    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    app = create_app(cfg)
    client = TestClient(app)

    # full-market (scope=None) -> background job
    r = client.post(
        "/api/v1/bagua/same-gua",
        json={"code": "600000", "date": "2024-01-03"},
        params={"async_mode": "true"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("mode") == "async"
    job_id = body.get("job_id")
    assert job_id
    assert body.get("kind") == "same_gua"
    assert body.get("status") in ("queued", "running")

    # job status endpoint returns metadata (no result until done)
    j = client.get(f"/api/v1/bagua/same-gua/jobs/{job_id}")
    assert j.status_code == 200
    jb = j.json()
    assert jb.get("kind") == "same_gua"
    assert jb.get("code") == "600000"

    # result endpoint fails closed before done (or already done if the
    # background thread finished between the two requests)
    res = client.get(f"/api/v1/bagua/same-gua/jobs/{job_id}/result")
    assert res.status_code in (200, 409)

    # unknown job id -> 404
    assert client.get("/api/v1/bagua/same-gua/jobs/__missing__").status_code == 404


def test_same_gua_sync_small_scope(tmp_path: Path):
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    app = create_app(cfg)
    client = TestClient(app)

    # small scope (< 50) stays synchronous; whatever the data availability
    # on this machine, the route must never return a 500.
    r = client.post(
        "/api/v1/bagua/same-gua",
        json={"code": "600000", "date": "2024-01-03", "scope": ["600000", "000001"]},
        params={"async_mode": "true"},
    )
    assert r.status_code < 500
    assert r.json() is not None


def test_same_rizhu_route_registered(tmp_path: Path, monkeypatch):
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.service import bagua_query as bq

    # force an empty 日柱 table so the error path is deterministic regardless
    # of what is present on this machine's Desktop.
    monkeypatch.setattr(bq, "load_rizhu_map", lambda _p=None: {})
    monkeypatch.setattr(bq, "resolve_stock_name", lambda _c, _k, std_code="": "")

    storage = tmp_path / "st"
    storage.mkdir()
    ind = tmp_path / "ind"
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    app = create_app(cfg)
    client = TestClient(app)

    # empty 日柱 table -> structured ok=False, never 500
    r = client.get("/api/v1/bagua/same-rizhu?code=600000")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is False
    assert "未在日柱表中找到" in body.get("error", "")


# ---------------------------------------------------------------------------
# 导出 review_rules 勾选参数透传（卦象查询页信号规则自定义导出）
# ---------------------------------------------------------------------------

def _export_client(tmp_path: Path, monkeypatch):
    """起 app + mock 导出函数，返回 (client, captured)。"""
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.service import bagua_query as bq

    captured = {}

    def _fake_export(cfg, **kw):
        captured.update(kw)
        from pathlib import Path as _P
        out = tmp_path / "fake_export.xlsx"
        out.write_bytes(b"fake")
        return out

    monkeypatch.setattr(bq, "export_bagua_multi_period_xlsx", _fake_export)
    storage = tmp_path / "st"
    storage.mkdir()
    cfg = get_default_config(storage_root=storage)
    app = create_app(cfg)
    client = TestClient(app)
    return client, captured


def test_export_post_review_rules_passthrough(tmp_path: Path, monkeypatch):
    """POST /api/v1/bagua/export：body.review_rules 到达导出函数（同步路径）。"""
    client, captured = _export_client(tmp_path, monkeypatch)
    r = client.post(
        "/api/v1/bagua/export?async_mode=false",
        json={
            "codes": ["600000"],
            "all_stocks": False,
            "date": "2024-01-15",
            "periods": ["DAY", "WEEK", "MONTH"],
            "adjust": "tushare_qfq",
            "review_rules": ["txt_735金叉及趋势", "user_demo"],
        },
    )
    assert r.status_code == 200, r.text
    assert captured["review_rules"] == ["txt_735金叉及趋势", "user_demo"]


def test_export_get_review_rules_passthrough(tmp_path: Path, monkeypatch):
    """GET /api/v1/bagua/export：query review_rules（逗号分隔）解析并透传。"""
    client, captured = _export_client(tmp_path, monkeypatch)
    r = client.get(
        "/api/v1/bagua/export"
        "?date=2024-01-15&period=DAY,WEEK,MONTH&adjust=tushare_qfq"
        "&all_stocks=true&async_mode=false"
        "&review_rules=txt_735金叉及趋势%2Cuser_demo"
    )
    assert r.status_code == 200, r.text
    assert captured["review_rules"] == ["txt_735金叉及趋势", "user_demo"]
    # 空串 = 明确不带信号 sheet（区别于参数缺席的 None 默认行为）
    r2 = client.get(
        "/api/v1/bagua/export"
        "?date=2024-01-15&period=DAY,WEEK,MONTH&adjust=tushare_qfq"
        "&all_stocks=true&async_mode=false&review_rules="
    )
    assert r2.status_code == 200, r2.text
    assert captured["review_rules"] == []


def test_export_get_review_rules_default_none(tmp_path: Path, monkeypatch):
    """GET 不带 review_rules：透传 None（后端保持现状全量行为）。"""
    client, captured = _export_client(tmp_path, monkeypatch)
    r = client.get(
        "/api/v1/bagua/export"
        "?date=2024-01-15&period=DAY,WEEK,MONTH&adjust=tushare_qfq"
        "&all_stocks=true&async_mode=false"
    )
    assert r.status_code == 200, r.text
    assert captured["review_rules"] is None


def test_export_async_job_review_rules_recorded(tmp_path: Path, monkeypatch):
    """GET 异步路径：review_rules 进 job 记录与 worker params。"""
    import threading
    from types import SimpleNamespace

    from wtpy.apps.astock.api_routes import bagua as br

    captured = {}
    class FakeThread:
        def __init__(self, *a, **k):
            captured["args"] = k.get("args")
        def start(self):
            pass

    monkeypatch.setattr(br._bq_threading, "Thread", FakeThread)
    jobs = {}
    lock = threading.Lock()
    ctx = SimpleNamespace(cfg=object(), bq_export_jobs=jobs, bq_export_lock=lock)
    out = br._bq_start_export_job(
        ctx,
        date="2024-01-15",
        periods=["WEEK", "MONTH"],
        adjust="tushare_qfq",
        codes=None,
        all_stocks=True,
        limit=None,
        review_rules=["txt_735金叉及趋势"],
    )
    assert out["review_rules"] == ["txt_735金叉及趋势"]
    params = captured["args"][2]
    assert params["review_rules"] == ["txt_735金叉及趋势"]


def test_rules_include_hidden_query(tmp_path: Path):
    """GET /api/v1/rules?include_hidden=true：返回隐藏规则并标 hidden。"""
    import json

    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config

    storage = tmp_path / "st"
    storage.mkdir()
    ind = tmp_path / "ind"
    ind.mkdir()
    # 一条隐藏的用户规则：RuleService 创建后写 hidden_rule_ids.json
    from wtpy.apps.astock.service.rules import RuleService

    cfg = get_default_config(storage_root=storage, indicator_dir=ind)

    store = RuleService(cfg)
    r = store.create_rule(name="隐藏规则甲", formula_text="XG:CLOSE>OPEN;")
    hidden_path = store._hidden_path()
    hidden_path.parent.mkdir(parents=True, exist_ok=True)
    hidden_path.write_text(
        json.dumps([r["id"]], ensure_ascii=False), encoding="utf-8"
    )

    app = create_app(cfg)
    client = TestClient(app)
    base = client.get("/api/v1/rules").json()
    hidden_in_base = [x for x in base if x["id"] == r["id"]]
    assert not hidden_in_base, "默认列表必须隐藏隐藏规则"
    full = client.get("/api/v1/rules?include_hidden=true").json()
    hidden_in_full = [x for x in full if x["id"] == r["id"]]
    assert hidden_in_full and hidden_in_full[0].get("hidden") is True
