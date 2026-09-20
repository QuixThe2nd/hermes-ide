from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import discord_history.ingest as ingest_module

from discord_history.db import apply_migrations
from discord_history.ingest import (_iter_export_values, import_export,
                                    load_export, normalize_export,
                                    record_inventory_manifests)

FIXTURE = Path(__file__).parent / "fixtures" / "dce-channel.json"


def test_normalizes_dce_variants_and_related_records():
    guild, channel, messages = load_export(FIXTURE)
    message = messages[0]
    assert (guild.guild_id, channel.channel_id, channel.parent_channel_id) == (
        "10000000000000001", "20000000000000002", "20000000000000001")
    assert channel.is_thread and message.author.global_name == "Alice"
    assert message.reference.message_id == "30000000000000002"
    assert message.attachments[0].filename == "proof.png"
    assert message.embeds[0].title == "Evidence"
    assert {(m.mention_type, m.mentioned_id) for m in message.mentions} == {
        ("user", "40000000000000004"), ("role", "60000000000000006")}
    assert len(message.content_hash) == 64


def test_normalization_is_deterministic_with_missing_related_ids():
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc["messages"][0]["embeds"][0].pop("id", None)
    first = normalize_export(doc)[2][0]
    second = normalize_export(doc)[2][0]
    assert first.content_hash == second.content_hash
    assert first.embeds[0].embed_id == second.embeds[0].embed_id


def test_streaming_decoder_crosses_tiny_chunks_and_rejects_bad_documents(tmp_path):
    values = list(_iter_export_values(FIXTURE, chunk_size=7))
    assert sum(key == "messages" for key, _value in values) == 1
    assert dict((key, value) for key, value in values if key != "messages")["guild"]["id"] == "10000000000000001"

    trailing = tmp_path / "trailing.json"
    trailing.write_text('{"messages":[]} x', encoding="utf-8")
    with pytest.raises(ValueError, match="trailing JSON data"):
        list(_iter_export_values(trailing, chunk_size=2))

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"guild":{},"guild":{},"messages":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate top-level"):
        list(_iter_export_values(duplicate, chunk_size=3))


def test_rejects_cross_channel_and_duplicate_messages():
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc["messages"][0]["channelId"] = "999"
    with pytest.raises(ValueError, match="another channel"):
        normalize_export(doc)
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc["messages"].append(dict(doc["messages"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        normalize_export(doc)


class FakeTransaction:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): self.conn.entered += 1; return self
    def __exit__(self, typ, value, tb): self.conn.exited.append(typ); return False


class FakeConn:
    def __init__(self, fail=False): self.sql=[]; self.entered=0; self.exited=[]; self.rollbacks=0; self.fail=fail; self.rowcount=0
    def transaction(self): return FakeTransaction(self)
    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.fail: raise RuntimeError("injected")
        return self
    def fetchone(self): return None
    def rollback(self): self.rollbacks += 1


def test_streaming_import_writes_related_rows_cursor_and_accounting():
    conn = FakeConn()
    result = import_export(conn, FIXTURE, mode="incremental")
    sql = "\n".join(statement for statement, _params in conn.sql)
    assert result["exported"] == result["inserted"] == 1
    assert result["updated"] == result["tombstoned"] == 0
    assert conn.entered == 1 and conn.exited == [None]
    for table in ("messages", "message_revisions", "attachments", "embeds",
                  "message_mentions", "message_references", "ingest_cursors"):
        assert f"discord_archive.{table}" in sql


def test_file_import_decodes_each_message_once(monkeypatch):
    original = ingest_module._iter_export_values
    decoded = []
    def counted(path, **kwargs):
        for key, value in original(path, **kwargs):
            if key == "messages": decoded.append(value["id"])
            yield key, value
    monkeypatch.setattr(ingest_module, "_iter_export_values", counted)
    result = import_export(FakeConn(), FIXTURE, mode="incremental")
    assert result["exported"] == 1
    assert decoded == ["30000000000000003"]


def test_stream_accepts_and_validates_dce_trailing_message_count(tmp_path):
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["messageCount"] = 1
    path = tmp_path / "with-count.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert len(load_export(path)[2]) == 1
    document["messageCount"] = 2
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="messageCount does not match messages"):
        load_export(path)
    messages = document.pop("messages")
    early = {"messageCount": 1, **document, "messages": messages}
    path.write_text(json.dumps(early), encoding="utf-8")
    with pytest.raises(ValueError, match="messageCount must follow messages"):
        load_export(path)


def test_complete_reconcile_requires_and_uses_source_cutoff():
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    inventory_run_id = uuid4()
    with pytest.raises(ValueError, match="complete_reconcile_requires_cutoff"):
        import_export(FakeConn(), document, mode="reconcile", complete=True,
                      inventory_run_id=inventory_run_id)
    cutoff = datetime(2026, 7, 17, tzinfo=timezone.utc)
    conn = FakeConn()
    result = import_export(conn, document, mode="reconcile", complete=True,
                           inventory_run_id=inventory_run_id, source_before=cutoff)
    tombstone = next((sql, params) for sql, params in conn.sql
                     if "SET deleted_at" in sql)
    assert "created_at<%s" in tombstone[0]
    assert tombstone[1][3] == cutoff
    assert result["tombstoned"] == 0


def test_migration_is_one_transaction_and_contains_contract(tmp_path):
    source = (Path(__file__).parents[1] / "migrations" / "001_initial.sql").read_text(encoding="utf-8")
    required = ["schema_migrations", "ingest_run_scope", "inventory_endpoint_manifests",
                "inventory_pages", "inventory_parent_unions", "message_revisions",
                "attachments", "embeds", "message_mentions", "message_references",
                "ingest_cursors", "content_tsv", "pg_trgm"]
    assert all(name in source for name in required)
    assert "unaccent" in (Path(__file__).parents[1] / "migrations" / "002_unaccent.sql").read_text(encoding="utf-8")
    hardening = (Path(__file__).parents[1] / "migrations" / "004_harden_search_audit.sql").read_text(encoding="utf-8")
    assert "principal_user_hmac" in hardening and "DROP COLUMN IF EXISTS principal_user_id" in hardening
    assert "search_audit_append_only" in hardening
    migration = tmp_path / "001_initial.sql"; migration.write_text(source, encoding="utf-8")
    conn = FakeConn()
    assert apply_migrations(conn, tmp_path) == ["001_initial.sql"]
    assert conn.entered == 2 and conn.exited == [None, None]
    assert any("schema_migrations WHERE version" in sql for sql, _ in conn.sql)


def test_migration_failure_rolls_back(tmp_path):
    (tmp_path / "001_bad.sql").write_text("broken", encoding="utf-8")
    conn = FakeConn(fail=True)
    with pytest.raises(RuntimeError, match="injected"):
        apply_migrations(conn, tmp_path)
    assert conn.rollbacks == 1


def test_inventory_manifests_are_atomic_and_parameterized():
    from uuid import uuid4
    conn = FakeConn()
    record_inventory_manifests(conn, uuid4(), "parent", [{
        "endpoint": "public", "state": "complete", "final_cursor": "cursor",
        "thread_ids": ["1"], "global_union_ids": ["1"], "termination_reason": "has_more_false",
        "pages": [{"request_cursor": None, "response_cursor": "cursor", "has_more": False,
                   "fingerprint": "sha256", "thread_ids": ["1"]}],
    }], {"state": "complete", "active_thread_ids": [], "archived_thread_ids": ["1"],
         "all_thread_ids": ["1"], "termination_reason": "all_endpoints_complete"})
    assert conn.entered == 1
    assert len(conn.sql) == 4
    assert all(params is not None for _, params in conn.sql)


@pytest.mark.skipif(not os.getenv("DISCORD_HISTORY_TEST_DATABASE_URL"), reason="real PostgreSQL opt-in")
def test_real_postgres_migration_idempotence():
    from discord_history.db import connect
    conn = connect(os.environ["DISCORD_HISTORY_TEST_DATABASE_URL"])
    try:
        apply_migrations(conn); apply_migrations(conn)
        count = conn.execute("SELECT count(*) FROM discord_archive.schema_migrations WHERE version=1").fetchone()[0]
        assert count == 1
    finally:
        conn.close()
