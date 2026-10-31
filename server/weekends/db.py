"""SQLite storage. Secrets (session tokens, login nonces, login codes) are
stored only as hashes, so a leaked database file does not let anyone in."""
from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id     INTEGER PRIMARY KEY,
    lang        TEXT    NOT NULL DEFAULT 'en',
    status      TEXT    NOT NULL CHECK (status IN ('active', 'waitlist')),
    paused      INTEGER NOT NULL DEFAULT 0,
    settings    TEXT    NOT NULL DEFAULT '',
    version     INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS users_status ON users (status, created_at);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  BLOB    PRIMARY KEY,
    chat_id     INTEGER NOT NULL REFERENCES users (chat_id) ON DELETE CASCADE,
    created_at  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_chat ON sessions (chat_id);

CREATE TABLE IF NOT EXISTS logins (
    nonce_hash  BLOB    PRIMARY KEY,
    secret_hash BLOB    NOT NULL,
    chat_id     INTEGER,
    code_hash   BLOB,
    lang        TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        con = sqlite3.connect(self.path, timeout=10)
        try:
            con.executescript(SCHEMA)
        finally:
            con.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One serialised transaction. The app is small; a global lock keeps
        SQLite free of 'database is locked' errors under concurrent requests."""
        with self._lock:
            con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            try:
                con.row_factory = sqlite3.Row
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA foreign_keys=ON")
                con.execute("PRAGMA secure_delete=ON")
                con.execute("BEGIN IMMEDIATE")
                try:
                    yield con
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
                con.execute("COMMIT")
            finally:
                con.close()

    def cleanup(self) -> None:
        now = int(time.time())
        with self.tx() as con:
            con.execute("DELETE FROM logins WHERE expires_at < ?", (now,))
            con.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
