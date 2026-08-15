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

SCHEMA_VERSION = 1

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


class DeltaWriteError(RuntimeError):
    """A delta batch failed to commit; nothing was written."""


#: module-level read-only connection pool keyed by (thread_id, db_path).
#: DuckDB forbids mixing read-only and read-write connections to the same
#: file within one process, and opening a connection costs ~20ms — so readers
#: reuse ONE read-only connection per (thread, file), and any read-write
#: ``connect()`` releases it first (consolidate / tests mix both in one
#: thread).
_READ_CONN_POOL: Dict[tuple, tuple] = {}
_READ_CONN_LOCK = threading.Lock()


class DeltaStore:
    """Versioned incremental store on top of a DuckDB file."""

    def __init__(self, root: Path | str, store_id: str = "main"):
        self.root = Path(root)
        self.store_id = store_id
        self.delta_dir = self.root / DELTA_DIR_NAME
        self.db_path = self.delta_dir / DELTA_DB_NAME
        self.overlay_path = self.delta_dir / OVERLAY_STATE_NAME

    # ------------------------------------------------------------------
    # connection helpers
    # ------------------------------------------------------------------
    def connect(self, *, read_only: bool = False):
        """Open a short DuckDB connection.

        Readers pass read_only=True so a long-lived server process can share
        the file with the single-writer EOD child without lock contention
        (DuckDB allows one read-write writer + any number of read-only
        readers on the same file). A read-write open first releases any
        pooled read-only connection for this file in the current thread
        (DuckDB forbids mixing the two configurations).
        """
        import duckdb

        self.delta_dir.mkdir(parents=True, exist_ok=True)
        if not read_only:
            self._release_read_conn()
        return duckdb.connect(str(self.db_path), read_only=read_only)

    def _db_sig(self):
        """Stat signature of the DB file (None when it does not exist yet)."""
        try:
            st = self.db_path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _read_conn_key(self):
        return (threading.get_ident(), str(self.db_path))

    def _get_read_conn(self):
        """Pooled read-only connection, invalidated on file change.

        A DuckDB read-only connection pins the database state it opened; the
        EOD writer (a separate process) changes the file mtime on commit, so
        the signature is re-checked on every access and the connection is
        reopened when the file moved. The pool is per (thread, file) so
        threads never share a DuckDB connection (which is not thread-safe),
        while different DeltaStore instances on the same file in one thread
        still share one connection.
        """
        key = self._read_conn_key()
        sig = self._db_sig()
        with _READ_CONN_LOCK:
            entry = _READ_CONN_POOL.get(key)
            if entry is not None and entry[0] == sig:
                return entry[1]
            if entry is not None:
                old = entry[1]
                _READ_CONN_POOL.pop(key, None)
            else:
                old = None
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        conn = self.connect(read_only=True)
        with _READ_CONN_LOCK:
            _READ_CONN_POOL[key] = (sig, conn)
        return conn

    def _release_read_conn(self):
        """Close any pooled read-only connection for this file in this thread."""
        key = self._read_conn_key()
        with _READ_CONN_LOCK:
            entry = _READ_CONN_POOL.pop(key, None)
        if entry is not None:
            try:
                entry[1].close()
            except Exception:
                pass

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_batches (
                    batch_id        TEXT PRIMARY KEY,
                    store_id        TEXT NOT NULL,
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
                    "SELECT row_count, watermark FROM sync_batches "
                    "WHERE batch_id = ?",
                    [batch_id],
                ).fetchone()
                return {
                    "batch_id": batch_id,
                    "status": BATCH_COMMITTED,
                    "new_rows": 0,
                    "skipped_rows": int(row[0] or 0),
                    "watermark": int(row[1] or watermark),
                    "idempotent": True,
                }
            if not rows:
                conn.execute(
                    "INSERT INTO sync_batches (batch_id, store_id, kind, source, "
                    "adjustment, period, base_dataset_id, watermark, row_count, "
                    "created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    [batch_id, self.store_id, kind, source, adjustment, period,
                     base_dataset_id, int(watermark), created_at, BATCH_COMMITTED],
                )
                return {
                    "batch_id": batch_id,
                    "status": BATCH_COMMITTED,
                    "new_rows": 0,
                    "skipped_rows": 0,
                    "watermark": int(watermark),
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

            if inserts:
                n_cols = 6 + len(value_cols)
                ph = ",".join("?" * n_cols)
                conn.executemany(
                    f"INSERT INTO {table} (store_id, symbol, trade_date, "
                    f"batch_seq, batch_id, watermark, {', '.join(value_cols)}) "
                    f"VALUES ({ph})",
                    inserts,
                )
            conn.execute(
                "INSERT INTO sync_batches (batch_id, store_id, kind, source, "
                "adjustment, period, base_dataset_id, watermark, row_count, "
                "created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [batch_id, self.store_id, kind, source, adjustment, period,
                 base_dataset_id, int(watermark), new_rows, created_at,
                 BATCH_COMMITTED],
            )
        return {
            "batch_id": batch_id,
            "status": BATCH_COMMITTED,
            "new_rows": new_rows,
            "skipped_rows": skipped_rows,
            "watermark": int(watermark),
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

    def load_visible_bars(
        self,
        symbols: Sequence[str],
        watermark: int,
        *,
        min_date: Optional[int] = None,
        max_date: Optional[int] = None,
    ) -> Dict[str, Dict[int, Tuple[float, float, float, float, float, float]]]:
        """Versioned read: newest batch_seq with batch watermark <= watermark.

        Returns {symbol: {trade_date: (open, high, low, close, volume, amount)}}.
        Only rows within [min_date, max_date] (when given) are returned.
        """
        if not self.db_path.exists() or not symbols:
            return {}
        out: Dict[str, Dict[int, Tuple]] = {}
        conn = self._get_read_conn()
        clauses = ["store_id = ?", "watermark <= ?"]
        params: List = [self.store_id, int(watermark)]
        if min_date is not None:
            clauses.append("trade_date >= ?")
            params.append(int(min_date))
        if max_date is not None:
            clauses.append("trade_date <= ?")
            params.append(int(max_date))
        for i in range(0, len(symbols), 900):
            chunk = list(symbols[i : i + 900])
            ph = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT symbol, trade_date, open, high, low, close, "
                f"volume, amount FROM daily_bars "
                f"WHERE {(' AND ').join(clauses)} AND symbol IN ({ph}) "
                f"QUALIFY row_number() OVER "
                f"(PARTITION BY symbol, trade_date ORDER BY batch_seq DESC) = 1",
                [*params, *chunk],
            ).fetchall()
            for r in rows:
                sym = str(r[0])
                out.setdefault(sym, {})[int(r[1])] = (
                    float(r[2]), float(r[3]), float(r[4]),
                    float(r[5]), float(r[6]), float(r[7]),
                )
        return out

    def load_visible_factors(
        self,
        symbols: Sequence[str],
        watermark: int,
    ) -> Dict[str, Dict[int, float]]:
        """Versioned factor read: newest adj_factor per date with watermark <=."""
        if not self.db_path.exists() or not symbols:
            return {}
        out: Dict[str, Dict[int, float]] = {}
        conn = self._get_read_conn()
        for i in range(0, len(symbols), 900):
            chunk = list(symbols[i : i + 900])
            ph = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT symbol, trade_date, adj_factor FROM adj_factors "
                f"WHERE store_id = ? AND watermark <= ? AND symbol IN ({ph}) "
                f"QUALIFY row_number() OVER "
                f"(PARTITION BY symbol, trade_date ORDER BY batch_seq DESC) = 1",
                [self.store_id, int(watermark), *chunk],
            ).fetchall()
            for r in rows:
                out.setdefault(str(r[0]), {})[int(r[1])] = float(r[2])
        return out

    def distinct_symbols(self, watermark: int, kind: str = KIND_BARS) -> Set[str]:
        """All symbols that have at least one delta row up to ``watermark``.

        Used to surface symbols that exist ONLY in the delta (e.g. IPOs whose
        base snapshot predates their listing) so the virtual pool does not
        silently drop them.
        """
        if not self.db_path.exists():
            return set()
        table = "adj_factors" if kind == KIND_FACTOR else "daily_bars"
        conn = self._get_read_conn()
        rows = conn.execute(
            f"SELECT DISTINCT symbol FROM {table} "
            f"WHERE store_id = ? AND watermark <= ?",
            [self.store_id, int(watermark)],
        ).fetchall()
        return {str(r[0]) for r in rows}

    def load_all_visible_bars(
        self, watermark: int
    ) -> Dict[str, Dict[int, Tuple[float, float, float, float, float, float]]]:
        """All visible bar rows up to ``watermark`` in one query.

        The delta surface is small by design (target < 10MB/day), so a
        whole-market backtest can afford to pull every visible delta row into
        memory ONCE and then serve per-symbol merges from that dict — this is
        what keeps per-symbol ``load_bars`` from opening a connection or
        running SQL per symbol.
        """
        if not self.db_path.exists():
            return {}
        out: Dict[str, Dict[int, Tuple]] = {}
        conn = self._get_read_conn()
        rows = conn.execute(
            "SELECT symbol, trade_date, open, high, low, close, volume, "
            "amount FROM daily_bars WHERE store_id = ? AND watermark <= ? "
            "QUALIFY row_number() OVER (PARTITION BY symbol, trade_date "
            "ORDER BY batch_seq DESC) = 1",
            [self.store_id, int(watermark)],
        ).fetchall()
        for r in rows:
            out.setdefault(str(r[0]), {})[int(r[1])] = (
                float(r[2]), float(r[3]), float(r[4]),
                float(r[5]), float(r[6]), float(r[7]),
            )
        return out

    def load_all_visible_factors(
        self, watermark: int
    ) -> Dict[str, Dict[int, float]]:
        """All visible factor rows up to ``watermark`` in one query."""
        if not self.db_path.exists():
            return {}
        out: Dict[str, Dict[int, float]] = {}
        conn = self._get_read_conn()
        rows = conn.execute(
            "SELECT symbol, trade_date, adj_factor FROM adj_factors "
            "WHERE store_id = ? AND watermark <= ? "
            "QUALIFY row_number() OVER (PARTITION BY symbol, trade_date "
            "ORDER BY batch_seq DESC) = 1",
            [self.store_id, int(watermark)],
        ).fetchall()
        for r in rows:
            out.setdefault(str(r[0]), {})[int(r[1])] = float(r[2])
        return out

    # ------------------------------------------------------------------
    # governance / stats
    # ------------------------------------------------------------------
    def list_batches(self, kind: Optional[str] = None) -> List[Dict]:
        if not self.db_path.exists():
            return []
        with self.connect(read_only=True) as conn:
            q = "SELECT batch_id, store_id, kind, source, adjustment, period, " \
                "base_dataset_id, watermark, row_count, created_at, status " \
                "FROM sync_batches"
            params: List = []
            if kind:
                q += " WHERE kind = ?"
                params.append(kind)
            rows = conn.execute(q + " ORDER BY watermark", params).fetchall()
        return [
            {
                "batch_id": r[0], "store_id": r[1], "kind": r[2],
                "source": r[3], "adjustment": r[4], "period": r[5],
                "base_dataset_id": r[6], "watermark": int(r[7]),
                "row_count": int(r[8]), "created_at": r[9], "status": r[10],
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
        try:
            return self.db_path.stat().st_size if self.db_path.exists() else 0
        except OSError:
            return 0

    def health_check(self, watermark: int) -> Dict:
        """Validate the committed store up to ``watermark`` is readable."""
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
                    w = conn.execute(
                        "SELECT MAX(watermark) FROM sync_batches "
                        "WHERE store_id = ? AND kind = ? AND status = ?",
                        [self.store_id, KIND_BARS, BATCH_COMMITTED],
                    ).fetchone()[0]
                    if watermark and (w or 0) < watermark:
                        problems.append(
                            f"committed watermark {w} < expected {watermark}"
                        )
        except Exception as e:  # noqa: BLE001
            problems.append(f"read failed: {type(e).__name__}: {e}")
        return {
            "db_path": str(self.db_path),
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
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OverlayState":
        return cls(
            enabled=bool(d.get("enabled", False)),
            schema_version=int(d.get("schema_version") or SCHEMA_VERSION),
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
            return hit[1]
    try:
        state = OverlayState.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except Exception:
        state = OverlayState()
    if sig is not None:
        with _OVERLAY_CACHE_LOCK:
            _OVERLAY_CACHE[key] = (sig, state)
    return state


def save_overlay_state(root: Path | str, state: OverlayState) -> Path:
    path = Path(root) / DELTA_DIR_NAME / OVERLAY_STATE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
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
