"""Session clarify lifecycle for /v1/runs agents (PR #189 review repair).

A run admitted through POST /v1/runs carries the gateway clarify
callback, so an agent that asks a question parks in
tools.clarify_gateway under the run's *canonical session id* instead of
being auto-answered by a headless default. These tests drive that whole
boundary with the real adapter and a mock agent whose
run_conversation invokes the assigned agent.clarify_callback — the same
call the ``clarify`` tool makes — and prove:

- the admission 202 names the canonical session id deterministically
  (explicit session ids run exactly that session; fresh runs echo the
  assigned id);
- the pending question surfaces on GET /api/sessions/{sid}/clarify for
  the exact session, and only for the profile that owns the run;
- an authenticated POST with the exact clarify_id resumes the *same*
  run (the parked callback returns the answer, the run completes) and
  clears every piece of pending state;
- stale, cross-session, cross-profile and lost-race answers all fail
  closed (409) without leaking whether the id ever existed;
- run completion and run stop both leave the registry empty.

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


def _create_clarify_app(adapter: APIServerAdapter) -> web.Application:
    """The runs surface plus the session clarify routes it feeds."""
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
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


def _clarifying_agent(question, choices=None, multi_select=False):
    """A mock agent whose turn parks in the clarify callback once.

    Returns (agent, answered) where answered is an event set once the
    callback returned; agent.captured_clarify holds the answer the
    parked worker received (set from the executor thread).
    """
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


async def _wait_run_status(cli, run_id, want, timeout=15.0, headers=None):
    """Poll GET /v1/runs/<run_id> until status is in want; returns it."""
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        resp = await cli.get("/v1/runs/%s" % run_id, headers=headers)
        assert resp.status == 200
        last = await resp.json()
        if last.get("status") in want:
            return last
        await asyncio.sleep(0.05)
    pytest.fail("run %s never reached %s (last: %r)" % (run_id, want, last))


async def _wait_for_card(cli, session_id, headers=None):
    """Poll the session clarify GET until a card appears; returns it."""
    deadline = asyncio.get_running_loop().time() + 15.0
    last = None
    while asyncio.get_running_loop().time() < deadline:
        resp = await cli.get("/api/sessions/%s/clarify" % session_id,
                             headers=headers)
        assert resp.status == 200
        last = (await resp.json()).get("pending_clarify")
        if last:
            return last
        await asyncio.sleep(0.05)
    pytest.fail("no pending clarify ever appeared (last: %r)" % (last,))


# ---------------------------------------------------------------------------
# Admission: the canonical session id is in the 202, deterministically
# ---------------------------------------------------------------------------


class TestAdmissionNamesSessionId:
    @pytest.mark.asyncio
    async def test_explicit_session_id_is_echoed_and_run_on(self, adapter):
        app = _create_clarify_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                agent, _answered = _clarifying_agent(
                    "Pick one?", choices=["json", "yaml"])
                mock_create.return_value = agent
                with patch.object(adapter, "_conversation_history_for_session",
                                  new=AsyncMock(return_value=[])):
                    resp = await cli.post(
                        "/v1/runs",
                        json={"input": "hi", "session_id": "mc_exact_sid_1"},
                    )
                assert resp.status == 202
                data = await resp.json()
                assert data["session_id"] == "mc_exact_sid_1"
                # the run's persisted status carries the same canonical id
                status = await _wait_run_status(
                    cli, data["run_id"], {"waiting_for_clarify"})
                assert status["session_id"] == "mc_exact_sid_1"

    @pytest.mark.asyncio
    async def test_fresh_run_gets_assigned_id_in_202(self, adapter):
        app = _create_clarify_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                agent, _answered = _clarifying_agent(
                    "Pick one?", choices=["json", "yaml"])
                mock_create.return_value = agent
                resp = await cli.post("/v1/runs", json={"input": "hi"})
                assert resp.status == 202
                data = await resp.json()
                # deterministic: the assigned id is a plain string the
                # client can navigate/poll immediately
                assert isinstance(data["session_id"], str)
                assert data["session_id"].strip()
                card = await _wait_for_card(cli, data["session_id"])
                assert card["question"] == "Pick one?"


# ---------------------------------------------------------------------------
# The card and the answer, end to end on the exact session
# ---------------------------------------------------------------------------


class TestClarifyLifecycle:
    @pytest.mark.asyncio
    async def test_card_answer_resumes_same_run_and_clears_state(self, adapter):
        app = _create_clarify_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                agent, answered = _clarifying_agent(
                    "Which format?", choices=["json", "yaml"])
                mock_create.return_value = agent
                with patch.object(adapter, "_conversation_history_for_session",
                                  new=AsyncMock(return_value=[])):
                    resp = await cli.post(
                        "/v1/runs",
                        json={"input": "hi", "session_id": "mc_life_1"})
                data = await resp.json()
                run_id = data["run_id"]

                # the card appears on the exact session, bounded and read-only
                card = await _wait_for_card(cli, "mc_life_1")
                assert card["clarify_id"]
                assert card["question"] == "Which format?"
                assert card["choices"] == ["json", "yaml"]
                assert card["multi_select"] is False
                # the run status mirrors the pause while the worker parks
                status = await cli.get("/v1/runs/%s" % run_id)
                assert (await status.json())["status"] == "waiting_for_clarify"

                # the authenticated answer resolves the exact id on the
                # exact session
                answer = await cli.post(
                    "/api/sessions/mc_life_1/clarify",
                    json={"clarify_id": card["clarify_id"],
                          "response": "json"})
                assert answer.status == 200
                body = await answer.json()
                assert body["resolved"] is True

                # the SAME run resumed with the answered text and completed
                assert answered.wait(timeout=15)
                assert agent.captured_clarify == "json"
                final = await _wait_run_status(cli, run_id, {"completed"})
                assert final["status"] == "completed"

                # every piece of pending state is gone
                cleared = await cli.get("/api/sessions/mc_life_1/clarify")
                assert (await cleared.json())["pending_clarify"] is None
                assert adapter._run_clarify_registrations == {}

    @pytest.mark.asyncio
    async def test_multi_select_list_answer_round_trips(self, adapter):
        app = _create_clarify_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                agent, answered = _clarifying_agent(
                    "Which checks?", choices=["lint", "types", "tests"],
                    multi_select=True)
                mock_create.return_value = agent
                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hi", "session_id": "mc_multi_1"})
                run_id = (await resp.json())["run_id"]
                card = await _wait_for_card(cli, "mc_multi_1")
                assert card["multi_select"] is True

                answer = await cli.post(
                    "/api/sessions/mc_multi_1/clarify",
                    json={"clarify_id": card["clarify_id"],
                          "response": ["lint", "tests"]})
                assert answer.status == 200
                assert answered.wait(timeout=15)
                # the multi-select answer reaches the parked worker as the
                # JSON array form the clarify tool decodes back to a list
                assert agent.captured_clarify == '["lint", "tests"]'
                await _wait_run_status(cli, run_id, {"completed"})

    @pytest.mark.asyncio
    async def test_list_answer_on_single_select_is_refused(self, adapter):
        app = _create_clarify_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                agent, answered = _clarifying_agent(
                    "Which format?", choices=["json", "yaml"])
                mock_create.return_value = agent
                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hi", "session_id": "mc_single_1"})
                run_id = (await resp.json())["run_id"]
                card = await _wait_for_card(cli, "mc_single_1")
                answer = await cli.post(
                    "/api/sessions/mc_single_1/clarify",
                    json={"clarify_id": card["clarify_id"],
                          "response": ["json", "yaml"]})
                assert answer.status == 400
                # still pending: the refusal changed nothing
                again = await cli.get("/api/sessions/mc_single_1/clarify")
                assert (await again.json())["pending_clarify"]["clarify_id"] == \
                    card["clarify_id"]
                # settle the run cleanly
                ok = await cli.post(
                    "/api/sessions/mc_single_1/clarify",
                    json={"clarify_id": card["clarify_id"],
                          "response": "json"})
                assert ok.status == 200
                assert answered.wait(timeout=15)
                await _wait_run_status(cli, run_id, {"completed"})

    @pytest.mark.asyncio
    async def test_free_text_question_answers_with_a_string(self, adapter):
        app = _create_clarify_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                agent, answered = _clarifying_agent("What timezone?")
                mock_create.return_value = agent
                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hi", "session_id": "mc_free_1"})
                run_id = (await resp.json())["run_id"]
                card = await _wait_for_card(cli, "mc_free_1")
                assert card["choices"] is None
                answer = await cli.post(
                    "/api/sessions/mc_free_1/clarify",
                    json={"clarify_id": card["clarify_id"],
                          "response": "UTC+10"})
                assert answer.status == 200
                assert answered.wait(timeout=15)
                assert agent.captured_clarify == "UTC+10"
                await _wait_run_status(cli, run_id, {"completed"})


# ---------------------------------------------------------------------------
# Fail-closed answers
# ---------------------------------------------------------------------------


class TestAnswersFailClosed:
    async def _parked_run_and_card(self, cli, adapter, session_id):
        """Admit one clarifying run on session_id; (run_id, card)."""
        with patch.object(adapter, "_create_agent") as mock_create:
            agent, _answered = _clarifying_agent(
                "Which format?", choices=["json", "yaml"])
            mock_create.return_value = agent
            with patch.object(adapter, "_conversation_history_for_session",
                              new=AsyncMock(return_value=[])):
                resp = await cli.post(
                    "/v1/runs", json={"input": "hi", "session_id": session_id})
            assert resp.status == 202
            run_id = (await resp.json())["run_id"]
            return run_id, await _wait_for_card(cli, session_id)

    @pytest.mark.asyncio
    async def test_cross_session_answer_is_refused(self, adapter):
        app = _create_clarify_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            _run_id, card = await self._parked_run_and_card(
                cli, adapter, "mc_owner_1")
            # the right clarify_id posted at ANOTHER session's route
            resp = await cli.post(
                "/api/sessions/mc_other_1/clarify",
                json={"clarify_id": card["clarify_id"], "response": "json"})
            assert resp.status == 409
            # the owner session keeps its pending question untouched
            still = await cli.get("/api/sessions/mc_owner_1/clarify")
            assert (await still.json())["pending_clarify"]["clarify_id"] == \
                card["clarify_id"]
            # settle the parked run
            ok = await cli.post(
                "/api/sessions/mc_owner_1/clarify",
                json={"clarify_id": card["clarify_id"], "response": "json"})
            assert ok.status == 200

    @pytest.mark.asyncio
    async def test_stale_and_unknown_ids_are_refused(self, adapter):
        app = _create_clarify_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            run_id, card = await self._parked_run_and_card(
                cli, adapter, "mc_stale_1")
            # answered once: the second answer with the same id is stale
            first = await cli.post(
                "/api/sessions/mc_stale_1/clarify",
                json={"clarify_id": card["clarify_id"], "response": "json"})
            assert first.status == 200
            second = await cli.post(
                "/api/sessions/mc_stale_1/clarify",
                json={"clarify_id": card["clarify_id"], "response": "yaml"})
            assert second.status == 409
            # never-registered ids fail identically (no oracle)
            ghost = await cli.post(
                "/api/sessions/mc_stale_1/clarify",
                json={"clarify_id": "nope123456", "response": "json"})
            assert ghost.status == 409
            # shape garbage is a 400, never a 500
            bad = await cli.post(
                "/api/sessions/mc_stale_1/clarify",
                json={"clarify_id": "", "response": "json"})
            assert bad.status == 400
            bad2 = await cli.post(
                "/api/sessions/mc_stale_1/clarify",
                json={"clarify_id": "x" * 500, "response": "json"})
            assert bad2.status == 400
            await _wait_run_status(cli, run_id, {"completed"})

    @pytest.mark.asyncio
    async def test_requires_auth_when_key_configured(self):
        adapter = _make_adapter(api_key="sk-clarify-secret")
        app = _create_clarify_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/api/sessions/any/clarify")
            assert resp.status == 401
            resp = await cli.post("/api/sessions/any/clarify",
                                  json={"clarify_id": "x", "response": "y"})
            assert resp.status == 401


# ---------------------------------------------------------------------------
# Profile isolation: only the owning profile sees and answers the card
# ---------------------------------------------------------------------------


class TestProfileIsolation:
    KEYS = {"owner": "sk-owner-profile-key-000001",
            "intruder": "sk-intruder-profile-key-0001"}

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

        app = _create_clarify_app(adapter)
        app.middlewares.append(stamp_profile)
        return adapter, app

    def _headers(self, profile):
        return {"X-Test-Profile": profile,
                "Authorization": "Bearer %s" % self.KEYS[profile]}

    @pytest.mark.asyncio
    async def test_card_is_invisible_to_other_profiles(self):
        adapter, app = self._profile_app()
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                agent, _answered = _clarifying_agent(
                    "Which format?", choices=["json", "yaml"])
                mock_create.return_value = agent
                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hi", "session_id": "mc_prof_1"},
                    headers=self._headers("owner"))
                assert resp.status == 202
                run_id = (await resp.json())["run_id"]

                owner_card = await _wait_for_card(
                    cli, "mc_prof_1", headers=self._headers("owner"))
                assert owner_card["clarify_id"]

                # the intruder profile sees nothing on the same session id
                intruder = await cli.get(
                    "/api/sessions/mc_prof_1/clarify",
                    headers=self._headers("intruder"))
                assert intruder.status == 200
                assert (await intruder.json())["pending_clarify"] is None

                # and cannot answer what it cannot see
                stolen = await cli.post(
                    "/api/sessions/mc_prof_1/clarify",
                    json={"clarify_id": owner_card["clarify_id"],
                          "response": "yaml"},
                    headers=self._headers("intruder"))
                assert stolen.status == 409

                # the owner's answer still resumes its own run
                ok = await cli.post(
                    "/api/sessions/mc_prof_1/clarify",
                    json={"clarify_id": owner_card["clarify_id"],
                          "response": "json"},
                    headers=self._headers("owner"))
                assert ok.status == 200
                await _wait_run_status(cli, run_id, {"completed"},
                                       headers=self._headers("owner"))


# ---------------------------------------------------------------------------
# Cleanup: completion and stop leave no pending state
# ---------------------------------------------------------------------------


class TestPendingStateCleanup:
    @pytest.mark.asyncio
    async def test_stop_releases_the_parked_clarify(self, adapter):
        app = _create_clarify_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                agent, answered = _clarifying_agent(
                    "Which format?", choices=["json", "yaml"])
                mock_create.return_value = agent
                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hi", "session_id": "mc_stop_1"})
                run_id = (await resp.json())["run_id"]
                card = await _wait_for_card(cli, "mc_stop_1")

                stop = await cli.post("/v1/runs/%s/stop" % run_id)
                assert stop.status == 200  # accepted, run is stopping

                # the parked worker was released (with the timeout
                # sentinel, never a real answer) and the run settled
                assert answered.wait(timeout=15)
                assert agent.captured_clarify != "json"
                assert agent.captured_clarify  # the sentinel is non-empty
                await _wait_run_status(
                    cli, run_id, {"cancelled", "interrupted", "completed"})

                # no pending entry, no registration, no card left behind
                gone = await cli.get("/api/sessions/mc_stop_1/clarify")
                assert (await gone.json())["pending_clarify"] is None
                assert adapter._run_clarify_registrations == {}
                with clarify_gateway._lock:
                    assert not any(
                        e.session_key == "mc_stop_1"
                        for e in clarify_gateway._entries.values())
                # a late answer for the stopped card fails closed
                late = await cli.post(
                    "/api/sessions/mc_stop_1/clarify",
                    json={"clarify_id": card["clarify_id"],
                          "response": "json"})
                assert late.status == 409

    @pytest.mark.asyncio
    async def test_answered_run_leaves_registry_empty(self, adapter):
        app = _create_clarify_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                agent, answered = _clarifying_agent(
                    "Which format?", choices=["json", "yaml"])
                mock_create.return_value = agent
                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hi", "session_id": "mc_done_1"})
                run_id = (await resp.json())["run_id"]
                card = await _wait_for_card(cli, "mc_done_1")
                ok = await cli.post(
                    "/api/sessions/mc_done_1/clarify",
                    json={"clarify_id": card["clarify_id"],
                          "response": "json"})
                assert ok.status == 200
                assert answered.wait(timeout=15)
                await _wait_run_status(cli, run_id, {"completed"})
                assert adapter._run_clarify_registrations == {}
                with clarify_gateway._lock:
                    assert clarify_gateway._entries == {}


# ---------------------------------------------------------------------------
# The unrelated -q CLI callback stays headless and registry-free
# ---------------------------------------------------------------------------


def test_single_query_clarify_callback_never_touches_the_gateway():
    """The CLI -q contract is unchanged by the API-run callback: it
    answers immediately and never parks anything in the process-wide
    registry (that registry now serves API-run sessions)."""
    from hermes_cli.cli_agent_setup_mixin import _single_query_clarify_callback

    with clarify_gateway._lock:
        before = set(clarify_gateway._entries)

    result = _single_query_clarify_callback(
        "Format?", choices=["json", "yaml"])

    assert result.startswith("[single-query mode: no user available")
    with clarify_gateway._lock:
        after = set(clarify_gateway._entries)
    assert before == after
