"""Destructive gateway slashes run without a confirmation gate.

``_maybe_confirm_destructive_slash`` and the Always/Once/Cancel preference
write are gone. /new, /reset, and /undo execute directly. MCP reload and
model-selection guards still use ``_request_slash_confirm``.
"""

from __future__ import annotations

import inspect

import gateway.run as gw


def test_destructive_slash_confirm_method_is_gone():
    assert not hasattr(gw.GatewayRunner, "_maybe_confirm_destructive_slash")
    assert not hasattr(gw.GatewayRunner, "_confirm_destructive_slash")
    # Shared confirm primitive stays for MCP reload and model guards.
    assert callable(gw.GatewayRunner._request_slash_confirm)


def test_reset_undo_and_busy_new_do_not_ask_for_confirm():
    for name in (
        "_handle_reset_command",
        "_handle_undo_command",
        "_busy_new_command",
        "_hm_cmd_new",
        "_hm_cmd_undo",
    ):
        src = inspect.getsource(getattr(gw.GatewayRunner, name))
        assert "_maybe_confirm_destructive_slash" not in src, name
        assert "_request_slash_confirm" not in src, name
        assert "destructive_slash_confirm" not in src, name
