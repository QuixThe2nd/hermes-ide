from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from discord_history import doctor, verify
from discord_history.config import ConfigError, Secrets


GUILD = "12345678901234567"
CHANNEL = "22345678901234567"
MESSAGE = "32345678901234567"
VALID_CONFIG = {
    "owner_user_ids": ["42345678901234567"],
    "allowed_guild_ids": [GUILD],
    "allowed_channel_ids": {GUILD: [CHANNEL]},
}


class ScriptedConnection:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.closed = False

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return SimpleNamespace(**next(self.responses))

    def close(self):
        self.closed = True


def test_verify_channel_passes_complete_accounting_and_linked_evidence():
    stamp = datetime(2026, 7, 17, 12, 30, tzinfo=timezone.utc)
    conn = ScriptedConnection([
        {"fetchone": lambda: (1,)},
        {"fetchone": lambda: (8, 2, 10)},
        {"fetchone": lambda: (1,)},
        {"fetchall": lambda: [("m1", "h1", stamp), ("m2", "h2", stamp)]},
        {"fetchall": lambda: [("deleted",)]},
        {"fetchone": lambda: (1,)},
    ])

    result = verify.verify_channel(
        conn, CHANNEL, cutoff=stamp + timedelta(seconds=1),
        dce_messages={"m1": ("h1", stamp), "m2": ("h2", stamp)},
    )

    assert result["ok"] is True
    assert all(result["checks"].values())
    assert result["window_live_count"] == result["exported_count"] == 2
    assert result["sampled_message_ids"] == ["m1", "m2"]
    assert conn.calls[-1][1] == (CHANNEL,)
    assert "inventory_parent_unions" in conn.calls[-1][0]


def test_verify_channel_unknown_id_fails_closed_without_linked_lookup():
    stamp = datetime(2026, 7, 17, 12, 30, tzinfo=timezone.utc)
    conn = ScriptedConnection([
        {"fetchone": lambda: None},
        {"fetchone": lambda: None},
        {"fetchone": lambda: (1,)},
        {"fetchall": lambda: []},
        {"fetchall": lambda: []},
    ])

    result = verify.verify_channel(conn, CHANNEL, cutoff=stamp, dce_messages={})

    assert result["ok"] is False
    assert result["checks"] == {
        "channel_exists": False,
        "canonical_accounting": False,
        "coverage_complete": True,
        "linked_inventory_reconciliation": False,
        "dce_live_set_equality": True,
        "tombstones_disjoint": True,
        "timestamp_bounds_equal": True,
        "sampled_content_hashes_equal": True,
    }
    assert (result["live_count"], result["tombstone_count"], result["total_count"]) == (0, 0, 0)
    assert len(conn.calls) == 5


def test_verify_e2e_passes_only_when_persisted_and_callable_probes_pass():
    conn = ScriptedConnection([
        {"fetchall": lambda: [(1,), (2,), (3,), (4,)]},
        {"fetchone": lambda: (CHANNEL,)},
        {"fetchone": lambda: (1,)},
        {"fetchone": lambda: (1,)},
        {"fetchone": lambda: (1,)},
    ])
    probe_calls = []

    result = verify.verify_e2e(
        conn,
        guild_id=GUILD,
        owner_audit_id="audit-1",
        expected_message_id=MESSAGE,
        expected_phrase=None,
        owner_principal_hmacs=["a" * 64],
        probes=lambda: probe_calls.append(True) or {
            "no_db_denial_probes": True,
            "denial_log_checks": True,
            "retrieval_checks": True,
            "dce_set_equality": True,
            "dce_hash_equality": True,
            "retrieval_bounds_and_citation": True,
        },
    )

    assert result["verdict"] == "PASS"
    assert result["failed_checks"] == []
    assert all(result["checks"].values())
    assert probe_calls == [True]
    audit_params = conn.calls[2][1]
    assert audit_params[0] == "audit-1" and audit_params[1] == ["a" * 64]
    assert audit_params[3] == MESSAGE


def test_verify_e2e_missing_message_and_default_probes_produce_objective_failures():
    conn = ScriptedConnection([
        {"fetchall": lambda: [(1,), (3,)]},
        {"fetchone": lambda: None},
        {"fetchone": lambda: None},
    ])

    result = verify.verify_e2e(
        conn,
        guild_id=GUILD,
        owner_audit_id="audit-2",
        expected_message_id=MESSAGE,
        expected_phrase="absent",
        owner_principal_hmacs=["a" * 64],
    )

    assert result["verdict"] == "FAIL"
    assert {
        "schema_idempotence",
        "expected_live_message",
        "recent_owner_audit_proof",
        "linked_inventory_reconciliation",
        "freshness",
        "no_db_denial_probes",
        "denial_log_checks",
        "retrieval_checks",
        "dce_set_equality",
        "dce_hash_equality",
        "retrieval_bounds_and_citation",
    } == set(result["failed_checks"])
    assert len(conn.calls) == 3  # no channel-scoped queries when the message is unknown


def test_doctor_success_checks_secrets_config_pinned_dce_and_database(monkeypatch, tmp_path):
    payload = b"pinned dce archive"
    executable = tmp_path / "DiscordChatExporter.Cli"
    archive = tmp_path / "dce.zip"
    executable.write_bytes(b"executable")
    archive.write_bytes(payload)
    monkeypatch.setattr(doctor, "DCE_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(
        "discord_history.config.load_secrets",
        lambda: Secrets("postgresql://archive/discord", b"k" * 32),
    )
    conn = ScriptedConnection([
        {"fetchone": lambda: ("discord",)},
        {"fetchall": lambda: [("pg_trgm",)]},
    ])
    connector_calls = []

    result = doctor.run_doctor(
        connector=lambda dsn: connector_calls.append(dsn) or conn,
        config_mapping=VALID_CONFIG,
        dce_path=executable,
        dce_archive_path=archive,
        environ={"DISCORD_BOT_TOKEN": "present"},
        runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=f"DiscordChatExporter {doctor.DCE_VERSION}", stderr=""
        ),
    )

    assert result["ok"] is True
    assert {check["name"]: check["ok"] for check in result["checks"]} == {
        "secrets": True,
        "audit_hmac_key": True,
        "discord_bot_token": True,
        "plugin_config": True,
        "dce_checksum": True,
        "dce_version": True,
        "database": True,
        "extensions": True,
    }
    assert connector_calls == ["postgresql://archive/discord"]
    assert conn.closed is True


def test_doctor_reports_redacted_config_dce_and_database_failures(monkeypatch, tmp_path):
    executable = tmp_path / "DiscordChatExporter.Cli"
    archive = tmp_path / "dce.zip"
    executable.write_bytes(b"executable")
    archive.write_bytes(b"wrong archive")
    monkeypatch.setattr(
        "discord_history.config.load_secrets",
        lambda: Secrets("postgresql://contains-secret/discord", b"k" * 32),
    )

    result = doctor.run_doctor(
        connector=lambda _dsn: (_ for _ in ()).throw(RuntimeError("database leaked detail")),
        config_mapping={},
        dce_path=executable,
        dce_archive_path=archive,
        environ={},
        runner=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("cannot execute")),
    )

    assert result["ok"] is False
    checks = {check["name"]: check for check in result["checks"]}
    assert checks["discord_bot_token"]["code"] == "discord_bot_token_missing"
    assert checks["plugin_config"]["code"] == "missing_config_key"
    assert checks["dce_checksum"]["code"] == "dce_checksum_mismatch"
    assert checks["dce_version"]["code"] == "dce_version_mismatch"
    assert checks["database"]["code"] == "database_unreachable"
    assert checks["extensions"]["code"] == "required_extension_missing"
    assert "contains-secret" not in repr(result)
    assert "leaked detail" not in repr(result)


def test_doctor_secret_failure_and_missing_dce_do_not_attempt_database(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "discord_history.config.load_secrets",
        lambda: (_ for _ in ()).throw(ConfigError("invalid_secret_mode")),
    )
    connector_calls = []

    result = doctor.run_doctor(
        connector=lambda dsn: connector_calls.append(dsn),
        config_mapping=VALID_CONFIG,
        dce_path=tmp_path / "missing-dce",
        environ={"DISCORD_BOT_TOKEN": "present"},
    )

    checks = {check["name"]: check for check in result["checks"]}
    assert result["ok"] is False
    assert checks["secrets"]["code"] == "invalid_secret_mode"
    assert checks["audit_hmac_key"]["code"] == "secrets_unavailable"
    assert checks["dce_checksum"]["code"] == checks["dce_version"]["code"] == "dce_missing"
    assert checks["database"]["code"] == "database_unreachable"
    assert connector_calls == []


def test_requirements_ready_reflects_doctor_verdict(monkeypatch):
    monkeypatch.setattr(doctor, "run_doctor", lambda: {"ok": True})
    assert doctor.requirements_ready() is True
    monkeypatch.setattr(doctor, "run_doctor", lambda: {"ok": False})
    assert doctor.requirements_ready() is False
