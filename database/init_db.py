#!/usr/bin/env python3
"""Initialize SQLite database for database

This script is the CLI boundary for database initialization.

It delegates all schema creation/migration to SQLiteInitAndMigrateFlow
(db_migrations.initialize_and_migrate_sqlite) to avoid duplicated, patchy logic.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sqlite3
import subprocess

from db_migrations import initialize_and_migrate_sqlite


DB_NAME = "myapp.db"
DB_USER = "kaviasqlite"  # Not used for SQLite, but kept for consistency
DB_PASSWORD = "kaviadefaultpassword"  # Not used for SQLite, but kept for consistency
DB_PORT = "5000"  # Not used for SQLite, but kept for consistency


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


def _write_db_visualizer_env(db_path: Path) -> None:
    """Write db_visualizer/sqlite.env for the Node DB viewer."""
    viz_dir = Path("db_visualizer")
    viz_dir.mkdir(parents=True, exist_ok=True)
    env_path = viz_dir / "sqlite.env"
    env_path.write_text(f'export SQLITE_DB="{db_path}"\n', encoding="utf-8")


def _maybe_write_db_connection_txt(canonical_db_path: Path) -> None:
    """
    Ensure db_connection.txt exists and points at the canonical DB file path.

    This script must NOT overwrite an existing db_connection.txt because that file
    is canonical guidance and may be intentionally pinned to a specific path.
    """
    txt_path = Path("db_connection.txt")
    if txt_path.exists():
        return

    connection_string = f"sqlite:///{canonical_db_path}"
    txt_path.write_text(
        "\n".join(
            [
                "# SQLite connection methods:",
                f"# Python: sqlite3.connect('{canonical_db_path.name}')",
                f"# Connection string: {connection_string}",
                f"# File path: {canonical_db_path}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _print_db_stats(db_file: Path) -> None:
    """Print basic DB stats (tables count, app_info count, and schema version)."""
    conn = sqlite3.connect(str(db_file))
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA user_version")
        user_version = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        table_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM app_info")
        record_count = cur.fetchone()[0]

        print("Database statistics:")
        print(f"  user_version: {user_version}")
        print(f"  Tables: {table_count}")
        print(f"  App info records: {record_count}")
    finally:
        conn.close()


def main() -> None:
    """Entrypoint for initializing/migrating the SQLite DB."""
    _configure_logging()
    print("Starting SQLite setup...")

    paths = initialize_and_migrate_sqlite(default_db_name=DB_NAME)

    # If db_connection.txt didn't exist, create it using the resolved canonical path.
    _maybe_write_db_connection_txt(paths.db_file_path.resolve())

    # Keep db_visualizer env aligned with the real DB path.
    _write_db_visualizer_env(paths.db_file_path.resolve())
    print("Environment variables saved to db_visualizer/sqlite.env")

    print("\nSQLite setup complete!")
    print(f"Database: {paths.db_file_path.name}")
    print(f"Location: {paths.db_file_path.resolve()}")
    print("")
    print("To use with Node.js viewer, run: source db_visualizer/sqlite.env")

    print("\nTo connect to the database, use one of the following methods:")
    print(f"1. Python: sqlite3.connect('{paths.db_file_path.name}')")
    print(f"2. Connection string: sqlite:///{paths.db_file_path.resolve()}")
    print(f"3. Direct file access: {paths.db_file_path.resolve()}")
    print("")

    _print_db_stats(paths.db_file_path)

    # If sqlite3 CLI is available, show how to use it
    try:
        result = subprocess.run(["which", "sqlite3"], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            print("")
            print("SQLite CLI is available. You can also use:")
            print(f"  sqlite3 {paths.db_file_path.name}")
    except Exception:
        pass

    print("\nScript completed successfully.")


if __name__ == "__main__":
    main()
