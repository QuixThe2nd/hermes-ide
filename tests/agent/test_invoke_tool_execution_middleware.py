"""invoke_tool must dispatch through registered TOOL_EXECUTION middleware and return the chain's
result — a dropped return tail would silently bypass every registered execution middleware."""

import json
from types import SimpleNamespace


def test_execution_middleware_fires_rewrites_args_and_returns_chain_result(monkeypatch):
    from agent.agent_runtime_helpers import invoke_tool
    from hermes_cli.middleware import TOOL_EXECUTION_MIDDLEWARE
    from hermes_cli.plugins import get_plugin_manager
    from tools.todo_tool import TodoStore

    class CountingStore(TodoStore):
        # One write per real executor run: proves next_call's downstream reached the tool exactly once.
        def __init__(self):
            super().__init__()
            self.writes = []

        def write(self, todos, merge=False):
            self.writes.append(todos)
            return super().write(todos, merge)

    agent = SimpleNamespace(
        enabled_toolsets=["todo"], disabled_toolsets=[],
        session_id="t", _todo_store=CountingStore(), _memory_manager=None,
    )
    observed = []
    chain_results = []
    next_calls = [0]

    def middleware(**kwargs):
        observed.append(
            {"tool_name": kwargs["tool_name"], "args": kwargs["args"],
             "original_args": kwargs["original_args"]})
        next_calls[0] += 1
        # Rewrite the args: the marker payload must be what the executor actually receives.
        chain_results.append(kwargs["next_call"]({"todos": [
            {"id": "mw", "content": "middleware-rewrite-landed", "status": "pending"}]}))
        return chain_results[-1]

    mgr = get_plugin_manager()
    monkeypatch.setattr(mgr, "_middleware", {TOOL_EXECUTION_MIDDLEWARE: [middleware]})

    # pre_tool_block_checked: the built-in pre_tool_call hook (live-gateway callback) can't answer
    # in-process and blocks the call; the proven fixture skips that check, not the middleware under test.
    result = invoke_tool(agent, "todo_list", {
        "todos": [{"id": "orig", "content": "pre-middleware-payload", "status": "pending"}]},
        "task", tool_call_id="call", pre_tool_block_checked=True)

    # The callback fired once with the dispatched tool name and the pre-chain args.
    assert len(observed) == 1
    assert observed[0]["tool_name"] == "todo_list"
    assert observed[0]["original_args"]["todos"][0]["id"] == "orig"
    # next_call ran exactly once and its downstream executed the real todo tool with the rewrite.
    assert next_calls == [1]
    assert len(agent._todo_store.writes) == 1
    assert agent._todo_store.writes[0][0]["content"] == "middleware-rewrite-landed"
    assert agent._todo_store.read()[0]["content"] == "middleware-rewrite-landed"
    # The executed result came back out: both as invoke_tool's return and inside its JSON payload.
    assert result == chain_results[0]
    assert json.loads(result)["todos"][0]["content"] == "middleware-rewrite-landed"


def test_skip_tool_execution_middleware_bypasses_chain_and_still_executes(monkeypatch):
    from agent.agent_runtime_helpers import invoke_tool
    from hermes_cli.middleware import TOOL_EXECUTION_MIDDLEWARE
    from hermes_cli.plugins import get_plugin_manager
    from tools.todo_tool import TodoStore

    agent = SimpleNamespace(
        enabled_toolsets=["todo"], disabled_toolsets=[],
        session_id="t", _todo_store=TodoStore(), _memory_manager=None,
    )
    invoked = []

    def middleware(**kwargs):
        invoked.append(kwargs)
        return kwargs["next_call"]()

    mgr = get_plugin_manager()
    monkeypatch.setattr(mgr, "_middleware", {TOOL_EXECUTION_MIDDLEWARE: [middleware]})

    result = invoke_tool(agent, "todo_list", {
        "todos": [{"id": "a", "content": "bypass-path-write-landed", "status": "pending"}]},
        "task", tool_call_id="call", pre_tool_block_checked=True,
        skip_tool_execution_middleware=True)

    assert invoked == []  # bypass path: the registered chain never fires
    assert agent._todo_store.read()[0]["content"] == "bypass-path-write-landed"
    assert json.loads(result)["todos"][0]["content"] == "bypass-path-write-landed"
