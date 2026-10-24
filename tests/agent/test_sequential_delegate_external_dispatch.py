"""Sequential-path delegate_agent(backend=claude|cursor) must reach the registry
dispatch with session/tool-call ids (spawn receipts key on them); the subagent
fast path is unchanged."""

import agent.tool_executor as te


class _FakeAgent:
    def __init__(self):
        self.session_id = "sess-test"
        self.quiet_mode = False
        self.valid_tool_names = None
        self._context_engine_tool_names = set()
        self._memory_manager = None

        def _boom(*a, **k):
            raise AssertionError("fast path must not run for external backends")

        self._dispatch_delegate_agent = _boom


def _make_ref(name, args, task_id="task-1", call_id="call-1"):
    return te._ToolCallRef(name, args, task_id, call_id, [])


def test_external_backend_routes_to_registry(monkeypatch):
    agent = _FakeAgent()
    captured = {}

    def fake_dispatch(name, args, **kwargs):
        captured["name"] = name
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "sentinel-result"

    monkeypatch.setattr("tools.registry.registry.dispatch", fake_dispatch)
    ref = _make_ref("delegate_agent", {"backend": "claude", "goal": "x", "workdir": "/repo"})
    dispatch = te._resolve_sequential_dispatch(agent, ref, [])
    assert dispatch.execute(ref.args) == "sentinel-result"
    assert captured["name"] == "delegate_agent"
    assert captured["args"] == {"backend": "claude", "goal": "x", "workdir": "/repo"}
    assert captured["kwargs"]["session_id"] == "sess-test"
    assert captured["kwargs"]["tool_call_id"] == "call-1"
    assert captured["kwargs"]["task_id"] == "task-1"
    assert getattr(agent, "_delegate_spinner", None) is None


def test_subagent_fast_path_unchanged(monkeypatch):
    agent = _FakeAgent()
    received = []

    def fake_dispatch_delegate(next_args):
        received.append(next_args)
        return "fast-path-result"

    agent._dispatch_delegate_agent = fake_dispatch_delegate
    dummy_spinner = object()
    monkeypatch.setattr(te, "_start_quiet_tool_spinner", lambda *a, **k: dummy_spinner)
    finished = []
    monkeypatch.setattr(te, "_finish_quiet_tool_spinner", lambda *a, **k: finished.append(a))
    ref = _make_ref("delegate_agent", {"goal": "x", "workdir": "/repo"})
    dispatch = te._resolve_sequential_dispatch(agent, ref, [])
    assert dispatch.execute(ref.args) == "fast-path-result"
    assert received == [{"goal": "x", "workdir": "/repo"}]
    assert agent._delegate_spinner is dummy_spinner


def test_legacy_alias_preserved(monkeypatch):
    agent = _FakeAgent()
    captured = {}
    monkeypatch.setattr(
        "tools.registry.registry.dispatch",
        lambda name, args, **kw: captured.update(name=name, kwargs=kw) or "ok",
    )
    ref = _make_ref("delegate_task", {"backend": "cursor", "goal": "x", "workdir": "/repo"})
    dispatch = te._resolve_sequential_dispatch(agent, ref, [])
    dispatch.execute(ref.args)
    assert captured["name"] == "delegate_task"
    assert captured["kwargs"]["tool_call_id"] == "call-1"


def test_missing_backend_takes_fast_path(monkeypatch):
    agent = _FakeAgent()
    received = []
    agent._dispatch_delegate_agent = received.append
    monkeypatch.setattr(te, "_start_quiet_tool_spinner", lambda *a, **k: None)
    monkeypatch.setattr(te, "_finish_quiet_tool_spinner", lambda *a, **k: None)
    ref = _make_ref("delegate_agent", {"goal": "x"})
    dispatch = te._resolve_sequential_dispatch(agent, ref, [])
    dispatch.execute(ref.args)
    assert received == [{"goal": "x"}]
