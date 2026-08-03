"""A-share multi-period TN6 / bagua research and backtest extension."""

from .config import AStockConfig, get_default_config
from .version import (
    APP_VERSION as __version__,
    get_build_info,
    get_version_info,
    get_version_string,
)

__all__ = [
    "AStockConfig",
    "get_default_config",
    "get_build_info",
    "get_version_info",
    "get_version_string",
]
