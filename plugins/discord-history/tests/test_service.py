from __future__ import annotations

from pathlib import Path

import pytest

from discord_history.config import PluginConfig, Secrets
from discord_history import service

GUILD = "22222222222222222"
CHANNEL = "33333333333333333"
OWNER = "11111111111111111"


class Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, sql, _params=()):
        if "pg_try_advisory_lock" in sql or "pg_advisory_unlock" in sql:
            return Result((True,))
        return Result()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class Exporter:
    def __init__(self, _binary):
        pass

    def export(self, request, _token):
        request.output.write_text("{}", encoding="utf-8")
        return {"state": "ok", "exit_code": 0, "output": str(request.output)}


def configure(monkeypatch, tmp_path: Path, conn: Connection):
    config = PluginConfig.from_mapping({
        "owner_user_ids": [OWNER],
        "allowed_guild_ids": [GUILD],
        "allowed_channel_ids": {GUILD: [CHANNEL]},
    })
    metadata = {"id": CHANNEL, "guild_id": GUILD, "type": 0, "name": "general"}
    monkeypatch.setattr(service, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(service, "DCE_BINARY", tmp_path / "dce")
    monkeypatch.setattr(service, "load_plugin_config", lambda: config)
    monkeypatch.setattr(service, "load_secrets", lambda: Secrets("postgresql://test/db", b"k" * 32))
    monkeypatch.setattr(service, "connect", lambda _dsn: conn)
    monkeypatch.setattr(service, "_discord_helpers", lambda: (lambda *_a, **_k: metadata, lambda: "token"))
    monkeypatch.setattr("discord_history.dce.DCEExporter", Exporter)


def test_sync_commits_cursor_transaction_and_import(monkeypatch, tmp_path):
    conn = Connection()
    configure(monkeypatch, tmp_path, conn)
    monkeypatch.setattr(
        "discord_history.ingest.import_export",
        lambda *_a, **_k: {"run_id": "probe", "exported": 1, "inserted": 1, "updated": 0, "tombstoned": 0},
    )

    result = service.run_sync(
        guild_id=GUILD, channel_ids=[CHANNEL], mode="backfill", keep_export=False
    )

    assert result["ok"] is True
    assert conn.commits == 4
    assert conn.rollbacks == 0
    assert conn.closed is True
    assert not list((tmp_path / "tmp").glob("**/*.json"))


def test_sync_rolls_back_and_preserves_failed_export(monkeypatch, tmp_path):
    conn = Connection()
    configure(monkeypatch, tmp_path, conn)

    def fail(*_args, **_kwargs):
        raise RuntimeError("import failed")

    monkeypatch.setattr("discord_history.ingest.import_export", fail)
    with pytest.raises(RuntimeError, match="import failed"):
        service.run_sync(
            guild_id=GUILD, channel_ids=[CHANNEL], mode="backfill", keep_export=False
        )

    assert conn.commits == 3
    assert conn.rollbacks == 1
    assert conn.closed is True
    assert len(list((tmp_path / "tmp").glob("**/*.json"))) == 1


def test_sync_returns_clean_already_locked_without_export(monkeypatch, tmp_path):
    class LockedConnection(Connection):
        def execute(self, sql, params=()):
            if "pg_try_advisory_lock" in sql:
                return Result((False,))
            return super().execute(sql, params)

    conn = LockedConnection()
    configure(monkeypatch, tmp_path, conn)
    result = service.run_sync(
        guild_id=GUILD, channel_ids=[CHANNEL], mode="incremental", keep_export=False
    )

    assert result["ok"] is False
    assert result["results"] == [
        {"channel_id": CHANNEL, "ok": False, "state": "already_locked"}
    ]
    assert conn.commits == 1
    assert conn.closed is True
    assert not list((tmp_path / "tmp").glob("**/*.json"))
