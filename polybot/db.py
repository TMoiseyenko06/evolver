"""Small SQLite helpers — the persistence pattern the rest of the code reuses.

Nothing fancy: a connection factory that turns on ``Row`` access and foreign
keys, plus a schema-application helper. ``evolver.store`` layers its own schema
and queries on top of this.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Union


def connect(path: Union[str, Path]) -> sqlite3.Connection:
    """Open a SQLite connection with dict-like rows and FK enforcement."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def apply_schema(conn: sqlite3.Connection, schema_sql: str) -> None:
    """Idempotently apply a schema (statements should use IF NOT EXISTS)."""
    conn.executescript(schema_sql)
    conn.commit()


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None
