"""API + service structural and functional tests (TestClient)."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import tests.apps.astock.conftest  # noqa: F401

import pytest

from wtpy.apps.astock.api import STATIC_DIR, create_app
from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.service.backtest import BacktestRequest


def test_static_frontend_exists():
    index = STATIC_DIR / "index.html"
    assert index.is_file(), f"missing frontend {index}"
    text = index.read_text(encoding="utf-8")
    assert "entry_lag" in text or "entryLag" in text
    assert "/api/v1/backtests" in text
    assert "/api/v1/rules" in text


def test_api_health_and_rules(tmp_path: Path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    ind.mkdir()
    storage.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    app = create_app(cfg)
    client = TestClient(app)

    h = client.get("/api/v1/health")
    assert h.status_code == 200
    assert h.json()["ok"] is True

    v = client.post(
        "/api/v1/rules/validate",
        json={"formula_text": "XG:C>0;", "name": "t"},
    )
    assert v.status_code == 200
    assert v.json()["ok"] is True

    c = client.post(
        "/api/v1/rules",
        json={"name": "api_rule", "formula_text": "XG:C>OPEN;"},
    )
    assert c.status_code == 200
    rid = c.json()["id"]
    assert rid.startswith("user_")

    lst = client.get("/api/v1/rules")
    assert lst.status_code == 200
    assert any(r["id"] == rid for r in lst.json())

    page = client.get("/")
    assert page.status_code == 200
    assert "回测" in page.text


def _rules_client(tmp_path: Path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    ind.mkdir(parents=True, exist_ok=True)
    storage.mkdir(parents=True, exist_ok=True)
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    return TestClient(create_app(cfg)), cfg


def test_rules_patch_recomputes_formula_state(tmp_path: Path):
    client, _cfg = _rules_client(tmp_path)
    created = client.post(
        "/api/v1/rules",
        json={
            "name": "回踩",
            "formula_text": "XG:C>0;",
            "description": "初版",
            "category": "趋势",
        },
    )
    assert created.status_code == 200, created.text
    rid = created.json()["id"]
    assert created.json()["description"] == "初版"
    assert created.json()["category"] == "趋势"

    m60 = 'DIF60:="MACD.DIF#MIN60";\nXG:C>0 AND DIF60>0;'
    patched = client.patch(
        f"/api/v1/rules/{rid}",
        json={
            "name": "回踩改",
            "formula_text": m60,
            "description": "新版",
            "category": "低吸",
        },
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["name"] == "回踩改"
    assert body["dependencies"] == ["MIN60"]
    assert body["min60_day_proxy"] is True
    assert body["failure_reason"]
    assert body["description"] == "新版"
    assert body["category"] == "低吸"

    got = client.get(f"/api/v1/rules/{rid}").json()
    assert got["description"] == "新版" and got["category"] == "低吸"
    row = next(r for r in client.get("/api/v1/rules").json() if r["id"] == rid)
    assert row["description"] == "新版" and row["category"] == "低吸"
    assert row["min60_day_proxy"] is True

    back = client.patch(
        f"/api/v1/rules/{rid}", json={"formula_text": "XG:C>0;"}
    ).json()
    assert back["dependencies"] == []
    assert back["min60_day_proxy"] is False
    assert not back["failure_reason"]
    assert back["description"] == "新版" and back["category"] == "低吸"


def test_rules_categories_get_post_idempotent_and_validation(tmp_path: Path):
    client, cfg = _rules_client(tmp_path)
    # 静态路径先于 GET /{rule_id} 注册：必须返回列表而非 404 rule not found
    assert client.get("/api/v1/rules/categories").json() == {"categories": []}

    r1 = client.post("/api/v1/rules/categories", json={"name": "趋势"})
    assert r1.status_code == 200 and r1.json() == {"categories": ["趋势"]}
    # 重复幂等
    r2 = client.post("/api/v1/rules/categories", json={"name": "趋势"})
    assert r2.status_code == 200 and r2.json() == {"categories": ["趋势"]}
    r3 = client.post("/api/v1/rules/categories", json={"name": "低吸"})
    assert r3.json() == {"categories": ["趋势", "低吸"]}
    assert client.get("/api/v1/rules/categories").json() == {
        "categories": ["趋势", "低吸"]
    }
    path = Path(cfg.storage_root) / "indicators" / "rule_categories.json"
    assert path.exists()

    assert client.post(
        "/api/v1/rules/categories", json={"name": "   "}
    ).status_code == 400
    assert client.post(
        "/api/v1/rules/categories", json={"name": "x" * 21}
    ).status_code == 400


def test_rules_import_route_guards_and_basename(tmp_path: Path):
    client, cfg = _rules_client(tmp_path)
    ind = Path(cfg.indicator_dir)

    ok = client.post(
        "/api/v1/rules/import",
        json={"filename": "导入公式.txt", "content": "XG:C>0;"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"id": "txt_导入公式", "name": "导入公式"}
    assert (ind / "导入公式.txt").read_text(encoding="utf-8") == "XG:C>0;"

    dup = client.post(
        "/api/v1/rules/import",
        json={"filename": "导入公式.txt", "content": "XG:C>1;"},
    )
    assert dup.status_code == 400 and "已存在" in dup.json()["detail"]

    # 路径穿越：取 basename，不得写出指标目录
    trav = client.post(
        "/api/v1/rules/import",
        json={"filename": "..\\evil.txt", "content": "XG:C>0;"},
    )
    assert trav.status_code == 200, trav.text
    assert (ind / "evil.txt").exists()
    assert not (tmp_path / "evil.txt").exists()

    assert client.post(
        "/api/v1/rules/import", json={"filename": "pkg.tn6", "content": "x"}
    ).status_code == 400
    assert client.post(
        "/api/v1/rules/import", json={"filename": "note.md", "content": "x"}
    ).status_code == 400
    assert client.post(
        "/api/v1/rules/import", json={"filename": "empty.txt", "content": " "}
    ).status_code == 400


def test_rules_batch_validate_route_summary(tmp_path: Path):
    client, cfg = _rules_client(tmp_path)
    created = client.post(
        "/api/v1/rules", json={"name": "ok规则", "formula_text": "XG:C>0;"}
    ).json()
    (Path(cfg.indicator_dir) / "broken.txt").write_text(
        "MA5:=MA(C,5);\n", encoding="utf-8"
    )
    (Path(cfg.indicator_dir) / "pkg.tn6").write_bytes(b"pkg")

    r = client.post(
        "/api/v1/rules/batch-validate",
        json={"ids": [created["id"], "txt_broken", "tn6_pkg", "nope"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] == {"total": 4, "ok": 1, "failed": 3}
    by_id = {x["id"]: x for x in body["results"]}
    assert by_id[created["id"]]["ok"] is True
    assert by_id["txt_broken"]["ok"] is False
    assert by_id["tn6_pkg"]["ok"] is False and by_id["tn6_pkg"]["error"]
    assert by_id["nope"]["ok"] is False

    # ids 缺省 = 全量
    all_body = client.post("/api/v1/rules/batch-validate", json={}).json()
    s = all_body["summary"]
    assert s["total"] >= 4
    assert s["ok"] == s["total"] - s["failed"]


# --- 安全回归：HTTP 层路径穿越 / 长度上限 / 请求体大小 -----------------------


def test_rules_update_delete_reject_backslash_traversal(tmp_path: Path):
    client, cfg = _rules_client(tmp_path)
    created = client.post(
        "/api/v1/rules", json={"name": "穿越防护", "formula_text": "XG:C>0;"}
    ).json()
    rid = created["id"]

    sentinel = tmp_path / "xxx.txt"
    sentinel.write_text("SENTINEL", encoding="utf-8")
    before = {p.name for p in tmp_path.iterdir()}

    evil = "user_..%5C..%5C..%5Cxxx"
    patched = client.patch(f"/api/v1/rules/{evil}", json={"formula_text": "XG:C>1;"})
    assert patched.status_code in (400, 404), patched.text
    deleted = client.delete(f"/api/v1/rules/{evil}?permanent=true")
    assert deleted.status_code in (400, 404), deleted.text

    assert sentinel.read_text(encoding="utf-8") == "SENTINEL"
    assert {p.name for p in tmp_path.iterdir()} == before
    user_dir = Path(cfg.storage_root) / "indicators" / "user"
    assert (user_dir / f"{rid}.txt").exists()


def test_rules_patch_alias_writes_canonical_file(tmp_path: Path):
    client, cfg = _rules_client(tmp_path)
    created = client.post(
        "/api/v1/rules", json={"name": "user_HTTP别名", "formula_text": "XG:C>0;"}
    ).json()
    canonical = created["id"]

    patched = client.patch(
        "/api/v1/rules/user_HTTP别名", json={"formula_text": "XG:C>1;"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["id"] == canonical

    user_dir = Path(cfg.storage_root) / "indicators" / "user"
    assert (user_dir / f"{canonical}.txt").read_text(
        encoding="utf-8"
    ).strip() == "XG:C>1;"


def test_rules_payload_length_limits(tmp_path: Path):
    client, _cfg = _rules_client(tmp_path)
    assert client.post(
        "/api/v1/rules", json={"name": "x" * 65, "formula_text": "XG:C>0;"}
    ).status_code == 422
    assert client.post(
        "/api/v1/rules",
        json={"name": "ok", "formula_text": "XG:C>0;", "description": "d" * 501},
    ).status_code == 422
    assert client.post(
        "/api/v1/rules",
        json={"name": "ok", "formula_text": "XG:C>0;", "category": "c" * 21},
    ).status_code == 422

    rid = client.post(
        "/api/v1/rules", json={"name": "边界", "formula_text": "XG:C>0;"}
    ).json()["id"]
    assert client.patch(
        f"/api/v1/rules/{rid}", json={"name": "n" * 65}
    ).status_code == 422
    assert client.patch(
        f"/api/v1/rules/{rid}", json={"category": "c" * 21}
    ).status_code == 422
    assert client.post(
        "/api/v1/rules/batch-validate", json={"ids": ["r"] * 201}
    ).status_code == 422
    # 边界内仍正常
    assert client.patch(
        f"/api/v1/rules/{rid}", json={"name": "n" * 64, "category": "c" * 20}
    ).status_code == 200


def test_oversized_request_body_rejected_413(tmp_path: Path):
    client, _cfg = _rules_client(tmp_path)
    big = "x" * (2 * 1024 * 1024 + 1)
    r = client.post(
        "/api/v1/rules/validate", json={"formula_text": big, "name": "t"}
    )
    assert r.status_code == 413, r.text
    # GET 不受影响
    assert client.get("/api/v1/health").status_code == 200


def test_rules_body_limits_import_reaches_service(tmp_path: Path):
    """/rules 小上限 2MB：513KB 到达业务层（服务层 512KB -> 400），>2MB 413。"""
    client, _cfg = _rules_client(tmp_path)
    over_service = "x" * (513 * 1024)
    r = client.post(
        "/api/v1/rules/import",
        json={"filename": "big513.txt", "content": over_service},
    )
    assert r.status_code == 400, r.text
    assert "512KB" in str(r.json().get("detail"))
    under_service = "XG:C>0;\n" + "x" * (500 * 1024)
    ok = client.post(
        "/api/v1/rules/import",
        json={"filename": "ok500.txt", "content": under_service},
    )
    assert ok.status_code == 200, ok.text
    huge = "x" * (2 * 1024 * 1024 + 10)
    assert client.post(
        "/api/v1/rules/import",
        json={"filename": "huge.txt", "content": huge},
    ).status_code == 413


def test_chunked_rules_body_rejected_411_413(tmp_path: Path):
    """无 Content-Length 的 chunked 请求不得绕过 /rules 的 2MB 上限。"""
    client, _cfg = _rules_client(tmp_path)

    def _gen():
        yield b'{"formula_text": "'
        yield b"XG:C>0;"
        yield b'", "name": "t"}'

    r = client.post("/api/v1/rules/validate", content=_gen())
    assert r.status_code in (411, 413), (r.status_code, r.text)

    # 明确无 body（既无 CL 也无 TE）不误伤：由路由层 422，而非中间件 411/413
    lenient = client.post("/api/v1/rules/batch-validate")
    assert lenient.status_code not in (411, 413), lenient.text


def test_large_body_allowed_outside_rules_paths(tmp_path: Path):
    """>2MB 非 rules 路径不再被 2MB 中间件误伤（宽松 64MB 上限）。"""
    from fastapi.testclient import TestClient

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    ind.mkdir(parents=True, exist_ok=True)
    storage.mkdir(parents=True, exist_ok=True)
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    app = create_app(cfg)

    @app.post("/api/v1/_probe_large_body")
    def _probe_large_body(payload: dict = None):  # noqa: ANN001
        return {"ok": True, "size": len((payload or {}).get("data") or "")}

    client = TestClient(app)
    big = "x" * (3 * 1024 * 1024)
    r = client.post("/api/v1/_probe_large_body", json={"data": big})
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert r.json()["size"] == len(big)


def test_rules_delete_hide_restore_via_api(tmp_path: Path):
    client, _cfg = _rules_client(tmp_path)
    imp = client.post(
        "/api/v1/rules/import",
        json={"filename": "API隐藏.txt", "content": "XG:C>0;"},
    ).json()
    rid = imp["id"]
    assert rid.startswith("txt_")

    deleted = client.delete(f"/api/v1/rules/{rid}?permanent=true")
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["id"] == rid and body["deleted"] is False and body["mode"] == "hide"
    assert rid not in {x["id"] for x in client.get("/api/v1/rules").json()}
    full = {x["id"]: x for x in client.get("/api/v1/rules?include_hidden=true").json()}
    assert full[rid]["hidden"] is True

    restored = client.post(f"/api/v1/rules/{rid}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["hidden"] is False
    assert rid in {x["id"] for x in client.get("/api/v1/rules").json()}


def test_rules_unicode_ids_patch_delete_via_api(tmp_path: Path):
    from urllib.parse import quote

    client, _cfg = _rules_client(tmp_path)
    for name in ("Café趋势", "テスト規則", "Кирилл", "한국규칙"):
        created = client.post(
            "/api/v1/rules", json={"name": name, "formula_text": "XG:C>0;"}
        )
        assert created.status_code == 200, created.text
        rid = created.json()["id"]
        url = quote(rid, safe="")
        patched = client.patch(f"/api/v1/rules/{url}", json={"description": "d"})
        assert patched.status_code == 200, (rid, patched.text)
        deleted = client.delete(f"/api/v1/rules/{url}?permanent=true")
        assert deleted.status_code == 200, (rid, deleted.text)
        assert deleted.json()["mode"] == "hard"

    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("SENTINEL", encoding="utf-8")
    for evil in ("user_..%5C..%5C..%5Csentinel", "user_..%2e%2e%2f..%2fsentinel"):
        assert client.patch(
            f"/api/v1/rules/{evil}", json={"description": "x"}
        ).status_code in (400, 404)
        assert client.delete(
            f"/api/v1/rules/{evil}?permanent=true"
        ).status_code in (400, 404)
    assert sentinel.read_text(encoding="utf-8") == "SENTINEL"


def test_rule_illegal_unicode_name_sanitized(tmp_path: Path):
    """x\\uffff 之类的规则名入口清洗为 `_`，列表/详情响应编码不炸。"""
    client, _cfg = _rules_client(tmp_path)
    r = client.post(
        "/api/v1/rules", json={"name": "x\uffff", "formula_text": "XG:C>0;"}
    )
    assert r.status_code == 200, r.text
    assert "\uffff" not in r.json()["name"]
    listed = client.get("/api/v1/rules")
    assert listed.status_code == 200
    assert all("\uffff" not in x["name"] for x in listed.json())


def test_bagua_export_review_rules_entry_validation(tmp_path: Path):
    client, _cfg = _rules_client(tmp_path)
    base = {
        "date": "2026-08-28",
        "periods": ["WEEK", "MONTH"],
        "adjust": "tushare_qfq",
        "all_stocks": False,
        "codes": ["SSE.STK.600000"],
    }
    params = {"async_mode": "false"}
    ctrl = dict(base, review_rules=["bad\x01id"])
    assert client.post(
        "/api/v1/bagua/export", json=ctrl, params=params
    ).status_code == 400
    too_many = dict(base, review_rules=["r"] * 201)
    assert client.post(
        "/api/v1/bagua/export", json=too_many, params=params
    ).status_code == 400
    too_long = dict(base, review_rules=["r" * 129])
    assert client.post(
        "/api/v1/bagua/export", json=too_long, params=params
    ).status_code == 400
    nonchar = dict(base, review_rules=["bad\ufffeid", "bad\uffffid"])
    assert client.post(
        "/api/v1/bagua/export", json=nonchar, params=params
    ).status_code == 400
    get_bad = client.get(
        "/api/v1/bagua/export",
        params={
            "date": "2026-08-28",
            "all_stocks": "false",
            "codes": "600000",
            "review_rules": "bad\x01id",
        },
    )
    assert get_bad.status_code == 400
    get_nonchar = client.get(
        "/api/v1/bagua/export",
        params={
            "date": "2026-08-28",
            "all_stocks": "false",
            "codes": "600000",
            "review_rules": "bad\uffffid",
        },
    )
    assert get_nonchar.status_code == 400


def test_legacy_index_escapes_rule_fields():
    text = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "function esc(" in text

    m = re.search(r"function renderRules\(\)\s*\{", text)
    assert m, "renderRules not found"
    brace = text.index("{", m.start())
    depth = 0
    fn = ""
    for j in range(brace, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                fn = text[m.start(): j + 1]
                break
    assert fn
    for token in (
        "esc(r.name)",
        "esc(r.id)",
        "esc(r.compile_status)",
        "esc(r.failure_reason)",
    ):
        assert token in fn, token
    assert 'esc(r.error || "校验失败")' in text
    assert "esc(r.name || r.id)" in text
    assert "esc(r.compile_status)" in text


def test_legacy_index_script_node_syntax():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.I | re.S)
    assert scripts
    main = max(scripts, key=len)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as f:
        f.write(main)
        path = f.name
    try:
        proc = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
    finally:
        Path(path).unlink(missing_ok=True)


def test_backtest_request_to_dict_includes_entry_lag():
    req = BacktestRequest(rule_ids=["x"], entry_lag=2, hold=3)
    d = req.to_dict()
    assert d["entry_lag"] == 2
    assert d["hold"] == 3
    assert d["rule_ids"] == ["x"]
    assert d["corporate_action_policy"] is None


def test_api_maps_corporate_action_policy_to_request(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.service.backtest import BacktestService

    captured = {}

    def fake_run(self, req, *, progress_cb=None):
        captured["request"] = req
        return {"run_id": "bt_ca_api", "status": "ok"}

    monkeypatch.setattr(BacktestService, "run", fake_run)
    storage = tmp_path / "st"
    indicators = tmp_path / "ind"
    storage.mkdir()
    indicators.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=indicators)
    client = TestClient(create_app(cfg))

    response = client.post(
        "/api/v1/backtests",
        json={
            "rule_ids": ["test_rule"],
            "codes": ["SSE.STK.600000"],
            "corporate_action_policy": "event_ledger",
        },
    )

    assert response.status_code == 200
    assert captured["request"].corporate_action_policy == "event_ledger"

def test_factor_sync_start_adds_universe_file_from_env(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
    universe = tmp_path / "factor_universe.csv"
    universe.write_text(
        "canonical_symbol,inclusion_status\nSSE.STK.600000,included\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TUSHARE_FACTOR_UNIVERSE_FILE", str(universe))

    started = {}

    class FakeThread:
        def __init__(self, *args, **kwargs):
            started["args"] = kwargs.get("args", ())

        def start(self):
            started["started"] = True

    import threading

    monkeypatch.setattr(threading, "Thread", FakeThread)
    storage = tmp_path / "st"
    indicators = tmp_path / "ind"
    storage.mkdir()
    indicators.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=indicators)
    client = TestClient(create_app(cfg))

    response = client.post("/api/v1/data-sync/start", json={"task": "factor"})

    assert response.status_code == 200
    # Bug 2 regression: the worker thread must receive (ctx, cmd, task_name);
    # a missing ctx makes _run_sync_process crash inside the thread while the
    # API already returned 200.
    assert len(started["args"]) == 3
    ctx_arg, cmd, task_arg = started["args"]
    assert hasattr(ctx_arg, "cfg") and hasattr(ctx_arg, "sync_state")
    assert task_arg == "factor"
    assert "--adjustment" in cmd
    assert "adj_factor" in cmd
    # Bug 1 regression: adj_factor tasks are ALWAYS incremental (window fetch
    # + parent merge), never a full-history refetch.
    assert "--mode" in cmd
    assert cmd[cmd.index("--mode") + 1] == "incremental"
    assert "--universe-file" in cmd
    assert cmd[cmd.index("--universe-file") + 1] == str(universe)
    assert cmd[cmd.index("--end-date") + 1]


def test_factor_sync_start_passes_explicit_start_date(tmp_path, monkeypatch):
    """A user-pinned start_date must reach the adj_factor command line."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
    universe = tmp_path / "factor_universe.csv"
    universe.write_text(
        "canonical_symbol,inclusion_status\nSSE.STK.600000,included\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TUSHARE_FACTOR_UNIVERSE_FILE", str(universe))

    started = {}

    class FakeThread:
        def __init__(self, *args, **kwargs):
            started["args"] = kwargs.get("args", ())

        def start(self):
            started["started"] = True

    import threading

    monkeypatch.setattr(threading, "Thread", FakeThread)
    storage = tmp_path / "st"
    indicators = tmp_path / "ind"
    storage.mkdir()
    indicators.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=indicators)
    client = TestClient(create_app(cfg))

    response = client.post(
        "/api/v1/data-sync/start",
        json={"task": "factor", "start_date": 20260720},
    )
    assert response.status_code == 200
    cmd = started["args"][1]
    assert "--start-date" in cmd
    assert cmd[cmd.index("--start-date") + 1] == "20260720"


def test_sync_scripts_resolve_to_existing_files(tmp_path, monkeypatch):
    """Regression: sync script paths must stay anchored to the project root.

    system.py lives one package level deeper than the old api.py, so a stale
    `parents[3]` anchor resolves scripts to wtpy/scripts/... which does not
    exist and makes every UI-launched sync fail inside the worker thread
    (HTTP still returns 200, which is why route probes miss it).
    """
    pytest.importorskip("fastapi")
    from pathlib import Path

    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api_routes import system as system_routes

    script = Path(system_routes.SYNC_SCRIPT)
    assert script.is_file(), f"sync script missing: {script}"

    ca_script = system_routes.PROJECT_ROOT / "scripts" / "sync_ca_events.py"
    assert ca_script.is_file(), f"ca script missing: {ca_script}"

    started = {}

    class FakeThread:
        def __init__(self, *args, **kwargs):
            started["args"] = kwargs.get("args", ())

        def start(self):
            started["started"] = True

    import threading

    monkeypatch.setattr(threading, "Thread", FakeThread)
    universe = tmp_path / "factor_universe.csv"
    universe.write_text(
        "canonical_symbol,inclusion_status\nSSE.STK.600000,included\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TUSHARE_FACTOR_UNIVERSE_FILE", str(universe))
    storage = tmp_path / "st"
    indicators = tmp_path / "ind"
    storage.mkdir()
    indicators.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=indicators)

    # tdx is DISABLED by the Tushare-only policy: returns a structured skip
    # without spawning any process.
    started.clear()
    client = TestClient(create_app(cfg))
    started.clear()  # create_app may spawn worker threads (JobStore)
    r = client.post("/api/v1/data-sync/start", json={"task": "tdx"})
    assert r.status_code == 200
    assert r.json()["skipped"] == "disabled_by_policy"
    assert "args" not in started

    for task in ("tushare", "factor", "derive", "ca"):
        started.clear()
        client = TestClient(create_app(cfg))
        r = client.post("/api/v1/data-sync/start", json={"task": task})
        assert r.status_code == 200, (task, r.text)
        args = started.get("args", ())
        assert len(args) == 3, f"{task}: thread must receive (ctx, cmd, task_name)"
        ctx_arg, cmd, task_arg = args
        assert hasattr(ctx_arg, "cfg") and hasattr(ctx_arg, "sync_state"), task
        assert task_arg == task
        # cmd = [sys.executable, "-u", <script>, ...] -> script at index 2
        assert Path(cmd[2]).is_file(), f"{task}: script missing: {cmd[2]}"

    # task=tushare runs the zero-config chain in the script: raw incremental
    # without a pinned --adjustment (factor + reconcile follow inside).
    started.clear()
    client = TestClient(create_app(cfg))
    r = client.post("/api/v1/data-sync/start", json={"task": "tushare"})
    assert r.status_code == 200
    cmd = started["args"][1]
    assert "--source" in cmd
    assert cmd[cmd.index("--source") + 1] == "tushare"
    assert "--mode" in cmd
    assert cmd[cmd.index("--mode") + 1] == "incremental"
    assert "--adjustment" not in cmd


def test_factor_sync_start_reuses_latest_manifest_universe(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.data.dataset_store import DatasetManifest, DatasetStore

    monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
    monkeypatch.delenv("TUSHARE_FACTOR_UNIVERSE_FILE", raising=False)
    monkeypatch.delenv("ASTOCK_FACTOR_UNIVERSE_FILE", raising=False)
    universe = tmp_path / "manifest_universe.csv"
    universe.write_text(
        "canonical_symbol,inclusion_status\nSSE.STK.600000,included\n",
        encoding="utf-8",
    )
    storage = tmp_path / "st"
    indicators = tmp_path / "ind"
    storage.mkdir()
    indicators.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=indicators)
    store = DatasetStore(cfg.market_data_root)
    store.save_manifest(
        DatasetManifest(
            dataset_id="tushare_adjfactor_1d_test",
            source="tushare",
            adjustment="adj_factor",
            period="1d",
            status="ready",
            dataset_type="factor",
            data_cutoff_date=20260729,
            symbol_count=1,
            universe_file=str(universe),
        )
    )

    started = {}

    class FakeThread:
        def __init__(self, *args, **kwargs):
            started["args"] = kwargs.get("args", ())

        def start(self):
            started["started"] = True

    import threading

    monkeypatch.setattr(threading, "Thread", FakeThread)
    client = TestClient(create_app(cfg))

    response = client.post("/api/v1/data-sync/start", json={"task": "factor"})

    assert response.status_code == 200
    cmd = started["args"][1]
    assert cmd[cmd.index("--universe-file") + 1] == str(universe)


def test_dashboard_overview_and_page(tmp_path: Path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    client = TestClient(create_app(cfg))

    r = client.get("/api/v1/dashboard/overview")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    for key in ("data", "sync", "ca", "universe", "findings", "watchlist"):
        assert key in d
    # Graceful on a bare server: data block reports the missing root, no crash.
    assert d["data"]["exists"] is False
    assert isinstance(d["findings"], list)
    assert isinstance(d["watchlist"]["count"], (int, type(None)))

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "关键发现" in page.text

    # 30s TTL cache: repeated call must not recompute (same generated_at).
    r2 = client.get("/api/v1/dashboard/overview")
    assert r2.status_code == 200
    assert r2.json()["generated_at"] == d["generated_at"]

    index = client.get("/")
    assert "/dashboard" in index.text


def test_quick_query_endpoint_and_page(tmp_path: Path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    client = TestClient(create_app(cfg))

    # Structure is stable regardless of data availability.
    r = client.get("/api/v1/quick/600000")
    assert r.status_code == 200
    d = r.json()
    assert d["std_code"] in ("SSE.STK.600000", "sh600000")
    for key in ("code", "name", "std_code", "market", "gua", "gua_week", "related_runs"):
        assert key in d
    assert isinstance(d["related_runs"], list)
    # market degrades gracefully without warehouse data
    assert "market" in d and isinstance(d["market"], dict)

    # Daily + weekly hexagram blocks share the same shape.
    for gk in ("gua", "gua_week"):
        g = d[gk]
        assert isinstance(g, dict)
        if g.get("error") is None:
            assert g["period"] in ("DAY", "WEEK")

    # Same code is served from the 60s TTL cache (identical object id).
    r2 = client.get("/api/v1/quick/600000")
    assert r2.status_code == 200
    assert r2.json()["gua"] == d["gua"]

    # Chinese-name input resolves to a code (only when a non-empty name
    # cache is available; empty warehouses have no name data to resolve).
    _has_names = False
    try:
        from wtpy.apps.astock.service.stock_names import ensure_name_cache

        _has_names = bool(ensure_name_cache(cfg))
    except Exception:
        _has_names = False
    if _has_names:
        r = client.get("/api/v1/quick/平安银行")
        assert r.status_code == 200

    # Invalid input -> 4xx, not 500.
    r = client.get("/api/v1/quick/zzzzz")
    assert r.status_code in (400, 404)

    page = client.get("/quick.html?code=600000")
    assert page.status_code == 200
    assert "个股快速查询" in page.text
    assert "周卦" in page.text

    index = client.get("/")
    assert "quickCode" in index.text and "/quick.html" in index.text


class FakeSyncProc:
    """Stand-in for subprocess.Popen in _run_sync_process tests.

    poll_result=None means the child is still alive (poll() -> None);
    pass 0/1 for an already-exited child.
    """

    def __init__(self, lines=(), returncode=0, raise_on_iter=None, poll_result=None):
        self.stdout = _FakeSyncStdout(lines, raise_on_iter)
        self.returncode = returncode
        self._poll = poll_result
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self._poll is None:
            self._poll = self.returncode
        return self.returncode

    def poll(self):
        return self._poll

    def terminate(self):
        self.terminate_calls += 1
        if self._poll is None:
            self._poll = 1

    def kill(self):
        self.kill_calls += 1
        if self._poll is None:
            self._poll = 1


class _FakeSyncStdout:
    def __init__(self, lines, raise_on_iter):
        self._lines = list(lines)
        self._raise = raise_on_iter

    def __iter__(self):
        if self._raise is not None:
            raise self._raise
        return iter(self._lines)


def _sync_app(tmp_path):
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    app = create_app(cfg)
    return app, TestClient(app)


def _run_sync_with_proc(monkeypatch, ctx, proc):
    from wtpy.apps.astock.api_routes import system as system_routes

    def fake_popen(*args, **kwargs):
        return proc

    monkeypatch.setattr(system_routes.subprocess, "Popen", fake_popen)
    system_routes._run_sync_process(ctx, [sys.executable, "-u", "sync.py"], "tushare")


def test_sync_done_when_rc0_even_if_stop_requested_raced(tmp_path, monkeypatch):
    """Fix: a sync that already exited rc=0 must be reported done, never
    stopped, even when the user clicked stop while the reader was draining."""
    pytest.importorskip("fastapi")
    app, _client = _sync_app(tmp_path)
    ctx = app.state.astock
    with ctx.sync_lock:
        ctx.sync_state["running"] = True
        ctx.sync_state["status"] = "stopping"
        ctx.sync_state["stop_requested"] = True
        ctx.sync_state["task"] = "tushare"
    proc = FakeSyncProc(lines=["[SYNC_PROGRESS] done=1 total=1 phase=raw"], returncode=0)
    _run_sync_with_proc(monkeypatch, ctx, proc)
    assert ctx.sync_state["status"] == "done"
    assert ctx.sync_state["error"] is None
    assert ctx.sync_state["running"] is False
    assert ctx.sync_proc["proc"] is None
    # progress lines still captured
    assert ctx.sync_state["progress_done"] == 1


def test_sync_terminated_by_stop_reports_stopped(tmp_path, monkeypatch):
    """A genuinely terminated process (rc!=0 + stop request) reports stopped."""
    pytest.importorskip("fastapi")
    app, _client = _sync_app(tmp_path)
    ctx = app.state.astock
    with ctx.sync_lock:
        ctx.sync_state["running"] = True
        ctx.sync_state["status"] = "stopping"
        ctx.sync_state["stop_requested"] = True
        ctx.sync_state["task"] = "tushare"
    proc = FakeSyncProc(returncode=1)
    _run_sync_with_proc(monkeypatch, ctx, proc)
    assert ctx.sync_state["status"] == "stopped"
    assert ctx.sync_state["error"] == "用户手动停止"


def test_sync_nonzero_without_stop_reports_error(tmp_path, monkeypatch):
    """Business failure (rc!=0, no stop request) stays an error."""
    pytest.importorskip("fastapi")
    app, _client = _sync_app(tmp_path)
    ctx = app.state.astock
    with ctx.sync_lock:
        ctx.sync_state["running"] = True
        ctx.sync_state["status"] = "running"
        ctx.sync_state["task"] = "tushare"
    proc = FakeSyncProc(returncode=1)
    _run_sync_with_proc(monkeypatch, ctx, proc)
    assert ctx.sync_state["status"] == "error"
    assert ctx.sync_state["error"] == "exit code 1"


def test_sync_reader_exception_during_stop_keeps_stopped(tmp_path, monkeypatch):
    """Fix: OSError from the stdout read loop while stopping (Windows
    TerminateProcess) must not turn an intentional stop into an error."""
    pytest.importorskip("fastapi")
    app, _client = _sync_app(tmp_path)
    ctx = app.state.astock
    with ctx.sync_lock:
        ctx.sync_state["running"] = True
        ctx.sync_state["status"] = "stopping"
        ctx.sync_state["stop_requested"] = True
        ctx.sync_state["task"] = "tushare"
    proc = FakeSyncProc(raise_on_iter=OSError("read failed"), poll_result=None)
    _run_sync_with_proc(monkeypatch, ctx, proc)
    assert ctx.sync_state["status"] == "stopped"
    assert ctx.sync_state["error"] == "用户手动停止"


def test_sync_reader_exception_terminates_orphan_proc(tmp_path, monkeypatch):
    """Fix: an exception in the reader loop must terminate the still-alive
    child so it can never hold the SyncTaskLock for later runs."""
    pytest.importorskip("fastapi")
    app, _client = _sync_app(tmp_path)
    ctx = app.state.astock
    with ctx.sync_lock:
        ctx.sync_state["running"] = True
        ctx.sync_state["status"] = "running"
        ctx.sync_state["task"] = "tushare"
    proc = FakeSyncProc(raise_on_iter=OSError("pipe closed"), poll_result=None)
    _run_sync_with_proc(monkeypatch, ctx, proc)
    assert proc.terminate_calls >= 1
    assert ctx.sync_state["status"] == "error"
    assert ctx.sync_state["running"] is False
    assert ctx.sync_proc["proc"] is None


def test_sync_popen_failure_reports_error_and_clears(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    app, _client = _sync_app(tmp_path)
    ctx = app.state.astock
    with ctx.sync_lock:
        ctx.sync_state["running"] = True
        ctx.sync_state["status"] = "running"
        ctx.sync_state["task"] = "tushare"

    from wtpy.apps.astock.api_routes import system as system_routes

    def boom_popen(*args, **kwargs):
        raise FileNotFoundError("no such script")

    monkeypatch.setattr(system_routes.subprocess, "Popen", boom_popen)
    system_routes._run_sync_process(ctx, ["python", "missing.py"], "tushare")
    assert ctx.sync_state["status"] == "error"
    assert "no such script" in (ctx.sync_state["error"] or "")
    assert ctx.sync_state["running"] is False
    assert ctx.sync_proc["proc"] is None


def test_sync_stop_endpoint_marks_stop_requested_but_payload_stays_clean(tmp_path):
    """stop_requested is an internal marker: set on stop, never leaked into
    the status payload consumed by renderSyncProgress / pollSyncStatus."""
    pytest.importorskip("fastapi")
    app, client = _sync_app(tmp_path)
    ctx = app.state.astock
    proc = FakeSyncProc(returncode=1, poll_result=None)
    with ctx.sync_lock:
        ctx.sync_state["running"] = True
        ctx.sync_state["task"] = "tushare"
        ctx.sync_state["status"] = "running"
        ctx.sync_proc["proc"] = proc
    r = client.post("/api/v1/data-sync/stop", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    with ctx.sync_lock:
        assert ctx.sync_state["status"] == "stopping"
        assert ctx.sync_state["stop_requested"] is True
    st = client.get("/api/v1/data-sync/status").json()
    assert st["status"] == "stopping"
    assert "stop_requested" not in st


# ---------------------------------------------------------------------------
# P1-3: L1/L2 source-freshness tiles follow the ACTIVE product pair
# ---------------------------------------------------------------------------


def _save_product_manifest(store, dataset_id, source, adjustment, *, cutoff,
                           provenance, raw_dataset_id="", factor_dataset_id="",
                           dataset_type="bars"):
    """Publish a minimal ready manifest (no blobs needed for the API)."""
    from wtpy.apps.astock.data.dataset_store import DatasetManifest

    store.save_manifest(DatasetManifest(
        dataset_id=dataset_id,
        source=source,
        adjustment=adjustment,
        period="1d",
        status="ready",
        data_cutoff_date=cutoff,
        dataset_type=dataset_type,
        provenance=dict(provenance or {}),
        raw_dataset_id=raw_dataset_id,
        factor_dataset_id=factor_dataset_id,
        created_at=dataset_id,
        symbol_count=1,
        row_count=100,
    ))


def test_market_status_tiles_follow_active_pair(tmp_path):
    """P1-3 regression: the L1/L2 tiles must show the ACTIVE pair surfaces.

    An independent-latest composite face that is NOT tushare_only_v1 (and has
    no valid L2 parent) must never win a tile: the tile and the product block
    always come from the same validated pair.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    store = DatasetStore(cfg.market_data_root)

    # Valid pair: OLDER ready L1 (tushare_only_v1) whose raw parent is the
    # matching formal L2 (composite_none, tushare_only_v1). The lineage is
    # complete (L2 parents + L1 factor parent exist and carry the formal
    # roles) so the strict fail-closed pair validation accepts it.
    _save_product_manifest(
        store, "raw_base_pair", "tushare", "none", cutoff=20260701,
        provenance={"data_policy": "tushare_only_v1"},
    )
    _save_product_manifest(
        store, "raw_supp_pair", "tushare", "none", cutoff=20260701,
        provenance={"data_policy": "tushare_only_v1"},
    )
    _save_product_manifest(
        store, "tushare_adjfactor_1d_pair", "tushare", "adj_factor",
        cutoff=20260701, dataset_type="factor",
        provenance={"data_policy": "tushare_only_v1"},
    )
    _save_product_manifest(
        store, "l2_pair_old", "internal", "composite_none", cutoff=20260701,
        provenance={
            "data_policy": "tushare_only_v1",
            "base_source": "tushare",
            "supplement_source": "tushare",
            "parents": [
                {"dataset_id": "raw_base_pair", "role": "base"},
                {"dataset_id": "raw_supp_pair", "role": "supplement"},
            ],
        },
    )
    _save_product_manifest(
        store, "l1_pair_old", "internal", "composite_tushare_factor_qfq",
        cutoff=20260701,
        provenance={"data_policy": "tushare_only_v1"},
        raw_dataset_id="l2_pair_old",
        factor_dataset_id="tushare_adjfactor_1d_pair",
    )
    # NEWER L1 face with NO tushare_only_v1 marker and no parent: freshest by
    # cutoff but NOT part of any valid pair.
    _save_product_manifest(
        store, "l1_orphan_new", "internal", "composite_tushare_factor_qfq",
        cutoff=20260730, provenance={},
    )

    client = TestClient(create_app(cfg))
    d = client.get("/api/v1/market-data/status").json()
    assert d["exists"] is True
    tiles = {t["key"]: t for t in d["source_freshness"]}
    # Tiles follow the ACTIVE pair, never the independent freshest face.
    assert tiles["l1_product"]["dataset_id"] == "l1_pair_old"
    assert tiles["l1_product"]["status"] == "ready"
    assert tiles["l1_product"]["data_policy"] == "tushare_only_v1"
    assert tiles["l1_product"]["data_cutoff_date"] == 20260701
    assert tiles["l2_product"]["dataset_id"] == "l2_pair_old"
    assert tiles["l2_product"]["data_policy"] == "tushare_only_v1"
    # Tiles and the product block come from the SAME pair.
    assert d["product"]["active"] is True
    assert d["product"]["l1"]["dataset_id"] == tiles["l1_product"]["dataset_id"]
    assert d["product"]["l2"]["dataset_id"] == tiles["l2_product"]["dataset_id"]


def test_market_status_tiles_inactive_without_pair(tmp_path):
    """P1-3: with no valid pair the L1/L2 tiles degrade to inactive/None
    (existing field structure kept, product.active False)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    store = DatasetStore(cfg.market_data_root)
    # Only an orphan composite face (no tushare_only_v1 marker, no parent).
    _save_product_manifest(
        store, "l1_orphan_only", "internal", "composite_tushare_factor_qfq",
        cutoff=20260730, provenance={},
    )

    client = TestClient(create_app(cfg))
    d = client.get("/api/v1/market-data/status").json()
    tiles = {t["key"]: t for t in d["source_freshness"]}
    for key in ("l1_product", "l2_product"):
        assert tiles[key]["status"] == "inactive"
        assert tiles[key]["dataset_id"] is None
        assert tiles[key]["data_cutoff_date"] is None
        assert tiles[key]["symbol_count"] == 0
    assert d["product"] == {"l1": None, "l2": None, "active": False}


# ---------------------------------------------------------------------------
# P2-1b: factor tile prefers the LATEST candidate (freshness gate aware)
# ---------------------------------------------------------------------------


def _save_factor_manifest(store, dataset_id, *, cutoff, status,
                          freshness=None, provenance=None, created_at=None):
    from wtpy.apps.astock.data.dataset_store import DatasetManifest

    prov = dict(provenance or {})
    if freshness is not None:
        prov["freshness"] = freshness
    store.save_manifest(DatasetManifest(
        dataset_id=dataset_id,
        source="tushare",
        adjustment="adj_factor",
        period="1d",
        status=status,
        dataset_type="factor",
        data_cutoff_date=cutoff,
        symbol_count=1,
        row_count=100,
        created_at=created_at or dataset_id,
        provenance=prov,
    ))


def test_market_status_factor_tile_prefers_latest_partial_with_freshness(tmp_path):
    """P2-1b regression: the newest factor surface wins the tile even when it
    is a freshness-gate-blocked partial, and the tile carries the freshness
    gate summary (an older ready factor must not shadow the stall)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data.dataset_store import DatasetManifest, DatasetStore

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    store = DatasetStore(cfg.market_data_root)

    # OLD ready factor (no freshness metadata) vs NEW freshness-blocked partial.
    _save_factor_manifest(
        store, "tushare_adjfactor_1d_ready_old", cutoff=20260701,
        status="ready",
    )
    _save_factor_manifest(
        store, "tushare_adjfactor_1d_partial_fresh", cutoff=20260804,
        status="partial",
        freshness={
            "fresh_symbol_ratio": 0.5,
            "fresh_count": 2,
            "active_count": 4,
            "stale_active_symbols": [
                {"symbol": f"SZSE.STK.{600000 + i}", "factor_last_date": 20260701,
                 "raw_last_date": 20260804}
                for i in range(7)
            ],
            "p50_last_date": 20260730,
            "p10_last_date": 20260701,
            "raw_dataset_id": "tushare_none_1d_raw_x",
            "factor_dataset_id": "tushare_adjfactor_1d_partial_fresh",
            "fresh_tolerance_days": 3,
            "gate": "blocked",
            "reason": "freshness_below_threshold",
        },
    )
    # Raw tile must KEEP ready-first: a newer raw partial must not displace
    # the ready raw surface (raw has no freshness semantics).
    # 注意：真实 raw 数据集必须带 STK 符号（纯 ETF/指数数据集不能冒充
    # 股票地基），这里补上真实 symbols，否则会被 raw 卡的资产类别过滤排除。
    from wtpy.apps.astock.data.dataset_store import SymbolRecord

    def _raw_symbols():
        return [SymbolRecord(
            symbol="SSE.STK.600000", blob_sha256="x" * 64,
            first_date=20240101, last_date=20260804, row_count=100,
            quality="ok",
        )]

    store.save_manifest(DatasetManifest(
        dataset_id="tushare_none_1d_raw_ready", source="tushare",
        adjustment="none", period="1d", status="ready",
        dataset_type="bars", data_cutoff_date=20260804,
        symbol_count=1, row_count=100, created_at="raw_ready",
        symbols=_raw_symbols(),
    ))
    store.save_manifest(DatasetManifest(
        dataset_id="tushare_none_1d_raw_partial", source="tushare",
        adjustment="none", period="1d", status="partial",
        dataset_type="bars", data_cutoff_date=20260805,
        symbol_count=1, row_count=100, created_at="raw_partial",
        symbols=_raw_symbols(),
    ))

    client = TestClient(create_app(cfg))
    d = client.get("/api/v1/market-data/status").json()
    tiles = {t["key"]: t for t in d["source_freshness"]}
    # Factor tile shows the LATEST partial surface, not the old ready one.
    assert tiles["factor"]["dataset_id"] == "tushare_adjfactor_1d_partial_fresh"
    assert tiles["factor"]["status"] == "partial"
    assert tiles["factor"]["data_cutoff_date"] == 20260804
    assert tiles["factor"]["updated_to"] == 20260804
    # Freshness summary carried from provenance, stale sample capped at 5.
    f = tiles["factor"]["freshness"]
    assert f is not None
    assert f["gate"] == "blocked"
    assert f["reason"] == "freshness_below_threshold"
    assert f["fresh_symbol_ratio"] == 0.5
    assert f["fresh_count"] == 2
    assert f["active_count"] == 4
    assert f["p50"] == 20260730
    assert f["p10"] == 20260701
    assert len(f["stale_active_symbols"]) == 5
    assert f["stale_active_symbols"][0]["symbol"] == "SZSE.STK.600000"
    # Raw tile keeps ready priority even when a newer partial exists.
    assert tiles["tushare"]["dataset_id"] == "tushare_none_1d_raw_ready"
    assert tiles["tushare"]["status"] == "ready"


def test_market_status_factor_tile_old_manifest_without_freshness(tmp_path):
    """P2-1b: an old-format factor manifest without provenance freshness must
    not crash and the tile reports freshness=None (fields stay intact)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    store = DatasetStore(cfg.market_data_root)
    _save_factor_manifest(
        store, "tushare_adjfactor_1d_legacy", cutoff=20260729, status="ready",
    )

    client = TestClient(create_app(cfg))
    d = client.get("/api/v1/market-data/status").json()
    tiles = {t["key"]: t for t in d["source_freshness"]}
    t = tiles["factor"]
    assert t["dataset_id"] == "tushare_adjfactor_1d_legacy"
    assert t["status"] == "ready"
    assert t["data_cutoff_date"] == 20260729
    assert t["freshness"] is None
    # Existing tile field structure preserved (frontend compatible).
    for key in ("source", "adjustment", "earliest_date", "latest_date",
                "updated_to", "symbol_count", "row_count", "created_at"):
        assert key in t


def test_data_health_factor_item_reports_gate(tmp_path):
    """P2-1b: /api/v1/system/data-health reports the LATEST factor surface
    (a freshness-blocked partial, not the old ready one) and its gate state."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    store = DatasetStore(cfg.market_data_root)
    # Derive-path gate shape: status (not gate) — the report must read both.
    _save_factor_manifest(
        store, "tushare_adjfactor_1d_ready_old", cutoff=20260701,
        status="ready",
    )
    _save_factor_manifest(
        store, "tushare_adjfactor_1d_partial_gate", cutoff=20260804,
        status="partial",
        freshness={
            "status": "blocked",
            "reason": "freshness_below_threshold",
            "fresh_symbol_ratio": 0.25,
            "fresh_count": 1,
            "active_count": 4,
            "stale_active_symbols": [
                {"symbol": "SZSE.STK.600000", "factor_last_date": 20260701,
                 "raw_last_date": 20260804}
            ],
            "factor_dataset_id": "tushare_adjfactor_1d_partial_gate",
            "raw_dataset_id": "tushare_none_1d_raw_x",
            "min_ratio": 0.9,
        },
    )

    client = TestClient(create_app(cfg))
    h = client.get("/api/v1/system/data-health").json()
    factor = h["current_freshness"]["tushare_factor"]
    assert factor["dataset_id"] == "tushare_adjfactor_1d_partial_gate"
    assert factor["status"] == "partial"
    assert factor["freshness_gate"] == "blocked"
    assert factor["fresh_symbol_ratio"] == 0.25
    assert factor["data_cutoff_date"] == 20260804
    # Data health itself remains fail-closed on the missing formal pair.
    assert h["status"] in ("stale", "warning")


# ---------------------------------------------------------------------------
# P2-1: recent sync errors carry the concrete failure detail
# ---------------------------------------------------------------------------


def test_data_health_recent_errors_carry_failure_details(tmp_path):
    """P2-1 regression: a partial sync log's result (missing_factor, missing
    list, counts, issues_sample) must reach /api/v1/system/data-health; old
    minimal logs keep working with graceful None fields."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    store = DatasetStore(cfg.market_data_root)
    store.save_sync_log("partial_rich", {
        "sync_run_id": "partial_rich",
        "dataset_id": "internal_composite_tushare_factor_qfq_1d_20260730_x",
        "result": {
            "status": "partial",
            "error": None,
            "missing_factor": 553,
            "missing": ["SSE.STK.600001", "SSE.STK.600002", "SSE.STK.600003",
                        "SSE.STK.600004", "SSE.STK.600005"],
            "imported": 5000, "eligible": 5100, "row_count": 120000,
            "failed": 2, "no_data": 553,
            "warning": "strict policy partial",
            "reason": "missing_factor",
        },
        "issues_sample": ["issue-a", "issue-b", "issue-c", "issue-d"],
    })
    store.save_sync_log("failed_minimal", {
        "sync_run_id": "failed_minimal",
        "dataset_id": "tushare_none_1d_x",
        "result": {"status": "failed", "error": "rate_limited"},
    })

    client = TestClient(create_app(cfg))
    h = client.get("/api/v1/system/data-health").json()
    errors = {e["sync_run_id"]: e for e in h["recent_sync_errors"]}
    rich = errors["partial_rich"]
    assert rich["status"] == "partial"
    assert rich["error"] is None
    assert rich["missing_factor"] == 553
    assert rich["missing_count"] == 5
    assert rich["imported"] == 5000
    assert rich["eligible"] == 5100
    assert rich["row_count"] == 120000
    assert rich["failed"] == 2
    assert rich["no_data"] == 553
    assert rich["warning"] == "strict policy partial"
    assert rich["reason"] == "missing_factor"
    # issues_sample: first 3 entries only.
    assert rich["issues_sample"] == ["issue-a", "issue-b", "issue-c"]
    # Old/minimal logs must not crash and keep graceful None defaults.
    old = errors["failed_minimal"]
    assert old["status"] == "failed"
    assert old["error"] == "rate_limited"
    assert old["missing_factor"] is None
    assert old["missing_count"] is None
    assert old["imported"] is None
    assert "issues_sample" not in old


def test_bagua_query_tdx_front_disabled_returns_400(tmp_path):
    """The disabled tdx_front price plane is a client error: the API must
    answer 400 with the clear disabled-source message (never a 500)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    client = TestClient(create_app(cfg))
    for url in ("/api/v1/bagua/query",):
        resp = client.get(
            url,
            params={"code": "600000", "date": "2026-08-04",
                    "adjust": "tdx_front"},
        )
        assert resp.status_code == 400, resp.text
        assert "已停用" in resp.text


def test_factor_sync_universe_reuse_ignores_non_factor_manifests(
        tmp_path, monkeypatch):
    """_latest_factor_universe_file must only reuse universe files from real
    factor manifests (dataset_type=factor), matching the sync script's
    selector — a bars manifest must not win the selection."""
    pytest.importorskip("fastapi")
    from wtpy.apps.astock.api_routes import system as system_routes

    from wtpy.apps.astock.data.dataset_store import DatasetManifest, DatasetStore

    monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
    monkeypatch.delenv("TUSHARE_FACTOR_UNIVERSE_FILE", raising=False)
    monkeypatch.delenv("ASTOCK_FACTOR_UNIVERSE_FILE", raising=False)
    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    universe = tmp_path / "factor_universe.csv"
    universe.write_text("canonical_symbol,inclusion_status\n", encoding="utf-8")
    bars_universe = tmp_path / "bars_universe.csv"
    bars_universe.write_text("canonical_symbol,inclusion_status\n", encoding="utf-8")
    store = DatasetStore(cfg.market_data_root)
    store.save_manifest(DatasetManifest(
        dataset_id="tushare_adjfactor_1d_fake_bars",
        source="tushare", adjustment="adj_factor", period="1d",
        status="ready", dataset_type="bars",  # NOT a factor dataset
        data_cutoff_date=20260804, symbol_count=1,
        universe_file=str(bars_universe),
    ))
    store.save_manifest(DatasetManifest(
        dataset_id="tushare_adjfactor_1d_real",
        source="tushare", adjustment="adj_factor", period="1d",
        status="ready", dataset_type="factor",
        data_cutoff_date=20260729, symbol_count=1,
        universe_file=str(universe),
    ))
    ctx = _FakeCtx(cfg)
    picked = system_routes._latest_factor_universe_file(ctx)
    assert picked == str(universe)


class _FakeCtx:
    """Minimal ApiContext stand-in for route helper tests."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.sync_state = {}
        self.sync_proc = {}
        self.sync_lock = None


def test_rules_min60_proxy_note_exposed(tmp_path: Path):
    client, _cfg = _rules_client(tmp_path)
    m60 = 'DIF60:="MACD.DIF#MIN60";\nXG:C>0 AND DIF60>0;'
    created = client.post(
        "/api/v1/rules", json={"name": "代理note", "formula_text": m60}
    )
    assert created.status_code == 200, created.text
    assert created.json()["min60_proxy_note"]
    listed = next(
        r for r in client.get("/api/v1/rules").json()
        if r["id"] == created.json()["id"]
    )
    assert listed["min60_proxy_note"] == created.json()["min60_proxy_note"]

    plain = client.post(
        "/api/v1/rules", json={"name": "普通note", "formula_text": "XG:C>0;"}
    ).json()
    assert plain["min60_proxy_note"] == ""


def test_rules_min60_native_exposed_default_false(tmp_path: Path):
    client, _cfg = _rules_client(tmp_path)
    created = client.post(
        "/api/v1/rules", json={"name": "原生标记", "formula_text": "XG:C>0;"}
    )
    assert created.status_code == 200, created.text
    rid = created.json()["id"]
    assert created.json()["min60_native"] is False

    listed = next(r for r in client.get("/api/v1/rules").json() if r["id"] == rid)
    assert listed["min60_native"] is False

    detail = client.get(f"/api/v1/rules/{rid}").json()
    assert detail["min60_native"] is False


def test_rules_benchmark_profile_route_order(tmp_path: Path):
    client, _cfg = _rules_client(tmp_path)
    r = client.get("/api/v1/rules/benchmark-profile")
    assert r.status_code == 200, r.text
    assert r.json()["profile"]["profile_id"] == "rule_benchmark_v1"