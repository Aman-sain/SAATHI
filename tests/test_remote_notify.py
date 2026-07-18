"""Remote push tests (D-005 relay + D-008 alert-v1 flow): inert unless
configured, first page at active-L3, re-pages every REMOTE_NOTIFY_REPEAT_S
while unacknowledged (capped, stopped by ack/resolve, latest text wins),
notification carries Click + Acknowledge action URLs built from HUB_LAN_IP,
failures are log-only (§16), payload is the facts allowlist, and the real
urllib path works against a local stdlib HTTP server — no network touched."""

import asyncio
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from fastapi.testclient import TestClient

from app.bus import EventBus
from app.config import Settings
from app.domain import Alert
from app.main import create_app
from app.notify import remote
from app.notify.remote import RemoteNotifier, build_request


def _settings(tmp_path=None, **kw) -> Settings:
    if tmp_path is not None:
        kw.setdefault("db_path", tmp_path / "saathi.db")
        kw.setdefault("log_dir", tmp_path / "logs")
    return Settings(_env_file=None, **kw)


def _alert(id="a-0001", state="ESCALATED", level=3, synthetic=False, **kw) -> Alert:
    now = time.time()
    return Alert(
        id=id, kind="GAS", level=level, state=state, title="Gas emergency",
        message="Gas level critical", created_ts=now, updated_ts=now,
        synthetic=synthetic, **kw,
    )


async def _until(predicate, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def _remote_settings(**kw) -> Settings:
    kw.setdefault("remote_notify_url", "http://127.0.0.1:9")
    kw.setdefault("remote_notify_topic", "demo")
    return _settings(**kw)


# --- configuration gate ---


def test_off_by_default_and_needs_both_keys():
    assert _settings().remote_notify_configured is False
    assert _settings(remote_notify_url="http://x").remote_notify_configured is False
    assert _settings(remote_notify_topic="t").remote_notify_configured is False
    assert _remote_settings().remote_notify_configured is True


def test_app_does_not_start_notifier_when_unconfigured(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.app.state.notifier is None


def test_app_starts_notifier_when_configured(tmp_path):
    settings = _settings(
        tmp_path, remote_notify_url="http://127.0.0.1:9", remote_notify_topic="demo"
    )
    with TestClient(create_app(settings)) as client:
        assert client.app.state.notifier is not None


# --- firing rules (network stubbed out) ---


def _run_with_notifier(events, fake_post, settings=None, settle_s=0.15,
                       between_s=0.0):
    """Start a RemoteNotifier on a fresh bus, publish `events`, return sent bodies."""
    calls = []

    async def scenario():
        bus = EventBus()
        notifier = RemoteNotifier(bus, settings or _remote_settings())
        await notifier.start()
        for topic, alert in events:
            bus.publish(topic, alert)
            if between_s:
                await asyncio.sleep(between_s)
        # drain: pumps + to_thread hops need real loop turns
        await asyncio.sleep(settle_s)
        await notifier.stop()

    def spy(req, timeout_s):
        calls.append(req)
        return fake_post(req, timeout_s)

    original = remote._post
    remote._post = spy
    try:
        asyncio.run(scenario())
    finally:
        remote._post = original
    return calls


def test_immediate_updates_do_not_double_page():
    """LLM-upgrade broadcasts must not re-fire instantly — only the re-page
    timer (90 s default, far beyond this test) sends again (D-008)."""
    escalated = _alert(state="ESCALATED")
    upgraded = escalated.model_copy(update={"message": "LLM rewrote this"})
    calls = _run_with_notifier(
        [("alert.updated", escalated), ("alert.updated", upgraded)],
        lambda req, t: 200,
    )
    assert len(calls) == 1


def test_fires_on_alert_created_at_level_3():
    calls = _run_with_notifier(
        [("alert.created", _alert(state="OPEN", level=3))], lambda req, t: 200
    )
    assert len(calls) == 1


def test_ignores_sub_l3_and_inactive_states():
    calls = _run_with_notifier(
        [
            ("alert.created", _alert(id="a-l2", state="OPEN", level=2)),
            ("alert.updated", _alert(id="a-l2", state="ANNOUNCED", level=2)),
            ("alert.updated", _alert(id="a-done", state="ACKED", level=3)),
            ("alert.updated", _alert(id="a-res", state="RESOLVED", level=3)),
        ],
        lambda req, t: 200,
    )
    assert calls == []


def test_send_failure_is_log_only_and_pump_survives():
    def flaky(req, t):
        if "[a-boom]" in req.data.decode():
            raise OSError("relay unreachable")
        return 200

    calls = _run_with_notifier(
        [
            ("alert.updated", _alert(id="a-boom")),
            ("alert.updated", _alert(id="a-next")),
        ],
        flaky,
    )
    # both attempted (failure didn't kill the pump), failed one NOT retried
    assert len(calls) == 2


# --- D-008: re-paging until acknowledged ---


def test_repages_until_cap():
    settings = _remote_settings(remote_notify_repeat_s=0.05, remote_notify_max_pages=3)
    calls = _run_with_notifier(
        [("alert.updated", _alert())], lambda req, t: 200,
        settings=settings, settle_s=0.5,
    )
    assert len(calls) == 3  # first page + re-pages, capped at max_pages TOTAL


def test_ack_stops_repaging():
    settings = _remote_settings(remote_notify_repeat_s=0.05, remote_notify_max_pages=50)
    calls = _run_with_notifier(
        [("alert.updated", _alert()), ("alert.updated", _alert(state="ACKED"))],
        lambda req, t: 200,
        settings=settings, between_s=0.12, settle_s=0.4,
    )
    # pages happen only in the 0.12 s before the ack; the 0.4 s after is silent
    # (an unstopped repager at 0.05 s cadence would have sent ~10 by now)
    assert 1 <= len(calls) <= 4


def test_repage_carries_latest_llm_text():
    settings = _remote_settings(remote_notify_repeat_s=0.05, remote_notify_max_pages=5)
    upgraded = _alert().model_copy(update={"message": "LLM rewrote this calmly"})
    calls = _run_with_notifier(
        [("alert.updated", _alert()), ("alert.updated", upgraded)],
        lambda req, t: 200,
        settings=settings, settle_s=0.3,
    )
    assert len(calls) >= 2
    assert "LLM rewrote this calmly" in calls[-1].data.decode()


def test_only_first_page_is_max_priority():
    """Insistent alarm applies to max priority only — re-pages must nudge, not
    stack extra eternal ringers (3 stacked alarms observed live 2026-07-12)."""
    settings = _remote_settings()
    first = build_request(settings, _alert(), page=1)
    reminder = build_request(settings, _alert(), page=3)
    assert first.get_header("X-priority") == "urgent"
    assert reminder.get_header("X-priority") == "high"
    assert reminder.get_header("X-title") == "(reminder 3) Gas emergency"


def test_repeat_zero_pages_exactly_once():
    settings = _remote_settings(remote_notify_repeat_s=0, remote_notify_max_pages=5)
    calls = _run_with_notifier(
        [("alert.updated", _alert())], lambda req, t: 200,
        settings=settings, settle_s=0.3,
    )
    assert len(calls) == 1


# --- D-008: the notification is the console entry point ---


def test_notification_carries_click_and_ack_action():
    settings = _remote_settings(hub_lan_ip="192.168.137.1", http_port=8000)
    req = build_request(settings, _alert())
    assert req.get_header("X-click") == "http://192.168.137.1:8000/app"
    actions = req.get_header("X-actions")
    assert ("http, Acknowledge, http://192.168.137.1:8000/api/alerts/a-0001/ack, "
            "method=POST, clear=true") in actions
    assert "view, Open SAATHI, http://192.168.137.1:8000/app" in actions


# --- payload allowlist ---


def test_request_carries_alert_facts_only():
    req = build_request(_remote_settings(remote_notify_url="http://h:1/"), _alert())
    assert req.full_url == "http://h:1/demo"
    assert req.get_method() == "POST"
    body = req.data.decode()
    assert "Gas level critical" in body and "[a-0001] GAS L3" in body
    assert req.get_header("X-title") == "Gas emergency"
    assert req.get_header("X-priority") == "urgent"


def test_synthetic_flag_survives_into_the_push():
    req = build_request(_remote_settings(), _alert(synthetic=True))
    assert req.get_header("X-title") == "[SYNTHETIC] Gas emergency"


# --- real urllib POST against a local stdlib server (no network) ---


def test_real_post_to_local_ntfy_shaped_server():
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append({
                "path": self.path,
                "title": self.headers.get("X-Title"),
                "body": self.rfile.read(length).decode("utf-8"),
            })
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):  # keep pytest output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        settings = _settings(
            remote_notify_url=f"http://127.0.0.1:{server.server_address[1]}",
            remote_notify_topic="saathi-test",
        )

        async def scenario():
            bus = EventBus()
            notifier = RemoteNotifier(bus, settings)
            await notifier.start()
            bus.publish("alert.updated", _alert(id="a-live"))
            await _until(lambda: received)
            await notifier.stop()

        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()

    assert received[0]["path"] == "/saathi-test"
    assert received[0]["title"] == "Gas emergency"
    assert "[a-live] GAS L3" in received[0]["body"]
