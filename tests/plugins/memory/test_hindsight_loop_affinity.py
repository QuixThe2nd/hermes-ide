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
import concurrent.futures
import gc
import json
import logging
import threading
import time
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


def _make_provider(tmp_path, monkeypatch, client_cls=_LoopBoundHindsightClient):
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
            return SimpleNamespace(Hindsight=client_cls)
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
    # Let the writer finish its retain BEFORE shutdown begins: this test
    # targets the writer-triggered FIRST build's loop affinity, and a
    # first-client setup that straddles shutdown() is now (correctly)
    # fenced out and dropped instead of installed — a different regression
    # (see test_shutdown_fences_parked_caller_setup_and_closes_late_client).
    # Poll unfinished_tasks like the provider's own drain barrier does.
    deadline = time.monotonic() + 10.0
    while provider._retain_queue.unfinished_tasks > 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert provider._retain_queue.unfinished_tasks == 0, (
        "writer never drained the queued auto-retain"
    )
    provider.shutdown()

    client = _assert_single_loop_owned_client()
    assert client.retain_calls == 1
    assert "Hindsight loop unavailable" not in caplog.text
    assert "Timeout context manager should be used inside a task" not in caplog.text


# ---------------------------------------------------------------------------
# Regression: every client the provider displaces must be closed on the loop
# it was built on — not just the one shutdown() happens to find cached.
# ---------------------------------------------------------------------------


class _LoopBoundEmbeddedClient:
    """HindsightEmbedded stand-in: a sync wrapper around an inner async client.

    The real wrapper delegates the async data plane to ``self._client`` and
    ships only a sync ``close()`` of its own, which is why the provider has to
    release the inner client on the owning loop before calling ``close()``.
    """

    def __init__(self, inner):
        self._client = inner
        self.close_calls = []

    def __getattr__(self, name):
        # arecall / aretain_batch / directives / ... live on the inner client.
        return getattr(self._client, name)

    def close(self):
        self.close_calls.append(threading.current_thread().name)
        self._client = None


def _track_aclose(client):
    """Record which thread runs client.aclose(), then do the real close."""
    real_aclose = client.aclose
    threads = []

    async def _tracked_aclose():
        threads.append(threading.current_thread().name)
        await real_aclose()

    client.aclose = _tracked_aclose
    return threads


def _stale_embedded_provider(tmp_path, monkeypatch):
    """Provider whose cached client fails with a stale embedded-daemon error.

    Returns (provider, stale_wrapper, stale_inner). The stale client's recall
    creates its session (as in production) and then raises the connection
    error _run_hindsight_operation retries on; the caller supplies whatever
    replacement clients it wants _build_client to hand back.
    """
    provider = _make_provider(tmp_path, monkeypatch)
    # Only the embedded daemon's failures are retried, so opt the provider in.
    provider._mode = "local_embedded"
    provider._bank_defaults_applied = True

    stale_inner = _LoopBoundHindsightClient()

    async def _stale_recall(**kwargs):
        stale_inner._request("POST", "/recall")
        raise RuntimeError("Cannot connect to host 127.0.0.1:8888")

    stale_inner.arecall = _stale_recall
    stale = _LoopBoundEmbeddedClient(stale_inner)
    provider._client = stale
    return provider, stale, stale_inner


def test_stale_daemon_retry_closes_displaced_client_on_owning_loop(
    tmp_path, monkeypatch, caplog
):
    """The client a retry displaces is closed on the shared loop, not leaked.

    Production symptom: the retry blanked ``self._client`` and dropped the
    stale SDK client without closing it, so its aiohttp session survived until
    interpreter teardown — and shutdown() then closed only the replacement.
    """
    caplog.set_level(logging.DEBUG, logger="plugins.memory.hindsight")
    provider, stale, stale_inner = _stale_embedded_provider(tmp_path, monkeypatch)

    replacement_inner = _LoopBoundHindsightClient()
    replacement = _LoopBoundEmbeddedClient(replacement_inner)
    monkeypatch.setattr(provider, "_build_client", lambda: next(iter([replacement])))

    result = json.loads(_call_in_thread(
        provider.handle_tool_call, "hindsight_recall", {"query": "anything"}
    ))

    assert result == {"result": "1. memory one\n2. memory two"}
    assert provider._client is replacement
    # The displaced inner client was released ON the owning loop — the same
    # loop its session was created on.
    assert stale_inner._session.closed is True
    assert stale_inner._session.closed_thread == "hindsight-loop"
    assert len(stale.close_calls) == 1
    # The retry was served by the replacement, on that same loop.
    assert replacement_inner._session.loop is hindsight_mod._loop
    assert {t for t, _m, _p in replacement_inner._session.requests} == {"hindsight-loop"}

    # Shutdown stays correct: it closes only the surviving client.
    provider.shutdown()
    assert provider._client is None
    assert replacement_inner._session.closed is True
    assert replacement_inner._session.closed_thread == "hindsight-loop"
    assert len(replacement.close_calls) == 1
    assert "Unclosed client session" not in caplog.text


def test_stale_daemon_retry_defers_to_concurrently_installed_client(
    tmp_path, monkeypatch
):
    """A client installed while the retry recreates its own wins the slot.

    The window is reachable in production: caller A's operation fails, A drops
    the stale client from the slot and closes it, and in that gap caller B's
    _get_client() builds and caches a fresh client. The retry then stored its
    own replacement unconditionally — clobbering B's client and orphaning its
    session.
    """
    provider, stale, stale_inner = _stale_embedded_provider(tmp_path, monkeypatch)

    newer_inner = _LoopBoundHindsightClient()
    newer = _LoopBoundEmbeddedClient(newer_inner)  # built + cached by caller B

    real_aclose = provider._aclose_client

    async def _close_while_b_installs(client):
        # Caller B's rebuild lands in the slot while A is still closing the
        # client that failed.
        provider._client = newer
        return await real_aclose(client)

    monkeypatch.setattr(provider, "_aclose_client", _close_while_b_installs)
    # The retry must reuse B's client, not build a third one.
    monkeypatch.setattr(
        provider, "_build_client",
        lambda: (_ for _ in ()).throw(AssertionError("retry built a duplicate client")),
    )

    result = json.loads(_call_in_thread(
        provider.handle_tool_call, "hindsight_recall", {"query": "anything"}
    ))

    assert result == {"result": "1. memory one\n2. memory two"}
    # Caller B's install survived — neither replaced nor closed.
    assert provider._client is newer
    assert len(newer.close_calls) == 0
    assert newer_inner._session.loop is hindsight_mod._loop
    assert {t for t, _m, _p in newer_inner._session.requests} == {"hindsight-loop"}
    # The client that actually failed was still closed, on the owning loop.
    assert stale_inner._session.closed is True
    assert stale_inner._session.closed_thread == "hindsight-loop"


def test_install_client_closes_the_losing_duplicate_on_owning_loop(
    tmp_path, monkeypatch
):
    """_install_client keeps the installed client and closes only the loser.

    Exercises the cloud-mode branch of the close path, which the retry can't
    reach (its retriable errors are embedded-only) but a concurrent first-use
    race can.
    """
    provider = _make_provider(tmp_path, monkeypatch)
    winner = _LoopBoundHindsightClient()
    winner_close_threads = _track_aclose(winner)
    loser = _LoopBoundHindsightClient()
    loser_close_threads = _track_aclose(loser)
    provider._client = winner

    assert provider._install_client(loser) is winner
    assert provider._client is winner
    assert loser_close_threads == ["hindsight-loop"]
    # The winner is returned, never closed.
    assert winner_close_threads == []
    assert provider._install_client(winner) is winner
    assert winner_close_threads == []


# ---------------------------------------------------------------------------
# Regression: first-client setup is a lifecycle step, not an HTTP request.
#
# _build_client() can legitimately run far longer than one request (lazy
# dependency install, embedded daemon start), so the caller's wait for it
# must not be bounded by the configured REQUEST timeout — and when that wait
# is abandoned, the client the build eventually completes with must be
# released on the owning loop, not stranded where shutdown() can never see
# it. The fakes below gate construction on Events the test controls, so the
# "slow install" is deterministic and no test sleeps anywhere near the
# production timeouts (120s request / 300s setup).
# ---------------------------------------------------------------------------


# How long a gated fake build may block before the test declares it wedged.
# Ten seconds is generous for an Event handoff and keeps a broken provider
# from hanging the suite.
_GATE_WAIT_S = 10.0


class _GatedHindsightClient(_LoopBoundHindsightClient):
    """Loop-bound SDK stand-in whose construction the test gates.

    Construction signals ``started``, then blocks until ``gate`` opens (or
    raises immediately while ``fail_constructions`` counts down), modeling a
    first-client build that is slower than any request timeout. ``aclose``
    raises while ``fail_closes`` counts down, modeling a client whose close
    itself fails, and parks on ``close_gate`` while ``block_closes`` counts
    down, modeling a reconciliation close that outlives the reconciler's
    bounded wait. Every close attempt is recorded (at entry, so a scheduled
    but never-run attempt is still visible) with the thread it ran on, and
    ``events`` preserves the total order of constructions vs closes for the
    next-builder boundary assertions.
    """

    started: threading.Event | None = None
    gate: threading.Event | None = None
    closed: threading.Event | None = None
    close_failed: threading.Event | None = None
    close_gate: threading.Event | None = None
    fail_constructions = 0
    fail_closes = 0
    block_closes = 0
    constructed: list["_GatedHindsightClient"] = []
    events: list[tuple[str, "_GatedHindsightClient"]] = []
    _order_lock = threading.Lock()

    def __init__(self, **kwargs):
        self.close_threads: list[str] = []
        self.close_attempts: list[str] = []
        if type(self).fail_constructions:
            type(self).fail_constructions -= 1
            raise RuntimeError("simulated lazy-install failure")
        if type(self).started is not None:
            type(self).started.set()
        if type(self).gate is not None:
            assert type(self).gate.wait(timeout=_GATE_WAIT_S), (
                "gated construction never released"
            )
        super().__init__(**kwargs)
        with type(self)._order_lock:
            type(self).constructed.append(self)
            type(self).events.append(("constructed", self))

    async def aclose(self):
        # Recorded at entry, before any gating: a close the test merely
        # SCHEDULED onto a parked loop must still be countable here.
        self.close_attempts.append(threading.current_thread().name)
        if type(self).fail_closes:
            type(self).fail_closes -= 1
            with type(self)._order_lock:
                type(self).events.append(("close-failed", self))
            if type(self).close_failed is not None:
                type(self).close_failed.set()
            raise RuntimeError("simulated close failure")
        if type(self).block_closes:
            type(self).block_closes -= 1
            assert type(self).close_gate.wait(timeout=_GATE_WAIT_S), (
                "gated close never released"
            )
        self.close_threads.append(threading.current_thread().name)
        try:
            await super().aclose()
        finally:
            with type(self)._order_lock:
                type(self).events.append(("closed", self))
            if type(self).closed is not None:
                type(self).closed.set()


def _reset_gated_client(*, blocked: bool) -> tuple[threading.Event, ...]:
    """Arm per-test class state; returns (started, gate, closed).

    ``blocked=False`` leaves the gate open so construction runs immediately.
    The class-level ``close_failed`` Event (signalled when an aclose attempt
    raises) and the ``close_gate``/``block_closes`` parking controls are
    also armed for the failed-/parked-close regressions.
    """
    started, gate, closed = threading.Event(), threading.Event(), threading.Event()
    _GatedHindsightClient.started = started
    _GatedHindsightClient.gate = gate
    _GatedHindsightClient.closed = closed
    _GatedHindsightClient.close_failed = threading.Event()
    _GatedHindsightClient.close_gate = threading.Event()
    _GatedHindsightClient.fail_constructions = 0
    _GatedHindsightClient.fail_closes = 0
    _GatedHindsightClient.block_closes = 0
    _GatedHindsightClient.constructed = []
    _GatedHindsightClient.events = []
    if not blocked:
        gate.set()
    return started, gate, closed


class _ClientCallThread:
    """Runs provider._get_client() on a worker thread WITHOUT blocking the
    test — the scenario has to steer events (open the construction gate,
    let budgets expire) while the caller is mid-wait. ``join()`` returns the
    outcome dict and fails the test if the caller hung."""

    def __init__(self, provider):
        self.outcome = {}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self._provider = provider

    def _run(self):
        try:
            self.outcome["value"] = self._provider._get_client()
        except BaseException as exc:
            self.outcome["error"] = exc

    def start(self) -> "_ClientCallThread":
        self.thread.start()
        return self

    def join(self) -> dict:
        self.thread.join(timeout=30)
        assert not self.thread.is_alive(), "first-use caller hung"
        return self.outcome


def test_slow_client_setup_outlives_request_timeout(tmp_path, monkeypatch):
    """A short REQUEST timeout must not fail a slower-but-valid client setup.

    Production symptom: _get_client() waited for the first build under the
    request timeout (default 120s) while tools.lazy_deps allows 300s for the
    lazy install. The wait expired, the operation failed, and the client the
    build eventually completed with was installed by nobody and closed by
    nobody — while the next caller built a second one.
    """
    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    # A short request timeout: valid for retain/recall, far too short for a
    # cold install. The setup wait must outlive it.
    provider._timeout = 0.25
    started, _gate, _closed = _reset_gated_client(blocked=True)

    caller = _ClientCallThread(provider).start()

    # Let the request-timeout window expire while the build is still gated —
    # on the unfixed provider the caller has already given up by here.
    assert started.wait(timeout=_GATE_WAIT_S), "client build never started"
    time.sleep(0.8)
    _GatedHindsightClient.gate.set()

    outcome = caller.join()

    assert "error" not in outcome, (
        f"slow-but-valid setup failed under a {provider._timeout}s request "
        f"timeout: {outcome.get('error')!r}"
    )
    slow_client = outcome["value"]
    # Exactly one client was built; it was installed and served, not orphaned.
    assert _GatedHindsightClient.constructed == [slow_client]
    assert provider._client is slow_client
    assert slow_client.construction_thread == "hindsight-loop"
    assert slow_client.close_threads == []

    # The slowly-built client actually serves traffic.
    result = json.loads(provider.handle_tool_call(
        "hindsight_recall", {"query": "what does the user like?"}
    ))
    assert result == {"result": "1. memory one\n2. memory two"}

    provider.shutdown()
    assert slow_client.close_threads == ["hindsight-loop"]


def test_abandoned_setup_closes_late_client_on_owning_loop(tmp_path, monkeypatch):
    """A setup whose caller gave up must close its late client on the loop.

    When the caller abandons the build wait, the coroutine is the only side
    left that knows the finished client — it must release it ON the owning
    loop, and the next builder must wait for that release before treating a
    replacement as installed (never two live clients with nobody to close
    the displaced one).
    """
    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    provider._timeout = 0.25
    # Shrink the setup budget symmetrically on both sides of the comparison:
    # the module constant exists only on the fixed provider, where it caps
    # the setup wait; on the unfixed provider the request timeout below was
    # the only cap. Either way the caller abandons a still-blocked build.
    monkeypatch.setattr(
        hindsight_mod, "_CLIENT_SETUP_TIMEOUT", 0.5, raising=False
    )
    started, _gate, closed = _reset_gated_client(blocked=True)

    caller = _ClientCallThread(provider).start()
    assert started.wait(timeout=_GATE_WAIT_S), "client build never started"
    # Past both budgets (0.25s request / 0.5s setup) with the build gated:
    # the caller must have failed by now, not hung.
    time.sleep(1.0)
    outcome = caller.join()
    assert "value" not in outcome, "caller outlived its setup budget"
    assert type(outcome["error"]).__name__ == "TimeoutError", outcome["error"]

    # The build completes LATE — after every waiter is gone.
    _GatedHindsightClient.gate.set()
    assert closed.wait(timeout=_GATE_WAIT_S), (
        "client completed after its setup was abandoned was never closed"
    )
    late_client = _GatedHindsightClient.constructed[0]
    assert late_client.close_threads == ["hindsight-loop"]

    # The next caller gets a fresh client — built only after the late one
    # was released, so at no point were two live clients installed/ignored.
    replacement = _ClientCallThread(provider).start().join()
    assert "error" not in replacement, replacement.get("error")
    assert _GatedHindsightClient.constructed == [late_client, replacement["value"]]
    assert provider._client is replacement["value"]
    with _GatedHindsightClient._order_lock:
        events = list(_GatedHindsightClient.events)
    assert events.index(("closed", late_client)) < events.index(
        ("constructed", replacement["value"])
    )

    provider.shutdown()
    assert replacement["value"].close_threads == ["hindsight-loop"]


def test_client_setup_handshake_contract():
    """The _ClientSetup state machine grants the finished client to exactly
    one side: the caller claims it, or the coroutine disposes of it."""

    _ClientSetup = hindsight_mod._ClientSetup

    # Caller gave up before the build landed: the offerer must dispose.
    abandoned = _ClientSetup()
    assert not abandoned.settled.is_set()
    assert abandoned.abandon() is True
    assert abandoned.offer(object()) is False

    # Build landed while the caller was still waiting: claim it, once.
    claimed = _ClientSetup()
    client = object()
    assert claimed.offer(client) is True
    assert claimed.abandon() is False  # arrived in time — do not abandon
    assert claimed.claim() is client
    assert claimed.claim() is None

    # A failed build settles immediately: nothing to hand over or close.
    failed = _ClientSetup()
    failed.fail()
    assert failed.settled.is_set()


def test_normal_operations_still_obey_request_timeout(tmp_path, monkeypatch):
    """retain/recall/reflect keep the configured REQUEST timeout even though
    the first-client setup wait is deliberately longer."""
    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    _reset_gated_client(blocked=False)
    client = provider._get_client()  # built under the default budget
    assert client is _GatedHindsightClient.constructed[0]

    provider._timeout = 0.25

    async def _never_completes(**kwargs):
        await asyncio.Future()  # models a wedged server response

    client.arecall = _never_completes

    began = time.monotonic()
    result = provider.handle_tool_call(
        "hindsight_recall", {"query": "what does the user like?"}
    )
    elapsed = time.monotonic() - began
    assert "Failed to search memory" in result
    assert elapsed < 5.0, f"recall outlived the request timeout: {elapsed:.2f}s"


def test_failed_first_build_surfaces_error_and_next_call_rebuilds(
    tmp_path, monkeypatch
):
    """A raising build settles its setup (nothing to join later) and the
    error reaches the caller; the next first-use attempt rebuilds."""
    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    _reset_gated_client(blocked=False)
    _GatedHindsightClient.fail_constructions = 1

    first = _ClientCallThread(provider).start().join()
    assert isinstance(first.get("error"), RuntimeError)
    assert "simulated lazy-install failure" in str(first["error"])
    # The failure left no abandoned setup behind for the next builder to join.
    assert getattr(provider, "_abandoned_setup", None) is None

    second = _ClientCallThread(provider).start().join()
    assert "error" not in second, second.get("error")
    assert _GatedHindsightClient.constructed == [second["value"]]
    assert provider._client is second["value"]


# ---------------------------------------------------------------------------
# Regression: the abandoned-setup join is bounded and fail-closed.
#
# Nothing mechanically guarantees _build_client() or the late client's
# aclose() ever finish, so the join that waits for an abandoned generation
# must itself be bounded: on expiry the caller gets an error, the exact
# generation stays recorded for later reconciliation, and NO replacement is
# built. settled must mean "safely terminal" (failed build, or a client
# actually closed) — never mere coroutine completion — and shutdown must
# reconcile a close that failed there. All waits below are Event-gated or
# monkeypatched to sub-second bounds; no production-duration sleeps.
# ---------------------------------------------------------------------------


def test_never_settling_abandoned_setup_fails_next_caller_closed(
    tmp_path, monkeypatch
):
    """A wedged abandoned generation must not hang every later caller.

    Production defect: _join_abandoned_client_setup() waited on settled
    with NO timeout while the caller held _client_lock, so one wedged
    generation — a build or cleanup that never finishes — blocked every
    later first-use caller forever. The join must instead be bounded and
    fail CLOSED: bounded error to the caller, the exact generation kept
    recorded for later reconciliation, no replacement constructed.
    """
    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    provider._timeout = 0.25
    monkeypatch.setattr(hindsight_mod, "_CLIENT_SETUP_TIMEOUT", 0.5, raising=False)
    started, gate, closed = _reset_gated_client(blocked=True)

    first = _ClientCallThread(provider).start()
    assert started.wait(timeout=_GATE_WAIT_S), "client build never started"
    # Past both budgets (0.25s request / 0.5s setup) with the build gated.
    time.sleep(1.0)
    outcome = first.join()
    assert type(outcome["error"]).__name__ == "TimeoutError", outcome["error"]

    # The abandoned cleanup never settles: the gate stays closed, so the
    # coroutine never completes and its close never runs. The next caller
    # must get a bounded cleanup-pending error — never a hang, never a
    # replacement built beside the un-reconciled generation.
    second = _ClientCallThread(provider).start()
    outcome2 = second.join()
    assert "value" not in outcome2, (
        "a wedged abandoned setup must not hand out a client"
    )
    error2 = outcome2["error"]
    assert type(error2).__name__ == "TimeoutError", error2
    assert "fail-closed" in str(error2), error2
    assert provider._abandoned_setup is not None, (
        "fail-closed join dropped the wedged generation instead of keeping "
        "it recorded for later reconciliation"
    )
    assert _GatedHindsightClient.constructed == [], (
        "a replacement was constructed while the abandoned generation "
        "could not be reconciled"
    )

    # Recovery stays possible: once the wedged build finally completes, its
    # own coroutine closes the late client and settles the generation.
    gate.set()
    assert closed.wait(timeout=_GATE_WAIT_S), (
        "late client of the finally-settled generation was never closed"
    )


def test_failed_late_close_blocks_replacement_until_reconciled(
    tmp_path, monkeypatch
):
    """A close that raises must leave settled CLEAR and the client tracked.

    Production defect: _aclose_client() swallowed close errors and the
    setup coroutine set settled unconditionally, so the next builder's join
    mistook a possibly-still-live late client for a finished cleanup and
    built a replacement beside it — an orphan nobody (including shutdown,
    which only saw self._client) would ever close.
    """
    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    provider._timeout = 0.25
    monkeypatch.setattr(hindsight_mod, "_CLIENT_SETUP_TIMEOUT", 0.5, raising=False)
    started, gate, closed = _reset_gated_client(blocked=True)
    # The late client's close raises once; every later attempt succeeds.
    _GatedHindsightClient.fail_closes = 1

    first = _ClientCallThread(provider).start()
    assert started.wait(timeout=_GATE_WAIT_S), "client build never started"
    time.sleep(1.0)
    outcome = first.join()
    assert type(outcome["error"]).__name__ == "TimeoutError", outcome["error"]

    # The build completes LATE and its close FAILS on the owning loop.
    gate.set()
    assert _GatedHindsightClient.close_failed.wait(timeout=_GATE_WAIT_S), (
        "late client close was never attempted"
    )
    late = _GatedHindsightClient.constructed[0]
    setup = provider._abandoned_setup
    assert setup is not None
    # Coroutine completion is NOT a safe disposition: settled alone must
    # not authorize a replacement while the late client may still be live.
    assert not setup.settled.is_set()
    assert setup.tracked_client() is late
    assert late.close_threads == []  # no successful close has happened yet

    # The next caller's bounded join sees the tracked close failure and
    # retries the close itself, ON the owning loop; only once that
    # succeeds is exactly one replacement built.
    replacement = _ClientCallThread(provider).start().join()
    assert "error" not in replacement, replacement.get("error")
    assert _GatedHindsightClient.constructed == [late, replacement["value"]]
    assert late.close_threads == ["hindsight-loop"]
    with _GatedHindsightClient._order_lock:
        events = list(_GatedHindsightClient.events)
    assert events.index(("closed", late)) < events.index(
        ("constructed", replacement["value"])
    )
    assert provider._client is replacement["value"]

    provider.shutdown()
    assert replacement["value"].close_threads == ["hindsight-loop"]


def test_shutdown_reconciles_tracked_client_after_failed_late_close(
    tmp_path, monkeypatch
):
    """shutdown() must not let a close-failed abandoned client die live.

    Production defect: shutdown() closed only self._client and dropped the
    abandoned setup entirely, so a late client whose coroutine-close raised
    outlived the provider with no remaining reference that could close it.
    """
    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    provider._timeout = 0.25
    monkeypatch.setattr(hindsight_mod, "_CLIENT_SETUP_TIMEOUT", 0.5, raising=False)
    started, gate, closed = _reset_gated_client(blocked=True)
    _GatedHindsightClient.fail_closes = 1

    first = _ClientCallThread(provider).start()
    assert started.wait(timeout=_GATE_WAIT_S), "client build never started"
    time.sleep(1.0)
    outcome = first.join()
    assert type(outcome["error"]).__name__ == "TimeoutError", outcome["error"]

    gate.set()
    assert _GatedHindsightClient.close_failed.wait(timeout=_GATE_WAIT_S), (
        "late client close was never attempted"
    )
    late = _GatedHindsightClient.constructed[0]
    setup = provider._abandoned_setup
    assert setup is not None
    # The failed close left the generation unsafe (settled clear) — which
    # is exactly why shutdown must take over the tracked client below.
    assert not setup.settled.is_set()
    assert setup.tracked_client() is late
    assert late.close_threads == []

    provider.shutdown()
    assert provider._client is None
    assert late.close_threads == ["hindsight-loop"], (
        "shutdown did not make a bounded owning-loop close attempt for the "
        "tracked abandoned client"
    )
    assert provider._abandoned_setup is None


def test_abandoned_join_is_bounded_and_keeps_owning_loop_live(
    tmp_path, monkeypatch
):
    """No wait path may block the owning Hindsight loop thread.

    Production defect: the abandoned join waited on settled with no timeout
    WHILE HOLDING _client_lock, so a wedged generation starved every later
    first-use caller forever — including a loop-side _get_client(), which
    would park the shared loop thread on that lock. The join must release
    the caller within a deterministic bound, and the wait machinery itself
    must never sit on the owning loop: every wait runs on the caller
    thread, and the loop only ever runs short close attempts.

    The wedged state here is a close that keeps failing (the loop is idle
    between attempts), so loop liveness is attributable to the provider's
    wait paths alone — not to the gated construction fake, which models a
    slow synchronous install by parking the loop on purpose.
    """
    from agent.async_utils import safe_schedule_threadsafe

    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    provider._timeout = 0.25
    monkeypatch.setattr(hindsight_mod, "_CLIENT_SETUP_TIMEOUT", 0.5, raising=False)
    started, gate, closed = _reset_gated_client(blocked=True)
    # Both close attempts fail — the coroutine's own and the join's retry —
    # leaving the generation recorded but permanently unsafe to replace.
    _GatedHindsightClient.fail_closes = 2

    first = _ClientCallThread(provider).start()
    assert started.wait(timeout=_GATE_WAIT_S), "client build never started"
    time.sleep(1.0)
    outcome = first.join()
    assert type(outcome["error"]).__name__ == "TimeoutError", outcome["error"]

    # The build completes late; its close fails, so the generation is
    # recorded unsafe (settled clear, client tracked) and the loop is idle.
    gate.set()
    assert _GatedHindsightClient.close_failed.wait(timeout=_GATE_WAIT_S), (
        "late client close was never attempted"
    )
    late = _GatedHindsightClient.constructed[0]
    assert late.close_threads == []

    second = _ClientCallThread(provider).start()
    # Let the second caller reach the abandoned join and take the lock.
    time.sleep(0.3)

    async def _loop_probe():
        await asyncio.sleep(0)
        return "alive"

    # While the join is waiting, the owning loop stays live: the wait runs
    # on the caller thread, so work scheduled on the loop keeps running.
    probe = safe_schedule_threadsafe(_loop_probe(), hindsight_mod._get_loop())
    assert probe is not None, "could not schedule the loop-liveness probe"
    assert probe.result(timeout=5.0) == "alive"

    # And the join is bounded: even with its close retry failing, the
    # caller releases _client_lock within the deterministic budget instead
    # of holding it forever.
    assert provider._client_lock.acquire(timeout=5.0), (
        "abandoned join held _client_lock past its bounded wait"
    )
    provider._client_lock.release()

    outcome2 = second.join()
    assert "value" not in outcome2, outcome2
    assert type(outcome2["error"]).__name__ == "TimeoutError", outcome2["error"]
    assert _GatedHindsightClient.constructed == [late], (
        "a replacement was constructed beside the unreconciled generation"
    )

    # Shutdown makes the final bounded owning-loop close attempt itself.
    provider.shutdown()
    assert late.close_threads == ["hindsight-loop"]


# ---------------------------------------------------------------------------
# Regression: fail-closed ownership across reconciliation attempts.
#
# The join/shutdown reconciliation must (a) keep the unsafe generation
# tracked when its own close attempt fails — dropping the provider's last
# reference to a possibly-live client is the leak the tracking exists to
# prevent; (b) wake on a RECORDED close failure promptly instead of
# sleeping out the (correctly large) setup allowance first; and (c) never
# schedule a second concurrent close for one client when an earlier
# reconciler's attempt is still in flight, while still recording that
# attempt's late result so the generation settles and exactly one
# replacement is permitted afterward.
# ---------------------------------------------------------------------------


def _abandon_setup_with_failed_late_close(
    tmp_path, monkeypatch, *, fail_closes: int
):
    """Drive the provider into the close-failed abandoned state.

    Returns (provider, late, setup): the first caller abandoned the gated
    build, the build completed late on the owning loop, and its close
    failed ``fail_closes`` times — leaving the exact client tracked on an
    unsettled abandoned generation.
    """
    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    provider._timeout = 0.25
    monkeypatch.setattr(hindsight_mod, "_CLIENT_SETUP_TIMEOUT", 0.5, raising=False)
    started, gate, _closed = _reset_gated_client(blocked=True)
    _GatedHindsightClient.fail_closes = fail_closes

    first = _ClientCallThread(provider).start()
    assert started.wait(timeout=_GATE_WAIT_S), "client build never started"
    time.sleep(1.0)
    outcome = first.join()
    assert type(outcome["error"]).__name__ == "TimeoutError", outcome["error"]

    gate.set()
    assert _GatedHindsightClient.close_failed.wait(timeout=_GATE_WAIT_S), (
        "late client close was never attempted"
    )
    late = _GatedHindsightClient.constructed[0]
    setup = provider._abandoned_setup
    assert setup is not None, "abandoned generation was dropped"
    assert setup.tracked_client() is late
    return provider, late, setup


def test_shutdown_keeps_unsafe_generation_tracked_across_failed_reconciles(
    tmp_path, monkeypatch
):
    """Consecutive failed shutdown reconciliations must not drop the client.

    Production defect: shutdown() nulled ``_abandoned_setup`` BEFORE making
    its close attempt, and _close_client() discarded both the attempt's
    boolean outcome and its timeout — so a reconciliation close that failed
    (or timed out) destroyed the provider's last tracked reference to a
    possibly-live client. Every failed reconciliation must keep the exact
    generation and client recorded, and only safe settlement may clear it.
    """
    provider, late, setup = _abandon_setup_with_failed_late_close(
        tmp_path, monkeypatch, fail_closes=3
    )

    # First shutdown reconciliation fails: the generation survives it.
    provider.shutdown()
    assert provider._abandoned_setup is setup, (
        "shutdown dropped an unresolved abandoned generation"
    )
    assert setup.tracked_client() is late
    assert not setup.settled.is_set()
    assert late.close_attempts == ["hindsight-loop", "hindsight-loop"], (
        "shutdown did not make exactly one bounded owning-loop close attempt"
    )

    # Second shutdown reconciliation fails too: still tracked, still live
    # for a later reconciler — never dropped while the outcome is unsafe.
    provider.shutdown()
    assert provider._abandoned_setup is setup, (
        "a second failed shutdown reconciliation dropped the generation"
    )
    assert setup.tracked_client() is late
    assert not setup.settled.is_set()
    assert late.close_attempts == ["hindsight-loop"] * 3

    # Recovery stays possible: the next reconciliation succeeds and only
    # then is the generation cleared.
    provider.shutdown()
    assert provider._abandoned_setup is None
    assert setup.settled.is_set()
    assert setup.tracked_client() is None
    assert late.close_threads == ["hindsight-loop"]


def test_recorded_close_failure_wakes_next_caller_promptly(tmp_path, monkeypatch):
    """A close failure already on record must be retried NOW, not after the
    full setup allowance.

    Production defect: _join_abandoned_client_setup() waited out all of
    _client_setup_timeout() on ``settled`` before it ever consulted the
    tracked client, so with the (correctly large, install-scale) setup
    allowance a close the abandoned coroutine had already failed sat
    between every later caller and its reconciliation retry for the whole
    budget — 300s by default.
    """
    provider, late, setup = _abandon_setup_with_failed_late_close(
        tmp_path, monkeypatch, fail_closes=1
    )
    # Make the setup allowance production-scale NOW: any join that sleeps
    # it out before reconciling stalls far beyond what this test tolerates.
    monkeypatch.setattr(hindsight_mod, "_CLIENT_SETUP_TIMEOUT", 30.0, raising=False)

    second = _ClientCallThread(provider).start()
    assert _GatedHindsightClient.closed.wait(timeout=3.0), (
        "a close failure recorded before the join did not wake the next "
        "caller promptly — reconciliation stalled for the full setup "
        "allowance"
    )
    outcome = second.join()
    assert "error" not in outcome, outcome.get("error")
    assert late.close_threads == ["hindsight-loop"]
    assert setup.settled.is_set()
    assert provider._abandoned_setup is None
    # Exactly one replacement, built only after the failed close was
    # reconciled — never beside a possibly-live displaced client.
    assert _GatedHindsightClient.constructed == [late, outcome["value"]]
    assert provider._client is outcome["value"]


def test_in_flight_reconciliation_close_is_never_duplicated(tmp_path, monkeypatch):
    """One in-flight close per generation; the late result is not lost.

    Production defect: a reconciliation close whose coroutine outlived the
    reconciler's bounded wait left no record of the attempt, so a second
    caller scheduled ANOTHER aclose() for the same possibly-live client —
    concurrent closes on one client — and the parked attempt's eventual
    success died with its abandoned Future: nothing settled the generation.
    """
    provider, late, setup = _abandon_setup_with_failed_late_close(
        tmp_path, monkeypatch, fail_closes=1
    )
    # The first reconciliation close parks on close_gate well past the
    # 0.25s reconciler bound; once released it succeeds.
    _GatedHindsightClient.block_closes = 1

    # Count every _aclose_client the provider schedules. The counter is a
    # SYNC wrapper (append, then hand back the awaiting coroutine) so a
    # close the caller thread merely schedules onto the parked loop is
    # counted at creation time — exactly the duplicate the unfixed
    # provider issues, whose coroutine would otherwise sit invisibly in
    # the loop's queue.
    aclose_targets: list = []
    real_aclose_client = provider._aclose_client

    def _counting_aclose(client):
        aclose_targets.append(client)

        async def _awaited():
            return await real_aclose_client(client)

        return _awaited()

    monkeypatch.setattr(provider, "_aclose_client", _counting_aclose)

    # Caller 2 reconciles; its one attempt parks and outlives the caller's
    # bounded wait. The attempt — not a replacement — must stay tracked.
    # (The coroutine's own failed close predates the counting patch; only
    # caller 2's reconciliation attempt is counted.)
    second = _ClientCallThread(provider).start()
    outcome2 = second.join()
    assert type(outcome2["error"]).__name__ == "TimeoutError", outcome2["error"]
    assert aclose_targets == [late]
    assert late.close_attempts == ["hindsight-loop", "hindsight-loop"]
    assert provider._abandoned_setup is setup
    assert setup.tracked_client() is late
    assert not setup.settled.is_set()

    # Caller 3 must WAIT on caller 2's in-flight attempt — never schedule
    # a concurrent duplicate close for the same client.
    third = _ClientCallThread(provider).start()
    time.sleep(0.8)  # caller 3 sits in (and exhausts) its bounded wait
    assert len(aclose_targets) == 1, (
        "a second caller scheduled a concurrent duplicate close for the "
        "same client while a reconciliation attempt was in flight"
    )
    assert provider._abandoned_setup is setup
    outcome3 = third.join()
    assert type(outcome3["error"]).__name__ == "TimeoutError", outcome3["error"]
    assert len(aclose_targets) == 1

    # The parked attempt finally succeeds: its result is recorded on the
    # generation (not lost with the caller that timed out), the SAME
    # generation settles, and exactly one replacement is permitted after.
    _GatedHindsightClient.close_gate.set()
    assert _GatedHindsightClient.closed.wait(timeout=_GATE_WAIT_S), (
        "parked reconciliation close never finished"
    )
    assert setup.settled.is_set()
    assert setup.tracked_client() is None
    assert len(aclose_targets) == 1

    fourth = _ClientCallThread(provider).start()
    outcome4 = fourth.join()
    assert "error" not in outcome4, outcome4.get("error")
    assert _GatedHindsightClient.constructed == [late, outcome4["value"]]
    assert provider._client is outcome4["value"]
    assert late.close_attempts == ["hindsight-loop", "hindsight-loop"]
    with _GatedHindsightClient._order_lock:
        events = list(_GatedHindsightClient.events)
    assert events.index(("closed", late)) < events.index(
        ("constructed", outcome4["value"])
    )

    provider.shutdown()
    assert outcome4["value"].close_threads == ["hindsight-loop"]


# ---------------------------------------------------------------------------
# Regression: the owner-loop _get_client() path must never wait and must
# never bypass an unsafe abandoned generation.
#
# The owner-loop branch exists so an operation lambda running ON the shared
# loop can get the first client without going through _run_sync (which would
# deadlock the loop on a future only it can run). But it previously entered
# _client_lock BEFORE checking _on_hindsight_loop_thread() — a caller thread
# holding that lock through its bounded setup/reconciliation wait parked the
# loop thread for the whole wait — and once released it called _build_client()
# directly, installing a replacement while the exact abandoned generation was
# still unsettled with its client tracked.
# ---------------------------------------------------------------------------


def test_owner_loop_get_client_never_waits_and_fails_closed(
    tmp_path, monkeypatch
):
    """Owner-loop _get_client(): no lock wait, no replacement, fail closed.

    With a caller thread owning _client_lock and an unsettled abandoned
    generation tracking its late client, the REAL owner-loop _get_client()
    must complete promptly (without waiting for the lock release), raise a
    fail-closed error, construct zero replacements, and leave the exact
    generation/client tracked — after which a normal caller-thread caller
    performs the bounded reconciliation and builds exactly one replacement.
    """
    from agent.async_utils import safe_schedule_threadsafe

    provider, late, setup = _abandon_setup_with_failed_late_close(
        tmp_path, monkeypatch, fail_closes=1
    )
    assert provider._client is None

    # A caller thread owns _client_lock for the whole probe window: the
    # owner-loop call must not even try to wait on it.
    assert provider._client_lock.acquire(timeout=5.0)
    loop_outcome: dict = {}
    loop_done = threading.Event()

    async def _get_client_on_loop():
        # The REAL _get_client(), invoked on the owning loop thread — not
        # a no-op coroutine standing in for it.
        try:
            loop_outcome["value"] = provider._get_client()
        except BaseException as exc:
            loop_outcome["error"] = exc
        finally:
            loop_done.set()

    probe = safe_schedule_threadsafe(
        _get_client_on_loop(), hindsight_mod._get_loop()
    )
    assert probe is not None, "could not schedule the owner-loop _get_client probe"
    # On the unfixed provider the loop thread is parked on _client_lock, so
    # this wait expires (the lock is then released to keep the suite moving,
    # and the prompt-completion assertion below fails on the behavior).
    completed_promptly = loop_done.wait(timeout=2.0)
    provider._client_lock.release()
    probe.result(timeout=5.0)

    assert completed_promptly, (
        "owner-loop _get_client() blocked behind _client_lock instead of "
        "completing promptly"
    )
    assert "value" not in loop_outcome, loop_outcome
    error = loop_outcome.get("error")
    assert type(error).__name__ == "RuntimeError", error
    assert "fail-closed" in str(error), error

    # Zero replacement construction; the exact generation and its tracked
    # client are unchanged, still unsettled.
    assert _GatedHindsightClient.constructed == [late]
    assert provider._client is None
    assert provider._abandoned_setup is setup
    assert setup.tracked_client() is late
    assert not setup.settled.is_set()

    # The owning loop stayed responsive the whole time.
    async def _loop_probe():
        await asyncio.sleep(0)
        return "alive"

    responsiveness = safe_schedule_threadsafe(
        _loop_probe(), hindsight_mod._get_loop()
    )
    assert responsiveness is not None
    assert responsiveness.result(timeout=5.0) == "alive"

    # A later non-loop caller performs the bounded reconciliation and only
    # then builds exactly one replacement.
    outcome = _ClientCallThread(provider).start().join()
    assert "error" not in outcome, outcome.get("error")
    assert _GatedHindsightClient.constructed == [late, outcome["value"]]
    assert provider._client is outcome["value"]
    assert late.close_threads == ["hindsight-loop"]
    with _GatedHindsightClient._order_lock:
        events = list(_GatedHindsightClient.events)
    assert events.index(("closed", late)) < events.index(
        ("constructed", outcome["value"])
    )

    provider.shutdown()
    assert outcome["value"].close_threads == ["hindsight-loop"]


# ---------------------------------------------------------------------------
# Regression: a reconciliation Future that goes terminal before its wrapper
# records an outcome must release the generation's single retry slot.
#
# launch_retry() records _retry_future and the loop coroutine's
# note_retry_outcome() clears it — but a Future CANCELLED before its
# coroutine starts never runs that wrapper, so the dead Future owned the
# slot forever: wait_reconcilable() kept reporting "in-flight" and no later
# caller or shutdown could ever retry the close.
# ---------------------------------------------------------------------------


def test_canceled_reconciliation_future_releases_retry_slot(
    tmp_path, monkeypatch
):
    """A pre-start cancellation must not wedge the retry slot in-flight.

    The first reconciliation attempt's Future is cancelled BEFORE its
    coroutine starts (the loop-teardown window). The caller must still get
    a bounded fail-closed error; the same generation/client must stay
    tracked and unsettled; the slot must become retryable again; and a
    later caller must launch exactly one replacement retry, close the
    tracked client on the owning loop, settle the SAME generation, and only
    then build one replacement — with no duplicate close scheduled.
    """
    import agent.async_utils as async_utils

    provider, late, setup = _abandon_setup_with_failed_late_close(
        tmp_path, monkeypatch, fail_closes=1
    )

    real_schedule = async_utils.safe_schedule_threadsafe
    cancel_once = {"armed": True}
    cancelled_futures: list = []

    def _schedule_cancel_first(coro, loop):
        if cancel_once["armed"]:
            cancel_once["armed"] = False
            # Terminal BEFORE the coroutine ever runs: the wrapper can
            # never record an outcome for this attempt.
            future: concurrent.futures.Future = concurrent.futures.Future()
            future.cancel()
            coro.close()  # never started; close it so nothing is leaked
            cancelled_futures.append(future)
            return future
        return real_schedule(coro, loop)

    monkeypatch.setattr(
        async_utils, "safe_schedule_threadsafe", _schedule_cancel_first
    )

    # Caller 2's reconciliation attempt is cancelled pre-start: bounded
    # fail-closed error, and the dead slot must be released — the exact
    # generation stays tracked and retryable, never settled.
    second = _ClientCallThread(provider).start()
    outcome2 = second.join()
    assert "value" not in outcome2, outcome2
    assert type(outcome2["error"]).__name__ == "TimeoutError", outcome2["error"]
    assert len(cancelled_futures) == 1 and cancelled_futures[0].cancelled()
    assert setup.retry_future() is None, (
        "a reconciliation Future cancelled before its coroutine started "
        "kept the generation's retry slot forever"
    )
    assert setup.wait_reconcilable(timeout=1.0) == "retry", (
        "the dead attempt left the generation wedged in-flight"
    )
    assert setup.tracked_client() is late
    assert not setup.settled.is_set()
    assert provider._abandoned_setup is setup
    assert _GatedHindsightClient.constructed == [late]

    # A later caller launches exactly one replacement retry: the tracked
    # client is closed on the owning loop, the SAME generation settles only
    # after that close succeeds, and exactly one replacement is built. The
    # cancelled attempt never ran, so exactly one subsequent close exists.
    third = _ClientCallThread(provider).start()
    outcome3 = third.join()
    assert "error" not in outcome3, outcome3.get("error")
    assert late.close_attempts == ["hindsight-loop", "hindsight-loop"], (
        "expected exactly one subsequent close (the cancelled attempt must "
        "not run, and no duplicate may be scheduled)"
    )
    assert late.close_threads == ["hindsight-loop"]
    assert setup.settled.is_set()
    assert setup.tracked_client() is None
    assert provider._abandoned_setup is None
    assert _GatedHindsightClient.constructed == [late, outcome3["value"]]
    assert provider._client is outcome3["value"]
    with _GatedHindsightClient._order_lock:
        events = list(_GatedHindsightClient.events)
    assert events.index(("closed", late)) < events.index(
        ("constructed", outcome3["value"])
    )

    provider.shutdown()
    assert outcome3["value"].close_threads == ["hindsight-loop"]


def test_retry_slot_release_and_outcome_recording_check_attempt_identity():
    """_ClientSetup frees dead-attempt slots and ignores stale reports.

    A Future terminal without a wrapper outcome frees the slot back to the
    retryable fail-closed state (client tracked, settled clear); a LATE
    callback or wrapper report from a dead attempt never clears a newer
    in-flight attempt; and only a recorded wrapper outcome for the LIVE
    attempt settles the generation.
    """
    _ClientSetup = hindsight_mod._ClientSetup

    setup = _ClientSetup()
    client = object()
    assert setup.abandon() is True
    assert setup.offer(client) is False
    setup.close_failed()
    assert setup.tracked_client() is client
    assert setup.wait_reconcilable(timeout=0.2) == "retry"

    attempts: list = []

    def _schedule(future):
        # Signature-agnostic: the attempt token is an implementation detail
        # of the fixed provider; what matters here is slot bookkeeping.
        def _do(*args):
            attempts.append(args[0] if args else None)
            return future

        return _do

    # Attempt A is cancelled before its wrapper records anything: its
    # done-callback frees the slot, restoring the retryable fail-closed
    # state with the exact client still tracked.
    cancelled = concurrent.futures.Future()
    assert setup.launch_retry(_schedule(cancelled))
    assert setup.retry_future() is cancelled
    cancelled.cancel()
    assert setup.retry_future() is None, (
        "a cancelled-before-start attempt kept the retry slot forever"
    )
    assert setup.wait_reconcilable(timeout=0.2) == "retry"
    assert setup.tracked_client() is client
    assert not setup.settled.is_set()
    # A stale wrapper report from the dead attempt settles nothing.
    setup.note_retry_outcome(attempts[0], True)
    assert not setup.settled.is_set()
    assert setup.tracked_client() is client

    # Attempt B takes the freed slot; a LATE done-callback from attempt
    # A's Future must not clear it, and neither does a stale report.
    live = concurrent.futures.Future()
    assert setup.launch_retry(_schedule(live))
    assert setup.retry_future() is live
    setup._release_dead_retry_slot(cancelled)
    assert setup.retry_future() is live
    setup.note_retry_outcome(attempts[0], False)
    assert setup.retry_future() is live

    # Attempt B's Future completes WITHOUT a wrapper outcome (loop torn
    # down mid-run): the slot is freed, but nothing is settled — no close
    # was confirmed.
    live.set_result(None)
    assert setup.retry_future() is None
    assert setup.wait_reconcilable(timeout=0.2) == "retry"
    assert setup.tracked_client() is client
    assert not setup.settled.is_set()

    # Attempt C's wrapper records a successful close for the LIVE attempt:
    # only now does the generation settle, with the client confirmed
    # released.
    done = concurrent.futures.Future()
    assert setup.launch_retry(_schedule(done))
    setup.note_retry_outcome(attempts[2], True)
    assert setup.settled.is_set()
    assert setup.tracked_client() is None
    assert setup.wait_reconcilable(timeout=0.2) == "settled"


# ---------------------------------------------------------------------------
# Regression: the owner-loop _get_client() must not build or install while a
# first-setup offer/claim/install handoff is still in flight.
#
# Fresh probe interleaving on the unfixed provider: a caller thread owns
# _client_lock and starts the first setup; the setup produces client A and
# its Future becomes ready, but the caller has NOT yet assigned self._client;
# the owner-loop branch sees _client is None and no abandoned generation,
# builds and installs client B; the caller resumes and unconditionally
# assigns A over B — B stays live, uninstalled, and unclosed. The handoff
# must stay observably in flight until the install lands, and the owner loop
# must fail closed promptly (no blocking lock/wait, no B construction).
# ---------------------------------------------------------------------------


def test_owner_loop_fails_closed_during_ready_but_uninstalled_handoff(
    tmp_path, monkeypatch
):
    """Future-ready-before-install: owner loop fails closed, builds nothing.

    Pauses the REAL caller path after setup client A is ready (its Future
    completed) but BEFORE installation, then invokes the REAL owner-loop
    _get_client(). The loop call must complete promptly with a fail-closed
    error, construct zero clients, and overwrite nothing; the loop stays
    responsive; the resumed caller then installs exactly one client (A).
    """
    from agent.async_utils import safe_schedule_threadsafe

    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    provider._timeout = 0.25
    _reset_gated_client(blocked=False)  # construction runs immediately

    a_ready = threading.Event()
    release_caller = threading.Event()
    real_await = provider._await_client_setup

    def _pause_before_install():
        # Inside _get_client under _client_lock: the setup Future has
        # completed with client A, but the caller has not installed it yet.
        client = real_await()
        a_ready.set()
        assert release_caller.wait(timeout=_GATE_WAIT_S), (
            "test harness never released the paused caller"
        )
        return client

    monkeypatch.setattr(provider, "_await_client_setup", _pause_before_install)

    caller = _ClientCallThread(provider).start()
    try:
        assert a_ready.wait(timeout=_GATE_WAIT_S), (
            "caller never reached the ready-but-uninstalled window"
        )
        client_a = _GatedHindsightClient.constructed[0]
        assert provider._client is None
        assert _GatedHindsightClient.constructed == [client_a]

        # The REAL owner-loop _get_client() during the handoff window.
        loop_outcome: dict = {}
        loop_done = threading.Event()

        async def _get_client_on_loop():
            try:
                loop_outcome["value"] = provider._get_client()
            except BaseException as exc:
                loop_outcome["error"] = exc
            finally:
                loop_done.set()

        probe = safe_schedule_threadsafe(
            _get_client_on_loop(), hindsight_mod._get_loop()
        )
        assert probe is not None, "could not schedule the owner-loop probe"
        # Prompt completion without waiting on the caller-held _client_lock.
        assert loop_done.wait(timeout=2.0), (
            "owner-loop _get_client() blocked during the install handoff"
        )
        probe.result(timeout=5.0)
        assert "value" not in loop_outcome, loop_outcome
        error = loop_outcome.get("error")
        assert type(error).__name__ == "RuntimeError", error
        assert "fail-closed" in str(error), error

        # No second client was built, nothing was installed over the window.
        assert _GatedHindsightClient.constructed == [client_a]
        assert provider._client is None

        # The owning loop stayed responsive throughout.
        async def _loop_probe():
            await asyncio.sleep(0)
            return "alive"

        responsiveness = safe_schedule_threadsafe(
            _loop_probe(), hindsight_mod._get_loop()
        )
        assert responsiveness is not None
        assert responsiveness.result(timeout=5.0) == "alive"
    finally:
        release_caller.set()

    # The resumed caller installs exactly the client its setup built.
    outcome = caller.join()
    assert "error" not in outcome, outcome.get("error")
    assert outcome["value"] is client_a
    assert provider._client is client_a
    assert _GatedHindsightClient.constructed == [client_a]

    # The installed client serves traffic from the owning loop.
    result = json.loads(provider.handle_tool_call(
        "hindsight_recall", {"query": "what does the user like?"}
    ))
    assert result == {"result": "1. memory one\n2. memory two"}

    provider.shutdown()
    assert client_a.close_threads == ["hindsight-loop"]


def test_owner_loop_build_losing_install_race_closes_on_owning_loop(
    tmp_path, monkeypatch
):
    """The inverse race: a loop-side build that loses the slot is closed.

    The loop branch's build can START before any handoff is registered (its
    pre-build checks pass while a caller thread is still on its way into the
    locked setup). The install must then be atomic against the caller's
    handoff: the caller's setup wins the slot, and the loop-built loser is
    closed ON the owner loop — never installed, never overwritten, never
    orphaned. The owner-loop call fails closed rather than handing back a
    client whose slot it lost.
    """
    from agent.async_utils import safe_schedule_threadsafe

    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    provider._timeout = 0.25
    _started, _gate, closed = _reset_gated_client(blocked=False)

    caller_parked = threading.Event()
    release_caller = threading.Event()
    real_await = provider._await_client_setup

    def _parked_before_setup_registration():
        # The caller holds _client_lock but has not registered (or even
        # started) its setup yet: the loop branch's pre-build checks all
        # pass in this window on every provider version.
        caller_parked.set()
        assert release_caller.wait(timeout=_GATE_WAIT_S), (
            "test harness never released the paused caller"
        )
        return real_await()

    monkeypatch.setattr(
        provider, "_await_client_setup", _parked_before_setup_registration
    )

    caller = _ClientCallThread(provider).start()
    try:
        assert caller_parked.wait(timeout=_GATE_WAIT_S), (
            "caller never reached the pre-setup window"
        )

        loop_outcome: dict = {}
        loop_done = threading.Event()

        async def _get_client_on_loop():
            try:
                loop_outcome["value"] = provider._get_client()
            except BaseException as exc:
                loop_outcome["error"] = exc
            finally:
                loop_done.set()

        probe = safe_schedule_threadsafe(
            _get_client_on_loop(), hindsight_mod._get_loop()
        )
        assert probe is not None, "could not schedule the owner-loop probe"
        assert loop_done.wait(timeout=2.0), (
            "owner-loop _get_client() blocked behind the caller's lock"
        )
        probe.result(timeout=5.0)
        # The loop-side build lost the slot to the caller's handoff: a
        # fail-closed error, not a client.
        assert "value" not in loop_outcome, loop_outcome
        error = loop_outcome.get("error")
        assert type(error).__name__ == "RuntimeError", error
        assert "fail-closed" in str(error), error

        # The losing client was constructed (its build legitimately started
        # before the handoff) but must already be on its way to a confirmed
        # close ON THE OWNER LOOP before the race can be declared over.
        assert len(_GatedHindsightClient.constructed) == 1
        loser = _GatedHindsightClient.constructed[0]
        assert closed.wait(timeout=_GATE_WAIT_S), (
            "the loop-built loser of the install race was never closed"
        )
        assert loser.close_threads == ["hindsight-loop"]
        assert provider._client is None
    finally:
        release_caller.set()

    # The caller's setup proceeds and installs exactly one live client.
    outcome = caller.join()
    assert "error" not in outcome, outcome.get("error")
    winner = outcome["value"]
    assert provider._client is winner
    assert _GatedHindsightClient.constructed == [loser, winner]
    assert winner.close_threads == []
    with _GatedHindsightClient._order_lock:
        events = list(_GatedHindsightClient.events)
    assert events.index(("closed", loser)) < events.index(
        ("constructed", winner)
    )

    provider.shutdown()
    assert winner.close_threads == ["hindsight-loop"]


# ---------------------------------------------------------------------------
# Regression: cancelling a reconciliation Future AFTER its close coroutine
# started must not free the retry slot before the coroutine's
# cancellation/finalization completes.
#
# Fresh probe on the unfixed provider: the reconciliation Future was
# cancelled after _aclose_client had started; the Future went terminal
# immediately and its done-callback freed the slot while the loop coroutine
# was still in cancellation/finalization — so a later caller launched a
# SECOND close for the same client, overlapping the first attempt.
# ---------------------------------------------------------------------------


def test_mid_close_cancellation_holds_slot_until_finalization(
    tmp_path, monkeypatch
):
    """A started close keeps its slot through cancellation cleanup.

    Drives a real reconciliation attempt whose _aclose_client signals it
    started, then cancels the real reconciliation Future while the
    coroutine's cancellation cleanup is parked behind a test gate. While
    parked: the slot stays held (the terminal Future is NOT proof the close
    stopped), a concurrent caller and shutdown() both fail bounded without
    scheduling another close, and the loop stays responsive. After
    finalization is released, the SAME generation becomes retryable and
    exactly one later close succeeds on the owning loop — and because
    shutdown() already began, no replacement may then be built at all: the
    reconciling caller still fails closed and nothing new is constructed.
    """
    provider, late, setup = _abandon_setup_with_failed_late_close(
        tmp_path, monkeypatch, fail_closes=1
    )

    close_started = threading.Event()
    cleanup_entered = threading.Event()
    cleanup_gate = threading.Event()
    aclose_calls: list = []
    real_aclose_client = provider._aclose_client
    calls = {"count": 0}

    def _parking_aclose(client):
        # Counted at SCHEDULE time (the provider wraps the returned
        # coroutine), so a merely-scheduled duplicate is visible.
        aclose_calls.append(client)
        calls["count"] += 1
        if calls["count"] > 1:
            return real_aclose_client(client)

        async def _parked():
            close_started.set()
            try:
                await asyncio.sleep(30)  # mid-close; only cancellation ends this
            finally:
                # Cancellation/finalization phase: parked behind a gate the
                # test holds, via the executor so the LOOP stays responsive.
                cleanup_entered.set()
                await asyncio.get_running_loop().run_in_executor(
                    None, cleanup_gate.wait, _GATE_WAIT_S
                )

        return _parked()

    monkeypatch.setattr(provider, "_aclose_client", _parking_aclose)

    second = _ClientCallThread(provider).start()
    assert close_started.wait(timeout=_GATE_WAIT_S), (
        "reconciliation close never started on the owning loop"
    )
    future = setup.retry_future()
    assert future is not None, "reconciliation attempt was never recorded"

    # Cancel AFTER the close coroutine started. The Future goes terminal
    # immediately; the loop-side cleanup keeps running behind the gate.
    future.cancel()
    assert future.cancelled()
    assert setup.retry_future() is future, (
        "a mid-close cancellation freed the retry slot while the close "
        "coroutine's cancellation cleanup was still in flight"
    )
    assert cleanup_entered.wait(timeout=_GATE_WAIT_S), (
        "cancellation cleanup never began on the owning loop"
    )

    # The cancelling caller still gets a bounded fail-closed error.
    outcome2 = second.join()
    assert "value" not in outcome2, outcome2
    assert type(outcome2["error"]).__name__ == "TimeoutError", outcome2["error"]

    # While cleanup is parked: the exact attempt stays in flight, the
    # client stays tracked, nothing settles, and no replacement is built.
    assert setup.tracked_client() is late
    assert not setup.settled.is_set()
    assert provider._abandoned_setup is setup
    assert provider._client is None
    assert _GatedHindsightClient.constructed == [late]

    # A concurrent caller must fail bounded WITHOUT scheduling another
    # close while the cancelled cleanup can still touch the client.
    third = _ClientCallThread(provider).start()
    outcome3 = third.join()
    assert "value" not in outcome3, outcome3
    assert type(outcome3["error"]).__name__ == "TimeoutError", outcome3["error"]
    assert len(aclose_calls) == 1, (
        "a second close was scheduled while the cancelled attempt's "
        "cleanup was still in flight"
    )
    assert late.close_attempts == ["hindsight-loop"]

    # shutdown() likewise: bounded, no overlapping close, generation kept.
    provider.shutdown()
    assert len(aclose_calls) == 1
    assert provider._abandoned_setup is setup
    assert setup.tracked_client() is late
    assert not setup.settled.is_set()

    # The owning loop stayed responsive through all of the above (the
    # parked cleanup must not block it).
    from agent.async_utils import safe_schedule_threadsafe

    async def _loop_probe():
        await asyncio.sleep(0)
        return "alive"

    probe = safe_schedule_threadsafe(_loop_probe(), hindsight_mod._get_loop())
    assert probe is not None
    assert probe.result(timeout=5.0) == "alive"

    # Release finalization: the cancelled attempt's wrapper records its
    # outcome (close NOT confirmed), freeing the SAME generation back to
    # the retryable fail-closed state.
    cleanup_gate.set()
    deadline = time.monotonic() + _GATE_WAIT_S
    while setup.retry_future() is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert setup.retry_future() is None, (
        "the cancelled attempt kept its slot after finalization completed"
    )
    assert setup.wait_reconcilable(timeout=1.0) == "retry"
    assert setup.tracked_client() is late
    assert not setup.settled.is_set()

    # Exactly one later close succeeds, on the owning loop, and the SAME
    # generation settles then — but shutdown() has already begun, so no
    # replacement may be built beside it: the reconciling caller drives the
    # confirmed close and still fails closed, constructing nothing.
    fourth = _ClientCallThread(provider).start()
    outcome4 = fourth.join()
    assert "value" not in outcome4, outcome4
    assert type(outcome4["error"]).__name__ == "RuntimeError", outcome4["error"]
    assert "fail-closed" in str(outcome4["error"]), outcome4["error"]
    assert len(aclose_calls) == 2
    assert late.close_attempts == ["hindsight-loop", "hindsight-loop"]
    assert late.close_threads == ["hindsight-loop"]
    assert setup.settled.is_set()
    assert setup.tracked_client() is None
    assert provider._abandoned_setup is None
    assert _GatedHindsightClient.constructed == [late]
    assert provider._client is None

    # A repeat shutdown stays bounded and closes nothing further.
    provider.shutdown()
    assert len(aclose_calls) == 2


# ---------------------------------------------------------------------------
# Regression: shutdown() must fence every first-client build/install path.
#
# Reproduced on the rejected change: a caller-thread first-client setup was
# parked mid-build; shutdown() set _shutting_down, observed self._client is
# None, ignored the in-flight (_active_setup) handoff, and returned; the
# parked caller's build then completed and installed its client AFTER
# shutdown — live, usable, and invisible to the close sweep that had
# already passed:
#
#     {'active_before_shutdown': True, 'shutdown_seconds': 0.0,
#      'caller_error': None, 'installed_after_shutdown': True,
#      'close_threads_after_shutdown': []}
#
# Once shutdown begins, no first-build path — caller-thread setup, the
# future-ready-but-uninstalled handoff window, or the owner-loop first
# build — may install or return a newly built client as usable; the fenced
# client must be closed exactly once on the owning loop, tracked
# fail-closed whenever that close cannot be confirmed; shutdown() and the
# fenced caller stay bounded; and the owning loop never blocks.
# ---------------------------------------------------------------------------


def test_shutdown_fences_parked_caller_setup_and_closes_late_client(
    tmp_path, monkeypatch
):
    """The reproduced blocker: parked first-client setup vs shutdown().

    The caller's first-client build is gated mid-construction on the owning
    loop. shutdown() must return promptly WITHOUT waiting for (or losing)
    the in-flight setup; when the build then completes, the caller must get
    an error instead of a usable client, provider._client must stay None,
    the late client must be closed exactly once on the owning loop, and no
    replacement may appear.
    """
    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    started, gate, closed = _reset_gated_client(blocked=True)

    caller = _ClientCallThread(provider).start()
    assert started.wait(timeout=_GATE_WAIT_S), "client build never started"

    # shutdown() while the setup is parked: bounded, and the slot stays
    # empty (nothing to sweep — the client does not exist yet).
    began = time.monotonic()
    provider.shutdown()
    shutdown_seconds = time.monotonic() - began
    assert shutdown_seconds < 5.0, (
        f"shutdown blocked {shutdown_seconds:.2f}s on a parked first-client "
        "setup instead of returning bounded"
    )
    assert provider._client is None

    # The parked build now completes — after shutdown began.
    gate.set()
    outcome = caller.join()
    assert "value" not in outcome, (
        "caller received a usable client from a setup that completed after "
        "shutdown began"
    )
    error = outcome["error"]
    assert type(error).__name__ == "RuntimeError", error
    assert "fail-closed" in str(error), error

    # Never installed; closed exactly once, on the owning loop.
    assert provider._client is None, "late client installed after shutdown"
    late = _GatedHindsightClient.constructed[0]
    assert closed.wait(timeout=_GATE_WAIT_S), (
        "client completed after shutdown began was never closed"
    )
    assert late.close_attempts == ["hindsight-loop"], (
        "the fenced client was not closed exactly once on the owning loop"
    )
    assert late.close_threads == ["hindsight-loop"]
    # The generation was driven to a confirmed close (settled and cleared),
    # and no replacement was built beside it.
    assert provider._abandoned_setup is None
    assert _GatedHindsightClient.constructed == [late]

    # The owning loop stayed responsive through the whole fenced handoff.
    from agent.async_utils import safe_schedule_threadsafe

    async def _loop_probe():
        await asyncio.sleep(0)
        return "alive"

    probe = safe_schedule_threadsafe(_loop_probe(), hindsight_mod._get_loop())
    assert probe is not None
    assert probe.result(timeout=5.0) == "alive"


def test_shutdown_during_ready_but_uninstalled_handoff_fences_install(
    tmp_path, monkeypatch
):
    """The future-ready-before-install window: shutdown lands between the
    setup Future completing and the caller installing the client.

    The setup coroutine finished and its Future is ready, but the caller
    has not assigned self._client yet. shutdown() runs in that window and
    its close sweep passes an empty slot. The resumed caller must refuse to
    publish, dispose of the client through the generation's bounded
    exactly-once owning-loop close, and fail closed — never install a
    client the sweep can no longer see.
    """
    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    provider._timeout = 0.25
    _reset_gated_client(blocked=False)  # construction runs immediately

    a_ready = threading.Event()
    release_caller = threading.Event()
    real_await = provider._await_client_setup

    def _pause_before_install():
        # Inside _get_client under _client_lock: the setup Future has
        # completed with client A, but the caller has not published it yet.
        client = real_await()
        a_ready.set()
        assert release_caller.wait(timeout=_GATE_WAIT_S), (
            "test harness never released the paused caller"
        )
        return client

    monkeypatch.setattr(provider, "_await_client_setup", _pause_before_install)

    caller = _ClientCallThread(provider).start()
    try:
        assert a_ready.wait(timeout=_GATE_WAIT_S), (
            "caller never reached the ready-but-uninstalled window"
        )
        client_a = _GatedHindsightClient.constructed[0]
        assert provider._client is None

        # The owning loop is idle in this window; it must stay responsive.
        from agent.async_utils import safe_schedule_threadsafe

        async def _loop_probe():
            await asyncio.sleep(0)
            return "alive"

        probe = safe_schedule_threadsafe(
            _loop_probe(), hindsight_mod._get_loop()
        )
        assert probe is not None
        assert probe.result(timeout=5.0) == "alive"

        # Shutdown sweeps the (empty) slot while the handoff is parked.
        began = time.monotonic()
        provider.shutdown()
        assert time.monotonic() - began < 5.0
        assert provider._client is None
    finally:
        release_caller.set()

    # The resumed caller must NOT publish the fenced client.
    outcome = caller.join()
    assert "value" not in outcome, (
        "caller installed/returned a client whose handoff lost to shutdown"
    )
    error = outcome["error"]
    assert type(error).__name__ == "RuntimeError", error
    assert "fail-closed" in str(error), error

    assert provider._client is None
    # The fenced client was released exactly once, on the owning loop,
    # through the generation's single-slot reconciliation.
    assert client_a.close_attempts == ["hindsight-loop"], (
        "the fenced handoff client was not closed exactly once"
    )
    assert client_a.close_threads == ["hindsight-loop"]
    assert provider._abandoned_setup is None
    assert _GatedHindsightClient.constructed == [client_a]


def test_shutdown_fences_owner_loop_first_build_and_closes_loser(
    tmp_path, monkeypatch
):
    """Owner-loop first build vs shutdown: no install, tracked release.

    A REAL owner-loop _get_client() build is gated mid-construction (the
    loop itself is parked inside the fake build). shutdown() must return
    bounded even while the owning loop is parked; once the build resumes
    and completes, the loop path must refuse the install, close the client
    exactly once on the owning loop with the outcome tracked on a
    generation, and fail closed — never publish into a slot the sweep has
    already passed.
    """
    from agent.async_utils import safe_schedule_threadsafe

    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    provider._timeout = 0.25
    started, gate, closed = _reset_gated_client(blocked=True)

    loop_outcome: dict = {}
    loop_done = threading.Event()

    async def _get_client_on_loop():
        # The REAL _get_client(), invoked on the owning loop thread.
        try:
            loop_outcome["value"] = provider._get_client()
        except BaseException as exc:
            loop_outcome["error"] = exc
        finally:
            loop_done.set()

    probe = safe_schedule_threadsafe(
        _get_client_on_loop(), hindsight_mod._get_loop()
    )
    assert probe is not None, "could not schedule the owner-loop probe"
    assert started.wait(timeout=_GATE_WAIT_S), "loop-side build never started"

    # The owning loop itself is parked inside the gated build: shutdown
    # must still return bounded (it may not depend on the loop thread).
    began = time.monotonic()
    provider.shutdown()
    assert time.monotonic() - began < 5.0, (
        "shutdown blocked on a parked owning-loop first build"
    )
    assert provider._client is None

    gate.set()
    assert loop_done.wait(timeout=_GATE_WAIT_S)
    probe.result(timeout=5.0)
    assert "value" not in loop_outcome, loop_outcome
    error = loop_outcome.get("error")
    assert type(error).__name__ == "RuntimeError", error
    assert "fail-closed" in str(error), error

    # Never installed; released exactly once on the owning loop, with the
    # outcome tracked on a generation that reaches a confirmed close.
    assert provider._client is None
    loser = _GatedHindsightClient.constructed[0]
    assert closed.wait(timeout=_GATE_WAIT_S), (
        "loop-built client fenced out by shutdown was never closed"
    )
    assert loser.close_attempts == ["hindsight-loop"], (
        "the fenced loop-built client was not closed exactly once"
    )
    assert loser.close_threads == ["hindsight-loop"]
    tracked = provider._abandoned_setup
    assert tracked is not None, "fenced loop build left no tracked generation"
    assert tracked.settled.is_set(), (
        "tracked generation never reached a confirmed close"
    )
    assert _GatedHindsightClient.constructed == [loser]

    # The owning loop is free again and responsive.
    async def _loop_probe():
        await asyncio.sleep(0)
        return "alive"

    responsiveness = safe_schedule_threadsafe(
        _loop_probe(), hindsight_mod._get_loop()
    )
    assert responsiveness is not None
    assert responsiveness.result(timeout=5.0) == "alive"


def test_first_client_setup_refused_after_shutdown(tmp_path, monkeypatch):
    """No first-client setup may START once shutdown has begun.

    A provider that has shut down must fail closed on the next first-use
    call without constructing anything — not build a client into a slot the
    close sweep has already passed.
    """
    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    _reset_gated_client(blocked=False)

    provider.shutdown()

    outcome = _ClientCallThread(provider).start().join()
    assert "value" not in outcome, (
        "a first client was built and returned after shutdown began"
    )
    error = outcome["error"]
    assert type(error).__name__ == "RuntimeError", error
    assert "fail-closed" in str(error), error
    assert _GatedHindsightClient.constructed == [], (
        "a client was constructed after shutdown began"
    )
    assert provider._client is None
    assert provider._abandoned_setup is None


def test_fenced_late_client_with_failed_close_stays_tracked_fail_closed(
    tmp_path, monkeypatch
):
    """A shutdown-fenced client whose loop-side close FAILS must stay
    tracked until a confirmed close, then settle exactly once.

    The setup coroutine hands its shutdown-fenced client to the generation
    and schedules the disposal close on the owning loop; that close RAISES
    (recorded on the generation). The caller is parked until AFTER that
    failure is recorded — deterministically separating the failed disposal
    from what must happen next — so its disposition has to drive the SAME
    generation's single-slot reconciliation, retrying the close exactly
    once on the owning loop, and only a CONFIRMED close may clear the
    record. The caller fails closed either way; nothing is ever installed.
    """
    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    provider._timeout = 0.25
    started, gate, closed = _reset_gated_client(blocked=True)
    # The coroutine's post-shutdown disposal close fails once; every later
    # attempt succeeds.
    _GatedHindsightClient.fail_closes = 1

    # Park the caller AFTER the setup returns: the generation already owns
    # the fenced client with its disposal attempt scheduled, but the
    # caller-side disposition has not run — the exact handoff point where
    # the recorded close failure must be reconciled, not dropped.
    setup_returned = threading.Event()
    release_caller = threading.Event()
    real_await = provider._await_client_setup

    def _pause_after_setup():
        client = real_await()
        setup_returned.set()
        assert release_caller.wait(timeout=_GATE_WAIT_S), (
            "test harness never released the paused caller"
        )
        return client

    monkeypatch.setattr(provider, "_await_client_setup", _pause_after_setup)

    caller = _ClientCallThread(provider).start()
    assert started.wait(timeout=_GATE_WAIT_S), "client build never started"

    provider.shutdown()
    assert provider._client is None

    gate.set()
    assert setup_returned.wait(timeout=_GATE_WAIT_S), (
        "the setup coroutine never completed after shutdown began"
    )
    setup = provider._active_setup
    assert setup is not None, "fenced setup generation was not observable"

    # The coroutine's disposal close ran exactly once, on the owning loop,
    # and FAILED: the attempt's report landed (hand_back already records
    # the owed close, so the reconcilable STATE is set from the start —
    # what must be awaited is the attempt finishing), the exact client
    # stays tracked with the failure recorded as retryable, and nothing
    # has settled.
    deadline = time.monotonic() + _GATE_WAIT_S
    while setup.retry_future() is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert setup.retry_future() is None, (
        "the failed disposal close never reported its outcome"
    )
    assert setup.wait_reconcilable(timeout=0.5) == "retry", (
        "the failed disposal close was not recorded as retryable on the "
        "tracked generation"
    )
    late = _GatedHindsightClient.constructed[0]
    assert late.close_attempts == ["hindsight-loop"], (
        "the loop-side disposal close did not run exactly once"
    )
    assert not setup.settled.is_set()
    assert setup.tracked_client() is late
    assert provider._abandoned_setup is None

    release_caller.set()
    outcome = caller.join()
    assert "value" not in outcome, outcome
    error = outcome["error"]
    assert type(error).__name__ == "RuntimeError", error
    assert "fail-closed" in str(error), error

    # Exactly two close attempts total — the failed disposal plus the
    # disposition's single reconciliation retry — both on the owning loop,
    # with exactly one confirmed close.
    assert late.close_attempts == ["hindsight-loop", "hindsight-loop"], (
        "expected exactly the failed loop-side disposal attempt plus one "
        "confirmed reconciliation retry"
    )
    assert late.close_threads == ["hindsight-loop"]
    assert closed.wait(timeout=_GATE_WAIT_S), "the retry close never confirmed"
    assert provider._client is None
    # The generation settled only via the confirmed retry close, and only
    # then was the record cleared.
    assert setup.settled.is_set()
    assert provider._abandoned_setup is None
    assert provider._active_setup is None
    assert _GatedHindsightClient.constructed == [late]


def test_install_client_shutdown_fence_uses_tracked_close_protocol(
    tmp_path, monkeypatch
):
    """_install_client must not use swallowing _close_client when shutdown
    fences publication — the loser stays on the generation's reconciliation
    protocol until a confirmed close."""
    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    _reset_gated_client(blocked=False)
    _GatedHindsightClient.fail_closes = 1

    loser = _GatedHindsightClient()
    assert loser is _GatedHindsightClient.constructed[0]

    close_client_calls = []

    def _fail_if_close_client(client):
        close_client_calls.append(client)
        raise AssertionError(
            "_close_client must not be used for shutdown-fenced publish"
        )

    monkeypatch.setattr(provider, "_close_client", _fail_if_close_client)

    provider._client = None
    provider._shutting_down.set()

    result = provider._install_client(loser)
    assert result is None
    assert provider._client is None
    setup = provider._abandoned_setup
    assert setup is not None
    assert setup.tracked_client() is loser
    assert not setup.settled.is_set()
    assert close_client_calls == []

    assert _GatedHindsightClient.close_failed.wait(timeout=_GATE_WAIT_S), (
        "the first tracked close never attempted"
    )
    assert loser.close_attempts == ["hindsight-loop"]
    assert not setup.settled.is_set()
    assert setup.tracked_client() is loser
    assert provider._abandoned_setup is setup

    settled = provider._reconcile_close_attempt(setup)
    assert settled
    assert setup.settled.is_set()
    assert loser.close_attempts == ["hindsight-loop", "hindsight-loop"]
    assert loser.close_threads == ["hindsight-loop"]
    assert _GatedHindsightClient.closed.wait(timeout=_GATE_WAIT_S)
    assert close_client_calls == []


def test_release_fenced_client_launch_retry_false_stays_fail_closed(
    tmp_path, monkeypatch
):
    """When launch_retry refuses, _release_fenced_client_on_owning_loop must
    not clear _abandoned_setup or start untracked _aclose_client."""
    from agent.async_utils import safe_schedule_threadsafe

    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    _reset_gated_client(blocked=False)
    client = _GatedHindsightClient()

    aclose_calls = 0
    real_aclose = provider._aclose_client

    async def _tracked_aclose(c):
        nonlocal aclose_calls
        aclose_calls += 1
        return await real_aclose(c)

    monkeypatch.setattr(provider, "_aclose_client", _tracked_aclose)

    real_launch_retry = hindsight_mod._ClientSetup.launch_retry

    def _launch_retry_false(self, schedule):
        return False

    monkeypatch.setattr(
        hindsight_mod._ClientSetup, "launch_retry", _launch_retry_false
    )

    async def _invoke_release():
        provider._release_fenced_client_on_owning_loop(client)

    release_future = safe_schedule_threadsafe(
        _invoke_release(), hindsight_mod._get_loop()
    )
    assert release_future is not None
    release_future.result(timeout=5.0)

    setup = provider._abandoned_setup
    assert setup is not None
    assert setup.tracked_client() is client
    assert not setup.settled.is_set()
    assert aclose_calls == 0

    monkeypatch.setattr(
        hindsight_mod._ClientSetup, "launch_retry", real_launch_retry
    )

    settled = _call_in_thread(provider._reconcile_close_attempt, setup)
    assert settled
    assert setup.settled.is_set()
    assert client.close_threads == ["hindsight-loop"]
    assert aclose_calls == 1


def _unresolved_generations(provider):
    """Collect every setup/generation the provider is still tracking."""
    found = []
    for name in (
        "_abandoned_setup",
        "_active_setup",
        "_extra_abandoned_setups",
        "_abandoned_setups",
        "_unresolved_setups",
        "_tracked_generations",
    ):
        val = getattr(provider, name, None)
        if val is None:
            continue
        items = val if isinstance(val, (list, tuple, set)) else [val]
        for item in items:
            if item is not None and item not in found:
                found.append(item)
    return found


def test_install_client_shutdown_fence_preserves_prior_unresolved_generation(
    tmp_path, monkeypatch
):
    """A second shutdown-fenced publish must not overwrite an occupied
    _abandoned_setup — both exact unresolved generations stay tracked."""
    from agent.async_utils import safe_schedule_threadsafe

    provider = _make_provider(tmp_path, monkeypatch, client_cls=_GatedHindsightClient)
    _reset_gated_client(blocked=False)
    _GatedHindsightClient.fail_closes = 2

    prior_client = _GatedHindsightClient()
    prior_setup = hindsight_mod._ClientSetup()
    prior_setup.hand_back(prior_client)
    provider._abandoned_setup = prior_setup
    prior_setup.launch_retry(
        lambda attempt: safe_schedule_threadsafe(
            provider._reconciliation_close_coro(prior_setup, prior_client, attempt),
            hindsight_mod._get_loop(),
        )
    )
    assert _GatedHindsightClient.close_failed.wait(timeout=_GATE_WAIT_S), (
        "prior generation's first close never attempted"
    )
    assert prior_client.close_attempts == ["hindsight-loop"]
    assert not prior_setup.settled.is_set()
    prior_close_count = len(prior_client.close_attempts)

    new_client = _GatedHindsightClient()
    close_client_calls = []

    def _fail_close_client(client):
        close_client_calls.append(client)
        raise AssertionError(
            "_close_client must not be used for shutdown-fenced publish"
        )

    monkeypatch.setattr(provider, "_close_client", _fail_close_client)

    provider._client = None
    provider._shutting_down.set()
    _GatedHindsightClient.close_failed.clear()
    result = provider._install_client(new_client)

    assert result is None
    assert provider._client is None
    assert provider._abandoned_setup is prior_setup
    assert prior_setup.tracked_client() is prior_client

    unresolved = _unresolved_generations(provider)
    assert len(unresolved) >= 2, unresolved
    setups_with_new = [
        s
        for s in unresolved
        if isinstance(s, hindsight_mod._ClientSetup)
        and s.tracked_client() is new_client
    ]
    assert len(setups_with_new) == 1, unresolved
    new_setup = setups_with_new[0]
    assert new_setup is not prior_setup

    assert _GatedHindsightClient.close_failed.wait(timeout=_GATE_WAIT_S), (
        "new generation's first close never attempted"
    )
    assert len(prior_client.close_attempts) == prior_close_count, (
        "prior client received an overlapping close"
    )
    assert new_client.close_attempts == ["hindsight-loop"]
    assert not prior_setup.settled.is_set()
    assert not new_setup.settled.is_set()
    assert prior_setup.tracked_client() is prior_client
    assert new_setup.tracked_client() is new_client
    assert close_client_calls == []


def test_recreate_client_fails_closed_when_stale_close_fails(
    tmp_path, monkeypatch
):
    """A stale-client retry must not build or publish a replacement until the
    exact stale generation is confirmed closed — close failure is fail-closed."""
    provider, stale, stale_inner = _stale_embedded_provider(tmp_path, monkeypatch)

    replacement_inner = _LoopBoundHindsightClient()
    replacement = _LoopBoundEmbeddedClient(replacement_inner)

    constructions = {"count": 0}

    def _counting_build():
        constructions["count"] += 1
        return replacement

    monkeypatch.setattr(provider, "_build_client", _counting_build)

    real_aclose = provider._aclose_client

    async def _fail_stale_close(client):
        if client is stale:
            return False
        return await real_aclose(client)

    monkeypatch.setattr(provider, "_aclose_client", _fail_stale_close)

    try:
        _call_in_thread(provider._recreate_client, stale)
    except RuntimeError:
        pass

    assert constructions["count"] == 0, (
        "a replacement client was constructed after stale close failed"
    )
    assert provider._client is not replacement
    assert provider._client is None
    assert stale in [
        s.tracked_client()
        for s in _unresolved_generations(provider)
        if isinstance(s, hindsight_mod._ClientSetup)
    ]


def _stale_is_live(stale, aclose_targets):
    """True while the stale wrapper or its inner client has not been released."""
    inner = stale._client
    return stale not in aclose_targets and inner is not None


def _stale_is_tracked(provider, stale):
    """True when stale is still published or owned by a tracked generation."""
    if provider._client is stale:
        return True
    for gen in _unresolved_generations(provider):
        if isinstance(gen, hindsight_mod._ClientSetup) and gen.tracked_client() is stale:
            return True
    return False


def test_recreate_client_does_not_overlap_shutdown_owned_close(
    tmp_path, monkeypatch
):
    """Recreate must not leave the stale client live and untracked across
    shutdown while _ClientSetup construction is parked before registration."""
    provider, stale, _stale_inner = _stale_embedded_provider(tmp_path, monkeypatch)

    aclose_targets = []
    aclose_threads = []

    real_aclose = provider._aclose_client

    async def _track_aclose(client):
        aclose_targets.append(client)
        aclose_threads.append(threading.current_thread().name)
        return await real_aclose(client)

    monkeypatch.setattr(provider, "_aclose_client", _track_aclose)

    entered = threading.Event()
    gate = threading.Event()
    real_setup_init = hindsight_mod._ClientSetup.__init__

    def _gated_setup_init(self, *args, **kwargs):
        entered.set()
        assert gate.wait(timeout=_GATE_WAIT_S), (
            "recreate _ClientSetup construction gate never released"
        )
        return real_setup_init(self, *args, **kwargs)

    monkeypatch.setattr(hindsight_mod._ClientSetup, "__init__", _gated_setup_init)

    recreate_outcome = {}

    def _run_recreate():
        try:
            recreate_outcome["value"] = provider._recreate_client(stale)
        except BaseException as exc:
            recreate_outcome["error"] = exc

    recreate_thread = threading.Thread(target=_run_recreate, name="recreate-worker")
    recreate_thread.start()

    assert entered.wait(timeout=_GATE_WAIT_S), (
        "recreate never parked in _ClientSetup construction"
    )

    provider.shutdown()

    assert not (
        _stale_is_live(stale, aclose_targets) and not _stale_is_tracked(provider, stale)
    ), (
        "shutdown returned while the stale client was still live and untracked "
        f"(aclose_targets={aclose_targets!r}, stale._client is not None="
        f"{stale._client is not None}, provider._client is stale="
        f"{provider._client is stale}, unresolved={_unresolved_generations(provider)!r})"
    )

    gate.set()
    recreate_thread.join(timeout=30)
    assert not recreate_thread.is_alive(), "recreate worker hung after gate release"

    assert "error" in recreate_outcome, recreate_outcome
    error = recreate_outcome["error"]
    assert type(error).__name__ == "RuntimeError", error
    assert "fail-closed" in str(error), error

    assert aclose_threads == ["hindsight-loop"], (
        f"expected exactly one owning-loop close of the stale client, got {aclose_threads!r}"
    )
    assert aclose_targets.count(stale) == 1, aclose_targets


def test_register_abandoned_setup_is_identity_idempotent(tmp_path, monkeypatch):
    """Registering the same _ClientSetup twice must not duplicate tracking."""
    provider = _make_provider(tmp_path, monkeypatch)
    first = hindsight_mod._ClientSetup()
    second = hindsight_mod._ClientSetup()

    provider._register_abandoned_setup(first)
    provider._register_abandoned_setup(first)
    assert provider._abandoned_setup is first
    assert provider._extra_abandoned_setups == []

    provider._register_abandoned_setup(second)
    provider._register_abandoned_setup(second)
    assert provider._abandoned_setup is first
    assert provider._extra_abandoned_setups == [second]

    provider._clear_abandoned_setup(first)
    assert provider._abandoned_setup is None
    assert provider._extra_abandoned_setups == [second]
    assert first not in provider._extra_abandoned_setups
