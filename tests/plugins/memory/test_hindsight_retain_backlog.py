"""Hindsight retain backlog: provider-rate-limited retains persist to a durable
per-profile JSONL backlog and drain automatically once the limit resets.

Covers the two retain paths (the ``hindsight_retain`` tool and the writer-thread
turn job), classification of the real failure signature (HTTP 500 wrapping
``ProviderRateLimitResetError`` / ``deferred by provider quota``), restart
durability, FIFO drain, exactly-once removal, and the untouched behaviour for
non-rate-limited failures and successes.
"""

import json
import re
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
from plugins.memory.hindsight.retain_backlog import (
    BACKLOG_FILENAME,
    RetainBacklog,
    classify_exception,
    classify_rate_limit,
)

UTC = timezone.utc


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


def _fake_client(
    outcomes: list | None = None,
    sink: list | None = None,
    *,
    op_status="not_found",
    op_sink: list | None = None,
    delete_sink: list | None = None,
    delete_error: Exception | None = None,
):
    """MagicMock Hindsight client; ``aretain_batch`` records kwargs into *sink*.

    ``outcomes`` pop per call: an Exception is raised, anything else returns ok.
    Exhausted outcomes (or None) mean success. ``op_status`` drives the
    operations API double: a constant status string, a callable op_id ->
    status, ``"raise"`` (probe transport failure), or ``"NotFound"`` (SDK
    NotFoundException). Unknown statuses exercise the submit fallback.
    """
    from hindsight_client_api.exceptions import NotFoundException

    sink = sink if sink is not None else []
    outcomes = list(outcomes or [])

    async def _aretain_batch(**kwargs):
        sink.append(kwargs)
        if outcomes:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        return SimpleNamespace(ok=True, operation_id=kwargs.get("operation_id"))

    async def _get_status(**kwargs):
        if op_sink is not None:
            op_sink.append((kwargs.get("bank_id"), kwargs.get("operation_id")))
        status = op_status(kwargs.get("operation_id")) if callable(op_status) else op_status
        if status == "raise":
            raise RuntimeError("status endpoint down")
        if status == "NotFound":
            raise NotFoundException(status=404, body='{"detail": "not found"}')
        return SimpleNamespace(status=status)

    async def _delete_op(**kwargs):
        if delete_sink is not None:
            delete_sink.append((kwargs.get("bank_id"), kwargs.get("operation_id")))
        if delete_error is not None:
            raise delete_error
        return SimpleNamespace(success=True)

    client = MagicMock()
    client.aretain_batch = AsyncMock(side_effect=_aretain_batch)
    client.operations = MagicMock()
    client.operations.get_operation_status = AsyncMock(side_effect=_get_status)
    client.operations.delete_operation = AsyncMock(side_effect=_delete_op)
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


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ---------------------------------------------------------------------------
# Classification
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
        entries = _read_backlog(tmp_path)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["bank_id"] == "test-bank"
        assert entry["item"]["content"] == "user likes dark mode"
        assert entry["retry_after"]
        assert entry["queued_at"]
        assert entry["id"]
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
        assert not Path(str(_backlog_path(tmp_path)) + ".lock").exists()

    def test_successful_retain_writes_no_backlog(self, tmp_path):
        client, _ = _fake_client()
        provider = _make_provider(tmp_path, client=client)

        payload = _retain(provider, "user likes dark mode")

        assert payload == {"result": "Memory stored successfully."}
        assert not _backlog_path(tmp_path).exists()
        assert not Path(str(_backlog_path(tmp_path)) + ".lock").exists()
        assert provider._retain_backlog.may_exist is False


# ---------------------------------------------------------------------------
# Restart durability + init drain
# ---------------------------------------------------------------------------


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
        assert _wait_until(lambda: restarted._retain_backlog.entries())
        entries = restarted._retain_backlog.entries()
        assert len(entries) == 1
        assert entries[0]["item"]["content"] == "durable memory"

    def test_due_backlog_drains_once_on_initialize(self, tmp_path, monkeypatch):
        client, _ = _fake_client(
            outcomes=[_FakeApiError(500, _quota_body(_future_iso()))]
        )
        p1 = _make_provider(tmp_path, client=client)
        p1.handle_tool_call("hindsight_retain", {"content": "drain me"})
        p1._retain_queue.join()
        assert len(_read_backlog(tmp_path)) == 1
        _flip_retry_after(tmp_path, _past_iso())

        drained_client, sink = _fake_client()  # limit cleared
        p2 = _make_provider(tmp_path, client=drained_client)

        assert _wait_until(lambda: not _backlog_path(tmp_path).exists()), (
            "init drain did not empty the backlog"
        )
        assert drained_client.aretain_batch.call_count == 1
        kwargs = sink[0]
        assert kwargs["bank_id"] == "test-bank"
        assert kwargs["items"][0]["content"] == "drain me"
        # Exactly-once: a second pass sends nothing.
        p2._drain_retain_backlog()
        assert drained_client.aretain_batch.call_count == 1
        assert not _backlog_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# Drain semantics
# ---------------------------------------------------------------------------


class TestBacklogDrain:
    def test_fifo_order_and_cleanup(self, tmp_path):
        client, sink = _fake_client(
            outcomes=[
                _FakeApiError(500, _quota_body(_future_iso())),
                _FakeApiError(500, _quota_body(_future_iso())),
            ]
        )
        provider = _make_provider(tmp_path, client=client)
        _retain(provider, "first memory")
        _retain(provider, "second memory")
        provider._retain_queue.join()
        assert len(_read_backlog(tmp_path)) == 2

        _flip_retry_after(tmp_path, _past_iso())
        provider._drain_retain_backlog()

        # sink also holds the two failed tool attempts; the drain comes after them.
        drained = [kwargs["items"][0]["content"] for kwargs in sink[2:]]
        assert drained == ["first memory", "second memory"]
        assert _read_backlog(tmp_path) == []

    def test_not_due_drain_sends_nothing(self, tmp_path):
        backlog = RetainBacklog(lambda: tmp_path)
        backlog.enqueue(
            bank_id="b",
            item={"content": "x"},
            retry_after=datetime.now(UTC) + timedelta(hours=2),
        )
        calls = []
        backlog.drain(lambda entry: calls.append(entry))
        assert calls == []
        assert len(backlog.entries()) == 1

    def test_drain_stops_on_rate_limit_and_pushes_retry_forward(self, tmp_path):
        backlog = RetainBacklog(lambda: tmp_path)
        past = datetime.now(UTC) - timedelta(minutes=1)
        backlog.enqueue(bank_id="b", item={"content": "one"}, retry_after=past)
        backlog.enqueue(bank_id="b", item={"content": "two"}, retry_after=past)
        new_retry = datetime.now(UTC) + timedelta(hours=3)
        calls = []

        def _retain(entry):
            calls.append(entry)
            raise _FakeApiError(500, _quota_body(new_retry.isoformat()))

        backlog.drain(_retain)

        assert len(calls) == 1  # FIFO head failed; the pass stopped
        entries = backlog.entries()
        assert len(entries) == 2
        assert all(e["retry_after"] == new_retry.isoformat() for e in entries)
        backlog.drain(lambda entry: calls.append(entry))  # still limited: not due
        assert len(calls) == 1

    def test_drain_keeps_entry_on_other_failure(self, tmp_path):
        backlog = RetainBacklog(lambda: tmp_path)
        backlog.enqueue(
            bank_id="b",
            item={"content": "one"},
            retry_after=datetime.now(UTC) - timedelta(minutes=1),
        )

        def _boom(entry):
            raise RuntimeError("transient network blip")

        backlog.drain(_boom)
        assert len(backlog.entries()) == 1

        sent = []
        backlog.drain(lambda entry: sent.append(entry))
        assert len(sent) == 1
        assert backlog.entries() == []


# ---------------------------------------------------------------------------
# Writer path: sync_turn job
# ---------------------------------------------------------------------------


class TestWriterPath:
    def test_rate_limited_turn_job_queues_backlog(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "plugins.memory.hindsight._check_api_supports_update_mode_append",
            lambda *a, **k: False,
        )
        client, _ = _fake_client(
            outcomes=[_FakeApiError(500, _quota_body(_future_iso()))]
        )
        provider = _make_provider(tmp_path, client=client, retain_async=False)

        provider.sync_turn("hello", "world")
        provider._retain_queue.join()

        entries = _read_backlog(tmp_path)
        assert len(entries) == 1
        assert entries[0]["bank_id"] == "test-bank"
        assert "hello" in entries[0]["item"]["content"]
        assert entries[0]["retain_async"] is False


# ---------------------------------------------------------------------------
# Operation identity: entry id is the server-side idempotency key
# ---------------------------------------------------------------------------


class TestOperationIdentity:
    def test_entry_id_is_uuid_operation_identity(self, tmp_path):
        backlog = RetainBacklog(lambda: tmp_path)
        entry_id = backlog.enqueue(
            bank_id="b", item={"content": "x"},
            retry_after=datetime.now(UTC) - timedelta(minutes=1),
        )
        # The persisted id must parse as the UUID the server validates
        # RetainRequest.operation_id against — it is generated at enqueue,
        # before any remote submission, and never regenerated later.
        assert uuid.UUID(entry_id)
        assert backlog.entries()[0]["id"] == entry_id

    def test_drain_replays_async_with_persisted_operation_id(self, tmp_path):
        client, sink = _fake_client(
            outcomes=[_FakeApiError(500, _quota_body(_future_iso()))]
        )
        provider = _make_provider(tmp_path, client=client)
        _retain(provider, "idempotent replay")
        provider._retain_queue.join()
        entry = _read_backlog(tmp_path)[0]

        _flip_retry_after(tmp_path, _past_iso())
        provider._drain_retain_backlog()

        submit = sink[-1]
        assert submit["retain_async"] is True
        assert submit["operation_id"] == entry["id"]
        assert uuid.UUID(submit["operation_id"])


# ---------------------------------------------------------------------------
# Reconcile against server-side operation state before submitting
# ---------------------------------------------------------------------------


class TestReconcileBeforeSubmit:
    def test_pending_server_op_settles_without_resubmit(self, tmp_path):
        client, sink = _fake_client(op_status="pending")
        provider = _make_provider(tmp_path, client=client)
        _write_backlog(tmp_path, [{
            "id": str(uuid.uuid4()), "bank_id": "test-bank",
            "item": {"content": "server owns it"}, "queued_at": _past_iso(),
            "retry_after": _past_iso(),
        }])
        provider._retain_backlog.refresh_may_exist()

        provider._drain_retain_backlog()

        assert _read_backlog(tmp_path) == []
        assert client.aretain_batch.call_count == 0

    def test_completed_server_op_settles_without_resubmit(self, tmp_path):
        client, sink = _fake_client(op_status="completed")
        provider = _make_provider(tmp_path, client=client)
        _write_backlog(tmp_path, [{
            "id": str(uuid.uuid4()), "bank_id": "test-bank",
            "item": {"content": "already stored"}, "queued_at": _past_iso(),
            "retry_after": _past_iso(),
        }])
        provider._retain_backlog.refresh_may_exist()

        provider._drain_retain_backlog()

        assert _read_backlog(tmp_path) == []
        assert client.aretain_batch.call_count == 0

    @pytest.mark.parametrize("terminal", ["failed", "cancelled"])
    def test_terminal_op_deletes_row_then_resubmits_same_id(self, tmp_path, terminal):
        client, sink = _fake_client(op_status=terminal)
        provider = _make_provider(tmp_path, client=client)
        op_id = str(uuid.uuid4())
        _write_backlog(tmp_path, [{
            "id": op_id, "bank_id": "test-bank",
            "item": {"content": "fresh chance"}, "queued_at": _past_iso(),
            "retry_after": _past_iso(),
        }])
        provider._retain_backlog.refresh_may_exist()
        delete_sink = []
        # Re-bind the delete double so this test can observe the call.
        provider._client.operations.delete_operation.side_effect = (
            lambda **kw: delete_sink.append((kw.get("bank_id"), kw.get("operation_id")))
            or SimpleNamespace(success=True)
        )

        provider._drain_retain_backlog()

        assert delete_sink == [("test-bank", op_id)]
        assert sink[-1]["operation_id"] == op_id
        assert sink[-1]["retain_async"] is True
        assert _read_backlog(tmp_path) == []

    def test_not_found_exception_maps_to_submit(self, tmp_path):
        client, sink = _fake_client(op_status="NotFound")
        provider = _make_provider(tmp_path, client=client)
        op_id = str(uuid.uuid4())
        _write_backlog(tmp_path, [{
            "id": op_id, "bank_id": "test-bank",
            "item": {"content": "never submitted"}, "queued_at": _past_iso(),
            "retry_after": _past_iso(),
        }])
        provider._retain_backlog.refresh_may_exist()

        provider._drain_retain_backlog()

        assert sink[-1]["operation_id"] == op_id
        assert _read_backlog(tmp_path) == []

    def test_status_probe_failure_keeps_entry(self, tmp_path):
        client, _ = _fake_client(op_status="raise")
        provider = _make_provider(tmp_path, client=client)
        _write_backlog(tmp_path, [{
            "id": str(uuid.uuid4()), "bank_id": "test-bank",
            "item": {"content": "keep me"}, "queued_at": _past_iso(),
            "retry_after": _past_iso(),
        }])
        provider._retain_backlog.refresh_may_exist()

        provider._drain_retain_backlog()  # must not raise

        assert len(_read_backlog(tmp_path)) == 1


# ---------------------------------------------------------------------------
# Crash window: submit acked, local removal lost
# ---------------------------------------------------------------------------


class TestCrashWindowNoDuplicate:
    def test_crash_after_submit_before_remove_resolves_without_duplicate(self, tmp_path):
        client, sink = _fake_client()
        provider = _make_provider(tmp_path, client=client)
        op_id = str(uuid.uuid4())
        _write_backlog(tmp_path, [{
            "id": op_id, "bank_id": "test-bank",
            "item": {"content": "crash window"}, "queued_at": _past_iso(),
            "retry_after": _past_iso(),
        }])
        provider._retain_backlog.refresh_may_exist()

        # Phase 1: settle reaches the server (submit acked) then the process
        # "crashes" before the local removal can run.
        real_settle = provider._settle_backlog_entry

        def _crash_after_ack(entry):
            real_settle(entry)
            raise SystemExit("crash between ack and removal")

        with pytest.raises(SystemExit):
            provider._retain_backlog.drain(_crash_after_ack)
        assert len(_read_backlog(tmp_path)) == 1
        submits_after_crash = [k for k in sink if k.get("operation_id") == op_id]
        assert len(submits_after_crash) == 1

        # Phase 2: restart. The server holds the operation (pending: quota
        # deferrals park server-side), so reconcile settles without a second
        # submission — no duplicate.
        client2, sink2 = _fake_client(op_status="pending")
        provider2 = _make_provider(tmp_path, client=client2)
        provider2._drain_retain_backlog()

        assert _read_backlog(tmp_path) == []
        assert client2.aretain_batch.call_count == 0
        # Exactly one submission of this operation id ever reached the
        # server across the crash — reconcile, not duplicate.
        assert len([k for k in sink2 if k.get("operation_id") == op_id]) == 0


# ---------------------------------------------------------------------------
# Corrupt lines are quarantined, never silently dropped
# ---------------------------------------------------------------------------


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

        entries = backlog.entries()

        assert [e["id"] for e in entries] == [good_id]
        corrupt = Path(str(_backlog_path(tmp_path)) + ".corrupt").read_text()
        assert "not-a-uuid" in corrupt
        assert "just garbage" in corrupt
        assert '"id": "b"' not in corrupt  # the good line is never quarantined

    def test_quarantine_survives_rewrite_without_duplicates(self, tmp_path):
        self._corrupt_mixture(tmp_path)
        backlog = RetainBacklog(lambda: tmp_path)
        backlog.entries()
        backlog.enqueue(
            bank_id="b", item={"content": "new"},
            retry_after=datetime.now(UTC) + timedelta(hours=1),
        )
        # A second load must not re-append the already-quarantined lines.
        backlog.entries()

        corrupt = Path(str(_backlog_path(tmp_path)) + ".corrupt").read_text()
        assert corrupt.count("not-a-uuid") == 1
        assert corrupt.count("just garbage") == 1
        assert len(_read_backlog(tmp_path)) == 2  # good + new entry only


# ---------------------------------------------------------------------------
# Wakeup timer: future-due entries drain without new chat
# ---------------------------------------------------------------------------


class TestWakeupTimer:
    def test_future_entry_arms_wakeup_and_drains_when_due(self, tmp_path):
        client, sink = _fake_client()
        provider = _make_provider(tmp_path, client=client)
        _write_backlog(tmp_path, [{
            "id": str(uuid.uuid4()), "bank_id": "test-bank",
            "item": {"content": "wake me"}, "queued_at": _past_iso(),
            "retry_after": datetime.now(UTC).isoformat(),  # due almost now
        }])
        provider._retain_backlog.refresh_may_exist()

        provider._arm_backlog_wakeup(datetime.now(UTC))
        assert _wait_until(lambda: not _backlog_path(tmp_path).exists()), (
            "wakeup drain did not empty the backlog without incoming chat"
        )
        assert len([k for k in sink if k.get("operation_id")]) == 1

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
        assert backlog.entries() == []

        monkeypatch.setenv("HERMES_HOME", str(home_a))
        assert backlog.refresh_may_exist() is True
        assert [e["item"]["content"] for e in backlog.entries()] == ["profile a memory"]


# ---------------------------------------------------------------------------
# Concurrency: one settle per entry, always
# ---------------------------------------------------------------------------


class TestConcurrentDrain:
    def test_concurrent_drains_settle_each_entry_once(self, tmp_path):
        backlog = RetainBacklog(lambda: tmp_path)
        ids = [
            backlog.enqueue(
                bank_id="b", item={"content": f"m{i}"},
                retry_after=datetime.now(UTC) - timedelta(minutes=1),
            )
            for i in range(3)
        ]
        settled: list[str] = []
        settled_lock = threading.Lock()

        def _slow_settle(entry):
            time.sleep(0.05)
            with settled_lock:
                settled.append(entry["id"])

        threads = [
            threading.Thread(target=backlog.drain, args=(_slow_settle,))
            for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert sorted(settled) == sorted(ids)
        assert backlog.entries() == []


# ---------------------------------------------------------------------------
# Real HTTP contract: the actual SDK client against a server-shaped stub
# ---------------------------------------------------------------------------


class _HindsightStubHandler(BaseHTTPRequestHandler):
    """Implements the observed live-server contract for the three calls the
    backlog makes: idempotent async retain (client operation_id), operation
    status, terminal-operation delete. Records every creation in
    ``server.created`` (op_id list) — duplicates would show up here."""

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
        m = re.match(r"^/v1/default/banks/([^/]+)/operations/([^/]+)/delete$", self.path)
        if not m:
            return self._json(404, {"detail": "not found"})
        _, op_id = m.groups()
        server = self.server
        with server.lock:
            if server.ops.pop(op_id, None) is None:
                return self._json(404, {"detail": f"Operation {op_id} not found"})
        return self._json(200, {"success": True, "message": "deleted", "operation_id": op_id})

    def do_POST(self):
        m = re.match(r"^/v1/default/banks/([^/]+)/memories$", self.path)
        if not m:
            return self._json(404, {"detail": "not found"})
        bank_id = m.group(1)
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        server = self.server
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
        self.lock = threading.Lock()
        self.quota_mode = False


class TestRealHttpContract:
    def test_replay_and_reconcile_over_real_http(self, tmp_path):
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

            # Limit clears; drain reconciles: op unknown server-side -> one
            # async submission carrying the persisted operation_id.
            stub.quota_mode = False
            _flip_retry_after(tmp_path, _past_iso())
            provider._drain_retain_backlog()
            assert _read_backlog(tmp_path) == []
            assert stub.created == [op_id]
            assert stub.ops[op_id] == "pending"

            # Crash window: the entry file is restored to its pre-removal
            # state (submit acked, local removal lost; retry was due). Restart
            # + drain must reconcile against the pending op — no second
            # submission.
            crashed_entry = dict(entry, retry_after=_past_iso())
            _write_backlog(tmp_path, [crashed_entry])
            provider2 = _make_provider(tmp_path, client=client)
            provider2._retain_backlog.refresh_may_exist()
            provider2._drain_retain_backlog()
            assert _read_backlog(tmp_path) == []
            assert stub.created == [op_id], "reconcile re-submitted a live op"

            # Replay-by-id (same payload, same id) creates no new work even
            # on a raw resubmission — the server contract's own guarantee.
            # Routed through the provider's shared loop: the client's aiohttp
            # session is bound to it.
            provider2._run_hindsight_operation(lambda c: c.aretain_batch(
                bank_id="test-bank", items=[entry["item"]],
                retain_async=True, operation_id=op_id))
            assert stub.created == [op_id]
        finally:
            stub.shutdown()
            stub.server_close()
