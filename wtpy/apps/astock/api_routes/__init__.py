"""AStock API router modules (split out of api.create_app)."""

from .context import ApiContext, get_ctx
from . import backtests, bagua, experiments, forecast, research, rules, system

__all__ = [
    "ApiContext",
    "get_ctx",
    "backtests",
    "bagua",
    "experiments",
    "forecast",
    "research",
    "rules",
    "system",
]
