"""SQLite-хранилище сессий, сообщений, ходов, трасс и заявок (S2–S4).

Одна база-файл, WAL, версионированные миграции. Все записи идут через
одно соединение под блокировкой — для одного процесса uvicorn этого
достаточно; для нескольких воркеров см. docs/backend.md.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        client_token_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active','closed')),
        channel TEXT NOT NULL DEFAULT 'web',
        client_context_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        closed_at TEXT,
        close_reason TEXT
    );
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id),
        seq INTEGER NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('user','assistant')),
        content TEXT NOT NULL,
        request_id TEXT NOT NULL,
        in_context INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        UNIQUE (session_id, seq),
        UNIQUE (session_id, request_id, role)
    );
    CREATE TABLE turns (
        session_id TEXT NOT NULL REFERENCES sessions(id),
        request_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending','completed','failed','cancelled')),
        user_text TEXT NOT NULL,
        response_json TEXT,
        error_code TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        finished_at TEXT,
        attempts INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (session_id, request_id)
    );
    CREATE TABLE traces (
        trace_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(id),
        request_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        status TEXT NOT NULL,
        data_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (session_id, request_id, attempt)
    );
    CREATE TABLE leads (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
        status TEXT NOT NULL CHECK (status IN ('draft','saved')),
        contact_json TEXT NOT NULL,
        callback_requested INTEGER NOT NULL DEFAULT 0,
        summary_json TEXT,
        revision INTEGER NOT NULL DEFAULT 0,
        idempotency_key TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        saved_at TEXT
    );
    CREATE INDEX idx_messages_session ON messages(session_id, seq);
    CREATE INDEX idx_traces_session ON traces(session_id, request_id);
    """,
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    # ------------------------------------------------------------------
    def migrate(self) -> int:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = self._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] or 0
            for i, sql in enumerate(MIGRATIONS[current:], start=current + 1):
                with self.tx():
                    for stmt in [s for s in sql.split(";") if s.strip()]:
                        self._conn.execute(stmt)
                    self._conn.execute("INSERT INTO schema_version(version) VALUES (?)", (i,))
            return len(MIGRATIONS)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(s: Optional[str]) -> Any:
    return json.loads(s) if s else None
