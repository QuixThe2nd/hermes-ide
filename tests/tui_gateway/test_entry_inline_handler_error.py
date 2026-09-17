"""Regression coverage for stdio inline-handler failures (#112816)."""

from __future__ import annotations

import io

from tui_gateway import entry


def test_main_reports_inline_handler_error_and_keeps_reading(monkeypatch):
    """A failed inline request must not prevent the next request from replying."""
    replies: list[dict] = []
    requests = iter((
        {"jsonrpc": "2.0", "id": "broken", "method": "clipboard.save"},
        {"jsonrpc": "2.0", "id": "next", "method": "ping"},
    ))

    monkeypatch.setattr(entry, "_install_sidecar_publisher", lambda: None)
    monkeypatch.setattr(entry, "ensure_mcp_discovery_started", lambda: None)
    monkeypatch.setattr(entry, "resolve_skin", lambda: "default")
    monkeypatch.setattr(entry.server, "_start_backend_heartbeat_refresher", lambda: None)
    monkeypatch.setattr(entry.server, "_schedule_startup_orphan_sweep", lambda: None)
    monkeypatch.setattr(entry.server, "_ensure_skin_watcher", lambda: None)
    monkeypatch.setattr(entry, "handle_spurious_eof", lambda *_args: False)
    monkeypatch.setattr(entry, "write_json", lambda payload: replies.append(payload) or True)
    monkeypatch.setattr(entry.sys, "stdin", io.StringIO("first\nsecond\n"))
    monkeypatch.setattr(entry.json, "loads", lambda _line: next(requests))

    def dispatch(req):
        if req["id"] == "broken":
            raise NameError("clipboard backend is unavailable")
        return {"jsonrpc": "2.0", "id": req["id"], "result": {"ok": True}}

    monkeypatch.setattr(entry, "dispatch", dispatch)

    entry.main()

    assert replies[1] == {
        "jsonrpc": "2.0",
        "id": "broken",
        "error": {"code": -32000, "message": "handler error: clipboard backend is unavailable"},
    }
    assert replies[2] == {"jsonrpc": "2.0", "id": "next", "result": {"ok": True}}
