from __future__ import annotations

import contextvars
import hashlib
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from discord_history import service
from discord_history.config import PluginConfig, Secrets

GUILD = "22222222222222222"
ROOT = "33333333333333333"
THREAD = "44444444444444444"
OWNER = "11111111111111111"
MESSAGE = "55555555555555555"


def config() -> PluginConfig:
    return PluginConfig.from_mapping({
        "owner_user_ids": [OWNER],
        "allowed_guild_ids": [GUILD],
        "allowed_channel_ids": {GUILD: [ROOT]},
    })


def secrets() -> Secrets:
    return Secrets("postgresql://test/archive", b"k" * 32)


def endpoint(reason: str) -> dict[str, str]:
    return {"state": "error", "termination_reason": reason}


@pytest.mark.parametrize(
    "reasons,expected_attempts,expected_sleeps",
    [
        (["transport_error", "http_429", "done"], 3, [1, 2]),
        (["http_503", "done"], 2, [1]),
        (["http_404"], 1, []),
        (["http_bad"], 1, []),
    ],
)
def test_run_inventory_retries_only_transient_failures(
    monkeypatch, reasons, expected_attempts, expected_sleeps
):
    inventories = []
    for reason in reasons:
        if reason == "done":
            inventories.append({
                "state": "complete",
                "parents": {ROOT: {"all_thread_ids": [THREAD, THREAD]}},
            })
        else:
            inventories.append({
                "state": "error",
                "parents": {ROOT: {
                    "all_thread_ids": [THREAD],
                    "endpoints": {"active": endpoint(reason)},
                }},
            })
    collected = iter(inventories)
    persisted_states = []
    sleeps = []
    monkeypatch.setattr(service, "load_plugin_config", config)
    monkeypatch.setattr(service, "_collect_inventory", lambda *_: next(collected))
    monkeypatch.setattr(
        service,
        "_persist_inventory",
        lambda _guild, inventory: persisted_states.append(inventory["state"])
        or {"run_id": f"run-{len(persisted_states)}", "status": inventory["state"]},
    )
    monkeypatch.setattr(service.time, "sleep", sleeps.append)

    result = service.run_inventory(guild_id=GUILD)

    assert result == {
        "ok": reasons[expected_attempts - 1] == "done",
        "command": "inventory",
        "guild_id": GUILD,
        "thread_count": 1,
        "attempts": expected_attempts,
        "run_id": f"run-{expected_attempts}",
        "status": persisted_states[-1],
    }
    assert len(persisted_states) == expected_attempts
    assert sleeps == expected_sleeps


def test_archive_status_shapes_rows_dates_scope_and_closes(monkeypatch):
    stamp = datetime(2026, 7, 17, 12, 30, tzinfo=timezone.utc)

    class Result:
        description = [SimpleNamespace(name=name) for name in (
            "channel_id", "parent_channel_id", "parent_channel_name", "name", "is_thread",
            "coverage_state", "coverage_start", "coverage_end",
            "last_incremental_at", "last_reconciled_at", "live_count",
            "newest_message_at", "last_successful_run", "last_error_code",
            "lag_seconds", "stale",
        )]

        def fetchall(self):
            return [(THREAD, ROOT, "general", "topic", True, "complete", stamp, None,
                     stamp, stamp, 7, stamp, stamp, "previous_error", 60, True)]

    class Connection:
        def __init__(self):
            self.calls = []
            self.closed = False

        def execute(self, sql, params):
            self.calls.append((sql, params))
            return Result()

        def close(self):
            self.closed = True

    conn = Connection()
    monkeypatch.setattr(service, "load_plugin_config", config)
    monkeypatch.setattr(service, "load_secrets", secrets)
    monkeypatch.setattr(service, "connect", lambda dsn: conn)

    rows = service.archive_status(guild_id=GUILD, channel_id=THREAD)

    assert rows == [{
        "channel_id": THREAD,
        "parent_channel_id": ROOT,
        "parent_channel_name": "general",
        "name": "topic",
        "is_thread": True,
        "coverage_state": "complete",
        "coverage_start": "2026-07-17T12:30:00Z",
        "coverage_end": None,
        "last_incremental_at": "2026-07-17T12:30:00Z",
        "last_reconciled_at": "2026-07-17T12:30:00Z",
        "live_count": 7,
        "newest_message_at": "2026-07-17T12:30:00Z",
        "last_successful_run": "2026-07-17T12:30:00Z",
        "last_error_code": "previous_error",
        "lag_seconds": 60,
        "stale": True,
    }]
    sql, params = conn.calls[0]
    assert "m.deleted_at IS NULL" in sql
    assert params == [GUILD, [ROOT], [ROOT], THREAD]
    assert conn.closed is True


def test_run_channel_verification_exports_at_cutoff_and_forwards_exact_hashes(monkeypatch, tmp_path):
    stamp = datetime(2026, 7, 18, 8, 12, tzinfo=timezone.utc)

    class ScopeConnection:
        def __init__(self): self.closed = False
        def execute(self, sql, params):
            assert "source_before" in sql and params == (THREAD,)
            return SimpleNamespace(fetchone=lambda: (GUILD, ROOT, stamp))
        def close(self): self.closed = True

    class VerifyConnection:
        def __init__(self): self.closed = False
        def close(self): self.closed = True

    scope_conn, verify_conn = ScopeConnection(), VerifyConnection()
    connections = [scope_conn, verify_conn]
    monkeypatch.setattr(service, "load_plugin_config", config)
    monkeypatch.setattr(service, "load_secrets", secrets)
    monkeypatch.setattr(service, "connect", lambda _dsn: connections.pop(0))
    monkeypatch.setattr(service, "_discord_helpers", lambda: (None, lambda: "token"))
    state = tmp_path / "state"
    (state / "tmp").mkdir(parents=True)
    monkeypatch.setattr(service, "STATE_ROOT", state)

    dce = __import__("discord_history.dce", fromlist=["DCEExporter"])
    ingest = __import__("discord_history.ingest", fromlist=["load_export"])
    verifier = __import__("discord_history.verify", fromlist=["verify_channel"])
    seen = {}

    class Exporter:
        def __init__(self, binary, timeout):
            seen["binary"] = binary
            seen["timeout"] = timeout
        def export(self, request, token):
            seen["request"] = request
            seen["token"] = token
            return {"state": "ok", "exit_code": 0}

    monkeypatch.setattr(dce, "DCEExporter", Exporter)
    monkeypatch.setattr(ingest, "load_export", lambda _path: (
        None, None, [SimpleNamespace(message_id=MESSAGE, content="body", created_at=stamp)]
    ))

    def verify_channel(_conn, channel_id, *, cutoff, dce_messages):
        seen["verify"] = (channel_id, cutoff, dce_messages)
        return {"ok": True, "checks": {"fixed_cutoff": True}}

    monkeypatch.setattr(verifier, "verify_channel", verify_channel)
    result = service.run_channel_verification(THREAD)

    assert result["ok"] is True
    assert result["checks"] == {"fixed_cutoff": True, "dce_exit_zero": True}
    assert seen["token"] == "token" and seen["timeout"] == 600
    assert seen["request"].channel_id == THREAD
    assert seen["request"].before == "2026-07-18T08:12:00Z"
    assert seen["verify"][0:2] == (THREAD, stamp)
    assert seen["verify"][2][MESSAGE] == (hashlib.sha256(b"body").hexdigest(), stamp)
    assert scope_conn.closed and verify_conn.closed


def test_run_channel_verification_rejects_non_ascii_id_before_config(monkeypatch):
    monkeypatch.setattr(service, "load_plugin_config",
                        lambda: pytest.fail("config must not load"))
    with pytest.raises(RuntimeError, match="invalid_channel_id"):
        service.run_channel_verification("١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦٧")


def install_session_context(monkeypatch):
    module = types.ModuleType("gateway.session_context")
    module._UNSET = object()
    module._SESSION_PLATFORM = contextvars.ContextVar("platform", default="original-platform")
    module._SESSION_USER_ID = contextvars.ContextVar("user", default="original-user")
    module._SESSION_CHAT_ID = contextvars.ContextVar("chat", default="original-chat")
    module._SESSION_THREAD_ID = contextvars.ContextVar("thread", default="original-thread")
    gateway = types.ModuleType("gateway")
    gateway.session_context = module
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", module)
    return module


def test_denial_probes_reject_before_database_and_allow_owner(monkeypatch, tmp_path):
    context = install_session_context(monkeypatch)
    calls = []

    class Connection:
        def execute(self, sql, _params):
            if "resolve_channels" in sql:
                return [{"channel_id": ROOT}]
            if "scope_coverage" in sql:
                return []
            if "/* status */" in sql:
                return []
            if "search_audit" in sql:
                return []
            raise AssertionError(sql)

        def commit(self):
            pass

        def close(self):
            pass

    tool = __import__("discord_history.tool", fromlist=["append_denial_event"])
    monkeypatch.setattr(service, "load_plugin_config", config)
    monkeypatch.setattr(service, "load_secrets", secrets)
    monkeypatch.setattr(
        service, "connect", lambda dsn: calls.append(dsn) or Connection()
    )
    monkeypatch.setattr(service, "STATE_ROOT", tmp_path)
    log = tmp_path / "logs" / "access-denied.jsonl"

    def record_denial(*_args, **_kwargs):
        log.parent.mkdir(mode=0o700, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp": "2026-07-18T00:00:00Z",
                                     "reason": "denied",
                                     "platform_present": True,
                                     "user_id_hmac": "a" * 64}) + "\n")
        log.chmod(0o600)

    monkeypatch.setattr(tool, "append_denial_event", record_denial)

    result = service.run_denial_and_retrieval_probes()

    assert result == {
        "no_db_denial_probes": True,
        "denial_log_checks": True,
        "retrieval_checks": True,
        "retrieval_bounds_suite": True,
    }
    assert calls == ["postgresql://test/archive"]
    assert context._SESSION_PLATFORM.get() == "original-platform"
    assert context._SESSION_USER_ID.get() == "original-user"


def test_run_live_acceptance_probes_with_export_db_and_retrieval_fakes(monkeypatch, tmp_path):
    context = install_session_context(monkeypatch)
    content = "needle phrase"
    digest = hashlib.sha256(content.encode()).hexdigest()
    cutoff = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)

    class Result:
        def __init__(self, *, row=None, rows=None):
            self.row = row
            self.rows = rows or []

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        def __init__(self):
            self.closed = False
            self.queries = []

        def execute(self, sql, params):
            self.queries.append((sql, params))
            if "JOIN discord_archive.channels" in sql and "m.message_id=%s" in sql:
                return Result(row=(THREAD, ROOT, content))
            if "inventory_parent_unions" in sql:
                return Result(row=(cutoff, "inventory-run"))
            if "content_hash" in sql:
                return Result(rows=[(MESSAGE, digest)])
            raise AssertionError(sql)

        def close(self):
            self.closed = True

    conn = Connection()
    exports = []

    class Exporter:
        def __init__(self, binary, timeout):
            assert binary == tmp_path / "dce"
            assert timeout == 600

        def export(self, request, token):
            exports.append((request, token))
            request.output.write_text("{}", encoding="utf-8")
            return {"state": "ok"}

    message = SimpleNamespace(message_id=MESSAGE, content=content)

    def fake_handle(arguments, *, config, secrets, connector):
        del config, secrets
        assert connector is service.connect
        assert arguments["channel_ids"] == [THREAD]
        assert context._SESSION_PLATFORM.get() == "discord"
        assert context._SESSION_USER_ID.get() == OWNER
        assert context._SESSION_CHAT_ID.get() == ROOT
        assert context._SESSION_THREAD_ID.get() == THREAD
        return json.dumps({
            "results": [{
                "message_id": MESSAGE,
                "permalink": f"https://discord.com/channels/{GUILD}/{THREAD}/{MESSAGE}",
            }],
            "coverage": {"channel_count": 1},
        })

    dce = __import__("discord_history.dce", fromlist=["DCEExporter"])
    ingest = __import__("discord_history.ingest", fromlist=["load_export"])
    tool = __import__("discord_history.tool", fromlist=["handle_discord_history"])
    monkeypatch.setattr(dce, "DCEExporter", Exporter)
    monkeypatch.setattr(ingest, "load_export", lambda path: (object(), object(), [message]))
    monkeypatch.setattr(tool, "handle_discord_history", fake_handle)
    monkeypatch.setattr(service, "load_plugin_config", config)
    monkeypatch.setattr(service, "load_secrets", secrets)
    monkeypatch.setattr(service, "connect", lambda dsn: conn)
    monkeypatch.setattr(service, "_discord_helpers", lambda: (object(), lambda: "bot-token"))
    monkeypatch.setattr(service, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(service, "DCE_BINARY", tmp_path / "dce")
    (tmp_path / "tmp").mkdir()

    result = service.run_live_acceptance_probes(
        guild_id=GUILD, expected_message_id=MESSAGE, expected_phrase=content
    )

    assert result == {
        "dce_set_equality": True,
        "dce_hash_equality": True,
        "reconciliation_inventory_link": True,
        "retrieval_bounds_and_citation": True,
    }
    assert conn.closed is True
    assert len(exports) == 1
    request, token = exports[0]
    assert token == "bot-token"
    assert request.channel_id == THREAD
    assert request.before == "2026-07-17T12:00:00Z"
    assert context._SESSION_PLATFORM.get() == "original-platform"
    assert not list((tmp_path / "tmp").iterdir())


class Transaction:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): return self
    def __exit__(self, kind, _value, _traceback): self.conn.exits.append(kind); return False


class PersistConnection:
    def __init__(self):
        self.calls = []
        self.commits = self.rollbacks = 0
        self.closed = False
        self.exits = []
    def transaction(self): return Transaction(self)
    def execute(self, sql, params=()): self.calls.append((sql, params)); return SimpleNamespace()
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1
    def close(self): self.closed = True


def complete_inventory():
    def manifest(name):
        return {"endpoint": name, "state": "complete", "page_count": 0,
                "endpoint_thread_ids": [], "global_union_ids_after_endpoint": [],
                "termination_reason": "has_more_false", "pages": []}
    return {
        "guild": {"id": GUILD, "name": "Guild"}, "state": "complete",
        "parents": {ROOT: {
            "parent_channel": {"id": ROOT, "name": "general", "type": 0},
            "state": "complete", "active_thread_ids": [THREAD],
            "archived_thread_ids": [MESSAGE], "all_thread_ids": [THREAD, MESSAGE],
            "termination_reason": "all_endpoints_complete",
            "endpoints": {name: manifest(name) for name in ("active", "public", "private", "joined_private")},
        }},
    }


def test_inventory_persistence_records_exact_scope_and_rolls_back_atomically(monkeypatch):
    ingest = __import__("discord_history.ingest", fromlist=["record_inventory_manifests"])
    conn = PersistConnection()
    recorded = []
    monkeypatch.setattr(service, "connect", lambda _dsn: conn)
    monkeypatch.setattr(service, "load_secrets", secrets)
    monkeypatch.setattr(ingest, "record_inventory_manifests",
                        lambda *args: recorded.append(args[1:]))
    result = service._persist_inventory(GUILD, complete_inventory())
    scope_params = [params for sql, params in conn.calls if "INSERT INTO discord_archive.ingest_run_scope" in sql]
    channel_params = next(params for sql, params in conn.calls
                          if "INSERT INTO discord_archive.channels" in sql)
    assert result["status"] == "ok" and result["scope_count"] == 3 and result["parent_count"] == 1
    assert {params[2] for params in scope_params} == {"channel", "active_thread", "archived_thread"}
    assert channel_params[:4] == (ROOT, GUILD, 0, "general")
    assert len(recorded) == 1 and conn.commits == 1 and conn.rollbacks == 0 and conn.closed

    broken = PersistConnection()
    monkeypatch.setattr(service, "connect", lambda _dsn: broken)
    monkeypatch.setattr(ingest, "record_inventory_manifests",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("injected")))
    with pytest.raises(RuntimeError, match="injected"):
        service._persist_inventory(GUILD, complete_inventory())
    assert broken.rollbacks == 1 and broken.closed and broken.exits == [RuntimeError]


def test_inventory_and_channel_scope_helpers_fail_closed(monkeypatch):
    monkeypatch.setattr(service, "load_plugin_config", config)
    with pytest.raises(RuntimeError, match="guild_not_allowlisted"):
        service.run_inventory(guild_id="99999999999999999")
    with pytest.raises(RuntimeError, match="inventory_guild_mismatch"):
        service._persist_inventory(GUILD, {"guild": {"id": THREAD}, "state": "complete"})

    monkeypatch.setattr(service, "_inventory_with_retries",
                        lambda *_args: (complete_inventory(), {"run_id": "inventory"}, 1))
    scopes, inventory = service._all_inventory_scopes(GUILD, config())
    assert scopes == sorted([ROOT, THREAD, MESSAGE], key=int)
    assert inventory["persistence"]["run_id"] == "inventory"
    monkeypatch.setattr(service, "_inventory_with_retries",
                        lambda *_args: ({"state": "error"}, {"run_id": "bad"}, 1))
    with pytest.raises(RuntimeError, match="inventory_incomplete"):
        service._all_inventory_scopes(GUILD, config())

    service._validate_channel_scope({"id": THREAD, "guild_id": GUILD, "parent_id": ROOT},
                                    guild_id=GUILD, allowed_roots=frozenset({ROOT}))
    with pytest.raises(RuntimeError, match="channel_guild_mismatch"):
        service._validate_channel_scope({"id": THREAD, "guild_id": MESSAGE, "parent_id": ROOT},
                                        guild_id=GUILD, allowed_roots=frozenset({ROOT}))
    with pytest.raises(RuntimeError, match="channel_not_allowlisted"):
        service._validate_channel_scope({"id": THREAD, "guild_id": GUILD, "parent_id": MESSAGE},
                                        guild_id=GUILD, allowed_roots=frozenset({ROOT}))


def test_run_sync_rejects_invalid_inputs_before_database_or_export(monkeypatch):
    monkeypatch.setattr(service, "load_plugin_config", config)
    with pytest.raises(RuntimeError, match="invalid_sync_mode"):
        service.run_sync(guild_id=GUILD, channel_ids=[THREAD], mode="bad")
    with pytest.raises(RuntimeError, match="guild_not_allowlisted"):
        service.run_sync(guild_id=MESSAGE, channel_ids=[THREAD], mode="incremental")

    monkeypatch.setattr(service, "_discord_helpers", lambda: (lambda *_a, **_k: {}, lambda: None))
    with pytest.raises(RuntimeError, match="discord_token_missing"):
        service.run_sync(guild_id=GUILD, channel_ids=[THREAD], mode="incremental")

    monkeypatch.setattr(service, "_discord_helpers", lambda: (lambda *_a, **_k: {}, lambda: "token"))
    monkeypatch.setattr(service, "_all_inventory_scopes", lambda *_args: ([], {"state": "complete"}))
    with pytest.raises(RuntimeError, match="no_sync_scopes"):
        service.run_sync(guild_id=GUILD, channel_ids=[], mode="incremental")
    with pytest.raises(RuntimeError, match="invalid_channel_id"):
        service.run_sync(guild_id=GUILD, channel_ids=["bad"], mode="incremental")
    with pytest.raises(RuntimeError, match="discord_channel_metadata_invalid"):
        service.run_sync(guild_id=GUILD, channel_ids=[THREAD], mode="incremental")

    monkeypatch.setattr(service, "_inventory_with_retries",
                        lambda *_args: ({"state": "error"}, {"run_id": "bad"}, 1))
    with pytest.raises(RuntimeError, match="inventory_incomplete"):
        service.run_sync(guild_id=GUILD, channel_ids=[THREAD], mode="reconcile")
    monkeypatch.setattr(service, "_inventory_with_retries",
                        lambda *_args: ({"state": "complete", "parents": {}}, {"run_id": "ok"}, 1))
    with pytest.raises(RuntimeError, match="reconcile_scope_missing_from_inventory"):
        service.run_sync(guild_id=GUILD, channel_ids=[THREAD], mode="reconcile")
