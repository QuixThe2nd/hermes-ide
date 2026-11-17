"""Hindsight retain backlog: provider-rate-limited retains persist to a durable
per-profile JSONL backlog and drain automatically once the limit resets.

Covers the two retain paths (the ``hindsight_retain`` tool and the writer-thread
turn job), classification of the real failure signature (HTTP 500 wrapping
``ProviderRateLimitResetError`` / ``deferred by provider quota``), restart
durability, FIFO drain, exactly-once removal, and the untouched behaviour for
non-rate-limited failures and successes.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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


def _fake_client(outcomes: list | None = None, sink: list | None = None):
    """MagicMock Hindsight client; ``aretain_batch`` records kwargs into *sink*.

    ``outcomes`` pop per call: an Exception is raised, anything else returns ok.
    Exhausted outcomes (or None) mean success.
    """
    sink = sink if sink is not None else []
    outcomes = list(outcomes or [])

    async def _aretain_batch(**kwargs):
        sink.append(kwargs)
        if outcomes:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        return SimpleNamespace(ok=True, operation_id=None)

    client = MagicMock()
    client.aretain_batch = AsyncMock(side_effect=_aretain_batch)
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
