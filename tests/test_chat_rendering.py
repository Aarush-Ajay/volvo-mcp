"""Tests for the transcript rendering in chat.py.

The Gemini MCP adapter does not hand back the tool's JSON. It stringifies the
whole ``CallToolResult`` and passes the repr, so the chatbot has to fish the
payload back out. That parsing is fiddly enough to be worth pinning.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chat  # noqa: E402


def as_adapter_sees_it(payload: dict, *, structured: bool = False) -> dict:
    """Wrap a tool payload the way the Gemini MCP adapter delivers it."""
    result = CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))],
        **({"structuredContent": payload} if structured else {}),
    )
    return {"result": str(result)}


PAYLOADS = [
    pytest.param({"range_km": 111.9, "model": "Volvo FH Electric"}, id="range"),
    pytest.param({"source": "offline_seed", "stations": [{"n": 1}]}, id="seed"),
    pytest.param({"error": "unknown model 'FX'"}, id="error"),
    pytest.param({"feasible_without_stopping": False}, id="trip"),
    # Spec `source` strings really do contain quotes, commas and slashes.
    pytest.param(
        {"note": "quotes ' and \" , commas, / slashes {braces}"}, id="awkward-chars"
    ),
]


@pytest.mark.parametrize("payload", PAYLOADS)
@pytest.mark.parametrize("structured", [False, True], ids=["plain", "structured"])
def test_unwrap_recovers_the_payload(payload: dict, structured: bool) -> None:
    assert chat._unwrap(as_adapter_sees_it(payload, structured=structured)) == payload


def test_first_json_object_stops_at_the_first_balanced_object() -> None:
    """A second object may follow; taking the last brace would break parsing."""
    text = 'junk {"a": {"b": 1}} trailing {"c": 2} more'
    assert chat._first_json_object(text) == '{"a": {"b": 1}}'


def test_first_json_object_ignores_braces_inside_strings() -> None:
    assert chat._first_json_object('{"a": "} not the end {"}') == '{"a": "} not the end {"}'


def test_unwrap_returns_none_when_there_is_no_json() -> None:
    assert chat._unwrap({"result": "no payload here"}) is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"range_km": 111.9}, "range=111.9 km"),
        ({"source": "offline_seed", "stations": [1, 2]}, "offline_seed"),
        ({"error": "boom"}, "boom"),
        ({"feasible_without_stopping": True}, "feasible=True"),
    ],
)
def test_summarise_mentions_the_salient_field(payload: dict, expected: str) -> None:
    assert expected in chat._summarise(as_adapter_sees_it(payload))


def test_summarise_flags_offline_seed_in_yellow() -> None:
    """The colour is load-bearing - it is how a stale answer is spotted."""
    summary = chat._summarise(as_adapter_sees_it({"source": "offline_seed"}))
    assert "[yellow]" in summary


def test_summarise_prints_the_real_consumption() -> None:
    """The model has invented a consumption figure before; print the true one."""
    summary = chat._summarise(as_adapter_sees_it(
        {"effective_consumption": 1.4801, "consumption_unit": "kWh/km"}
    ))
    assert "1.4801 kWh/km" in summary


ESTIMATE_ASSUMPTIONS = {
    "level_pct": 40.0,
    "gcw_tonnes": 38.0,
    "reference_gcw_tonnes": 40,
    "terrain": "hilly",
    "ambient_c": 2.0,
    "reserve_fraction": 0.1,
    "payload_factor": 0.97,
}


def test_assumptions_line_renders_every_dispatcher_field() -> None:
    line = chat._assumptions_line(
        as_adapter_sees_it({"range_km": 111.9, "assumptions": ESTIMATE_ASSUMPTIONS})
    )
    assert line == "40%, 38 t, hilly, 2 C, 10% reserve"


def test_assumptions_line_drops_pointless_decimals() -> None:
    """40.0% reads as noise; 40% reads as a fact."""
    line = chat._assumptions_line(
        as_adapter_sees_it({"assumptions": {"level_pct": 40.0, "ambient_c": 2.0}})
    )
    assert line == "40%, 2 C"


def test_assumptions_line_keeps_real_decimals() -> None:
    line = chat._assumptions_line(
        as_adapter_sees_it({"assumptions": {"gcw_tonnes": 38.5, "ambient_c": -7.5}})
    )
    assert line == "38.5 t, -7.5 C"


def test_assumptions_line_skips_absent_fields() -> None:
    line = chat._assumptions_line(
        as_adapter_sees_it({"assumptions": {"terrain": "flat"}})
    )
    assert line == "flat"


@pytest.mark.parametrize(
    "payload",
    [{"range_km": 111.9}, {"assumptions": "not a dict"}, {"error": "boom"}],
    ids=["no-assumptions", "wrong-type", "error"],
)
def test_assumptions_line_absent_when_there_is_nothing_to_show(payload: dict) -> None:
    assert chat._assumptions_line(as_adapter_sees_it(payload)) is None
