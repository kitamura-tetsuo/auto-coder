"""Read-only preflight for retaining an existing OAuth authority."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from .oauth_config import OAuthSettings
from .oauth_store import SCHEMA


def validate_retained_oauth(settings: OAuthSettings) -> None:
    """Refuse missing, incomplete, mismatched or migration-requiring state."""
    path = Path(settings.database_path)
    wal = Path(str(path) + "-wal")
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or wal.is_symlink() or (wal.exists() and wal.stat().st_size != 0):
        raise ValueError("retained OAuth authority or its journal is unavailable")
    before = path.stat()
    with closing(sqlite3.connect(":memory:")) as expected:
        expected.executescript(SCHEMA)
        tables = [row[0] for row in expected.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        schemas = {name: tuple(expected.execute(f"PRAGMA table_info({name})")) for name in tables}
        index_names = {row[0] for row in expected.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
        indexes = {name: tuple(expected.execute(f"PRAGMA index_info({name})")) for name in index_names}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5)) as connection:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("retained OAuth integrity is unavailable")
        existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        actual_indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
        if not set(tables) <= existing or not set(indexes) <= actual_indexes:
            raise ValueError("retained OAuth schema is incomplete")
        for name, schema in schemas.items():
            if tuple(connection.execute(f"PRAGMA table_info({name})")) != schema:
                raise ValueError("retained OAuth schema requires migration")
        for name, schema in indexes.items():
            if tuple(connection.execute(f"PRAGMA index_info({name})")) != schema:
                raise ValueError("retained OAuth index differs")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("retained OAuth references are inconsistent")
        identity = connection.execute("SELECT issuer,resource,project FROM authority WHERE singleton=1").fetchall()
        if identity != [(settings.canonical_issuer, settings.resource, settings.firebase_project_id)]:
            raise ValueError("retained OAuth authority differs")
        for table in ("access_tokens", "refresh_tokens"):
            if connection.execute(f"SELECT 1 FROM {table} WHERE scope IS NULL LIMIT 1").fetchone() is not None:
                raise ValueError("retained OAuth tokens require migration")
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or wal.is_symlink() or (wal.exists() and wal.stat().st_size != 0):
        raise ValueError("retained OAuth authority changed during validation")
