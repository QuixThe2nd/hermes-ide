"""The ``manage_connections`` card is one ``connection`` server request per operation
(``tui_gateway/server.py::_connection_request``): the renderer's per-target outcomes reach the tool
as JSON text, and an unanswered request yields ``''`` so the operation settles on its own deadline
instead of raising.
"""

import json

import pytest

import tui_gateway.server as server
from tui_gateway import server_requests
from tui_gateway.contracts import registry


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_send(method, sid, params, *, timeout, **_kw):
        calls.append({"method": method, "sid": sid, "params": params, "timeout": timeout})
        return fake_send.result

    fake_send.result = None
    fake_send.calls = calls
    monkeypatch.setattr(server_requests, "send", fake_send)
    return fake_send


def _payload():
    return {"op_id": "op1", "deadline_at": 1.0, "timeout_seconds": 42.0, "reason": "",
            "targets": [{"name": "linear", "kind": "mcp", "action": "authorize"}]}


def test_answer_round_trips_as_json_and_waits_the_operation_deadline(sent):
    sent.result = {"settled_by": "all_resolved",
                   "targets": [{"name": "linear", "state": "authorized", "tools": ["list_issues"]}]}

    raw = server._connection_request("s1", _payload())

    assert sent.calls[0]["method"] == "connection"
    assert sent.calls[0]["timeout"] == 42.0
    assert json.loads(raw) == sent.result


def test_unanswered_request_is_the_empty_answer(sent):
    sent.result = None
    assert server._connection_request("s1", _payload()) == ""


def test_request_payload_satisfies_the_declared_contract():
    """The tool's payload and the card's answer both validate against the contract the TS is
    generated from, so a drift on either side fails here before it fails in a renderer."""
    contract = registry.SERVER_REQUESTS["connection"]
    params, problem = registry.validate_params(contract, {"session_id": "s1", **_payload()})
    assert problem is None and params is not None
    registry.check_result(contract, {"settled_by": "continue",
                                     "targets": [{"name": "linear", "state": "declined"}]})
