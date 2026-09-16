# -*- coding: utf-8 -*-
"""跟踪「补算指定历史周」路由测试（2026-09-15）。

全部 tmp 隔离：不跑真 CLI（分钟级），用替身验证"校验 → 投递 → 状态"链路
与命令拼装口径（全局参数必须在子命令前）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.api_routes import track_backfill as tb
from wtpy.apps.astock.config import get_default_config


@pytest.fixture()
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir(parents=True)
    ind.mkdir(parents=True)
    (ind / "测试规则A.txt").write_text("MA5:=MA(C,5);\nXG:CROSS(C,MA5);", encoding="utf-8")
    cfg = get_default_config(
        storage_root=storage, indicator_dir=ind, output_root=tmp_path / "out"
    )
    c = TestClient(create_app(cfg))
    yield c, cfg
    c.close()


@pytest.fixture()
def fake_runner(monkeypatch):
    """替身：不真起子进程，直接标记 done（含一段输出，供状态展示断言）。"""
    calls = []

    def _fake(ctx, job):
        calls.append(dict(job))
        with ctx.track_backfill_lock:
            job["status"] = "done"
            job["exit_code"] = 0
            job["finished_at"] = tb._now()
            job["output"] = ["[TRACK] 20260731: complete（exit=0）"]

    monkeypatch.setattr(tb, "_run_backfill_job", _fake)
    return calls


def _wait_status(client, **params):
    for _ in range(50):
        r = client.get("/api/v1/bagua/track/backfill/status", params=params)
        assert r.status_code == 200
        body = r.json()
        if not body["running"]:
            return body
        time.sleep(0.02)
    raise AssertionError("worker 未在预期时间内结束")


class TestSignalDayGuard:
    """信号日必须是该周最后一个交易日（用户按「指定某一天」理解入口，
    填周中日期必须被入口挡住并告知正确日期，不能白跑两小时后才失败）。"""

    @staticmethod
    def _patch_calendar(monkeypatch):
        from wtpy.apps.astock.data.calendar import TradeCalendar
        from wtpy.apps.astock.service import screen_tracking as tracksvc

        # 2025-01-06(一)~01-10(五) 为交易周，01-08 是周中；01-11 周六
        cal = TradeCalendar(
            [20250106, 20250107, 20250108, 20250109, 20250110, 20250113]
        )
        monkeypatch.setattr(tracksvc, "_load_calendar_or_none", lambda _cfg: cal)
        return cal

    def test_week_middle_day_400_with_correct_hint(self, client, monkeypatch):
        c, _ = client
        self._patch_calendar(monkeypatch)
        r = c.post("/api/v1/bagua/track/backfill", json={"week": "20250108"})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "不是该周最后一个交易日" in detail
        assert "20250110" in detail, "错误提示必须给出该周正确日期: " + detail

    def test_non_trading_day_400(self, client, monkeypatch):
        c, _ = client
        self._patch_calendar(monkeypatch)
        r = c.post("/api/v1/bagua/track/backfill", json={"week": "20250111"})  # 周六
        assert r.status_code == 400
        assert "不是交易日" in r.json()["detail"]

    def test_week_last_trading_day_accepted(self, client, monkeypatch, fake_runner):
        c, _ = client
        self._patch_calendar(monkeypatch)
        r = c.post("/api/v1/bagua/track/backfill", json={"week": "20250110"})
        assert r.status_code == 200, r.text
        _wait_status(c)

    def test_calendar_unavailable_does_not_block(self, client, monkeypatch, fake_runner):
        """日历不可用时不拦（CLI 侧仍会 fail-closed）——不能因校验器故障阻断入口。"""
        from wtpy.apps.astock.service import screen_tracking as tracksvc

        monkeypatch.setattr(tracksvc, "_load_calendar_or_none", lambda _cfg: None)
        c, _ = client
        r = c.post("/api/v1/bagua/track/backfill", json={"week": "20260626"})
        assert r.status_code == 200, r.text
        _wait_status(c)


class TestValidation:
    def test_invalid_week_400(self, client):
        c, _ = client
        for bad in ("2026-7", "abc", "2026073", "202607311"):
            r = c.post("/api/v1/bagua/track/backfill", json={"week": bad})
            assert r.status_code == 400, bad

    def test_accepts_dashed_week(self, client, fake_runner):
        """2026-07-31 这种写法也要能用（去掉分隔符后 8 位）。"""
        c, _ = client
        r = c.post("/api/v1/bagua/track/backfill", json={"week": "2026-07-31"})
        assert r.status_code == 200
        assert r.json()["week"] == "20260731"

    def test_weeks_back_bounds_400(self, client):
        c, _ = client
        for bad in (0, 53, -1):
            r = c.post("/api/v1/bagua/track/backfill", json={"weeks_back": bad})
            assert r.status_code == 400, bad

    def test_requires_exactly_one(self, client):
        c, _ = client
        assert c.post("/api/v1/bagua/track/backfill", json={}).status_code == 400
        r = c.post(
            "/api/v1/bagua/track/backfill",
            json={"week": "20260731", "weeks_back": 3},
        )
        assert r.status_code == 400


class TestCommandBuild:
    def test_week_mode_puts_globals_before_subcommand(self, client):
        """--storage/--indicator-dir 必须在子命令前（放后面不生效）。"""
        _, cfg = client
        ctx = type("C", (), {"cfg": cfg})()

        cmd = tb._build_cmd(ctx, week="20260731", weeks_back=None)  # type: ignore[arg-type]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-u", "-m"]
        i_sub = cmd.index("track-weekly")
        assert cmd.index("--storage") < i_sub, "全局 --storage 必须在子命令前"
        assert cmd.index("--indicator-dir") < i_sub
        assert cmd[i_sub + 1:] == ["--week", "20260731", "--backfill", "1"]

    def test_weeks_back_mode(self, client):
        _, cfg = client
        ctx = type("C", (), {"cfg": cfg})()
        cmd = tb._build_cmd(ctx, week=None, weeks_back=8)  # type: ignore[arg-type]
        assert cmd[cmd.index("track-weekly") + 1:] == ["--backfill", "8"]


class TestSubmitAndStatus:
    def test_submit_then_status_done(self, client, fake_runner):
        c, _ = client
        r = c.post("/api/v1/bagua/track/backfill", json={"week": "20260731"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["status"] == "queued"
        assert "重建" in body["message"], "提交提示必须带回填口径免责说明"
        assert fake_runner and fake_runner[0]["_cmd"][-4:] == [
            "--week", "20260731", "--backfill", "1"
        ]
        st = _wait_status(c)
        job = st["jobs"][0]
        assert job["status"] == "done"
        assert job["exit_code"] == 0
        assert job["week"] == "20260731"
        assert "[TRACK] 20260731" in job["output"][0]
        assert "_cmd" not in job, "内部命令不得暴露在状态接口"

    def test_weeks_back_job_target(self, client, fake_runner):
        c, _ = client
        r = c.post("/api/v1/bagua/track/backfill", json={"weeks_back": 3})
        assert r.json()["weeks_back"] == 3
        assert "最近 3 周" in r.json()["message"]
        _wait_status(c)

    def test_retryable_exit_code_maps_to_retryable(self, client, monkeypatch):
        """CLI exit 3（锁被占用/版本变化）→ status=retryable，不是 failed。"""
        def _fake(ctx, job):
            with ctx.track_backfill_lock:
                job["status"] = "retryable"
                job["exit_code"] = 3

        monkeypatch.setattr(tb, "_run_backfill_job", _fake)
        c, _ = client
        c.post("/api/v1/bagua/track/backfill", json={"week": "20260731"})
        st = _wait_status(c)
        assert st["jobs"][0]["status"] == "retryable"

    def test_duplicate_in_flight_409(self, client, monkeypatch):
        """同周任务在途 → 409（防连点重复发起全市场扫描）。"""
        started = []

        def _blocking(ctx, job):
            started.append(job["job_id"])
            with ctx.track_backfill_lock:
                job["status"] = "running"
            time.sleep(0.3)  # 制造在途窗口
            with ctx.track_backfill_lock:
                job["status"] = "done"

        monkeypatch.setattr(tb, "_run_backfill_job", _blocking)
        c, _ = client
        assert c.post("/api/v1/bagua/track/backfill", json={"week": "20260731"}).status_code == 200
        time.sleep(0.05)
        dup = c.post("/api/v1/bagua/track/backfill", json={"week": "20260731"})
        assert dup.status_code == 409
        # 不同周不受影响
        other = c.post("/api/v1/bagua/track/backfill", json={"week": "20260724"})
        assert other.status_code == 200
        _wait_status(c)

    def test_status_empty_when_no_job(self, client):
        c, _ = client
        body = c.get("/api/v1/bagua/track/backfill/status").json()
        assert body["ok"] is True and body["jobs"] == [] and body["running"] == 0
