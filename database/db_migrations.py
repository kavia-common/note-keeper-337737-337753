#!/usr/bin/env python3
"""
SQLite schema initialization and migrations for the notes app.

This module centralizes all schema DDL and migration logic so it remains:
- safe to run multiple times (idempotent),
- debuggable (structured logs),
- forward-migratable (PRAGMA user_version based).

It is used by init_db.py (and can be imported by other components if needed).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sqlite3
from typing import Iterable, Optional, Tuple


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DbPaths:
    """Resolved DB-related paths based on db_connection.txt guidance."""

    db_file_path: Path
    db_connection_txt_path: Path


class MigrationError(RuntimeError):
    """Raised when a migration cannot be applied safely."""


def _configure_logging() -> None:
    """Configure a simple, consistent logger for CLI usage."""
    # Keep it minimal; init_db.py can configure logging too, but this ensures
    # reasonable output when the module is invoked indirectly.
    if logger.handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


def _parse_db_connection_txt(connection_txt_path: Path) -> Optional[DbPaths]:
    """
    Parse db_connection.txt to find the canonical SQLite file path.

    Expected line format (as present in this repo):
      # File path: /abs/path/to/myapp.db

    Returns:
      DbPaths if file path is found, otherwise None.

    Errors:
      None (returns None and logs at DEBUG level).
    """
    if not connection_txt_path.exists():
        logger.debug("db_connection.txt not found at %s", connection_txt_path)
        return None

    try:
        content = connection_txt_path.read_text(encoding="utf-8")
    except Exception:
        logger.exception("Failed reading %s", connection_txt_path)
        return None

    for line in content.splitlines():
        line = line.strip()
        if line.lower().startswith("# file path:"):
            raw = line.split(":", 1)[1].strip()
            if raw:
                return DbPaths(db_file_path=Path(raw), db_connection_txt_path=connection_txt_path)

    logger.debug("No '# File path:' entry found in %s", connection_txt_path)
    return None


def resolve_db_path(default_db_name: str = "myapp.db") -> DbPaths:
    """
    Resolve the SQLite DB file path using db_connection.txt as canonical guidance.

    Contract:
      Inputs:
        - default_db_name: file name to use if db_connection.txt is missing/incomplete.
      Outputs:
        - DbPaths with db_file_path (absolute or relative) and db_connection_txt_path.
      Side-effects:
        - None.
      Errors:
        - Never raises for missing db_connection.txt; falls back to local default path.

    Invariants:
      - If db_connection.txt exists and contains a File path line, that path wins.
    """
    # db_connection.txt lives in the database container directory.
    connection_txt_path = Path(__file__).resolve().parent / "db_connection.txt"
    parsed = _parse_db_connection_txt(connection_txt_path)
    if parsed:
        return parsed

    # Fallback: local db in CWD (historical behavior of init_db.py).
    return DbPaths(db_file_path=Path(default_db_name), db_connection_txt_path=connection_txt_path)


def _connect(db_file_path: Path) -> sqlite3.Connection:
    """
    Create a SQLite connection with sane defaults for this app.

    Notes:
      - Sets row_factory for better debugging ergonomics (dict-like rows).
      - Enables foreign keys.
      - Uses WAL to improve concurrent read/write characteristics.
    """
    conn = sqlite3.connect(str(db_file_path))
    conn.row_factory = sqlite3.Row

    # Ensure consistent behavior.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _get_user_version(conn: sqlite3.Connection) -> int:
    """Return PRAGMA user_version."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    """Set PRAGMA user_version."""
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    return row is not None


def _ensure_base_tables(conn: sqlite3.Connection) -> None:
    """
    Ensure legacy/base tables exist (app_info, users).

    This keeps backward compatibility with the template DB that already ships
    in this repo and avoids creating divergent schemas.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            value TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _seed_app_info(conn: sqlite3.Connection) -> None:
    """Seed app_info keys (idempotent)."""
    # Keep existing keys stable; do not overwrite user-authored description.
    conn.execute(
        "INSERT OR IGNORE INTO app_info (key, value) VALUES (?, ?)",
        ("project_name", "database"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_info (key, value) VALUES (?, ?)",
        ("version", "0.1.0"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_info (key, value) VALUES (?, ?)",
        ("author", "John Doe"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_info (key, value) VALUES (?, ?)",
        ("description", ""),
    )


def _ensure_notes_table_and_indexes(conn: sqlite3.Connection) -> None:
    """
    Ensure notes table + indexes exist.

    Schema contract (v1):
      - notes.id: integer PK
      - title: non-null text (can be empty string)
      - content: non-null text
      - created_at: timestamp default current_timestamp
      - updated_at: timestamp default current_timestamp (kept updated by trigger)

    Indexes:
      - idx_notes_updated_at for ordering / listing
      - idx_notes_title for basic title search
      - idx_notes_created_at for stable ordering by creation
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Updated_at trigger is the most portable way in SQLite to keep updated_at in sync.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_notes_set_updated_at
        AFTER UPDATE ON notes
        FOR EACH ROW
        BEGIN
            UPDATE notes
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = OLD.id;
        END
        """
    )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notes_updated_at ON notes(updated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notes_created_at ON notes(created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notes_title ON notes(title)"
    )


def _apply_migration_v1(conn: sqlite3.Connection) -> None:
    """
    Migration v1: Introduce notes table + indexes.

    Safe to run on an existing DB (CREATE IF NOT EXISTS).
    """
    logger.info("Applying migration v1 (notes schema)")
    _ensure_notes_table_and_indexes(conn)
    _set_user_version(conn, 1)


def _apply_migrations(conn: sqlite3.Connection) -> Tuple[int, int]:
    """
    Apply all needed migrations in a single transaction.

    Returns:
      (from_version, to_version)
    """
    from_version = _get_user_version(conn)
    to_version = from_version

    # Important: keep this monotonic and explicit.
    if from_version < 1:
        _apply_migration_v1(conn)
        to_version = 1

    return from_version, to_version


# PUBLIC_INTERFACE
def initialize_and_migrate_sqlite(
    *,
    default_db_name: str = "myapp.db",
) -> DbPaths:
    """
    Initialize and migrate the SQLite DB to the latest schema version.

    Flow name:
      SQLiteInitAndMigrateFlow

    Contract:
      Inputs:
        - default_db_name: fallback db file name if db_connection.txt is missing.
      Outputs:
        - DbPaths with resolved db_file_path and db_connection_txt_path.
      Errors:
        - Raises MigrationError on unrecoverable migration/connection failures.
      Side effects:
        - Creates DB file if absent.
        - Creates/updates tables/indexes/triggers as needed.
        - Updates PRAGMA user_version to current schema version.

    Debugging:
      - Check logs from init_db.py.
      - Use `python db_shell.py` then `.tables` / `.schema notes`.
      - Inspect PRAGMA user_version.
    """
    _configure_logging()
    paths = resolve_db_path(default_db_name=default_db_name)

    logger.info(
        "SQLiteInitAndMigrateFlow start: db_file_path=%s (from %s)",
        paths.db_file_path,
        paths.db_connection_txt_path,
    )

    # Ensure parent dir exists when path is absolute and includes directories.
    try:
        if paths.db_file_path.is_absolute():
            paths.db_file_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise MigrationError(
            f"Failed ensuring directory for DB path {paths.db_file_path}: {e}"
        ) from e

    try:
        conn = _connect(paths.db_file_path)
    except Exception as e:
        raise MigrationError(f"Failed connecting to SQLite DB at {paths.db_file_path}: {e}") from e

    try:
        with conn:
            _ensure_base_tables(conn)
            _seed_app_info(conn)
            from_v, to_v = _apply_migrations(conn)
    except Exception as e:
        raise MigrationError(
            f"Failed initializing/migrating DB at {paths.db_file_path}: {e}"
        ) from e
    finally:
        conn.close()

    logger.info("SQLiteInitAndMigrateFlow done: user_version %s -> %s", from_v, to_v)
    return paths
