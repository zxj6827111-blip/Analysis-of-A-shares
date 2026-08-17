# -*- coding: utf-8 -*-
"""DuckDB versioned delta store for the overlay storage mode.

Storage layout:
  <market_data_root>/delta/
  ├── market_delta.duckdb     (DuckDB file, schema below)
  ├── overlay_state.json      (which base datasets the delta overlays)
  ├── pins.json               (dataset pin registry, governance)
  └── .locks/                 (delta write lock, reuse SyncTaskLock)

Why versioned rows instead of "UPDATE in place":
  - bars/factors are appended per (symbol, trade_date, batch_seq); a revision
    of a historical date appends a NEW row with a higher batch_seq instead of
    overwriting. Reading at a given watermark then returns the newest version
    whose batch watermark is <= the requested watermark, which preserves
    backtest reproducibility (the same request always returns the same bars).
  - re-running the same window is idempotent: rows whose visible value is
    identical to the incoming value are skipped (no new batch_seq consumed).

Concurrency model:
  - short connections; a single-writer transaction per batch commit
  - readers open their own short read-only connection per batch query
    (or reuse a Repository-scoped read connection); per-symbol connection
    creation in whole-market loops is forbidden at the call sites
  - the delta write lock serializes writers across processes (server EOD
    child vs manual CLI)

Atomic publish contract (coordinated by the caller):
  1. commit_batch() commits the DB transaction
  2. the caller runs a health check against the committed watermark
  3. only then the overlay_state.json watermark advances (atomic write)
  A batch whose DB commit succeeded but whose overlay publish failed stays
  invisible (watermark not advanced) and is swept by the 72h governance job.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np

from .io_util import atomic_write_json

#: delta dir name under the market data root
DELTA_DIR_NAME = "delta"
#: DuckDB file name under delta dir
DELTA_DB_NAME = "market_delta.duckdb"
#: overlay registry file name under delta dir
OVERLAY_STATE_NAME = "overlay_state.json"
#: pin registry file name under delta dir
PINS_FILE_NAME = "pins.json"

SCHEMA_VERSION = 2

#: bars batch kind
KIND_BARS = "bars"
#: factor batch kind
KIND_FACTOR = "factor"

#: committed batches are the visible truth
BATCH_COMMITTED = "committed"
#: batches whose DB commit succeeded but whose overlay publish never advanced
#: the watermark (invisible; swept after retention)
BATCH_ORPHANED = "orphaned"

_BAR_COLS = ("open", "high", "low", "close", "volume", "amount")
_INSERT_CHUNK_ROWS = 500


class DeltaWriteError(RuntimeError):
    """A delta batch failed to commit; nothing was written."""


class OverlayStateError(RuntimeError):
    """The overlay registry exists but is unreadable or inconsistent."""


class DeltaStore:
    """Versioned incremental store on top of a DuckDB file."""

    def __init__(self, root: Path | str, store_id: str = "main"):
        self.root = Path(root)
        self.store_id = str(store_id or "main")
        self.delta_dir = self.root / DELTA_DIR_NAME
        if self.store_id == "main":
            db_name = DELTA_DB_NAME
        else:
            safe_id = "".join(
                c if c.isalnum() or c in ("-", "_") else "_"
                for c in self.store_id
            )
            db_name = f"market_delta_{safe_id}.duckdb"
        self.db_path = self.delta_dir / db_name
        self.overlay_path = self.delta_dir / OVERLAY_STATE_NAME

    # ------------------------------------------------------------------
    # connection helpers
    # ------------------------------------------------------------------
    def connect(self, *, read_only: bool = False):
        """Open a short DuckDB connection.

        Connections are deliberately never pooled. On Windows, a read-only
        DuckDB connection in the long-running API process can prevent the EOD
        child from opening the same file read-write. Keeping each read scoped
        to one query removes that persistent cross-process lock. Writers retry
        briefly so an in-flight read query can finish.
        """
        import duckdb

        self.delta_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + (30.0 if not read_only else 0.0)
        while True:
            try:
                return duckdb.connect(str(self.db_path), read_only=read_only)
            except Exception:
                if read_only or time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_batches (
                    batch_id        TEXT PRIMARY KEY,
                    store_id        TEXT NOT NULL,
                    commit_seq      BIGINT,
                    kind            TEXT NOT NULL,
                    source          TEXT NOT NULL,
                    adjustment      TEXT NOT NULL,
                    period          TEXT NOT NULL,
                    base_dataset_id TEXT NOT NULL,
                    watermark       INTEGER NOT NULL,
                    row_count       INTEGER NOT NULL,
                    created_at      TEXT NOT NULL,
                    status          TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info('sync_batches')"
                ).fetchall()
            }
            if "commit_seq" not in columns:
                conn.execute(
                    "ALTER TABLE sync_batches ADD COLUMN commit_seq BIGINT"
                )
            conn.execute(
                """
                UPDATE sync_batches AS target
                SET commit_seq = ranked.commit_seq
                FROM (
                    SELECT
                        batch_id,
                        row_number() OVER (
                            PARTITION BY store_id
                            ORDER BY created_at, batch_id
                        ) AS commit_seq
                    FROM sync_batches
                ) AS ranked
                WHERE target.batch_id = ranked.batch_id
                  AND target.commit_seq IS NULL
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_bars (
                    store_id    TEXT NOT NULL,
                    symbol      TEXT NOT NULL,
                    trade_date  INTEGER NOT NULL,
                    batch_seq   INTEGER NOT NULL,
                    batch_id    TEXT NOT NULL,
                    watermark   INTEGER NOT NULL,
                    open        DOUBLE,
                    high        DOUBLE,
                    low         DOUBLE,
                    close       DOUBLE,
                    volume      DOUBLE,
                    amount      DOUBLE,
                    PRIMARY KEY (store_id, symbol, trade_date, batch_seq)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS adj_factors (
                    store_id    TEXT NOT NULL,
                    symbol      TEXT NOT NULL,
                    trade_date  INTEGER NOT NULL,
                    batch_seq   INTEGER NOT NULL,
                    batch_id    TEXT NOT NULL,
                    watermark   INTEGER NOT NULL,
                    adj_factor  DOUBLE,
                    PRIMARY KEY (store_id, symbol, trade_date, batch_seq)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bars_lookup ON daily_bars "
                "(store_id, symbol, trade_date)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_factors_lookup ON adj_factors "
                "(store_id, symbol, trade_date)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_batches_commit_seq "
                "ON sync_batches (store_id, commit_seq)"
            )

    # ------------------------------------------------------------------
    # batch commit (single writer)
    # ------------------------------------------------------------------
    def batch_exists(self, batch_id: str) -> bool:
        if not self.db_path.exists():
            return False
        with self.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM sync_batches WHERE batch_id = ?", [batch_id]
            ).fetchone()
        return row is not None

    def commit_batch(
        self,
        *,
        batch_id: str,
        kind: str,
        source: str,
        adjustment: str,
        period: str,
        base_dataset_id: str,
        watermark: int,
        rows: Dict[str, Sequence[Tuple]],
        created_at: Optional[str] = None,
    ) -> Dict:
        """Append changed rows for one batch in a single transaction.

        ``rows`` maps symbol -> sequence of (trade_date, value...) tuples;
        for kind=bars the values are (open, high, low, close, volume, amount),
        for kind=factor they are (adj_factor,).

        Idempotency:
          - an already-committed ``batch_id`` is a no-op (returns skipped=0);
          - a row whose visible version (newest batch_seq for that symbol +
            trade_date) already carries the same values is skipped, so
            re-running the same 20-day window never grows the store.

        Returns a summary dict. Raises DeltaWriteError on failure (the
        transaction rolls back, so nothing is persisted).
        """
        self.init_schema()
        created_at = created_at or time.strftime("%Y-%m-%dT%H:%M:%S")
        table = "adj_factors" if kind == KIND_FACTOR else "daily_bars"
        value_cols = ("adj_factor",) if kind == KIND_FACTOR else _BAR_COLS

        with self.connect() as conn:
            # whole batch idempotent
            exists = conn.execute(
                "SELECT 1 FROM sync_batches WHERE batch_id = ?", [batch_id]
            ).fetchone()
            if exists:
                row = conn.execute(
                    "SELECT row_count, watermark, commit_seq FROM sync_batches "
                    "WHERE batch_id = ?",
                    [batch_id],
                ).fetchone()
                return {
                    "batch_id": batch_id,
                    "status": BATCH_COMMITTED,
                    "new_rows": 0,
                    "skipped_rows": int(row[0] or 0),
                    "watermark": int(row[1] or watermark),
                    "commit_seq": int(row[2] or 0),
                    "idempotent": True,
                }
            next_commit_seq = int(
                conn.execute(
                    "SELECT COALESCE(MAX(commit_seq), 0) + 1 "
                    "FROM sync_batches WHERE store_id = ?",
                    [self.store_id],
                ).fetchone()[0]
            )
            rows = {symbol: values for symbol, values in rows.items() if values}
            if not rows:
                conn.execute(
                    "INSERT INTO sync_batches (batch_id, store_id, commit_seq, "
                    "kind, source, adjustment, period, base_dataset_id, watermark, "
                    "row_count, created_at, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    [batch_id, self.store_id, next_commit_seq, kind, source,
                     adjustment, period, base_dataset_id, int(watermark),
                     created_at, BATCH_COMMITTED],
                )
                return {
                    "batch_id": batch_id,
                    "status": BATCH_COMMITTED,
                    "new_rows": 0,
                    "skipped_rows": 0,
                    "watermark": int(watermark),
                    "commit_seq": next_commit_seq,
                }

            # Load the current visible versions for the touched (symbol, date)
            # cells so we can skip identical rows and assign fresh batch_seq.
            symbols = sorted(rows)
            all_dates = sorted(
                {int(td) for sym_rows in rows.values() for td, *_ in sym_rows}
            )
            d0, d1 = all_dates[0], all_dates[-1]
            placeholders = ",".join("?" * len(symbols))
            current: Dict[Tuple[str, int], Tuple[int, Tuple]] = {}
            if len(symbols) <= 900:
                fetched = conn.execute(
                    f"SELECT symbol, trade_date, batch_seq, {', '.join(value_cols)} "
                    f"FROM {table} WHERE store_id = ? AND symbol IN ({placeholders}) "
                    f"AND trade_date BETWEEN ? AND ?",
                    [self.store_id, *symbols, d0, d1],
                ).fetchall()
            else:
                # very large symbol pools: chunked IN lists
                fetched = []
                for i in range(0, len(symbols), 900):
                    chunk = symbols[i : i + 900]
                    ph = ",".join("?" * len(chunk))
                    fetched.extend(
                        conn.execute(
                            f"SELECT symbol, trade_date, batch_seq, "
                            f"{', '.join(value_cols)} FROM {table} "
                            f"WHERE store_id = ? AND symbol IN ({ph}) "
                            f"AND trade_date BETWEEN ? AND ?",
                            [self.store_id, *chunk, d0, d1],
                        ).fetchall()
                    )
            for row in fetched:
                key = (str(row[0]), int(row[1]))
                cur_seq = int(row[2])
                cur_vals = tuple(float(v) if v is not None else None for v in row[3:])
                prev = current.get(key)
                if prev is None or cur_seq > prev[0]:
                    current[key] = (cur_seq, cur_vals)

            inserts: List[Tuple] = []
            new_rows = 0
            skipped_rows = 0
            for sym, sym_rows in rows.items():
                for entry in sym_rows:
                    td = int(entry[0])
                    vals = tuple(float(v) if v is not None else None
                                 for v in entry[1:])
                    if len(vals) != len(value_cols):
                        raise DeltaWriteError(
                            f"batch {batch_id}: symbol {sym} row {td} has "
                            f"{len(vals)} values, expected {len(value_cols)}"
                        )
                    key = (sym, td)
                    prev = current.get(key)
                    if prev is not None and prev[1] == vals:
                        skipped_rows += 1
                        continue
                    new_seq = (prev[0] + 1) if prev else 1
                    inserts.append(
                        (self.store_id, sym, td, new_seq, batch_id,
                         int(watermark), *vals)
                    )
                    current[key] = (new_seq, vals)
                    new_rows += 1

            try:
                conn.execute("BEGIN TRANSACTION")
                if inserts:
                    n_cols = 6 + len(value_cols)
                    row_placeholders = "(" + ",".join("?" * n_cols) + ")"
                    insert_prefix = (
                        f"INSERT INTO {table} (store_id, symbol, trade_date, "
                        f"batch_seq, batch_id, watermark, {', '.join(value_cols)}) "
                        "VALUES "
                    )
                    # DuckDB executemany executes row by row and is prohibitively
                    # slow for a whole-market EOD batch. Send bounded multi-row
                    # statements inside one explicit transaction.
                    for offset in range(0, len(inserts), _INSERT_CHUNK_ROWS):
                        chunk = inserts[offset : offset + _INSERT_CHUNK_ROWS]
                        values_sql = ",".join([row_placeholders] * len(chunk))
                        params = [value for row in chunk for value in row]
                        conn.execute(insert_prefix + values_sql, params)
                conn.execute(
                    "INSERT INTO sync_batches (batch_id, store_id, commit_seq, kind, "
                    "source, adjustment, period, base_dataset_id, watermark, row_count, "
                    "created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [batch_id, self.store_id, next_commit_seq, kind, source,
                     adjustment, period, base_dataset_id, int(watermark), new_rows,
                     created_at, BATCH_COMMITTED],
                )
                conn.execute("COMMIT")
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                if isinstance(exc, DeltaWriteError):
                    raise
                raise DeltaWriteError(
                    f"batch {batch_id} transaction failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        return {
            "batch_id": batch_id,
            "status": BATCH_COMMITTED,
            "new_rows": new_rows,
            "skipped_rows": skipped_rows,
            "watermark": int(watermark),
            "commit_seq": next_commit_seq,
        }

    # ------------------------------------------------------------------
    # watermark / versioned reads
    # ------------------------------------------------------------------
    def current_watermark(self, kind: str) -> int:
        """Highest committed batch watermark for this store + kind."""
        if not self.db_path.exists():
            return 0
        with self.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT MAX(watermark) FROM sync_batches "
                "WHERE store_id = ? AND kind = ? AND status = ?",
                [self.store_id, kind, BATCH_COMMITTED],
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def current_commit_seq(
        self, kind: str, *, watermark: Optional[int] = None
    ) -> int:
        """Highest committed sequence for one kind, optionally by watermark."""
        if not self.db_path.exists():
            return 0
        clauses = ["store_id = ?", "kind = ?", "status = ?"]
        params: List = [self.store_id, kind, BATCH_COMMITTED]
        if watermark is not None:
            clauses.append("watermark <= ?")
            params.append(int(watermark))
        with self.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT MAX(commit_seq) FROM sync_batches WHERE "
                + " AND ".join(clauses),
                params,
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def load_visible_bars(
        self,
        symbols: Sequence[str],
        watermark: int,
        *,
        min_date: Optional[int] = None,
        max_date: Optional[int] = None,
        commit_seq: Optional[int] = None,
    ) -> Dict[str, Dict[int, Tuple[float, float, float, float, float, float]]]:
        """Versioned read locked by watermark and optional commit sequence."""
        if not self.db_path.exists() or not symbols:
            return {}
        out: Dict[str, Dict[int, Tuple]] = {}
        clauses = [
            "d.store_id = ?",
            "d.watermark <= ?",
            "b.status = ?",
        ]
        params: List = [self.store_id, int(watermark), BATCH_COMMITTED]
        if commit_seq is not None:
            clauses.append("b.commit_seq <= ?")
            params.append(int(commit_seq))
        if min_date is not None:
            clauses.append("d.trade_date >= ?")
            params.append(int(min_date))
        if max_date is not None:
            clauses.append("d.trade_date <= ?")
            params.append(int(max_date))
        with self.connect(read_only=True) as conn:
            for i in range(0, len(symbols), 900):
                chunk = list(symbols[i : i + 900])
                ph = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT d.symbol, d.trade_date, d.open, d.high, d.low, "
                    f"d.close, d.volume, d.amount FROM daily_bars AS d "
                    f"JOIN sync_batches AS b ON b.batch_id = d.batch_id "
                    f"AND b.store_id = d.store_id "
                    f"WHERE {(' AND ').join(clauses)} "
                    f"AND d.symbol IN ({ph}) "
                    f"QUALIFY row_number() OVER "
                    f"(PARTITION BY d.symbol, d.trade_date "
                    f"ORDER BY d.batch_seq DESC) = 1",
                    [*params, *chunk],
                ).fetchall()
                for row in rows:
                    sym = str(row[0])
                    out.setdefault(sym, {})[int(row[1])] = (
                        float(row[2]), float(row[3]), float(row[4]),
                        float(row[5]), float(row[6]), float(row[7]),
                    )
        return out

    def load_visible_factors(
        self,
        symbols: Sequence[str],
        watermark: int,
        *,
        commit_seq: Optional[int] = None,
    ) -> Dict[str, Dict[int, float]]:
        """Versioned factor read locked by watermark and commit sequence."""
        if not self.db_path.exists() or not symbols:
            return {}
        out: Dict[str, Dict[int, float]] = {}
        seq_clause = " AND b.commit_seq <= ?" if commit_seq is not None else ""
        with self.connect(read_only=True) as conn:
            for i in range(0, len(symbols), 900):
                chunk = list(symbols[i : i + 900])
                ph = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT d.symbol, d.trade_date, d.adj_factor "
                    f"FROM adj_factors AS d "
                    f"JOIN sync_batches AS b ON b.batch_id = d.batch_id "
                    f"AND b.store_id = d.store_id "
                    f"WHERE d.store_id = ? AND d.watermark <= ? "
                    f"AND b.status = ?{seq_clause} "
                    f"AND d.symbol IN ({ph}) "
                    f"QUALIFY row_number() OVER "
                    f"(PARTITION BY d.symbol, d.trade_date "
                    f"ORDER BY d.batch_seq DESC) = 1",
                    [
                        self.store_id,
                        int(watermark),
                        BATCH_COMMITTED,
                        *([int(commit_seq)] if commit_seq is not None else []),
                        *chunk,
                    ],
                ).fetchall()
                for row in rows:
                    out.setdefault(str(row[0]), {})[int(row[1])] = float(row[2])
        return out

    def distinct_symbols(
        self,
        watermark: int,
        kind: str = KIND_BARS,
        *,
        commit_seq: Optional[int] = None,
    ) -> Set[str]:
        """All symbols with a visible delta row at the requested version."""
        if not self.db_path.exists():
            return set()
        table = "adj_factors" if kind == KIND_FACTOR else "daily_bars"
        seq_clause = " AND b.commit_seq <= ?" if commit_seq is not None else ""
        with self.connect(read_only=True) as conn:
            rows = conn.execute(
                f"SELECT DISTINCT d.symbol FROM {table} AS d "
                f"JOIN sync_batches AS b ON b.batch_id = d.batch_id "
                f"AND b.store_id = d.store_id "
                f"WHERE d.store_id = ? AND d.watermark <= ? "
                f"AND b.status = ?{seq_clause}",
                [
                    self.store_id,
                    int(watermark),
                    BATCH_COMMITTED,
                    *([int(commit_seq)] if commit_seq is not None else []),
                ],
            ).fetchall()
        return {str(row[0]) for row in rows}

    def load_all_visible_bars(
        self, watermark: int, *, commit_seq: Optional[int] = None
    ) -> Dict[str, Dict[int, Tuple[float, float, float, float, float, float]]]:
        """All visible bar rows at the requested immutable version."""
        if not self.db_path.exists():
            return {}
        out: Dict[str, Dict[int, Tuple]] = {}
        seq_clause = " AND b.commit_seq <= ?" if commit_seq is not None else ""
        with self.connect(read_only=True) as conn:
            rows = conn.execute(
                "SELECT d.symbol, d.trade_date, d.open, d.high, d.low, d.close, "
                "d.volume, d.amount FROM daily_bars AS d "
                "JOIN sync_batches AS b ON b.batch_id = d.batch_id "
                "AND b.store_id = d.store_id "
                "WHERE d.store_id = ? AND d.watermark <= ? AND b.status = ?"
                f"{seq_clause} QUALIFY row_number() OVER "
                "(PARTITION BY d.symbol, d.trade_date "
                "ORDER BY d.batch_seq DESC) = 1",
                [
                    self.store_id,
                    int(watermark),
                    BATCH_COMMITTED,
                    *([int(commit_seq)] if commit_seq is not None else []),
                ],
            ).fetchall()
        for row in rows:
            out.setdefault(str(row[0]), {})[int(row[1])] = (
                float(row[2]), float(row[3]), float(row[4]),
                float(row[5]), float(row[6]), float(row[7]),
            )
        return out

    def load_all_visible_factors(
        self, watermark: int, *, commit_seq: Optional[int] = None
    ) -> Dict[str, Dict[int, float]]:
        """All visible factor rows at the requested immutable version."""
        if not self.db_path.exists():
            return {}
        out: Dict[str, Dict[int, float]] = {}
        seq_clause = " AND b.commit_seq <= ?" if commit_seq is not None else ""
        with self.connect(read_only=True) as conn:
            rows = conn.execute(
                "SELECT d.symbol, d.trade_date, d.adj_factor "
                "FROM adj_factors AS d "
                "JOIN sync_batches AS b ON b.batch_id = d.batch_id "
                "AND b.store_id = d.store_id "
                "WHERE d.store_id = ? AND d.watermark <= ? AND b.status = ?"
                f"{seq_clause} QUALIFY row_number() OVER "
                "(PARTITION BY d.symbol, d.trade_date "
                "ORDER BY d.batch_seq DESC) = 1",
                [
                    self.store_id,
                    int(watermark),
                    BATCH_COMMITTED,
                    *([int(commit_seq)] if commit_seq is not None else []),
                ],
            ).fetchall()
        for row in rows:
            out.setdefault(str(row[0]), {})[int(row[1])] = float(row[2])
        return out
    # ------------------------------------------------------------------
    # governance / stats
    # ------------------------------------------------------------------
    def list_batches(self, kind: Optional[str] = None) -> List[Dict]:
        if not self.db_path.exists():
            return []
        with self.connect(read_only=True) as conn:
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info('sync_batches')"
                ).fetchall()
            }
            has_commit_seq = "commit_seq" in columns
            commit_expr = "commit_seq" if has_commit_seq else "0 AS commit_seq"
            q = f"SELECT batch_id, store_id, {commit_expr}, kind, source, " \
                "adjustment, period, base_dataset_id, watermark, row_count, " \
                "created_at, status FROM sync_batches"
            params: List = []
            if kind:
                q += " WHERE kind = ?"
                params.append(kind)
            rows = conn.execute(q + " ORDER BY watermark", params).fetchall()
        return [
            {
                "batch_id": r[0], "store_id": r[1],
                "commit_seq": int(r[2] or 0), "kind": r[3],
                "source": r[4], "adjustment": r[5], "period": r[6],
                "base_dataset_id": r[7], "watermark": int(r[8]),
                "row_count": int(r[9]), "created_at": r[10], "status": r[11],
            }
            for r in rows
        ]

    def delta_row_count(self, kind: str) -> int:
        if not self.db_path.exists():
            return 0
        table = "adj_factors" if kind == KIND_FACTOR else "daily_bars"
        with self.connect(read_only=True) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE store_id = ?",
                [self.store_id],
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def db_file_size(self) -> int:
        total = 0
        for path in (
            self.db_path,
            self.db_path.with_suffix(self.db_path.suffix + ".wal"),
        ):
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total

    def visible_trade_dates(
        self,
        kind: str = KIND_BARS,
        *,
        watermark: Optional[int] = None,
        commit_seq: Optional[int] = None,
    ) -> List[int]:
        """Return distinct committed trade dates visible to this generation."""
        if not self.db_path.exists():
            return []
        table = "adj_factors" if kind == KIND_FACTOR else "daily_bars"
        clauses = ["d.store_id = ?", "b.status = ?"]
        params: List[object] = [self.store_id, BATCH_COMMITTED]
        if watermark is not None:
            clauses.append("d.watermark <= ?")
            params.append(int(watermark))
        if commit_seq is not None:
            clauses.append("b.commit_seq <= ?")
            params.append(int(commit_seq))
        sql = (
            f"SELECT DISTINCT d.trade_date FROM {table} AS d "
            "JOIN sync_batches AS b ON b.batch_id = d.batch_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY d.trade_date"
        )
        with self.connect(read_only=True) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [int(row[0]) for row in rows]

    def health_check(
        self,
        watermark: int,
        *,
        factor_watermark: Optional[int] = None,
        commit_seq: Optional[int] = None,
        factor_commit_seq: Optional[int] = None,
    ) -> Dict:
        """Validate the committed bar and optional factor surfaces."""
        problems: List[str] = []
        try:
            if not self.db_path.exists():
                problems.append("delta db missing")
            else:
                with self.connect(read_only=True) as conn:
                    n = conn.execute(
                        "SELECT COUNT(*) FROM sync_batches WHERE store_id = ?",
                        [self.store_id],
                    ).fetchone()[0]
                    if n is None:
                        problems.append("sync_batches unreadable")
                    bars_wm = conn.execute(
                        "SELECT MAX(watermark) FROM sync_batches "
                        "WHERE store_id = ? AND kind = ? AND status = ?",
                        [self.store_id, KIND_BARS, BATCH_COMMITTED],
                    ).fetchone()[0]
                    if watermark and (bars_wm or 0) < int(watermark):
                        problems.append(
                            f"committed watermark {bars_wm} < expected {watermark}"
                        )
                    if commit_seq:
                        bars_seq = conn.execute(
                            "SELECT MAX(commit_seq) FROM sync_batches "
                            "WHERE store_id = ? AND kind = ? AND status = ?",
                            [self.store_id, KIND_BARS, BATCH_COMMITTED],
                        ).fetchone()[0]
                        if (bars_seq or 0) < int(commit_seq):
                            problems.append(
                                f"committed bars sequence {bars_seq} < expected "
                                f"{commit_seq}"
                            )
                    if factor_watermark:
                        factor_wm = conn.execute(
                            "SELECT MAX(watermark) FROM sync_batches "
                            "WHERE store_id = ? AND kind = ? AND status = ?",
                            [self.store_id, KIND_FACTOR, BATCH_COMMITTED],
                        ).fetchone()[0]
                        if (factor_wm or 0) < int(factor_watermark):
                            problems.append(
                                f"committed factor watermark {factor_wm} < expected "
                                f"{factor_watermark}"
                            )
                    if factor_commit_seq:
                        factor_seq = conn.execute(
                            "SELECT MAX(commit_seq) FROM sync_batches "
                            "WHERE store_id = ? AND kind = ? AND status = ?",
                            [self.store_id, KIND_FACTOR, BATCH_COMMITTED],
                        ).fetchone()[0]
                        if (factor_seq or 0) < int(factor_commit_seq):
                            problems.append(
                                f"committed factor sequence {factor_seq} < expected "
                                f"{factor_commit_seq}"
                            )
        except Exception as exc:  # noqa: BLE001
            problems.append(f"read failed: {type(exc).__name__}: {exc}")
        return {
            "db_path": str(self.db_path),
            "store_id": self.store_id,
            "exists": self.db_path.exists(),
            "ok": not problems,
            "problems": problems,
            "file_size_bytes": self.db_file_size(),
            "bars_rows": self.delta_row_count(KIND_BARS),
            "factor_rows": self.delta_row_count(KIND_FACTOR),
            "batches": len(self.list_batches()),
        }

    def mark_orphaned(self, batch_id: str, conn=None) -> None:
        """Governance: mark an invisible batch orphaned (72h sweep target)."""
        def _do(c):
            c.execute(
                "UPDATE sync_batches SET status = ? WHERE batch_id = ?",
                [BATCH_ORPHANED, batch_id],
            )
        if conn is not None:
            _do(conn)
        else:
            with self.connect() as conn:
                _do(conn)

    def prune_batch_rows(self, batch_id: str, conn=None) -> int:
        """Governance: delete rows + registry entry of an invisible batch."""
        def _do(c):
            c.execute(
                "DELETE FROM daily_bars WHERE store_id = ? AND batch_id = ?",
                [self.store_id, batch_id],
            )
            c.execute(
                "DELETE FROM adj_factors WHERE store_id = ? AND batch_id = ?",
                [self.store_id, batch_id],
            )
            c.execute("DELETE FROM sync_batches WHERE batch_id = ?", [batch_id])
            return 0
        if conn is not None:
            return _do(conn)
        with self.connect() as conn:
            return _do(conn)


# ---------------------------------------------------------------------------
# overlay state registry
# ---------------------------------------------------------------------------


@dataclass
class OverlayState:
    """Registry describing which base datasets a delta store overlays."""

    enabled: bool = False
    schema_version: int = SCHEMA_VERSION
    delta_store_id: str = "main"
    base_dataset_id: str = ""
    base_manifest_sha256: str = ""
    delisted_base_dataset_id: str = ""
    delisted_base_manifest_sha256: str = ""
    factor_base_dataset_id: str = ""
    factor_base_manifest_sha256: str = ""
    supplement_factor_base_dataset_id: str = ""
    supplement_factor_base_manifest_sha256: str = ""
    delta_watermark: int = 0
    factor_watermark: int = 0
    delta_commit_seq: int = 0
    factor_commit_seq: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "schema_version": self.schema_version,
            "delta_store_id": self.delta_store_id,
            "base_dataset_id": self.base_dataset_id,
            "base_manifest_sha256": self.base_manifest_sha256,
            "delisted_base_dataset_id": self.delisted_base_dataset_id,
            "delisted_base_manifest_sha256": self.delisted_base_manifest_sha256,
            "factor_base_dataset_id": self.factor_base_dataset_id,
            "factor_base_manifest_sha256": self.factor_base_manifest_sha256,
            "supplement_factor_base_dataset_id": self.supplement_factor_base_dataset_id,
            "supplement_factor_base_manifest_sha256": (
                self.supplement_factor_base_manifest_sha256
            ),
            "delta_watermark": self.delta_watermark,
            "factor_watermark": self.factor_watermark,
            "delta_commit_seq": self.delta_commit_seq,
            "factor_commit_seq": self.factor_commit_seq,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OverlayState":
        schema_version = int(d.get("schema_version") or 1)
        legacy_sequence = -1 if schema_version < 2 else 0
        return cls(
            enabled=bool(d.get("enabled", False)),
            schema_version=schema_version,
            delta_store_id=str(d.get("delta_store_id") or "main"),
            base_dataset_id=str(d.get("base_dataset_id") or ""),
            base_manifest_sha256=str(d.get("base_manifest_sha256") or ""),
            delisted_base_dataset_id=str(d.get("delisted_base_dataset_id") or ""),
            delisted_base_manifest_sha256=str(
                d.get("delisted_base_manifest_sha256") or ""
            ),
            factor_base_dataset_id=str(d.get("factor_base_dataset_id") or ""),
            factor_base_manifest_sha256=str(
                d.get("factor_base_manifest_sha256") or ""
            ),
            supplement_factor_base_dataset_id=str(
                d.get("supplement_factor_base_dataset_id") or ""
            ),
            supplement_factor_base_manifest_sha256=str(
                d.get("supplement_factor_base_manifest_sha256") or ""
            ),
            delta_watermark=int(d.get("delta_watermark") or 0),
            factor_watermark=int(d.get("factor_watermark") or 0),
            delta_commit_seq=int(
                d.get("delta_commit_seq")
                if d.get("delta_commit_seq") is not None
                else legacy_sequence
            ),
            factor_commit_seq=int(
                d.get("factor_commit_seq")
                if d.get("factor_commit_seq") is not None
                else legacy_sequence
            ),
            created_at=str(d.get("created_at") or ""),
            updated_at=str(d.get("updated_at") or ""),
        )


_OVERLAY_CACHE: Dict[str, tuple] = {}
_OVERLAY_CACHE_LOCK = threading.Lock()


def load_overlay_state(root: Path | str) -> OverlayState:
    """Read the overlay registry with a stat-signature cache.

    Keyed by (mtime_ns, size) so a save is picked up immediately while
    repeated reads inside whole-market loops do not hit disk per symbol.
    """
    import threading

    path = Path(root) / DELTA_DIR_NAME / OVERLAY_STATE_NAME
    if not path.exists():
        return OverlayState()
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    key = str(Path(root))
    with _OVERLAY_CACHE_LOCK:
        hit = _OVERLAY_CACHE.get(key)
        if hit is not None and hit[0] == sig:
            return OverlayState.from_dict(hit[1].to_dict())
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("overlay state root must be a JSON object")
        state = OverlayState.from_dict(payload)
    except Exception as exc:
        raise OverlayStateError(
            f"overlay state exists but is unreadable: {path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if sig is not None:
        with _OVERLAY_CACHE_LOCK:
            _OVERLAY_CACHE[key] = (sig, state)
    return OverlayState.from_dict(state.to_dict())


def save_overlay_state(root: Path | str, state: OverlayState) -> Path:
    path = Path(root) / DELTA_DIR_NAME / OVERLAY_STATE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    state.schema_version = SCHEMA_VERSION
    state.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    atomic_write_json(path, state.to_dict())
    return path


# ---------------------------------------------------------------------------
# delta write lock (reuses SyncTaskLock primitives)
# ---------------------------------------------------------------------------


def delta_write_lock(root: Path | str, store_id: str = "main"):
    """Exclusive cross-process lock for delta batch commits.

    The EOD chain holds this lock for the whole raw+factor delta phase so two
    writers (server child + manual CLI) can never interleave batches.
    """
    from .sync_lock import SyncTaskLock

    return SyncTaskLock(
        Path(root) / DELTA_DIR_NAME,
        source="delta_store",
        adjustment=store_id,
        period="write",
    )


def make_batch_id(
    *,
    sync_run_id: str,
    kind: str,
    cutoff: int,
    suffix: str = "",
) -> str:
    """Deterministic batch id so retries of the same sync run are no-ops."""
    payload = f"{sync_run_id}|{kind}|{cutoff}|{suffix}"
    import hashlib

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
