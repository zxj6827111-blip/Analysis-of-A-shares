"""Tests for BacktestRequest multi-source fields."""
import os

import pytest

from wtpy.apps.astock.service.backtest_request import BacktestRequest


def _real_data_env_ok() -> bool:
    """True when the machine has a real data environment (external data root
    from env or local TDX day files). CI runners lack both; these regression
    tests exercise the full backtest path and would legitimately no-go there.
    """
    md = os.environ.get("MARKET_DATA_ROOT", "").strip()
    if md and os.path.isdir(md):
        return True
    try:
        from wtpy.apps.astock.config import get_default_config

        cfg = get_default_config()
        return (cfg.tdx_root / "vipdoc" / "sh" / "lday" / "sh600000.day").exists()
    except Exception:
        return False


class TestBacktestSourceSelection:
    def test_default_fields(self):
        req = BacktestRequest(rule_ids=["rule_a"])
        assert req.signal_data_source is None
        assert req.signal_adjustment is None
        assert req.dataset_id is None
        assert req.weekly_bar_mode == "local_aggregate"
        # Tushare-only policy: product default execution plane is formal L2
        assert req.execution_data_source == "internal"
        assert req.execution_dataset_id is None

    def test_tdxquant_source(self):
        req = BacktestRequest(
            rule_ids=["rule_a"],
            signal_data_source="tdxquant",
            signal_adjustment="front",
            dataset_id="tdxquant_front_1d_20260724_a3f2b1c4d5e6",
        )
        assert req.signal_data_source == "tdxquant"
        assert req.signal_adjustment == "front"
        assert req.dataset_id == "tdxquant_front_1d_20260724_a3f2b1c4d5e6"

    def test_tushare_source(self):
        req = BacktestRequest(
            rule_ids=["rule_a"],
            signal_data_source="tushare",
            signal_adjustment="qfq",
            dataset_id="tushare_qfq_1d_anchor20260724_7f8e9d0c1b2a",
        )
        assert req.signal_data_source == "tushare"
        assert req.signal_adjustment == "qfq"

    def test_internal_source(self):
        req = BacktestRequest(
            rule_ids=["rule_a"],
            signal_data_source="internal",
            signal_adjustment="asof_qfq",
        )
        assert req.signal_data_source == "internal"
        assert req.signal_adjustment == "asof_qfq"

    def test_execution_default_is_formal_l2(self):
        req = BacktestRequest(rule_ids=["rule_a"])
        # product default = formal L2 (internal/composite_none); legacy
        # families remain available via explicit selection
        assert req.execution_data_source == "internal"
        req_legacy = BacktestRequest(rule_ids=["rule_a"], execution_data_source="local_vendor")
        assert req_legacy.execution_data_source == "local_vendor"

    def test_to_dict_includes_new_fields(self):
        req = BacktestRequest(
            rule_ids=["rule_a"],
            signal_data_source="tdxquant",
            dataset_id="ds_123",
            weekly_bar_mode="vendor_native",
        )
        d = req.to_dict()
        assert d["signal_data_source"] == "tdxquant"
        assert d["dataset_id"] == "ds_123"
        assert d["weekly_bar_mode"] == "vendor_native"
        assert d["execution_data_source"] == "internal"

    def test_vendor_native_weekly_mode(self):
        req = BacktestRequest(
            rule_ids=["rule_a"],
            weekly_bar_mode="vendor_native",
        )
        assert req.weekly_bar_mode == "vendor_native"


@pytest.mark.skipif(
    not _real_data_env_ok(),
    reason="real data environment (external data root / TDX day files) required",
)
class TestLegacyTdxFrontBaguaPlaneRerun:
    """Regression: rerunning a legacy experiment with plane=tdx_front must
    degrade per code (bagua plane disabled by the Tushare-only policy), not
    abort the whole rerun with an uncaught SourceDisabledError (API 500).
    """

    def _run(self, tmp_path):
        import tests.apps.astock.conftest  # noqa: F401

        from wtpy.apps.astock.config import get_default_config
        from wtpy.apps.astock.service.backtest import run_backtest
        from wtpy.apps.astock.service.rules import RuleService

        storage = tmp_path / "st"
        ind = tmp_path / "ind"
        storage.mkdir()
        ind.mkdir()
        cfg = get_default_config(storage_root=storage, indicator_dir=ind)

        d = storage / "csv" / "day" / "SSE"
        d.mkdir(parents=True)
        lines = ["date,open,high,low,close,amount,volume"]
        for i in range(1, 20):
            dt = 20260700 + i
            o = 10 + i * 0.1
            lines.append(f"{dt},{o:.2f},{o + 0.3:.2f},{o - 0.2:.2f},{o + 0.2:.2f},1000000,100000")
        (d / "600000.csv").write_text("\n".join(lines), encoding="utf-8")

        rs = RuleService(cfg)
        created = rs.create_rule(name="t_plane", formula_text="XG:C>OPEN;")
        assert created.get("id", "").startswith("user_")

        req = BacktestRequest(
            rule_ids=[created["id"]],
            codes=["SSE.STK.600000"],
            start=20260701,
            end=20260730,
            with_bagua=True,
            bagua_price_plane="tdx_front",
            use_signal_cache=False,
            artifact_level="summary",
        )
        return run_backtest(cfg, req)

    def test_rerun_tdx_front_degrades_instead_of_raising(self, tmp_path):
        res = self._run(tmp_path)
        assert res["status"] == "ok"
        assert res["bagua_price_plane_effective"] == "tdx_front"
        # Per-code fallback recorded, whole rerun not aborted.
        assert res["bagua_plane_missing_count"] == 1
        sample = res.get("errors_sample") or []
        assert any(
            e.get("code") == "SSE.STK.600000"
            and "bagua_plane_load_failed(tdx_front)" in (e.get("error") or "")
            for e in sample
        )

    def test_rerun_does_not_silently_use_raw_plane(self, tmp_path):
        res = self._run(tmp_path)
        repro = res.get("repro") or {}
        # The request keeps the requested plane; the run reports the disabled
        # plane fidelity loss instead of claiming a successful tdx_front attach.
        assert repro.get("bagua_price_plane") == "tdx_front"
        assert repro.get("bagua_plane_missing_count") == 1
