"""MCP read-only annotations drive retry safety, not human authorization."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools import mcp_tool
from tools import mcp_tool_handlers as _mcp_handlers
from tools import mcp_tool_registration as _mcp_registration


class _FakeContentBlock:
    def __init__(self, text: str):
        self.text = text
        self.type = "text"


class _FakeCallToolResult:
    def __init__(self, content):
        self.content = content
        self.isError = False
        self.structuredContent = None


def _fake_run_on_mcp_loop(coro_or_factory, timeout=30):
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    loop = asyncio.new_event_loop()
    try:
        async def _install_lock_and_run():
            for server in list(mcp_tool._servers.values()):
                if getattr(server, "_rpc_lock", None) is None:
                    server._rpc_lock = asyncio.Lock()
            return await coro

        return loop.run_until_complete(_install_lock_and_run())
    finally:
        loop.close()


@pytest.fixture
def fake_session():
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=_FakeCallToolResult(content=[_FakeContentBlock("ok")])
    )
    server = SimpleNamespace(session=session, _rpc_lock=None)
    with patch.dict(mcp_tool._servers, {"srv": server}), \
         patch("tools.mcp_tool_loop._run_on_mcp_loop", side_effect=_fake_run_on_mcp_loop), \
         patch.dict(mcp_tool._server_error_counts, {}, clear=True), \
         patch.dict(mcp_tool._tool_read_only_hints, {"srv": {"delete_repo": False}}, clear=True):
        yield session


def test_legacy_untrusted_write_config_executes_without_consent(fake_session):
    handler = _mcp_handlers._make_tool_handler("srv", "delete_repo", 30.0)

    raw = handler({"repo": "x"})

    assert json.loads(raw) == {"result": "ok"}
    fake_session.call_tool.assert_awaited_once()


def test_only_exact_true_annotation_is_retry_safe():
    with patch.dict(
        mcp_tool._tool_read_only_hints,
        {"srv": {"list": True, "delete": False}},
        clear=True,
    ):
        assert _mcp_handlers._tool_is_read_only("srv", "list") is True
        assert _mcp_handlers._tool_is_read_only("srv", "delete") is False
        assert _mcp_handlers._tool_is_read_only("srv", "unknown") is False


class TestAnnotationCaptureAtDiscovery:
    def _make_tool(self, name, annotations=None):
        return SimpleNamespace(
            name=name, description="", inputSchema=None, annotations=annotations,
        )

    def test_registration_records_read_only_hints(self):
        from tools.registry import ToolRegistry

        server = mcp_tool.MCPServerTask("srv")
        server.session = MagicMock()
        server._tools = [
            self._make_tool("list_repos", SimpleNamespace(readOnlyHint=True)),
            self._make_tool("delete_repo", SimpleNamespace(readOnlyHint=False)),
            self._make_tool("no_annotations", None),
        ]
        config = {"trust": "untrusted", "tools": {"resources": False, "prompts": False}}
        with patch("tools.registry.registry", ToolRegistry()), \
             patch("tools.mcp_tool_registration._server_key_for_task", return_value="srv"), \
             patch("tools.mcp_tool_registration._record_tool_annotations", wraps=_mcp_registration._record_tool_annotations) as record_annotations, \
             patch("tools.mcp_tool_registration._track_mcp_tool_server"), \
             patch.dict(mcp_tool._tool_read_only_hints, {}, clear=True):
            _mcp_registration._register_server_tools("srv", server, config)
            hints = mcp_tool._tool_read_only_hints["srv"]
        record_annotations.assert_called_once()
        assert hints == {
            "list_repos": True,
            "delete_repo": False,
            "no_annotations": False,
        }

    def test_dict_annotations_supported(self):
        assert _mcp_registration._annotation_read_only_hint(
            SimpleNamespace(annotations={"readOnlyHint": True})
        ) is True
        assert _mcp_registration._annotation_read_only_hint(
            SimpleNamespace(annotations={"readOnlyHint": "yes"})
        ) is False
        assert _mcp_registration._annotation_read_only_hint(
            SimpleNamespace(annotations=None)
        ) is False
        assert _mcp_registration._annotation_read_only_hint(SimpleNamespace()) is False
