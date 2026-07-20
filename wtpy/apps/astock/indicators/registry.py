"""Unified indicator registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from .models import IndicatorSpec
from .tn6_importer import build_specs_from_indicator_dir, load_source_map


BAGUA_SPEC = IndicatorSpec(
    id="bagua_ohlc",
    name="八卦OHLC",
    version="1.0.0",
    kind="native",
    output_type="classification",
    supported_periods=("DAY", "WEEK", "MONTH"),
    compile_status="ready",
    parameters={},
    aliases=["bagua", "八卦"],
    content_id="native_bagua_ohlc",
    failure_reason=None,
    dependencies=[],
    is_default_visible=True,
    formula_text=None,
)


class IndicatorRegistry:
    def __init__(self, specs: Optional[List[IndicatorSpec]] = None):
        self._by_id: Dict[str, IndicatorSpec] = {}
        for s in specs or []:
            self.register(s)

    def register(self, spec: IndicatorSpec) -> None:
        self._by_id[spec.id] = spec

    def get(self, indicator_id: str) -> IndicatorSpec:
        if indicator_id in self._by_id:
            return self._by_id[indicator_id]
        # alias lookup
        for s in self._by_id.values():
            if indicator_id == s.name or indicator_id in (s.aliases or []):
                return s
            if s.package_sha256 and indicator_id == s.package_sha256:
                return s
        raise KeyError(indicator_id)

    def list(self) -> List[IndicatorSpec]:
        return list(self._by_id.values())

    def list_visible(self) -> List[IndicatorSpec]:
        return [s for s in self._by_id.values() if s.is_default_visible]

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "indicators": [s.to_dict() for s in self.list()],
        }
        # do not persist full formula text optionally huge — keep it
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "IndicatorRegistry":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        specs = [IndicatorSpec.from_dict(x) for x in data.get("indicators", [])]
        return cls(specs)

    @classmethod
    def bootstrap(
        cls,
        indicator_dir: Path,
        mapping_path: Path,
        *,
        min60_available: bool = False,
        include_bagua: bool = True,
    ) -> "IndicatorRegistry":
        mapping = load_source_map(mapping_path)
        specs = build_specs_from_indicator_dir(
            indicator_dir, mapping, min60_available=min60_available
        )
        reg = cls(specs)
        if include_bagua:
            reg.register(BAGUA_SPEC)
        return reg
