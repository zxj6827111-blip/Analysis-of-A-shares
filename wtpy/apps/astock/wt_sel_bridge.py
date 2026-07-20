"""Optional WonderTrader ET_SEL bridge.

Default A-share backtests use the pure-Python PortfolioBacktester (plan rules).
This module probes whether WtBtPorter / ET_SEL can be loaded. It does **not**
claim production SEL parity until a full stock SEL demo is validated.

Status:
- load_ok: DLL import succeeded
- sel_ready: False until an end-to-end stock ET_SEL run is proven on this host
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass
class WtSelProbe:
    load_ok: bool
    sel_ready: bool
    detail: str
    engine_type_available: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def probe_wt_sel() -> WtSelProbe:
    try:
        from wtpy import EngineType  # type: ignore
        has_et = hasattr(EngineType, "ET_SEL")
    except Exception as e:  # noqa: BLE001
        return WtSelProbe(
            load_ok=False,
            sel_ready=False,
            detail=f"wtpy import failed: {e}",
            engine_type_available=False,
        )
    try:
        from wtpy import WtBtEngine  # type: ignore
        # Instantiation may pull native DLL; catch failures
        _ = WtBtEngine
        return WtSelProbe(
            load_ok=True,
            sel_ready=False,
            detail=(
                "WtBtEngine import ok; stock ET_SEL end-to-end not wired as default. "
                "Use pure-Python PortfolioBacktester for formal A-share path. "
                f"ET_SEL enum present={has_et}."
            ),
            engine_type_available=has_et,
        )
    except Exception as e:  # noqa: BLE001
        return WtSelProbe(
            load_ok=False,
            sel_ready=False,
            detail=f"WtBtEngine unavailable: {e}",
            engine_type_available=has_et,
        )
