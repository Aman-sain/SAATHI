"""§10/§6.14 storage tests: WAL on, writer-thread round-trip, RESTART-SAFE DB
(Phase 1 completion criterion), a bad write never kills the writer, §10 pruning."""

import time

import pytest

from app.domain import Alert, Telemetry
from app.storage.db import Database
from app.storage.repo import Repo


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "saathi.db")
    database.start()
    yield database
    database.stop()


def _alert(alert_id="a-7f3c", state="OPEN") -> Alert:
    return Alert(
        id=alert_id, kind="GAS", level=2, state=state, title="Gas warning",
        message="Gas levels rising in the kitchen.", message_engine="template",
        facts={"gas_norm": 0.62, "last_motion_s": 540}, created_ts=100.0, updated_ts=100.0,
    )


def test_wal_mode_is_enabled(db):
    assert db.query("PRAGMA journal_mode")[0][0] == "wal"


def test_alert_round_trips_through_writer_thread(db):
    repo = Repo(db)
    repo.upsert_alert(_alert())
    db.flush()
    assert repo.get_alert("a-7f3c") == _alert()


def test_upsert_updates_state_in_place(db):
    repo = Repo(db)
    repo.upsert_alert(_alert())
    updated = _alert(state="ESCALATED").model_copy(update={"level": 3, "updated_ts": 130.0})
    repo.upsert_alert(updated)
    db.flush()
    got = repo.get_alert("a-7f3c")
    assert got.state == "ESCALATED" and got.level == 3
    assert len(repo.list_alerts()) == 1


def test_db_is_restart_safe(tmp_path):
    path = tmp_path / "saathi.db"
    first = Database(path)
    first.start()
    Repo(first).upsert_alert(_alert())
    first.flush()
    first.stop()

    second = Database(path)  # simulated hub restart on the same file
    second.start()
    try:
        assert Repo(second).get_alert("a-7f3c") == _alert()
    finally:
        second.stop()


def test_writer_survives_a_broken_statement(db):
    repo = Repo(db)
    db.execute("TOTALLY BOGUS SQL")
    repo.insert_event(ts=1.0, source="fusion", type="R_GAS_FIRED", payload={"gas_norm": 0.62})
    db.flush()  # would TimeoutError if the writer thread had died
    events = repo.list_events()
    assert len(events) == 1 and events[0]["payload"] == {"gas_norm": 0.62}


def test_events_filtered_and_newest_first(db):
    repo = Repo(db)
    for ts in (10.0, 20.0, 30.0):
        repo.insert_event(ts=ts, source="node1", type="MOTION")
    db.flush()
    got = repo.list_events(since_ts=15.0)
    assert [e["ts"] for e in got] == [30.0, 20.0]


def test_prune_enforces_section_10_retention(db):
    repo = Repo(db)
    now = time.time()
    old_t = Telemetry(node_id="node1", ts=now - 25 * 3600, gas_raw=300, gas_norm=0.1,
                      temp_c=30.0, motion=False, sound_rms=0.05, fw="1.0")
    fresh_t = old_t.model_copy(update={"ts": now - 3600})
    repo.insert_telemetry(old_t)
    repo.insert_telemetry(fresh_t)
    repo.insert_event(ts=now - 8 * 24 * 3600, source="node1", type="NODE_BOOT")
    repo.insert_event(ts=now - 3600, source="node1", type="MOTION")
    repo.prune(now)
    db.flush()
    assert [r["ts"] for r in db.query("SELECT ts FROM telemetry")] == [fresh_t.ts]
    assert len(repo.list_events()) == 1
