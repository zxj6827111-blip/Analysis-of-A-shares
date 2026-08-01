"""Runtime configuration for the A-share extension."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


def _project_root() -> Path:
    # wtpy/apps/astock/config.py -> project root
    return Path(__file__).resolve().parents[3]


def load_env_file(path: Optional[Path] = None, *, override: bool = False) -> Dict[str, str]:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Existing environment variables win unless override=True. Lines starting
    with '#' and blank lines are ignored. Returns the parsed mapping.
    Machine-local paths (e.g. the production MARKET_DATA_ROOT) belong in
    this file, which must stay out of Git.
    """
    path = Path(path) if path else (_project_root() / ".env")
    parsed: Dict[str, str] = {}
    if not path.exists():
        return parsed
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if not key:
                continue
            parsed[key] = val
            if override or key not in os.environ:
                os.environ[key] = val
    except Exception:
        return parsed
    return parsed


def market_data_root_guard(cfg: "AStockConfig") -> Dict[str, Any]:
    """Production-mode guard for the market data root (Gate A 防呆).

    Rules:
      - ASTOCK_ENV=production and root resolves inside the project storage
        (internal test repository) -> blocked, unless
        ASTOCK_ALLOW_INTERNAL_DATA_ROOT=1 is set explicitly.
      - ASTOCK_ENV=production and MARKET_DATA_ROOT env missing -> blocked
        (silent internal fallback is forbidden in production).
      - development/test -> never blocked, but is_internal is reported so
        callers can warn.
    """
    env = cfg.astock_env
    env_val = os.environ.get("MARKET_DATA_ROOT", "").strip()
    root = cfg.market_data_root
    is_internal = not cfg.market_data_root_is_external
    allow = os.environ.get("ASTOCK_ALLOW_INTERNAL_DATA_ROOT", "").strip() in ("1", "true", "yes")
    blocked = False
    reason = ""
    if env == "production":
        if not env_val:
            blocked = not allow
            reason = "MARKET_DATA_ROOT env is not set (production refuses silent internal fallback)"
        elif is_internal:
            blocked = not allow
            reason = f"MARKET_DATA_ROOT resolves inside project storage: {root}"
    return {
        "astock_env": env,
        "market_data_root": str(root),
        "market_data_root_env_set": bool(env_val),
        "is_internal": is_internal,
        "blocked": blocked,
        "reason": reason,
        "override_allowed_by": "ASTOCK_ALLOW_INTERNAL_DATA_ROOT=1" if blocked else "",
    }


@dataclass
class CostConfig:
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.001  # sell side
    slippage: float = 0.0
    note: str = "Example costs only; not user real trading costs."


@dataclass
class AStockConfig:
    project_root: Path = field(default_factory=_project_root)
    tdx_root: Path = field(default_factory=lambda: Path(r"D:\通达信"))
    indicator_dir: Optional[Path] = None
    storage_root: Optional[Path] = None
    output_root: Optional[Path] = None
    registry_path: Optional[Path] = None
    mapping_path: Optional[Path] = None
    manifest_path: Optional[Path] = None
    bagua_json: Optional[Path] = None
    # Forecast module (isolated from backtest outputs)
    forecast_root: Optional[Path] = None
    forecast_kb_path: Optional[Path] = None
    forecast_weekly_dir: Optional[Path] = None
    forecast_exports_dir: Optional[Path] = None
    forecast_yao_index_base: int = 0  # weekly 变卦 prefix 0..5 -> yao_order 1..6

    initial_capital: float = 1_000_000.0
    max_weight: float = 0.10
    lot_size: int = 100
    costs: CostConfig = field(default_factory=CostConfig)

    # Universe defaults
    exclude_bj: bool = True
    only_ashare: bool = True

    def __post_init__(self) -> None:
        root = Path(self.project_root)
        self.project_root = root
        self.tdx_root = Path(self.tdx_root)
        if self.indicator_dir is None:
            self.indicator_dir = root / "指标"
        else:
            self.indicator_dir = Path(self.indicator_dir)
        if self.storage_root is None:
            self.storage_root = root / "storage" / "astock"
        else:
            self.storage_root = Path(self.storage_root)
        if self.output_root is None:
            self.output_root = root / "outputs" / "astock"
        else:
            self.output_root = Path(self.output_root)
        if self.registry_path is None:
            self.registry_path = self.storage_root / "indicators" / "registry.json"
        else:
            self.registry_path = Path(self.registry_path)
        if self.mapping_path is None:
            self.mapping_path = self.storage_root / "indicators" / "tn6_source_map.json"
        else:
            self.mapping_path = Path(self.mapping_path)
        if self.manifest_path is None:
            self.manifest_path = self.storage_root / "manifest.json"
        else:
            self.manifest_path = Path(self.manifest_path)
        if self.bagua_json is None:
            self.bagua_json = (
                Path(__file__).resolve().parent / "bagua" / "bagua_384.json"
            )
        else:
            self.bagua_json = Path(self.bagua_json)
        if self.forecast_root is None:
            self.forecast_root = self.storage_root / "forecast"
        else:
            self.forecast_root = Path(self.forecast_root)
        if self.forecast_kb_path is None:
            self.forecast_kb_path = self.forecast_root / "kb" / "bagua_384_forecast.json"
        else:
            self.forecast_kb_path = Path(self.forecast_kb_path)
        if self.forecast_weekly_dir is None:
            self.forecast_weekly_dir = self.forecast_root / "weekly"
        else:
            self.forecast_weekly_dir = Path(self.forecast_weekly_dir)
        if self.forecast_exports_dir is None:
            self.forecast_exports_dir = self.forecast_root / "exports"
        else:
            self.forecast_exports_dir = Path(self.forecast_exports_dir)

    @property
    def sh_lday(self) -> Path:
        return self.tdx_root / "vipdoc" / "sh" / "lday"

    @property
    def sz_lday(self) -> Path:
        return self.tdx_root / "vipdoc" / "sz" / "lday"

    @property
    def bj_lday(self) -> Path:
        return self.tdx_root / "vipdoc" / "bj" / "lday"

    @property
    def his_day_root(self) -> Path:
        return self.storage_root / "his" / "day"

    @property
    def parquet_root(self) -> Path:
        return self.storage_root / "parquet"

    @property
    def csv_root(self) -> Path:
        return self.storage_root / "csv"

    @property
    def calendar_path(self) -> Path:
        return self.storage_root / "calendar.json"

    @property
    def universe_path(self) -> Path:
        return self.storage_root / "universe.json"

    @property
    def adj_root(self) -> Path:
        return self.storage_root / "adjustments"

    @property
    def astock_env(self) -> str:
        """Deployment environment: production | development | test."""
        return os.environ.get("ASTOCK_ENV", "development").strip().lower() or "development"

    @property
    def market_data_root(self) -> Path:
        """Resolve market data root: env MARKET_DATA_ROOT > storage_root/market_data."""
        env_val = os.environ.get("MARKET_DATA_ROOT", "").strip()
        if env_val:
            return Path(env_val)
        return self.storage_root / "market_data"

    @property
    def market_data_root_is_external(self) -> bool:
        """True if market_data_root points outside the project storage directory."""
        md = self.market_data_root.resolve()
        sr = self.storage_root.resolve()
        return not str(md).startswith(str(sr))

    def ensure_dirs(self) -> None:
        for p in [
            self.storage_root,
            self.output_root,
            self.his_day_root / "SSE",
            self.his_day_root / "SZSE",
            self.parquet_root,
            self.csv_root,
            self.adj_root,
            self.registry_path.parent,
            self.forecast_root,
            Path(self.forecast_kb_path).parent,
            self.forecast_weekly_dir,
            self.forecast_exports_dir,
        ]:
            Path(p).mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, Path):
                d[k] = str(v)
        if isinstance(d.get("costs"), dict):
            pass
        else:
            d["costs"] = asdict(self.costs)
        # asdict already flattens costs; convert nested paths
        for key in (
            "project_root",
            "tdx_root",
            "indicator_dir",
            "storage_root",
            "output_root",
            "registry_path",
            "mapping_path",
            "manifest_path",
            "bagua_json",
            "forecast_root",
            "forecast_kb_path",
            "forecast_weekly_dir",
            "forecast_exports_dir",
        ):
            if key in d and d[key] is not None:
                d[key] = str(d[key])
        return d

    def save(self, path: Optional[Path] = None) -> Path:
        path = Path(path or (self.storage_root / "config.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: Path) -> "AStockConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        costs = data.pop("costs", None)
        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        if costs:
            cfg.costs = CostConfig(**costs)
        return cfg


def get_default_config(**overrides: Any) -> AStockConfig:
    """Build config; re-run path defaults when storage/indicator roots override."""
    # Pass known constructor fields so __post_init__ derives registry/mapping paths.
    ctor_keys = set(AStockConfig.__dataclass_fields__.keys())  # type: ignore[attr-defined]
    ctor = {k: v for k, v in overrides.items() if k in ctor_keys}
    # If storage_root overridden, clear derived paths unless explicitly provided
    if "storage_root" in ctor:
        for derived in ("registry_path", "mapping_path", "manifest_path", "output_root"):
            if derived not in ctor:
                ctor[derived] = None
    if "indicator_dir" in ctor and "bagua_json" not in ctor:
        pass
    cfg = AStockConfig(**ctor) if ctor else AStockConfig()
    for k, v in overrides.items():
        if k not in ctor and hasattr(cfg, k):
            setattr(cfg, k, v)
    # Re-bind derived paths if still pointing at default storage after override
    if "storage_root" in overrides:
        sr = Path(cfg.storage_root)
        if cfg.registry_path is None or "storage" in str(cfg.registry_path):
            # force re-derive under new storage_root
            cfg.registry_path = sr / "indicators" / "registry.json"
            cfg.mapping_path = sr / "indicators" / "tn6_source_map.json"
            cfg.manifest_path = sr / "manifest.json"
    cfg.ensure_dirs()
    return cfg
