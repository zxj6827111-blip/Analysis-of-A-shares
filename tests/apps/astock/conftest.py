"""Shared import bootstrap for astock tests (avoid heavy wtpy DLL imports)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _restore_environ_after_test():
    """Roll back every os.environ mutation after each test.

    Script-entry tests call main(), which loads the machine-local .env
    (MARKET_DATA_ROOT / TUSHARE_* / ASTOCK_ENV). Without a rollback those
    values leak into later tests in the same process, redirecting
    get_default_config() and AStockConfig to the PRODUCTION data root and
    writing test manifests into the real external disk.
    """
    import os

    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


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


# ---------------------------------------------------------------------------
# 真实指标公式依赖（指标/ 目录被 .gitignore）
#
# 两条默认复核公式（735金叉及趋势 / 先跌后涨新版5日外）已作为测试 fixture
# 收进仓库（tests/fixtures/formulas/，用户确认可入库），CI 检出即可解析。
# 本机存在真实 指标/ 时仍优先用真实目录，fixture 只做兜底。
# ---------------------------------------------------------------------------

# tests/apps/astock/conftest.py → parents[2] = tests/；公式 fixture 在 tests/fixtures/formulas/
FORMULA_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "formulas"

_FORMULA_RESOLUTION: dict = {}


def formula_indicator_dir() -> Path | None:
    """返回可解析默认复核公式的公式目录：真实 指标/ 优先，fixture 兜底。

    两次调用间做 memo（注册表 bootstrap 会扫描目录+编译，没必要重复）。
    """
    if "dir" in _FORMULA_RESOLUTION:
        return _FORMULA_RESOLUTION["dir"]
    out: Path | None = None
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.indicators.registry import IndicatorRegistry

    try:
        cfg = get_default_config()
        IndicatorRegistry.bootstrap(cfg.indicator_dir, cfg.mapping_path)
        out = Path(cfg.indicator_dir)
    except Exception:  # noqa: BLE001 目录/映射缺失或损坏 → 走 fixture
        out = None
    if out is None and FORMULA_FIXTURE_DIR.exists():
        # mapping 传不存在的路径：load_source_map 对缺失文件返回 {}（txt 规则不依赖 tn6 映射）
        try:
            IndicatorRegistry.bootstrap(FORMULA_FIXTURE_DIR, FORMULA_FIXTURE_DIR / "_no_tn6_map.json")
            out = FORMULA_FIXTURE_DIR
        except Exception:  # noqa: BLE001 fixture 也损坏才算彻底不可用
            out = None
    _FORMULA_RESOLUTION["dir"] = out
    return out


def formula_cfg(tmp_path):
    """构造 indicator_dir 指向可用公式源的隔离 cfg（storage 全在 tmp_path）。

    供依赖真实公式编译的用例使用；本机/CI 都能拿到相同公式内容。

    注意：indicator_dir 指向**真实** 指标/（或 fixture）——只适合只读用例。
    需要建规则/导入（会往 indicator_dir 写文件）的用例请用
    ``isolated_formula_cfg``，否则测试产物会落进真实 指标/ 目录。
    """
    from wtpy.apps.astock.config import get_default_config

    cfg = get_default_config(storage_root=tmp_path)
    d = formula_indicator_dir()
    if d is not None:
        cfg.indicator_dir = d
    return cfg


def isolated_indicator_dir(tmp_path, *, name: str = "ind") -> Path:
    """tmp 下的公式源**副本**目录：既可解析默认复核公式，又不写真实 指标/。

    背景（2026-09-13 踩坑）：导入/建规则类用例会往 cfg.indicator_dir 写文件；
    此前把这类用例的 indicator_dir 直接指向真实 指标/，导致测试产物
    （独立导入规则.txt / pkg_batch.tn6 / escape.txt 等）落进用户目录，
    并让后续用例读到脏状态（"同名文件已存在"、tn6 计数不符）。
    """
    import shutil

    d = Path(tmp_path) / name
    d.mkdir(parents=True, exist_ok=True)
    src = formula_indicator_dir()
    if src is not None:
        for p in sorted(Path(src).glob("*")):
            if p.is_file() and p.suffix.lower() in (".txt", ".tn6"):
                shutil.copy2(p, d / p.name)
    return d


def isolated_formula_cfg(
    tmp_path, *, storage_sub: str = "st", ind_sub: str = "ind", output_sub: str = "out"
):
    """隔离 cfg：storage / indicator / output 全在 tmp_path，公式目录为副本。

    适合既需要「能解析默认复核公式」又「写入不得污染真实目录」的用例。
    """
    from wtpy.apps.astock.config import get_default_config

    cfg = get_default_config(
        storage_root=Path(tmp_path) / storage_sub,
        indicator_dir=Path(tmp_path) / ind_sub,
        output_root=Path(tmp_path) / output_sub,
    )
    Path(cfg.storage_root).mkdir(parents=True, exist_ok=True)
    isolated_indicator_dir(tmp_path, name=ind_sub)
    return cfg


def real_formulas_available(*rule_ids: str) -> bool:
    """每个 rule_id 都能在可用公式源（真实 指标/ 或仓库 fixture）里解析。

    以前 CI 全新检出没有 指标/（.gitignore），依赖公式的用例只能整体
    skip；公式 fixture 入库后 CI 也能跑，仅在公式源与 fixture 都缺失时跳过。
    """
    from wtpy.apps.astock.indicators.registry import IndicatorRegistry

    d = formula_indicator_dir()
    if d is None:
        return False
    reg = IndicatorRegistry.bootstrap(d, d / "_no_tn6_map.json")
    for rid in rule_ids:
        try:
            reg.get(rid)
        except KeyError:
            return False
    return True


requires_real_formulas = pytest.mark.skipif(
    not real_formulas_available("txt_735金叉及趋势", "txt_先跌后涨新版5日外"),
    reason=(
        "指标公式不可用：本机 指标/ 缺失且 tests/fixtures/formulas/  "
        "无法解析默认两条公式"
    ),
)


# ---------------------------------------------------------------------------
# overlay / delta storage fixtures (overlay_v1 改造)
# ---------------------------------------------------------------------------


def _mk_overlay_bar(symbol: str, d: int, base: float):
    from wtpy.apps.astock.data.providers.base import MarketBar

    return MarketBar(
        symbol=symbol, trade_date=d, period="1d",
        open=base, high=base + 0.5, low=base - 0.5, close=base + 0.2,
        volume=1000.0, amount=100000.0, source="tushare", adjustment="none",
    )


OVERLAY_BASE_DATES = [20240101, 20240102, 20240103, 20240104, 20240105, 20240108]


def build_overlay_warehouse(root, *, delisted: bool = False) -> "DatasetStore":
    """Create a store with base raw + factor datasets and overlay enabled."""
    from wtpy.apps.astock.data.dataset_store import (
        DatasetManifest,
        DatasetStore,
        SymbolRecord,
    )
    from wtpy.apps.astock.data.delta_store import (
        OverlayState,
        save_overlay_state,
    )

    store = DatasetStore(root)
    sym_specs = {
        "SSE.STK.600000": 10.0,
        "SZSE.STK.000001": 5.0,
        "SSE.STK.601088": 20.0,
    }
    base_recs = {}
    for sym, base in sym_specs.items():
        bars = [_mk_overlay_bar(sym, d, base) for d in OVERLAY_BASE_DATES]
        sha = store.store_bars(sym, bars)
        base_recs[sym] = SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=OVERLAY_BASE_DATES[0],
            last_date=OVERLAY_BASE_DATES[-1], row_count=len(bars), quality="ok",
        )
    base = DatasetManifest(
        dataset_id="tushare_none_1d_base", source="tushare", adjustment="none",
        period="1d", data_cutoff_date=OVERLAY_BASE_DATES[-1],
        snapshot_date=OVERLAY_BASE_DATES[-1], provider_version="test",
        status="ready", created_at="2024-01-08T18:00:00",
    )
    base.symbols = list(base_recs.values())
    base.symbol_count = len(base_recs)
    base.row_count = sum(r.row_count for r in base_recs.values())
    base.expected_symbol_count = len(base_recs)
    base.imported_symbol_count = len(base_recs)
    base.coverage_ratio = 1.0
    store.publish(base)

    fac_recs = {}
    for sym in sym_specs:
        sha = store.store_factors(sym, [20230101, 20240101], [1.0, 1.5])
        fac_recs[sym] = SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=20230101,
            last_date=20240101, row_count=2, quality="ok",
        )
    fac = DatasetManifest(
        dataset_id="tushare_adjfactor_1d_base", source="tushare",
        adjustment="adj_factor", period="1d", dataset_type="factor",
        data_cutoff_date=OVERLAY_BASE_DATES[-1],
        snapshot_date=OVERLAY_BASE_DATES[-1], provider_version="test",
        status="ready", created_at="2024-01-08T18:05:00",
    )
    fac.symbols = list(fac_recs.values())
    fac.symbol_count = len(fac_recs)
    fac.row_count = 2 * len(fac_recs)
    fac.expected_symbol_count = len(fac_recs)
    fac.imported_symbol_count = len(fac_recs)
    fac.coverage_ratio = 1.0
    store.publish(fac)

    delisted_id = ""
    if delisted:
        dl_sym = "SZSE.STK.300104"
        bars = [_mk_overlay_bar(dl_sym, d, 3.0) for d in OVERLAY_BASE_DATES[:4]]
        sha = store.store_bars(dl_sym, bars)
        dl_rec = SymbolRecord(
            symbol=dl_sym, blob_sha256=sha, first_date=OVERLAY_BASE_DATES[0],
            last_date=OVERLAY_BASE_DATES[3], row_count=len(bars), quality="ok",
        )
        dl = DatasetManifest(
            dataset_id="internal_delisted_complement_base", source="internal",
            adjustment="delisted_complement", period="1d",
            data_cutoff_date=OVERLAY_BASE_DATES[3],
            snapshot_date=OVERLAY_BASE_DATES[3], provider_version="test",
            status="ready", created_at="2024-01-08T18:10:00",
        )
        dl.symbols = [dl_rec]
        dl.symbol_count = 1
        dl.row_count = len(bars)
        dl.expected_symbol_count = 1
        dl.imported_symbol_count = 1
        dl.coverage_ratio = 1.0
        store.publish(dl)
        delisted_id = dl.dataset_id

    st = OverlayState(
        enabled=True,
        base_dataset_id=base.dataset_id,
        base_manifest_sha256=base.manifest_sha256,
        delisted_base_dataset_id=delisted_id,
        delisted_base_manifest_sha256="",
        factor_base_dataset_id=fac.dataset_id,
        factor_base_manifest_sha256=fac.manifest_sha256,
        delta_watermark=OVERLAY_BASE_DATES[-1],
        factor_watermark=OVERLAY_BASE_DATES[-1],
    )
    save_overlay_state(root, st)
    return store


@pytest.fixture
def warehouse(tmp_path):
    """A ready overlay warehouse (base + factor + registry)."""
    return build_overlay_warehouse(tmp_path)


@pytest.fixture
def delta(tmp_path, warehouse):
    from wtpy.apps.astock.data.delta_store import DeltaStore

    return DeltaStore(tmp_path)


def commit_eod_delta(
    store,
    *,
    cutoff: int,
    rows: dict,
    factor_rows: dict = None,
    batch_suffix: str = "eod",
) -> dict:
    """Commit raw (+ optional factor) delta and publish the watermark."""
    from wtpy.apps.astock.data.delta_store import load_overlay_state
    from wtpy.apps.astock.data.delta_writer import DeltaEodWriter

    writer = DeltaEodWriter(store)
    base = store.load_manifest(load_overlay_state(store.root).base_dataset_id)
    batch = writer.commit_bars(
        sync_run_id=f"test_{batch_suffix}", source="tushare",
        base_dataset_id=base.dataset_id, cutoff=cutoff, rows=rows,
        batch_suffix=batch_suffix,
    )
    if factor_rows:
        fac = store.load_manifest(
            load_overlay_state(store.root).factor_base_dataset_id
        )
        writer.commit_factors(
            sync_run_id=f"test_{batch_suffix}", source="tushare",
            factor_base_dataset_id=fac.dataset_id, cutoff=cutoff,
            rows=factor_rows, batch_suffix=f"{batch_suffix}_factor",
        )
    writer.publish(
        delta_watermark=cutoff,
        factor_watermark=cutoff if factor_rows else None,
    )
    return batch
