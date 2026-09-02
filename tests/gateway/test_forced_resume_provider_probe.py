"""Real local-provider probes for the forced-interruption recovery contract.

The TurnRunner-level integration proofs (``test_forced_resume_integration.py``)
stub the agent at the provider boundary; these probes drive one level deeper —
the REAL ``AIAgent.run_conversation`` against an in-process HTTP provider
(openai-compat transport) and a REAL ``SessionDB`` — to pin the two wire
invariants the reviewer asked for as durable evidence:

* the tool-tail continuation seam sends roles ``['system', 'user',
  'assistant', 'tool']`` with the EXACT call id pairing and zero synthetic
  recovery rows (the positive evidence the fix must preserve);
* the text-only interruption recovery — durable tail trimmed through the real
  ``trim_incomplete_assistant_text_tail`` + ``SessionDB.replace_messages``
  primitives, then continued through the ordinary ``continue_interrupted_turn``
  seam — yields a legal role sequence on BOTH sides of the boundary: what the
  provider receives and what the transcript persists.  The pre-fix behavior
  persisted ``['user', 'assistant', 'assistant']`` (REPAIR.md finding 7).

Unlike the integration file, these probes intentionally import the fixed
helpers: they are positive-evidence probes for the shipped behavior, not the
base-failing regressions (those live in the integration file).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.replay_cleanup import is_interrupted_tool_result
from gateway.config import GatewayConfig, Platform
from gateway.forced_resume_replay import (
    ReplayExecutionLedger,
    build_victim_replay_plan,
    claim_forced_recovery_ownership,
    execute_victim_replay,
    trim_incomplete_assistant_text_tail,
)
from gateway.session import SessionSource, SessionStore
from hermes_state import SessionDB

_INTERRUPTED_RESULT = '{"exit_code": 130, "output": "[Command interrupted]"}'


@pytest.fixture(autouse=True)
def _isolated_replay_reservations():
    """Keep the replay-execution fence out of neighboring tests.

    These probes exercise the helper/DB layer directly; the in-process
    fallback map is the only reservation state they can share with other
    tests, so that is all this clears."""
    import gateway.forced_resume_replay as _freplay

    with _freplay._FALLBACK_RESERVATION_LOCK:
        _freplay._FALLBACK_RESERVATIONS.clear()


class _MockHandler(BaseHTTPRequestHandler):
    """Records every chat-completions payload; replies from a queue."""

    captured_requests: list = []
    response_queue: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured_requests.append(req)
        is_stream = req.get("stream") is True
        resp = (
            type(self).response_queue.pop(0)
            if type(self).response_queue
            else _text_resp("DONE")
        )
        msg = resp["choices"][0]["message"]
        if is_stream:
            content = msg.get("content") or ""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {
                    "id": "m",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": ""},
                            "finish_reason": None,
                        }
                    ],
                }
            ]
            if content:
                chunks.append({
                    "id": "m",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": content},
                            "finish_reason": None,
                        }
                    ],
                })
            chunks.append({
                "id": "m",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            })
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


def _text_resp(text: str) -> dict:
    return {
        "id": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.fixture()
def provider_env():
    """In-process provider + isolated home + a shared SessionDB.

    Yields ``(make_agent, handler, db, sid)``; ``make_agent()`` builds a fresh
    AIAgent bound to the shared DB/session, modeling the gateway constructing
    the agent for the resume turn.
    """
    _MockHandler.captured_requests = []
    _MockHandler.response_queue = []
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    test_home = tempfile.mkdtemp(prefix="hermes_resume_probe_")
    os.makedirs(os.path.join(test_home, ".hermes"))
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")

    from run_agent import AIAgent

    db = SessionDB(db_path=Path(test_home) / "state.db")
    sid = "sess-resume-probe"

    def make_agent():
        agent = AIAgent(
            api_key="test-key",
            base_url=f"http://127.0.0.1:{port}/v1",
            provider="openai-compat",
            model="test-model",
            max_iterations=10,
            enabled_toolsets=[],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            save_trajectories=False,
            platform="cli",
            session_db=db,
            session_id=sid,
        )
        agent.valid_tool_names = {"read_file"}
        return agent

    try:
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            yield make_agent, _MockHandler, db, sid
    finally:
        srv.shutdown()
        db.close()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home
        # Drop the cached file_ops and active local-terminal environment so
        # the next test sees fresh state (same teardown the file-ops suites
        # use).  Without this, a later real ``read_file`` dispatch reuses the
        # env whose session-snapshot ``mktemp`` template lived under THIS
        # test's already-deleted home; the mktemp error then glues onto every
        # command's stdout and the stat parse silently yields file_size 0.
        try:
            from tools.file_tools import clear_file_ops_cache

            clear_file_ops_cache()
        except Exception:
            pass
        try:
            from tools.terminal_tool import _active_environments, _env_lock

            with _env_lock:
                _active_environments.clear()
        except Exception:
            pass


def _chat_requests(handler) -> list:
    # The context-length probe also hits the mock; keep chat payloads only.
    return [r for r in handler.captured_requests if "messages" in r]


def _assert_no_synthetic_recovery_rows(rows):
    for row in rows:
        text = str(row.get("content") or "")
        lowered = text.lower()
        assert "[system note:" not in lowered, row
        assert "was interrupted by" not in lowered, row
        assert "gateway restart" not in lowered, row


class TestToolTailContinuationSeam:
    def test_repaired_batch_sends_exact_pairing_with_no_synthetic_row(
        self, provider_env
    ):
        """Positive evidence (preserved by the fix): the repaired tool tail
        reaches the real provider as the ordinary "continue after tool
        results" request — ``['system', 'user', 'assistant', 'tool']``, the
        tool row answering the EXACT issuing call id, and nothing synthetic
        anywhere in the payload."""
        make_agent, handler, _db, _sid = provider_env
        agent = make_agent()
        repaired_history = [
            {"role": "user", "content": "deploy the service"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_deploy",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"file_path": "/nonexistent-path"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_deploy",
                "name": "read_file",
                "content": '{"error": "deployed cleanly"}',
            },
        ]
        handler.response_queue.append(_text_resp("Deploy finished cleanly."))

        result = agent.run_conversation(
            None,
            conversation_history=repaired_history,
            task_id="resume-tool-tail",
            continue_interrupted_turn=True,
        )

        reqs = _chat_requests(handler)
        assert len(reqs) >= 1
        messages = reqs[-1]["messages"]
        assert [m.get("role") for m in messages] == [
            "system",
            "user",
            "assistant",
            "tool",
        ]
        # The wire batch pairs on the exact original call id.
        wire_calls = messages[2].get("tool_calls") or []
        assert [c.get("id") for c in wire_calls] == ["call_deploy"]
        assert messages[3].get("tool_call_id") == "call_deploy"
        _assert_no_synthetic_recovery_rows(messages)

        assert result.get("completed") is True
        assert result.get("final_response") == "Deploy finished cleanly."


class TestTextOnlyInterruptionRoleSequence:
    def test_trimmed_continuation_sends_and_persists_legal_sequences(
        self, provider_env
    ):
        """Finding 7, at the real provider + real SessionDB boundary: after
        the durable tail is trimmed through the REAL trim helper and the REAL
        ``replace_messages(active_only=True)`` primitive (what
        ``SessionStore.rewrite_transcript`` delegates to), the continuation
        seam sends ``['system', 'user']`` and the transcript persists
        ``['user', 'assistant']`` — never the invalid
        ``user, assistant, assistant`` sequence strict providers reject."""
        make_agent, handler, db, sid = provider_env

        # The durable transcript a text-only forced interruption leaves
        # behind: the user row plus the incomplete assistant text tail.
        db.create_session(session_id=sid, source="cli")
        db.append_message(sid, "user", content="summarize the incident")
        db.append_message(sid, "assistant", content="Working on it…")

        # The gateway's durable trim: load → trim → rewrite (active_only).
        rows = db.get_messages_as_conversation(sid)
        assert [r.get("role") for r in rows] == ["user", "assistant"]
        trimmed, dropped = trim_incomplete_assistant_text_tail(rows)
        assert [r.get("role") for r in trimmed] == ["user"]
        assert [r.get("role") for r in dropped] == ["assistant"]
        db.replace_messages(sid, trimmed, active_only=True)

        # The resume turn reloads history from the store and continues
        # through the ordinary in-loop seam — no message, no note.
        history = db.get_messages_as_conversation(sid)
        assert [r.get("role") for r in history] == ["user"]
        agent = make_agent()
        handler.response_queue.append(_text_resp("The incident summary."))
        result = agent.run_conversation(
            None,
            conversation_history=history,
            task_id="resume-text-only",
            continue_interrupted_turn=True,
        )
        assert result.get("completed") is True

        # SENT: the request is an ordinary first response to the user row —
        # no leftover incomplete assistant tail, no synthetic replacement.
        reqs = _chat_requests(handler)
        assert reqs, "no request reached the provider"
        messages = reqs[-1]["messages"]
        assert [m.get("role") for m in messages] == ["system", "user"]
        _assert_no_synthetic_recovery_rows(messages)

        # PERSISTED: exactly one user row, then the fresh assistant row —
        # the trimmed tail never reappears as a second assistant row.
        persisted = db.get_messages_as_conversation(sid)
        roles = [r.get("role") for r in persisted]
        assert roles == ["user", "assistant"]
        assert persisted[0]["content"] == "summarize the incident"
        assert persisted[1]["content"] == "The incident summary."
        _assert_no_synthetic_recovery_rows(persisted)

    def test_stale_interrupted_tool_row_survives_trim_untouched(self, provider_env):
        """Negative control for the trim policy: a trailing tool RESULT row
        (interrupted marker included) is an assistant(tool_calls) concern —
        the text-tail trim must not touch it, so the replay path keeps its
        raw material (the marker stays detectable downstream)."""
        _make_agent, _handler, db, sid = provider_env
        db.create_session(session_id=sid, source="cli")
        db.append_message(sid, "user", content="deploy")
        db.append_message(
            sid,
            "assistant",
            content=None,
            tool_calls=[
                {
                    "id": "call_d",
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "arguments": '{"command": "make deploy"}',
                    },
                }
            ],
        )
        db.append_message(
            sid, "tool", content=_INTERRUPTED_RESULT, tool_call_id="call_d"
        )

        rows = db.get_messages_as_conversation(sid)
        trimmed, dropped = trim_incomplete_assistant_text_tail(rows)
        assert [r.get("role") for r in trimmed] == [
            "user",
            "assistant",
            "tool",
        ]
        assert dropped == []
        assert is_interrupted_tool_result(trimmed[-1].get("content"))


# ===========================================================================
# REPAIR 2 real-path probes: the real dispatcher, the real SessionStore
# persistence primitive, the durable cross-worker fences — one class per
# blocking finding.  Helpers in test_forced_resume_replay.py supplement
# these; they cannot replace them.
# ===========================================================================


def _make_real_store(db: SessionDB) -> SessionStore:
    """A real SessionStore whose transcript substrate is the probe's SessionDB."""
    store = SessionStore(
        sessions_dir=Path(os.environ["HERMES_HOME"]) / "sessions",
        config=GatewayConfig(),
    )
    if store._db is not None and store._db is not db:
        store._db.close()
    store._db = db
    return store


def _bind_session(store: SessionStore, db: SessionDB, sid: str) -> None:
    """Point a routing entry at *sid* so ``_db_for_session_id(sid)`` resolves."""
    source = SessionSource(
        platform=Platform.LOCAL, chat_id="probe-chat", user_id="probe-user"
    )
    entry = store.get_or_create_session(source)
    with store._lock:
        store._entries[entry.session_key].session_id = sid
    db.create_session(session_id=sid, source="cli")


class _RecordingDispatcher:
    """Minimal agent surface for ``execute_victim_replay`` with a REAL
    store/ledger — only the tool dispatch itself is a stub."""

    def __init__(self, result="OK"):
        self.executions: list = []
        self._result = result

    def _execute_tool_calls(self, assistant_message, messages, task_id, api_call_count):
        from agent.tool_dispatch_helpers import make_tool_result_message

        for call in assistant_message.tool_calls:
            self.executions.append((call.function.name, call.id))
            messages.append(
                make_tool_result_message(
                    call.function.name, self._result, call.id
                )
            )
        return "ok"


def _composite_victim_history(cid: str, data_file: Path) -> list:
    """Durable-transcript shape of the incident: an unanswered composite-id
    ``read_file`` call in the final assistant batch."""
    return [
        {"role": "user", "content": "read the config"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": cid,
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": str(data_file)}),
                    },
                }
            ],
        },
    ]


class TestRealDispatcherCompositeId:
    """REPAIR 2 finding 1: composite ids stay EXACT through the REAL
    dispatcher, the REAL SessionDB, and the provider wire — with no
    normalized alias row anywhere."""

    def test_composite_id_exact_through_real_dispatcher_db_and_wire(
        self, provider_env, tmp_path
    ):
        make_agent, handler, db, _sid = provider_env
        store = _make_real_store(db)
        sid = "sess-composite-real"
        _bind_session(store, db, sid)

        data_file = tmp_path / "payload.txt"
        data_file.write_text("REAL-DISPATCH-MARKER")
        cid = "call_alpha|item_beta"

        for row in _composite_victim_history(cid, data_file):
            db.append_message(
                sid,
                row["role"],
                content=row.get("content"),
                tool_calls=row.get("tool_calls"),
            )
        history = db.get_messages_as_conversation(sid)

        plan = build_victim_replay_plan(history)
        assert [c.call_id for c in plan.replay_calls] == [cid]

        agent = make_agent()
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            outcome = execute_victim_replay(
                agent,
                plan,
                raw_history=history,
                session_store=store,
                session_id=sid,
                effective_task_id=sid,
            )
        assert outcome.failure is None, outcome.failure
        assert outcome.replayed_call_ids == [cid]

        # DB: exactly ONE tool row, byte-for-byte the exact composite id —
        # the live dispatcher's normalized echo never landed as an alias.
        tool_rows = [
            r for r in db.get_messages_as_conversation(sid) if r.get("role") == "tool"
        ]
        assert [r["tool_call_id"] for r in tool_rows] == [cid]
        assert "REAL-DISPATCH-MARKER" in str(tool_rows[0]["content"])

        # Wire: the continuation pairs on the exact composite id too.
        handler.response_queue.append(_text_resp("Config read."))
        result = agent.run_conversation(
            None,
            conversation_history=db.get_messages_as_conversation(sid),
            task_id="resume-composite",
            continue_interrupted_turn=True,
        )
        assert result.get("completed") is True
        messages = _chat_requests(handler)[-1]["messages"]
        assert [m.get("role") for m in messages] == [
            "system",
            "user",
            "assistant",
            "tool",
        ]
        wire_calls = messages[2].get("tool_calls") or []
        assert [c.get("id") for c in wire_calls] == [cid]
        assert messages[3].get("tool_call_id") == cid
        _assert_no_synthetic_recovery_rows(messages)

    @pytest.mark.parametrize(
        "cid",
        [
            " call_pad ",          # padded plain id
            " call_c|half ",       # padded composite bridge id
        ],
    )
    def test_padded_ids_preserved_through_real_dispatcher(
        self, provider_env, tmp_path, cid
    ):
        """REPAIR 2 finding 8 at the real boundary: non-empty ids keep
        their leading/trailing bytes through the REAL dispatcher — the
        dispatcher's canonical (stripped/pipe-normalized) echo is
        re-stamped to the verbatim id for persistence and pairing."""
        make_agent, _handler, db, _sid = provider_env
        store = _make_real_store(db)
        sid = "sess-padded-real"
        _bind_session(store, db, sid)

        data_file = tmp_path / "padded.txt"
        data_file.write_text("PADDED-MARKER")

        for row in _composite_victim_history(cid, data_file):
            db.append_message(
                sid,
                row["role"],
                content=row.get("content"),
                tool_calls=row.get("tool_calls"),
            )
        history = db.get_messages_as_conversation(sid)
        plan = build_victim_replay_plan(history)
        assert [c.call_id for c in plan.replay_calls] == [cid]

        agent = make_agent()
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            outcome = execute_victim_replay(
                agent,
                plan,
                raw_history=history,
                session_store=store,
                session_id=sid,
                effective_task_id=sid,
            )
        assert outcome.failure is None, outcome.failure
        tool_rows = [
            r for r in db.get_messages_as_conversation(sid) if r.get("role") == "tool"
        ]
        assert [r["tool_call_id"] for r in tool_rows] == [cid]
        assert "PADDED-MARKER" in str(tool_rows[0]["content"])


class TestCheckedAppendRealStore:
    """REPAIR 2 finding 2: the synchronous checked append exposes a real
    bool — missing store, failed write, and read-back mismatch are all
    failures, never an inferred success."""

    def _row(self):
        return {
            "role": "tool",
            "name": "read_file",
            "tool_name": "read_file",
            "content": '{"ok": true}',
            "tool_call_id": "call_checked",
        }

    def test_success_returns_true_and_row_is_durable(self, provider_env):
        _make_agent, _handler, db, _sid = provider_env
        store = _make_real_store(db)
        sid = "sess-checked-ok"
        _bind_session(store, db, sid)

        assert store.append_to_transcript_checked(sid, self._row()) is True
        rows = db.get_messages_as_conversation(sid)
        assert rows and rows[-1].get("tool_call_id") == "call_checked"

    def test_missing_owning_db_returns_false(self, provider_env):
        _make_agent, _handler, db, _sid = provider_env
        store = SessionStore(
            sessions_dir=Path(os.environ["HERMES_HOME"]) / "sessions2",
            config=GatewayConfig(),
        )
        if store._db is not None:
            store._db.close()
        store._db = None  # no resolvable substrate for any session
        assert store.append_to_transcript_checked("sess-unknown", self._row()) is False

    def test_failed_db_write_returns_false_while_queue_append_stays_silent(
        self, provider_env, monkeypatch
    ):
        """The contrast the finding names: the legacy queue append
        swallows a DB failure, the checked append reports it."""
        _make_agent, _handler, db, _sid = provider_env
        store = _make_real_store(db)
        sid = "sess-checked-fail"
        _bind_session(store, db, sid)

        def _raise(*_a, **_kw):
            raise RuntimeError("simulated DB write failure")

        monkeypatch.setattr(db, "append_message", _raise)
        # Legacy path: silently queued, no exception, no signal.
        store.append_to_transcript(sid, self._row())
        # Checked path: a real failure disposition.
        assert store.append_to_transcript_checked(sid, self._row()) is False

    def test_readback_mismatch_returns_false(self, provider_env, monkeypatch):
        _make_agent, _handler, db, _sid = provider_env
        store = _make_real_store(db)
        sid = "sess-checked-mismatch"
        _bind_session(store, db, sid)

        def _wrong_tail(*_a, **_kw):
            return [{"role": "user", "content": "a different tail row"}]

        monkeypatch.setattr(db, "get_messages_as_conversation", _wrong_tail)
        assert store.append_to_transcript_checked(sid, self._row()) is False

    def test_silent_noop_over_stale_marker_is_not_acknowledgement(
        self, provider_env, monkeypatch
    ):
        """REPAIR3 finding 1 (verifier exit 41): with a pre-existing
        ``[Command interrupted]`` row under the same pairing id and a
        SILENT append no-op, the candidate ``DEPLOYED`` result must read
        back FALSE — the exact canonical row, not just role + call id, is
        what acknowledges a write.  The durable tail stays visibly stale,
        and a recovery whose persistence cannot be proven never reaches
        the model."""
        from agent.tool_dispatch_helpers import make_tool_result_message

        _make_agent, _handler, db, _sid = provider_env
        store = _make_real_store(db)
        sid = "sess-exit41"
        _bind_session(store, db, sid)

        db.append_message(sid, "user", content="deploy")
        db.append_message(
            sid,
            "assistant",
            content=None,
            tool_calls=[
                {
                    "id": "call_same",
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "arguments": '{"command": "make deploy"}',
                    },
                }
            ],
        )
        db.append_message(
            sid,
            "tool",
            content=_INTERRUPTED_RESULT,
            tool_call_id="call_same",
            tool_name="terminal",
        )

        def _noop(*_a, **_kw):
            return None  # silent no-op: nothing lands, nothing raises

        candidate = make_tool_result_message(
            "terminal",
            '{"exit_code": 0, "output": "DEPLOYED"}',
            "call_same",
            effect_disposition="success",
        )

        # The primitive's own write path: pairing id alone must never
        # acknowledge a DIFFERENT candidate.
        monkeypatch.setattr(db, "append_message", _noop)
        assert store.append_to_transcript_checked(sid, candidate) is False

        # And the whole recovery, on a store whose every write silently
        # no-ops, stays BLOCKED below the provider with the stale tail.
        history = db.get_messages_as_conversation(sid)
        plan = build_victim_replay_plan(history)
        assert [c.call_id for c in plan.replay_calls] == ["call_same"]
        monkeypatch.setattr(db, "supersede_tool_results", _noop)
        outcome = execute_victim_replay(
            _RecordingDispatcher('{"exit_code": 0, "output": "DEPLOYED"}'),
            plan,
            raw_history=history,
            session_store=store,
            session_id=sid,
            effective_task_id=sid,
        )
        assert outcome.repaired_history is None
        assert outcome.failure
        # The typed continuation gate is closed: zero model continuation,
        # recovery stays pending.
        assert outcome.ready_for_continuation is False

        monkeypatch.undo()
        tail = db.get_messages_as_conversation(sid)[-1]
        assert tail.get("role") == "tool"
        assert tail.get("tool_call_id") == "call_same"
        # The durable tail is still visibly the STALE interrupted marker,
        # not the candidate that was "acknowledged" pre-fix.
        assert "[Command interrupted]" in str(tail.get("content") or "")


class TestDurableReservationCrossWorker:
    """REPAIR 2 finding 3: the execution reservation is ONE atomic durable
    state transition in the session SQLite — independent handles (workers)
    cannot both claim, a corrupt record still fences, and only a verified
    release unfences."""

    def test_cross_handle_claim_held_corrupt_and_release(self, provider_env):
        _make_agent, _handler, db, _sid = provider_env
        db2 = SessionDB(db_path=db.db_path)
        try:
            key = "freplay:cross-handle"
            assert db.reserve_replay_execution(key, '{"ts": 1}') == ("claimed", None)
            # An independent handle (worker B) sees the reservation held.
            state, existing = db2.reserve_replay_execution(key, '{"ts": 2}')
            assert state == "held"
            assert existing == '{"ts": 1}'

            # A corrupt value record still fences — fail closed, never
            # reopen the side effect by guessing what it meant.
            db2.release_replay_reservation(key)
            db._execute_write(
                lambda conn: conn.execute(
                    "INSERT INTO state_meta (key, value) VALUES (?, ?)",
                    (key, "not-json-at-all{"),
                )
            )
            state, _existing = db.reserve_replay_execution(key, '{"ts": 3}')
            assert state == "held"

            # A verified release unfences: a later legitimate claim works.
            db.release_replay_reservation(key)
            assert db2.reserve_replay_execution(key, '{"ts": 4}') == ("claimed", None)
        finally:
            db2.close()

    def test_concurrent_claims_yield_exactly_one(self, provider_env):
        _make_agent, _handler, db, _sid = provider_env
        db2 = SessionDB(db_path=db.db_path)
        key = "freplay:concurrent"
        claims: list = []
        barrier = threading.Barrier(8)
        lock = threading.Lock()

        def _worker(handle, n):
            barrier.wait()
            state, _existing = handle.reserve_replay_execution(
                key, json.dumps({"ts": n})
            )
            with lock:
                claims.append(state)

        try:
            threads = [
                threading.Thread(
                    target=_worker, args=(db if n % 2 else db2, n), daemon=True
                )
                for n in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            assert claims.count("claimed") == 1
            assert claims.count("held") == 7
        finally:
            db2.close()

    def test_unprovable_persistence_fences_second_worker(
        self, provider_env, monkeypatch
    ):
        """Worker A executes but its durable persistence FAILS (the
        transactional supersede raises; no release); worker B — an
        independent store/handle over the same database — must not execute
        the side effect again: it stands down BLOCKED with zero rows."""
        _make_agent, _handler, db, _sid = provider_env
        store_a = _make_real_store(db)
        sid = "sess-fence"
        _bind_session(store_a, db, sid)

        history = [
            {"role": "user", "content": "deploy"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_fence",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": '{"command": "make deploy"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_fence",
                "content": _INTERRUPTED_RESULT,
            },
        ]

        def _raise(*_a, **_kw):
            raise RuntimeError("simulated DB write failure")

        # The replay persists through the ONE-transaction supersede
        # (archive stale marker + insert fresh row), so that is the seam a
        # durable write failure surfaces through.
        monkeypatch.setattr(db, "supersede_tool_results", _raise)
        worker_a = _RecordingDispatcher('{"exit_code": 0, "output": "deployed"}')
        outcome_a = execute_victim_replay(
            worker_a,
            build_victim_replay_plan(history),
            raw_history=history,
            session_store=store_a,
            session_id=sid,
        )
        # Side effect ran ONCE; nothing may claim repair.
        assert worker_a.executions == [("terminal", "call_fence")]
        assert outcome_a.repaired_history is None
        assert outcome_a.failure

        # Worker B: independent handle + store over the SAME database.
        monkeypatch.undo()
        db_b = SessionDB(db_path=db.db_path)
        store_b = _make_real_store(db_b)
        _bind_session(store_b, db_b, sid)
        try:
            worker_b = _RecordingDispatcher("SHOULD NOT RUN")
            outcome_b = execute_victim_replay(
                worker_b,
                build_victim_replay_plan(history),
                raw_history=history,
                session_store=store_b,
                session_id=sid,
            )
            assert worker_b.executions == []
            # The held reservation is a conflict another worker owns: B
            # stands down BLOCKED — no repaired history (no provider
            # continuation), no UNKNOWN row fabricated over the unresolved
            # side effect, recovery left pending.
            assert outcome_b.repaired_history is None
            assert outcome_b.failure and "reservation conflict" in outcome_b.failure
            assert outcome_b.ready_for_continuation is False
            rows = [
                r
                for r in db.get_messages_as_conversation(sid)
                if r.get("role") == "tool" and r.get("tool_call_id") == "call_fence"
            ]
            # No fabricated row landed for the unresolved call — neither
            # worker's write made it, and B added nothing on top.
            assert len(rows) == 0
        finally:
            db_b.close()


class TestRecoveryTailClaim:
    """REPAIR 2 finding 7 substrate: the recovery-ownership claim is a
    durable compare-and-swap on the session's exact tail identity."""

    def test_claim_already_claimed_superseded(self, provider_env):
        from hermes_state import forced_recovery_tail_digest

        _make_agent, _handler, db, _sid = provider_env
        sid = "sess-claim"
        db.create_session(session_id=sid, source="cli")
        db.append_message(sid, "user", content="summarize")
        db.append_message(sid, "assistant", content="Working on it…")
        tail_digest = forced_recovery_tail_digest(
            db.get_messages_as_conversation(sid)[-1]
        )

        assert db.claim_forced_recovery_tail(sid, tail_digest) == "claimed"
        assert db.claim_forced_recovery_tail(sid, tail_digest) == "already_claimed"

        # The durable tail moved (the winner rewrote it): a stale plan is
        # superseded, never executed against the moved transcript.
        db.append_message(sid, "assistant", content="The summary.")
        assert db.claim_forced_recovery_tail(sid, tail_digest) == "superseded"


# ===========================================================================
# REPAIR 3 real-path regressions: exactly one durable exact-ID result after a
# successful replay (verifier exit 44), and the two-worker text-tail race at
# independent SessionStore/SessionDB handles with a REAL provider-boundary
# continuation (fresh-review finding 7).
# ===========================================================================


class TestOneDurableExactIdResult:
    """REPAIR3 finding 2 (verifier exit 44): a successful explicit-interrupt
    replay must leave EXACTLY ONE active durable result row per exact call id
    — the fresh one.  Stale-marker archiving and fresh-row persistence are
    one durable transaction, so DB, model-facing history, and the provider
    wire all agree."""

    def test_successful_replay_leaves_exactly_one_active_durable_row(
        self, provider_env, tmp_path
    ):
        make_agent, handler, db, _sid = provider_env
        store = _make_real_store(db)
        sid = _sid  # make_agent() persists under the fixture session id
        _bind_session(store, db, sid)

        data_file = tmp_path / "exact-id.txt"
        data_file.write_text("EXACT-ID-MARKER")

        # The durable transcript an explicit interrupt leaves behind: the
        # user turn, the interrupted batch, and the STALE marker row.
        db.append_message(sid, "user", content="deploy the service")
        db.append_message(
            sid,
            "assistant",
            content=None,
            tool_calls=[
                {
                    "id": "call_one",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": str(data_file)}),
                    },
                }
            ],
        )
        db.append_message(
            sid,
            "tool",
            content=_INTERRUPTED_RESULT,
            tool_call_id="call_one",
            tool_name="read_file",
        )

        history = db.get_messages_as_conversation(sid)
        plan = build_victim_replay_plan(history)
        assert [c.call_id for c in plan.replay_calls] == ["call_one"]

        agent = make_agent()
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            outcome = execute_victim_replay(
                agent,
                plan,
                raw_history=history,
                session_store=store,
                session_id=sid,
                effective_task_id=sid,
            )
        assert outcome.failure is None, outcome.failure
        assert outcome.replayed_call_ids == ["call_one"]

        # DB: EXACTLY ONE active tool row for the exact id — the fresh
        # result.  The stale interrupted marker was archived in the SAME
        # transaction that landed it, never left alongside it.
        tool_rows = [
            r
            for r in db.get_messages_as_conversation(sid)
            if r.get("role") == "tool" and r.get("tool_call_id") == "call_one"
        ]
        assert len(tool_rows) == 1
        assert "EXACT-ID-MARKER" in str(tool_rows[0].get("content"))
        assert "[Command interrupted]" not in str(tool_rows[0].get("content"))

        # Provider wire: the continuation pairs on the exact id with exactly
        # one tool result — the fresh row — and persists one final assistant
        # row after it.
        handler.response_queue.append(_text_resp("Deploy finished cleanly."))
        result = agent.run_conversation(
            None,
            conversation_history=db.get_messages_as_conversation(sid),
            task_id="resume-exact-id",
            continue_interrupted_turn=True,
        )
        assert result.get("completed") is True

        reqs = _chat_requests(handler)
        assert len(reqs) == 1  # ONE provider call for the whole recovery
        messages = reqs[-1]["messages"]
        assert [m.get("role") for m in messages] == [
            "system",
            "user",
            "assistant",
            "tool",
        ]
        wire_calls = messages[2].get("tool_calls") or []
        assert [c.get("id") for c in wire_calls] == ["call_one"]
        wire_tool_rows = [
            m for m in messages if m.get("role") == "tool"
        ]
        assert [m.get("tool_call_id") for m in wire_tool_rows] == ["call_one"]
        assert "EXACT-ID-MARKER" in str(wire_tool_rows[0].get("content"))
        _assert_no_synthetic_recovery_rows(messages)

        # Persisted history: the batch answered once, then ONE final
        # assistant row — and still exactly one active exact-ID result.
        persisted = db.get_messages_as_conversation(sid)
        assert [r.get("role") for r in persisted] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        durable_tool_rows = [
            r
            for r in persisted
            if r.get("role") == "tool" and r.get("tool_call_id") == "call_one"
        ]
        assert len(durable_tool_rows) == 1
        assert persisted[-1].get("content") == "Deploy finished cleanly."
        _assert_no_synthetic_recovery_rows(persisted)


class TestTextTailRaceRealBoundaries:
    """REPAIR3 finding 7: two workers over the SAME exact text-only tail,
    on INDEPENDENT SessionStore/SessionDB handles, share one recovery —
    one provider call, one final durable assistant row — and the loser
    stands down at the durable ownership boundary with no forced-recovery
    prose or rows."""

    def test_two_workers_one_provider_call_one_durable_assistant_row(
        self, provider_env
    ):
        make_agent, handler, db, _sid = provider_env
        sid = _sid  # make_agent() persists under the fixture session id
        db.create_session(session_id=sid, source="cli")
        db.append_message(sid, "user", content="summarize the incident")
        db.append_message(sid, "assistant", content="Working on it…")

        # Both workers loaded the SAME tail before either ran.
        tail = db.get_messages_as_conversation(sid)
        assert [r.get("role") for r in tail] == ["user", "assistant"]

        # Worker A (independent handles over the same database file) wins
        # the durable ownership claim.
        db_a = SessionDB(db_path=db.db_path)
        store_a = _make_real_store(db_a)
        _bind_session(store_a, db_a, sid)
        assert claim_forced_recovery_ownership(store_a, sid, tail) == "claimed"

        # Worker B — a SECOND independent SessionStore + SessionDB handle
        # pair — attempts the same recovery and stands down at the claim:
        # no trim, no rewrite, no provider invocation.
        db_b = SessionDB(db_path=db.db_path)
        store_b = _make_real_store(db_b)
        _bind_session(store_b, db_b, sid)
        try:
            loser_claim = claim_forced_recovery_ownership(store_b, sid, tail)
            assert loser_claim in ("already_claimed", "superseded")
            # The loser made no provider call and touched no transcript
            # state: nothing has been sent or written yet.
            assert _chat_requests(handler) == []
            assert [
                r.get("role") for r in db.get_messages_as_conversation(sid)
            ] == ["user", "assistant"]

            # The winner runs the real recovery: durable trim through the
            # REAL helpers, then the ordinary continuation seam against the
            # REAL local provider.  (The agent is built BEFORE the reply is
            # queued: agent-init's context-length probe also hits the mock
            # and would otherwise consume the queued response.)
            trimmed, dropped = trim_incomplete_assistant_text_tail(tail)
            assert [r.get("role") for r in dropped] == ["assistant"]
            db.replace_messages(sid, trimmed, active_only=True)
            agent = make_agent()
            handler.response_queue.append(_text_resp("The incident summary."))
            result = agent.run_conversation(
                None,
                conversation_history=db.get_messages_as_conversation(sid),
                task_id="resume-tail-race",
                continue_interrupted_turn=True,
            )
            assert result.get("completed") is True

            # ONE provider call for the whole two-worker race.
            reqs = _chat_requests(handler)
            assert len(reqs) == 1
            messages = reqs[-1]["messages"]
            assert [m.get("role") for m in messages] == ["system", "user"]
            _assert_no_synthetic_recovery_rows(messages)

            # ONE final durable assistant row — the loser added nothing,
            # the winner's continuation persisted exactly once.
            persisted = db.get_messages_as_conversation(sid)
            assert [r.get("role") for r in persisted] == ["user", "assistant"]
            assistant_rows = [
                r for r in persisted if r.get("role") == "assistant"
            ]
            assert len(assistant_rows) == 1
            assert assistant_rows[0].get("content") == "The incident summary."
            _assert_no_synthetic_recovery_rows(persisted)

            # And the delivered answer carries no forced-recovery prose.
            assert result.get("final_response") == "The incident summary."
        finally:
            db_b.close()
            db_a.close()

    def test_corrupt_or_stale_claim_does_not_poison_recovery(self, provider_env):
        from hermes_state import forced_recovery_tail_digest

        _make_agent, _handler, db, _sid = provider_env
        sid = "sess-claim-poison"
        db.create_session(session_id=sid, source="cli")
        db.append_message(sid, "user", content="hello")
        digest = forced_recovery_tail_digest(
            db.get_messages_as_conversation(sid)[-1]
        )

        # Unparseable claim value: no provable age/owner — retake, do not
        # let a corrupt row fence recovery forever.
        db._execute_write(
            lambda conn: conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?)",
                (f"forced_recovery_claim:{sid}", "garbage{"),
            )
        )
        assert db.claim_forced_recovery_tail(sid, digest) == "claimed"

        # An abandoned (crashed-owner) claim older than the TTL retakes.
        db._execute_write(
            lambda conn: conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (
                    f"forced_recovery_claim:{sid}",
                    json.dumps({"digest": digest, "ts": 1.0}),
                ),
            )
        )
        assert db.claim_forced_recovery_tail(sid, digest) == "claimed"
