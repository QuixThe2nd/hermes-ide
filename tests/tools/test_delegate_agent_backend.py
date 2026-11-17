"""The unified ``delegate_agent`` backend surface.

One advertised delegation dispatch tool: ``backend`` routes subagent (default)
and claude calls to the matching existing handler; the old
``delegate_claude_agent`` name stays dispatchable
as a hidden alias that forwards every argument verbatim with the backend
preset. All backend boundaries are mocked — no binaries, no network.
"""

import json

import pytest

import tools.claude_agent_tool  # noqa: F401  (registers the hidden alias)
import tools.delegate_tool  # noqa: F401  (registers delegate_agent)
from tools.delegate_tool import DELEGATE_TASK_SCHEMA, _delegate_agent_handler
from tools.registry import registry


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_unified_schema_exposes_backend():
    props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    assert props["backend"]["enum"] == ["subagent", "claude"]
    # `backend` is optional (default subagent) — not in required.
    assert "backend" not in DELEGATE_TASK_SCHEMA["parameters"].get("required", [])
    # Backend-conditional parameters are on the one unified schema.
    for name in ("goal", "workdir", "model", "timeout_seconds",
                 "allowed_tools", "permission_mode", "background"):
        assert name in props, name
    # The subagent shape is unchanged.
    assert "tasks" in props and "action" in props


def test_exactly_one_advertised_delegation_dispatch_tool():
    advertised = {
        name for name in ("delegate_agent", "delegate_task",
                          "delegate_claude_agent")
        if (entry := registry.get_entry(name)) is not None and entry.advertise is not False
    }
    assert advertised == {"delegate_agent"}


def test_old_names_hidden_but_dispatchable():
    for name in ("delegate_claude_agent", "delegate_task"):
        entry = registry.get_entry(name)
        assert entry is not None, name
        assert entry.advertise is False, name
        # Hidden entries are excluded from advertised definitions.
        assert registry.get_definitions({name}, quiet=True) == []


# ---------------------------------------------------------------------------
# Verbatim alias forwarding (backend preset, everything else untouched)
# ---------------------------------------------------------------------------

@pytest.fixture
def captured_claude(monkeypatch):
    calls = []

    def _capture(**kwargs):
        calls.append(kwargs)
        return json.dumps({"success": True, "final_report": "ok"})

    monkeypatch.setattr(tools.claude_agent_tool, "delegate_claude_agent", _capture)
    monkeypatch.setattr(
        tools.claude_agent_tool, "check_claude_agent_requirements", lambda: True
    )
    return calls



def test_claude_alias_forwards_verbatim(captured_claude):
    out = registry.dispatch(
        "delegate_claude_agent",
        {"task": "refactor it", "workdir": "/tmp", "model": "glm-5.2",
         "timeout_seconds": 120, "allowed_tools": "Read,Bash",
         "permission_mode": "plan", "background": False},
        session_id="sess-1", tool_call_id="call-1",
    )
    assert json.loads(out)["success"] is True
    (kwargs,) = captured_claude
    assert kwargs["task"] == "refactor it"
    assert kwargs["workdir"] == "/tmp"
    assert kwargs["model"] == "glm-5.2"
    assert kwargs["timeout_seconds"] == 120
    assert kwargs["allowed_tools"] == "Read,Bash"
    assert kwargs["permission_mode"] == "plan"
    assert kwargs["background"] is False  # explicit value forwarded verbatim
    assert kwargs["session_id"] == "sess-1"
    assert kwargs["tool_call_id"] == "call-1"



# ---------------------------------------------------------------------------
# backend routing on the unified name
# ---------------------------------------------------------------------------

def test_backend_claude_routes_to_claude_handler(captured_claude):
    registry.dispatch(
        "delegate_agent",
        {"backend": "claude", "goal": "/goal tests pass", "workdir": "/tmp",
         "allowed_tools": "Read,Bash"},
        session_id="s", tool_call_id="c",
    )
    (kwargs,) = captured_claude
    assert kwargs["task"] == "/goal tests pass"  # goal -> task mapping
    assert kwargs["allowed_tools"] == "Read,Bash"



def test_backend_subagent_default_reaches_subagent_path(monkeypatch):
    import tools.delegate_tool as dt

    called = {}

    def _fake_delegate_agent(**kwargs):
        called.update(kwargs)
        return json.dumps({"results": []})

    monkeypatch.setattr(dt, "delegate_agent", _fake_delegate_agent)
    _delegate_agent_handler({"tasks": [{"goal": "g"}]}, parent_agent=object())
    assert called["tasks"] == [{"goal": "g"}]
    assert "background" in called  # decided once by _model_background_value


# ---------------------------------------------------------------------------
# Wrong-backend parameter rejection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("backend,args,offender", [
    ("claude", {"backend": "claude", "goal": "g", "workdir": "/tmp",
                "tasks": [{"goal": "g"}]}, "tasks"),
    ("claude", {"backend": "claude", "goal": "g", "workdir": "/tmp",
                "action": "list"}, "action"),
    ("subagent", {"tasks": [{"goal": "g"}], "allowed_tools": "Read"}, "allowed_tools"),
    ("subagent", {"tasks": [{"goal": "g"}], "permission_mode": "plan"}, "permission_mode"),
])
def test_wrong_backend_params_rejected(backend, args, offender, captured_claude):
    out = _delegate_agent_handler(args, parent_agent=object())
    assert offender in out
    assert f"backend={backend}" in out or backend == "subagent"
    assert not captured_claude  # nothing dispatched


def test_unknown_backend_rejected():
    out = _delegate_agent_handler({"backend": "docker", "goal": "g", "workdir": "/tmp"})
    assert "Unknown backend" in out


def test_missing_goal_or_workdir_rejected(captured_claude):
    assert "goal is required" in _delegate_agent_handler(
        {"backend": "claude", "workdir": "/tmp"})
    assert "workdir is required" in _delegate_agent_handler(
        {"backend": "claude", "goal": "g"})


# ---------------------------------------------------------------------------
# Missing-prerequisite errors (per-backend dispatch gating)
# ---------------------------------------------------------------------------

def test_claude_without_wrapper_clear_error(monkeypatch, captured_claude):
    monkeypatch.setattr(
        tools.claude_agent_tool, "check_claude_agent_requirements", lambda: False
    )
    out = _delegate_agent_handler(
        {"backend": "claude", "goal": "g", "workdir": "/tmp"})
    assert "backend=claude requires the Claude Code wrapper" in out
    assert not captured_claude



def test_delegate_agent_tool_always_available():
    import tools.delegate_tool as dt

    entry = registry.get_entry("delegate_agent")
    assert entry is not None
    assert dt.check_delegate_requirements() is True  # no external prerequisite


# ---------------------------------------------------------------------------
# background decided exactly once, forwarded verbatim
# ---------------------------------------------------------------------------

def test_background_resolved_once_for_claude(monkeypatch, captured_claude):
    import tools.async_delegation as ad

    calls = []

    def _fake_resolve(args):
        calls.append(dict(args))
        return False

    monkeypatch.setattr(ad, "resolve_background_arg", _fake_resolve)
    _delegate_agent_handler(
        {"backend": "claude", "goal": "g", "workdir": "/tmp", "background": False})
    assert len(calls) == 1
    assert calls[0].get("background") is False  # verbatim, not re-decided
    assert captured_claude[0]["background"] is False
