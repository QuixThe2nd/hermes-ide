from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from discord_history.auth import AuthorizationError, AuthorizedScope, GatewayPrincipal
from discord_history.config import PluginConfig, Secrets
from discord_history.retrieval import MAX_JSON_BYTES, Request, RetrievalValidationError
from discord_history.tool import DISCORD_HISTORY_SCHEMA, handle_discord_history

GUILD = "22222222222222222"
CHANNEL = "33333333333333333"
OWNER = "11111111111111111"
MESSAGE = "44444444444444444"


class Result:
    def __init__(self, rows=()): self._rows = list(rows)
    def fetchall(self): return self._rows


class FakeConnection:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.closed = False
        self.committed = False
        self.rolled_back = False
    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        for marker, rows in self.routes.items():
            if marker in sql:
                return Result(rows)
        return Result()
    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True
    def close(self): self.closed = True


def config():
    return PluginConfig.from_mapping({"owner_user_ids": [OWNER], "allowed_guild_ids": [GUILD], "allowed_channel_ids": {GUILD: [CHANNEL]}})


def row(content="hello exact world", deleted_at=None):
    return {"message_id": MESSAGE, "guild_id": GUILD, "guild_name": "Guild", "channel_id": CHANNEL,
            "channel_name": "general", "parent_channel_id": None,
            "parent_channel_name": None, "is_thread": False, "author_id": OWNER,
            "username": "owner", "global_name": "Owner", "author_name_snapshot": "Owner", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "content": content, "deleted_at": deleted_at, "score": 0.9, "coverage_state": "complete",
            "coverage_start": datetime(2025, 1, 1, tzinfo=timezone.utc), "coverage_end": datetime(2026, 1, 2, tzinfo=timezone.utc)}


def authorize(monkeypatch):
    scope = AuthorizedScope(GatewayPrincipal("discord", OWNER, "chat", "thread"), GUILD, frozenset({CHANNEL}))
    monkeypatch.setattr("discord_history.tool.auth.authorize_bound_request", lambda *a, **k: scope)


def invoke(monkeypatch, arguments, routes):
    authorize(monkeypatch)
    routes = {"resolve_channels": [{"channel_id": CHANNEL}], **routes}
    conn = FakeConnection(routes)
    output = handle_discord_history(arguments, config=config(), secrets=Secrets("postgresql://test/db", b"k" * 32), connector=lambda dsn: conn)
    return json.loads(output), conn, output


def test_schema_is_one_strict_typed_read_only_tool():
    assert DISCORD_HISTORY_SCHEMA["additionalProperties"] is False
    assert set(DISCORD_HISTORY_SCHEMA["properties"]["action"]["enum"]) == {"search", "get", "context", "status"}
    assert DISCORD_HISTORY_SCHEMA["properties"]["limit"]["maximum"] == 50
    assert "sql" not in DISCORD_HISTORY_SCHEMA["properties"]


@pytest.mark.parametrize("arguments,error", [
    ({"action": "search", "guild_id": GUILD}, "query_required"),
    ({"action": "search", "guild_id": GUILD, "query": "x" * 501}, "invalid_query"),
    ({"action": "search", "guild_id": GUILD, "query": "x", "limit": 51}, "invalid_limit"),
    ({"action": "get", "guild_id": GUILD}, "message_id_required"),
    ({"action": "status", "guild_id": GUILD, "sql": "DROP TABLE x"}, "invalid_arguments"),
    ({"action": "status", "guild_id": GUILD, "message_id": MESSAGE}, "invalid_arguments"),
    ({"action": "status", "guild_id": "٢٢٢٢٢٢٢٢٢٢٢٢٢٢٢٢٢"}, "invalid_guild_id"),
    ({"action": "search", "guild_id": GUILD, "query": "x", "channel_ids": (CHANNEL,)}, "invalid_channel_ids"),
])
def test_strict_validation_never_opens_database(arguments, error):
    opened = []
    output = handle_discord_history(arguments, config=config(), secrets=Secrets("postgresql://test/db", b"k" * 32), connector=lambda dsn: opened.append(dsn))
    assert json.loads(output) == {"error": error}
    assert opened == []


def test_authorization_happens_before_database_open_and_denial_is_generic(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr("discord_history.tool.auth.authorize_bound_request", lambda *a, **k: (_ for _ in ()).throw(AuthorizationError("not_owner")))
    monkeypatch.setattr("discord_history.tool._presented_principal", lambda: (True, "99999999999999999"))
    output = handle_discord_history({"action": "status", "guild_id": GUILD}, config=config(),
        secrets=Secrets("postgresql://test/db", b"k" * 32), connector=lambda dsn: opened.append(dsn), denial_path=str(tmp_path / "denied.jsonl"))
    assert json.loads(output) == {"error": "authorization_failed"}
    assert opened == []
    assert "99999999999999999" not in (tmp_path / "denied.jsonl").read_text(encoding="utf-8")


def test_wrong_current_thread_parent_never_opens_database(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(
        "discord_history.auth.get_bound_gateway_principal",
        lambda: GatewayPrincipal("discord", OWNER, CHANNEL,
                                 "55555555555555555"),
    )
    monkeypatch.setattr(
        "discord_history.tool._resolve_thread_metadata",
        lambda _thread: {"id": "55555555555555555", "guild_id": GUILD,
                         "parent_id": "99999999999999999", "type": 11},
    )
    output = handle_discord_history(
        {"action": "status", "guild_id": GUILD}, config=config(),
        secrets=Secrets("postgresql://test/db", b"k" * 32),
        connector=lambda dsn: opened.append(dsn),
        denial_path=str(tmp_path / "denied.jsonl"),
    )
    assert json.loads(output) == {"error": "authorization_failed"}
    assert opened == []


def test_search_uses_parameterized_simple_fts_then_bounded_trigram_and_audits(monkeypatch):
    payload, conn, encoded = invoke(monkeypatch, {"action": "search", "guild_id": GUILD, "query": "exact", "limit": 10},
                                    {"search_fts": [row()], "search_trigram": [row()]})
    assert payload["results"][0]["permalink"] == f"https://discord.com/channels/{GUILD}/{CHANNEL}/{MESSAGE}"
    assert "parent_channel_name" in payload["results"][0]
    assert payload["results"][0]["coverage"]["state"] == "complete"
    assert len(payload["results"][0]["snippet"]) <= 320
    fts = next(call for call in conn.calls if "search_fts" in call[0])
    trigram = next(call for call in conn.calls if "search_trigram" in call[0])
    assert "websearch_to_tsquery('simple', %s)" in fts[0] and "exact" not in fts[0]
    assert "LIMIT 100" in fts[0] and "LIMIT 50" in trigram[0] and 0.25 in trigram[1]
    assert any("search_audit" in sql for sql, _ in conn.calls)
    assert conn.committed and conn.closed and len(encoded.encode("utf-8")) <= MAX_JSON_BYTES


def test_authorized_database_failure_rolls_back_then_audits(monkeypatch):
    authorize(monkeypatch)

    class FailingConnection(FakeConnection):
        def execute(self, sql, params=()):
            if "search_fts" in sql:
                self.calls.append((sql, params))
                raise RuntimeError("database error")
            return super().execute(sql, params)

    conn = FailingConnection({"resolve_channels": [{"channel_id": CHANNEL}]})
    output = handle_discord_history({"action": "search", "guild_id": GUILD, "query": "exact"}, config=config(),
        secrets=Secrets("postgresql://test/db", b"k" * 32), connector=lambda dsn: conn)
    assert json.loads(output) == {"error": "retrieval_failed"}
    assert conn.rolled_back and conn.committed and conn.closed
    audit = next(params for sql, params in conn.calls if "search_audit" in sql)
    assert audit[-1] == "error"


def test_author_and_channel_names_are_resolved_inside_authorized_scope(monkeypatch):
    payload, conn, _ = invoke(monkeypatch, {"action": "search", "guild_id": GUILD, "query": "exact", "channel_names": ["General"], "author_names": ["Owner"]},
        {"resolve_channels": [{"channel_id": CHANNEL}], "resolve_authors": [{"user_id": OWNER}], "search_fts": [row()]})
    assert payload["results"][0]["message_id"] == MESSAGE
    assert all("deleted_at IS NULL" in sql for sql, _ in conn.calls if any(marker in sql for marker in ("search_fts", "search_trigram")))


def test_get_context_and_status_all_exclude_deleted_rows_and_are_bounded(monkeypatch):
    payload, conn, _ = invoke(monkeypatch, {"action": "context", "guild_id": GUILD, "message_id": MESSAGE, "context_before": 20, "context_after": 20},
        {"get_message": [row()], "/* context */": [dict(row(), message_id="55555555555555555", content="nearby")]})
    assert len(payload["results"]) == 2
    selects = [sql for sql, _ in conn.calls if "get_message" in sql or "/* context */" in sql]
    assert selects and all("deleted_at IS NULL" in sql for sql in selects)

    status, status_conn, _ = invoke(monkeypatch, {"action": "status", "guild_id": GUILD}, {"/* status */": [{"channel_id": CHANNEL, "channel_name": "general", "live_message_count": 1}]})
    assert status["channels"][0]["live_message_count"] == 1
    assert "m.deleted_at IS NULL" in next(sql for sql, _ in status_conn.calls if "/* status */" in sql)


def test_tombstoned_or_unknown_exact_id_has_same_not_found_shape(monkeypatch):
    missing, _, _ = invoke(monkeypatch, {"action": "get", "guild_id": GUILD, "message_id": MESSAGE}, {})
    tombstone, conn, _ = invoke(monkeypatch, {"action": "get", "guild_id": GUILD, "message_id": MESSAGE}, {})
    assert missing["results"] == tombstone["results"] == []
    assert "deleted_at IS NULL" in next(sql for sql, _ in conn.calls if "get_message" in sql)


def test_request_time_and_exact_id_validation():
    request = Request.parse({"action": "search", "guild_id": GUILD, "query": "x", "after": "2026-01-01T00:00:00Z", "before": "2026-02-01T00:00:00Z", "author_ids": [OWNER]})
    assert request.after.tzinfo is not None and request.author_ids == (OWNER,)
    with pytest.raises(RetrievalValidationError, match="invalid_time_range"):
        Request.parse({"action": "search", "guild_id": GUILD, "query": "x", "after": "2026-02-01T00:00:00Z", "before": "2026-01-01T00:00:00Z"})
