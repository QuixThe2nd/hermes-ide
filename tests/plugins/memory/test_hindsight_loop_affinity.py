"""Regression: the Hindsight SDK client must live on one owning event loop.

Production symptom: the provider constructed its SDK client on whichever
thread first needed it and evaluated operation lambdas on the calling thread.
hindsight-client 0.6.1 ships several methods (``update_bank_config``,
``list_directives``, ``create_directive``) as sync ``_run_async`` wrappers
that run their HTTP on the CALLING thread's event loop, so a bank-defaults
call from a foreground thread bound the SDK's lazily-created aiohttp
ClientSession to that thread's loop. Every later call the provider scheduled
onto its shared background loop then failed with

    Timeout context manager should be used inside a task

(aiohttp.helpers.TimerContext finds no current task on the session's loop),
and the wrappers' non-coroutine returns made run_coroutine_threadsafe raise
TypeError, surfaced as "Hindsight loop unavailable".

The fakes below model the SDK's loop behavior: a session that binds to
``asyncio.get_running_loop()`` at creation (what aiohttp's lazy
``_ensure_session`` does) and refuses requests from any other loop with the
production error message, plus the sync wrappers that run on the caller's
loop. The provider must construct, use, and close the client on the shared
Hindsight loop no matter which thread triggers the call.
"""

import asyncio
import builtins
import gc
import json
import logging
import threading
import warnings
from types import SimpleNamespace

import pytest

import plugins.memory.hindsight as hindsight_mod
from plugins.memory.hindsight import HindsightMemoryProvider


# ---------------------------------------------------------------------------
# Fakes modeling hindsight-client 0.6.1's loop behavior
# ---------------------------------------------------------------------------


class _AiohttpStyleSession:
    """Session with aiohttp.ClientSession's loop affinity.

    Created lazily inside a request, binding to whichever loop runs that
    request. A request whose running loop is not the session's loop mirrors
    aiohttp.helpers.TimerContext.__enter__: no current task on the session's
    loop, so the timeout context manager refuses to run.
    """

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.requests = []
        self.closed = False
        self.closed_thread = None

    def request(self, method, path):
        if self.loop is not asyncio.get_running_loop():
            raise RuntimeError("Timeout context manager should be used inside a task")
        self.requests.append((threading.current_thread().name, method, path))
        return {"status": 200}

    async def close(self):
        if self.closed:
            return
        if self.loop is not asyncio.get_running_loop():
            raise RuntimeError("Session is bound to another event loop")
        self.closed = True
        self.closed_thread = threading.current_thread().name


class _DirectivesNamespace:
    """The SDK's generated async ``client.directives`` namespace."""

    def __init__(self, client):
        self._client = client

    async def list_directives(self, bank_id, _request_timeout=None):
        self._client._request("GET", f"/banks/{bank_id}/directives")
        return SimpleNamespace(items=[])

    async def create_directive(self, bank_id, body, _request_timeout=None):
        self._client._request("POST", f"/banks/{bank_id}/directives")
        return SimpleNamespace(ok=True)


class _LoopBoundHindsightClient:
    """hindsight-client stand-in with the surfaces the provider touches.

    Async data plane (arecall / areflect / aretain_batch / aclose,
    _aupdate_bank_config, directives.*) all funnel through one lazily created
    loop-bound session. The sync ``_run_async`` wrappers run their coroutine
    on the calling thread's event loop — the exact poison the fix removes, so
    the tests assert the provider never calls them.
    """

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.construction_thread = threading.current_thread().name
        self._session = None
        self.session_creations = 0
        self.retain_calls = 0
        self.sync_wrapper_calls = []
        self.directives = _DirectivesNamespace(self)

        async def _get_operation_status(**kwargs):
            return SimpleNamespace(status="completed")

        self.operations = SimpleNamespace(get_operation_status=_get_operation_status)
        _LoopBoundHindsightClient.instances.append(self)

    def _ensure_session(self):
        if self._session is None:
            self._session = _AiohttpStyleSession()
            self.session_creations += 1
        return self._session

    def _request(self, method, path):
        return self._ensure_session().request(method, path)

    @staticmethod
    def _run_async(coro):
        """The SDK's sync-wrapper runner: HTTP on the calling thread's loop."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

    # --- sync _run_async wrappers (shipped SDK surface; must stay unused) ---
    def update_bank_config(self, bank_id, retain_mission=None, reflect_mission=None):
        self.sync_wrapper_calls.append("update_bank_config")
        updates = {"retain_mission": retain_mission}
        if reflect_mission is not None:
            updates["reflect_mission"] = reflect_mission
        return self._run_async(self._aupdate_bank_config(bank_id, updates))

    def list_directives(self, bank_id):
        self.sync_wrapper_calls.append("list_directives")
        return self._run_async(self.directives.list_directives(bank_id))

    def create_directive(self, bank_id, name, content, priority, is_active=True):
        self.sync_wrapper_calls.append("create_directive")
        return self._run_async(
            self.directives.create_directive(
                bank_id,
                {"name": name, "content": content, "priority": priority,
                 "is_active": is_active},
            )
        )

    # --- async data plane ---
    async def _aupdate_bank_config(self, bank_id, updates):
        self._request("PATCH", f"/banks/{bank_id}")
        return {}

    async def arecall(self, **kwargs):
        self._request("POST", "/recall")
        return SimpleNamespace(
            results=[
                SimpleNamespace(text="memory one"),
                SimpleNamespace(text="memory two"),
            ]
        )

    async def areflect(self, **kwargs):
        self._request("POST", "/reflect")
        return SimpleNamespace(text="a synthesis")

    async def aretain_batch(self, **kwargs):
        self._request("POST", "/memories")
        self.retain_calls += 1
        return SimpleNamespace(ok=True)

    async def aclose(self):
        if self._session is not None:
            await self._session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Keep ambient HINDSIGHT_* env from steering config loading."""
    for key in (
        "HINDSIGHT_API_KEY", "HINDSIGHT_API_URL", "HINDSIGHT_BANK_ID",
        "HINDSIGHT_BUDGET", "HINDSIGHT_MODE", "HINDSIGHT_TIMEOUT",
        "HINDSIGHT_IDLE_TIMEOUT", "HINDSIGHT_LLM_API_KEY",
        "HINDSIGHT_RETAIN_TAGS", "HINDSIGHT_RETAIN_OBSERVATION_SCOPES",
        "HINDSIGHT_RETAIN_SOURCE",
    ):
        monkeypatch.delenv(key, raising=False)


def _call_in_thread(fn, *args, **kwargs):
    """Run fn in a short-lived worker thread; re-raise its failure, if any."""
    outcome = {}

    def _run():
        try:
            outcome["value"] = fn(*args, **kwargs)
        except BaseException as exc:
            outcome["error"] = exc

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=30)
    assert not t.is_alive(), "worker thread hung"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _make_provider(tmp_path, monkeypatch):
    """Provider in local_external mode whose SDK client is the loop-bound fake."""
    _LoopBoundHindsightClient.instances = []
    config = {
        "mode": "local_external",
        "api_url": "http://127.0.0.1:9/v1",  # never dialed: the client is a fake
        "bank_id": "loop-bank",
        "budget": "mid",
        "memory_mode": "hybrid",
        "retain_async": False,
    }
    (tmp_path / "hindsight").mkdir(parents=True, exist_ok=True)
    (tmp_path / "hindsight" / "config.json").write_text(json.dumps(config))
    monkeypatch.setattr("plugins.memory.hindsight.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *a, **kw: None)
    # Keep the retain-target resolution hermetic (no urllib /version probe).
    monkeypatch.setattr(
        "plugins.memory.hindsight._check_api_supports_update_mode_append",
        lambda *a, **kw: False,
    )

    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "hindsight_client":
            return SimpleNamespace(Hindsight=_LoopBoundHindsightClient)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    provider = HindsightMemoryProvider()
    provider.initialize(
        session_id="loop-session", hermes_home=str(tmp_path), platform="cli"
    )
    return provider


def _assert_single_loop_owned_client():
    """One client, built and closed on the shared Hindsight loop thread."""
    instances = _LoopBoundHindsightClient.instances
    assert len(instances) == 1, (
        f"expected exactly one SDK client, got {[c.construction_thread for c in instances]}"
    )
    client = instances[0]
    assert client.construction_thread == "hindsight-loop"
    assert client.session_creations == 1
    assert client._session is not None
    assert client._session.loop is hindsight_mod._loop
    assert client._session.closed is True
    assert client._session.closed_thread == "hindsight-loop"
    # Every HTTP request — bank defaults included — executed on the owning
    # loop thread, whatever thread the caller was on.
    requester_threads = {t for t, _m, _p in client._session.requests}
    assert requester_threads == {"hindsight-loop"}
    # The loop-poisoning sync wrappers must never be reached.
    assert client.sync_wrapper_calls == []
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_tools_work_across_caller_threads_on_one_owning_loop(
    tmp_path, monkeypatch, caplog
):
    """recall (main thread), reflect + retain (worker threads), writer-thread
    auto-retain: all succeed against one client/session on the shared loop."""
    caplog.set_level(logging.DEBUG, logger="plugins.memory.hindsight")
    provider = _make_provider(tmp_path, monkeypatch)

    # Decoy loop installed as the main thread's current loop: models the
    # ambient event loop the production CLI main thread has. The SDK's sync
    # _run_async wrappers grab exactly this loop — deterministically binding
    # the shared session to it on the unfixed provider.
    decoy = asyncio.new_event_loop()
    asyncio.set_event_loop(decoy)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")

            # 1) First-ever client use from the foreground (main) thread:
            #    constructs the client, applies bank defaults, recalls.
            main_result = provider.handle_tool_call(
                "hindsight_recall", {"query": "what does the user like?"}
            )
            assert json.loads(main_result) == {
                "result": "1. memory one\n2. memory two"
            }

            # 2) Reflect + retain foreground tools from other threads.
            reflect_result = _call_in_thread(
                provider.handle_tool_call,
                "hindsight_reflect", {"query": "summarize the session"},
            )
            assert json.loads(reflect_result) == {"result": "a synthesis"}

            retain_result = _call_in_thread(
                provider.handle_tool_call,
                "hindsight_retain",
                {"content": "user prefers deterministic tests"},
            )
            assert json.loads(retain_result) == {
                "result": "Memory stored successfully."
            }

            # 3) Writer-thread retain: sync_turn enqueues, the single-writer
            #    background thread drains it.
            _call_in_thread(
                provider.sync_turn,
                "user: hello", "assistant: hi",
                session_id="loop-session",
            )

            # 4) Clean shutdown drains the writer and closes the client.
            provider.shutdown()

            gc.collect()
    finally:
        asyncio.set_event_loop(None)
        decoy.close()

    client = _assert_single_loop_owned_client()
    # Foreground retain + writer auto-retain both shipped over that session.
    assert client.retain_calls == 2
    # Bank defaults rode the same session before the first recall.
    methods = [m for _t, m, _p in client._session.requests]
    assert methods[0] == "PATCH"

    # No loop-mismatch fallout and no leaked coroutines anywhere above.
    assert "Hindsight loop unavailable" not in caplog.text
    assert "Timeout context manager should be used inside a task" not in caplog.text
    never_awaited = [
        w for w in caught
        if issubclass(w.category, RuntimeWarning)
        and ("never awaited" in str(w.message) or "different loop" in str(w.message))
    ]
    assert never_awaited == []


def test_first_use_on_writer_thread_stays_on_owning_loop(tmp_path, monkeypatch, caplog):
    """First client creation triggered by the retain writer thread (the
    auto-retain path) still lands bank defaults + retains on the shared loop."""
    caplog.set_level(logging.DEBUG, logger="plugins.memory.hindsight")
    provider = _make_provider(tmp_path, monkeypatch)

    _call_in_thread(
        provider.sync_turn,
        "user: ping", "assistant: pong",
        session_id="loop-session",
    )
    provider.shutdown()

    client = _assert_single_loop_owned_client()
    assert client.retain_calls == 1
    assert "Hindsight loop unavailable" not in caplog.text
    assert "Timeout context manager should be used inside a task" not in caplog.text
