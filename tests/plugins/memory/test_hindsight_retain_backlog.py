"""Hindsight retain backlog: provider-rate-limited retains persist to a durable
per-profile JSONL backlog owned by the Go runtime (``internal/retainbacklog``,
invoked as the ``cmd/retainbacklog`` binary) and drain automatically once the
limit resets.

The Python side is a bridge: classification of the real failure signature
(HTTP 500 wrapping ``ProviderRateLimitResetError`` / ``deferred by provider
quota``), the SDK network half of the drain, and wakeup scheduling. Queue
state, persistence, locking, and the reconcile table are the Go runtime's —
exercised here through the real binary.

Contract (changed from the first cut of this feature, per the server-semantics
review): an entry is removed ONLY on an observed server-side ``completed``;
``pending``/``processing`` keep the entry and poll; ``failed``/``cancelled``
re-queue via the server's own ``retry_operation`` — never delete+resubmit;
``not_found`` submits idempotently by the persisted operation id and the entry
then stays queued until completion is observed. A fresh (never-submitted)
entry submits directly — the original sync attempt minted no server row.
Replays carry the same ``document_id`` as the original attempt so partially
committed chunks are recovered by chunk-hash, never duplicated.
"""

import json
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_constants import get_hermes_home
from plugins.memory.hindsight import HindsightMemoryProvider
from plugins.memory.hindsight import retain_backlog as rb
from plugins.memory.hindsight.retain_backlog import (
    BACKLOG_FILENAME,
    RetainBacklog,
    classify_exception,
    classify_rate_limit,
)

UTC = timezone.utc

# Every behaviour test below drives the real Go runtime binary; classification
# tests are pure functions and always run.
_RUNTIME_BUILDABLE = shutil.which("go") is not None or rb._prebuilt_binary() is not None
requires_runtime = pytest.mark.skipif(
    not _RUNTIME_BUILDABLE,
    reason="retain-backlog Go runtime unavailable (no go toolchain, no prebuilt binary)",
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Temp HERMES_HOME (real env var, no binding patch) and no stale HINDSIGHT_* env."""
    for key in (
        "HINDSIGHT_API_KEY",
        "HINDSIGHT_API_URL",
        "HINDSIGHT_BANK_ID",
        "HINDSIGHT_BUDGET",
        "HINDSIGHT_MODE",
        "HINDSIGHT_TIMEOUT",
        "HINDSIGHT_IDLE_TIMEOUT",
        "HINDSIGHT_RETAIN_TAGS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user-home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *args, **kwargs: None)
    return tmp_path


class _FakeApiError(Exception):
    """ServiceException-shaped double: int ``status`` + str ``body``, both in str()."""

    def __init__(self, status, body):
        super().__init__(
            f"({status})\nReason: Server Error\nHTTP response body: {body}"
        )
        self.status = status
        self.body = body


def _quota_body(retry_at: str) -> str:
    return json.dumps({
        "detail": (
            "Fact extraction deferred by provider quota: 1/1 chunks failed. "
            "First failures: chunk 0: ProviderRateLimitResetError: Provider quota "
            "exhausted (openai/glm-5.3-flash, scope=retain_extract_facts); "
            f"retry at {retry_at}: HTTP 429: quota exceeded"
        )
    })


def _backlog_path(home: Path) -> Path:
    return home / "hindsight" / BACKLOG_FILENAME


def _read_backlog(home: Path) -> list[dict]:
    path = _backlog_path(home)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_backlog(home: Path, entries: list[dict]) -> None:
    path = _backlog_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _seed_entry(home: Path, *, op_id: str | None = None, submitted: bool = True,
                content: str = "seeded", retry: str | None = None,
                document_id: str | None = None) -> str:
    """One due backlog entry as the Go runtime persists it."""
    entry: dict = {
        "id": op_id or str(uuid.uuid4()),
        "bank_id": "test-bank",
        "item": {"content": content},
        "document_id": document_id or f"doc-{uuid.uuid4()}",
        "queued_at": _past_iso(),
        "retry_after": retry or _past_iso(),
    }
    if submitted:
        entry["submitted"] = True
    entries = _read_backlog(home)
    entries.append(entry)
    _write_backlog(home, entries)
    return entry["id"]


def _future_iso(hours: float = 1.0) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


def _past_iso() -> str:
    return (datetime.now(UTC) - timedelta(minutes=5)).isoformat()


def _flip_retry_after(home: Path, retry_after: str) -> None:
    """Simulate time passing: every entry becomes due at *retry_after*."""
    entries = _read_backlog(home)
    for entry in entries:
        entry["retry_after"] = retry_after
    _write_backlog(home, entries)


class _ScriptedRemote:
    """BacklogRemote double for direct RetainBacklog.drain tests: records
    every call; statuses are scriptable per operation id."""

    def __init__(self, statuses: dict[str, str] | None = None,
                 submit_exc: Exception | None = None):
        self.calls: list[tuple] = []
        self.statuses = dict(statuses or {})
        self.submit_exc = submit_exc

    def status(self, bank_id: str, op_id: str) -> str:
        self.calls.append(("status", op_id))
        return self.statuses.get(op_id, "not_found")

    def retry_operation(self, bank_id: str, op_id: str) -> str:
        self.calls.append(("retry", op_id))
        if op_id not in self.statuses:
            return "gone"
        self.statuses[op_id] = "pending"
        return "ok"

    def submit(self, entry: dict) -> None:
        self.calls.append(("submit", entry["id"], entry.get("document_id")))
        if self.submit_exc is not None:
            raise self.submit_exc
        # The server accepts the async retain: op exists, pending.
        self.statuses[entry["id"]] = "pending"


def _fake_client(
    outcomes: list | None = None,
    sink: list | None = None,
    *,
    op_status="not_found",
    retry_sink: list | None = None,
):
    """MagicMock Hindsight client; ``aretain_batch`` records kwargs into *sink*.

    ``outcomes`` pop per call: an Exception is raised, anything else returns ok.
    Exhausted outcomes (or None) mean success. ``op_status`` drives the
    operations API double: a constant status string, a callable op_id ->
    status, ``"raise"`` (probe transport failure), or ``"NotFound"`` (SDK
    NotFoundException). ``retry_sink`` records retry_operation calls.
    ``delete_operation`` stays a bare mock so tests can assert the drain never
    deletes an operation row.
    """
    from hindsight_client_api.exceptions import NotFoundException

    sink = sink if sink is not None else []
    retry_sink = retry_sink if retry_sink is not None else []
    outcomes = list(outcomes or [])

    async def _aretain_batch(**kwargs):
        sink.append(kwargs)
        if outcomes:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        return SimpleNamespace(ok=True, operation_id=kwargs.get("operation_id"))

    async def _get_status(**kwargs):
        status = op_status(kwargs.get("operation_id")) if callable(op_status) else op_status
        if status == "raise":
            raise RuntimeError("status endpoint down")
        if status == "NotFound":
            raise NotFoundException(status=404, body='{"detail": "not found"}')
        return SimpleNamespace(status=status)

    async def _retry_operation(**kwargs):
        retry_sink.append((kwargs.get("bank_id"), kwargs.get("operation_id")))
        return SimpleNamespace(success=True)

    client = MagicMock()
    client.aretain_batch = AsyncMock(side_effect=_aretain_batch)
    client.operations = MagicMock()
    client.operations.get_operation_status = AsyncMock(side_effect=_get_status)
    client.operations.retry_operation = AsyncMock(side_effect=_retry_operation)
    client.aclose = AsyncMock()
    return client, sink


def _make_provider(
    home: Path, client=None, **config_overrides
) -> HindsightMemoryProvider:
    config = {
        "mode": "cloud",
        "apiKey": "test-key",
        "api_url": "http://localhost:9999",
        "bank_id": "test-bank",
        "budget": "mid",
        "memory_mode": "hybrid",
    }
    config.update(config_overrides)
    config_path = home / "hindsight" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config))
    provider = HindsightMemoryProvider()
    if client is not None:
        provider._client = client
    provider._bank_defaults_applied = True  # keep these tests off bank-default chatter
    provider.initialize(session_id="test-session", platform="cli")
    return provider


def _retain(provider: HindsightMemoryProvider, content: str) -> dict:
    return json.loads(
        provider.handle_tool_call("hindsight_retain", {"content": content})
    )


def _wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ---------------------------------------------------------------------------
# Classification (pure functions; no runtime needed)
# ---------------------------------------------------------------------------


class TestClassification:
    def test_http_429_is_rate_limited_with_fallback_retry(self):
        now = datetime(2026, 11, 17, 5, 0, tzinfo=UTC)
        limited, retry_at = classify_rate_limit(
            429, "HTTP 429: too many requests", now=now
        )
        assert limited
        assert retry_at == now + timedelta(hours=1)

    def test_quota_marker_500_parses_retry_at(self):
        retry_iso = "2026-11-17T05:36:33.557843+00:00"
        limited, retry_at = classify_rate_limit(500, _quota_body(retry_iso))
        assert limited
        assert retry_at == datetime.fromisoformat(retry_iso)

    def test_quota_marker_without_retry_at_falls_back_one_hour(self):
        now = datetime(2026, 11, 17, tzinfo=UTC)
        limited, retry_at = classify_rate_limit(
            500, "deferred by provider quota", now=now
        )
        assert limited
        assert retry_at == now + timedelta(hours=1)

    @pytest.mark.parametrize(
        "status,body",
        [
            (500, '{"detail": "internal server error"}'),
            (400, "bad request"),
            (503, "unavailable"),
        ],
    )
    def test_other_failures_are_not_rate_limited(self, status, body):
        limited, retry_at = classify_rate_limit(status, body)
        assert not limited
        assert retry_at is None

    def test_exception_without_status_is_not_rate_limited(self):
        assert classify_exception(ConnectionError("dial tcp: refused")) is None

    def test_exception_shape_is_classified(self):
        retry_iso = "2026-11-17T05:36:33Z"
        exc = _FakeApiError(500, _quota_body(retry_iso))
        assert classify_exception(exc) == datetime(2026, 11, 17, 5, 36, 33, tzinfo=UTC)

    def test_429_exception_body_marker_case_insensitive(self):
        assert (
            classify_exception(_FakeApiError(500, "PROVIDERRATELIMITRESETERROR: spent"))
            is not None
        )


# ---------------------------------------------------------------------------
# Tool path: rate-limited retain queues instead of erroring
# ---------------------------------------------------------------------------


@requires_runtime
class TestToolRetainBacklog:
    def test_rate_limited_retain_queues_instead_of_erroring(self, tmp_path):
        client, sink = _fake_client(
            outcomes=[_FakeApiError(500, _quota_body(_future_iso()))]
        )
        provider = _make_provider(tmp_path, client=client)

        payload = _retain(provider, "user likes dark mode")

        assert "error" not in payload
        assert payload["result"].startswith(
            "Memory queued: provider rate limit; will be stored automatically after 20"
        )
        assert client.aretain_batch.call_count == 1
        # The original call carried the document id the replay will reuse.
        assert sink[0]["document_id"]
        entries = _read_backlog(tmp_path)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["bank_id"] == "test-bank"
        assert entry["item"]["content"] == "user likes dark mode"
        assert entry["document_id"] == sink[0]["document_id"]
        assert entry["retry_after"]
        assert entry["queued_at"]
        assert uuid.UUID(entry["id"])
        # Drain stays gated while the retry hint is in the future.
        provider._retain_queue.join()
        assert len(_read_backlog(tmp_path)) == 1
        assert client.aretain_batch.call_count == 1

    def test_non_rate_limit_500_keeps_today_s_error_output(self, tmp_path):
        exc = _FakeApiError(500, '{"detail": "internal server error"}')
        client, _ = _fake_client(outcomes=[exc, exc])
        provider = _make_provider(tmp_path, client=client)

        payload = _retain(provider, "user likes dark mode")

        assert payload == {"error": f"Failed to store memory: {exc}"}
        assert _read_backlog(tmp_path) == []
        assert not _backlog_path(tmp_path).exists()

    def test_successful_retain_writes_no_backlog(self, tmp_path):
        client, _ = _fake_client()
        provider = _make_provider(tmp_path, client=client)

        payload = _retain(provider, "user likes dark mode")

        assert payload == {"result": "Memory stored successfully."}
        assert not _backlog_path(tmp_path).exists()
        assert provider._retain_backlog.may_exist is False


# ---------------------------------------------------------------------------
# Restart durability + init drain
# ---------------------------------------------------------------------------


@requires_runtime
class TestBacklogPersistence:
    def test_backlog_survives_restart(self, tmp_path):
        client, _ = _fake_client(
            outcomes=[_FakeApiError(500, _quota_body(_future_iso()))]
        )
        _make_provider(tmp_path, client=client).handle_tool_call(
            "hindsight_retain", {"content": "durable memory"}
        )
        assert len(_read_backlog(tmp_path)) == 1

        restarted = _make_provider(tmp_path)  # fresh instance, same home, no client
        assert restarted._retain_backlog.may_exist is True
        # Future-dated entry: the startup drain leaves it queued untouched.
        assert _wait_until(lambda: restarted._retain_backlog.peek()[1] == 1)
        entry = _read_backlog(tmp_path)[0]
        assert entry["item"]["content"] == "durable memory"

    def test_due_backlog_submits_once_on_initialize(self, tmp_path):
        client, _ = _fake_client(
            outcomes=[_FakeApiError(500, _quota_body(_future_iso()))]
        )
        p1 = _make_provider(tmp_path, client=client)
        p1.handle_tool_call("hindsight_retain", {"content": "drain me"})
        p1._retain_queue.join()
        assert len(_read_backlog(tmp_path)) == 1
        op_id = _read_backlog(tmp_path)[0]["id"]
        _flip_retry_after(tmp_path, _past_iso())

        drained_client, sink = _fake_client()  # limit cleared
        p2 = _make_provider(tmp_path, client=drained_client)

        # Init drain submits the due entry exactly once; the entry stays
        # queued (submitted, polling) — removal waits for observed completion.
        assert _wait_until(lambda: drained_client.aretain_batch.call_count == 1)
        assert sink[-1]["operation_id"] == op_id
        assert _wait_until(
            lambda: _read_backlog(tmp_path)
            and _read_backlog(tmp_path)[0].get("submitted")
        )
        # The poll gate holds: a manual pass right after adds no submission.
        p2._drain_retain_backlog()
        assert drained_client.aretain_batch.call_count == 1
        assert len(_read_backlog(tmp_path)) == 1


# ---------------------------------------------------------------------------
# Drain semantics, directly against the runtime
# ---------------------------------------------------------------------------


@requires_runtime
class TestBacklogDrain:
    def test_fifo_submit_then_completion_removal(self, tmp_path):
        backlog = RetainBacklog(lambda: tmp_path)
        past = datetime.now(UTC) - timedelta(minutes=1)
        id1 = backlog.enqueue(bank_id="b", item={"content": "one"},
                              retry_after=past, document_id="doc-1")
        id2 = backlog.enqueue(bank_id="b", item={"content": "two"},
                              retry_after=past, document_id="doc-2")
        remote = _ScriptedRemote()

        next_retry = backlog.drain(remote)
        # Fresh entries submit directly, in FIFO order, carrying their ids and
        # document ids; both stay queued pending observed completion.
        assert [c[:2] for c in remote.calls] == [("submit", id1), ("submit", id2)]
        assert [c[2] for c in remote.calls] == ["doc-1", "doc-2"]
        entries = {e["id"]: e for e in _read_backlog(tmp_path)}
        assert set(entries) == {id1, id2}
        assert all(e.get("submitted") for e in entries.values())
        assert next_retry is not None and next_retry > datetime.now(UTC)

        # Server finishes both: the next pass removes exactly them.
        _flip_retry_after(tmp_path, _past_iso())
        remote.statuses.update({id1: "completed", id2: "completed"})
        next_retry = backlog.drain(remote)
        assert _read_backlog(tmp_path) == []
        assert next_retry is None

    def test_not_due_drain_sends_nothing(self, tmp_path):
        backlog = RetainBacklog(lambda: tmp_path)
        backlog.enqueue(
            bank_id="b",
            item={"content": "x"},
            retry_after=datetime.now(UTC) + timedelta(hours=2),
        )
        calls = []
        remote = _ScriptedRemote()
        next_retry = backlog.drain(remote)
        assert remote.calls == []
        assert next_retry is not None and next_retry > datetime.now(UTC)
        assert backlog.peek()[1] == 1

    def test_drain_stops_on_rate_limit_and_pushes_retry_forward(self, tmp_path):
        backlog = RetainBacklog(lambda: tmp_path)
        past = datetime.now(UTC) - timedelta(minutes=1)
        backlog.enqueue(bank_id="b", item={"content": "one"}, retry_after=past)
        backlog.enqueue(bank_id="b", item={"content": "two"}, retry_after=past)
        new_retry = datetime.now(UTC) + timedelta(hours=3)
        remote = _ScriptedRemote(
            submit_exc=_FakeApiError(500, _quota_body(new_retry.isoformat()))
        )

        next_retry = backlog.drain(remote)

        assert [c[0] for c in remote.calls] == ["submit"]  # FIFO head failed; pass stopped
        entries = _read_backlog(tmp_path)
        assert len(entries) == 2
        assert all(
            datetime.fromisoformat(e["retry_after"]) >= new_retry - timedelta(seconds=1)
            for e in entries
        )
        assert next_retry is not None and next_retry >= new_retry - timedelta(seconds=1)
        # Still limited: the pushed-forward entries are not due, nothing sends.
        remote2 = _ScriptedRemote()
        backlog.drain(remote2)
        assert remote2.calls == []

    def test_drain_keeps_entry_on_other_failure(self, tmp_path):
        backlog = RetainBacklog(lambda: tmp_path)
        backlog.enqueue(
            bank_id="b",
            item={"content": "one"},
            retry_after=datetime.now(UTC) - timedelta(minutes=1),
        )

        remote = _ScriptedRemote(submit_exc=RuntimeError("transient network blip"))
        backlog.drain(remote)
        assert len(_read_backlog(tmp_path)) == 1

        # Backoff elapsed: the entry submits again and this time completes.
        _flip_retry_after(tmp_path, _past_iso())
        ok = _ScriptedRemote()
        backlog.drain(ok)
        assert [c[0] for c in ok.calls] == ["submit"]
        assert len(_read_backlog(tmp_path)) == 1  # ack != completion: still queued

        ok.statuses[_read_backlog(tmp_path)[0]["id"]] = "completed"
        _flip_retry_after(tmp_path, _past_iso())
        backlog.drain(ok)
        assert _read_backlog(tmp_path) == []


# ---------------------------------------------------------------------------
# Writer path: sync_turn job
# ---------------------------------------------------------------------------


@requires_runtime
class TestWriterPath:
    def test_rate_limited_turn_job_queues_backlog_with_same_document(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "plugins.memory.hindsight._check_api_supports_update_mode_append",
            lambda *a, **k: False,
        )
        client, sink = _fake_client(
            outcomes=[_FakeApiError(500, _quota_body(_future_iso()))]
        )
        provider = _make_provider(tmp_path, client=client, retain_async=False)

        provider.sync_turn("hello", "world")
        provider._retain_queue.join()

        entries = _read_backlog(tmp_path)
        assert len(entries) == 1
        assert entries[0]["bank_id"] == "test-bank"
        assert "hello" in entries[0]["item"]["content"]
        # The entry reuses the document id of the ORIGINAL (failed) attempt —
        # chunk-hash recovery on replay depends on it.
        assert entries[0]["document_id"] == sink[0]["document_id"]


# ---------------------------------------------------------------------------
# Reconcile against server-side operation state
# ---------------------------------------------------------------------------


@requires_runtime
class TestReconcileBeforeSubmit:
    def _seeded(self, tmp_path, client, **seed):
        provider = _make_provider(tmp_path, client=client)
        op_id = _seed_entry(tmp_path, **seed)
        provider._retain_backlog.refresh_may_exist()
        return provider, op_id

    def test_pending_server_op_polls_without_resubmit(self, tmp_path):
        client, sink = _fake_client(op_status="pending")
        provider, op_id = self._seeded(tmp_path, client)

        provider._drain_retain_backlog()

        # pending: the server owns completion; the entry stays queued polling.
        assert client.aretain_batch.call_count == 0
        entries = _read_backlog(tmp_path)
        assert [e["id"] for e in entries] == [op_id]
        assert datetime.fromisoformat(entries[0]["retry_after"]) > datetime.now(UTC)

    @pytest.mark.parametrize("terminal", ["failed", "cancelled"])
    def test_terminal_op_retries_via_server_never_delete(self, tmp_path, terminal):
        client, sink = _fake_client(op_status=terminal)
        retry_sink = []
        provider, op_id = self._seeded(tmp_path, client)
        provider._client.operations.retry_operation.side_effect = (
            lambda **kw: retry_sink.append((kw.get("bank_id"), kw.get("operation_id")))
            or SimpleNamespace(success=True)
        )

        provider._drain_retain_backlog()

        # failed/cancelled: re-queue through the server's own retry endpoint;
        # the row is never deleted and the item never resubmitted raw.
        assert retry_sink == [("test-bank", op_id)]
        assert provider._client.operations.delete_operation.call_count == 0
        assert client.aretain_batch.call_count == 0
        entries = _read_backlog(tmp_path)
        assert [e["id"] for e in entries] == [op_id]  # kept, polling the retry

    def test_not_found_submits_verbatim_then_polls(self, tmp_path):
        client, sink = _fake_client(op_status="NotFound")
        provider, op_id = self._seeded(tmp_path, client, document_id="doc-keep")

        provider._drain_retain_backlog()

        submit = sink[-1]
        assert submit["operation_id"] == op_id
        assert submit["document_id"] == "doc-keep"
        assert submit["retain_async"] is True
        # Ack is not completion: the entry stays queued for the poll pass.
        entries = _read_backlog(tmp_path)
        assert [e["id"] for e in entries] == [op_id]
        assert entries[0].get("submitted")

    def test_status_probe_failure_keeps_entry(self, tmp_path):
        client, _ = _fake_client(op_status="raise")
        provider, op_id = self._seeded(tmp_path, client)

        provider._drain_retain_backlog()  # must not raise

        assert [e["id"] for e in _read_backlog(tmp_path)] == [op_id]

    def test_completed_server_op_removes_entry(self, tmp_path):
        client, _ = _fake_client(op_status="completed")
        provider, op_id = self._seeded(tmp_path, client)

        provider._drain_retain_backlog()

        assert _read_backlog(tmp_path) == []
        assert client.aretain_batch.call_count == 0


@requires_runtime
class TestCrashWindowNoDuplicate:
    def test_crash_after_ack_resolves_by_polling_not_resubmitting(self, tmp_path):
        """The durable record is the ENTRY, not the ack: a crash between the
        submit ack and any later state change leaves a submitted entry on
        disk; the next pass polls the live operation instead of resubmitting."""
        client, sink = _fake_client()  # submit succeeds
        provider = _make_provider(tmp_path, client=client)
        _seed_entry(tmp_path, submitted=False, content="crash window")
        provider._retain_backlog.refresh_may_exist()

        provider._drain_retain_backlog()  # submit acked
        assert client.aretain_batch.call_count == 1
        op_id = _read_backlog(tmp_path)[0]["id"]
        assert _read_backlog(tmp_path)[0].get("submitted")

        # "Crash": nothing else happened; a fresh provider on the same home
        # sees the acked-but-uncompleted entry. The server holds it (pending).
        client2, sink2 = _fake_client(op_status="pending")
        provider2 = _make_provider(tmp_path, client=client2)
        provider2._retain_backlog.refresh_may_exist()
        _flip_retry_after(tmp_path, _past_iso())

        provider2._drain_retain_backlog()

        assert client2.aretain_batch.call_count == 0  # poll, not resubmit
        assert [e["id"] for e in _read_backlog(tmp_path)] == [op_id]
        # Exactly one submission of this operation id ever reached the server.
        assert len([k for k in sink if k.get("operation_id") == op_id]) == 1


# ---------------------------------------------------------------------------
# Corrupt lines are quarantined, never silently dropped
# ---------------------------------------------------------------------------


@requires_runtime
class TestCorruptLines:
    def _corrupt_mixture(self, home: Path) -> str:
        good_id = str(uuid.uuid4())
        good = json.dumps({
            "id": good_id, "bank_id": "b", "item": {"content": "good"},
            "queued_at": _past_iso(), "retry_after": _past_iso(),
        })
        bad_lines = [
            '{"id": "%s", "bank_id"' % good_id,  # torn/truncated JSON
            '{"id": "not-a-uuid", "bank_id": "b", "item": {}}',  # unusable op key
            '{"bank_id": "b"}',  # no id at all
            'just garbage',
        ]
        path = _backlog_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(good + "\n" + "\n".join(bad_lines) + "\n")
        return good_id

    def test_corrupt_lines_quarantined_not_silently_dropped(self, tmp_path):
        good_id = self._corrupt_mixture(tmp_path)
        backlog = RetainBacklog(lambda: tmp_path)

        # Enqueue forces a save; the good entry survives, the bad lines move.
        backlog.enqueue(
            bank_id="b", item={"content": "new"},
            retry_after=datetime.now(UTC) + timedelta(hours=1),
        )

        ids = [e["id"] for e in _read_backlog(tmp_path)]
        assert ids[0] == good_id and len(ids) == 2  # good + the new entry
        corrupt = Path(str(_backlog_path(tmp_path)) + ".corrupt").read_text()
        assert "not-a-uuid" in corrupt
        assert "just garbage" in corrupt
        # (The torn first bad line embeds good_id, so assert on the payload:
        # the intact good line itself must never be quarantined.)
        assert '"content": "good"' not in corrupt

    def test_quarantine_survives_rewrite_without_duplicates(self, tmp_path):
        self._corrupt_mixture(tmp_path)
        backlog = RetainBacklog(lambda: tmp_path)
        backlog.enqueue(
            bank_id="b", item={"content": "new"},
            retry_after=datetime.now(UTC) + timedelta(hours=1),
        )
        # A second pass must not re-append the already-quarantined lines.
        backlog.enqueue(
            bank_id="b", item={"content": "again"},
            retry_after=datetime.now(UTC) + timedelta(hours=1),
        )

        corrupt = Path(str(_backlog_path(tmp_path)) + ".corrupt").read_text()
        assert corrupt.count("not-a-uuid") == 1
        assert corrupt.count("just garbage") == 1
        assert len(_read_backlog(tmp_path)) == 3  # good + the two new entries


# ---------------------------------------------------------------------------
# Wakeup timer: future-due entries drain without new chat
# ---------------------------------------------------------------------------


@requires_runtime
class TestWakeupTimer:
    def test_future_entry_arms_wakeup_and_submits_when_due(self, tmp_path):
        client, sink = _fake_client()
        provider = _make_provider(tmp_path, client=client)
        _seed_entry(tmp_path, submitted=False, content="wake me",
                    retry=datetime.now(UTC).isoformat())
        provider._retain_backlog.refresh_may_exist()

        provider._arm_backlog_wakeup(datetime.now(UTC))
        assert _wait_until(lambda: client.aretain_batch.call_count == 1), (
            "wakeup drain did not submit the due entry without incoming chat"
        )
        assert sink[-1].get("operation_id")

    def test_wakeup_rearms_for_earlier_deadline(self, tmp_path):
        client, _ = _fake_client()
        provider = _make_provider(tmp_path, client=client)

        provider._arm_backlog_wakeup(datetime.now(UTC) + timedelta(hours=1))
        late = provider._backlog_wakeup_deadline
        provider._arm_backlog_wakeup(datetime.now(UTC) + timedelta(minutes=1))
        early = provider._backlog_wakeup_deadline

        assert early < late
        assert early - time.monotonic() <= 70.0

    def test_initialize_with_future_entry_arms_wakeup(self, tmp_path):
        client, _ = _fake_client()
        _write_backlog(tmp_path, [{
            "id": str(uuid.uuid4()), "bank_id": "test-bank",
            "item": {"content": "future"}, "queued_at": _past_iso(),
            "retry_after": _future_iso(hours=2),
        }])

        provider = _make_provider(tmp_path, client=client)

        assert _wait_until(
            lambda: provider._backlog_wakeup_deadline is not None
        ), "initialize did not arm a wakeup for a future-dated backlog entry"


# ---------------------------------------------------------------------------
# Per-profile isolation: the backlog follows the live home
# ---------------------------------------------------------------------------


@requires_runtime
class TestProfileIsolation:
    def test_backlog_follows_live_home_across_profiles(self, tmp_path, monkeypatch):
        home_a, home_b = tmp_path / "a", tmp_path / "b"
        home_a.mkdir(); home_b.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home_a))
        backlog = RetainBacklog(get_hermes_home)
        backlog.enqueue(
            bank_id="b", item={"content": "profile a memory"},
            retry_after=datetime.now(UTC) + timedelta(hours=1),
        )
        assert _backlog_path(home_a).exists()

        monkeypatch.setenv("HERMES_HOME", str(home_b))
        assert not _backlog_path(home_b).exists()
        assert backlog.refresh_may_exist() is False
        assert backlog.peek() == (None, 0)

        monkeypatch.setenv("HERMES_HOME", str(home_a))
        assert backlog.refresh_may_exist() is True
        assert [e["item"]["content"] for e in _read_backlog(home_a)] == ["profile a memory"]


# ---------------------------------------------------------------------------
# Concurrency: cross-process lock serializes drains; each entry settles once
# ---------------------------------------------------------------------------


@requires_runtime
class TestConcurrentDrain:
    def test_concurrent_drains_submit_each_entry_once(self, tmp_path):
        backlog = RetainBacklog(lambda: tmp_path)
        ids = [
            backlog.enqueue(
                bank_id="b", item={"content": f"m{i}"},
                retry_after=datetime.now(UTC) - timedelta(minutes=1),
            )
            for i in range(3)
        ]
        remotes = [_ScriptedRemote() for _ in range(3)]
        submits: list[str] = []
        submits_lock = threading.Lock()

        def _drain(remote):
            backlog.drain(remote)
            with submits_lock:
                submits.extend(c[1] for c in remote.calls if c[0] == "submit")

        threads = [
            threading.Thread(target=_drain, args=(r,)) for r in remotes
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads)

        # The flock serializes the passes; every entry is submitted exactly
        # once and stays queued pending observed completion.
        assert sorted(submits) == sorted(ids)
        assert {e["id"] for e in _read_backlog(tmp_path)} == set(ids)


# ---------------------------------------------------------------------------
# Real HTTP contract: the actual SDK client against a server-shaped stub
# ---------------------------------------------------------------------------


class _HindsightStubHandler(BaseHTTPRequestHandler):
    """Implements the observed live-server contract for the calls the backlog
    makes: idempotent async retain (client operation_id), operation status,
    retry_operation. Records creations in ``server.created`` and every delete
    attempt in ``server.deletes`` — duplicates or deletes would show there."""

    def log_message(self, *args):  # silence
        pass

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        m = re.match(r"^/v1/default/banks/([^/]+)/operations/([^/]+)$", self.path)
        if not m:
            return self._json(404, {"detail": "not found"})
        _, op_id = m.groups()
        server = self.server
        with server.lock:
            status = server.ops.get(op_id)
        if status is None:
            return self._json(404, {"detail": f"Operation {op_id} not found"})
        return self._json(200, {"operation_id": op_id, "status": status})

    def do_DELETE(self):
        server = self.server
        with server.lock:
            server.deletes.append(self.path)
        return self._json(405, {"detail": "deleting is not part of the retry contract"})

    def do_POST(self):
        server = self.server
        length = int(self.headers.get("Content-Length") or 0)
        m = re.match(r"^/v1/default/banks/([^/]+)/operations/([^/]+)/retry$", self.path)
        if m:
            _, op_id = m.groups()
            with server.lock:
                if op_id not in server.ops:
                    return self._json(404, {"detail": f"Operation {op_id} not found"})
                server.retries.append(op_id)
                server.ops[op_id] = "pending"
            return self._json(200, {"success": True, "operation_id": op_id})

        m = re.match(r"^/v1/default/banks/([^/]+)/memories$", self.path)
        if not m:
            return self._json(404, {"detail": "not found"})
        bank_id = m.group(1)
        request = json.loads(self.rfile.read(length) or b"{}")
        if not request.get("async"):
            if server.quota_mode:
                return self._json(500, {"detail": (
                    "Fact extraction deferred by provider quota: 1/1 chunks failed. "
                    "First failures: chunk 0: ProviderRateLimitResetError: quota; "
                    f"retry at {_future_iso()}: HTTP 429: quota exceeded"
                )})
            return self._json(200, {
                "success": True, "bank_id": bank_id,
                "items_count": len(request.get("items") or []), "async": False,
            })
        op_id = request.get("operation_id")
        with server.lock:
            if op_id in server.ops:
                # Replay: return the original operation, create no new work.
                return self._json(200, {
                    "success": True, "bank_id": bank_id, "async": True,
                    "items_count": 1, "operation_id": op_id,
                })
            server.ops[op_id] = "pending"
            server.created.append(op_id)
        return self._json(200, {
            "success": True, "bank_id": bank_id, "async": True,
            "items_count": 1, "operation_id": op_id,
        })


class _HindsightStubServer(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _HindsightStubHandler)
        self.ops: dict[str, str] = {}
        self.created: list[str] = []
        self.retries: list[str] = []
        self.deletes: list[str] = []
        self.lock = threading.Lock()
        self.quota_mode = False


@requires_runtime
class TestRealHttpContract:
    def test_replay_retry_and_removal_over_real_http(self, tmp_path):
        from hindsight_client import Hindsight

        stub = _HindsightStubServer()
        thread = threading.Thread(target=stub.serve_forever, daemon=True)
        thread.start()
        try:
            client = Hindsight(base_url=f"http://127.0.0.1:{stub.server_port}", api_key="k")
            stub.quota_mode = True
            provider = _make_provider(tmp_path, client=client)

            # Sync tool retain hits the quota 500 and queues durably.
            payload = _retain(provider, "real http contract memory")
            assert payload["result"].startswith("Memory queued")
            provider._retain_queue.join()
            entry = _read_backlog(tmp_path)[0]
            op_id = entry["id"]

            # Limit clears; drain: op unknown server-side -> one async
            # submission carrying the persisted operation_id and document_id.
            stub.quota_mode = False
            _flip_retry_after(tmp_path, _past_iso())
            provider._retain_backlog.refresh_may_exist()
            provider._drain_retain_backlog()
            assert stub.created == [op_id]
            assert [e["id"] for e in _read_backlog(tmp_path)] == [op_id]

            # Poll pass: the live op is pending — no new work, entry stays.
            _flip_retry_after(tmp_path, _past_iso())
            provider._drain_retain_backlog()
            assert stub.created == [op_id]
            assert [e["id"] for e in _read_backlog(tmp_path)] == [op_id]

            # Server-side failure: retry goes through the server's own
            # retry_operation endpoint — never a delete, never a resubmit.
            with stub.lock:
                stub.ops[op_id] = "failed"
            _flip_retry_after(tmp_path, _past_iso())
            provider._drain_retain_backlog()
            assert stub.retries == [op_id]
            assert stub.created == [op_id]
            assert stub.deletes == []
            assert [e["id"] for e in _read_backlog(tmp_path)] == [op_id]

            # Completion observed: the entry is removed.
            with stub.lock:
                stub.ops[op_id] = "completed"
            _flip_retry_after(tmp_path, _past_iso())
            provider._drain_retain_backlog()
            assert _read_backlog(tmp_path) == []
            assert stub.created == [op_id]
            assert stub.deletes == []

            # Replay-by-id (same payload, same id) creates no new work even
            # on a raw resubmission — the server contract's own guarantee.
            # Routed through the provider's shared loop: the client's aiohttp
            # session is bound to it.
            provider._run_hindsight_operation(lambda c: c.aretain_batch(
                bank_id="test-bank", items=[entry["item"]],
                retain_async=True, operation_id=op_id,
                document_id=entry["document_id"]))
            assert stub.created == [op_id]
        finally:
            stub.shutdown()
            stub.server_close()
