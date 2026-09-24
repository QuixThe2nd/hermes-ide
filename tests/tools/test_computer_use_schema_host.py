"""The computer_use schema only teaches the Bot Screen handoff (`request_handoff` / `wait_for_human`,
'take over this screen from the Hermes Desktop app') on hosts that can have a Bot Desktop; elsewhere
the model would learn actions that can never succeed."""

from __future__ import annotations

import json

from tools.computer_use.schema import COMPUTER_USE_SCHEMA, schema_for_host

_HANDOFF = ("request_handoff", "wait_for_human")


def _actions(schema) -> list[str]:
    return schema["parameters"]["properties"]["action"]["enum"]


def test_unsupported_host_schema_has_no_handoff_vocabulary():
    schema = schema_for_host(supported=False)
    text = json.dumps(schema)
    assert not any(action in text for action in _HANDOFF), text
    assert "take over this screen" not in text
    assert "reason" not in schema["parameters"]["properties"]  # request_handoff's only parameter
    assert "grace" not in schema["parameters"]["properties"]   # wait_for_human's only parameter
    # Everything else survives untouched.
    assert set(_actions(schema)) == set(_actions(COMPUTER_USE_SCHEMA)) - set(_HANDOFF)
    assert schema["parameters"]["required"] == ["action"]


def test_supported_host_schema_keeps_the_handoff_and_is_the_frozen_schema():
    schema = schema_for_host(supported=True)
    assert set(_HANDOFF) <= set(_actions(schema))
    assert schema is COMPUTER_USE_SCHEMA  # byte-frozen value: no per-call copy on the common path
