"""Central SQLite database for all persistent state."""

from __future__ import annotations

import sqlite3
import threading
from typing import Optional

SCHEMA_VERSION = 3

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS sessions (
    name        TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,
    pid         INTEGER,
    session_id  TEXT,
    status      TEXT NOT NULL DEFAULT 'stopped',
    created_at  REAL NOT NULL DEFAULT 0.0,
    ticket_id   TEXT,
    cwd         TEXT
);

CREATE TABLE IF NOT EXISTS tracked_prs (
    key              TEXT PRIMARY KEY,
    repo             TEXT NOT NULL,
    number           INTEGER NOT NULL,
    branch           TEXT NOT NULL DEFAULT '',
    last_issue       TEXT NOT NULL DEFAULT '',
    last_action      TEXT NOT NULL DEFAULT '',
    last_action_time REAL NOT NULL DEFAULT 0.0,
    claude_pid       INTEGER,
    ci_was_failing   INTEGER NOT NULL DEFAULT 0,
    slack_sent       INTEGER NOT NULL DEFAULT 0,
    watched          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pr_reviews (
    key         TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,
    number      INTEGER NOT NULL,
    head_sha    TEXT NOT NULL,
    reviewed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS session_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    tag          TEXT NOT NULL,
    text         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_session ON session_history(session_name);

CREATE TABLE IF NOT EXISTS session_summaries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    entry_type   TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summary_session ON session_summaries(session_name);
"""

_MIGRATIONS = {
    2: """\
CREATE TABLE IF NOT EXISTS pr_reviews (
    key         TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,
    number      INTEGER NOT NULL,
    head_sha    TEXT NOT NULL,
    reviewed_at REAL NOT NULL
);
""",
    3: "ALTER TABLE tracked_prs ADD COLUMN watched INTEGER NOT NULL DEFAULT 0;",
}


class Database:
    """Thread-safe SQLite wrapper with WAL mode."""

    def __init__(self, db_path: str):
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        self._ensure_schema()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, params_list: list) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.executemany(sql, params_list)
            self._conn.commit()
            return cur

    def fetchone(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def execute_script(self, sql: str):
        """Execute multiple SQL statements as a script (no params)."""
        with self._lock:
            self._conn.executescript(sql)

    def _ensure_schema(self):
        row = None
        try:
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        except sqlite3.OperationalError:
            pass

        if row is None:
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            self._conn.commit()
        else:
            current = row["version"]
            if current < SCHEMA_VERSION:
                for v in range(current + 1, SCHEMA_VERSION + 1):
                    if v in _MIGRATIONS:
                        self._conn.executescript(_MIGRATIONS[v])
                self._conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
                self._conn.commit()

    def close(self):
        self._conn.close()
