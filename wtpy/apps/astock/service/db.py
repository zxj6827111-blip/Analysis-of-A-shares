# -*- coding: utf-8 -*-
"""SQLite experiment / run registry for A-stock backtests (Stage D).

Dual-write design:
- Directory artifacts remain source of truth for large files (fills, equity, xlsx).
- SQLite stores searchable run rows, parameters, metrics summaries, artifact names.
- Legacy ``runs_index.json`` is still written and used as migration source.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..config import AStockConfig, get_default_config

_SCHEMA_VERSION = 2

_LOCK = threading.RLock()

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  title TEXT,
  status TEXT,
  created_at INTEGER,
  period TEXT,
  period_label TEXT,
  account_mode TEXT,
  start INTEGER,
  end INTEGER,
  hold INTEGER,
  entry_lag INTEGER,
  buy_weekday INTEGER,
  exit_weekday INTEGER,
  buy_on TEXT,
  sell_on TEXT,
  signal_weekdays_json TEXT,
  schedule_mode TEXT,
  with_bagua INTEGER,
  gua_filter_json TEXT,
  indicator_ids_json TEXT,
  indicator_names_json TEXT,
  param_hash TEXT,
  experiment_id TEXT,
  variant_id TEXT,
  code_version TEXT,
  bagua_rule_version TEXT,
  selected_codes_count INTEGER,
  n_signals_before_bagua INTEGER,
  n_signals_after_bagua INTEGER,
  error TEXT,
  extra_json TEXT,
  signal_data_source TEXT,
  signal_adjustment TEXT,
  dataset_id TEXT,
  weekly_bar_mode TEXT,
  execution_data_source TEXT,
  execution_dataset_id TEXT
);

CREATE TABLE IF NOT EXISTS parameters (
  run_id TEXT PRIMARY KEY,
  params_json TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS metrics (
  run_id TEXT PRIMARY KEY,
  metrics_json TEXT,
  total_return REAL,
  mean_symbol_return REAL,
  annual_return REAL,
  max_drawdown REAL,
  sharpe REAL,
  win_rate REAL,
  payoff_ratio REAL,
  profit_factor REAL,
  n_round_trips REAL,
  n_buys REAL,
  n_sells REAL,
  final_equity REAL,
  cost_total REAL,
  FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS artifacts (
  run_id TEXT NOT NULL,
  name TEXT NOT NULL,
  rel_path TEXT,
  PRIMARY KEY(run_id, name),
  FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS experiments (
  experiment_id TEXT PRIMARY KEY,
  name TEXT,
  status TEXT,
  created_at INTEGER,
  updated_at INTEGER,
  config_json TEXT,
  max_variants INTEGER,
  concurrency INTEGER,
  note TEXT,
  estimated_variants INTEGER,
  completed_variants INTEGER DEFAULT 0,
  failed_variants INTEGER DEFAULT 0,
  skipped_variants INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS experiment_variants (
  variant_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  param_hash TEXT,
  params_json TEXT NOT NULL,
  status TEXT,
  run_id TEXT,
  error TEXT,
  created_at INTEGER,
  updated_at INTEGER,
  FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_param_hash ON runs(param_hash);
CREATE INDEX IF NOT EXISTS idx_variants_exp ON experiment_variants(experiment_id, status);
"""


def db_path(cfg: Optional[AStockConfig] = None) -> Path:
    cfg = cfg or get_default_config()
    root = Path(cfg.output_root)
    root.mkdir(parents=True, exist_ok=True)
    return root / "astock_experiments.sqlite3"


def connect(cfg: Optional[AStockConfig] = None) -> sqlite3.Connection:
    path = db_path(cfg)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(cfg: Optional[AStockConfig] = None) -> Path:
    """Create schema if needed. Thread-safe."""
    with _LOCK:
        path = db_path(cfg)
        conn = connect(cfg)
        try:
            conn.executescript(SCHEMA_SQL)
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
            else:
                current = int(row[0])
                if current < 2:
                    _migrate_v1_to_v2(conn)
                    conn.execute(
                        "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                        (str(_SCHEMA_VERSION),),
                    )
            conn.commit()
        finally:
            conn.close()
        return path


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add multi-source market data columns to runs table."""
    _new_cols = [
        ("signal_data_source", "TEXT"),
        ("signal_adjustment", "TEXT"),
        ("dataset_id", "TEXT"),
        ("weekly_bar_mode", "TEXT"),
        ("execution_data_source", "TEXT"),
        ("execution_dataset_id", "TEXT"),
    ]
    existing = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    for col_name, col_type in _new_cols:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col_name} {col_type}")
    conn.execute(
        "UPDATE runs SET signal_data_source='legacy_tdx_local_asof' "
        "WHERE signal_data_source IS NULL"
    )
    conn.execute(
        "UPDATE runs SET signal_adjustment='asof_qfq' WHERE signal_adjustment IS NULL"
    )
    conn.execute(
        "UPDATE runs SET weekly_bar_mode='local_aggregate' WHERE weekly_bar_mode IS NULL"
    )
    conn.execute(
        "UPDATE runs SET execution_data_source='tdx_local' WHERE execution_data_source IS NULL"
    )


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _json_loads(s: Any, default=None):
    if s is None or s == "":
        return default
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except Exception:
        return default


def param_hash(params: Dict[str, Any]) -> str:
    """Stable hash for experiment de-dup (request parameters only).

    Prefer :func:`research_param_hash` when code/data version must invalidate
    cached results (phase-1 full research fingerprint).
    """
    def _norm(v):
        if isinstance(v, dict):
            return {str(k): _norm(v[k]) for k in sorted(v.keys(), key=str)}
        if isinstance(v, (list, tuple)):
            return [_norm(x) for x in v]
        return v

    blob = _json_dumps(_norm(params or {}))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def research_param_hash(
    params: Dict[str, Any],
    *,
    costs: Optional[Dict[str, Any]] = None,
    include_engine: bool = True,
) -> str:
    """16-char research fingerprint (params + optional engine code hash).

    Drop-in style companion to :func:`param_hash` for trials that must not
    reuse results after strategy/calendar/limit-rule code changes.
    """
    try:
        from ..research.fingerprint import research_fingerprint_from_params

        fp = research_fingerprint_from_params(
            params,
            costs=costs,
            engine_code_hash=None if include_engine else "omit",
        )
        if not include_engine:
            fp.execution["engine_code_hash"] = "omit"
        return fp.as_param_hash_compat()
    except Exception:
        return param_hash(params)


def _pick(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def upsert_run_from_index_row(
    cfg: AStockConfig,
    row: Dict[str, Any],
    *,
    out_dir: Optional[Path] = None,
) -> None:
    """Insert/update a run from history index row or backtest append payload."""
    init_db(cfg)
    rid = str(row.get("run_id") or "").strip()
    if not rid:
        return
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    gf = row.get("gua_filter")
    if gf is not None and not isinstance(gf, dict):
        gf = None
    params = {
        "indicator_ids": row.get("indicator_ids"),
        "indicator_names": row.get("indicator_names"),
        "period": row.get("period"),
        "hold": row.get("hold"),
        "entry_lag": row.get("entry_lag"),
        "buy_weekday": row.get("buy_weekday"),
        "exit_weekday": row.get("exit_weekday"),
        "buy_on": row.get("buy_on"),
        "sell_on": row.get("sell_on"),
        "signal_weekdays": row.get("signal_weekdays"),
        "schedule_mode": row.get("schedule_mode"),
        "account_mode": row.get("account_mode"),
        "start": row.get("start"),
        "end": row.get("end"),
        "with_bagua": row.get("with_bagua"),
        "gua_filter": gf,
        "stop_loss": row.get("stop_loss") or row.get("stop_loss_pct"),
        "take_profit": row.get("take_profit") or row.get("take_profit_pct"),
    }
    ph = row.get("param_hash") or param_hash(params)
    created = int(row.get("created_at") or time.time())
    artifacts: List[str] = []
    if out_dir is None:
        out_dir = Path(cfg.output_root) / rid
    if out_dir.exists():
        try:
            artifacts = sorted(p.name for p in out_dir.iterdir() if p.is_file())
        except Exception:
            artifacts = []

    with _LOCK:
        conn = connect(cfg)
        try:
            conn.execute(
                """
                INSERT INTO runs(
                  run_id, title, status, created_at, period, period_label, account_mode,
                  start, end, hold, entry_lag, buy_weekday, exit_weekday, buy_on, sell_on,
                  signal_weekdays_json, schedule_mode, with_bagua, gua_filter_json,
                  indicator_ids_json, indicator_names_json, param_hash,
                  experiment_id, variant_id, code_version, bagua_rule_version,
                  selected_codes_count, n_signals_before_bagua, n_signals_after_bagua,
                  error, extra_json,
                  signal_data_source, signal_adjustment, dataset_id,
                  weekly_bar_mode, execution_data_source, execution_dataset_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                  title=excluded.title,
                  status=excluded.status,
                  period=excluded.period,
                  period_label=excluded.period_label,
                  account_mode=excluded.account_mode,
                  start=excluded.start,
                  end=excluded.end,
                  hold=excluded.hold,
                  entry_lag=excluded.entry_lag,
                  buy_weekday=excluded.buy_weekday,
                  exit_weekday=excluded.exit_weekday,
                  buy_on=excluded.buy_on,
                  sell_on=excluded.sell_on,
                  signal_weekdays_json=excluded.signal_weekdays_json,
                  schedule_mode=excluded.schedule_mode,
                  with_bagua=excluded.with_bagua,
                  gua_filter_json=excluded.gua_filter_json,
                  indicator_ids_json=excluded.indicator_ids_json,
                  indicator_names_json=excluded.indicator_names_json,
                  param_hash=excluded.param_hash,
                  experiment_id=COALESCE(excluded.experiment_id, runs.experiment_id),
                  variant_id=COALESCE(excluded.variant_id, runs.variant_id),
                  n_signals_before_bagua=excluded.n_signals_before_bagua,
                  n_signals_after_bagua=excluded.n_signals_after_bagua,
                  error=excluded.error,
                  extra_json=excluded.extra_json,
                  signal_data_source=COALESCE(excluded.signal_data_source, runs.signal_data_source),
                  signal_adjustment=COALESCE(excluded.signal_adjustment, runs.signal_adjustment),
                  dataset_id=COALESCE(excluded.dataset_id, runs.dataset_id),
                  weekly_bar_mode=COALESCE(excluded.weekly_bar_mode, runs.weekly_bar_mode),
                  execution_data_source=COALESCE(excluded.execution_data_source, runs.execution_data_source),
                  execution_dataset_id=COALESCE(excluded.execution_dataset_id, runs.execution_dataset_id)
                """,
                (
                    rid,
                    row.get("title"),
                    row.get("status") or "ok",
                    created,
                    row.get("period"),
                    row.get("period_label"),
                    row.get("account_mode"),
                    row.get("start"),
                    row.get("end"),
                    row.get("hold"),
                    row.get("entry_lag"),
                    row.get("buy_weekday"),
                    row.get("exit_weekday"),
                    row.get("buy_on"),
                    row.get("sell_on"),
                    _json_dumps(row.get("signal_weekdays")),
                    row.get("schedule_mode"),
                    1 if row.get("with_bagua") else 0,
                    _json_dumps(gf) if gf is not None else None,
                    _json_dumps(row.get("indicator_ids")),
                    _json_dumps(row.get("indicator_names")),
                    ph,
                    row.get("experiment_id"),
                    row.get("variant_id"),
                    row.get("code_version") or row.get("astock_code_sha"),
                    (gf or {}).get("rule_version") if isinstance(gf, dict) else None,
                    row.get("selected_codes_count"),
                    row.get("n_signals_before_bagua"),
                    row.get("n_signals_after_bagua"),
                    row.get("error"),
                    _json_dumps({k: row.get(k) for k in (
                        "bagua_filter_label",
                        "research_fingerprint",
                        "signal_fp",
                        "filter_fp",
                        "execution_fp",
                    ) if k in row and row.get(k) is not None}),
                    row.get("signal_data_source"),
                    row.get("signal_adjustment"),
                    row.get("dataset_id"),
                    row.get("weekly_bar_mode"),
                    row.get("execution_data_source"),
                    row.get("execution_dataset_id"),
                ),
            )
            conn.execute(
                """
                INSERT INTO parameters(run_id, params_json) VALUES(?,?)
                ON CONFLICT(run_id) DO UPDATE SET params_json=excluded.params_json
                """,
                (rid, _json_dumps(params)),
            )
            if metrics:
                conn.execute(
                    """
                    INSERT INTO metrics(
                      run_id, metrics_json, total_return, mean_symbol_return, annual_return,
                      max_drawdown, sharpe, win_rate, payoff_ratio, profit_factor,
                      n_round_trips, n_buys, n_sells, final_equity, cost_total
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(run_id) DO UPDATE SET
                      metrics_json=excluded.metrics_json,
                      total_return=excluded.total_return,
                      mean_symbol_return=excluded.mean_symbol_return,
                      annual_return=excluded.annual_return,
                      max_drawdown=excluded.max_drawdown,
                      sharpe=excluded.sharpe,
                      win_rate=excluded.win_rate,
                      payoff_ratio=excluded.payoff_ratio,
                      profit_factor=excluded.profit_factor,
                      n_round_trips=excluded.n_round_trips,
                      n_buys=excluded.n_buys,
                      n_sells=excluded.n_sells,
                      final_equity=excluded.final_equity,
                      cost_total=excluded.cost_total
                    """,
                    (
                        rid,
                        _json_dumps(metrics),
                        metrics.get("total_return"),
                        metrics.get("mean_symbol_return"),
                        metrics.get("annual_return"),
                        metrics.get("max_drawdown"),
                        metrics.get("sharpe"),
                        metrics.get("win_rate"),
                        metrics.get("payoff_ratio") or metrics.get("profit_loss_ratio"),
                        metrics.get("profit_factor"),
                        metrics.get("n_round_trips"),
                        metrics.get("n_buys"),
                        metrics.get("n_sells"),
                        metrics.get("final_equity"),
                        metrics.get("cost_total"),
                    ),
                )
            for name in artifacts:
                conn.execute(
                    """
                    INSERT INTO artifacts(run_id, name, rel_path) VALUES(?,?,?)
                    ON CONFLICT(run_id, name) DO UPDATE SET rel_path=excluded.rel_path
                    """,
                    (rid, name, f"{rid}/{name}"),
                )
            conn.commit()
        finally:
            conn.close()


def delete_run_db(cfg: AStockConfig, run_id: str) -> None:
    init_db(cfg)
    with _LOCK:
        conn = connect(cfg)
        try:
            conn.execute("DELETE FROM artifacts WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM metrics WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM parameters WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
            conn.commit()
        finally:
            conn.close()


def _row_to_history(r: sqlite3.Row) -> dict:
    d = dict(r)
    metrics = _json_loads(d.pop("metrics_json", None), default=None) or {}
    # fill metrics from columns if json empty
    if not metrics:
        for k in (
            "total_return",
            "mean_symbol_return",
            "annual_return",
            "max_drawdown",
            "sharpe",
            "win_rate",
            "payoff_ratio",
            "profit_factor",
            "n_round_trips",
            "n_buys",
            "n_sells",
            "final_equity",
            "cost_total",
        ):
            if d.get(k) is not None:
                metrics[k] = d.get(k)
    out = {
        "run_id": d.get("run_id"),
        "title": d.get("title"),
        "status": d.get("status"),
        "created_at": d.get("created_at"),
        "period": d.get("period"),
        "period_label": d.get("period_label"),
        "account_mode": d.get("account_mode"),
        "start": d.get("start"),
        "end": d.get("end"),
        "hold": d.get("hold"),
        "entry_lag": d.get("entry_lag"),
        "buy_weekday": d.get("buy_weekday"),
        "exit_weekday": d.get("exit_weekday"),
        "buy_on": d.get("buy_on"),
        "sell_on": d.get("sell_on"),
        "signal_weekdays": _json_loads(d.get("signal_weekdays_json"), default=[]),
        "schedule_mode": d.get("schedule_mode"),
        "with_bagua": bool(d.get("with_bagua")),
        "gua_filter": _json_loads(d.get("gua_filter_json"), default=None),
        "indicator_ids": _json_loads(d.get("indicator_ids_json"), default=[]),
        "indicator_names": _json_loads(d.get("indicator_names_json"), default=[]),
        "param_hash": d.get("param_hash"),
        "experiment_id": d.get("experiment_id"),
        "variant_id": d.get("variant_id"),
        "n_signals_before_bagua": d.get("n_signals_before_bagua"),
        "n_signals_after_bagua": d.get("n_signals_after_bagua"),
        "selected_codes_count": d.get("selected_codes_count"),
        "error": d.get("error"),
        "metrics": metrics if metrics else None,
        "signal_data_source": d.get("signal_data_source"),
        "signal_adjustment": d.get("signal_adjustment"),
        "dataset_id": d.get("dataset_id"),
        "weekly_bar_mode": d.get("weekly_bar_mode"),
        "execution_data_source": d.get("execution_data_source"),
        "execution_dataset_id": d.get("execution_dataset_id"),
        "source": "sqlite",
    }
    return out


def list_runs_db(cfg: AStockConfig, *, limit: int = 50) -> List[dict]:
    init_db(cfg)
    limit = max(1, min(500, int(limit or 50)))
    with _LOCK:
        conn = connect(cfg)
        try:
            rows = conn.execute(
                """
                SELECT r.*, m.metrics_json, m.total_return, m.mean_symbol_return,
                       m.annual_return, m.max_drawdown, m.sharpe, m.win_rate,
                       m.payoff_ratio, m.profit_factor, m.n_round_trips, m.n_buys,
                       m.n_sells, m.final_equity, m.cost_total
                FROM runs r
                LEFT JOIN metrics m ON m.run_id = r.run_id
                ORDER BY COALESCE(r.created_at, 0) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [_row_to_history(r) for r in rows]
        finally:
            conn.close()


def count_runs_db(cfg: AStockConfig) -> int:
    init_db(cfg)
    with _LOCK:
        conn = connect(cfg)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        finally:
            conn.close()


def migrate_runs_index_to_sqlite(cfg: AStockConfig) -> Dict[str, Any]:
    """One-shot / idempotent import of runs_index.json into SQLite."""
    init_db(cfg)
    index_path = Path(cfg.output_root) / "runs_index.json"
    imported = 0
    skipped = 0
    if not index_path.exists():
        return {"imported": 0, "skipped": 0, "path": str(index_path), "ok": True}
    try:
        rows = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            rows = []
    except Exception as e:
        return {"imported": 0, "skipped": 0, "error": str(e), "ok": False}
    for row in rows:
        rid = (row or {}).get("run_id")
        if not rid:
            skipped += 1
            continue
        try:
            upsert_run_from_index_row(cfg, row, out_dir=Path(cfg.output_root) / rid)
            imported += 1
        except Exception:
            skipped += 1
    return {
        "imported": imported,
        "skipped": skipped,
        "path": str(index_path),
        "db": str(db_path(cfg)),
        "ok": True,
    }


# ----- experiments -----


def create_experiment(
    cfg: AStockConfig,
    *,
    name: str,
    config: Dict[str, Any],
    variants: Sequence[Dict[str, Any]],
    max_variants: int = 50,
    concurrency: int = 1,
    note: str = "",
) -> Dict[str, Any]:
    init_db(cfg)
    import uuid

    exp_id = f"exp_{uuid.uuid4().hex[:10]}"
    now = int(time.time())
    variants = list(variants or [])
    with _LOCK:
        conn = connect(cfg)
        try:
            conn.execute(
                """
                INSERT INTO experiments(
                  experiment_id, name, status, created_at, updated_at, config_json,
                  max_variants, concurrency, note, estimated_variants,
                  completed_variants, failed_variants, skipped_variants
                ) VALUES (?,?,?,?,?,?,?,?,?,?,0,0,0)
                """,
                (
                    exp_id,
                    name or exp_id,
                    "draft",
                    now,
                    now,
                    _json_dumps(config or {}),
                    int(max_variants),
                    max(1, int(concurrency or 1)),
                    note or "",
                    len(variants),
                ),
            )
            for i, params in enumerate(variants):
                vid = f"{exp_id}_v{i:03d}"
                ph = param_hash(params)
                conn.execute(
                    """
                    INSERT INTO experiment_variants(
                      variant_id, experiment_id, param_hash, params_json, status,
                      run_id, error, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        vid,
                        exp_id,
                        ph,
                        _json_dumps(params),
                        "pending",
                        None,
                        None,
                        now,
                        now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    return get_experiment(cfg, exp_id)


def get_experiment(cfg: AStockConfig, experiment_id: str) -> Dict[str, Any]:
    init_db(cfg)
    with _LOCK:
        conn = connect(cfg)
        try:
            exp = conn.execute(
                "SELECT * FROM experiments WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
            if not exp:
                raise FileNotFoundError(experiment_id)
            vars_ = conn.execute(
                """
                SELECT * FROM experiment_variants
                WHERE experiment_id=?
                ORDER BY variant_id
                """,
                (experiment_id,),
            ).fetchall()
            return {
                "experiment_id": exp["experiment_id"],
                "name": exp["name"],
                "status": exp["status"],
                "created_at": exp["created_at"],
                "updated_at": exp["updated_at"],
                "config": _json_loads(exp["config_json"], default={}),
                "max_variants": exp["max_variants"],
                "concurrency": exp["concurrency"],
                "note": exp["note"],
                "estimated_variants": exp["estimated_variants"],
                "completed_variants": exp["completed_variants"],
                "failed_variants": exp["failed_variants"],
                "skipped_variants": exp["skipped_variants"],
                "variants": [
                    {
                        "variant_id": v["variant_id"],
                        "param_hash": v["param_hash"],
                        "params": _json_loads(v["params_json"], default={}),
                        "status": v["status"],
                        "run_id": v["run_id"],
                        "error": v["error"],
                        "created_at": v["created_at"],
                        "updated_at": v["updated_at"],
                    }
                    for v in vars_
                ],
            }
        finally:
            conn.close()


def list_experiments(cfg: AStockConfig, *, limit: int = 50) -> List[dict]:
    init_db(cfg)
    limit = max(1, min(200, int(limit or 50)))
    with _LOCK:
        conn = connect(cfg)
        try:
            rows = conn.execute(
                """
                SELECT experiment_id, name, status, created_at, updated_at,
                       estimated_variants, completed_variants, failed_variants,
                       skipped_variants, concurrency, max_variants, note
                FROM experiments
                ORDER BY COALESCE(created_at, 0) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def update_experiment_status(
    cfg: AStockConfig, experiment_id: str, status: str, **counters
) -> None:
    init_db(cfg)
    now = int(time.time())
    sets = ["status=?", "updated_at=?"]
    args: List[Any] = [status, now]
    for k in (
        "completed_variants",
        "failed_variants",
        "skipped_variants",
        "estimated_variants",
    ):
        if k in counters and counters[k] is not None:
            sets.append(f"{k}=?")
            args.append(counters[k])
    args.append(experiment_id)
    with _LOCK:
        conn = connect(cfg)
        try:
            conn.execute(
                f"UPDATE experiments SET {', '.join(sets)} WHERE experiment_id=?",
                tuple(args),
            )
            conn.commit()
        finally:
            conn.close()


def update_variant(
    cfg: AStockConfig,
    variant_id: str,
    *,
    status: Optional[str] = None,
    run_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    init_db(cfg)
    now = int(time.time())
    with _LOCK:
        conn = connect(cfg)
        try:
            row = conn.execute(
                "SELECT * FROM experiment_variants WHERE variant_id=?",
                (variant_id,),
            ).fetchone()
            if not row:
                return
            conn.execute(
                """
                UPDATE experiment_variants
                SET status=COALESCE(?, status),
                    run_id=COALESCE(?, run_id),
                    error=COALESCE(?, error),
                    updated_at=?
                WHERE variant_id=?
                """,
                (status, run_id, error, now, variant_id),
            )
            conn.commit()
        finally:
            conn.close()


def find_run_id_by_param_hash(cfg: AStockConfig, ph: str) -> Optional[str]:
    if not ph:
        return None
    init_db(cfg)
    with _LOCK:
        conn = connect(cfg)
        try:
            row = conn.execute(
                """
                SELECT run_id FROM runs
                WHERE param_hash=? AND (status IS NULL OR status NOT IN ('failed','no_go'))
                ORDER BY created_at DESC LIMIT 1
                """,
                (ph,),
            ).fetchone()
            return row["run_id"] if row else None
        finally:
            conn.close()


def experiment_results_table(cfg: AStockConfig, experiment_id: str) -> Dict[str, Any]:
    """Side-by-side metrics for all variants (joined to runs)."""
    exp = get_experiment(cfg, experiment_id)
    rows = []
    for v in exp.get("variants") or []:
        rid = v.get("run_id")
        metrics = {}
        title = None
        if rid:
            with _LOCK:
                conn = connect(cfg)
                try:
                    m = conn.execute(
                        "SELECT * FROM metrics WHERE run_id=?", (rid,)
                    ).fetchone()
                    r = conn.execute(
                        "SELECT title, status FROM runs WHERE run_id=?", (rid,)
                    ).fetchone()
                    if m:
                        metrics = _json_loads(m["metrics_json"], default={}) or {
                            "total_return": m["total_return"],
                            "annual_return": m["annual_return"],
                            "max_drawdown": m["max_drawdown"],
                            "win_rate": m["win_rate"],
                            "n_round_trips": m["n_round_trips"],
                            "payoff_ratio": m["payoff_ratio"],
                        }
                    if r:
                        title = r["title"]
                finally:
                    conn.close()
        rows.append(
            {
                "variant_id": v.get("variant_id"),
                "status": v.get("status"),
                "param_hash": v.get("param_hash"),
                "params": v.get("params"),
                "run_id": rid,
                "title": title,
                "error": v.get("error"),
                "metrics": metrics,
            }
        )
    return {
        "experiment_id": experiment_id,
        "name": exp.get("name"),
        "status": exp.get("status"),
        "rows": rows,
        "n": len(rows),
    }
