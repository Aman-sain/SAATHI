"""SQLite core (§10): connection, WAL, schema DDL, single-writer thread.

Row SQL lives in repo.py (§5 placement rule) — db.py only owns lifecycle.
Writes are queued to ONE writer thread; reads open short-lived connections,
which WAL allows concurrently with the writer (§6.14).
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

log = logging.getLogger("storage")

# §10 schema verbatim (+ IF NOT EXISTS so re-opening an existing DB is a no-op:
# restart-safety is a Phase 1 completion criterion). Migrations: none, by design.
DDL = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL,
  type TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
  synthetic INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS telemetry(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, node_id TEXT NOT NULL,
  gas_norm REAL, temp_c REAL, motion INTEGER, sound_rms REAL);
CREATE INDEX IF NOT EXISTS idx_tel_ts ON telemetry(ts);

CREATE TABLE IF NOT EXISTS alerts(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, level INTEGER NOT NULL,
  state TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL,
  message_engine TEXT NOT NULL DEFAULT 'template',
  facts TEXT NOT NULL DEFAULT '{}', synthetic INTEGER NOT NULL DEFAULT 0,
  delivered INTEGER NOT NULL DEFAULT 0,
  created_ts REAL NOT NULL, updated_ts REAL NOT NULL);

CREATE TABLE IF NOT EXISTS digests(
  id INTEGER PRIMARY KEY, date TEXT NOT NULL, text TEXT NOT NULL,
  engine TEXT NOT NULL, created_ts REAL NOT NULL);
"""

_STOP = object()


class Database:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._q: queue.Queue[object] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # DDL synchronously, BEFORE the writer starts: reads are valid immediately
        with closing(sqlite3.connect(self._path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(DDL)
        self._thread = threading.Thread(
            target=self._writer_loop, name="db-writer", daemon=True
        )
        self._thread.start()
        log.info("storage up path=%s wal=on", self._path)

    def stop(self) -> None:
        """FIFO queue: the sentinel lands after all pending writes, so stop() drains."""
        if self._thread is None:
            return
        self._q.put(_STOP)
        self._thread.join(timeout=5)
        self._thread = None
        log.info("storage stopped path=%s", self._path)

    def execute(self, sql: str, params: tuple = ()) -> None:
        """Queue a write and return immediately (§6.14 single-writer)."""
        self._q.put((sql, params))

    def execute_many(self, sql: str, seq_of_params: list[tuple]) -> None:
        """Queue a batch as ONE write+commit (§6.3: telemetry batched, 1 write/2 s)."""
        if seq_of_params:
            self._q.put((sql, list(seq_of_params)))

    def flush(self, timeout: float = 5.0) -> None:
        """Block until every previously queued write hit disk (tests, shutdown)."""
        done = threading.Event()
        self._q.put(done)
        if not done.wait(timeout):
            raise TimeoutError("db writer did not drain in time")

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self._path)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()

    def _writer_loop(self) -> None:
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            while True:
                item = self._q.get()
                if item is _STOP:
                    break
                if isinstance(item, threading.Event):
                    item.set()
                    continue
                sql, params = item  # type: ignore[misc]
                try:
                    # a list of param tuples = one batched executemany commit
                    if isinstance(params, list):
                        conn.executemany(sql, params)
                    else:
                        conn.execute(sql, params)
                    conn.commit()
                except Exception:
                    # §16: a broken write must never take down the hub
                    log.exception("write failed, statement dropped sql=%r", sql[:60])
        finally:
            conn.close()
