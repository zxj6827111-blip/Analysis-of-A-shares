"""Version module + /api/v1/version endpoint tests."""
import re

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.version import (
    APP_VERSION,
    get_build_info,
    get_version_info,
    get_version_string,
    refresh_build_info,
)


class TestVersionModule:
    def test_version_semver(self):
        # 两段(2.9)或三段(2.9.1) semver 均合法；v2.9.1 起版本号为三段
        assert re.fullmatch(r"\d+\.\d+(\.\d+)?", APP_VERSION)

    def test_build_info_keys(self):
        info = get_build_info()
        for k in ("commit", "commit_count", "branch", "last_commit_at", "dirty"):
            assert k in info
        assert isinstance(info["dirty"], bool)

    def test_version_string_format(self):
        s = get_version_string()
        assert s.startswith(f"v{APP_VERSION}")
        # build suffix optional off-git, but never empty/None
        assert s.endswith("*") is (get_build_info()["dirty"] is True)

    def test_version_info_payload(self):
        info = get_version_info()
        assert info["app"] == "astock"
        assert info["version"] == APP_VERSION
        assert info["version_string"] == get_version_string()
        assert "build" in info

    def test_refresh_build_info(self):
        info = refresh_build_info()
        assert info == get_build_info()


class TestVersionApi:
    def test_version_endpoint(self, tmp_path):
        from fastapi.testclient import TestClient
        from wtpy.apps.astock.api import create_app
        from wtpy.apps.astock.config import AStockConfig

        cfg = AStockConfig()
        cfg.storage_root = tmp_path / "st"
        cfg.output_root = tmp_path / "out"
        cfg.ensure_dirs()
        client = TestClient(create_app(cfg))
        r = client.get("/api/v1/version")
        assert r.status_code == 200
        j = r.json()
        assert j["app"] == "astock"
        assert j["version"] == APP_VERSION
        assert j["version_string"].startswith(f"v{APP_VERSION}")
        assert "python_version" in j
        assert "build" in j
        assert "dirty" in j["build"]
