"""Phase 4 LLM tests: write_alert returns validated text on success and None
(= keep template) on timeout/garbage/HTTP errors with exactly one retry (§6.11);
output validation collapses/caps/rejects (§11.4); the upgrader fires once per
L3-active template alert and applies text via the injected update callback,
and a dead LLM leaves the template untouched (the completion criterion)."""

import asyncio
import json
import time

import httpx

from app.bus import EventBus
from app.config import Settings
from app.domain import Alert
from app.pipelines.llm import SYSTEM_ALERT, LlmClient, LlmUpgrader, _validate


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def _alert(id="a-0001", state="ESCALATED", level=3, engine="template") -> Alert:
    now = time.time()
    return Alert(
        id=id, kind="GAS", level=level, state=state, title="Gas emergency",
        message="[GAS] Level 3. Gas level 0.72 above threshold; last motion 45 s ago.",
        message_engine=engine, facts={"gas_norm": 0.72}, created_ts=now, updated_ts=now,
    )


def _chat_response(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def _client_with(handler) -> LlmClient:
    return LlmClient(_settings(), transport=httpx.MockTransport(handler))


async def _started(client: LlmClient) -> LlmClient:
    await client.start()
    return client


# --- output validation (§11.4) ---


def test_validate_collapses_caps_and_rejects():
    assert _validate("  Gas is high.\n\nPlease check.  ", 300) == "Gas is high. Please check."
    assert _validate("x" * 500, 300) == "x" * 300
    assert _validate("", 300) is None
    assert _validate("   \n\t ", 300) is None
    assert _validate(None, 300) is None


def test_validate_strips_markdown_decoration():
    # gemma wrote "**Alert:** …" live 2026-07-12 — PWA renders plain text
    assert _validate("**Alert:** Gas is `high`.", 300) == "Alert: Gas is high."
    assert _validate("## Alert\nGas rose.", 300) == "Alert Gas rose."
    assert _validate("gas_norm stays intact", 300) == "gas_norm stays intact"


# --- write_alert happy path ---


def test_write_alert_returns_llm_text():
    seen = {}

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        seen["body"] = json.loads(request.content)
        return _chat_response("Gas levels rose at home; please check in.")

    async def scenario():
        client = await _started(_client_with(handler))
        text = await client.write_alert(_alert())
        await client.stop()
        return text

    text = asyncio.run(scenario())
    assert text == "Gas levels rose at home; please check in."
    assert seen["body"]["messages"][0]["content"] == SYSTEM_ALERT
    assert seen["body"]["temperature"] == 0.3 and seen["body"]["max_tokens"] == 120
    facts = json.loads(seen["body"]["messages"][1]["content"])
    assert facts["kind"] == "GAS" and facts["sensor_facts"] == {"gas_norm": 0.72}


# --- failure = None = template stays, with exactly one retry ---


def test_write_alert_timeout_returns_none_after_one_retry():
    attempts = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={})
        attempts.append(1)
        raise httpx.ConnectTimeout("llama server dead")

    async def scenario():
        client = await _started(_client_with(handler))
        text = await client.write_alert(_alert())
        healthy = client.healthy
        await client.stop()
        return text, healthy

    text, healthy = asyncio.run(scenario())
    assert text is None
    assert len(attempts) == 2  # first try + exactly one retry (§6.11)
    assert healthy is False


def test_write_alert_garbage_and_empty_return_none():
    responses = iter([
        httpx.Response(200, json={"unexpected": "shape"}),
        _chat_response("   \n  "),
    ])

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={})
        return next(responses)

    async def scenario():
        client = await _started(_client_with(handler))
        text = await client.write_alert(_alert())
        await client.stop()
        return text

    assert asyncio.run(scenario()) is None


def test_probe_failure_marks_unhealthy_but_client_still_tries_later():
    """llama may start after the hub: probe says down, a later call recovers."""
    health = []

    def handler(request):
        if request.url.path.endswith("/models"):
            raise httpx.ConnectError("not up yet")
        return _chat_response("Recovered text.")

    async def scenario():
        client = LlmClient(
            _settings(), on_health=health.append,
            transport=httpx.MockTransport(handler),
        )
        await client.start()
        await client._probe_task  # background probe (§8) settles first
        assert client.healthy is False
        text = await client.write_alert(_alert())
        await client.stop()
        return text

    assert asyncio.run(scenario()) == "Recovered text."
    assert health == ["down", "up"]  # chip always tells the live truth (§26)


# --- upgrader bridge ---


def _run_upgrader(events, handler):
    updates = []

    async def scenario():
        bus = EventBus()
        client = LlmClient(_settings(), transport=httpx.MockTransport(handler))
        await client.start()
        upgrader = LlmUpgrader(
            bus, client, lambda aid, msg, eng: updates.append((aid, msg, eng))
        )
        await upgrader.start()
        for topic, alert in events:
            bus.publish(topic, alert)
        await asyncio.sleep(0.15)  # drain pumps
        await upgrader.stop()
        await client.stop()

    asyncio.run(scenario())
    return updates


def _ok_handler(request):
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={})
    return _chat_response("Calm caregiver text.")


def test_upgrader_fires_on_escalated_template_alert():
    updates = _run_upgrader([("alert.updated", _alert())], _ok_handler)
    assert updates == [("a-0001", "Calm caregiver text.", "local-llm")]


def test_upgrader_fires_on_created_at_level_3():
    updates = _run_upgrader([("alert.created", _alert(state="OPEN"))], _ok_handler)
    assert len(updates) == 1


def test_upgrader_once_per_alert_and_never_on_upgraded_text():
    escalated = _alert()
    announced_l3 = _alert(state="ANNOUNCED")
    upgraded = _alert(engine="local-llm")
    updates = _run_upgrader(
        [
            ("alert.created", escalated),
            ("alert.updated", announced_l3),
            ("alert.updated", upgraded),
        ],
        _ok_handler,
    )
    assert len(updates) == 1


def test_upgrader_ignores_sub_l3_and_final_states():
    updates = _run_upgrader(
        [
            ("alert.created", _alert(id="a-l2", state="OPEN", level=2)),
            ("alert.updated", _alert(id="a-ack", state="ACKED")),
            ("alert.updated", _alert(id="a-res", state="RESOLVED")),
        ],
        _ok_handler,
    )
    assert updates == []


def test_dead_llm_leaves_template_untouched():
    """The Phase-4 completion criterion in miniature: server killed → no update."""

    def dead(request):
        raise httpx.ConnectError("killed")

    updates = _run_upgrader([("alert.updated", _alert())], dead)
    assert updates == []
