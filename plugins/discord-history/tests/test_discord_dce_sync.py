from datetime import datetime, timezone
from pathlib import Path
import json
import subprocess

import pytest

from discord_history.discord_api import inventory_guild
from discord_history.dce import DCEExporter, ExportRequest
from discord_history.sync import plan_sync, plan_reconciliation


def test_inventory_endpoint_local_cursors_and_overlap():
    calls = []
    def request(method, path, token, params=None, **kw):
        calls.append((path, dict(params or {})))
        if path == "/users/@me/guilds": return [{"id": "123", "name": "g"}]
        if path == "/guilds/123/channels": return [{"id":"100","type":0,"name":"root"}]
        if path == "/guilds/123/threads/active": return {"threads":[{"id":"201","parent_id":"100","type":11}]}
        if path.endswith("/archived/public"):
            if not params.get("before"):
                return {"threads":[{"id":"202","parent_id":"100","thread_metadata":{"archive_timestamp":"2026-01-02T00:00:00Z"}}],"has_more":True}
            return {"threads":[{"id":"203","parent_id":"100","thread_metadata":{"archive_timestamp":"2026-01-01T00:00:00Z"}}],"has_more":False}
        if path.endswith("/threads/archived/private") and "/users/@me/" not in path:
            return {"threads":[{"id":"204","parent_id":"100","thread_metadata":{"archive_timestamp":"2025-01-01T00:00:00Z"}}],"has_more":False}
        if "/users/@me/threads/archived/private" in path:
            return {"threads":[{"id":"204","parent_id":"100"}],"has_more":False}
        raise AssertionError(path)
    result = inventory_guild("123", token="secret", request=request)
    assert result["state"] == "complete"
    assert result["parents"]["100"]["parent_channel"]["name"] == "root"
    assert result["parents"]["100"]["all_thread_ids"] == ["201","202","203","204"]
    public = result["parents"]["100"]["endpoints"]["public"]
    assert public["pages"][1]["request_cursor"] == "2026-01-02T00:00:00Z"
    joined = result["parents"]["100"]["endpoints"]["joined_private"]
    assert joined["endpoint_thread_ids"] == ["204"]
    assert joined["global_union_ids_after_endpoint"] == ["201","202","203","204"]


def test_inventory_repeated_cursor_is_error():
    def request(method, path, token, params=None, **kw):
        if path == "/users/@me/guilds": return [{"id":"123"}]
        if path.endswith("/channels"): return [{"id":"100","type":0}]
        if path.endswith("/threads/active"): return {"threads":[]}
        if path.endswith("/archived/public"): return {"threads":[{"id":"2","parent_id":"100","thread_metadata":{"archive_timestamp":"2026-01-01T00:00:00Z"}}],"has_more":True}
        return {"threads":[],"has_more":False}
    result = inventory_guild("123", token="x", request=request)
    assert result["state"] == "error"
    assert result["parents"]["100"]["endpoints"]["public"]["termination_reason"] in {"non_decreasing_cursor", "repeated_page_fingerprint"}


@pytest.mark.parametrize("active", [
    {"threads": [], "has_more": True},
    {"threads": [{"id": "2"}]},
])
def test_inventory_rejects_malformed_active_pagination_and_missing_parent(active):
    def request(method, path, token, params=None, **kw):
        if path == "/users/@me/guilds": return [{"id": "123"}]
        if path.endswith("/channels"): return [{"id": "100", "type": 0}]
        if path.endswith("/threads/active"): return active
        return {"threads": [], "has_more": False}
    result = inventory_guild("123", token="x", request=request)
    assert result["state"] == "error"
    assert result["parents"]["100"]["endpoints"]["active"]["termination_reason"] == "malformed_response"


@pytest.mark.parametrize("thread", [
    {"id": "2", "parent_id": "999", "thread_metadata": {"archive_timestamp": "2026-01-01T00:00:00Z"}},
    {"id": "2", "parent_id": "100", "thread_metadata": {"archive_timestamp": "not-a-time"}},
])
def test_inventory_rejects_wrong_parent_and_malformed_timestamp(thread):
    def request(method, path, token, params=None, **kw):
        if path == "/users/@me/guilds": return [{"id": "123"}]
        if path.endswith("/channels"): return [{"id": "100", "type": 0}]
        if path.endswith("/threads/active"): return {"threads": []}
        if path.endswith("/archived/public"):
            return {"threads": [thread], "has_more": False}
        return {"threads": [], "has_more": False}
    result = inventory_guild("123", token="x", request=request)
    endpoint = result["parents"]["100"]["endpoints"]["public"]
    assert endpoint["state"] == "error"
    assert endpoint["termination_reason"] in {"malformed_response", "malformed_cursor"}


def test_inventory_retries_transient_transport_without_retrying_403():
    class Error(Exception):
        def __init__(self, status): self.status, self.body = status, "{}"
    guild_calls = 0
    sleeps = []
    def request(method, path, token, params=None, **kw):
        nonlocal guild_calls
        if path == "/users/@me/guilds":
            guild_calls += 1
            if guild_calls < 3: raise Error(502)
            return [{"id":"123"}]
        if path.endswith("/channels"): return [{"id":"100","type":0}]
        if path.endswith("/threads/active"): return {"threads":[]}
        return {"threads":[],"has_more":False}
    result = inventory_guild("123", token="x", request=request, sleep=sleeps.append)
    assert result["state"] == "complete"
    assert guild_calls == 3 and sleeps == [1, 2]

    forbidden_calls = 0
    def forbidden(method, path, token, params=None, **kw):
        nonlocal forbidden_calls
        if path == "/users/@me/guilds": return [{"id":"123"}]
        if path.endswith("/channels"): return [{"id":"100","type":0}]
        if path.endswith("/threads/active"): return {"threads":[]}
        if path.endswith("/archived/public"):
            forbidden_calls += 1
            raise Error(403)
        return {"threads":[],"has_more":False}
    denied = inventory_guild("123", token="x", request=forbidden, sleep=sleeps.append)
    assert denied["state"] == "inaccessible"
    assert forbidden_calls == 1


def test_dce_redacts_token_and_runs_argv(tmp_path, monkeypatch):
    binary = tmp_path / "dce"
    binary.write_text("x", encoding="utf-8")
    binary.chmod(0o700)
    seen = {}
    def run(argv, **kwargs):
        seen["argv"] = argv; seen["kwargs"] = kwargs
        Path(argv[argv.index("-o") + 1]).write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    exporter = DCEExporter(binary, runner=run, timeout=9)
    req = ExportRequest("123", tmp_path / "out.json", after="2026-01-01T00:00:00Z")
    manifest = exporter.export(req, "super-secret")
    assert seen["argv"][0] == str(binary)
    assert seen["kwargs"]["shell"] is False and seen["kwargs"]["timeout"] == 9
    assert "super-secret" not in seen["argv"]
    assert seen["kwargs"]["env"]["DISCORD_TOKEN"] == "super-secret"
    assert "super-secret" not in manifest["command"]
    assert manifest["state"] == "ok"


def test_sync_overlap_and_reconciliation_plans():
    newest = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    p = plan_sync("123", "incremental", newest_created_at=newest, overlap_hours=48,
                  export_before=datetime(2026,7,17,13,tzinfo=timezone.utc))
    assert p.after == "2026-07-15T12:00:00Z"
    assert plan_reconciliation(["1","2"], {"1":{"state":"complete"},"2":{"state":"complete"}},
                               {"1":"ok","2":"empty"})["complete"] is True
    assert plan_reconciliation(["1","2"], {"1":{"state":"complete"}}, {"1":"ok"})["complete"] is False
