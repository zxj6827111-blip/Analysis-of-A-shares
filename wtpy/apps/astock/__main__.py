"""CLI entry that bootstraps lightweight package shells when full wtpy import is heavy/broken."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType


def _ensure_pkg(name: str, path: Path) -> None:
    if name in sys.modules:
        return
    m = ModuleType(name)
    m.__path__ = [str(path)]  # type: ignore[attr-defined]
    m.__package__ = name
    sys.modules[name] = m


def _bootstrap_if_needed() -> None:
    root = Path(__file__).resolve().parents[3]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    # Prefer normal imports when environment is complete.
    try:
        import wtpy  # noqa: F401
        import wtpy.apps  # noqa: F401
        return
    except Exception:
        pass
    _ensure_pkg("wtpy", root / "wtpy")
    _ensure_pkg("wtpy.apps", root / "wtpy" / "apps")
    _ensure_pkg("wtpy.apps.astock", root / "wtpy" / "apps" / "astock")
    _ensure_pkg("wtpy.apps.astock.data", root / "wtpy" / "apps" / "astock" / "data")
    _ensure_pkg(
        "wtpy.apps.astock.indicators", root / "wtpy" / "apps" / "astock" / "indicators"
    )
    _ensure_pkg("wtpy.apps.astock.bagua", root / "wtpy" / "apps" / "astock" / "bagua")
    _ensure_pkg("wtpy.apps.astock.forecast", root / "wtpy" / "apps" / "astock" / "forecast")


def main(argv=None) -> int:
    _bootstrap_if_needed()
    from .cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
