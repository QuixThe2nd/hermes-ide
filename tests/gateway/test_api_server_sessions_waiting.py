"""GET /api/sessions/waiting — the batch waiting-on-the-human boundary.

Mission Control's "Your turn" inbox needs one bounded, authenticated,
profile-scoped request that names every session of THIS profile parked on
an explicit wait: API-run clarifies, native gateway clarifies, and native
restart confirmations. These tests prove the endpoint's ownership rules
against the real adapter and the real process-wide clarify registry:

- the answer carries ONLY canonical session ids and a wait kind — never a
  question, choices, clarify_id, or run_id — and confers no ability to
  answer (the per-session clarify routes stay the only answer path);
- API-run waits come from the registration map JOINED to actual pending
  entries, so a resolved wait disappears immediately;
- native waits are visible only through their registration-time owner
  metadata, never guessed from the routing ``session_key``;
- a native wait stays invisible to the per-session clarify GET and cannot
  be consumed by its POST;
- one profile never sees another profile's waits, whatever their owner.

The process-wide clarify registry is drained after every test so one
test's parked worker can never bleed into the next.
"""

import asyncio
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    _api_request_profile,
    cors_middleware,
    security_headers_middleware,
)
from tools import clarify_gateway


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    extra = {}
    if api_key:
        extra["key"] = api_key
    return APIServerAdapter(PlatformConfig(enabled=True, extra=extra))


def _create_waiting_app(adapter: APIServerAdapter) -> web.Application:
    """The waiting route plus the runs and per-session clarify surfaces.

    ``/api/sessions/waiting`` is registered BEFORE any variable
    ``/api/sessions/{...}`` route, mirroring the adapter's route table —
    a variable session_id route registered first would swallow the static
    path wholesale.
    """
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_get("/api/sessions/waiting", adapter._handle_sessions_waiting)
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_post("/v1/runs/{run_id}/stop", adapter._handle_stop_run)
    app.router.add_get(
        "/api/sessions/{session_id}/clarify",
        adapter._handle_session_clarify_get,
    )
    app.router.add_post(
        "/api/sessions/{session_id}/clarify",
        adapter._handle_session_clarify_post,
    )
    return app


@pytest.fixture
def adapter():
    return _make_adapter()


@pytest.fixture(autouse=True)
def _drain_clarify_gateway():
    """No parked clarify outlives its test."""
    yield
    with clarify_gateway._lock:
        doomed = list(clarify_gateway._entries)
    for clarify_id in doomed:
        try:
            clarify_gateway.resolve_gateway_clarify(clarify_id, "")
        except Exception:
            pass
    with clarify_gateway._lock:
        clarify_gateway._entries.clear()
        clarify_gateway._session_index.clear()


def _clarifying_agent(question, choices=None, multi_select=False):
    """A mock agent whose turn parks in the clarify callback once."""
    agent = MagicMock()
    answered = threading.Event()
    agent.captured_clarify = None

    def _park(user_message=None, conversation_history=None, task_id=None):
        response = agent.clarify_callback(
            question, choices=choices, multi_select=multi_select
        )
        agent.captured_clarify = response
        answered.set()
        return {"final_response": "clarified:%s" % response}

    agent.run_conversation.side_effect = _park
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    return agent, answered


async def _get_waiting(cli, headers=None):
    resp = await cli.get("/api/sessions/waiting", headers=headers)
    assert resp.status == 200
    body = await resp.json()
    assert body["object"] == "hermes.sessions.waiting"
    return body["waiting"]


async def _wait_for_waiting(cli, predicate, timeout=15.0, headers=None):
    """Poll the waiting route until predicate(rows) holds; returns rows."""
    deadline = asyncio.get_running_loop().time() + timeout
    last = []
    while asyncio.get_running_loop().time() < deadline:
        last = await _get_waiting(cli, headers=headers)
        if predicate(last):
            return last
        await asyncio.sleep(0.05)
    pytest.fail("waiting rows never matched (last: %r)" % (last,))


# ---------------------------------------------------------------------------
# Auth and shape
# ---------------------------------------------------------------------------


class TestAuthAndShape:
    @pytest.mark.asyncio
    async def test_requires_auth_when_key_configured(self):
        adapter = _make_adapter(api_key="sk-waiting-secret")
        app = _create_waiting_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/api/sessions/waiting")
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_authenticated_empty_when_nothing_pending(self, adapter):
        app = _create_waiting_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            waiting = await _get_waiting(cli)
            assert waiting == []

    @pytest.mark.asyncio
    async def test_static_waiting_path_is_not_swallowed_by_session_routes(
        self, adapter
    ):
        """/api/sessions/waiting must reach the batch route, not be read
        as session_id="waiting" by a variable /api/sessions route."""
        app = _create_waiting_app(adapter)
        # A same-prefix variable route registered after the static one
        # must not shadow it (aiohttp resolves static paths first).
        async def _session_stub(request):
            return web.json_response(
                {"object": "hermes.session", "session_id": "waiting"})

        app.router.add_get("/api/sessions/{session_id}", _session_stub)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/api/sessions/waiting")
            assert resp.status == 200
            body = await resp.json()
            assert body["object"] == "hermes.sessions.waiting"


# ---------------------------------------------------------------------------
# API-run waits: registration joined to actual pending entries
# ---------------------------------------------------------------------------


class TestApiRunWaits:
    @pytest.mark.asyncio
    async def test_api_run_clarify_wait_appears_and_clears(self, adapter):
        app = _create_waiting_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                agent, answered = _clarifying_agent(
                    "Which format?", choices=["json", "yaml"])
                mock_create.return_value = agent
                with patch.object(adapter, "_conversation_history_for_session",
                                  new=AsyncMock(return_value=[])):
                    resp = await cli.post(
                        "/v1/runs",
                        json={"input": "hi", "session_id": "mc_wait_1"},
                    )
                assert resp.status == 202
                run_id = (await resp.json())["run_id"]

                # The parked run names its session with kind clarify —
                # ids and kind only, no prompt content in the payload.
                rows = await _wait_for_waiting(
                    cli,
                    lambda rows: any(
                        r["session_id"] == "mc_wait_1" for r in rows))
                row = next(r for r in rows if r["session_id"] == "mc_wait_1")
                assert row["kind"] == "clarify"
                assert set(row.keys()) == {"session_id", "kind"}

                # The card is answerable through the per-session route.
                card_resp = await cli.get("/api/sessions/mc_wait_1/clarify")
                card = (await card_resp.json())["pending_clarify"]
                assert card is not None
                ok = await cli.post(
                    "/api/sessions/mc_wait_1/clarify",
                    json={"clarify_id": card["clarify_id"],
                          "response": "json"})
                assert ok.status == 200

                # The moment the wait settles, the waiting evidence is
                # gone — even before the run itself finishes.
                rows = await _wait_for_waiting(
                    cli, lambda rows: not any(
                        r["session_id"] == "mc_wait_1" for r in rows))
                assert rows == []
                assert answered.wait(timeout=15)
                final = await cli.get("/v1/runs/%s" % run_id)
                assert (await final.json())["status"] == "completed"

    @pytest.mark.asyncio
    async def test_stopped_run_drops_its_wait(self, adapter):
        app = _create_waiting_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                agent, answered = _clarifying_agent(
                    "Which format?", choices=["json", "yaml"])
                mock_create.return_value = agent
                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hi", "session_id": "mc_wait_stop"},
                )
                run_id = (await resp.json())["run_id"]
                await _wait_for_waiting(
                    cli,
                    lambda rows: any(
                        r["session_id"] == "mc_wait_stop" for r in rows))

                stop = await cli.post("/v1/runs/%s/stop" % run_id)
                assert stop.status == 200
                assert answered.wait(timeout=15)
                rows = await _wait_for_waiting(
                    cli, lambda rows: not any(
                        r["session_id"] == "mc_wait_stop" for r in rows))
                assert rows == []


# ---------------------------------------------------------------------------
# Native waits: owner metadata only, never the routing key
# ---------------------------------------------------------------------------


class TestNativeWaits:
    @pytest.mark.asyncio
    async def test_native_owned_entry_appears_with_its_kind(self, adapter):
        app = _create_waiting_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            # What gateway/run.py and plugins/gateway_restart/tool.py do
            # in this same process when a native turn arms a wait.
            clarify_gateway.register(
                clarify_id="nat_restart",
                session_key="telegram:42",  # routing key, NOT the id
                question="Type restart to confirm",
                choices=None,
                owner_profile="default",
                owner_session_id="mc_native_restart",
                wait_kind="restart",
            )
            rows = await _wait_for_waiting(
                cli,
                lambda rows: any(
                    r["session_id"] == "mc_native_restart" for r in rows))
            assert rows == [
                {"session_id": "mc_native_restart", "kind": "restart"},
            ]

            # The per-session clarify GET stays empty for native waits:
            # the composer never sees a native prompt to answer.
            native_get = await cli.get(
                "/api/sessions/mc_native_restart/clarify")
            assert native_get.status == 200
            assert (await native_get.json())["pending_clarify"] is None

            # And the POST cannot consume it: no API-run registration
            # exists, so even the true clarify_id fails closed.
            stolen = await cli.post(
                "/api/sessions/mc_native_restart/clarify",
                json={"clarify_id": "nat_restart", "response": "restart"})
            assert stolen.status == 409
            # the native wait is untouched — still pending, still waiting
            assert clarify_gateway.get_pending_entry("nat_restart") is not None
            rows = await _get_waiting(cli)
            assert rows == [
                {"session_id": "mc_native_restart", "kind": "restart"},
            ]

    @pytest.mark.asyncio
    async def test_native_wait_without_owner_metadata_is_invisible(
        self, adapter
    ):
        """A native registration whose identity could not be resolved
        (both owner fields absent) must not be guessed onto a profile."""
        app = _create_waiting_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            clarify_gateway.register(
                clarify_id="nat_anon",
                session_key="discord:77",
                question="Q?",
                choices=["A"],
            )
            assert await _get_waiting(cli) == []

    @pytest.mark.asyncio
    async def test_resolved_native_wait_disappears_immediately(self, adapter):
        """The user answered through the native path (button/text): the
        signalled entry stops waiting before the waiter reaps it."""
        app = _create_waiting_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            clarify_gateway.register(
                clarify_id="nat_live",
                session_key="telegram:43",
                question="Q?",
                choices=["A"],
                owner_profile="default",
                owner_session_id="mc_native_live",
            )
            await _wait_for_waiting(
                cli,
                lambda rows: any(
                    r["session_id"] == "mc_native_live" for r in rows))

            assert clarify_gateway.resolve_gateway_clarify("nat_live", "A")
            # Entry still in the map until its waiter reaps — but the
            # answer is decided, so the waiting evidence is already gone.
            with clarify_gateway._lock:
                assert "nat_live" in clarify_gateway._entries
            rows = await _wait_for_waiting(
                cli, lambda rows: not any(
                    r["session_id"] == "mc_native_live" for r in rows))
            assert rows == []

    @pytest.mark.asyncio
    async def test_rotated_routing_key_keeps_the_registered_session(
        self, adapter
    ):
        """The routing key rotates to a new session: the old wait keeps
        the canonical id captured at register time, and the new wait
        reports its own — neither is re-attributed to the other."""
        app = _create_waiting_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            clarify_gateway.register(
                clarify_id="nat_before",
                session_key="sk-shares",
                question="Q?",
                choices=["A"],
                owner_profile="default",
                owner_session_id="mc_before_rotation",
            )
            # /new or a re-route: the SAME routing key now runs a
            # different canonical session, which arms its own wait.
            clarify_gateway.register(
                clarify_id="nat_after",
                session_key="sk-shares",
                question="Q2?",
                choices=["A"],
                owner_profile="default",
                owner_session_id="mc_after_rotation",
            )
            rows = await _wait_for_waiting(
                cli, lambda rows: len(rows) == 2)
            assert {r["session_id"] for r in rows} == {
                "mc_before_rotation", "mc_after_rotation"}
            assert all(r["kind"] == "clarify" for r in rows)


# ---------------------------------------------------------------------------
# Profile isolation: one profile never sees another profile's waits
# ---------------------------------------------------------------------------


class TestProfileIsolation:
    KEYS = {"owner": "sk-owner-waiting-key-00001",
            "intruder": "sk-intruder-waiting-key-01"}

    @staticmethod
    def _profile_app():
        adapter = _make_adapter()
        adapter._expected_api_key = lambda: TestProfileIsolation.KEYS.get(
            _api_request_profile.get(), "")

        @web.middleware
        async def stamp_profile(request, handler):
            token = _api_request_profile.set(
                request.headers.get("X-Test-Profile"))
            try:
                return await handler(request)
            finally:
                _api_request_profile.reset(token)

        app = _create_waiting_app(adapter)
        app.middlewares.append(stamp_profile)
        return adapter, app

    def _headers(self, profile):
        return {"X-Test-Profile": profile,
                "Authorization": "Bearer %s" % self.KEYS[profile]}

    @pytest.mark.asyncio
    async def test_api_run_wait_is_profile_scoped(self):
        adapter, app = self._profile_app()
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                agent, answered = _clarifying_agent(
                    "Which format?", choices=["json", "yaml"])
                mock_create.return_value = agent
                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hi", "session_id": "mc_prof_wait"},
                    headers=self._headers("owner"))
                assert resp.status == 202
                run_id = (await resp.json())["run_id"]

                await _wait_for_waiting(
                    cli,
                    lambda rows: any(
                        r["session_id"] == "mc_prof_wait" for r in rows),
                    headers=self._headers("owner"))
                # The intruder profile's list never names the owner's wait.
                intruder_rows = await _get_waiting(
                    cli, headers=self._headers("intruder"))
                assert intruder_rows == []

                # The intruder cannot answer it either (card invisible).
                ghost = await cli.get(
                    "/api/sessions/mc_prof_wait/clarify",
                    headers=self._headers("intruder"))
                assert ghost.status == 200
                assert (await ghost.json())["pending_clarify"] is None

                # Settle the owner's run.
                card = (await (
                    await cli.get("/api/sessions/mc_prof_wait/clarify",
                                  headers=self._headers("owner"))).json()
                )["pending_clarify"]
                ok = await cli.post(
                    "/api/sessions/mc_prof_wait/clarify",
                    json={"clarify_id": card["clarify_id"],
                          "response": "json"},
                    headers=self._headers("owner"))
                assert ok.status == 200
                assert answered.wait(timeout=15)
                final = await cli.get("/v1/runs/%s" % run_id,
                                      headers=self._headers("owner"))
                assert (await final.json())["status"] == "completed"

    @pytest.mark.asyncio
    async def test_native_wait_is_profile_scoped_by_owner_metadata(self):
        _adapter, app = self._profile_app()
        async with TestClient(TestServer(app)) as cli:
            clarify_gateway.register(
                clarify_id="nat_prof",
                session_key="telegram:91",
                question="Q?",
                choices=["A"],
                owner_profile="owner",
                owner_session_id="mc_native_prof",
            )
            owner_rows = await _wait_for_waiting(
                cli,
                lambda rows: any(
                    r["session_id"] == "mc_native_prof" for r in rows),
                headers=self._headers("owner"))
            assert owner_rows == [
                {"session_id": "mc_native_prof", "kind": "clarify"},
            ]
            intruder_rows = await _get_waiting(
                cli, headers=self._headers("intruder"))
            assert intruder_rows == []
