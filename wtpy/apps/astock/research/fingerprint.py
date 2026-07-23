# -*- coding: utf-8 -*-
"""Full research fingerprint for de-dup and cache invalidation (phase-1).

Unlike ``service.db.param_hash`` (request parameters only), a research fingerprint
includes code / data / rule versions so results are not reused after engine or
market-data changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

# Bump when fingerprint field set or hashing rules change.
FINGERPRINT_SCHEMA_VERSION = "research_fp_v2"


def _norm(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _norm(v[k]) for k in sorted(v.keys(), key=str)}
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, float):
        # stable short float repr
        return float(v)
    return v


def _json_dumps(obj: Any) -> str:
    return json.dumps(_norm(obj), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint_hex(payload: Dict[str, Any], *, n: Optional[int] = None) -> str:
    """SHA-256 hex of normalized payload; optional truncate to n chars."""
    digest = hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()
    if n is None:
        return digest
    return digest[: int(n)]


def short_fingerprint(payload: Dict[str, Any], n: int = 16) -> str:
    return fingerprint_hex(payload, n=n)


def file_sha256(path: Union[str, Path], *, n: int = 16) -> Optional[str]:
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:n]


def text_sha256(text: str, *, n: int = 16) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:n]


def code_paths_sha(
    paths: Sequence[Union[str, Path]],
    *,
    n: int = 16,
) -> str:
    """Combined hash of existing source files (sorted by path string)."""
    parts: List[str] = []
    for raw in sorted((str(Path(p)) for p in paths), key=str):
        p = Path(raw)
        if p.is_file():
            parts.append("%s:%s" % (p.name, file_sha256(p, n=32) or ""))
    return text_sha256("|".join(parts), n=n)


def default_engine_code_hash() -> str:
    """Hash of core backtest modules that affect trade outcomes."""
    root = Path(__file__).resolve().parents[1]  # wtpy/apps/astock
    paths = [
        root / "strategy.py",
        root / "study.py",
        root / "data" / "calendar.py",
        root / "data" / "limit_rules.py",
        root / "research" / "fingerprint.py",
    ]
    return code_paths_sha(paths, n=16)


@dataclass
class ResearchFingerprint:
    """Structured fingerprint components (signal / filter / execution)."""

    schema_version: str = FINGERPRINT_SCHEMA_VERSION
    signal: Dict[str, Any] = field(default_factory=dict)
    filter: Dict[str, Any] = field(default_factory=dict)
    execution: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "signal": _norm(self.signal),
            "filter": _norm(self.filter),
            "execution": _norm(self.execution),
        }

    def signal_hex(self, n: int = 16) -> str:
        return short_fingerprint({"schema": self.schema_version, **self.signal}, n=n)

    def filter_hex(self, n: int = 16) -> str:
        return short_fingerprint(
            {"schema": self.schema_version, "signal": self.signal_hex(32), **self.filter},
            n=n,
        )

    def execution_hex(self, n: int = 16) -> str:
        return short_fingerprint(
            {
                "schema": self.schema_version,
                "filter": self.filter_hex(32),
                **self.execution,
            },
            n=n,
        )

    def full_hex(self, n: int = 16) -> str:
        return short_fingerprint(self.to_dict(), n=n)

    def as_param_hash_compat(self) -> str:
        """16-char hex compatible with existing param_hash column width."""
        return self.full_hex(16)


def build_signal_fingerprint(
    *,
    indicator_ids: Optional[Sequence[str]] = None,
    indicator_names: Optional[Sequence[str]] = None,
    indicator_source_hash: Optional[str] = None,
    indicator_params: Optional[Dict[str, Any]] = None,
    period: Optional[str] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    universe_hash: Optional[str] = None,
    market_data_version: Optional[str] = None,
    adjust_mode: Optional[str] = None,
    calendar_version: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    body = {
        "indicator_ids": list(indicator_ids or []),
        "indicator_names": list(indicator_names or []),
        "indicator_source_hash": indicator_source_hash,
        "indicator_params": indicator_params or {},
        "period": period,
        "start": start,
        "end": end,
        "universe_hash": universe_hash,
        "market_data_version": market_data_version,
        "adjust_mode": adjust_mode,
        "calendar_version": calendar_version,
    }
    if extra:
        body["extra"] = extra
    return body


def build_filter_fingerprint(
    *,
    signal_fingerprint_hex: Optional[str] = None,
    signal_weekdays: Optional[Sequence[int]] = None,
    gua_filter: Optional[Dict[str, Any]] = None,
    gua_rule_version: Optional[str] = None,
    with_bagua: Optional[bool] = None,
    bagua_json_hash: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    body = {
        "signal_fingerprint": signal_fingerprint_hex,
        "signal_weekdays": list(signal_weekdays) if signal_weekdays is not None else None,
        "gua_filter": gua_filter,
        "gua_rule_version": gua_rule_version,
        "with_bagua": with_bagua,
        "bagua_json_hash": bagua_json_hash,
    }
    if extra:
        body["extra"] = extra
    return body


def build_execution_fingerprint(
    *,
    hold: Optional[int] = None,
    entry_lag: Optional[int] = None,
    buy_weekday: Optional[int] = None,
    exit_weekday: Optional[int] = None,
    buy_on: Optional[str] = None,
    sell_on: Optional[str] = None,
    holiday_policy: Optional[str] = None,
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    account_mode: Optional[str] = None,
    costs: Optional[Dict[str, Any]] = None,
    engine_code_hash: Optional[str] = None,
    limit_rules_version: Optional[str] = None,
    signal_price_mode: Optional[str] = None,
    execution_price_mode: Optional[str] = None,
    valuation_price_mode: Optional[str] = None,
    corporate_action_policy: Optional[str] = None,
    engine_result_version: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    body = {
        "hold": hold,
        "entry_lag": entry_lag,
        "buy_weekday": buy_weekday,
        "exit_weekday": exit_weekday,
        "buy_on": buy_on,
        "sell_on": sell_on,
        "holiday_policy": holiday_policy,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "account_mode": account_mode,
        "costs": costs or {},
        "engine_code_hash": engine_code_hash or default_engine_code_hash(),
        "limit_rules_version": limit_rules_version or "DefaultAShareLimitRule",
        "signal_price_mode": signal_price_mode,
        "execution_price_mode": execution_price_mode,
        "valuation_price_mode": valuation_price_mode,
        "corporate_action_policy": corporate_action_policy,
        "engine_result_version": engine_result_version,
    }
    if extra:
        body["extra"] = extra
    return body


def build_research_fingerprint(
    *,
    # signal
    indicator_ids: Optional[Sequence[str]] = None,
    indicator_names: Optional[Sequence[str]] = None,
    indicator_source_hash: Optional[str] = None,
    indicator_params: Optional[Dict[str, Any]] = None,
    period: Optional[str] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    universe_hash: Optional[str] = None,
    market_data_version: Optional[str] = None,
    adjust_mode: Optional[str] = None,
    calendar_version: Optional[str] = None,
    # filter
    signal_weekdays: Optional[Sequence[int]] = None,
    gua_filter: Optional[Dict[str, Any]] = None,
    gua_rule_version: Optional[str] = None,
    with_bagua: Optional[bool] = None,
    bagua_json_hash: Optional[str] = None,
    # execution
    hold: Optional[int] = None,
    entry_lag: Optional[int] = None,
    buy_weekday: Optional[int] = None,
    exit_weekday: Optional[int] = None,
    buy_on: Optional[str] = None,
    sell_on: Optional[str] = None,
    holiday_policy: Optional[str] = None,
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    account_mode: Optional[str] = None,
    costs: Optional[Dict[str, Any]] = None,
    engine_code_hash: Optional[str] = None,
    extra_signal: Optional[Dict[str, Any]] = None,
    extra_filter: Optional[Dict[str, Any]] = None,
    extra_execution: Optional[Dict[str, Any]] = None,
    signal_price_mode: Optional[str] = None,
    execution_price_mode: Optional[str] = None,
    valuation_price_mode: Optional[str] = None,
    corporate_action_policy: Optional[str] = None,
    engine_result_version: Optional[str] = None,
) -> ResearchFingerprint:
    sig = build_signal_fingerprint(
        indicator_ids=indicator_ids,
        indicator_names=indicator_names,
        indicator_source_hash=indicator_source_hash,
        indicator_params=indicator_params,
        period=period,
        start=start,
        end=end,
        universe_hash=universe_hash,
        market_data_version=market_data_version,
        adjust_mode=adjust_mode,
        calendar_version=calendar_version,
        extra=extra_signal,
    )
    flt = build_filter_fingerprint(
        signal_fingerprint_hex=short_fingerprint(sig, n=32),
        signal_weekdays=signal_weekdays,
        gua_filter=gua_filter,
        gua_rule_version=gua_rule_version,
        with_bagua=with_bagua,
        bagua_json_hash=bagua_json_hash,
        extra=extra_filter,
    )
    exe = build_execution_fingerprint(
        hold=hold,
        entry_lag=entry_lag,
        buy_weekday=buy_weekday,
        exit_weekday=exit_weekday,
        buy_on=buy_on,
        sell_on=sell_on,
        holiday_policy=holiday_policy,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        account_mode=account_mode,
        costs=costs,
        engine_code_hash=engine_code_hash,
        signal_price_mode=signal_price_mode,
        execution_price_mode=execution_price_mode,
        valuation_price_mode=valuation_price_mode,
        corporate_action_policy=corporate_action_policy,
        engine_result_version=engine_result_version,
        extra=extra_execution,
    )
    return ResearchFingerprint(signal=sig, filter=flt, execution=exe)


def research_fingerprint_from_params(
    params: Dict[str, Any],
    *,
    costs: Optional[Dict[str, Any]] = None,
    engine_code_hash: Optional[str] = None,
    gua_rule_version: Optional[str] = None,
    market_data_version: Optional[str] = None,
    universe_hash: Optional[str] = None,
    indicator_source_hash: Optional[str] = None,
    calendar_version: Optional[str] = None,
    bagua_json_hash: Optional[str] = None,
) -> ResearchFingerprint:
    """Build fingerprint from a loose params dict (API / experiment variant)."""
    p = params or {}
    research_unadj = bool(p.get("research_unadjusted"))
    signal_price_mode = p.get("signal_price_mode") or (
        "raw" if research_unadj else "causal_qfq"
    )
    execution_price_mode = p.get("execution_price_mode") or "raw"
    valuation_price_mode = p.get("valuation_price_mode") or "raw"
    corporate_action_policy = p.get("corporate_action_policy") or "ledger_factor_ratio"
    engine_result_version = p.get("engine_result_version") or "dual_price_v1"
    return build_research_fingerprint(
        indicator_ids=p.get("indicator_ids") or p.get("rule_ids"),
        indicator_names=p.get("indicator_names"),
        indicator_source_hash=indicator_source_hash or p.get("indicator_source_hash"),
        indicator_params=p.get("indicator_params"),
        period=p.get("period"),
        start=p.get("start"),
        end=p.get("end"),
        universe_hash=universe_hash or p.get("universe_hash"),
        market_data_version=market_data_version or p.get("market_data_version"),
        adjust_mode=p.get("adjust_mode")
        or ("research_unadjusted" if research_unadj else "adjusted"),
        calendar_version=calendar_version or p.get("calendar_version"),
        signal_weekdays=p.get("signal_weekdays"),
        gua_filter=p.get("gua_filter"),
        gua_rule_version=(
            gua_rule_version
            if gua_rule_version is not None
            else (
                (p.get("gua_filter") or {}).get("rule_version")
                if isinstance(p.get("gua_filter"), dict)
                else None
            )
        ),
        with_bagua=p.get("with_bagua"),
        bagua_json_hash=bagua_json_hash or p.get("bagua_json_hash"),
        hold=p.get("hold"),
        entry_lag=p.get("entry_lag"),
        buy_weekday=p.get("buy_weekday"),
        exit_weekday=p.get("exit_weekday"),
        buy_on=p.get("buy_on"),
        sell_on=p.get("sell_on"),
        holiday_policy=p.get("holiday_policy"),
        stop_loss_pct=p.get("stop_loss_pct") or p.get("stop_loss"),
        take_profit_pct=p.get("take_profit_pct") or p.get("take_profit"),
        account_mode=p.get("account_mode"),
        costs=costs or p.get("costs"),
        engine_code_hash=engine_code_hash,
        signal_price_mode=signal_price_mode,
        execution_price_mode=execution_price_mode,
        valuation_price_mode=valuation_price_mode,
        corporate_action_policy=corporate_action_policy,
        engine_result_version=engine_result_version,
    )
