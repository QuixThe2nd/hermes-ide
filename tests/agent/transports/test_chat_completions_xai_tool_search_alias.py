"""Tests for provider-native ``tool_search`` reserved-name aliases (#95003).

xAI's chat-completions API reserves that function name for its native server-side
tool and rejects a client-defined function with the same name. The transport uses
request-local wire aliases and reverses only aliases emitted by the paired request,
so a real plugin/MCP function named ``hermes_tool_search`` keeps its own identity.
"""

from types import SimpleNamespace

import pytest

from agent.transports import get_transport
from agent.transports.chat_completions import (
    _XAI_TOOL_SEARCH_ALIAS,
    _rename_reserved_tool_names_for_xai,
)


@pytest.fixture
def transport():
    import agent.transports.chat_completions  # noqa: F401
    return get_transport("chat_completions")


class TestRenameReservedToolNamesForXai:
    def test_tool_search_renamed_alias_value(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "tool_search",
                "description": "Search indexed content",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        out, alias_map = _rename_reserved_tool_names_for_xai(tools)
        assert out[0]["function"]["name"] == _XAI_TOOL_SEARCH_ALIAS
        assert out[0]["function"]["name"] == "hermes_tool_search"
        assert alias_map == {"hermes_tool_search": "tool_search"}

    def test_schema_and_description_untouched(self):
        fn = {
            "name": "tool_search",
            "description": "Search the indexed content catalog",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }
        out, _ = _rename_reserved_tool_names_for_xai([{"type": "function", "function": fn}])
        assert out[0]["function"]["description"] == fn["description"]
        assert out[0]["function"]["parameters"] == fn["parameters"]

    def test_sibling_function_names_not_reserved(self):
        tools = [
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "search_files"}},
        ]
        out, alias_map = _rename_reserved_tool_names_for_xai(tools)
        assert [t["function"]["name"] for t in out] == ["read_file", "search_files"]
        assert alias_map == {}

    def test_ordinary_tools_untouched(self):
        tools = [{"type": "function", "function": {"name": "web_search"}}]
        out, alias_map = _rename_reserved_tool_names_for_xai(tools)
        assert out[0]["function"]["name"] == "web_search"
        assert alias_map == {}

    def test_input_not_mutated(self):
        # The helper feeds a deep-copied list on the helper-layer path, but
        # pin copy semantics anyway: the shared per-agent tool registry must
        # never see the alias (#27907 lesson).
        tools = [{"type": "function", "function": {"name": "tool_search"}}]
        _rename_reserved_tool_names_for_xai(tools)
        assert tools[0]["function"]["name"] == "tool_search"

    def test_collision_with_real_hermes_tool_search_takes_suffix(self):
        # A legitimate tool already using the alias name must NOT be
        # shadowed and no duplicate wire names may be produced: the colliding
        # function takes hermes_tool_search_2 instead.
        tools = [
            {"type": "function", "function": {"name": "hermes_tool_search"}},
            {"type": "function", "function": {"name": "tool_search"}},
        ]
        out, alias_map = _rename_reserved_tool_names_for_xai(tools)
        names = [t["function"]["name"] for t in out]
        assert names == ["hermes_tool_search", "hermes_tool_search_2"]
        assert len(names) == len(set(names))
        assert alias_map == {"hermes_tool_search_2": "tool_search"}


def _fake_response(tool_name):
    tc = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name=tool_name, arguments='{"query": "x"}'),
    )
    msg = SimpleNamespace(tool_calls=[tc], reasoning=None, reasoning_content=None)
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls")
    return SimpleNamespace(choices=[choice], usage=None)


class TestNormalizeResponseMapsAliasBack:
    def test_alias_call_maps_back_to_original_function_name(self, transport):
        transport._last_wire_aliases = {"hermes_tool_search": "tool_search"}
        resp = transport.normalize_response(_fake_response(_XAI_TOOL_SEARCH_ALIAS))
        assert resp.tool_calls[0].name == "tool_search"

    def test_ordinary_call_name_preserved(self, transport):
        transport._last_wire_aliases = {"hermes_tool_search": "tool_search"}
        resp = transport.normalize_response(_fake_response("read_file"))
        assert resp.tool_calls[0].name == "read_file"

    def test_no_alias_emitted_means_no_reverse_rewrite(self, transport):
        # Provenance contract: if THIS request emitted no aliases, a tool
        # call named hermes_tool_search is a REAL tool (user/plugin/MCP)
        # and must dispatch under its own name.
        transport._last_wire_aliases = {}
        resp = transport.normalize_response(_fake_response("hermes_tool_search"))
        assert resp.tool_calls[0].name == "hermes_tool_search"

    def test_suffixed_alias_maps_back(self, transport):
        transport._last_wire_aliases = {"hermes_tool_search_2": "tool_search"}
        resp = transport.normalize_response(_fake_response("hermes_tool_search_2"))
        assert resp.tool_calls[0].name == "tool_search"
        # And the real tool occupying the plain alias name is untouched.
        resp2 = transport.normalize_response(_fake_response("hermes_tool_search"))
        assert resp2.tool_calls[0].name == "hermes_tool_search"

    def test_legacy_fallback_without_provenance(self, transport):
        # Normalize-only call sites (no request built on this instance)
        # keep the historical unconditional mapping.
        transport._last_wire_aliases = None
        resp = transport.normalize_response(_fake_response(_XAI_TOOL_SEARCH_ALIAS))
        assert resp.tool_calls[0].name == "tool_search"
