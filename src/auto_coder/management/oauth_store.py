"""Durable SQLite storage used exclusively by the management OAuth service."""

import sqlite3
from pathlib import Path

from .oauth_config import OAuthSettings

SCHEMA = """
CREATE TABLE IF NOT EXISTS authority (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), issuer TEXT NOT NULL,
 resource TEXT NOT NULL, project TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending (
 id TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL,
 resource TEXT NOT NULL, scope TEXT NOT NULL, state TEXT, challenge TEXT NOT NULL,
 browser_hash TEXT NOT NULL, csrf_hash TEXT NOT NULL, expires REAL NOT NULL,
 status TEXT NOT NULL, project TEXT, uid TEXT, auth_time REAL);
CREATE TABLE IF NOT EXISTS codes (
 hash TEXT PRIMARY KEY, pending_id TEXT NOT NULL, client_id TEXT NOT NULL,
 redirect_uri TEXT NOT NULL, resource TEXT NOT NULL, scope TEXT NOT NULL,
 challenge TEXT NOT NULL, project TEXT NOT NULL, uid TEXT NOT NULL,
 auth_time REAL NOT NULL, expires REAL NOT NULL, grant_expires REAL NOT NULL,
 consumed REAL);
CREATE TABLE IF NOT EXISTS grants (
 id TEXT PRIMARY KEY, client_id TEXT NOT NULL, resource TEXT NOT NULL,
 scope TEXT NOT NULL, project TEXT NOT NULL, uid TEXT NOT NULL,
 auth_time REAL NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
 revoked REAL, revoke_reason TEXT);
CREATE TABLE IF NOT EXISTS access_tokens (hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES grants(id), expires REAL NOT NULL, scope TEXT);
CREATE TABLE IF NOT EXISTS refresh_tokens (hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES grants(id), expires REAL NOT NULL, spent REAL, replaced_by TEXT, scope TEXT);
CREATE INDEX IF NOT EXISTS access_grant ON access_tokens(grant_id);
CREATE INDEX IF NOT EXISTS refresh_grant ON refresh_tokens(grant_id);
"""


class OAuthStore:
    def __init__(self, settings: OAuthSettings) -> None:
        self.path = Path(settings.database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = self.connect()
            connection.executescript(SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(access_tokens)")}
            if "scope" not in columns:
                connection.execute("ALTER TABLE access_tokens ADD COLUMN scope TEXT")
            connection.execute("UPDATE access_tokens SET scope=(SELECT scope FROM grants WHERE grants.id=access_tokens.grant_id) WHERE scope IS NULL")
            refresh_columns = {row[1] for row in connection.execute("PRAGMA table_info(refresh_tokens)")}
            if "scope" not in refresh_columns:
                connection.execute("ALTER TABLE refresh_tokens ADD COLUMN scope TEXT")
            connection.execute("UPDATE refresh_tokens SET scope=(SELECT scope FROM grants WHERE grants.id=refresh_tokens.grant_id) WHERE scope IS NULL")
            row = connection.execute("SELECT issuer,resource,project FROM authority WHERE singleton=1").fetchone()
            identity = (settings.canonical_issuer, settings.resource, settings.firebase_project_id)
            if row is None:
                connection.execute("INSERT INTO authority VALUES(1,?,?,?)", identity)
                connection.commit()
            elif tuple(row) != identity:
                raise ValueError("OAuth database belongs to another authority")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("OAuth database integrity check failed")
            connection.close()
        except (sqlite3.Error, OSError):
            # Never replace, truncate, or silently recreate an unreadable authority.
            raise

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection
