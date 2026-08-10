#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据根体检：检查服务器上预置的 MARKET_DATA_ROOT 能否被系统直接识别使用。

部署/排查时运行，输出各数据面状态与明确结论（"能用 / 不能用 / 缺什么"）。

用法:
  python scripts/check_data_root.py                          # 用 .env 的 MARKET_DATA_ROOT
  python scripts/check_data_root.py --storage-root D:\\AStockData\\datasets\\market_data

退出码:
  0 = 数据根可正常使用（系统先读这些数据，滞后时 18:30 自动更新补齐）
  1 = 数据根不可用（目录不存在 / 空根 / 缺少 manifests 无法识别，需先初始化）
  2 = 数据可用但不完整（缺正式产品面 / 数据滞后等，自动同步链会补齐）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

# 脚本在 scripts/ 下运行：把项目根加入 sys.path 以导入 wtpy 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_env() -> None:
    try:
        from wtpy.apps.astock.config import load_env_file

        load_env_file()
    except Exception:
        pass


def _resolve_root(arg: Optional[str]) -> Optional[Path]:
    if arg:
        return Path(arg)
    md = os.environ.get("MARKET_DATA_ROOT", "").strip()
    if md:
        return Path(md)
    try:
        from wtpy.apps.astock.config import get_default_config

        return Path(get_default_config().market_data_root)
    except Exception:
        return None


def _count_blobs(blobs_dir: Path) -> int:
    """第一层文件计数（blobs 目录可能上万文件，scandir 一层即可）。"""
    n = 0
    try:
        with os.scandir(str(blobs_dir)) as it:
            for entry in it:
                if entry.is_file():
                    n += 1
    except OSError:
        pass
    return n


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AStock 数据根体检：检查预置数据能否被系统直接识别使用"
    )
    parser.add_argument(
        "--storage-root", default=None,
        help="数据根路径（默认读 .env 的 MARKET_DATA_ROOT）",
    )
    args = parser.parse_args()

    _load_env()
    root = _resolve_root(args.storage_root)
    if root is None:
        print("❌ 无法确定数据根：未传 --storage-root，且 MARKET_DATA_ROOT / .env 未配置")
        return 1

    print("=" * 64)
    print("AStock 数据根体检")
    print(f"  数据根 : {root}")
    print(f"  存在   : {root.exists()}")

    if not root.exists():
        print()
        print("❌ 数据根目录不存在")
        print("   处理：确认服务器数据挂载/拷贝路径正确，或先运行一次同步生成数据")
        return 1

    manifests_dir = root / "manifests"
    blobs_dir = root / "blobs"
    manifest_files = sorted(manifests_dir.glob("*.json")) if manifests_dir.exists() else []
    blob_count = _count_blobs(blobs_dir) if blobs_dir.exists() else 0

    print(f"  manifests : {len(manifest_files)} 个清单文件")
    print(f"  blobs     : {blob_count} 个数据文件")
    print()

    if not manifest_files:
        if blob_count == 0:
            print("❌ 数据根为空（无 manifests 清单、无 blobs 数据）")
            print("   系统无法判断数据新鲜度，18:30 自动更新会跳过（无法计算滞后）")
        else:
            print("❌ 数据根有文件但缺少 manifests/ 清单 → 系统无法识别")
            print("   （这不是系统生成的数据格式：可能只是裸 CSV/其他软件导出的数据）")
        print()
        print("   处理：运行一次同步完成首次初始化")
        print("     python scripts/sync_market_data.py --source tushare --mode incremental"
              f" --storage-root {root}")
        print("   或整体拷贝一份系统格式（含 manifests/ + blobs/）的数据根到该路径")
        return 1

    # ---- 有可识别数据：统计状态 + 各面健康 ----
    from wtpy.apps.astock.data.dataset_store import DatasetStore
    from wtpy.apps.astock.data.tushare_product import tushare_product_data_health

    store = DatasetStore(root)
    ready = partial = failed = 0
    for mid in store.list_manifests():
        m = store.load_manifest(mid)
        if m is None:
            continue
        st = getattr(m, "status", "")
        if st == "ready":
            ready += 1
        elif st == "partial":
            partial += 1
        elif st == "failed":
            failed += 1
    print(f"  数据集状态 : ready={ready}  partial={partial}  failed={failed}")

    health = tushare_product_data_health(store)

    def _face(label: str, info: dict) -> str:
        if not info:
            return f"  {label:<14} 未生成"
        st = info.get("status") or "missing"
        md = info.get("max_date") or info.get("data_cutoff_date") or "—"
        return f"  {label:<14} {st:<8} 最新={md}"

    print()
    print("  各数据面状态:")
    cf = health.get("current_freshness") or {}
    print(_face("日线(raw)", cf.get("tushare_raw") or {}))
    print(_face("复权因子", cf.get("tushare_factor") or {}))
    print(_face("正式L2", health.get("formal_l2") or {}))
    print(_face("正式L1", health.get("formal_l1") or {}))
    ca_meta = root / "ca_events" / "_meta.json"
    if ca_meta.exists():
        try:
            ca_last = (json.loads(ca_meta.read_text(encoding="utf-8")) or {}).get("last_sync_at", "—")
        except Exception:
            ca_last = "—"
        print(f"  {'CA公司行为':<14} 上次同步={ca_last}")
    else:
        print(f"  {'CA公司行为':<14} 未同步（每日定时会自动拉取）")

    lag = health.get("trading_day_lag") or {}
    print()
    print(f"  数据滞后   : raw={lag.get('raw')} 个交易日  factor={lag.get('factor')} 个交易日")
    print(f"  预期交易日 : {health.get('expected_latest_trading_day')}")
    hc = health.get("historical_completeness") or {}
    print(f"  历史完整度 : {hc.get('note', '—')}")
    print(f"  综合状态   : {health.get('status')}")

    problems = []
    lag_raw = lag.get("raw")
    if lag_raw is None or (isinstance(lag_raw, int) and lag_raw > 5):
        problems.append("日线数据滞后过多或无法判断（18:30 自动更新会自动补齐）")
    if not health.get("formal_l2"):
        problems.append("正式L2(未复权)未生成（自动链 reconcile 步骤会补齐）")
    if not health.get("formal_l1"):
        problems.append("正式L1(前复权)未生成（自动链 reconcile 步骤会补齐）")

    print()
    if not problems:
        print("✅ 数据根可正常使用：系统启动即读取这些数据，滞后时 18:30 自动更新补齐")
        return 0
    print("⚠️  数据可用，但存在以下问题（均可由自动同步链补齐）：")
    for p in problems:
        print("   - " + p)
    print("   提示：无需手动初始化；等 18:30 自动链即可，或手动点一次更新加速")
    return 2


if __name__ == "__main__":
    sys.exit(main())
