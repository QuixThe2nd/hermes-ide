from __future__ import annotations

import json
import hashlib
import hmac
import os
import stat
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from discord_history.auth import AuthorizedScope, GatewayPrincipal
from discord_history.dce import DCEExportError, DCEExporter, ExportRequest
from discord_history.retrieval import (
    MAX_JSON_BYTES,
    Request,
    RetrievalService,
    RetrievalValidationError,
)
from discord_history.sync import plan_reconciliation


GUILD = "22222222222222222"
ROOT = "33333333333333333"
CHANNEL = "44444444444444444"
OWNER = "11111111111111111"
MESSAGE = "55555555555555555"


class Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []
        self.closed = False
        self.committed = False

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        for marker, response in self.routes.items():
            if marker in sql:
                rows = response(sql, params) if callable(response) else response
                return Result(rows)
        return Result()

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def scope(*, channels=(CHANNEL,), roots=(ROOT,)):
    return AuthorizedScope(
        GatewayPrincipal("discord", OWNER, CHANNEL, ""),
        GUILD,
        frozenset(channels),
        frozenset(roots),
    )


def message_row(**overrides):
    row = {
        "message_id": MESSAGE,
        "guild_id": GUILD,
        "guild_name": "Guild",
        "channel_id": CHANNEL,
        "channel_name": "General",
        "parent_channel_id": ROOT,
        "parent_channel_name": "Root Channel",
        "is_thread": True,
        "author_id": OWNER,
        "username": "owner",
        "global_name": "Owner",
        "author_name_snapshot": "Owner at send time",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "edited_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        "content": "edited content",
        "coverage_state": "partial",
        "coverage_start": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "coverage_end": datetime(2026, 1, 3, tzinfo=timezone.utc),
    }
    row.update(overrides)
    return row


def run_service(request, routes, *, authorized_scope=None):
    conn = FakeConnection(routes)
    payload = RetrievalService(lambda _dsn: conn, b"k" * 32).run(
        "postgresql://unused/test", authorized_scope or scope(), request
    )
    return payload, conn


def test_reconciliation_rejects_malformed_inventory_and_reports_missing_roots():
    with pytest.raises(AttributeError):
        plan_reconciliation([ROOT], {ROOT: None}, {})

    result = plan_reconciliation([ROOT, CHANNEL], {CHANNEL: {"state": "complete"}}, {CHANNEL: "ok"})
    assert result["complete"] is False
    assert result["tombstone_safe"] is False
    assert result["missing_inventory_ids"] == [ROOT]
    assert result["missing_export_ids"] == [ROOT]
    assert result["termination_reason"] == "scope_set_mismatch"


def test_missing_authorized_roots_resolves_no_channels_and_returns_miss_coverage():
    request = Request.parse({"action": "get", "guild_id": GUILD, "message_id": MESSAGE})
    payload, conn = run_service(request, {}, authorized_scope=scope(channels=(), roots=()))

    assert payload["results"] == []
    assert payload["coverage"] == {
        "channel_count": 0,
        "state_counts": {},
        "channels": [],
        "channels_truncated": False,
    }
    resolve = next(params for sql, params in conn.calls if "/* resolve_channels */" in sql)
    assert resolve == [GUILD, [], []]
    assert not any("/* get_message */" in sql for sql, _ in conn.calls)


def test_dce_nonzero_exit_keeps_created_output_private(tmp_path):
    binary = tmp_path / "dce"
    binary.write_text("fake", encoding="utf-8")
    binary.chmod(0o700)
    output = tmp_path / "exports" / "failed.json"

    def fail(argv, **_kwargs):
        # DCE writes into the private file pre-created by the exporter.
        output.write_text("partial secret export", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 9, "", "failure")

    exporter = DCEExporter(binary, runner=fail)
    with pytest.raises(DCEExportError) as caught:
        exporter.export(ExportRequest(CHANNEL, output), "secret")

    assert caught.value.code == "dce_failed"
    assert caught.value.manifest["exit_code"] == 9
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "arguments,error",
    [
        ({"action": "search", "guild_id": GUILD, "query": "x", "limit": True}, "invalid_limit"),
        ({"action": "search", "guild_id": GUILD, "query": "x", "after": "2026-01-01"}, "invalid_after"),
        ({"action": "search", "guild_id": GUILD, "query": "x", "channel_ids": [CHANNEL, CHANNEL]}, "invalid_channel_ids"),
        ({"action": "get", "guild_id": GUILD, "message_id": MESSAGE, "query": "forbidden"}, "invalid_arguments"),
        ({"action": "context", "guild_id": GUILD, "message_id": MESSAGE, "context_before": -1}, "invalid_context_before"),
        ({"action": "status", "guild_id": GUILD, "channel_names": []}, "invalid_channel_names"),
    ],
)
def test_request_parse_strictly_rejects_ambiguous_or_action_invalid_input(arguments, error):
    with pytest.raises(RetrievalValidationError, match=f"^{error}$"):
        Request.parse(arguments)


def test_search_miss_still_describes_partial_scope_coverage():
    request = Request.parse({"action": "search", "guild_id": GUILD, "query": "absent", "context_before": 0, "context_after": 0})
    coverage_row = {
        "channel_id": CHANNEL,
        "coverage_state": "partial",
        "coverage_start": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "coverage_end": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    payload, _ = run_service(
        request,
        {"/* resolve_channels */": [{"channel_id": CHANNEL}], "/* scope_coverage */": [coverage_row]},
    )

    assert payload["results"] == []
    assert payload["coverage"]["channel_count"] == 1
    assert payload["coverage"]["state_counts"] == {"partial": 1}
    assert payload["coverage"]["channels"][0] == {
        "channel_id": CHANNEL,
        "state": "partial",
        "start": "2025-01-01T00:00:00Z",
        "end": "2026-01-01T00:00:00Z",
    }


def test_exact_channel_name_resolution_short_circuits_fuzzy_resolution():
    request = Request.parse({"action": "status", "guild_id": GUILD, "channel_names": ["  GENERAL  "]})
    payload, conn = run_service(
        request,
        {
            "resolve_channels_exact": [{"channel_id": CHANNEL}],
            "/* status */": [{"channel_id": CHANNEL, "channel_name": "General", "live_message_count": 0}],
        },
    )

    assert payload["channels"][0]["channel_id"] == CHANNEL
    exact_sql, exact_params = next(call for call in conn.calls if "resolve_channels_exact" in call[0])
    assert "lower(name) = ANY(%s)" in exact_sql
    assert exact_params[-1] == ["general"]
    assert not any("similarity(lower(name)" in sql for sql, _ in conn.calls)


def test_get_returns_edited_metadata_and_oldest_to_newest_bounded_revisions():
    revisions = [
        {"revision_no": 3, "content_hash": "hash-3", "content": "third", "observed_at": datetime(2026, 1, 3, tzinfo=timezone.utc)},
        {"revision_no": 2, "content_hash": "hash-2", "content": "second", "observed_at": datetime(2026, 1, 2, tzinfo=timezone.utc)},
    ]
    request = Request.parse({"action": "get", "guild_id": GUILD, "message_id": MESSAGE})
    payload, conn = run_service(
        request,
        {
            "/* resolve_channels */": [{"channel_id": CHANNEL}],
            "/* get_message */": [message_row()],
            "/* revisions */": revisions,
        },
    )

    record = payload["results"][0]
    assert record["edited_at"] == "2026-01-02T03:04:05Z"
    assert record["author_name"] == "Owner at send time"
    assert record["parent_channel_id"] == ROOT
    assert record["parent_channel_name"] == "Root Channel"
    assert [revision["revision_no"] for revision in record["revisions"]] == [2, 3]
    assert record["revisions"][0]["observed_at"] == "2026-01-02T00:00:00Z"
    revision_sql = next(sql for sql, _ in conn.calls if "/* revisions */" in sql)
    assert "ORDER BY revision_no DESC LIMIT 20" in revision_sql


def test_result_byte_bound_drops_context_before_primary_results():
    service = RetrievalService(lambda _dsn: None, b"k" * 32)
    large = "é" * 30_000
    payload = {
        "action": "search",
        "results": [
            {"message_id": "1", "match_type": "fts", "snippet": large},
            {"message_id": "2", "match_type": "context", "snippet": large},
            {"message_id": "3", "match_type": "context", "snippet": large},
        ],
        "coverage": {},
        "truncated": False,
        "omitted_context_count": 0,
        "omitted_result_count": 0,
    }

    bounded = service._bound(payload)
    assert [row["message_id"] for row in bounded["results"]] == ["1"]
    assert bounded["omitted_context_count"] == 2
    assert bounded["omitted_result_count"] == 0
    assert bounded["truncated"] is True
    assert len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_JSON_BYTES


def test_result_record_bound_drops_farthest_context_and_hides_internal_distance():
    service = RetrievalService(lambda _dsn: None, b"k" * 32)
    contexts = [
        {"message_id": "far", "match_type": "context",
         "_context_distance": 999.0, "_context_anchor_id": "hit"},
        *[{"message_id": f"near-{index}", "match_type": "context",
           "_context_distance": float(index % 3), "_context_anchor_id": "hit"}
          for index in range(499)],
    ]
    payload = {"action": "search",
               "results": [{"message_id": "hit", "match_type": "fts"}, *contexts],
               "coverage": {}, "truncated": False,
               "omitted_context_count": 0, "omitted_result_count": 0}

    bounded = service._bound(payload)

    ids = [row["message_id"] for row in bounded["results"]]
    assert len(ids) == 500 and "hit" in ids and "far" not in ids
    assert bounded["omitted_context_count"] == 1
    assert all(not any(key.startswith("_") for key in row)
               for row in bounded["results"])


def test_overlapping_context_never_demotes_a_ranked_primary(monkeypatch):
    first = message_row(message_id="55555555555555551", score=2.0)
    second = message_row(message_id="55555555555555552", score=1.0,
                         created_at=datetime(2026, 1, 1, 0, 1,
                                             tzinfo=timezone.utc))
    conn = FakeConnection({"/* search_fts */": [first, second]})
    service = RetrievalService(lambda _dsn: conn, b"k" * 32)
    monkeypatch.setattr(
        service, "_neighbour_rows",
        lambda _conn, _scope, anchor, _before, _after:
        [second] if anchor["message_id"] == first["message_id"] else [],
    )
    request = Request.parse({"action": "search", "guild_id": GUILD,
                             "query": "edited", "context_before": 1,
                             "context_after": 1})

    payload = service._bound(service._search(
        conn, scope(), request, (CHANNEL,), None, {}
    ))

    assert [row["message_id"] for row in payload["results"]] == [
        first["message_id"], second["message_id"]
    ]
    assert all(row["match_type"] != "context" for row in payload["results"])
    assert all(not any(key.startswith("_") for key in row)
               for row in payload["results"])


def test_run_audits_only_delivered_ids_with_hmac_and_complete_filter_metadata(monkeypatch):
    conn = FakeConnection()
    service = RetrievalService(lambda _dsn: conn, b"k" * 32)
    request = Request.parse({"action": "search", "guild_id": GUILD, "query": "needle",
                             "channel_names": ["General"], "author_names": ["Owner"]})
    monkeypatch.setattr(service, "_resolve_channels", lambda *_: (CHANNEL,))
    monkeypatch.setattr(service, "_resolve_authors", lambda *_: (OWNER,))
    monkeypatch.setattr(service, "_scope_coverage", lambda *_: {})
    candidates = [{"message_id": str(i), "match_type": "context"} for i in range(600)]
    monkeypatch.setattr(service, "_search", lambda *_: {
        "action": "search", "results": candidates, "coverage": {},
        "truncated": False, "omitted_context_count": 0, "omitted_result_count": 0,
    })
    payload = service.run("postgresql://unused/test", scope(), request)
    audit_sql, audit_params = next(call for call in conn.calls if "/* search_audit */" in call[0])
    delivered = [row["message_id"] for row in payload["results"]]
    assert len(delivered) == 500 and payload["omitted_context_count"] == 100
    assert audit_params[5] == delivered
    assert "principal_user_hmac" in audit_sql and "principal_user_id" not in audit_sql
    assert audit_params[0] == hmac.new(b"k" * 32, OWNER.encode(), hashlib.sha256).hexdigest()
    assert audit_params[3] == hmac.new(b"k" * 32, b"needle", hashlib.sha256).hexdigest()
    metadata = json.loads(audit_params[4])
    assert metadata["channel_names"] == ["General"]
    assert metadata["author_names"] == ["Owner"]
    assert metadata["message_id"] is None
    assert metadata["limit"] == 10
    assert metadata["context_before"] == metadata["context_after"] == 3
    assert metadata["effective_channel_ids"] == [CHANNEL]
    assert metadata["effective_author_ids"] == [OWNER]
    assert metadata["authorized_root_channel_ids"] == [ROOT]
    assert all("m.*" not in sql for sql, _params in conn.calls)


def test_channel_resolution_has_a_hard_sql_and_materialization_limit():
    rows = [{"channel_id": str(10**16 + i)} for i in range(1001)]
    conn = FakeConnection({"resolve_channels": rows})
    service = RetrievalService(lambda _dsn: conn, b"k" * 32)
    request = Request.parse({"action": "status", "guild_id": GUILD})
    with pytest.raises(RetrievalValidationError, match="scope_too_large"):
        service.run("postgresql://unused/test", scope(), request)
    resolver_sql = next(sql for sql, _params in conn.calls if "/* resolve_channels */" in sql)
    assert "LIMIT 1001" in resolver_sql
    assert conn.closed is True


def test_installer_restores_previous_tree_after_late_switch_failure(tmp_path):
    archive = tmp_path / "dce.zip"
    launcher = zipfile.ZipInfo("DiscordChatExporter.Cli")
    launcher.external_attr = (stat.S_IFREG | 0o755) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(launcher, "#!/bin/sh\nprintf 'v2.47.3\\n'\n")
        bundle.writestr("new-marker", "new")
    destination = tmp_path / "installed"
    old = destination / "2.47.3"
    old.mkdir(parents=True)
    (old / "old-marker").write_text("old", encoding="utf-8")
    old_launcher = old / "DiscordChatExporter.Cli"
    old_launcher.write_text("#!/bin/sh\nprintf 'old-tree\\n'\n", encoding="utf-8")
    old_launcher.chmod(0o700)
    (destination / "current").symlink_to("2.47.3")
    wrappers = tmp_path / "bin"
    wrappers.mkdir()
    (wrappers / "curl").write_text("""#!/usr/bin/env python3
import os,shutil,sys
shutil.copy2(os.environ['FAKE_DCE_ZIP'], sys.argv[sys.argv.index('--output') + 1])
""", encoding="utf-8")
    (wrappers / "sha256sum").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (wrappers / "mv").write_text("""#!/bin/sh
case "$*" in *'.current.new'*'current'*) exit 97;; esac
exec /bin/mv "$@"
""", encoding="utf-8")
    for wrapper in wrappers.iterdir(): wrapper.chmod(0o700)
    env = os.environ.copy()
    env["PATH"] = f"{wrappers}:{env['PATH']}"
    env["FAKE_DCE_ZIP"] = str(archive)
    script = Path(__file__).parents[1] / "scripts" / "install-dce"
    completed = subprocess.run([str(script), str(destination)], env=env,
                               text=True, capture_output=True, timeout=30)
    assert completed.returncode == 97
    assert (destination / "2.47.3" / "old-marker").read_text(encoding="utf-8") == "old"
    assert not (destination / "2.47.3" / "new-marker").exists()
    assert not (destination / ".current.new").exists()
    assert subprocess.run([str(destination / "current" / "DiscordChatExporter.Cli")],
                          text=True, capture_output=True).stdout.strip() == "old-tree"
