"""SQLite helpers: connection, schema init, generic upsert."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: Path | str, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=30)
        conn.execute("PRAGMA query_only = ON")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.executescript(SCHEMA_PATH.read_text())
    conn.row_factory = sqlite3.Row
    return conn


def upsert(conn: sqlite3.Connection, table: str, key: str, rows: Iterable[Mapping[str, Any]]) -> int:
    """INSERT ... ON CONFLICT(key) DO UPDATE for every column. Idempotent."""
    n = 0
    for row in rows:
        cols = list(row)
        placeholders = ", ".join(f":{c}" for c in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != key)
        stamp = "parsed_at" if table == "activity_fit" else "synced_at"
        sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
               f"ON CONFLICT({key}) DO UPDATE SET {updates}{', ' if updates else ''}{stamp}=datetime('now')")
        conn.execute(sql, dict(row))
        n += 1
    return n
