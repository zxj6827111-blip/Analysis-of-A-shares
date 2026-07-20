"""wtpy.apps package.

Heavy submodules are imported lazily so lightweight extensions (e.g. astock)
can be used without optional analyst/optimizer dependencies.
"""

from __future__ import annotations

__all__ = [
    "WtBtAnalyst",
    "WtCtaOptimizer",
    "WtHftOptimizer",
    "WtHotPicker",
    "WtCacheMonExchg",
    "WtCacheMonSS",
    "WtMailNotifier",
    "WtCacheMon",
    "WtCCLoader",
    "WtCtaGAOptimizer",
    "OptimizeNotifier",
]


def __getattr__(name: str):
    if name == "WtBtAnalyst":
        from .WtBtAnalyst import WtBtAnalyst

        return WtBtAnalyst
    if name == "WtCtaOptimizer":
        from .WtCtaOptimizer import WtCtaOptimizer

        return WtCtaOptimizer
    if name == "OptimizeNotifier":
        from .WtCtaOptimizer import OptimizeNotifier

        return OptimizeNotifier
    if name == "WtHftOptimizer":
        from .WtHftOptimizer import WtHftOptimizer

        return WtHftOptimizer
    if name == "WtCtaGAOptimizer":
        from .WtCtaGAOptimizer import WtCtaGAOptimizer

        return WtCtaGAOptimizer
    if name == "WtCCLoader":
        from .WtCCLoader import WtCCLoader

        return WtCCLoader
    if name in {
        "WtHotPicker",
        "WtCacheMonExchg",
        "WtCacheMonSS",
        "WtMailNotifier",
        "WtCacheMon",
    }:
        from . import WtHotPicker as _mod

        return getattr(_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
