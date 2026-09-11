"""Unified indicator registry."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from ..data.io_util import atomic_write_json
from .models import IndicatorSpec
from .tn6_importer import build_specs_from_indicator_dir, load_source_map

logger = logging.getLogger(__name__)


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
        payload = {
            "indicators": [s.to_dict() for s in self.list()],
        }
        # do not persist full formula text optionally huge — keep it
        atomic_write_json(path, payload)

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
        user_registry_path: Optional[Path] = None,
    ) -> "IndicatorRegistry":
        mapping = load_source_map(mapping_path)
        specs = build_specs_from_indicator_dir(
            indicator_dir, mapping, min60_available=min60_available
        )
        reg = cls(specs)
        if include_bagua:
            reg.register(BAGUA_SPEC)
        # user rules load after system specs so same-id user entries win
        # (read-only merge; system bootstrap stays independent of RuleService)
        if user_registry_path is not None:
            upath = Path(user_registry_path)
            if upath.exists():
                try:
                    user = cls.load(upath)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "user registry load failed (%s), system specs kept: %s",
                        upath,
                        e,
                    )
                else:
                    for s in user.list():
                        reg.register(s)
        return reg
