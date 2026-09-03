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

    real_close = provider._close_client

    def _close_while_b_installs(client):
        # Caller B's rebuild lands in the slot while A is still closing the
        # client that failed.
        provider._client = newer
        real_close(client)

    monkeypatch.setattr(provider, "_close_client", _close_while_b_installs)
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
