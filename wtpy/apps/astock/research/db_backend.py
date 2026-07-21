# -*- coding: utf-8 -*-
"""Database backend abstraction (SQLite now; Postgres adapter hook)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, Sequence, runtime_checkable


@runtime_checkable
class DatabaseBackend(Protocol):
    def connect(self) -> Any:
        ...

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        ...

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list:
        ...

    def close(self) -> None:
        ...


class SqliteDatabaseBackend:
    """Thin sqlite3 wrapper with dict-like row access."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            parent = Path(self.path).parent
            if parent and str(parent) not in (".", ""):
                parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, timeout=30.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        conn = self.connect()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        conn = self.connect()
        cur = conn.executemany(sql, list(seq_of_params))
        conn.commit()
        return cur

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list:
        conn = self.connect()
        cur = conn.execute(sql, tuple(params))
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[dict]:
        conn = self.connect()
        cur = conn.execute(sql, tuple(params))
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class PostgresDatabaseBackend:
    """Optional Postgres backend; requires psycopg2 (or psycopg)."""

    def __init__(self, url: str):
        self.url = url
        self._conn = None

    def connect(self):
        try:
            import psycopg2  # type: ignore
        except ImportError as e:
            raise ImportError(
                "PostgresDatabaseBackend requires psycopg2. "
                "Install with: pip install psycopg2-binary"
            ) from e
        if self._conn is None:
            self._conn = psycopg2.connect(self.url)
        return self._conn

    def execute(self, sql: str, params: Sequence[Any] = ()):
        conn = self.connect()
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        conn.commit()
        return cur

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list:
        conn = self.connect()
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def get_database_backend(url: str) -> DatabaseBackend:
    """Factory: sqlite:///path or postgresql://..."""
    u = (url or "").strip()
    if u.startswith("sqlite:///"):
        path = u[len("sqlite:///") :]
        return SqliteDatabaseBackend(path)
    if u.startswith("sqlite://"):
        # sqlite://relative
        path = u[len("sqlite://") :]
        return SqliteDatabaseBackend(path)
    if u.startswith("postgresql://") or u.startswith("postgres://"):
        return PostgresDatabaseBackend(u)
    # bare path treated as sqlite file
    if "://" not in u:
        return SqliteDatabaseBackend(u)
    raise ValueError(f"Unsupported database URL scheme: {url!r}")
