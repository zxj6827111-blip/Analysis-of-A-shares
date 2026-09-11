# -*- coding: utf-8 -*-
"""Standardized rule benchmark (v1) for the rule center drawer.

Backs three frontend endpoints:
- ``GET  /api/v1/rules/benchmark-profile``
- ``GET  /api/v1/rules/{rule_id}/performance``
- ``POST /api/v1/rules/{rule_id}/benchmark``

The profile is a fixed v1 baseline (全市场 DAY / 组合资金 / T+1 open buy /
close sell), optionally overridden by a shallow sidecar JSON at
``<storage_root>/benchmark_profile.json``.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import AStockConfig
from ..strategy_schedule import parse_price_session
from .backtest import BacktestRequest
from .db import (
    create_rule_benchmark,
    get_latest_rule_benchmark,
    update_rule_benchmark,
)
from .runs import load_equity_curve, load_run_summary

logger = logging.getLogger("astock.rule_benchmark")

DEFAULT_MIN_ROUND_TRIPS = 30
MAX_QUEUED_BENCHMARKS = 50

# 串行化「查活跃记录 → 提交 job → 落库」，避免并发首次提交产生重复 job/记录。
_BENCHMARK_SUBMIT_LOCK = threading.RLock()

# resolve_data_max_date 的降级分支要遍历全部数据集清单；GET profile / POST
# benchmark 都会调用它。按 market_data_root 做 60 秒 TTL，避免每次全量扫描。
_DATA_MAX_CACHE_TTL = 60.0
_DATA_MAX_CACHE: Dict[str, tuple] = {}


class BenchmarkQueueFullError(RuntimeError):
    """基准任务队列已满（API 映射为 429）。"""

PROXY_MESSAGE = (
    "该规则依赖 60 分钟数据，当前以日线研究代理运行，结果仅供研究"
)
PROXY_DETAIL_FALLBACK = (
    "该规则依赖 60 分钟数据，当前以日线研究代理运行，仅供研究。"
)

DEFAULT_BENCHMARK_PROFILE: Dict[str, Any] = {
    "profile_id": "rule_benchmark_v1",
    "version": 1,
    "universe_label": "全市场 A股（最新就绪数据集）",
    "codes": ["ALL"],
    "period": "DAY",
    "start": 20180101,
    "end": None,
    "account_mode": "portfolio",
    "entry_lag": 1,
    "hold": 1,
    "buy_on": "open",
    "sell_on": "close",
    "signal_weekdays": None,
    "buy_weekday": None,
    "exit_weekday": None,
    "with_bagua": False,
    "stop_loss": None,
    "take_profit": None,
    "engine": "full",
    "artifact_level": "full",
    "data_quality": "全市场 · 最新就绪数据（非点时宇宙）",
    "fee_note": "",
    "gua_filter": {"enabled": False},
    # v1 不实现基准指数曲线（performance 恒返回 benchmark_equity: []），
    # 该字段为后续扩展保留。
    "benchmark_index": None,
    "sample_gates": {"min_round_trips": DEFAULT_MIN_ROUND_TRIPS},
}

_METRIC_KEYS = (
    "total_return",
    "annual_return",
    "max_drawdown",
    "win_rate",
    "sharpe",
    "n_round_trips",
)


def _fee_note_from_costs(costs: Any) -> str:
    commission = float(getattr(costs, "commission_rate", 0.0) or 0.0)
    min_commission = float(getattr(costs, "min_commission", 0.0) or 0.0)
    stamp = float(getattr(costs, "stamp_tax_rate", 0.0) or 0.0)
    slippage = float(getattr(costs, "slippage", 0.0) or 0.0)
    parts = [
        "佣金 %.4f%%" % (commission * 100.0),
        "最低 %.2f 元" % min_commission,
        "印花税 %.2f%%（卖出）" % (stamp * 100.0),
        "滑点 %.4f%%" % (slippage * 100.0),
    ]
    return "成本按当前配置：" + " · ".join(parts)


def _resolve_data_max_date_uncached(cfg: AStockConfig) -> Optional[int]:
    """Uncached body of :func:`resolve_data_max_date`."""
    try:
        from ..data.dataset_store import DatasetStore
        from ..data.tushare_product import resolve_active_tushare_product_pair

        if Path(cfg.market_data_root).exists():
            store = DatasetStore(cfg.market_data_root)
            pair = resolve_active_tushare_product_pair(store, deep_copy=False)
            if pair is not None:
                pair_max = max(
                    int(pair.l1_max_date or 0),
                    int(pair.l2_max_date or 0),
                )
                if pair_max > 0:
                    return pair_max
    except Exception:
        pass

    best_cut = 0
    try:
        from ..data.dataset_store import DatasetStore
        from ..data.repository import MarketDataRepository

        if Path(cfg.market_data_root).exists():
            store = DatasetStore(cfg.market_data_root)
            repo = MarketDataRepository(store)
            try:
                datasets = repo.list_datasets(deep_copy=False)
            except TypeError:
                # 兼容只接受无参 list_datasets() 的旧实现/测试替身
                datasets = repo.list_datasets()
            for d in datasets or []:
                period = str(getattr(d, "period", "1d") or "1d").strip().lower()
                if period not in ("1d", "day", "d"):
                    continue
                if (getattr(d, "status", "") or "") not in ("ready", "partial"):
                    continue
                cut = int(getattr(d, "data_cutoff_date", 0) or 0)
                if not cut:
                    lasts = [
                        s.last_date
                        for s in (getattr(d, "symbols", None) or [])
                        if getattr(s, "last_date", None)
                    ]
                    cut = max(lasts) if lasts else 0
                if cut > best_cut:
                    best_cut = cut
    except Exception:
        pass
    return best_cut if best_cut > 0 else None


def resolve_data_max_date(cfg: AStockConfig) -> Optional[int]:
    """Freshest available trading date, or None when nothing resolves.

    Prefers the active formal Tushare L1/L2 product pair; otherwise scans
    ready/partial day-line dataset cutoffs / symbol last dates (same idea as
    ``calendar_range`` best_cut). All failures degrade silently to None.
    Cached per ``market_data_root`` for 60 seconds to keep refresh cheap.
    """
    key = str(cfg.market_data_root)
    now = time.monotonic()
    cached = _DATA_MAX_CACHE.get(key)
    if cached is not None and now - float(cached[0]) < _DATA_MAX_CACHE_TTL:
        return cached[1]
    value = _resolve_data_max_date_uncached(cfg)
    _DATA_MAX_CACHE[key] = (now, value)
    return value


def _load_profile_sidecar(cfg: AStockConfig) -> Dict[str, Any]:
    path = Path(cfg.storage_root) / "benchmark_profile.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - sidecar must never break the API
        logger.warning(
            "benchmark_profile.json 读取失败（%s），使用默认 profile: %s", path, e
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "benchmark_profile.json 不是 JSON 对象（%s），使用默认 profile", path
        )
        return {}
    return data


# sidecar 允许覆盖的运行字段白名单；展示字段（data_quality/universe_label/
# fee_note/benchmark_index 等）不接受覆盖，避免展示与真实运行不一致。
_PROFILE_OVERRIDE_KEYS = frozenset(
    {
        "start",
        "end",
        "entry_lag",
        "hold",
        "buy_on",
        "sell_on",
        "account_mode",
        "engine",
        "artifact_level",
        "signal_weekdays",
        "buy_weekday",
        "exit_weekday",
        "with_bagua",
        "codes",
        "stop_loss",
        "take_profit",
        "gua_filter",
        "sample_gates",
    }
)

_ACCOUNT_MODES = ("portfolio", "per_symbol")
_ENGINE_MODES = ("full", "fast")
_ARTIFACT_LEVELS = ("summary", "candidate", "full")

_ALL_MARKET_CODE = "ALL"
_CUSTOM_UNIVERSE_LABEL = "自定义股票池（%d 只）"
_CUSTOM_DATA_QUALITY = "自定义股票池 · 最新就绪数据（非点时宇宙）"


def _is_all_market_codes(codes: Any) -> bool:
    if not isinstance(codes, (list, tuple)) or not codes:
        return True
    return any(str(code).strip().upper() == _ALL_MARKET_CODE for code in codes)


def _apply_universe_display(profile: dict) -> dict:
    """Derive display fields when the run universe is a custom code list."""
    codes = profile.get("codes")
    if _is_all_market_codes(codes):
        return profile
    profile["universe_label"] = _CUSTOM_UNIVERSE_LABEL % len(codes)
    profile["data_quality"] = _CUSTOM_DATA_QUALITY
    return profile


def _override_int(name: str, value: Any, *, minimum: int, allow_none: bool = False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s 必须是整数" % name)
    if value < minimum:
        raise ValueError("%s 必须 >= %d" % (name, minimum))
    return value


def _override_weekday(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s 必须是 1..7 的整数" % name)
    if value < 1 or value > 7:
        raise ValueError("%s 必须位于 1..7" % name)
    return value


def _override_signal_weekdays(value: Any) -> Optional[List[int]]:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ValueError("signal_weekdays 必须是列表")
    if len(value) > 7:
        raise ValueError("signal_weekdays 长度不能超过 7")
    out = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError("signal_weekdays 元素必须是 1..7 的整数")
        if item < 1 or item > 7:
            raise ValueError("signal_weekdays 元素必须位于 1..7")
        out.add(item)
    return sorted(out) if out else None


def _override_codes(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("codes 必须是列表")
    if len(value) > 1000:
        raise ValueError("codes 数量不能超过 1000")
    codes: List[str] = []
    for code in value:
        if not isinstance(code, str):
            raise ValueError("codes 元素必须是字符串")
        text = code.strip()
        if not text:
            raise ValueError("codes 元素不能为空")
        if len(text) > 32:
            raise ValueError("codes 元素长度不能超过 32")
        codes.append(text)
    if not codes or (len(codes) == 1 and codes[0].upper() == _ALL_MARKET_CODE):
        return [_ALL_MARKET_CODE]
    return codes


def _validate_profile_override(key: str, value: Any) -> Any:
    if key in ("start", "end"):
        return _override_int(key, value, minimum=1, allow_none=True)
    if key == "entry_lag":
        return _override_int(key, value, minimum=1)
    if key == "hold":
        return _override_int(key, value, minimum=1)
    if key in ("buy_weekday", "exit_weekday"):
        return _override_weekday(key, value)
    if key in ("buy_on", "sell_on"):
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError("%s 不能为空" % key)
        return parse_price_session(value)
    if key == "signal_weekdays":
        return _override_signal_weekdays(value)
    if key == "account_mode":
        mode = str(value or "").strip()
        if mode not in _ACCOUNT_MODES:
            raise ValueError("account_mode 必须是 %s" % (_ACCOUNT_MODES,))
        return mode
    if key == "engine":
        engine = str(value or "").strip()
        if engine not in _ENGINE_MODES:
            raise ValueError("engine 必须是 %s" % (_ENGINE_MODES,))
        return engine
    if key == "artifact_level":
        level = str(value or "").strip()
        if level not in _ARTIFACT_LEVELS:
            raise ValueError("artifact_level 必须是 %s" % (_ARTIFACT_LEVELS,))
        return level
    if key == "with_bagua":
        if not isinstance(value, bool):
            raise ValueError("with_bagua 必须是布尔值")
        return value
    if key == "codes":
        return _override_codes(value)
    if key in ("stop_loss", "take_profit"):
        if value is None:
            return None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise ValueError("%s 必须是非负数值" % key)
        return value
    if key == "gua_filter":
        if not isinstance(value, dict):
            raise ValueError("gua_filter 必须是对象")
        return copy.deepcopy(value)
    if key == "sample_gates":
        if not isinstance(value, dict):
            raise ValueError("sample_gates 必须是对象")
        gates = copy.deepcopy(value)
        if "min_round_trips" in gates:
            raw = gates.get("min_round_trips")
            try:
                trips = 0 if isinstance(raw, bool) else int(raw)
            except (TypeError, ValueError):
                trips = 0
            if trips <= 0:
                gates["min_round_trips"] = DEFAULT_MIN_ROUND_TRIPS
                logger.warning(
                    "benchmark_profile.json sample_gates.min_round_trips=%r "
                    "非法，回退默认 %d",
                    raw,
                    DEFAULT_MIN_ROUND_TRIPS,
                )
            else:
                gates["min_round_trips"] = trips
        return gates
    raise ValueError("unsupported profile override: %s" % key)


def _sanitize_profile_overrides(overrides: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key, value in overrides.items():
        if key not in _PROFILE_OVERRIDE_KEYS:
            logger.warning("benchmark_profile.json 忽略不支持的字段 %r", key)
            continue
        try:
            clean[key] = _validate_profile_override(key, value)
        except (TypeError, ValueError) as e:
            logger.warning(
                "benchmark_profile.json 字段 %r 值非法（%s），回退默认", key, e
            )
    return clean


def build_benchmark_profile(cfg: AStockConfig) -> Dict[str, Any]:
    """Default v1 profile with runtime end/fee note + shallow sidecar merge."""
    profile = copy.deepcopy(DEFAULT_BENCHMARK_PROFILE)
    profile["end"] = resolve_data_max_date(cfg)
    profile["fee_note"] = _fee_note_from_costs(cfg.costs)
    overrides = _load_profile_sidecar(cfg)
    if overrides:
        profile.update(_sanitize_profile_overrides(overrides))
    return _apply_universe_display(profile)


def _get_spec(ctx: Any, rule_id: str):
    try:
        return ctx.rules.load_full_registry().get(str(rule_id))
    except KeyError:
        raise KeyError(str(rule_id)) from None


def validate_rule_for_benchmark(spec: Any) -> None:
    """Mirror run_backtest's rule gates with user-facing 400 messages."""
    rid = str(getattr(spec, "id", "") or "")
    compile_status = str(getattr(spec, "compile_status", "") or "")
    if compile_status != "ready":
        raise ValueError(
            "规则不可回测（编译状态 %s）：%s" % (compile_status or "unknown", rid)
        )
    if getattr(spec, "output_type", None) != "signal" or rid == "bagua_ohlc":
        raise ValueError("规则不是可交易信号指标，无法运行基准回测：%s" % rid)
    deps = [str(d) for d in (getattr(spec, "dependencies", None) or [])]
    if "MIN1" in deps:
        raise ValueError("规则依赖分钟线 MIN1，当前无分钟历史，无法运行基准回测：%s" % rid)
    params = getattr(spec, "parameters", None) or {}
    if bool(params.get("min60_native")):
        raise ValueError("该规则依赖原生 60 分钟数据，暂不支持基准回测：%s" % rid)
    if "MIN60" in deps and not bool(getattr(spec, "backtestable", False)):
        raise ValueError(
            "规则依赖 MIN60 且没有可用的日线研究代理，无法运行基准回测：%s" % rid
        )


def needs_research_proxy(spec: Any) -> bool:
    """True when the rule runs through the day-line MIN60 research proxy.

    Native MIN60 rules are rejected by :func:`validate_rule_for_benchmark`
    before this helper matters, so only the day proxy counts as research.
    """
    params = getattr(spec, "parameters", None) or {}
    return bool(params.get("min60_day_proxy"))


def _proxy_detail(spec: Any) -> str:
    params = getattr(spec, "parameters", None) or {}
    note = str(params.get("min60_proxy_note") or "").strip()
    if note:
        return note
    reason = str(getattr(spec, "failure_reason", None) or "").strip()
    return reason or PROXY_DETAIL_FALLBACK


def _formula_audit_allowed(cfg: AStockConfig, spec: Any) -> bool:
    """Mirror run_backtest's tn6 confirmation gate before enqueue.

    txt_self_source user formulas have no package hash and stay allowed;
    package-backed formulas need a live confirmed source pair, otherwise the
    worker would reject the job with ``rejected_unconfirmed_formula`` after
    the user already waited in the queue.
    """
    package_sha = str(getattr(spec, "package_sha256", None) or "")
    if not package_sha:
        return True
    from ..indicators.tn6_importer import load_source_map, resolve_formula_audit

    try:
        mapping = load_source_map(Path(cfg.mapping_path))
    except Exception:
        mapping = {}
    entry = None
    if package_sha in mapping:
        entry = dict(mapping[package_sha])
        package_file = getattr(spec, "package_file", None)
        if package_file:
            entry["package_file"] = str(Path(package_file).resolve())
    audit = resolve_formula_audit(entry, package_sha256=package_sha)
    if (
        entry
        and entry.get("source_pair_status") == "txt_only"
        and str(getattr(spec, "compile_status", "") or "") == "ready"
    ):
        return True
    return bool(audit.get("formal_backtest_allowed", False))


def _assert_queue_capacity(ctx: Any) -> None:
    """Reject new submissions when the in-memory backtest queue is full."""
    try:
        snapshot = ctx.jobs.queue_snapshot()
    except Exception:
        return
    if not isinstance(snapshot, dict):
        return
    try:
        n_queued = int(snapshot.get("n_queued") or 0)
    except (TypeError, ValueError):
        return
    if n_queued >= MAX_QUEUED_BENCHMARKS:
        raise BenchmarkQueueFullError("基准任务队列已满，请稍后再试")


def submit_rule_benchmark(
    ctx: Any, rule_id: str, *, allow_research_proxy: bool = False
) -> Dict[str, Any]:
    """Validate a rule and enqueue its standardized benchmark job."""
    spec = _get_spec(ctx, rule_id)
    validate_rule_for_benchmark(spec)
    rid = str(getattr(spec, "id", "") or rule_id)
    cfg = ctx.cfg
    if not _formula_audit_allowed(cfg, spec):
        raise ValueError(
            "公式来源未确认，无法运行基准回测：%s（请先在指标页确认公式来源）" % rid
        )
    proxy = needs_research_proxy(spec)
    if proxy and not allow_research_proxy:
        return {
            "mode": "needs_confirmation",
            "message": PROXY_MESSAGE,
            "detail": _proxy_detail(spec),
        }

    # 锁内完成「查活跃记录 → 提交 → 落库」，并发首次提交只能产生一个 job。
    with _BENCHMARK_SUBMIT_LOCK:
        existing = get_latest_rule_benchmark(cfg, rid)
        if existing and str(existing.get("status") or "").lower() in (
            "queued",
            "running",
        ):
            job_id = str(existing.get("job_id") or "")
            if job_id:
                try:
                    rec = ctx.jobs.get(job_id)
                except Exception:
                    rec = None
                if rec is not None and str(getattr(rec, "status", "")) in (
                    "queued",
                    "running",
                ):
                    return {
                        "job_id": job_id,
                        "research_proxy": proxy,
                        "reused": True,
                    }

        _assert_queue_capacity(ctx)

        profile = build_benchmark_profile(cfg)
        codes = profile.get("codes")
        if not isinstance(codes, (list, tuple)) or not codes:
            codes = ["ALL"]
        gua_filter = profile.get("gua_filter")
        entry_lag = profile.get("entry_lag")
        hold = profile.get("hold")
        req = BacktestRequest(
            rule_ids=[rid],
            codes=[str(c) for c in codes],
            period=str(profile.get("period") or "DAY"),
            start=profile.get("start"),
            end=profile.get("end"),
            entry_lag=1 if entry_lag is None else int(entry_lag),
            hold=1 if hold is None else int(hold),
            buy_on=str(profile.get("buy_on") or "open"),
            sell_on=str(profile.get("sell_on") or "close"),
            account_mode=str(profile.get("account_mode") or "portfolio"),
            engine=str(profile.get("engine") or "full"),
            artifact_level=str(profile.get("artifact_level") or "full"),
            signal_weekdays=profile.get("signal_weekdays"),
            buy_weekday=profile.get("buy_weekday"),
            exit_weekday=profile.get("exit_weekday"),
            with_bagua=bool(profile.get("with_bagua")),
            gua_filter=copy.deepcopy(gua_filter) if isinstance(gua_filter, dict) else None,
            stop_loss=profile.get("stop_loss"),
            take_profit=profile.get("take_profit"),
        )
        rec = ctx.jobs.submit(req)
        job_id = str(getattr(rec, "job_id", "") or "")
        try:
            create_rule_benchmark(
                cfg,
                rid,
                job_id,
                json.dumps(profile, ensure_ascii=False),
            )
        except Exception:
            # 落库失败补偿取消已提交 job，避免孤儿任务占用队列。
            if job_id:
                try:
                    ctx.jobs.cancel(job_id)
                except Exception:
                    pass
            raise
    return {"job_id": job_id, "research_proxy": proxy, "reused": False}


def _profile_from_record(bench: Dict[str, Any]) -> Optional[dict]:
    profile = bench.get("profile")
    if not isinstance(profile, dict) or not profile:
        return None
    return _apply_universe_display(copy.deepcopy(profile))


def _min_round_trips(profile: Optional[dict]) -> int:
    if isinstance(profile, dict):
        gates = profile.get("sample_gates")
        if isinstance(gates, dict):
            try:
                value = int(gates.get("min_round_trips"))
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
    return DEFAULT_MIN_ROUND_TRIPS


def _metrics_view(metrics: Dict[str, Any]) -> Dict[str, Any]:
    return {key: metrics.get(key) for key in _METRIC_KEYS}


def _has_numeric_metrics(metrics: Dict[str, Any]) -> bool:
    """True when at least one metric holds a finite number (not None/str)."""
    for value in (metrics or {}).values():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(float(value)):
            return True
    return False


def _valid_equity_points(equity: Any) -> int:
    count = 0
    for point in equity or []:
        if not isinstance(point, dict):
            continue
        try:
            value = float(point.get("equity"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            count += 1
    return count


def _summary_failure_detail(summary: Dict[str, Any]) -> str:
    meta = summary.get("meta") if isinstance(summary.get("meta"), dict) else {}
    repro = summary.get("repro") if isinstance(summary.get("repro"), dict) else {}
    analysis = (
        summary.get("analysis_summary")
        if isinstance(summary.get("analysis_summary"), dict)
        else {}
    )
    return str(
        analysis.get("reason")
        or meta.get("reason")
        or meta.get("error")
        or repro.get("reason")
        or summary.get("status")
        or "回测失败"
    )


def _positive_int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _apply_run_cutoff(
    profile: Optional[dict], summary: Dict[str, Any]
) -> Optional[dict]:
    """Clamp the reported profile end to the interval actually backtested.

    Priority: min(run summary end, dataset cutoff) > min(requested end, dataset
    cutoff) > dataset cutoff (when the request had no explicit end). The run
    summary end may echo the requested end rather than the real last trading
    day, so clamping it by the dataset cutoff keeps the display conservative.
    """
    if not isinstance(profile, dict):
        return profile
    meta = summary.get("meta") if isinstance(summary.get("meta"), dict) else {}
    repro = summary.get("repro") if isinstance(summary.get("repro"), dict) else {}
    run_end = _positive_int(summary.get("end"))
    cutoff = 0
    for source in (
        summary.get("data_cutoff_date"),
        meta.get("data_cutoff_date"),
        repro.get("data_cutoff_date"),
    ):
        cutoff = _positive_int(source)
        if cutoff:
            break
    requested_end = _positive_int(profile.get("end"))
    if run_end:
        new_end = min(run_end, cutoff) if cutoff else run_end
    elif cutoff:
        new_end = min(requested_end, cutoff) if requested_end else cutoff
    else:
        return profile
    updated = copy.deepcopy(profile)
    updated["end"] = new_end
    return updated


def _mark_bench(bench: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
    updated = dict(bench)
    updated.update(fields)
    updated["updated_at"] = int(time.time())
    return updated


def _sync_from_job(ctx: Any, bench: Dict[str, Any]) -> Dict[str, Any]:
    """Lazily pull a benchmark record forward from its in-memory job state."""
    job_id = str(bench.get("job_id") or "")
    if not job_id:
        return bench
    cfg = ctx.cfg
    bench_id = int(bench.get("id") or 0)
    try:
        rec = ctx.jobs.get(job_id)
    except KeyError:
        if str(bench.get("status") or "").lower() in ("queued", "running"):
            error = "任务状态丢失（服务重启），请重跑基准测试"
            if bench_id:
                update_rule_benchmark(cfg, bench_id, status="failed", error=error)
            return _mark_bench(bench, status="failed", error=error)
        return bench
    except Exception:
        return bench

    rec_status = str(getattr(rec, "status", "") or "").lower()
    if rec_status == "succeeded":
        run_id = str(getattr(rec, "run_id", "") or "")
        if run_id:
            if bench_id:
                update_rule_benchmark(
                    cfg, bench_id, run_id=run_id, status="succeeded", error=""
                )
            return _mark_bench(bench, status="succeeded", run_id=run_id, error="")
        if bench_id:
            update_rule_benchmark(cfg, bench_id, status="succeeded", error="")
        return _mark_bench(bench, status="succeeded", error="")
    if rec_status in ("failed", "cancelled"):
        error = str(
            getattr(rec, "error", None)
            or ("用户取消" if rec_status == "cancelled" else "基准回测失败")
        )
        if bench_id:
            update_rule_benchmark(cfg, bench_id, status="failed", error=error)
        return _mark_bench(bench, status="failed", error=error)
    return bench


def _failure_payload(
    bench: Dict[str, Any],
    profile: Optional[dict],
    *,
    message: str,
    detail: str,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "has_performance": True,
        "validity": "failed",
        "metrics": {},
        "equity": [],
        "benchmark_equity": [],
        "benchmark_profile": profile,
        "updated_at": int(bench.get("updated_at") or 0) or int(time.time()),
        "demo": False,
        "show_metrics": False,
        "show_chart": False,
        "message": message,
        "detail": detail,
    }
    if run_id:
        payload["source_run_id"] = str(run_id)
    return payload


def _pending_payload(
    bench: Dict[str, Any],
    *,
    status: str,
    job_id: str,
    progress: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Untested-but-alive contract for queued/running benchmark jobs."""
    if status == "running":
        pct_text = ""
        note = ""
        if isinstance(progress, dict):
            pct = progress.get("pct")
            if isinstance(pct, (int, float)) and not isinstance(pct, bool):
                pct_text = "%.6g%%" % float(pct)
            note = str(progress.get("message") or "").strip()
        inner = " · ".join(part for part in (pct_text, note) if part)
        message = (
            "基准回测任务运行中（%s）…" % inner if inner else "基准回测任务运行中…"
        )
    else:
        position = None
        note = ""
        if isinstance(progress, dict):
            queue_position = progress.get("queue_position")
            if isinstance(queue_position, (int, float)) and not isinstance(
                queue_position, bool
            ):
                position = int(queue_position)
            note = str(progress.get("message") or "").strip()
        parts = []
        if position is not None and position > 1:
            parts.append("前面还有 %d 个任务" % (position - 1))
        elif position == 1:
            parts.append("即将开始")
        if note:
            parts.append(note)
        inner = " · ".join(parts)
        message = (
            "基准回测任务排队中（%s）" % inner
            if inner
            else "基准回测任务排队中，请稍后刷新"
        )
    return {
        "has_performance": False,
        "validity": "untested",
        "benchmark_status": status,
        "benchmark_job_id": job_id,
        "message": message,
        "updated_at": int(bench.get("updated_at") or 0) or int(time.time()),
        "demo": False,
        "show_metrics": False,
        "show_chart": False,
        "metrics": {},
        "equity": [],
        "benchmark_equity": [],
    }


def get_rule_performance(ctx: Any, rule_id: str) -> Dict[str, Any]:
    """Benchmark performance payload for the rule drawer (frontend contract)."""
    spec = _get_spec(ctx, rule_id)
    rid = str(getattr(spec, "id", "") or rule_id)
    cfg = ctx.cfg
    bench = get_latest_rule_benchmark(cfg, rid)
    if bench is None:
        return {
            "has_performance": False,
            "validity": "untested",
            "demo": False,
            "show_metrics": False,
            "show_chart": False,
            "metrics": {},
            "equity": [],
            "benchmark_equity": [],
        }

    if not bench.get("run_id") and bench.get("job_id"):
        bench = _sync_from_job(ctx, bench)

    run_id = str(bench.get("run_id") or "")
    profile = _profile_from_record(bench)
    updated_at = int(bench.get("updated_at") or 0) or int(time.time())

    if not run_id:
        status = str(bench.get("status") or "").lower()
        if status in ("queued", "running"):
            job_id = str(bench.get("job_id") or "")
            live = None
            try:
                live = ctx.jobs.get(job_id)
            except Exception:
                live = None
            live_status = (
                str(getattr(live, "status", "") or "").lower() if live is not None else ""
            )
            if live is not None and live_status in ("queued", "running"):
                return _pending_payload(
                    bench,
                    status=live_status,
                    job_id=job_id,
                    progress=getattr(live, "progress", None),
                )
            # job 无法确认存活（丢失/存储异常）：沿用失败语义。
            return _failure_payload(
                bench,
                profile,
                message="基准回测失败",
                detail="基准回测任务状态不可用，请重跑基准测试",
            )
        return _failure_payload(
            bench,
            profile,
            message="基准回测失败",
            detail=str(bench.get("error") or "基准回测任务未产生结果"),
        )

    try:
        summary = load_run_summary(cfg, run_id)
        equity = load_equity_curve(cfg, run_id, max_points=2000)
    except Exception as e:  # noqa: BLE001 - missing artifacts must degrade
        return _failure_payload(
            bench,
            profile,
            message="基准回测结果不可读",
            detail="回测产物缺失或不可读：%s" % e,
            run_id=run_id,
        )

    profile = _apply_run_cutoff(profile, summary)
    metrics_raw = (
        summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
    )
    metrics = _metrics_view(metrics_raw)
    status = str(summary.get("status") or "ok")
    if status != "ok":
        return _failure_payload(
            bench,
            profile,
            message="基准回测失败",
            detail=_summary_failure_detail(summary),
            run_id=run_id,
        )

    try:
        n_round_trips = float(metrics_raw.get("n_round_trips") or 0)
    except (TypeError, ValueError):
        n_round_trips = 0.0
    if not metrics_raw or n_round_trips <= 0:
        return {
            "has_performance": True,
            "validity": "no_trades",
            "metrics": metrics,
            "equity": equity,
            "benchmark_equity": [],
            "benchmark_profile": profile,
            "source_run_id": run_id,
            "updated_at": updated_at,
            "demo": False,
            "show_metrics": False,
            "show_chart": False,
            "detail": "本次基准测试没有产生有效交易",
        }

    message = ""
    if needs_research_proxy(spec):
        validity = "proxy_data"
        message = PROXY_MESSAGE
    else:
        threshold = _min_round_trips(profile)
        if n_round_trips < threshold:
            validity = "insufficient_samples"
            message = (
                "样本不足：已平仓 %d 笔，低于标准基准门槛 %d 笔，结果仅供研究"
                % (int(n_round_trips), threshold)
            )
        else:
            validity = "valid"

    show_metrics = validity in (
        "valid",
        "proxy_data",
        "insufficient_samples",
    ) and _has_numeric_metrics(metrics)
    show_chart = show_metrics and _valid_equity_points(equity) >= 2
    payload: Dict[str, Any] = {
        "has_performance": True,
        "validity": validity,
        "metrics": metrics,
        "equity": equity,
        "benchmark_equity": [],
        "benchmark_profile": profile,
        "source_run_id": run_id,
        "updated_at": updated_at,
        "demo": False,
        "show_metrics": show_metrics,
        "show_chart": show_chart,
    }
    if message:
        payload["message"] = message
    return payload
