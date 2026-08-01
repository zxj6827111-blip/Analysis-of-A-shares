"""Gate B5 focused tests: delisted-position terminal exit (synthetic engine)."""

from __future__ import annotations

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.delist_policy import (
    DELIST_EXIT_RULE_VERSION,
    EXIT_REASON_DELIST_TERMINAL,
    DelistExitPolicy,
    normalize_delist_policy,
)
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent

CODE = "SZSE.STK.300104"
# calendar continues past the stock's last bar (delisting on 20240105)
DATES = [20240102, 20240103, 20240104, 20240105, 20240108, 20240109, 20240110]
LAST_TRADE = 20240105


def _cfg(commission=0.0, stamp=0.0, slip=0.0):
    cfg = AStockConfig()
    cfg.initial_capital = 100_000
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(
        commission_rate=commission,
        min_commission=0.0,
        stamp_tax_rate=stamp,
        slippage=slip,
    )
    return cfg


def _delisting_bars():
    # bars stop after 20240105 (last tradable day); close ramps 10 -> 8
    return {
        CODE: [
            DayBar(20240102, 10, 11, 9, 10, 1, 1),
            DayBar(20240103, 10, 11, 9, 10, 1, 1),
            DayBar(20240104, 9, 10, 8, 9, 1, 1),
            DayBar(20240105, 8, 9, 7, 8, 1, 1),  # last trade, close=8
        ]
    }


def _run(policy, *, hold=100, cfg=None, account_mode="portfolio", terminal_dates=None):
    bt = PortfolioBacktester(
        cfg or _cfg(),
        TradeCalendar(DATES),
        _delisting_bars(),
        delist_policy=policy,
        delist_terminal_dates=(
            {CODE: LAST_TRADE} if terminal_dates is None else terminal_dates
        ),
    )
    return bt.run(
        [SignalEvent(CODE, 20240102, "DAY", "t")],  # buy 20240103 open=10
        hold=hold,
        entry_lag=1,
        period="DAY",
        formal_ok=True,
        account_mode=account_mode,
        _skip_zero_replay=True,
    )


class TestLegacyWithoutPolicy:
    def test_no_policy_no_terminal_exit(self):
        res = _run(None, terminal_dates={})
        reasons = {f.reason for f in res.fills if f.side == "SELL"}
        assert EXIT_REASON_DELIST_TERMINAL not in reasons
        # legacy behavior: position survives until EOD forced exit at sim end
        assert "delist_terminal_exit_count" not in res.metrics


class TestStandardScenario:
    def test_exit_first_day_after_last_trade(self):
        res = _run(DelistExitPolicy())
        d_fills = [f for f in res.fills if f.reason == EXIT_REASON_DELIST_TERMINAL]
        assert len(d_fills) == 1
        f = d_fills[0]
        assert f.date == 20240108  # first sim day after 20240105
        assert f.price == pytest.approx(8.0)  # last tradable close
        assert f.delist_exit_scenario == "last_tradable_price"
        assert f.delist_exit_rule_version == DELIST_EXIT_RULE_VERSION
        assert f.delist_terminal_date == LAST_TRADE
        assert f.delist_terminal_price == pytest.approx(8.0)
        assert f.delist_recovery_rate == pytest.approx(1.0)
        # bought 20240103 at open 10 -> 10000 shares? alloc=100k/10 => 10000
        # cost = 100_000; proceeds = 10000 * 8 = 80_000 -> loss -20_000
        assert f.delist_realized_loss == pytest.approx(-20_000.0)

    def test_no_infinite_position(self):
        res = _run(DelistExitPolicy())
        assert res.metrics["delisted_open_positions_at_end"] == 0
        assert res.metrics["n_open_positions"] == 0
        assert res.metrics["delist_terminal_exit_count"] == 1
        assert res.metrics["delist_realized_loss"] == pytest.approx(-20_000.0)

    def test_account_value_drops_not_frozen(self):
        res = _run(DelistExitPolicy())
        eq = {p.date: p for p in res.equity_curve}
        # after terminal exit market value is 0; equity = cash 80k
        assert eq[20240108].market_value == pytest.approx(0.0)
        assert eq[20240108].equity == pytest.approx(80_000.0)
        assert eq[20240110].equity == pytest.approx(80_000.0)

    def test_normal_sell_during_consolidation_period(self):
        # hold=1: normal time exit on 20240104 long before delisting
        res = _run(DelistExitPolicy(), hold=1)
        sells = [f for f in res.fills if f.side == "SELL"]
        assert len(sells) == 1
        assert sells[0].date == 20240104
        assert sells[0].reason != EXIT_REASON_DELIST_TERMINAL
        assert res.metrics["delist_terminal_exit_count"] == 0

    def test_sell_on_last_tradable_day_allowed(self):
        # signal 20240103 -> buy 20240104, hold=1 -> sell 20240105 (last day)
        bt = PortfolioBacktester(
            _cfg(),
            TradeCalendar(DATES),
            _delisting_bars(),
            delist_policy=DelistExitPolicy(),
            delist_terminal_dates={CODE: LAST_TRADE},
        )
        res = bt.run(
            [SignalEvent(CODE, 20240103, "DAY", "t")],
            hold=1,
            entry_lag=1,
            period="DAY",
            formal_ok=True,
            _skip_zero_replay=True,
        )
        sells = [f for f in res.fills if f.side == "SELL"]
        assert len(sells) == 1
        assert sells[0].date == LAST_TRADE
        assert sells[0].reason != EXIT_REASON_DELIST_TERMINAL


class TestConservativeScenarios:
    def test_discounted_recovery(self):
        res = _run(DelistExitPolicy(scenario="discounted_recovery", recovery_discount=0.5))
        f = [x for x in res.fills if x.reason == EXIT_REASON_DELIST_TERMINAL][0]
        assert f.price == pytest.approx(4.0)  # 8 * 0.5
        assert f.delist_recovery_rate == pytest.approx(0.5)
        assert f.delist_realized_loss == pytest.approx(-60_000.0)

    def test_discount_is_configurable_not_constant(self):
        res = _run(DelistExitPolicy(scenario="discounted_recovery", recovery_discount=0.25))
        f = [x for x in res.fills if x.reason == EXIT_REASON_DELIST_TERMINAL][0]
        assert f.price == pytest.approx(2.0)

    def test_zero_recovery(self):
        res = _run(DelistExitPolicy(scenario="zero_recovery"))
        f = [x for x in res.fills if x.reason == EXIT_REASON_DELIST_TERMINAL][0]
        assert f.price == pytest.approx(0.0)
        assert f.delist_realized_loss == pytest.approx(-100_000.0)
        eq = {p.date: p for p in res.equity_curve}
        assert eq[20240110].equity == pytest.approx(0.0)

    def test_results_sensitive_to_scenario(self):
        r1 = _run(DelistExitPolicy())
        r2 = _run(DelistExitPolicy(scenario="discounted_recovery"))
        r3 = _run(DelistExitPolicy(scenario="zero_recovery"))
        losses = [
            r["delist_realized_loss"]
            for r in (r1.metrics, r2.metrics, r3.metrics)
        ]
        assert losses[0] > losses[1] > losses[2]


class TestCostsAndAccounting:
    def test_book_out_has_no_costs_by_default(self):
        res = _run(
            DelistExitPolicy(),
            cfg=_cfg(commission=0.001, stamp=0.001, slip=0.001),
        )
        f = [x for x in res.fills if x.reason == EXIT_REASON_DELIST_TERMINAL][0]
        assert f.commission == 0.0
        assert f.stamp_tax == 0.0

    def test_apply_costs_true_charges_fees(self):
        res = _run(
            DelistExitPolicy(apply_costs=True),
            cfg=_cfg(commission=0.001, stamp=0.001),
        )
        f = [x for x in res.fills if x.reason == EXIT_REASON_DELIST_TERMINAL][0]
        assert f.commission > 0.0
        assert f.stamp_tax > 0.0

    def test_per_symbol_account_mode(self):
        res = _run(DelistExitPolicy(), account_mode="per_symbol")
        assert res.metrics["delist_terminal_exit_count"] == 1
        assert res.metrics["delisted_open_positions_at_end"] == 0

    def test_delisted_trade_count(self):
        res = _run(DelistExitPolicy())
        # BUY + terminal SELL on the delisted code
        assert res.metrics["delisted_trade_count"] == 2


class TestPolicyNormalization:
    def test_default_is_standard(self):
        p, notes = normalize_delist_policy(None)
        assert p.scenario == "last_tradable_price"
        assert p.rule_version == DELIST_EXIT_RULE_VERSION
        assert notes == []

    def test_unknown_scenario_rejected(self):
        with pytest.raises(ValueError, match="unknown delist_exit_scenario"):
            normalize_delist_policy("moon_recovery")

    def test_discount_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="within"):
            normalize_delist_policy("discounted_recovery", 1.5)
        with pytest.raises(ValueError, match="within"):
            normalize_delist_policy("discounted_recovery", -0.1)

    def test_ignored_discount_noted(self):
        p, notes = normalize_delist_policy("zero_recovery", 0.5)
        assert p.scenario == "zero_recovery"
        assert any("ignored" in n for n in notes)

    def test_recovery_rates(self):
        assert DelistExitPolicy().recovery_rate() == 1.0
        assert DelistExitPolicy(
            scenario="discounted_recovery", recovery_discount=0.3
        ).recovery_rate() == pytest.approx(0.3)
        assert DelistExitPolicy(scenario="zero_recovery").recovery_rate() == 0.0

    def test_meta_fields(self):
        meta = DelistExitPolicy().to_meta()
        assert meta["delist_exit_rule_version"] == DELIST_EXIT_RULE_VERSION
        assert meta["delist_exit_scenario"] == "last_tradable_price"


class TestCacheIsolation:
    def test_execution_cache_key_differs_by_scenario(self):
        from wtpy.apps.astock.research.execution_cache import execution_cache_key

        base = {"engine": "full", "rule_ids": ["t"], "start": 20240101, "end": 20241231}
        k1 = execution_cache_key({**base, "delist_exit_scenario": "last_tradable_price"})
        k2 = execution_cache_key({**base, "delist_exit_scenario": "zero_recovery"})
        k3 = execution_cache_key(base)
        assert len({k1, k2, k3}) == 3
