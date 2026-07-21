"""Run history index and artifact readers."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import AStockConfig, get_default_config


def _index_path(cfg: AStockConfig) -> Path:
    return Path(cfg.output_root) / "runs_index.json"


def append_run_index(cfg: AStockConfig, row: Dict[str, Any]) -> None:
    path = _index_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: List[dict] = []
    if path.exists():
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                rows = []
        except Exception:
            rows = []
    # de-dup by run_id
    rows = [r for r in rows if r.get("run_id") != row.get("run_id")]
    if "created_at" not in row:
        row = dict(row)
        row["created_at"] = int(time.time())
    rows.insert(0, row)
    rows = rows[:200]
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _metrics_brief(metrics: Optional[dict]) -> Optional[dict]:
    if not isinstance(metrics, dict) or not metrics:
        return None
    keys = (
        "total_return",
        "mean_symbol_return",
        "median_symbol_return",
        "annual_return",
        "max_drawdown",
        "final_equity",
        "n_buys",
        "n_sells",
        "n_round_trips",
        "win_rate",
        "n_days",
        "account_mode",
        "n_symbol_accounts",
        "capital_base",
        "pct_symbols_profitable",
        "sharpe",
        "profit_factor",
        "payoff_ratio",
        "profit_loss_ratio",
        "avg_win",
        "avg_loss",
        "cost_total",
    )
    return {k: metrics[k] for k in keys if k in metrics}


def _enrich_row(row: dict, out_dir: Optional[Path] = None) -> dict:
    """Normalize a history row for UI consumption."""
    r = dict(row or {})
    rid = r.get("run_id")
    metrics = r.get("metrics")
    if (not isinstance(metrics, dict) or not metrics) and out_dir is not None:
        for name in ("metrics.json", "run_meta.json", "meta.json"):
            p = out_dir / name
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if name == "metrics.json" and isinstance(data, dict):
                metrics = data
                break
            if isinstance(data.get("metrics"), dict):
                metrics = data["metrics"]
                if not r.get("status"):
                    r["status"] = data.get("status")
                if not r.get("indicator_ids"):
                    r["indicator_ids"] = data.get("indicator_ids") or (
                        (data.get("repro") or {}).get("indicator_ids")
                    )
                break
    if out_dir is not None and out_dir.exists():
        try:
            mtime = int(out_dir.stat().st_mtime)
        except Exception:
            mtime = None
        if not r.get("created_at") and mtime:
            r["created_at"] = mtime
        # fill missing fields from run_meta
        meta_p = out_dir / "run_meta.json"
        if meta_p.exists():
            try:
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
                r.setdefault("status", meta.get("status") or "ok")
                if not r.get("indicator_ids"):
                    r["indicator_ids"] = meta.get("indicator_ids") or (
                        (meta.get("repro") or {}).get("indicator_ids")
                    )
                r.setdefault("hold", meta.get("hold") or (meta.get("repro") or {}).get("hold"))
                r.setdefault(
                    "entry_lag",
                    meta.get("entry_lag") or (meta.get("repro") or {}).get("entry_lag"),
                )
                _repro = meta.get("repro") or {}
                _req = _repro.get("request") if isinstance(_repro.get("request"), dict) else {}
                if not isinstance(_req, dict):
                    _req = {}
                for _k in (
                    "buy_weekday",
                    "exit_weekday",
                    "buy_on",
                    "sell_on",
                    "signal_weekdays",
                ):
                    if r.get(_k) in (None, "", []):
                        val = meta.get(_k)
                        if val in (None, "", []):
                            val = _repro.get(_k)
                        if val in (None, "", []):
                            val = _req.get(_k)
                        if val not in (None, "", []):
                            r[_k] = val
                r.setdefault(
                    "period", meta.get("period") or (meta.get("repro") or {}).get("period")
                )
                r.setdefault("start", meta.get("start") or (meta.get("repro") or {}).get("start"))
                r.setdefault("end", meta.get("end") or (meta.get("repro") or {}).get("end"))
                r.setdefault("title", meta.get("title") or (meta.get("repro") or {}).get("title"))
                r.setdefault("indicator_names", meta.get("indicator_names") or (meta.get("repro") or {}).get("indicator_names"))
                if r.get("gua_filter") is None:
                    gf = meta.get("gua_filter")
                    if gf is None:
                        gf = (meta.get("repro") or {}).get("gua_filter")
                    if gf is not None:
                        r["gua_filter"] = gf
                r.setdefault("with_bagua", meta.get("with_bagua") if meta.get("with_bagua") is not None else (meta.get("repro") or {}).get("with_bagua"))
                r.setdefault("bagua_filter_label", meta.get("bagua_filter_label") or (meta.get("repro") or {}).get("bagua_filter_label"))
                # Rebuild title if meta has better title including 卦象
                mt = meta.get("title") or (meta.get("repro") or {}).get("title")
                if mt and (not r.get("title") or (r.get("gua_filter") or {}).get("enabled") and "卦象" not in str(r.get("title") or "") and "八卦" not in str(r.get("title") or "")):
                    r["title"] = mt
                if not metrics and isinstance(meta.get("metrics"), dict):
                    metrics = meta["metrics"]
            except Exception:
                pass
    r["metrics"] = metrics if isinstance(metrics, dict) else None
    r["metrics_brief"] = _metrics_brief(r.get("metrics"))

    # human title for UI
    if not r.get("title"):
        names = r.get("indicator_names") or r.get("indicator_ids") or []
        if isinstance(names, str):
            names = [names]
        # strip technical prefixes for display
        nice = []
        for n in names:
            s = str(n)
            for pref in ("tn6_", "txt_", "user_"):
                if s.startswith(pref):
                    s = s[len(pref):]
            nice.append(s)
        pl = r.get("period_label") or {
            "DAY": "日线", "WEEK": "周线", "MONTH": "月线",
            "DWM": "日周月", "MIN60": "60分钟",
        }.get(str(r.get("period") or "").upper(), r.get("period") or "")
        hold = r.get("hold")
        title = "、".join(nice) if nice else "回测任务"
        if pl:
            title += f" · {pl}"
        if hold is not None:
            title += f" · 持有{hold}"
        if r.get("start") or r.get("end"):
            title += f" · {r.get('start') or ''}~{r.get('end') or ''}"
        r["title"] = title
    if not r.get("period_label") and r.get("period"):
        r["period_label"] = {
            "DAY": "日线", "WEEK": "周线", "MONTH": "月线",
            "DWM": "日周月", "MIN60": "60分钟",
        }.get(str(r.get("period")).upper(), r.get("period"))
    status_map = {
        "ok": "完成",
        "research_unadjusted": "完成(未复权研究)",
        "research_unconfirmed_formula": "完成(公式未确认)",
        "no_go": "未通过",
        "failed": "失败",
        "succeeded": "完成",
    }
    r["status_label"] = status_map.get(str(r.get("status") or ""), str(r.get("status") or ""))

    r["run_id"] = rid
    return r


def list_runs(cfg: Optional[AStockConfig] = None, *, limit: int = 50) -> List[Dict[str, Any]]:
    cfg = cfg or get_default_config()
    path = _index_path(cfg)
    rows: List[dict] = []
    if path.exists():
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                rows = []
        except Exception:
            rows = []

    out = Path(cfg.output_root)
    seen = {r.get("run_id") for r in rows if r.get("run_id")}

    # Enrich index rows with filesystem meta/metrics
    enriched: List[dict] = []
    for row in rows:
        rid = row.get("run_id")
        out_dir = (out / rid) if rid and out.exists() else None
        if out_dir is not None and not out_dir.is_dir():
            out_dir = None
        enriched.append(_enrich_row(row, out_dir))

    # Scan output dirs not in index
    if out.exists():
        for d in sorted(out.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            rid = d.name
            if rid in seen:
                continue
            # skip non-run folders
            if not any(
                (d / n).exists()
                for n in ("run_meta.json", "meta.json", "metrics.json", "fills.csv")
            ):
                continue
            try:
                mtime = int(d.stat().st_mtime)
            except Exception:
                mtime = None
            base = {"run_id": rid, "created_at": mtime}
            enriched.append(_enrich_row(base, d))
            seen.add(rid)
            if len(enriched) >= limit * 3:
                break

    # sort by created_at desc when available
    def _key(r: dict):
        return int(r.get("created_at") or 0)

    enriched.sort(key=_key, reverse=True)
    return enriched[:limit]


def load_run_summary(cfg: AStockConfig, run_id: str) -> Dict[str, Any]:
    out_dir = Path(cfg.output_root) / run_id
    if not out_dir.exists():
        raise FileNotFoundError(run_id)
    meta = {}
    for name in ("run_meta.json", "meta.json"):
        p = out_dir / name
        if p.exists():
            meta = json.loads(p.read_text(encoding="utf-8"))
            break
    metrics = meta.get("metrics") if isinstance(meta.get("metrics"), dict) else {}
    repro = meta.get("repro") if isinstance(meta.get("repro"), dict) else {}
    result = {
        "run_id": run_id,
        "status": meta.get("status", "ok"),
        "metrics": metrics,
        "meta": meta,
        "artifacts": sorted([p.name for p in out_dir.iterdir() if p.is_file()]),
        "title": meta.get("title") or repro.get("title"),
        "indicator_ids": meta.get("indicator_ids") or repro.get("indicator_ids"),
        "indicator_names": meta.get("indicator_names") or repro.get("indicator_names"),
        "gua_filter": meta.get("gua_filter") if meta.get("gua_filter") is not None else repro.get("gua_filter"),
        "with_bagua": meta.get("with_bagua") if meta.get("with_bagua") is not None else repro.get("with_bagua"),
        "bagua_filter_label": meta.get("bagua_filter_label") or repro.get("bagua_filter_label"),
        "n_signals_before_bagua": meta.get("n_signals_before_bagua")
        if meta.get("n_signals_before_bagua") is not None
        else repro.get("n_signals_before_bagua"),
        "n_signals_after_bagua": meta.get("n_signals_after_bagua")
        if meta.get("n_signals_after_bagua") is not None
        else repro.get("n_signals_after_bagua"),
        "hold": meta.get("hold") if meta.get("hold") is not None else repro.get("hold"),
        "entry_lag": meta.get("entry_lag") if meta.get("entry_lag") is not None else repro.get("entry_lag"),
        "period": meta.get("period") or repro.get("period"),
        "start": meta.get("start") if meta.get("start") is not None else repro.get("start"),
        "end": meta.get("end") if meta.get("end") is not None else repro.get("end"),
        "buy_weekday": meta.get("buy_weekday") if meta.get("buy_weekday") is not None else repro.get("buy_weekday"),
        "exit_weekday": meta.get("exit_weekday") if meta.get("exit_weekday") is not None else repro.get("exit_weekday"),
        "buy_on": meta.get("buy_on") or repro.get("buy_on"),
        "sell_on": meta.get("sell_on") or repro.get("sell_on"),
        "signal_weekdays": meta.get("signal_weekdays") if meta.get("signal_weekdays") is not None else repro.get("signal_weekdays"),
        "account_mode": meta.get("account_mode") or repro.get("account_mode"),
        "repro": repro,
    }
    # Prefer title from runs_index when meta lacks a human title
    if not result.get("title"):
        try:
            for row in list_runs(cfg, limit=200):
                if row.get("run_id") == run_id and row.get("title"):
                    result["title"] = row.get("title")
                    if not result.get("indicator_names") and row.get("indicator_names"):
                        result["indicator_names"] = row.get("indicator_names")
                    if result.get("gua_filter") is None and row.get("gua_filter") is not None:
                        result["gua_filter"] = row.get("gua_filter")
                    break
        except Exception:
            pass
    mp = out_dir / "metrics.json"
    if mp.exists():
        try:
            result["metrics"] = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            pass
    return result


def read_artifact(cfg: AStockConfig, run_id: str, name: str) -> Path:
    out_dir = Path(cfg.output_root) / run_id
    safe = Path(name).name
    path = out_dir / safe
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(safe)
    return path

def delete_run(
    cfg: AStockConfig, run_id: str, *, remove_files: bool = True
) -> Dict[str, Any]:
    """Remove a backtest run from history index and optionally delete output folder."""
    import shutil

    run_id = str(run_id or "").strip()
    if not run_id or run_id in {".", ".."} or "/" in run_id or chr(92) in run_id:
        raise ValueError("invalid run_id")
    # use Path.name to block path segments
    if Path(run_id).name != run_id:
        raise ValueError("invalid run_id")

    out_dir = Path(cfg.output_root) / run_id
    removed_files = False
    if remove_files and out_dir.exists() and out_dir.is_dir():
        out_root = Path(cfg.output_root).resolve()
        target = out_dir.resolve()
        if target != out_root and out_root not in target.parents:
            raise ValueError("run path outside output_root")
        shutil.rmtree(target)
        removed_files = True

    path = _index_path(cfg)
    kept = 0
    if path.exists():
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                rows = []
        except Exception:
            rows = []
        new_rows = [r for r in rows if r.get("run_id") != run_id]
        kept = len(new_rows)
        path.write_text(
            json.dumps(new_rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return {
        "run_id": run_id,
        "deleted": True,
        "removed_files": removed_files,
        "index_remaining": kept,
    }
