# -*- coding: utf-8 -*-
"""独立复验（安全/并发修复轮）：
A. HTTP 路径穿越 / 别名规范路径
B. 并发 create / add_category / 损坏 sidecar 容错
C. Excel sheet 名与公式注入
D. import 后缀小写化 + 写失败无残留
E. 边界：Pydantic 长度 / 413 / note 截断 / computed-ok 兜底 / no_go 短名
F. legacy index.html 转义 + index_v3 导入编码回退与分类降级
G. 内置规则 DELETE 改 400 的前端影响核实

不改业务代码；仅新增测试。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote

import pytest

import tests.apps.astock.conftest  # noqa: F401
from tests.apps.astock.export_layout import data_rows

from wtpy.apps.astock.config import AStockConfig, get_default_config
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.service.rules import RuleService

BACKEND_ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = BACKEND_ROOT / "wtpy" / "apps" / "astock" / "web" / "static"
V3_HTML = STATIC_DIR / "index_v3.html"
LEGACY_HTML = STATIC_DIR / "index.html"
BAGUA_JSON = (
    BACKEND_ROOT / "wtpy" / "apps" / "astock" / "bagua" / "bagua_384.json"
)

_DS_META_MOCK = {
    "dataset_id": "mock",
    "dataset_source": "tdxquant",
    "dataset_adjustment": "front",
    "dataset_status": "ready",
    "covers_asof": True,
    "candidate_datasets": 1,
}
ASOF = 20260828


# ---------------------------------------------------------------------------
# 公共
# ---------------------------------------------------------------------------


def _weekdays(start: str, end: str) -> list:
    out = []
    cur = _dt.date.fromisoformat(start)
    end_d = _dt.date.fromisoformat(end)
    while cur <= end_d:
        if cur.isoweekday() <= 5:
            out.append(int(cur.strftime("%Y%m%d")))
        cur += _dt.timedelta(days=1)
    return out


def _rising_bars() -> list:
    days = [d for d in _weekdays("2026-06-01", "2026-08-28") if d <= ASOF]
    closes = [10.0 + i * 0.05 for i in range(len(days))]
    return [DayBar(d, c, c + 0.01, c - 0.01, c, 1e6, 1e7) for d, c in zip(days, closes)]


def _cfg(tmp_path: Path) -> AStockConfig:
    cfg = get_default_config(
        storage_root=tmp_path / "st",
        indicator_dir=tmp_path / "ind",
        output_root=tmp_path / "out",
    )
    Path(cfg.storage_root).mkdir(parents=True, exist_ok=True)
    Path(cfg.indicator_dir).mkdir(parents=True, exist_ok=True)
    return cfg


def _make_app(cfg):
    from wtpy.apps.astock.api import create_app

    return create_app(cfg)


def _make_client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture()
def rules_client(tmp_path: Path):
    pytest.importorskip("fastapi")
    cfg = _cfg(tmp_path)
    app = _make_app(cfg)
    try:
        yield _make_client(app), cfg
    finally:
        app.state.astock.jobs.shutdown(wait=False)


def _mock_export_data(monkeypatch, cfg: AStockConfig, bars: list) -> None:
    from wtpy.apps.astock.service import bagua_query as bq

    monkeypatch.setattr(
        bq, "_load_dataset_bars", lambda *_a, **_k: (bars, dict(_DS_META_MOCK))
    )
    monkeypatch.setattr(
        bq,
        "BaguaPlaneSession",
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("no md")),
    )
    monkeypatch.setattr(
        bq,
        "_resolve_batch_codes",
        lambda cfg_, codes=None, *, all_stocks=False: ["SSE.STK.600000"],
    )
    monkeypatch.setattr(bq, "list_etf_std_codes", lambda cfg_: [])


def _mock_review_data(monkeypatch, cfg: AStockConfig, bars: list) -> None:
    from wtpy.apps.astock.service import indicator_review as ir

    monkeypatch.setattr(
        ir,
        "_resolve_formal_surface",
        lambda _cfg: ({"formal_l1_id": "mock_l1", "max_date": ASOF}, ""),
    )
    monkeypatch.setattr(
        ir,
        "_default_bar_loader_factory",
        lambda _cfg: (
            lambda code, asof: (
                [b for b in bars if int(b.date) <= int(asof)],
                {"dataset_id": "mock"},
            )
        ),
    )


def _export(cfg, selected):
    from wtpy.apps.astock.service import bagua_query as bq

    info: dict = {}
    path = bq.export_bagua_multi_period_xlsx(
        cfg,
        date="2026-08-28",
        periods=["WEEK", "MONTH"],
        adjust="tushare_qfq",
        all_stocks=True,
        review_rules=selected,
        info_out=info,
    )
    return path, info


def _meta(path) -> dict:
    import openpyxl

    wb = openpyxl.load_workbook(path)
    return {
        row[0].value: row[1].value
        for row in wb["meta"].iter_rows(min_row=2)
        if row[0].value is not None
    }


def _write_review(cfg: AStockConfig, asof: int, rules: list) -> None:
    d = Path(cfg.storage_root) / "indicator_review"
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "asof": int(asof),
        "generated_at": "2026-08-28 19:00:00",
        "status": "ok",
        "no_go_reason": "",
        "universe_size": 1,
        "scanned": 1,
        "error_count": 0,
        "rules": rules,
    }
    (d / f"review_{asof}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _snapshot(root: Path) -> dict:
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = p.read_bytes()
        else:
            out[p.relative_to(root).as_posix()] = None
    return out


def _extract_js_function(src: str, name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + name + r"\s*\(", src)
    assert m, f"function not found: {name}"
    # 先配平参数括号（支持解构默认值如 { fromHistory = false } = {}）
    paren = src.index("(", m.start())
    depth = 0
    i = paren
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    brace = src.index("{", i)
    depth = 0
    for j in range(brace, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start(): j + 1]
    raise AssertionError(f"unbalanced {name}")


def _run_node(script: str) -> str:
    node = shutil.which("node")
    assert node, "node required"
    proc = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        cwd=str(BACKEND_ROOT),
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    return proc.stdout


# ===========================================================================
# A. 路径穿越 / 别名
# ===========================================================================


def test_round4_http_traversal_rejected_and_no_outside_io(rules_client, tmp_path):
    client, cfg = rules_client
    created = client.post(
        "/api/v1/rules", json={"name": "穿越目标", "formula_text": "XG:C>0;"}
    ).json()
    rid = created["id"]
    user_dir = Path(cfg.storage_root) / "indicators" / "user"
    canonical = user_dir / f"{rid}.txt"
    assert canonical.exists()

    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("SENTINEL", encoding="utf-8")
    before = _snapshot(tmp_path)

    evil_ids = [
        "user_..%5C..%5C..%5Csentinel",
        "user_..%2e%2e%5C..%5Csentinel",
        "user_..%5C..%2F..%5Csentinel",
        "USER_..%5C..%5Csentinel",
        "user_..%5C..%5C..%5C..%5Cwindows",
    ]
    for raw in evil_ids:
        r1 = client.patch(
            f"/api/v1/rules/{raw}", json={"formula_text": "XG:C>1;"}
        )
        assert r1.status_code in (400, 404), (raw, r1.status_code, r1.text)
        r2 = client.delete(f"/api/v1/rules/{raw}?permanent=true")
        assert r2.status_code in (400, 404), (raw, r2.status_code, r2.text)

    assert sentinel.read_text(encoding="utf-8") == "SENTINEL"
    assert canonical.read_text(encoding="utf-8").strip() == "XG:C>0;", (
        "合法规则文件不得被穿越请求改写"
    )
    assert _snapshot(tmp_path) == before, "穿越请求不得产生任何文件/目录变化"


def test_round4_alias_patch_delete_use_canonical_path(rules_client):
    client, cfg = rules_client
    created = client.post(
        "/api/v1/rules",
        json={"name": "user_别名乙", "formula_text": "XG:C>0;"},
    ).json()
    canonical = created["id"]
    assert canonical.startswith("user_")
    assert canonical != "user_别名乙"

    alias_url = quote("user_别名乙", safe="")
    patched = client.patch(
        f"/api/v1/rules/{alias_url}", json={"formula_text": "XG:C>1;"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["id"] == canonical

    user_dir = Path(cfg.storage_root) / "indicators" / "user"
    assert (user_dir / f"{canonical}.txt").read_text(encoding="utf-8").strip() == "XG:C>1;"
    assert not (user_dir / "user_别名乙.txt").exists(), "别名不得作为落盘文件名"

    deleted = client.delete(f"/api/v1/rules/{alias_url}?permanent=true")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["id"] == canonical and deleted.json()["deleted"] is True
    assert not (user_dir / f"{canonical}.txt").exists()


def test_round4_user_rule_unicode_slug_edit_delete_boundary(rules_client):
    """收口复验：Unicode 词字符规则名（é/日文/西里尔/韩文/中文）可创建且
    PATCH/DELETE 正常；穿越变体仍被拒绝。"""
    client, cfg = rules_client
    user_dir = Path(cfg.storage_root) / "indicators" / "user"
    names = ["Café趋势", "テスト規則", "Кирилл规则", "한국규칙", "中文规则名"]

    for nm in names:
        created = client.post(
            "/api/v1/rules", json={"name": nm, "formula_text": "XG:C>0;"}
        )
        assert created.status_code == 200, (nm, created.text)
        rid = created.json()["id"]
        assert rid.startswith("user_")
        url = quote(rid, safe="")
        patched = client.patch(
            f"/api/v1/rules/{url}", json={"description": f"{nm}说明"}
        )
        assert patched.status_code == 200, (nm, rid, patched.status_code, patched.text)
        assert patched.json()["description"] == f"{nm}说明"
        assert (user_dir / f"{rid}.txt").exists(), rid
        deleted = client.delete(f"/api/v1/rules/{url}?permanent=true")
        assert deleted.status_code == 200, (nm, rid, deleted.status_code, deleted.text)
        assert deleted.json()["id"] == rid and deleted.json()["mode"] == "hard"
        assert not (user_dir / f"{rid}.txt").exists()

    # 穿越变体仍拒（400/404），且不产生越界文件
    sentinel = Path(cfg.indicator_dir).parent / "escape5.txt"
    sentinel.write_text("S5", encoding="utf-8")
    before = _snapshot(Path(cfg.indicator_dir).parent)
    for raw in (
        "user_..%5C..%5C..%5Cescape5",
        "user_..%2F..%2Fescape5",
        "user_%2e%2e%5Cescape5",
        "USER_..%5Cescape5",
    ):
        assert client.patch(
            f"/api/v1/rules/{raw}", json={"description": "x"}
        ).status_code in (400, 404)
        assert client.delete(
            f"/api/v1/rules/{raw}?permanent=true"
        ).status_code in (400, 404)
    assert sentinel.read_text(encoding="utf-8") == "S5"
    assert _snapshot(Path(cfg.indicator_dir).parent) == before


# ===========================================================================
# B. 并发 / 损坏容错
# ===========================================================================


def test_round4_concurrent_create_all_registered_multi_instance(tmp_path):
    cfg = _cfg(tmp_path)
    names = [f"独立并发规则{i}" for i in range(8)]
    errors: list = []
    barrier = threading.Barrier(len(names))

    def _worker(nm):
        try:
            barrier.wait(timeout=10.0)
            RuleService(cfg).create_rule(name=nm, formula_text="XG:C>0;")
        except Exception as e:  # noqa: BLE001
            errors.append((nm, e))

    threads = [threading.Thread(target=_worker, args=(nm,)) for nm in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30.0)
    assert errors == [], errors

    svc = RuleService(cfg)
    rows = svc.list_rules()
    present = {r["name"] for r in rows}
    for nm in names:
        assert nm in present, nm
    for r in rows:
        if r["name"] in names:
            assert r["id"].startswith("user_")
            detail = svc.get_rule(r["id"], include_formula=True)
            assert Path(detail["source_file"]).exists(), r["id"]

    reg_path = Path(cfg.storage_root) / "indicators" / "user_registry.json"
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    registered = {i["id"] for i in data.get("indicators", [])}
    for r in rows:
        if r["name"] in names:
            assert r["id"] in registered


def test_round4_concurrent_add_category_no_loss_and_valid_json(tmp_path):
    cfg = _cfg(tmp_path)
    cats = [f"独立并发分类{i}" for i in range(8)]
    barrier = threading.Barrier(len(cats))

    def _worker(cat):
        barrier.wait(timeout=10.0)
        RuleService(cfg).add_category(cat)

    threads = [threading.Thread(target=_worker, args=(c,)) for c in cats]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30.0)

    final = RuleService(cfg).list_categories()
    for c in cats:
        assert c in final, c
    path = Path(cfg.storage_root) / "indicators" / "rule_categories.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["categories"] == final


def test_round4_corrupt_sidecars_degrade_without_500(rules_client):
    client, cfg = rules_client
    ind = Path(cfg.storage_root) / "indicators"
    ind.mkdir(parents=True, exist_ok=True)
    (ind / "user_registry.json").write_text("{broken json", encoding="utf-8")
    (ind / "rule_categories.json").write_text("[[[not-json", encoding="utf-8")
    (ind / "registry.json").write_text("garbage", encoding="utf-8")

    assert client.get("/api/v1/rules").status_code == 200
    assert client.get("/api/v1/rules/categories").json() == {"categories": []}
    assert client.post("/api/v1/rules/batch-validate", json={}).status_code == 200

    svc = RuleService(cfg)
    assert svc.list_categories() == []
    assert svc.batch_validate()["summary"]["total"] >= 1
    assert isinstance(svc.list_rules(), list)

    # 损坏后仍可正常新建并立即可见
    r = client.post(
        "/api/v1/rules", json={"name": "损坏后新建", "formula_text": "XG:C>0;"}
    )
    assert r.status_code == 200, r.text
    ids = {x["id"] for x in client.get("/api/v1/rules").json()}
    assert r.json()["id"] in ids


# ===========================================================================
# C. Excel 注入
# ===========================================================================


def test_round4_excel_unsafe_names_and_no_formula_cells(tmp_path, monkeypatch):
    if not BAGUA_JSON.exists():
        pytest.skip("bagua_384.json missing")
    cfg = _cfg(tmp_path)
    bars = _rising_bars()
    _mock_export_data(monkeypatch, cfg, bars)
    _mock_review_data(monkeypatch, cfg, bars)

    svc = RuleService(cfg)
    r_eq = svc.create_rule(name="=1+1", formula_text="XG:C>0;")
    r_ctl = svc.create_rule(name="\x01ctl\x7f", formula_text="XG:C>0;")
    r_hist = svc.create_rule(name="History", formula_text="XG:C>0;")

    path, _info = _export(cfg, [r_eq["id"], r_ctl["id"], r_hist["id"]])

    import openpyxl

    wb = openpyxl.load_workbook(path)
    signal = [
        n
        for n in wb.sheetnames
        if n not in ("meta", "stock-all", "index-all", "etf-all")
    ]
    assert len(signal) == 3, wb.sheetnames
    for name in signal:
        assert 0 < len(name) <= 31, name
        assert not (set(name) & set("[]:*?/\\")), name
        assert name[:1] not in "=+-@", name
        assert name.lower() not in {
            "meta", "stock-all", "index-all", "etf-all", "history"
        }
    assert "_1+1" in signal, signal
    assert "_ctl_" in signal, signal
    assert any(n.startswith("user_History") for n in signal), signal

    for sheet in ["meta"] + signal:
        for row in wb[sheet].iter_rows():
            for cell in row:
                assert cell.data_type != "f", (sheet, cell.coordinate, cell.value)


def test_round4_rule_named_index_all_case_variant_never_collides(tmp_path, monkeypatch):
    """回归：规则显示名为内置 index-all 的大小写变体时必须回退改名。

    Excel sheet 名大小写不敏感——修复前 _SHEET_RESERVED_NAMES 漏收
    "index-all"，名为 "Index-All" 的规则会生成与工作簿自带 index-all
    仅大小写不同的 sheet，文件被 Excel 判为损坏；精确小写 "index-all"
    则被导出侧局部保留字检查静默丢弃（规则无 sheet）。修复后两种输入
    都回退到 rule_id 派生名，规则仍有自己的 sheet。

    注意两条规则的回退名本身仅大小写不同（user_index_all_X /
    user_Index_All_X）：_unique_sheet_name 按小写镜像集再消解，第二条
    变为「截断前缀+~seed」形式——两个 sheet 按小写口径互不相同，
    且各自仍可从 rule_id 派生前缀/种子辨认。"""
    if not BAGUA_JSON.exists():
        pytest.skip("bagua_384.json missing")
    cfg = _cfg(tmp_path)
    bars = _rising_bars()
    _mock_export_data(monkeypatch, cfg, bars)
    _mock_review_data(monkeypatch, cfg, bars)

    svc = RuleService(cfg)
    r_exact = svc.create_rule(name="index-all", formula_text="XG:C>0;")
    r_mixed = svc.create_rule(name="Index-All", formula_text="XG:C>0;")

    path, _info = _export(cfg, [r_exact["id"], r_mixed["id"]])

    import openpyxl

    wb = openpyxl.load_workbook(path)
    # 内置指数表仍是唯一的 index-all（大小写不敏感计数）
    assert sum(1 for n in wb.sheetnames if n.lower() == "index-all") == 1
    signal = [
        n
        for n in wb.sheetnames
        if n not in ("meta", "stock-all", "index-all", "etf-all")
    ]
    # 两条规则都出了 sheet，且名字都不是 index-all 的大小写变体
    assert len(signal) == 2, wb.sheetnames
    assert all(n.lower() != "index-all" for n in signal)
    # 两个 sheet 按小写口径互不相同（回退名彼此也仅大小写不同，必须再消解）
    assert signal[0].lower() != signal[1].lower(), signal
    # 首条规则保留完整回退名（=rule_id）；第二条被消解，仍由 rule_id 派生
    assert r_exact["id"] in signal, signal
    other = [n for n in signal if n != r_exact["id"]][0]
    assert other != r_mixed["id"] and r_mixed["id"][:12] in other, signal


def test_round4_excel_formula_lead_meta_cell_neutralized(tmp_path, monkeypatch):
    if not BAGUA_JSON.exists():
        pytest.skip("bagua_384.json missing")
    cfg = _cfg(tmp_path)
    bars = _rising_bars()
    _mock_export_data(monkeypatch, cfg, bars)
    # 伪造复核 JSON：rule_id/sheet 以公式起始字符开头（防御纵深场景）
    _write_review(
        cfg,
        ASOF,
        [{"rule_id": "=1+1", "sheet": "=1+1", "count": 0, "matched": []}],
    )
    path, _info = _export(cfg, ["=1+1"])

    import openpyxl

    wb = openpyxl.load_workbook(path)
    by_key = {
        row[0].value: row[1]
        for row in wb["meta"].iter_rows(min_row=2)
        if row[0].value is not None
    }
    for key in ("indicator_review_rules_selected", "indicator_review_rule_sources"):
        cell = by_key[key]
        assert str(cell.value).startswith("'"), (key, cell.value)
        assert cell.data_type == "s", (key, cell.data_type)
    for row in wb["meta"].iter_rows():
        for cell in row:
            assert cell.data_type != "f"


def test_round4_excel_safe_cell_and_note_reason_unit():
    from wtpy.apps.astock.service.bagua_query import _excel_safe_cell, _note_reason

    for lead in ("=", "+", "-", "@"):
        assert _excel_safe_cell(lead + "x") == "'" + lead + "x"
    assert _excel_safe_cell("正常") == "正常"
    assert _excel_safe_cell(None) is None
    assert _excel_safe_cell(3) == 3

    reason = _note_reason("第一行\n第二行;第三行；x" * 20, limit=80)
    assert "\n" not in reason and "\r" not in reason
    assert ";" not in reason and "；" not in reason
    assert len(reason) <= 80


# ===========================================================================
# D. import
# ===========================================================================


def test_round4_import_uppercase_normalized_and_listed(rules_client):
    client, cfg = rules_client
    ind = Path(cfg.indicator_dir)
    r = client.post(
        "/api/v1/rules/import", json={"filename": "UPPER.TXT", "content": "XG:C>0;"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "UPPER"
    names_on_disk = {p.name for p in ind.iterdir()}
    # 落盘名必须是规范化的小写后缀（Windows 大小写不敏感，按目录实际名字断言）
    assert "UPPER.txt" in names_on_disk, names_on_disk
    assert "UPPER.TXT" not in names_on_disk, names_on_disk
    assert any(x["id"] == body["id"] for x in client.get("/api/v1/rules").json())
    # 同名（忽略大小写）必须 400
    dup = client.post(
        "/api/v1/rules/import", json={"filename": "upper.txt", "content": "XG:C>0;"}
    )
    assert dup.status_code == 400


def test_round4_import_write_failure_no_residue(rules_client, monkeypatch):
    client, cfg = rules_client
    ind = Path(cfg.indicator_dir)
    before = _snapshot(ind)

    import wtpy.apps.astock.service.rules as rules_mod

    def _boom(path, text, encoding="utf-8"):
        raise OSError("disk full simulated")

    monkeypatch.setattr(rules_mod, "atomic_write_text", _boom)
    r = client.post(
        "/api/v1/rules/import", json={"filename": "写失败.txt", "content": "XG:C>0;"}
    )
    assert r.status_code == 400, r.text
    assert "写入导入文件失败" in str(r.json().get("detail"))
    assert not (ind / "写失败.txt").exists()
    assert not list(ind.glob(".tmp-*"))
    assert _snapshot(ind) == before, "写失败不得残留半截/临时文件"
    assert not any(
        x["name"] == "写失败" for x in client.get("/api/v1/rules").json()
    )


# ===========================================================================
# E. 边界
# ===========================================================================


def test_round4_payload_limits_and_413(rules_client):
    client, _cfg = rules_client
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
    assert client.post(
        "/api/v1/rules/batch-validate", json={"ids": ["r"] * 201}
    ).status_code == 422

    # 超 2MB → 413（中间件）；略小于 2MB 不触发 413
    huge = "x" * (2 * 1024 * 1024 + 1)
    assert client.post(
        "/api/v1/rules/validate", json={"formula_text": huge, "name": "t"}
    ).status_code == 413
    below = "y" * (2 * 1024 * 1024 - 100)
    r = client.post(
        "/api/v1/rules/validate", json={"formula_text": below, "name": "t"}
    )
    assert r.status_code == 200, r.status_code
    assert r.json().get("ok") is False


def test_round4_placeholder_note_truncated_single_line(tmp_path, monkeypatch):
    if not BAGUA_JSON.exists():
        pytest.skip("bagua_384.json missing")
    cfg = _cfg(tmp_path)
    bars = _rising_bars()
    _mock_export_data(monkeypatch, cfg, bars)

    from wtpy.apps.astock.service import bagua_query as bq

    long_reason = "第一行\n第二行;" + "长" * 200 + "；尾"
    monkeypatch.setattr(
        bq,
        "_compute_rules_for_export",
        lambda cfg_, asof, rule_ids, *, codes=None, on_progress=None: {
            "asof": asof,
            "status": "error",
            "error_note": long_reason,
            "rules": [],
        },
    )
    path, _info = _export(cfg, ["user_trunc_a", "user_trunc_b"])
    meta = _meta(path)
    note = str(meta["indicator_review_note"])
    assert "\n" not in note and "\r" not in note
    assert "placeholder:user_trunc_a(" in note
    assert "placeholder:user_trunc_b(" in note
    m = re.search(r"placeholder:user_trunc_a\((.*?)\)", note)
    assert m, note
    assert len(m.group(1)) <= 80
    assert "\n" not in m.group(1) and ";" not in m.group(1)
    ph = str(meta["indicator_review_placeholders"])
    assert "user_trunc_a=" in ph and "user_trunc_b=" in ph
    # 占位 sheet 仍存在
    import openpyxl

    wb = openpyxl.load_workbook(path)
    assert "user_trunc_a" in wb.sheetnames and data_rows(wb["user_trunc_a"]) == []


def test_round4_computed_ok_no_sheet_gets_placeholder(tmp_path, monkeypatch):
    if not BAGUA_JSON.exists():
        pytest.skip("bagua_384.json missing")
    cfg = _cfg(tmp_path)
    bars = _rising_bars()
    _mock_export_data(monkeypatch, cfg, bars)

    from wtpy.apps.astock.service import bagua_query as bq

    monkeypatch.setattr(
        bq,
        "_compute_rules_for_export",
        lambda cfg_, asof, rule_ids, *, codes=None, on_progress=None: {
            "asof": asof,
            "status": "ok",
            "rules": [],  # 整体 ok 但未返回任何规则
        },
    )
    path, _info = _export(cfg, ["user_nosheet1", "user_nosheet2"])
    import openpyxl

    wb = openpyxl.load_workbook(path)
    for rid in ("user_nosheet1", "user_nosheet2"):
        assert rid in wb.sheetnames, wb.sheetnames
        assert data_rows(wb[rid]) == []
    meta = _meta(path)
    assert "computed_ok_no_sheet" in str(meta["indicator_review_placeholders"])
    note = str(meta["indicator_review_note"])
    assert "placeholder:user_nosheet1(computed_ok_no_sheet)" in note
    assert "已即时计算" in note


def test_round4_no_go_short_names_service_and_export(tmp_path, monkeypatch):
    if not BAGUA_JSON.exists():
        pytest.skip("bagua_384.json missing")
    cfg = _cfg(tmp_path)
    bars = _rising_bars()
    _mock_export_data(monkeypatch, cfg, bars)

    from wtpy.apps.astock.service import bagua_query as bq
    from wtpy.apps.astock.service import indicator_review as ir

    # 服务层：no_go 摘要的 sheet 名仍是 735/5日外
    summary = ir.run_weekly_review(
        cfg,
        asof=ASOF,
        rule_ids=["txt_735金叉及趋势", "txt_先跌后涨新版5日外"],
        persist=False,
        surface_resolver=lambda _cfg: (None, "no_formal_l1_product"),
    )
    assert summary["status"] == "no_go"
    assert [(r["rule_id"], r["sheet"]) for r in summary["rules"]] == [
        ("txt_735金叉及趋势", "735"),
        ("txt_先跌后涨新版5日外", "5日外"),
    ]

    # 导出层：no_go 占位也用短名
    monkeypatch.setattr(
        bq,
        "_compute_rules_for_export",
        lambda cfg_, asof, rule_ids, *, codes=None, on_progress=None: {
            "asof": asof,
            "status": "no_go",
            "no_go_reason": "no_formal_l1_product",
            "rules": [],
        },
    )
    path, _info = _export(cfg, ["txt_735金叉及趋势", "txt_先跌后涨新版5日外"])
    import openpyxl

    wb = openpyxl.load_workbook(path)
    assert "735" in wb.sheetnames and "5日外" in wb.sheetnames, wb.sheetnames
    assert data_rows(wb["735"]) == [] and data_rows(wb["5日外"]) == []
    note = str(_meta(path)["indicator_review_note"])
    assert "placeholder:txt_735金叉及趋势(no_formal_l1_product)" in note


def test_round4_rule_to_public_source_and_fields(rules_client):
    client, cfg = rules_client
    user = client.post(
        "/api/v1/rules", json={"name": "来源核对", "formula_text": "XG:C>0;"}
    ).json()
    imp = client.post(
        "/api/v1/rules/import", json={"filename": "来源导入.txt", "content": "XG:C>0;"}
    ).json()
    rows = {r["id"]: r for r in client.get("/api/v1/rules").json()}
    assert rows[user["id"]]["source"] == "user"
    assert rows[imp["id"]]["source"] == "builtin"
    assert rows["bagua_ohlc"]["source"] == "system"
    for rid in (user["id"], imp["id"], "bagua_ohlc"):
        assert "description" in rows[rid] and "category" in rows[rid]


# ===========================================================================
# F. 前端静态 / Node
# ===========================================================================


def test_round4_legacy_index_esc_node_and_render_templates():
    assert LEGACY_HTML.is_file()
    html = LEGACY_HTML.read_text(encoding="utf-8")
    fn = _extract_js_function(html, "esc")
    out = _run_node(
        fn
        + "\n"
        + """
function assert(c, m) { if (!c) throw new Error(m); }
assert(esc('<img src=x onerror=alert(1)>') === '&lt;img src=x onerror=alert(1)&gt;', "tag not escaped");
assert(esc('" onmouseover="x') === '&quot; onmouseover=&quot;x', "quote not escaped");
assert(esc("a'b&c") === 'a&#39;b&amp;c', "apostrophe/amp not escaped");
assert(esc(null) === "", "null");
console.log("PASS legacy esc");
"""
    )
    assert "PASS legacy esc" in out

    render = _extract_js_function(html, "renderRules")
    for token in ("esc(r.name)", "esc(r.id)", "esc(r.compile_status)", "esc(r.failure_reason)"):
        assert token in render, token
    assert 'esc(r.error || "校验失败")' in html


def test_round4_v3_import_decode_gbk_and_category_fallback():
    assert V3_HTML.is_file()
    src = V3_HTML.read_text(encoding="utf-8")
    assert len(re.findall(r"function\s+decodeRuleImportBuffer\s*\(", src)) == 1
    fn = _extract_js_function(src, "decodeRuleImportBuffer")
    out = _run_node(
        fn
        + "\n"
        + """
function assert(c, m) { if (!c) throw new Error(m); }
var gbk = new Uint8Array([0xB2, 0xE2, 0xCA, 0xD4]);
assert(decodeRuleImportBuffer(gbk) === "\u6d4b\u8bd5", "gbk fallback failed: " + decodeRuleImportBuffer(gbk));
var utf8 = new TextEncoder().encode("\u8d8b\u52bf");
assert(decodeRuleImportBuffer(utf8) === "\u8d8b\u52bf", "utf-8 decode failed");
console.log("PASS decodeRuleImportBuffer");
"""
    )
    assert "PASS decodeRuleImportBuffer" in out

    # 编辑回填 rule.category；分类创建失败时前端本地降级
    editor = _extract_js_function(src, "openRuleEditor")
    assert "rule.category" in editor and "newRuleCategory" in editor
    cat = _extract_js_function(src, "createRuleCategory")
    assert "AppState.ruleCategories" in cat
    assert "已在前端本地生效" in cat
    assert "catch (e)" in cat


# ===========================================================================
# G. 内置规则删除改为 400 的影响
# ===========================================================================


def test_round4_builtin_delete_hide_default_list_and_restore(rules_client):
    """收口复验：非 user DELETE 恢复「仅隐藏」语义——200 hide、默认列表不可见、
    include_hidden 可见、restore 恢复；user_ 硬删；未知 id 404。"""
    client, cfg = rules_client
    imp = client.post(
        "/api/v1/rules/import",
        json={"filename": "内置删除验证.txt", "content": "XG:C>0;"},
    ).json()
    rid = imp["id"]
    assert rid.startswith("txt_")
    source = Path(cfg.indicator_dir) / "内置删除验证.txt"
    hidden_path = Path(cfg.storage_root) / "indicators" / "hidden_rule_ids.json"

    assert any(x["id"] == rid for x in client.get("/api/v1/rules").json())

    # DELETE → 200 hide：源文件保留、deleted=False
    r = client.delete(f"/api/v1/rules/{quote(rid, safe='')}?permanent=true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "id": rid,
        "deleted": False,
        "mode": "hide",
        "name": "内置删除验证",
        "note": "builtin/import rule hidden; source files kept",
    }
    assert source.exists(), "隐藏不得删除源文件"

    default_ids = {x["id"] for x in client.get("/api/v1/rules").json()}
    assert rid not in default_ids, "隐藏后默认列表不可见"
    row = next(
        x
        for x in client.get("/api/v1/rules?include_hidden=true").json()
        if x["id"] == rid
    )
    assert row["hidden"] is True
    assert json.loads(hidden_path.read_text(encoding="utf-8")) == [rid]

    # 幂等：重复隐藏集合不重复
    assert client.delete(
        f"/api/v1/rules/{quote(rid, safe='')}?permanent=true"
    ).status_code == 200
    assert json.loads(hidden_path.read_text(encoding="utf-8")) == [rid]

    # restore → 默认列表恢复、hidden 清空
    rr = client.post(f"/api/v1/rules/{quote(rid, safe='')}/restore")
    assert rr.status_code == 200 and rr.json()["hidden"] is False
    assert any(x["id"] == rid for x in client.get("/api/v1/rules").json())
    assert json.loads(hidden_path.read_text(encoding="utf-8")) == []

    # 内置 native 规则同样 hide/restore
    rb = client.delete("/api/v1/rules/bagua_ohlc?permanent=true")
    assert rb.status_code == 200 and rb.json()["mode"] == "hide"
    assert "bagua_ohlc" not in {x["id"] for x in client.get("/api/v1/rules").json()}
    assert client.post("/api/v1/rules/bagua_ohlc/restore").status_code == 200
    assert "bagua_ohlc" in {x["id"] for x in client.get("/api/v1/rules").json()}

    # user 规则：permanent=true 硬删（文件消失）；permanent=false 归档
    u = client.post(
        "/api/v1/rules", json={"name": "用户可删", "formula_text": "XG:C>0;"}
    ).json()
    ufile = Path(cfg.storage_root) / "indicators" / "user" / f"{u['id']}.txt"
    assert ufile.exists()
    rd = client.delete(f"/api/v1/rules/{u['id']}?permanent=true")
    assert rd.status_code == 200
    assert rd.json()["deleted"] is True and rd.json()["mode"] == "hard"
    assert not ufile.exists()

    u2 = client.post(
        "/api/v1/rules", json={"name": "用户可归档", "formula_text": "XG:C>0;"}
    ).json()
    ra = client.delete(f"/api/v1/rules/{u2['id']}?permanent=false")
    assert ra.status_code == 200 and ra.json().get("archived") is True
    assert not any(x["id"] == u2["id"] for x in client.get("/api/v1/rules").json())
    assert any(
        x["id"] == u2["id"]
        for x in client.get("/api/v1/rules?include_archived=true").json()
    )

    # 未知 id 404
    assert client.delete(
        "/api/v1/rules/txt_不存在的规则_zz?permanent=true"
    ).status_code == 404


def test_round4_frontend_builtin_delete_hide_flow_static():
    """前端内置规则删除入口与后端 hide 语义一致：v3 按 deletable 渲删除并走
    DELETE + loadRules 刷新；legacy 明确「从列表移除/仅隐藏」且同样走 DELETE。"""
    v3 = V3_HTML.read_text(encoding="utf-8")
    legacy = LEGACY_HTML.read_text(encoding="utf-8")

    table = _extract_js_function(v3, "renderRuleTable")
    assert "r.deletable !== false" in table
    assert 'data-act="del"' in table

    action = _extract_js_function(v3, "onRuleAction")
    del_block = action[action.index('act === "del"'):]
    assert "DELETE" in del_block
    assert "loadRules" in del_block
    assert "删除失败" in del_block

    # legacy：非 user 显示「从列表移除」+ 仅隐藏提示，同样走 DELETE
    assert "从列表移除" in legacy
    assert "仅隐藏" in legacy
    assert 'method: "DELETE"' in legacy


# ===========================================================================
# H. 收口复验：413 策略 / Excel 坏输入 / legacy 残余 sink / 三按钮
# ===========================================================================


def test_round4_body_limit_policy_rules_513kb_chunked_other_paths(rules_client):
    """413 策略：/rules 2MB 超限 413；513KB import 可达业务层（服务层 400）；
    chunked /rules 411；其他路径 3MB 不再 413。"""
    client, _cfg = rules_client

    huge = "x" * (2 * 1024 * 1024 + 1)
    r = client.post(
        "/api/v1/rules/validate", json={"formula_text": huge, "name": "t"}
    )
    assert r.status_code == 413, r.text

    r513 = client.post(
        "/api/v1/rules/import",
        json={"filename": "超限513.txt", "content": "x" * (513 * 1024)},
    )
    assert r513.status_code == 400, r513.text
    assert "too large" in str(r513.json().get("detail")).lower()

    def _chunked_body():
        yield b'{"name":"t","formula_text":"XG:C>0;"}'

    rc = client.post("/api/v1/rules/validate", content=_chunked_body())
    assert rc.status_code == 411, (rc.status_code, rc.text)

    # 非 /rules 路径：3MB 交给路由处理，不得被 413
    r_other = client.post(
        "/api/v1/experiments/estimate", json={"junk": "x" * (3 * 1024 * 1024)}
    )
    assert r_other.status_code == 200, r_other.text
    r_404 = client.post("/api/v1/does-not-exist", content=b"x" * (3 * 1024 * 1024))
    assert r_404.status_code == 404, r_404.text


def test_round4_export_bad_review_rule_id_does_not_500(tmp_path, monkeypatch):
    """`review_rules=["bad\\x01id"]` 服务层导出不 500：sheet 名/单元格/note
    均被清洗，workbook 可正常打开。"""
    if not BAGUA_JSON.exists():
        pytest.skip("bagua_384.json missing")
    cfg = _cfg(tmp_path)
    bars = _rising_bars()
    _mock_export_data(monkeypatch, cfg, bars)

    path, _info = _export(cfg, ["bad\x01id"])
    assert path.exists()

    import openpyxl

    wb = openpyxl.load_workbook(path)
    signal = [
        n for n in wb.sheetnames if n not in ("meta", "stock-all", "index-all")
    ]

    def _has_illegal(s: str) -> bool:
        return any(
            ord(ch) < 32
            or 0x7F <= ord(ch) <= 0x9F
            or ord(ch) in (0xFFFE, 0xFFFF)
            or 0xD800 <= ord(ch) <= 0xDFFF
            for ch in s
        )

    assert signal, wb.sheetnames
    for name in signal:
        assert not _has_illegal(name), repr(name)
    meta = _meta(path)
    for key in ("indicator_review_note", "indicator_review_placeholders"):
        assert not _has_illegal(str(meta.get(key) or "")), key
    for sheet in ["meta"] + signal:
        for row in wb[sheet].iter_rows():
            for cell in row:
                assert cell.data_type != "f", (sheet, cell.coordinate)
                if isinstance(cell.value, str):
                    assert not _has_illegal(cell.value), (sheet, cell.coordinate)


def test_round4_export_route_rejects_control_review_rule_id(rules_client):
    """HTTP 入口控制字符 review_rules → 400（不是 500）；GET 同样。"""
    client, _cfg = rules_client
    r = client.post(
        "/api/v1/bagua/export?async_mode=false",
        json={
            "codes": ["600000"],
            "all_stocks": False,
            "date": "2026-08-28",
            "periods": ["WEEK", "MONTH"],
            "review_rules": ["bad\x01id"],
        },
    )
    assert r.status_code == 400, r.text
    assert "控制字符" in str(r.json().get("detail")) or "非法" in str(
        r.json().get("detail")
    )
    rg = client.get(
        "/api/v1/bagua/export",
        params={
            "date": "2026-08-28",
            "all_stocks": "true",
            "async_mode": "false",
            "review_rules": "bad\x01id",
        },
    )
    assert rg.status_code == 400, rg.text
    # 数量/长度上限同样 400
    assert client.post(
        "/api/v1/bagua/export?async_mode=false",
        json={
            "codes": ["600000"],
            "all_stocks": False,
            "date": "2026-08-28",
            "review_rules": ["r%d" % i for i in range(201)],
        },
    ).status_code == 400


def test_round4_create_rule_uffff_and_control_text_sanitized(rules_client):
    """`x\\uffff` 名字创建被清洗；description 控制字符同样清洗；仍可编辑/删除。"""
    client, _cfg = rules_client
    r = client.post(
        "/api/v1/rules",
        json={
            "name": "x\uffff规则",
            "formula_text": "XG:C>0;",
            "description": "d\x01e",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "\uffff" not in body["name"]
    assert "\x01" not in str(body["description"])
    rid = body["id"]
    detail = client.get(f"/api/v1/rules/{quote(rid, safe='')}").json()
    assert "\uffff" not in detail["name"] and "\x01" not in str(detail["description"])
    row = next(
        x for x in client.get("/api/v1/rules").json() if x["id"] == rid
    )
    assert "\uffff" not in row["name"]
    assert client.patch(
        f"/api/v1/rules/{quote(rid, safe='')}", json={"description": "ok"}
    ).status_code == 200
    assert client.delete(
        f"/api/v1/rules/{quote(rid, safe='')}?permanent=true"
    ).status_code == 200


def test_round4_excel_formula_protection_no_regression(tmp_path, monkeypatch):
    """`=1+1` 规则名与伪造复核 sheet/rule_id 的公式防护不回退。"""
    if not BAGUA_JSON.exists():
        pytest.skip("bagua_384.json missing")
    cfg = _cfg(tmp_path)
    bars = _rising_bars()
    _mock_export_data(monkeypatch, cfg, bars)
    _mock_review_data(monkeypatch, cfg, bars)

    from wtpy.apps.astock.service.rules import RuleService

    svc = RuleService(cfg)
    r_eq = svc.create_rule(name="=1+1", formula_text="XG:C>0;")
    # 另加伪造复核规则：sheet 保留名 → 回退 rule_<hash> 安全名
    _write_review(
        cfg,
        ASOF,
        [
            {"rule_id": "=1+1", "sheet": "=1+1", "count": 0, "matched": []},
            {"rule_id": "meta", "sheet": "meta", "count": 0, "matched": []},
        ],
    )
    path, _info = _export(cfg, [r_eq["id"], "=1+1", "meta"])

    import openpyxl

    wb = openpyxl.load_workbook(path)
    signal = [
        n for n in wb.sheetnames if n not in ("meta", "stock-all", "index-all")
    ]
    assert "_1+1" in signal, wb.sheetnames
    assert len(signal) == len(set(signal)), signal
    assert not any(
        n.lower() in {"meta", "stock-all", "index-all", "etf-all", "history"}
        for n in signal
    )
    # 保留名即使来自伪造复核也被丢弃；rule_<hash> 兜底在单元层可用
    from wtpy.apps.astock.service.indicator_review import _sanitize_sheet_name

    fallback = _sanitize_sheet_name("meta", "meta")
    assert fallback.startswith("rule_") and len(fallback) <= 31
    for sheet in ["meta"] + signal:
        for row in wb[sheet].iter_rows():
            for cell in row:
                assert cell.data_type != "f", (sheet, cell.coordinate)


def test_round4_legacy_residual_sinks_escaped():
    """legacy index.html 残余 sink（信号徽标/卦象匹配块）使用 escHtml，
    且 esc 对恶意 payload 的 Node 真跑转义不回归。"""
    html = LEGACY_HTML.read_text(encoding="utf-8")
    assert len(re.findall(r"function\s+escHtml\s*\(", html)) == 1
    for fn_name in ("renderSignalBadge", "renderMatchBlock"):
        fn = _extract_js_function(html, fn_name)
        assert "escHtml(" in fn, fn_name
    # 残余：不得再有裸拼接 innerHTML 的规则字段
    render = _extract_js_function(html, "renderRules")
    assert "${r.name}" not in render
    assert "${r.id}" not in render

    fn = _extract_js_function(html, "esc")
    out = _run_node(
        fn
        + "\n"
        + """
function assert(c, m) { if (!c) throw new Error(m); }
var payload = '<img src=x onerror=alert(1)>';
assert(esc(payload) === '&lt;img src=x onerror=alert(1)&gt;', "esc sink");
assert(esc(payload).indexOf('<') < 0 && esc(payload).indexOf('>') < 0, "angle left");
console.log("PASS residual esc");
"""
    )
    assert "PASS residual esc" in out


def test_round4_ui_three_buttons_edit_and_new_clear():
    """规则弹窗三按钮（校验/取消/保存）唯一且接线；编辑 GET+PATCH；新建/取消
    resetRuleForm 清空表单。导出空 sheet/全规则 sheet 由 round3 服务层用例覆盖。"""
    src = V3_HTML.read_text(encoding="utf-8")
    for eid in ("btnValidateRule", "btnCancelCreate", "btnSaveRule"):
        assert src.count(f'id="{eid}"') == 1, eid
        assert f'$("{eid}")' in src, eid

    # 新建/取消 → resetRuleForm；保存 → PATCH(编辑)/POST(新建) 后 reset
    assert re.search(
        r'btnNewRule"\)\.onclick = \(\) => \{\s*resetRuleForm\(\)', src
    )
    assert re.search(
        r'btnCancelCreate"\)\.onclick = \(\) => \{\s*resetRuleForm\(\)', src
    )
    save = _extract_js_function(src, "saveNewRule")
    assert 'method: "PATCH"' in save
    assert '"/api/v1/rules"' in save
    assert "resetRuleForm()" in save
    editor = _extract_js_function(src, "openRuleEditor")
    assert 'api("/api/v1/rules/"' in editor
    reset = _extract_js_function(src, "resetRuleForm")
    for eid in (
        "newRuleName",
        "newRuleFormula",
        "newRuleDesc",
        "newRuleCategory",
        "newRuleStatus",
    ):
        assert eid in reset, eid


# ===========================================================================
# I. 最终小修复：toast 分模式 / 截断顺序 / FFFE-FFFF / 结果面板转义
# ===========================================================================


def test_round4_ui_delete_toast_by_mode_static():
    """v3 删除 toast 按后端 mode 区分：hard → 已删除；hide → 已从列表移除（可恢复）。"""
    src = V3_HTML.read_text(encoding="utf-8")
    action = _extract_js_function(src, "onRuleAction")
    del_block = action[action.index('act === "del"'):]
    assert 'res && res.mode === "hide"' in del_block
    assert '"已从列表移除（可恢复）"' in del_block
    assert '"已删除"' in del_block
    # 旧的无差别文案不得残留
    assert 'toast("已删除");' not in del_block


def test_round4_excel_safe_cell_truncation_boundaries():
    """前缀分支先截 32766 再加 `'`：任何输入结果 ≤32767；边界 32765/32766/32767/32768。"""
    from wtpy.apps.astock.service.bagua_query import (
        _EXCEL_MAX_CELL_LEN,
        _excel_safe_cell,
    )

    assert _EXCEL_MAX_CELL_LEN == 32767
    for n in (1, 32765, 32766, 32767, 32768, 40000):
        for lead in ("=", "+", "-", "@"):
            out = _excel_safe_cell(lead + "x" * (n - 1))
            assert out.startswith("'" + lead), (n, lead)
            assert len(out) <= _EXCEL_MAX_CELL_LEN, (n, lead, len(out))
    # 具体边界值
    assert len(_excel_safe_cell("=" + "x" * 32764)) == 32766
    assert len(_excel_safe_cell("=" + "x" * 32765)) == 32767
    assert len(_excel_safe_cell("=" + "x" * 32766)) == 32767
    # 非公式前缀：直接截到 32767，无前缀
    out = _excel_safe_cell("x" * 40000)
    assert len(out) == 32767 and not out.startswith("'")
    # 清洗仍先于截断（控制字符不会逃逸）
    dirty = "=" + "\x01" * 5 + "x" * 40000
    out2 = _excel_safe_cell(dirty)
    assert "\x01" not in out2 and len(out2) <= 32767 and out2.startswith("'")


def test_round4_review_rules_reject_noncharacters(rules_client):
    """review_rules 含 U+FFFE/U+FFFF：POST 与 GET 均 400（不是 500）。"""
    client, _cfg = rules_client
    for ch in ("\ufffe", "\uffff"):
        rp = client.post(
            "/api/v1/bagua/export?async_mode=false",
            json={
                "codes": ["600000"],
                "all_stocks": False,
                "date": "2026-08-28",
                "review_rules": [f"bad{ch}id"],
            },
        )
        assert rp.status_code == 400, (hex(ord(ch)), rp.status_code, rp.text)
        rg = client.get(
            "/api/v1/bagua/export",
            params={
                "date": "2026-08-28",
                "all_stocks": "true",
                "async_mode": "false",
                "review_rules": f"bad{ch}id",
            },
        )
        assert rg.status_code == 400, (hex(ord(ch)), rg.status_code, rg.text)


def test_round4_legacy_result_panel_escaped():
    """legacy 结果面板（showRunResult / renderSettingsKv / showMetrics /
    renderAdvancedKv）关键渲染路径均已 esc/escAttr。"""
    html = LEGACY_HTML.read_text(encoding="utf-8")
    for fn_name in (
        "showRunResult",
        "renderSettingsKv",
        "showMetrics",
        "renderAdvancedKv",
    ):
        fn = _extract_js_function(html, fn_name)
        assert "esc(" in fn or "escAttr(" in fn, fn_name
    # 具体高危字段必须包装
    assert 'esc(r.title || r.run_id || "")' in html
    assert 'esc(metricLabel(k))' in html
    assert 'esc(c.tip)' in html
    assert "escAttr(c.tip || c.k)" in html
    # Node 真跑 esc：结果面板可能承载的字符串 payload
    fn = _extract_js_function(html, "esc")
    out = _run_node(
        fn
        + "\n"
        + """
function assert(c, m) { if (!c) throw new Error(m); }
var p = '<img src=x onerror=alert(1)>';
assert(esc(p).indexOf('<') < 0 && esc(p).indexOf('>') < 0, "result panel esc");
assert(esc('a"b') === 'a&quot;b', "attr quote");
console.log("PASS result panel esc");
"""
    )
    assert "PASS result panel esc" in out


# ===========================================================================
# J. 卦象知识库渲染 XSS 转义（renderDetail/runSearch/renderActionBrowse）
# ===========================================================================


def _call_chain(src: str, pos: int, max_depth: int = 4):
    chain = []
    i = pos - 1
    for _ in range(max_depth):
        depth = 0
        found = None
        while i >= 0:
            ch = src[i]
            if ch == ")":
                depth += 1
            elif ch == "(":
                if depth == 0:
                    m = re.search(r"([A-Za-z_$][\w$]*)\s*$", src[:i])
                    if m:
                        found = (m.group(1), m.start())
                    break
                depth -= 1
            i -= 1
        if not found:
            break
        chain.append(found[0])
        i = found[1] - 1
        while i >= 0 and src[i] in " \t\r\n.":
            i -= 1
    return chain


_KB_TOKENS = (
    "hexagram_symbol", "main_hexagram_name", "gua_order", "core_gang", "gua_ci",
    "state_id", "main_hexagram_id", "line_name", "line_text",
    "market_summary", "market_judgement", "action_signal", "gaodao_commerce",
    "changed_hexagram_name", "biangua",
)
_KB_SINK_MARKERS = ("html +=", "innerHTML =", "const bg =", "const gd =")
_KB_APPROVED = {"esc", "escAttr", "escHtml"}


def _scan_kb_sinks(html: str) -> list:
    """返回 HTML 拼接语句里未被 esc 包裹的 KB 字段证据（应仅剩常量约束项）。"""
    findings = []
    for fn_name in ("renderDetail", "runSearch", "renderActionBrowse"):
        fn = _extract_js_function(html, fn_name)
        for line in fn.splitlines():
            if not any(mk in line for mk in _KB_SINK_MARKERS):
                continue
            for tok in _KB_TOKENS:
                for m in re.finditer(re.escape(tok), line):
                    after = line[m.end():].lstrip()
                    if after.startswith("?"):
                        continue  # 三元条件分支，不是插值
                    chain = _call_chain(line, m.start())
                    if set(chain) & _KB_APPROVED:
                        continue
                    if "emptyDisp" in chain and "esc" in chain:
                        continue
                    if re.search(r"indexOf\(\s*$", line[:m.start()]):
                        continue
                    findings.append((fn_name, tok, line.strip()[:120]))
    return findings


def test_kb_render_static_sink_escaping_both_uis():
    """静态证据：两套 UI 三个 KB 渲染函数的 HTML 拼接语句中，KB 字段一律
    esc/escAttr/emptyDisp+esc 包裹；renderActionBrowse 的 act 只来自常量清单。"""
    for path in (V3_HTML, LEGACY_HTML):
        html = path.read_text(encoding="utf-8")
        findings = _scan_kb_sinks(html)
        assert findings == [], (path.name, findings)
        browse = _extract_js_function(html, "renderActionBrowse")
        # 渲染循环只迭代硬编码操作语义清单，KB 任意 action_signal 不进 HTML
        assert 'order.concat(["其他"]).forEach(function(act)' in browse
        assert 'const act = (ln.action_signal || "").trim() || "其他";' in browse


def _build_kb_render_harness(html: str) -> str:
    esc_fn = _extract_js_function(html, "esc")
    fns = "\n".join(
        _extract_js_function(html, n)
        for n in ("renderDetail", "runSearch", "renderActionBrowse")
    )
    preamble = r"""
var payloadTag = '<img src=x onerror=alert(1)>';
var payloadAttr = "x' onmouseover='alert(1)";
var activeGuaId = payloadAttr;
var hitKbIndex = -1;
var hitsCache = [];
var activePreset = null;
var draft = { selected_state_ids: [], selected_main_hexagram_ids: [], selected_action_signals: [] };
var committed = { selected_state_ids: [], selected_main_hexagram_ids: [], selected_action_signals: [] };
var hexagrams = [{
  main_hexagram_id: payloadAttr,
  hexagram_symbol: payloadTag,
  main_hexagram_name: payloadTag,
  gua_order: 1,
  core_gang: payloadTag,
  gua_ci: payloadTag,
  lines: [
    { state_id: payloadAttr, line_name: payloadTag, line_text: payloadTag,
      market_summary: payloadTag, market_judgement: payloadTag,
      action_signal: "新开仓", biangua: payloadTag, gaodao_commerce: payloadTag },
    { state_id: "sid-evil", line_name: "evil", line_text: payloadTag,
      market_summary: payloadTag, action_signal: payloadTag, biangua: payloadTag }
  ]
}];
function makeEl() {
  return {
    innerHTML: "", checked: false, style: {}, dataset: {},
    className: "",
    classList: { add: function(){}, remove: function(){}, toggle: function(){} },
    querySelector: function(){ return makeEl(); },
    querySelectorAll: function(){ return []; },
    appendChild: function(){}
  };
}
var __el = {};
function $(id) { if (!__el[id]) __el[id] = makeEl(); return __el[id]; }
var __created = [];
var document = { createElement: function(){ var e = makeEl(); __created.push(e); return e; } };
function emptyDisp(v) { return String(v == null ? "" : v).trim() || "—"; }
function getMode() { return "main_hexagram"; }
function selectedCountForGua() { return 0; }
function toggleAllSix() {}
function toggleLine() {}
function renderNav() {}
function clone(x) { return JSON.parse(JSON.stringify(x)); }
function writeActions() {}
function markPresetButtons() {}
function updateSelCount() {}
function schedulePreview() {}
var __items = [{
  state_id: payloadAttr, main_hexagram_id: payloadAttr,
  hexagram_symbol: payloadTag, main_hexagram_name: payloadTag,
  line_name: payloadTag, line_text: payloadTag, action_signal: payloadAttr,
  biangua: payloadTag, changed_hexagram_name: payloadTag,
  gaodao_commerce: payloadTag
}];
function fetch(url) {
  return Promise.resolve({ json: function(){ return Promise.resolve({ items: __items }); } });
}
function assertNoRaw(html, label) {
  if (html.indexOf("<img") >= 0) throw new Error(label + ": raw <img>");
  if (html.indexOf("onmouseover='alert") >= 0) throw new Error(label + ": attr breakout");
  if (html.indexOf("x' onmouseover") >= 0) throw new Error(label + ": raw quote breakout");
  if (html.indexOf("&lt;img src=x onerror=alert(1)&gt;") < 0) throw new Error(label + ": payload not escaped");
  if (html.indexOf("&#39;") < 0) throw new Error(label + ": single quote not escaped");
}
"""
    epilogue = r"""
(async function () {
  renderDetail();
  assertNoRaw($("guaDetailPane").innerHTML, "renderDetail");
  await runSearch("q");
  var searchHtml = $("guaSearchHits").innerHTML +
    __created.map(function (e) { return e.innerHTML; }).join("");
  assertNoRaw(searchHtml, "runSearch");
  renderActionBrowse();
  var browse = $("guaActionBrowse").innerHTML;
  assertNoRaw(browse, "renderActionBrowse");
  if (browse.indexOf("sid-evil") >= 0) throw new Error("arbitrary action group rendered");
  console.log("PASS kb render");
})().catch(function (e) { console.error("FAIL", e && e.stack || e); process.exit(1); });
"""
    return esc_fn + "\n" + fns + preamble + epilogue


def test_kb_render_payloads_node_v3():
    """Node 真跑 V3 三个渲染函数：恶意 payload 无原始标签/属性逃逸，
    单引号被 esc 转为 &#39;。"""
    html = V3_HTML.read_text(encoding="utf-8")
    out = _run_node(_build_kb_render_harness(html))
    assert "PASS kb render" in out, out


def test_kb_render_payloads_node_legacy():
    html = LEGACY_HTML.read_text(encoding="utf-8")
    out = _run_node(_build_kb_render_harness(html))
    assert "PASS kb render" in out, out


def test_v3_esc_single_quote_added_without_regression():
    """V3 esc 新增 ' 转义：恶意串不残留裸引号；普通文本不受影响。"""
    html = V3_HTML.read_text(encoding="utf-8")
    fn = _extract_js_function(html, "esc")
    script = (
        fn
        + "\n"
        + r"""
function assert(c, m) { if (!c) throw new Error(m); }
assert(esc("x' onmouseover='alert(1)") === "x&#39; onmouseover=&#39;alert(1)", "single quote");
assert(esc("x' onmouseover='alert(1)").indexOf("'") < 0, "raw quote left");
assert(esc("<>&\"") === "&lt;&gt;&amp;&quot;", "angle/quote");
assert(esc("趋势 → 上涨 100%") === "趋势 → 上涨 100%", "normal text untouched");
assert(esc("A股/ETF（复权）") === "A股/ETF（复权）", "normal cjk untouched");
assert(esc(null) === "" && esc(undefined) === "", "nullish");
assert(esc("a&b") === "a&amp;b", "amp not over-encoded");
console.log("PASS v3 esc");
"""
    )
    out = _run_node(script)
    assert "PASS v3 esc" in out, out
    # 既有调用规模粗检（约 30+ 处），单引号修复不应改变调用形态
    assert len(re.findall(r"\besc\(", html)) >= 25
