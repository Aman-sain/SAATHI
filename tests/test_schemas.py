"""§19 test_schemas — every frozen JSON example in docs/CONTRACTS.md must validate
against the domain models AND round-trip unchanged (node code and frontends are
built against those exact shapes), and contract violations must be rejected."""

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain import Alert, ASREvent, Digest, NodeEvent, Telemetry, VisionEvent

REPO_ROOT = Path(__file__).resolve().parents[1]
_TEXT = (REPO_ROOT / "docs" / "CONTRACTS.md").read_text(encoding="utf-8")
EXAMPLES = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", _TEXT, re.DOTALL)]


def example(*keys: str) -> dict:
    """Find a CONTRACTS.md example by its signature keys (robust to reordering)."""
    for ex in EXAMPLES:
        if set(keys) <= set(ex):
            return ex
    raise AssertionError(f"no CONTRACTS.md example containing {keys}")


def test_telemetry_example_validates_and_round_trips():
    ex = example("gas_raw", "gas_norm", "temp_c")
    assert Telemetry.model_validate(ex).model_dump() == ex


def test_node_event_example_validates_and_round_trips():
    ex = example("node_id", "type", "value")
    assert NodeEvent.model_validate(ex).model_dump() == ex


def test_alert_example_validates_and_round_trips():
    ex = example("kind", "level", "state")
    assert Alert.model_validate(ex).model_dump() == ex


def test_error_schema_example_has_frozen_shape():
    err = example("error")["error"]
    assert {"code", "message", "request_id"} <= set(err)


@pytest.mark.parametrize(
    "model, example_keys, patch",
    [
        (Telemetry, ("gas_raw",), {"gas_norm": 1.5}),
        (Telemetry, ("gas_raw",), {"sound_rms": -0.1}),
        (Telemetry, ("gas_raw",), {"gas_raw": "many"}),
        (NodeEvent, ("type", "value"), {"type": "SMOKE"}),
        (Alert, ("kind",), {"state": "REOPENED"}),
        (Alert, ("kind",), {"level": 4}),
    ],
)
def test_contract_violations_rejected(model, example_keys, patch):
    bad = {**example(*example_keys), **patch}
    with pytest.raises(ValidationError):
        model.model_validate(bad)


def test_internal_event_models_construct():
    assert VisionEvent(ts=1.0, conf=0.9).kind == "FALL"
    assert ASREvent(ts=1.0, kind="HELP", keyword="bachao", conf=0.8).keyword == "bachao"
    assert Digest(date="2026-07-19", text="quiet day", engine="mock", created_ts=2.0).engine == "mock"
    with pytest.raises(ValidationError):
        Digest(date="19-07-2026", text="x", engine="mock", created_ts=2.0)
