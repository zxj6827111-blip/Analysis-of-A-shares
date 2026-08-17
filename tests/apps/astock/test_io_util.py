from __future__ import annotations

from wtpy.apps.astock.data import io_util


def test_atomic_write_retries_transient_permission_error(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    real_replace = io_util.os.replace
    calls = {"count": 0}

    def flaky_replace(src, dst):
        calls["count"] += 1
        if calls["count"] < 3:
            raise PermissionError("transient Windows file lock")
        return real_replace(src, dst)

    monkeypatch.setattr(io_util.os, "replace", flaky_replace)
    io_util.atomic_write_text(path, "complete")

    assert path.read_text(encoding="utf-8") == "complete"
    assert calls["count"] == 3
    assert list(tmp_path.glob(".*.tmp")) == []
