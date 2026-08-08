"""Formal CLI: --dry-run/--preflight are real (no writes) and local_vendor validates args."""
import csv
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[3]
SCRIPT = PROJ / "scripts" / "sync_market_data.py"

CSV_HEADER = (
    "code,datetime,open,high,low,close,pre_close,change,pct_chg,volume,amount,"
    "turnover,turnover_free,volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,dv_yield,dv_ttm,"
    "total_share,float_share,free_share,total_mv,circ_mv"
)


@pytest.fixture
def incoming(tmp_path):
    daily = tmp_path / "incoming" / "daily"
    daily.mkdir(parents=True)
    for year in range(2015, 2025):  # 10 year zips -> provider daily-dir detection
        with zipfile.ZipFile(daily / f"{year}.zip", "w") as zf:
            row = (f"600000.SH,{year}-06-03,10,11,9,10.5,10,0.5,5,100,105,"
                   "1,1,1,10,10,1,1,1,1,1,100000,90000,80000,1000000,900000")
            zf.writestr(f"{year}/600000.SH.csv", CSV_HEADER + "\n" + row + "\n")
    return tmp_path / "incoming"


def _run(args, incoming_root, storage_root):
    env = {
        **__import__("os").environ,
        "LOCAL_VENDOR_RAW_ROOT": str(incoming_root),
        "ASTOCK_ENV": "test",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--storage-root", str(storage_root)],
        capture_output=True, text=True, timeout=180, env=env, cwd=str(PROJ),
    )


class TestDryRunPreflight:
    def test_dry_run_writes_nothing(self, incoming, tmp_path):
        root = tmp_path / "md_root"
        p = _run(["--source", "local_vendor", "--mode", "full",
                  "--symbol", "SSE.STK.600000", "--dry-run"], incoming, root)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "DRY RUN" in p.stdout
        assert "No data will be written" in p.stdout
        assert not (root / "blobs").exists()
        assert not (root / "manifests").exists()

    def test_preflight_writes_nothing_and_reports(self, incoming, tmp_path):
        root = tmp_path / "md_root"
        root.mkdir(parents=True)  # preflight fails (exit 1) on a missing root
        p = _run(["--source", "local_vendor", "--mode", "full", "--preflight"],
                 incoming, root)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "PREFLIGHT CHECK" in p.stdout
        assert "Incoming root" in p.stdout
        assert "Free disk space" in p.stdout
        assert "Lock file" in p.stdout
        assert "Checkpoint" in p.stdout
        assert not (root / "blobs").exists()

    def test_dry_run_universe_file(self, incoming, tmp_path):
        root = tmp_path / "md_root"
        uf = tmp_path / "uni.csv"
        with open(uf, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["canonical_symbol", "inclusion_status"])
            w.writerow(["SSE.STK.600000", "included"])
            w.writerow(["SSE.STK.999999", "excluded"])
        p = _run(["--source", "local_vendor", "--mode", "full",
                  "--universe-file", str(uf), "--dry-run"], incoming, root)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "1 included from universe file" in p.stdout

    def test_local_vendor_rejects_non_none_adjustment(self, incoming, tmp_path):
        root = tmp_path / "md_root"
        p = _run(["--source", "local_vendor", "--mode", "full",
                  "--symbol", "SSE.STK.600000", "--adjustment", "front"],
                 incoming, root)
        assert "only supports adjustment=none period=1d" in p.stdout
        assert not (root / "manifests").exists() or not list(
            (root / "manifests").glob("*.json"))

    def test_local_vendor_requires_incoming_configured(self, tmp_path):
        root = tmp_path / "md_root"
        env = {**__import__("os").environ, "ASTOCK_ENV": "test"}
        env.pop("LOCAL_VENDOR_RAW_ROOT", None)
        env["LOCAL_VENDOR_RAW_ROOT"] = ""  # explicit empty -> not configured
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "--source", "local_vendor",
             "--mode", "full", "--symbol", "SSE.STK.600000",
             "--storage-root", str(root)],
            capture_output=True, text=True, timeout=120, env=env, cwd=str(PROJ),
        )
        assert "incoming root not set" in p.stdout.lower() or \
               "incoming_root_not_configured" in p.stdout


class TestServeGuard:
    def test_production_internal_root_refuses_start(self, tmp_path, monkeypatch):
        from wtpy.apps.astock.config import AStockConfig
        cfg = AStockConfig(
            storage_root=str(tmp_path / "storage"),
            output_root=str(tmp_path / "out"),
            tdx_root=str(tmp_path / "tdx"),
        )
        monkeypatch.setenv("ASTOCK_ENV", "production")
        monkeypatch.setenv(
            "MARKET_DATA_ROOT", str(Path(cfg.storage_root) / "market_data"))
        monkeypatch.delenv("ASTOCK_ALLOW_INTERNAL_DATA_ROOT", raising=False)
        from wtpy.apps.astock.api import serve
        with pytest.raises(SystemExit) as ei:
            serve(cfg=cfg)  # must refuse BEFORE binding any port
        assert ei.value.code == 2
