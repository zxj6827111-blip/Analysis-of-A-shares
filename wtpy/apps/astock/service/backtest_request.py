# -*- coding: utf-8 -*-
"""Backtest request DTO and research fingerprint wiring."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class BacktestRequest:
    rule_ids: List[str]
    period: str = "DAY"
    hold: int = 1
    entry_lag: int = 1
    # ISO weekdays 1=Mon..7=Sun; empty/None = all. Example: [5] = Friday only.
    signal_weekdays: Optional[List] = None
    # open | close — default open (T+N open buy / open sell)
    buy_on: str = "open"
    sell_on: str = "open"
    # Preferred UI: ISO weekday 1=Mon..7=Sun; overrides entry_lag / hold when set
    buy_weekday: Optional[int] = None
    exit_weekday: Optional[int] = None
    combine: Optional[str] = None  # all | any | None
    codes: Optional[List[str]] = None
    start: Optional[int] = None
    end: Optional[int] = None
    dwm: bool = False
    with_bagua: bool = False
    # When with_bagua / bagua_ohlc is on: default best3 (最佳3爻) filter.
    bagua_filter_mode: Optional[str] = None
    # Flexible gua filter (UI); when active, takes precedence over bagua_filter_mode.
    gua_filter: Optional[dict] = None
    research_unadjusted: bool = False
    research_unconfirmed_formula: bool = False
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    # portfolio = shared cash; per_symbol = TDX-style independent capital per stock
    account_mode: str = "portfolio"
    run_id: Optional[str] = None
    # Phase-3 research options
    engine: str = "full"  # full | fast
    use_signal_cache: bool = False
    artifact_level: str = "full"  # summary | candidate | full
    holiday_policy: Optional[str] = None
    # Multi-source market data fields
    signal_data_source: Optional[str] = None  # tdxquant | tushare | internal | legacy_tdx_local_asof
    signal_adjustment: Optional[str] = None  # front | qfq | asof_qfq
    dataset_id: Optional[str] = None  # locked dataset for reproducibility
    weekly_bar_mode: str = "local_aggregate"  # local_aggregate | vendor_native
    execution_data_source: str = "tdx_local"  # fixed: tdx_local
    execution_dataset_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)





def research_fingerprint_fields_from_request(
    req: "BacktestRequest",
    *,
    costs: Optional[Dict[str, Any]] = None,
    universe_hash: Optional[str] = None,
    indicator_source_hash: Optional[str] = None,
    engine_code_hash: Optional[str] = None,
    market_data_version: Optional[str] = None,
    calendar_version: Optional[str] = None,
    bagua_json_hash: Optional[str] = None,
) -> Dict[str, str]:
    """Compute research fingerprint metadata for run summary / repro / index.

    Does not replace experiment ``param_hash`` (request-params only). Returns
    16-char ``research_fingerprint`` plus cheap component hashes.
    """
    from ..research.fingerprint import research_fingerprint_from_params

    params = req.to_dict() if hasattr(req, "to_dict") else dict(req or {})
    # Align with fingerprint param keys (indicator_ids preferred over rule_ids)
    if params.get("rule_ids") and not params.get("indicator_ids"):
        params = dict(params)
        params["indicator_ids"] = params.get("rule_ids")
    fp = research_fingerprint_from_params(
        params,
        costs=costs,
        engine_code_hash=engine_code_hash,
        universe_hash=universe_hash,
        indicator_source_hash=indicator_source_hash,
        market_data_version=market_data_version,
        calendar_version=calendar_version,
        bagua_json_hash=bagua_json_hash,
    )
    return {
        "research_fingerprint": fp.full_hex(16),
        "signal_fp": fp.signal_hex(16),
        "filter_fp": fp.filter_hex(16),
        "execution_fp": fp.execution_hex(16),
    }


