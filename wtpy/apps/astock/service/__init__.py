"""Service layer for A-stock rules, backtests, and jobs (web + CLI)."""

from .rules import RuleService, rule_to_public
from .backtest import BacktestRequest, BacktestService, run_backtest
from .jobs import JobStore
from .runs import list_runs, load_run_summary, read_artifact

__all__ = [
    "RuleService",
    "rule_to_public",
    "BacktestRequest",
    "BacktestService",
    "run_backtest",
    "JobStore",
    "list_runs",
    "load_run_summary",
    "read_artifact",
]
