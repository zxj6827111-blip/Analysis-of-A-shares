"""Indicator models and registry types."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

CompileStatus = Literal["ready", "source_required", "unsupported", "invalid"]
IndicatorKind = Literal["tdx_formula", "native"]
OutputType = Literal["signal", "series", "classification"]


@dataclass
class Tn6Package:
    """Registered .tn6 package metadata (binary never reverse-engineered)."""

    path: str
    name: str
    sha256: str
    size: int
    content_id: str


@dataclass
class IndicatorSpec:
    id: str
    name: str
    version: str
    kind: IndicatorKind
    output_type: OutputType
    supported_periods: Tuple[str, ...]
    source_file: Optional[str] = None
    source_sha256: Optional[str] = None
    package_file: Optional[str] = None
    package_sha256: Optional[str] = None
    compile_status: CompileStatus = "source_required"
    parameters: Dict[str, Any] = field(default_factory=dict)
    aliases: List[str] = field(default_factory=list)
    content_id: Optional[str] = None  # shared content version for duplicate packages
    failure_reason: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    is_default_visible: bool = True
    formula_text: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["supported_periods"] = list(self.supported_periods)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "IndicatorSpec":
        data = dict(data)
        periods = data.get("supported_periods") or []
        data["supported_periods"] = tuple(periods)
        # filter unknown keys
        allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore
        data = {k: v for k, v in data.items() if k in allowed}
        return cls(**data)

    @property
    def uses_min60_day_proxy(self) -> bool:
        """True when MIN60 refs are filled with day-line MACD research proxy."""
        return bool((self.parameters or {}).get("min60_day_proxy"))

    @property
    def backtestable(self) -> bool:
        """True when formula is ready signal output.

        Formulas that reference #MIN60 can still be backtestable when the registry
        enabled the day-MACD research proxy (parameters.min60_day_proxy). That is
        **not** true 60-minute data. MIN1 remains non-backtestable without minute bars.
        """
        if self.compile_status != "ready" or self.output_type != "signal":
            return False
        deps = list(self.dependencies or [])
        if "MIN1" in deps:
            return False
        if "MIN60" in deps:
            return bool((self.parameters or {}).get("min60_day_proxy", False))
        return True
