"""Every SQL statement in the project lives here (§5 placement rule).

Writes go through the Database writer queue (fire-and-forget); reads hit the
file directly — call db.flush() first when read-your-write matters.
"""

from __future__ import annotations

import json
import time
from typing import Any

from app.domain import Alert, Telemetry
from app.storage.db import Database

TELEMETRY_KEEP_S = 24 * 3600  # §10 retention: telemetry >24 h pruned
EVENTS_KEEP_S = 7 * 24 * 3600  # §10 retention: events >7 days pruned


class Repo:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- events ---

    def insert_event(
        self,
        *,
        ts: float,
        source: str,
        type: str,
        payload: dict[str, Any] | None = None,
        synthetic: bool = False,
    ) -> None:
        self._db.execute(
            "INSERT INTO events(ts, source, type, payload, synthetic) VALUES(?,?,?,?,?)",
            (ts, source, type, json.dumps(payload or {}), int(synthetic)),
        )

    def list_events(self, since_ts: float = 0.0, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT ts, source, type, payload, synthetic FROM events"
            " WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
            (since_ts, limit),
        )
        return [
            {**dict(r), "payload": json.loads(r["payload"]), "synthetic": bool(r["synthetic"])}
            for r in rows
        ]

    # --- telemetry ---

    def insert_telemetry(self, t: Telemetry) -> None:
        self._db.execute(
            "INSERT INTO telemetry(ts, node_id, gas_norm, temp_c, motion, sound_rms)"
            " VALUES(?,?,?,?,?,?)",
            (t.ts, t.node_id, t.gas_norm, t.temp_c, int(t.motion), t.sound_rms),
        )

    def insert_telemetry_batch(self, batch: list[Telemetry]) -> None:
        """§6.3: ingest buffers 2 s of telemetry, lands it as one write."""
        self._db.execute_many(
            "INSERT INTO telemetry(ts, node_id, gas_norm, temp_c, motion, sound_rms)"
            " VALUES(?,?,?,?,?,?)",
            [(t.ts, t.node_id, t.gas_norm, t.temp_c, int(t.motion), t.sound_rms)
             for t in batch],
        )

    # --- alerts ---

    def upsert_alert(self, a: Alert) -> None:
        """Insert or update-in-place: the §6.6 state machine persists every transition."""
        self._db.execute(
            "INSERT INTO alerts(id, kind, level, state, title, message, message_engine,"
            " facts, synthetic, created_ts, updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, level=excluded.level,"
            " state=excluded.state, title=excluded.title, message=excluded.message,"
            " message_engine=excluded.message_engine, facts=excluded.facts,"
            " synthetic=excluded.synthetic, updated_ts=excluded.updated_ts",
            (
                a.id, a.kind, a.level, a.state, a.title, a.message, a.message_engine,
                json.dumps(a.facts), int(a.synthetic), a.created_ts, a.updated_ts,
            ),
        )

    def get_alert(self, alert_id: str) -> Alert | None:
        rows = self._db.query("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        return _row_to_alert(rows[0]) if rows else None

    def list_alerts(self, limit: int = 20, undelivered: bool = False) -> list[Alert]:
        where = "WHERE delivered = 0 " if undelivered else ""
        rows = self._db.query(
            f"SELECT * FROM alerts {where}ORDER BY created_ts DESC LIMIT ?", (limit,)
        )
        return [_row_to_alert(r) for r in rows]

    def mark_alert_delivered(self, alert_id: str) -> None:
        """Contract #3 `undelivered` filter: set once a caregiver WS received it."""
        self._db.execute(
            "UPDATE alerts SET delivered = 1 WHERE id = ?", (alert_id,)
        )

    def list_active_alerts(self) -> list[Alert]:
        """§14 restart semantics: the engine re-adopts these on startup."""
        rows = self._db.query(
            "SELECT * FROM alerts WHERE state IN ('OPEN','ANNOUNCED','ESCALATED')"
            " ORDER BY created_ts"
        )
        return [_row_to_alert(r) for r in rows]

    # --- digests ---

    def insert_digest(self, *, date: str, text: str, engine: str, created_ts: float) -> None:
        self._db.execute(
            "INSERT INTO digests(date, text, engine, created_ts) VALUES(?,?,?,?)",
            (date, text, engine, created_ts),
        )

    # --- retention (§10) ---

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._db.execute("DELETE FROM telemetry WHERE ts < ?", (now - TELEMETRY_KEEP_S,))
        self._db.execute("DELETE FROM events WHERE ts < ?", (now - EVENTS_KEEP_S,))


def _row_to_alert(r) -> Alert:
    return Alert(
        id=r["id"],
        kind=r["kind"],
        level=r["level"],
        state=r["state"],
        title=r["title"],
        message=r["message"],
        message_engine=r["message_engine"],
        facts=json.loads(r["facts"]),
        synthetic=bool(r["synthetic"]),
        created_ts=r["created_ts"],
        updated_ts=r["updated_ts"],
    )
