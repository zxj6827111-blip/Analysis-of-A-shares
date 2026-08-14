"""Gate B2: sync Tushare raw (unadjusted) daily bars for delisted stocks
missing from local_vendor, and publish them as an immutable dataset.

Only the B1-approved candidate list is downloaded — never the whole market.
``--auto-candidates`` (zero-config) derives that list from Tushare's official
``stock_basic list_status='D'`` delisted-stock roster, so fresh installs get
the delisted pool without any hand-maintained candidate file.

Features: Windows-safe task lock, per-symbol checkpoint with atomic writes,
resume, rate limiting with exponential backoff, 6000-row pagination, strict
validation before publish, unit transform (vol 手->股 x100, amount 千元->元
x1000), per-symbol provenance, token never printed.

Usage:
  python scripts/sync_tushare_delisted.py --candidates <b1_candidates.csv> \
      [--limit N] [--symbols 600001.SH,300104.SZ] [--publish] [--fresh-symbol X]
  python scripts/sync_tushare_delisted.py --auto-candidates --publish \
      [--state-dir <dir>] [--cutoff YYYYMMDD]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from wtpy.apps.astock.data import tushare_delisted_sync as tds  # noqa: E402
from wtpy.apps.astock.data.dataset_store import (  # noqa: E402
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
    evaluate_strict_publish,
    make_dataset_id,
)
from wtpy.apps.astock.data.historical_universe import (  # noqa: E402
    ts_code_to_canonical,
)
from wtpy.apps.astock.data.io_util import atomic_write_json  # noqa: E402
from wtpy.apps.astock.data.sync_lock import (  # noqa: E402
    SyncLockHeldError,
    SyncTaskLock,
)

def resolve_market_data_root() -> Optional[Path]:
    """Resolve the market data root from ``MARKET_DATA_ROOT`` env.

    Returns None when unset — callers must fail loudly instead of falling
    back to a hardcoded Windows path (which silently breaks Linux/fresh
    installs). Sync chains always set the env var; standalone users must
    export it.
    """
    env_val = os.environ.get("MARKET_DATA_ROOT", "").strip()
    if env_val:
        return Path(env_val)
    return None


DEFAULT_STATE_DIR = Path(r"E:\AStockData\raw\tushare\delisted_daily")
CALL_INTERVAL_SEC = 0.35
MAX_RETRIES = 4


def state_path(state_dir: Path) -> Path:
    return state_dir / "sync_state.json"


def load_state(state_dir: Path) -> dict:
    p = state_path(state_dir)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"version": 1, "symbols": {}}


def save_state(state_dir: Path, state: dict) -> None:
    atomic_write_json(state_path(state_dir), state)


def fetch_with_retry(pro, ts_code: str, end_date: str) -> pd.DataFrame:
    last_cls, last_msg = "api_failed", ""
    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(CALL_INTERVAL_SEC)
            return pro.daily(ts_code=ts_code, end_date=end_date)
        except Exception as exc:  # noqa: BLE001
            cls = tds.classify_sync_error(exc)
            last_cls, last_msg = cls, tds.scrub_secret(exc)[:300]
            if cls in ("permission_denied", "auth_failed"):
                raise RuntimeError(f"{cls}: {last_msg}")
            wait = 2.0 * (2**attempt)
            print(f"    retry({cls}) in {wait:.0f}s: {last_msg[:120]}")
            time.sleep(wait)
    raise RuntimeError(f"{last_cls}: {last_msg}")


def sync_symbol(pro, ts_code: str, cand: dict, state_dir: Path, cutoff: int) -> dict:
    """Download+validate one symbol. Returns its state entry."""
    raw_dir = state_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{ts_code.replace('.', '_')}.csv"

    entry: Dict = {
        "ts_code": ts_code,
        "symbol": ts_code_to_canonical(ts_code),
        "requested_start": int(cand.get("requested_start") or 0),
        "requested_end": int(cand.get("requested_end") or cutoff),
        "status": tds.STATUS_DOWNLOADING,
        "retry_count": 0,
        "source": "tushare",
        "api_name": tds.API_NAME,
        "unit_transform": tds.UNIT_TRANSFORM_VERSION,
    }

    if raw_path.exists():
        merged = pd.read_csv(raw_path)
        entry["pages"] = -1  # from cache
        entry["from_cache"] = True
    else:
        def fetch_page(ts_code: str, end_date: str) -> pd.DataFrame:
            return fetch_with_retry(pro, ts_code, end_date)

        try:
            merged, pages = tds.fetch_daily_paginated(fetch_page, ts_code)
        except RuntimeError as exc:
            cls = str(exc).split(":", 1)[0]
            entry["status"] = tds.STATUS_FAILED
            entry["error_class"] = cls if cls in (
                "permission_denied", "auth_failed", "rate_limited",
                "timeout", "network_error", "api_failed",
            ) else "api_failed"
            entry["error"] = tds.scrub_secret(exc)[:300]
            return entry
        entry["pages"] = pages
        entry["from_cache"] = False
        if merged.empty:
            entry["status"] = tds.STATUS_NO_DATA
            entry["no_data_reason"] = "tushare_daily_empty_response"
            # persist an empty marker so resume does not refetch
            tmp = raw_path.with_suffix(".csv.tmp")
            pd.DataFrame(
                columns=["ts_code", "trade_date", "open", "high", "low",
                         "close", "vol", "amount"]
            ).to_csv(tmp, index=False)
            os.replace(tmp, raw_path)
            return entry
        tmp = raw_path.with_suffix(".csv.tmp")
        merged.to_csv(tmp, index=False)
        os.replace(tmp, raw_path)

    if merged.empty:
        entry["status"] = tds.STATUS_NO_DATA
        entry["no_data_reason"] = "tushare_daily_empty_response"
        return entry

    entry["status"] = tds.STATUS_DOWNLOADED
    merged = tds.clip_to_cutoff(merged, cutoff)
    if merged.empty:
        entry["status"] = tds.STATUS_NO_DATA
        entry["no_data_reason"] = f"all_bars_after_cutoff_{cutoff}"
        return entry

    merged, repaired_dates = tds.repair_ohlc_envelope(merged)
    if repaired_dates:
        ratio = len(repaired_dates) / len(merged)
        entry["ohlc_repaired_rows"] = len(repaired_dates)
        entry["ohlc_repaired_dates"] = repaired_dates[:20]
        entry["ohlc_repair_rule"] = tds.OHLC_REPAIR_RULE_VERSION
        if ratio > tds.OHLC_REPAIR_MAX_RATIO:
            entry["status"] = tds.STATUS_FAILED
            entry["error_class"] = "validation_failed"
            entry["error"] = (
                f"ohlc_repair_ratio_{ratio:.4f}_exceeds_"
                f"{tds.OHLC_REPAIR_MAX_RATIO}"
            )
            return entry

    v = tds.validate_daily_frame(merged)
    if not v.ok:
        entry["status"] = tds.STATUS_FAILED
        entry["error_class"] = "validation_failed"
        entry["error"] = ";".join(v.reasons)[:300]
        return entry

    entry["status"] = tds.STATUS_VALIDATED
    entry["actual_first_date"] = v.first_date
    entry["actual_last_date"] = v.last_date
    entry["bar_count"] = v.row_count
    return entry


def auto_generate_candidates(token: str, state_dir: Path) -> Path:
    """Zero-config candidate list from Tushare's official delisted roster.

    Fetches ``stock_basic list_status='D'`` via :class:`TushareProvider` and
    writes a candidate CSV compatible with ``--candidates``
    (``ts_code/requested_start/requested_end`` columns, list_date/delist_date
    as YYYYMMDD ints). Deterministic content hash keeps repeated runs on the
    same roster idempotent. Returns the generated CSV path.
    """
    from wtpy.apps.astock.data.providers.tushare import TushareProvider

    provider = TushareProvider(token=token or None)
    entries = provider.fetch_universe(include_delisted=True)
    delisted = [e for e in entries if e.status == "delisted"]
    if not delisted:
        raise RuntimeError(
            "Tushare 退市名单为空（stock_basic list_status='D' 无数据）"
        )
    rows = []
    skipped = []
    for e in delisted:
        ts_code = TushareProvider._to_ts_code(e.symbol)
        try:
            ts_code_to_canonical(ts_code)
        except ValueError:
            # roster 里偶有非规范代码（如 T 前缀退市整理标记），
            # 下载阶段 ts_code_to_canonical 会崩溃——过滤而不是带病下载
            skipped.append(ts_code)
            continue
        rows.append(
            {
                "ts_code": ts_code,
                "requested_start": int(e.list_date or 0),
                "requested_end": int(e.delist_date or 0),
            }
        )
    if skipped:
        print(
            f"[auto] skipped {len(skipped)} non-canonical codes: "
            f"{skipped[:10]}"
        )
    rows.sort(key=lambda r: r["ts_code"])
    payload = pd.DataFrame(rows).to_csv(index=False)
    sha8 = hashlib.sha256(payload.encode("utf-8-sig")).hexdigest()[:8]
    path = state_dir / f"candidates_auto_{sha8}.csv"
    if not path.exists():
        path.write_text(payload, encoding="utf-8-sig")
    print(
        f"[auto] delisted candidates: {len(rows)} symbols "
        f"<- stock_basic list_status='D' ({path.name})"
    )
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default="",
                    help="manual B1 candidate CSV (mutually exclusive with "
                         "--auto-candidates)")
    ap.add_argument("--auto-candidates", action="store_true",
                    help="derive the candidate list from stock_basic "
                         "list_status='D' (zero-config, no manual CSV)")
    ap.add_argument("--token", default="",
                    help="Tushare API token (prefer ts.get_token()); required "
                         "for --auto-candidates when not configured elsewhere")
    ap.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    ap.add_argument("--cutoff", type=int, default=0,
                    help="data cutoff YYYYMMDD (default: today)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--allow-no-data-file", default="")
    ap.add_argument("--report-path", default="")
    args = ap.parse_args()

    if bool(args.candidates) == bool(args.auto_candidates):
        ap.error("exactly one of --candidates / --auto-candidates is required")
    md_root = resolve_market_data_root()
    if md_root is None:
        print(
            "ERROR: MARKET_DATA_ROOT 环境变量未设置，无法确定数据根。\n"
            "  请 export MARKET_DATA_ROOT=<数据根目录>（或由同步链自动传入）后重试。"
        )
        return 1
    cutoff = args.cutoff or int(time.strftime("%Y%m%d"))
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    if args.auto_candidates:
        try:
            candidates_csv = auto_generate_candidates(args.token, state_dir)
        except Exception as exc:  # noqa: BLE001
            # AuthenticationError etc. -> a readable failure instead of a
            # bare traceback; every fresh install hits this when TUSHARE_TOKEN
            # was never configured.
            print(
                f"ERROR: 自动生成退市候选失败: {type(exc).__name__}: {exc}\n"
                "  请确认已配置 TUSHARE_TOKEN（README 部署章节），"
                "或改用 --candidates 传入人工候选清单。"
            )
            return 1
    else:
        candidates_csv = Path(args.candidates)

    cands = pd.read_csv(candidates_csv)
    if args.symbols:
        wanted = set(args.symbols.split(","))
        cands = cands[cands["ts_code"].isin(wanted)]
    if args.limit:
        cands = cands.head(args.limit)
    cand_list = cands.to_dict("records")
    print(f"candidates in scope: {len(cand_list)}")

    lock = SyncTaskLock(
        md_root,
        source="tushare_delisted",
        adjustment="none",
        period="1d",
        sync_run_id=f"tushare_delisted_{time.strftime('%Y%m%dT%H%M%S')}",
    )
    try:
        lock.acquire()
    except SyncLockHeldError as e:
        print(f"ERROR: sync lock held by {e.holder}")
        return 2

    try:
        import tushare as ts

        pro = ts.pro_api(args.token or None)

        state = load_state(state_dir)
        done_statuses = (tds.STATUS_VALIDATED, tds.STATUS_NO_DATA, tds.STATUS_PUBLISHED)
        n_done = 0
        for i, cand in enumerate(cand_list):
            ts_code = str(cand["ts_code"])
            prev = state["symbols"].get(ts_code)
            if prev and prev.get("status") in done_statuses:
                n_done += 1
                continue
            print(f"[{i + 1}/{len(cand_list)}] {ts_code} ...")
            entry = sync_symbol(pro, ts_code, cand, state_dir, cutoff)
            if prev and prev.get("status") == tds.STATUS_FAILED:
                entry["retry_count"] = int(prev.get("retry_count", 0)) + 1
            state["symbols"][ts_code] = entry
            save_state(state_dir, state)
            print(f"    -> {entry['status']} "
                  f"rows={entry.get('bar_count', 0)} "
                  f"{entry.get('error', entry.get('no_data_reason', ''))}")

        counts: Dict[str, int] = {}
        for ts_code in [str(c["ts_code"]) for c in cand_list]:
            st = state["symbols"].get(ts_code, {}).get("status", "missing")
            counts[st] = counts.get(st, 0) + 1
        print(f"status counts: {counts}")

        result = {
            "scope": len(cand_list),
            "counts": counts,
            "published": False,
            "dataset_id": None,
        }

        failed = [
            t for t in [str(c["ts_code"]) for c in cand_list]
            if state["symbols"].get(t, {}).get("status") == tds.STATUS_FAILED
        ]
        if args.publish and failed:
            print(f"REFUSING publish: {len(failed)} failed symbols: {failed[:10]}")
            result["publish_refused"] = f"failed_symbols={len(failed)}"
        elif args.publish:
            store = DatasetStore(md_root)
            records: List[SymbolRecord] = []
            provenance_symbols: Dict[str, dict] = {}
            total_rows = 0
            for cand in cand_list:
                ts_code = str(cand["ts_code"])
                entry = state["symbols"][ts_code]
                symbol = entry["symbol"]
                if entry["status"] == tds.STATUS_NO_DATA:
                    records.append(
                        SymbolRecord(
                            symbol=symbol, blob_sha256="", quality="no_data",
                            error=entry.get("no_data_reason", "no_data"),
                        )
                    )
                    provenance_symbols[symbol] = {
                        k: entry.get(k)
                        for k in ("ts_code", "requested_start", "requested_end",
                                  "status", "retry_count", "no_data_reason",
                                  "source", "api_name", "unit_transform")
                    }
                    continue
                raw_path = state_dir / "raw" / f"{ts_code.replace('.', '_')}.csv"
                merged = tds.clip_to_cutoff(pd.read_csv(raw_path), cutoff)
                merged, repaired_dates = tds.repair_ohlc_envelope(merged)
                if repaired_dates and (
                    len(repaired_dates) / len(merged) > tds.OHLC_REPAIR_MAX_RATIO
                ):
                    raise RuntimeError(
                        f"publish-time repair ratio exceeded for {ts_code}: "
                        f"{len(repaired_dates)}/{len(merged)}"
                    )
                v = tds.validate_daily_frame(merged)
                if not v.ok:
                    raise RuntimeError(
                        f"publish-time validation failed for {ts_code}: {v.reasons}"
                    )
                arrays = tds.transform_to_bar_arrays(merged)
                blob_sha = store.store_bar_arrays(symbol, arrays)
                content_hash = hashlib.sha256(
                    arrays["trade_date"].tobytes()
                    + arrays["close"].tobytes()
                    + arrays["volume"].tobytes()
                ).hexdigest()
                records.append(
                    SymbolRecord(
                        symbol=symbol, blob_sha256=blob_sha,
                        first_date=v.first_date, last_date=v.last_date,
                        row_count=v.row_count, quality="ok",
                    )
                )
                total_rows += v.row_count
                provenance_symbols[symbol] = {
                    "ts_code": ts_code,
                    "requested_start": entry.get("requested_start"),
                    "requested_end": entry.get("requested_end"),
                    "actual_first_date": v.first_date,
                    "actual_last_date": v.last_date,
                    "bar_count": v.row_count,
                    "download_status": tds.STATUS_PUBLISHED,
                    "retry_count": entry.get("retry_count", 0),
                    "no_data_reason": "",
                    "source": "tushare",
                    "api_name": tds.API_NAME,
                    "unit_transform": tds.UNIT_TRANSFORM_VERSION,
                    "content_hash": content_hash,
                    "pages": entry.get("pages"),
                    "ohlc_repaired_rows": len(repaired_dates),
                    "ohlc_repaired_dates": repaired_dates[:20],
                    "ohlc_repair_rule": (
                        tds.OHLC_REPAIR_RULE_VERSION if repaired_dates else ""
                    ),
                }

            allowlist = {}
            if args.allow_no_data_file and Path(args.allow_no_data_file).exists():
                allowlist = json.loads(
                    Path(args.allow_no_data_file).read_text(encoding="utf-8")
                )
            policy = evaluate_strict_publish(
                records,
                expected_symbol_count=len(cand_list),
                no_data_allowlist=allowlist,
                max_allow_count=len(allowlist),
            )
            sync_run_id = lock.sync_run_id
            canonical_pre = json.dumps(
                {
                    "source": "tushare_delisted", "adjustment": "none",
                    "period": "1d", "cutoff": cutoff,
                    "candidates_sha": hashlib.sha256(
                        candidates_csv.read_bytes()
                    ).hexdigest(),
                    "symbols": sorted(r.symbol for r in records),
                },
                sort_keys=True,
            )
            pre_sha = hashlib.sha256(canonical_pre.encode()).hexdigest()
            dataset_id = make_dataset_id(
                "tushare_delisted", "none", "1d", str(cutoff), pre_sha
            )
            if store.load_manifest(dataset_id) is not None:
                print(f"dataset already published: {dataset_id} (idempotent no-op)")
                result["published"] = True
                result["dataset_id"] = dataset_id
            else:
                manifest = DatasetManifest(
                    dataset_id=dataset_id,
                    source="tushare",
                    adjustment="none",
                    period="1d",
                    data_cutoff_date=cutoff,
                    snapshot_date=int(time.strftime("%Y%m%d")),
                    provider_version="tushare_pro_daily",
                    sync_run_id=sync_run_id,
                    status="building",
                    created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                    dataset_type="bars",
                    universe_type="b1_delisted_supplement",
                    universe_definition_version="gate_b1_candidates_v1",
                    survivorship_bias=False,
                    historical_universe_complete=False,
                    delisted_coverage_complete=None,
                    warning_text=(
                        "Delisted-stock supplement for the B1 reference universe. "
                        "Covers ONLY stocks missing from local_vendor; combine with "
                        "the local_vendor dataset (B3 composite) for full coverage."
                    ),
                    recommended_use=[
                        "B3 composite_none parent",
                        "delisted-stock research",
                    ],
                    prohibited_or_discouraged_use=[
                        "standalone whole-market backtest",
                    ],
                    provenance={
                        "gate": "B2",
                        "candidates_file": str(candidates_csv),
                        "candidates_sha256": hashlib.sha256(
                            candidates_csv.read_bytes()
                        ).hexdigest(),
                        "unit_transform": tds.UNIT_TRANSFORM_VERSION,
                        "api_name": tds.API_NAME,
                        "symbols": provenance_symbols,
                    },
                    token_exposed=False,
                )
                manifest.symbols = records
                manifest.symbol_count = len(records)
                manifest.row_count = total_rows
                manifest.expected_symbol_count = policy["expected_symbol_count"]
                manifest.imported_symbol_count = policy["imported_symbol_count"]
                manifest.excluded_symbol_count = policy["excluded_symbol_count"]
                manifest.no_data_symbol_count = policy["no_data_symbol_count"]
                manifest.failed_symbol_count = policy["failed_symbol_count"]
                manifest.warning_symbol_count = policy["warning_symbol_count"]
                manifest.coverage_ratio = policy["coverage_ratio"]
                manifest.no_data_allowlist = policy["no_data_allowlist"]
                if policy["target_status"] != "ready":
                    manifest.status = "partial"
                    print(f"STRICT POLICY -> partial: {policy['block_reasons']}")
                store.publish(manifest)
                print(f"published dataset: {dataset_id} status={manifest.status} "
                      f"symbols={len(records)} rows={total_rows}")
                for r in records:
                    ts_c = [k for k, v in provenance_symbols.items() if k == r.symbol]
                    _ = ts_c
                for ts_code in [str(c["ts_code"]) for c in cand_list]:
                    if state["symbols"][ts_code]["status"] == tds.STATUS_VALIDATED:
                        state["symbols"][ts_code]["status"] = tds.STATUS_PUBLISHED
                save_state(state_dir, state)
                store.save_sync_log(
                    sync_run_id,
                    {
                        "sync_run_id": sync_run_id,
                        "gate": "B2",
                        "dataset_id": dataset_id,
                        "status": manifest.status,
                        "counts": counts,
                        "rows": total_rows,
                    },
                )
                result["published"] = True
                result["dataset_id"] = dataset_id
                result["dataset_status"] = manifest.status

        if args.report_path:
            atomic_write_json(Path(args.report_path), result)
        print(json.dumps({k: v for k, v in result.items() if k != "counts_detail"},
                         ensure_ascii=False))
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
