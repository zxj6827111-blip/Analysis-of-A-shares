# -*- coding: utf-8 -*-
"""EOD 链尾自动全市场数据表（auto_export 服务 + CLI wiring + 下载 API）。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wtpy.apps.astock.service import auto_export as ae


def _cfg(tmp_path) -> SimpleNamespace:
    cfg = SimpleNamespace()
    cfg.storage_root = tmp_path / "astock"
    cfg.market_data_root = tmp_path / "md"
    cfg.storage_root.mkdir(parents=True, exist_ok=True)
    cfg.market_data_root.mkdir(parents=True, exist_ok=True)

    def _ensure_dirs():
        cfg.storage_root.mkdir(parents=True, exist_ok=True)
        cfg.market_data_root.mkdir(parents=True, exist_ok=True)

    cfg.ensure_dirs = _ensure_dirs
    return cfg


def _fake_export(path: Path, *_, **kwargs):
    """假导出器：写一个小 xlsx（含三张固定 sheet），回传 info。"""
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = "stock-all"
    wb["stock-all"].append(["code", "name"])
    wb["stock-all"].append(["600000", "浦发银行"])
    wb.create_sheet("index-all").append(["code", "name"])
    wb.create_sheet("etf-all").append(["code", "name"])
    wb.create_sheet("739 近5日")  # 被调用方传 review_rules=[] 时不应有此类表
    wb.remove(wb["739 近5日"])
    wb.save(path)
    info = kwargs.get("info_out")
    if info is not None:
        info.update({"query_date": 20260924})


def test_run_auto_export_writes_state_and_file(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    captured = {}

    def _export(_cfg, *, date, path, review_rules, info_out, **_kw):
        captured["date"] = date
        captured["review_rules"] = review_rules
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        _fake_export(out)
        return out

    from wtpy.apps.astock.service import bagua_query as _bq

    monkeypatch.setattr(_bq, "export_bagua_multi_period_xlsx", _export)
    res = ae.run_auto_export(cfg, date=20260924)
    assert res["status"] == "done"
    # review_rules=[] 显式空表——不传 None（None 会沿用全量信号 sheet）
    assert captured["review_rules"] == []
    assert Path(res["path"]).is_file()
    assert res["sheets"].get("stock-all", 0) >= 2

    state = json.loads(ae.auto_export_state_path(cfg).read_text(encoding="utf-8"))
    assert state["schema"] == ae.AUTO_EXPORT_SCHEMA
    assert state["status"] == "done"
    assert state["filename"] == Path(res["path"]).name
    # 自动产物带 auto 前缀，与手工导出 bagua_weekly_* 分开
    assert state["filename"].startswith(ae.AUTO_FILE_PREFIX)


def test_run_auto_export_failure_records_error(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    def _boom(*_a, **_kw):
        raise RuntimeError("export exploded")

    from wtpy.apps.astock.service import bagua_query as _bq

    monkeypatch.setattr(_bq, "export_bagua_multi_period_xlsx", _boom)
    res = ae.run_auto_export(cfg)
    # 附属产物失败不抛栈（链尾不能让同步整体变失败），但状态如实记 error
    assert res["status"] == "error"
    assert "export exploded" in res["error"]
    state = ae.load_auto_export_state(cfg)
    assert state["status"] == "error"


def test_auto_export_retention_prunes_oldest(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    def _export(_cfg, *, date, path, **_kw):
        out = Path(path)
        _fake_export(out)
        return out

    from wtpy.apps.astock.service import bagua_query as _bq

    monkeypatch.setattr(_bq, "export_bagua_multi_period_xlsx", _export)
    made = []
    for i in range(6):
        # 手工延迟让 mtime 有先后（Windows 时间戳粒度细，保险起见 sleep）
        res = ae.run_auto_export(cfg, date=20260921 + i, keep=3)
        assert res["status"] == "done"
        made.append(Path(res["path"]))
        import time as _t

        _t.sleep(0.02)
    left = sorted(
        p.name for p in ae.auto_export_dir(cfg).glob(f"{ae.AUTO_FILE_PREFIX}*.xlsx")
    )
    assert len(left) == 3
    assert made[0].name not in left and made[-1].name in left


def test_cli_export_weekly(tmp_path, monkeypatch):
    """CLI 子命令接线：--date 透传 + heavy-job 锁占用时 exit 3。"""
    from wtpy.apps.astock import cli

    class _Args(SimpleNamespace):
        storage = None
        tdx_root = None
        indicator_dir = None
        date = "20260924"
        keep = 2

    called = {}

    def _fake_run(cfg, *, date=None, keep=None):
        called["date"] = date
        called["keep"] = keep
        return {"status": "done", "export_date": date}

    monkeypatch.setenv("ASTOCK_STORAGE_ROOT_TEST", str(tmp_path))
    monkeypatch.setattr(
        cli, "_cfg_from_args",
        lambda _args: _cfg(tmp_path),
    )
    from wtpy.apps.astock.service import auto_export as _ae_mod

    monkeypatch.setattr(_ae_mod, "run_auto_export", _fake_run)
    rc = cli.cmd_export_weekly(_Args())
    assert rc == 0
    assert called["date"] == "20260924"
    assert called["keep"] == 2


def test_auto_export_api_endpoints(tmp_path, monkeypatch):
    """latest/download 端点：done 可直下、非 done 409、路径越界 403。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASTOCK_STORAGE_ROOT_TEST", str(tmp_path))
    from wtpy.apps.astock import api_routes
    from wtpy.apps.astock.api_routes import context as ctx_mod

    app = FastAPI()
    app.include_router(api_routes.bagua.router)
    cfg = _cfg(tmp_path)

    def _get_ctx():
        return SimpleNamespace(cfg=cfg)

    app.dependency_overrides[ctx_mod.get_ctx] = _get_ctx
    client = TestClient(app)

    # 无状态 → 404/available=False
    r = client.get("/api/v1/bagua/export/auto/latest")
    assert r.status_code == 200 and r.json()["available"] is False
    r = client.get("/api/v1/bagua/export/auto/download")
    assert r.status_code == 409

    # 写一份 done 状态 + 产物
    out = ae.auto_export_dir(cfg) / "auto_weekly_20260924_x.xlsx"
    ae.auto_export_dir(cfg).mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"PK\x03\x04 fake")
    ae.save_auto_export_state(
        cfg,
        {
            "status": "done",
            "export_date": 20260924,
            "finished_at": "2026-09-24 19:00:00",
            "path": str(out),
            "filename": out.name,
            "size_bytes": out.stat().st_size,
            "sheets": {"stock-all": 2},
        },
    )
    r = client.get("/api/v1/bagua/export/auto/latest")
    assert r.status_code == 200
    payload = r.json()
    assert payload["available"] is True
    assert payload["filename"] == out.name
    assert "path" not in payload  # 磁盘路径不外泄

    r = client.get("/api/v1/bagua/export/auto/download")
    assert r.status_code == 200
    assert r.content == b"PK\x03\x04 fake"

    # 状态文件被窜改为目录外路径 → 403（绝不发放任意文件）
    ae.save_auto_export_state(
        cfg,
        {"status": "done", "path": str(tmp_path / "outside.txt"),
         "filename": "outside.txt"},
    )
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    r = client.get("/api/v1/bagua/export/auto/download")
    assert r.status_code == 403
