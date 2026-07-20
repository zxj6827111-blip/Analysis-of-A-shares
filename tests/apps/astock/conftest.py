"""Shared import bootstrap for astock tests (avoid heavy wtpy DLL imports)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ensure_pkg(name: str, path: Path) -> None:
    if name in sys.modules:
        return
    m = ModuleType(name)
    m.__path__ = [str(path)]  # type: ignore[attr-defined]
    m.__package__ = name
    sys.modules[name] = m


def bootstrap() -> None:
    _ensure_pkg("wtpy", ROOT / "wtpy")
    _ensure_pkg("wtpy.apps", ROOT / "wtpy" / "apps")
    _ensure_pkg("wtpy.apps.astock", ROOT / "wtpy" / "apps" / "astock")
    _ensure_pkg("wtpy.apps.astock.data", ROOT / "wtpy" / "apps" / "astock" / "data")
    _ensure_pkg(
        "wtpy.apps.astock.indicators", ROOT / "wtpy" / "apps" / "astock" / "indicators"
    )
    _ensure_pkg("wtpy.apps.astock.bagua", ROOT / "wtpy" / "apps" / "astock" / "bagua")


bootstrap()
