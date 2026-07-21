# -*- coding: utf-8 -*-
"""Experiment center MVP (Stage E): expand param grid, queue variants, aggregate results."""
from __future__ import annotations

import itertools
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..bagua.filter_rules import BEST3, GuaFilter
from ..config import AStockConfig, get_default_config
from .backtest import BacktestRequest, BacktestService
from . import db as exp_db

# Soft cap — UI must warn; hard refuse above this unless force=True
DEFAULT_MAX_VARIANTS = 50
HARD_MAX_VARIANTS = 200

# Weekday schedule templates (UI labels → engine fields)
WEEKDAY_TEMPLATES = {
    "fri_signal_mon_buy_thu_exit": {
        "label": "仅周五信号·周一买·周四平",
        "signal_weekdays": [5],
        "buy_weekday": 1,
        "exit_weekday": 4,
        "buy_on": "open",
        "sell_on": "open",
        "entry_lag": 1,
        "hold": 1,
    },
    "all_signal_tn12": {
        "label": "不限信号·经典T+1开/T+2收",
        "signal_weekdays": None,
        "buy_weekday": None,
        "exit_weekday": None,
        "buy_on": "open",
        "sell_on": "close",
        "entry_lag": 1,
        "hold": 1,
    },
    "fri_signal_fri_buy_mon_exit": {
        "label": "仅周五信号·周五买·下周一平",
        "signal_weekdays": [5],
        "buy_weekday": 5,
        "exit_weekday": 1,
        "buy_on": "open",
        "sell_on": "open",
        "entry_lag": 1,
        "hold": 1,
    },
}

GUA_PRESETS = {
    "none": {
        "label": "无卦象",
        "gua_filter": {
            "enabled": False,
            "selection_mode": "none",
            "selected_main_hexagram_ids": [],
            "selected_state_ids": [],
            "selected_action_signals": [],
        },
        "with_bagua": False,
    },
    "best3": {
        "label": "最佳3爻",
        "gua_filter": {
            "enabled": True,
            "selection_mode": "exact_line",
            "selected_main_hexagram_ids": [],
            "selected_state_ids": ["24-1", "46-1", "11-1"],
            "selected_action_signals": [],
        },
        "with_bagua": True,
    },
    "bull": {
        "label": "偏多操作信号",
        "gua_filter": {
            "enabled": True,
            "selection_mode": "action_signal",
            "selected_main_hexagram_ids": [],
            "selected_state_ids": [],
            "selected_action_signals": ["新开仓", "加仓"],
        },
        "with_bagua": True,
    },
}


def estimate_grid_size(
    rule_ids: Sequence[str],
    gua_keys: Sequence[str],
    weekday_keys: Sequence[str],
    stop_loss_list: Optional[Sequence[Optional[float]]] = None,
) -> int:
    n_rules = max(1, len(list(rule_ids or [])))
    n_gua = max(1, len(list(gua_keys or ["none"])))
    n_wd = max(1, len(list(weekday_keys or ["all_signal_tn12"])))
    n_sl = max(1, len(list(stop_loss_list if stop_loss_list is not None else [None])))
    return n_rules * n_gua * n_wd * n_sl


def expand_param_grid(
    *,
    rule_ids: Sequence[str],
    gua_keys: Sequence[str],
    weekday_keys: Sequence[str],
    stop_loss_list: Optional[Sequence[Optional[float]]] = None,
    period: str = "DAY",
    codes: Optional[Sequence[str]] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    account_mode: str = "portfolio",
    research_unadjusted: bool = False,
) -> List[Dict[str, Any]]:
    """Cartesian product of research axes → list of BacktestRequest-like dicts."""
    rules = list(rule_ids or [])
    if not rules:
        raise ValueError("rule_ids required")
    guas = list(gua_keys or ["none"])
    wds = list(weekday_keys or ["all_signal_tn12"])
    sls = list(stop_loss_list if stop_loss_list is not None else [None])

    for g in guas:
        if g not in GUA_PRESETS:
            raise ValueError(f"unknown gua preset: {g}")
    for w in wds:
        if w not in WEEKDAY_TEMPLATES:
            raise ValueError(f"unknown weekday template: {w}")

    out: List[Dict[str, Any]] = []
    for rule_id, gkey, wkey, sl in itertools.product(rules, guas, wds, sls):
        g = GUA_PRESETS[gkey]
        w = WEEKDAY_TEMPLATES[wkey]
        params: Dict[str, Any] = {
            "rule_ids": [rule_id],
            "period": period,
            "account_mode": account_mode,
            "codes": list(codes) if codes else ["sh600000", "sz000001"],
            "start": start,
            "end": end,
            "research_unadjusted": bool(research_unadjusted),
            "hold": w.get("hold", 1),
            "entry_lag": w.get("entry_lag", 1),
            "buy_weekday": w.get("buy_weekday"),
            "exit_weekday": w.get("exit_weekday"),
            "signal_weekdays": w.get("signal_weekdays"),
            "buy_on": w.get("buy_on", "open"),
            "sell_on": w.get("sell_on", "open"),
            "with_bagua": g.get("with_bagua", False),
            "gua_filter": dict(g.get("gua_filter") or {}),
            "stop_loss": sl,
            "take_profit": None,
            # labels for result table
            "_meta": {
                "rule_id": rule_id,
                "gua_key": gkey,
                "gua_label": g.get("label"),
                "weekday_key": wkey,
                "weekday_label": w.get("label"),
                "stop_loss": sl,
            },
        }
        out.append(params)
    return out


def create_experiment_from_grid(
    cfg: Optional[AStockConfig],
    *,
    name: str,
    rule_ids: Sequence[str],
    gua_keys: Sequence[str],
    weekday_keys: Sequence[str],
    stop_loss_list: Optional[Sequence[Optional[float]]] = None,
    period: str = "DAY",
    codes: Optional[Sequence[str]] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    account_mode: str = "portfolio",
    research_unadjusted: bool = False,
    max_variants: int = DEFAULT_MAX_VARIANTS,
    concurrency: int = 1,
    force: bool = False,
    note: str = "",
) -> Dict[str, Any]:
    cfg = cfg or get_default_config()
    n = estimate_grid_size(rule_ids, gua_keys, weekday_keys, stop_loss_list)
    warning = None
    if n > max_variants and not force:
        raise ValueError(
            f"组合数 {n} 超过上限 max_variants={max_variants}；"
            f"请缩小空间或提高 max_variants，或 force=true（硬顶 {HARD_MAX_VARIANTS}）"
        )
    if n > HARD_MAX_VARIANTS:
        raise ValueError(f"组合数 {n} 超过硬顶 {HARD_MAX_VARIANTS}")
    if n > 20:
        warning = f"组合数 {n} 较大，建议先用演示池试跑"

    variants = expand_param_grid(
        rule_ids=rule_ids,
        gua_keys=gua_keys,
        weekday_keys=weekday_keys,
        stop_loss_list=stop_loss_list,
        period=period,
        codes=codes,
        start=start,
        end=end,
        account_mode=account_mode,
        research_unadjusted=research_unadjusted,
    )
    config = {
        "rule_ids": list(rule_ids),
        "gua_keys": list(gua_keys),
        "weekday_keys": list(weekday_keys),
        "stop_loss_list": list(stop_loss_list) if stop_loss_list is not None else [None],
        "period": period,
        "codes": list(codes) if codes else ["sh600000", "sz000001"],
        "start": start,
        "end": end,
        "account_mode": account_mode,
        "research_unadjusted": research_unadjusted,
        "estimated": n,
        "warning": warning,
    }
    exp = exp_db.create_experiment(
        cfg,
        name=name or f"实验·{n}组合",
        config=config,
        variants=variants,
        max_variants=max_variants,
        concurrency=concurrency,
        note=note or "",
    )
    if warning:
        exp["warning"] = warning
    return exp


class ExperimentRunner:
    """In-process limited-concurrency runner for experiment variants."""

    def __init__(self, cfg: Optional[AStockConfig] = None):
        self.cfg = cfg or get_default_config()
        self._lock = threading.Lock()
        self._cancel: Dict[str, bool] = {}
        self._threads: Dict[str, threading.Thread] = {}

    def cancel(self, experiment_id: str) -> None:
        with self._lock:
            self._cancel[experiment_id] = True

    def is_cancelled(self, experiment_id: str) -> bool:
        with self._lock:
            return bool(self._cancel.get(experiment_id))

    def start(self, experiment_id: str) -> Dict[str, Any]:
        exp = exp_db.get_experiment(self.cfg, experiment_id)
        if exp.get("status") in ("running",):
            return exp
        with self._lock:
            self._cancel[experiment_id] = False
            if experiment_id in self._threads and self._threads[experiment_id].is_alive():
                return exp
            t = threading.Thread(
                target=self._run_experiment,
                args=(experiment_id,),
                daemon=True,
                name=f"exp-{experiment_id}",
            )
            self._threads[experiment_id] = t
            t.start()
        exp_db.update_experiment_status(self.cfg, experiment_id, "running")
        return exp_db.get_experiment(self.cfg, experiment_id)

    def _run_one(self, experiment_id: str, variant: dict) -> Tuple[str, str, Optional[str], Optional[str]]:
        """Returns (variant_id, status, run_id, error)."""
        vid = variant["variant_id"]
        if self.is_cancelled(experiment_id):
            return vid, "cancelled", None, "cancelled"
        params = dict(variant.get("params") or {})
        meta = params.pop("_meta", None)
        ph = variant.get("param_hash") or exp_db.param_hash(params)
        # de-dup
        existing = exp_db.find_run_id_by_param_hash(self.cfg, ph)
        if existing:
            exp_db.update_variant(
                self.cfg, vid, status="skipped", run_id=existing, error="param_hash dedup"
            )
            return vid, "skipped", existing, None

        exp_db.update_variant(self.cfg, vid, status="running")
        try:
            req = BacktestRequest(
                rule_ids=list(params.get("rule_ids") or []),
                period=params.get("period") or "DAY",
                hold=int(params.get("hold") or 1),
                entry_lag=int(params.get("entry_lag") or 1),
                signal_weekdays=params.get("signal_weekdays"),
                buy_on=params.get("buy_on") or "open",
                sell_on=params.get("sell_on") or "open",
                buy_weekday=params.get("buy_weekday"),
                exit_weekday=params.get("exit_weekday"),
                codes=params.get("codes"),
                start=params.get("start"),
                end=params.get("end"),
                with_bagua=bool(params.get("with_bagua")),
                gua_filter=params.get("gua_filter"),
                research_unadjusted=bool(params.get("research_unadjusted")),
                stop_loss=params.get("stop_loss"),
                take_profit=params.get("take_profit"),
                account_mode=params.get("account_mode") or "portfolio",
            )
            svc = BacktestService(self.cfg)
            summary = svc.run(req)
            rid = summary.get("run_id")
            # link experiment on run row
            if rid:
                try:
                    exp_db.upsert_run_from_index_row(
                        self.cfg,
                        {
                            "run_id": rid,
                            "title": summary.get("title"),
                            "status": summary.get("status"),
                            "created_at": int(time.time()),
                            "indicator_ids": (summary.get("repro") or {}).get("indicator_ids")
                            or params.get("rule_ids"),
                            "period": params.get("period"),
                            "hold": params.get("hold"),
                            "entry_lag": params.get("entry_lag"),
                            "buy_weekday": params.get("buy_weekday"),
                            "exit_weekday": params.get("exit_weekday"),
                            "buy_on": params.get("buy_on"),
                            "sell_on": params.get("sell_on"),
                            "signal_weekdays": params.get("signal_weekdays"),
                            "account_mode": params.get("account_mode"),
                            "start": params.get("start"),
                            "end": params.get("end"),
                            "with_bagua": params.get("with_bagua"),
                            "gua_filter": summary.get("gua_filter") or params.get("gua_filter"),
                            "metrics": summary.get("metrics"),
                            "param_hash": ph,
                            "experiment_id": experiment_id,
                            "variant_id": vid,
                        },
                    )
                except Exception:
                    pass
            st = "succeeded"
            if (summary.get("status") or "").startswith("no_go") or summary.get("status") == "failed":
                st = "failed"
            exp_db.update_variant(self.cfg, vid, status=st, run_id=rid, error=summary.get("error"))
            return vid, st, rid, summary.get("error")
        except Exception as e:
            err = f"{e}\n{traceback.format_exc()[:500]}"
            exp_db.update_variant(self.cfg, vid, status="failed", error=str(e)[:500])
            return vid, "failed", None, str(e)

    def _run_experiment(self, experiment_id: str) -> None:
        try:
            exp = exp_db.get_experiment(self.cfg, experiment_id)
            concurrency = max(1, int(exp.get("concurrency") or 1))
            pending = [
                v
                for v in (exp.get("variants") or [])
                if v.get("status") in ("pending", "failed")
            ]
            # only re-run pending; failed retry if restarted
            pending = [
                v
                for v in (exp.get("variants") or [])
                if v.get("status") == "pending"
            ]
            completed = int(exp.get("completed_variants") or 0)
            failed = int(exp.get("failed_variants") or 0)
            skipped = int(exp.get("skipped_variants") or 0)

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futs = {
                    pool.submit(self._run_one, experiment_id, v): v for v in pending
                }
                for fut in as_completed(futs):
                    if self.is_cancelled(experiment_id):
                        break
                    try:
                        _vid, st, _rid, _err = fut.result()
                    except Exception:
                        st = "failed"
                    if st == "succeeded":
                        completed += 1
                    elif st == "skipped":
                        skipped += 1
                    elif st == "cancelled":
                        pass
                    else:
                        failed += 1
                    exp_db.update_experiment_status(
                        self.cfg,
                        experiment_id,
                        "running",
                        completed_variants=completed,
                        failed_variants=failed,
                        skipped_variants=skipped,
                    )

            final = "cancelled" if self.is_cancelled(experiment_id) else "completed"
            exp_db.update_experiment_status(
                self.cfg,
                experiment_id,
                final,
                completed_variants=completed,
                failed_variants=failed,
                skipped_variants=skipped,
            )
        except Exception as e:
            exp_db.update_experiment_status(
                self.cfg, experiment_id, "failed"
            )
            # best-effort
            _ = e


_RUNNER: Optional[ExperimentRunner] = None
_RUNNER_LOCK = threading.Lock()


def get_runner(cfg: Optional[AStockConfig] = None) -> ExperimentRunner:
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is None:
            _RUNNER = ExperimentRunner(cfg)
        return _RUNNER


def write_experiment_excel(cfg: AStockConfig, experiment_id: str, path=None):
    """Write a simple results workbook for the experiment."""
    from pathlib import Path

    try:
        from openpyxl import Workbook
    except ImportError as e:
        raise RuntimeError("openpyxl required for experiment excel") from e

    table = exp_db.experiment_results_table(cfg, experiment_id)
    out = Path(path) if path else Path(cfg.output_root) / experiment_id / "experiment_summary.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "实验结果"
    headers = [
        "variant_id",
        "status",
        "run_id",
        "rule",
        "gua",
        "weekday",
        "stop_loss",
        "total_return",
        "annual_return",
        "max_drawdown",
        "win_rate",
        "n_round_trips",
        "payoff_ratio",
        "param_hash",
        "error",
    ]
    ws.append(headers)
    for row in table.get("rows") or []:
        p = row.get("params") or {}
        meta = p.get("_meta") or {}
        m = row.get("metrics") or {}
        ws.append(
            [
                row.get("variant_id"),
                row.get("status"),
                row.get("run_id"),
                meta.get("rule_id") or (p.get("rule_ids") or [None])[0],
                meta.get("gua_label") or meta.get("gua_key"),
                meta.get("weekday_label") or meta.get("weekday_key"),
                meta.get("stop_loss"),
                m.get("total_return"),
                m.get("annual_return"),
                m.get("max_drawdown"),
                m.get("win_rate"),
                m.get("n_round_trips"),
                m.get("payoff_ratio") or m.get("profit_loss_ratio"),
                row.get("param_hash"),
                row.get("error"),
            ]
        )
    wb.save(out)
    return out
