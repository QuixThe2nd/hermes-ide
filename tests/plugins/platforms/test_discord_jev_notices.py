"""Jev classification notices for the Discord response and reaction gates, through the real owning callers.

Every real judge request (``_response_gate_decide`` / ``_reaction_gate_run``)
posts one visible notice line into the originating conversation — allow, deny,
react, abstain and failure alike. No-call paths (credential missing, bypass,
echo channels, gate off) post nothing. A notice send failure is logged and
never changes a decision, and a notice never re-enters the gates (no
recursion, no evidence pollution). Only the two judge HTTP sockets are
stubbed; dispatch, admission, runtimes and the notice path are the real
production paths.
"""

import asyncio
import logging
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

discord = pytest.importorskip("discord")

from gateway.jev_notice import go_formatter_binary  # noqa: E402

pytestmark = pytest.mark.skipif(
    go_formatter_binary() is None,
    reason="jev notices are rendered by the Go formatter binary; no cached "
           "binary and no Go toolchain to build one",
)

from gateway.config import PlatformConfig, ReactionGateConfig, ResponseGateConfig  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402
from plugins.platforms.discord.reaction_gate import JevChoiceClient  # noqa: E402
from plugins.platforms.discord.response_gate import JevDecisionClient, ResponseGateError  # noqa: E402

GATE_ENV_VARS = [
    "DISCORD_ALLOWED_CHANNELS",
    "DISCORD_IGNORED_CHANNELS",
    "DISCORD_FREE_RESPONSE_CHANNELS",
    "DISCORD_ALLOWED_USERS",
    "DISCORD_ALLOW_ALL_USERS",
    "DISCORD_REQUIRE_MENTION",
    "DISCORD_IGNORE_NO_MENTION",
    "DISCORD_ALLOW_BOTS",
]

BOT_NAME = "Hermes"
BOT_ID = 999
HUMAN_ID = 7


@pytest.fixture(autouse=True)
def _clean_gate_env(monkeypatch):
    for var in GATE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield
    for var in GATE_ENV_VARS:
        os.environ.pop(var, None)


class _Channel:
    def __init__(self, channel_id, name="general", guild_id=1):
        self.id = channel_id
        self.name = name
        self.guild = SimpleNamespace(name="Test Server", id=guild_id)
        self.parent = None
        self.parent_id = None


def _message(
    *, channel=None, msg_id=42, content="nice one!", author=None, mentions=None,
    reference=None, msg_type=None, add_reaction=None,
):
    channel = channel if channel is not None else _Channel(555)
    return SimpleNamespace(
        id=msg_id,
        content=content,
        mentions=list(mentions or []),
        attachments=[],
        reference=reference,
        channel=channel,
        author=author or SimpleNamespace(
            id=HUMAN_ID, name="alice", display_name="Alice", bot=False,
        ),
        guild=channel.guild,
        type=msg_type if msg_type is not None else discord.MessageType.default,
        add_reaction=add_reaction if add_reaction is not None else AsyncMock(),
    )


def _adapter(
    *, reaction=None, response=None, credential="test-credential",
) -> DiscordAdapter:
    config = PlatformConfig(enabled=True, token="x")
    if reaction is not None:
        config.reaction_gate = ReactionGateConfig.from_dict(reaction)
    if response is not None:
        config.response_gate = ResponseGateConfig.from_dict(response)
    adapter = DiscordAdapter(config)
    adapter._response_gate_credential = credential
    adapter._response_gate_init()
    adapter._reaction_gate_init()
    adapter._client = SimpleNamespace(
        user=SimpleNamespace(id=BOT_ID, name="hermes", display_name=BOT_NAME, bot=True),
        close=AsyncMock(),
    )
    adapter._ready_event.set()
    adapter._handle_message = AsyncMock(return_value=True)
    adapter.send = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="1")
    )
    adapter._is_pairing_approved_user = lambda uid, **_: False
    adapter._allowed_user_ids = {str(HUMAN_ID)}
    adapter._text_batch_delay_seconds = 0
    return adapter


async def _drain(adapter) -> list:
    tasks = list(adapter._reaction_gate_tasks)
    if tasks:
        await asyncio.gather(*tasks)
    return tasks


def _notices(adapter) -> list:
    """The jev-notice sends only (each is (chat_id, line, kwargs))."""
    out = []
    for call in adapter.send.await_args_list:
        metadata = call.kwargs.get("metadata") or {}
        if metadata.get("jev_notice") is True:
            out.append((call.args[0], call.args[1], call.kwargs))
    return out


def _notice_lines(adapter) -> list:
    return [entry[1] for entry in _notices(adapter)]


class _DecisionRecorder:
    """Socket-level stub for JevDecisionClient._request (the speaking gate)."""

    def __init__(self, scores=None, error=None):
        self.calls = 0
        self.scores = scores or {
            "addresses_bot": 0.91, "continues_bot_thread": 0.12,
            "joins_bot_thread": 0.03, "noise": 0.05,
        }
        self.error = error

    async def __call__(self, state, *, chat_id=None, questions=None):
        self.calls += 1
        if self.error:
            raise ResponseGateError(self.error, "stub")
        return {"answers": {
            key: {"type": "noul", "noul": value} for key, value in self.scores.items()
        }}


class _ChoiceRecorder:
    """Socket-level stub for JevChoiceClient._request (the reaction gate)."""

    def __init__(self, probabilities=None, error=None):
        self.calls = 0
        self.probabilities = probabilities
        self.error = error

    async def __call__(self, state, *, chat_id=None, questions=None):
        self.calls += 1
        if self.error:
            raise ResponseGateError(self.error, "stub")
        options = list((questions or {}).get("reaction", {}).get("criteria", {}))
        if self.probabilities is not None:
            return {"answers": {"reaction": {
                "type": "choice", "probabilities": dict(self.probabilities),
            }}}
        share = 1.0 / len(options)
        return {"answers": {"reaction": {
            "type": "choice", "probabilities": {option: share for option in options},
        }}}


def _stub_speaking(monkeypatch, recorder):
    monkeypatch.setattr(JevDecisionClient, "_request", recorder)


def _stub_choice(monkeypatch, recorder):
    monkeypatch.setattr(JevChoiceClient, "_request", recorder)


REACT_FIRE = {
    "👍": 0.00, "❤️": 0.00, "😂": 0.00, "🎉": 0.00, "😢": 0.00, "😮": 0.00,
    "🔥": 0.80, "🤔": 0.00, "None": 0.10, "Other": 0.10,
}


class TestResponseGateNotices:
    @pytest.mark.asyncio
    async def test_admitted_candidate_posts_allow_notice(self, monkeypatch):
        recorder = _DecisionRecorder()
        _stub_speaking(monkeypatch, recorder)
        adapter = _adapter(response={"channels": ["555"], "mode": "enforce"})
        ambient = _message(content="ambient chatter, no mention")

        # Drive the real dispatch seam (the owning caller of the gate).
        dispatched = await adapter._dispatch_discord_message(ambient)

        assert dispatched is True

        assert recorder.calls == 1
        lines = _notice_lines(adapter)
        assert lines == [
            "🔎 jev response-gate user message from Alice: allow "
            "(addresses_bot=0.91 continues_bot_thread=0.12 joins_bot_thread=0.03 noise=0.05)"
        ]
        notice = _notices(adapter)[0]
        assert notice[0] == "555"
        # Unpinged plain notice: never a Discord reply (a reply would mention
        # the user). Correlation is the channel + the author in the line.
        assert "reply_to" not in notice[2]

    @pytest.mark.asyncio
    async def test_denied_candidate_posts_deny_notice(self, monkeypatch):
        recorder = _DecisionRecorder(scores={
            "addresses_bot": 0.09, "continues_bot_thread": 0.88,
            "joins_bot_thread": 0.02, "noise": 0.75,
        })
        _stub_speaking(monkeypatch, recorder)
        adapter = _adapter(response={"channels": ["555"], "mode": "enforce"})
        ambient = _message(content="ambient chatter, no mention")

        await adapter._dispatch_discord_message(ambient)

        assert recorder.calls == 1
        assert _notice_lines(adapter) == [
            "🔎 jev response-gate user message from Alice: deny "
            "(addresses_bot=0.09 continues_bot_thread=0.88 joins_bot_thread=0.02 noise=0.75)"
        ]

    @pytest.mark.asyncio
    async def test_judge_failure_posts_failed_notice_and_denies(self, monkeypatch):
        recorder = _DecisionRecorder(error="http_502")
        _stub_speaking(monkeypatch, recorder)
        adapter = _adapter(response={"channels": ["555"], "mode": "enforce"})
        ambient = _message(content="ambient chatter")

        dispatched = await adapter._dispatch_discord_message(ambient)

        assert dispatched is False  # fail-closed deny unchanged by the notice path
        assert _notice_lines(adapter) == [
            "🔎 jev response-gate user message from Alice: failed (http_502)"
        ]

    @pytest.mark.asyncio
    async def test_missing_credential_posts_nothing(self, monkeypatch):
        recorder = _DecisionRecorder()
        _stub_speaking(monkeypatch, recorder)
        adapter = _adapter(
            response={"channels": ["555"], "mode": "enforce"}, credential=None,
        )
        ambient = _message(content="ambient chatter")

        await adapter._dispatch_discord_message(ambient)

        assert recorder.calls == 0  # no credential: the judge is never called
        assert _notices(adapter) == []

    @pytest.mark.asyncio
    async def test_bypass_message_consults_nothing_posts_nothing(self, monkeypatch):
        recorder = _DecisionRecorder()
        _stub_speaking(monkeypatch, recorder)
        adapter = _adapter(response={"channels": ["555"], "mode": "enforce"})
        # An explicit mention is an authorizing trigger: the legacy policy admits
        # it and the judge is not consulted — no request, no notice.
        mentioned = _message(
            content=f"<@{BOT_ID}> what's up", mentions=[SimpleNamespace(id=BOT_ID, bot=False)],
        )

        await adapter._dispatch_discord_message(mentioned)

        assert recorder.calls == 0
        assert _notices(adapter) == []

    @pytest.mark.asyncio
    async def test_echo_channel_posts_the_go_notice_line_unpinged(self, monkeypatch):
        """Echo channels post the SAME Go-formatted notice every lane posts.

        The old echo lane rendered a local Python line and sent it as a
        Discord reply (a ping); it now goes through the single formatter
        owner and the unpinged non-conversational notice path. Contract test
        corrected: the prior version exempted the echo lane from the
        all-consumers formatter contract, which the acceptance review
        rejected (two production formatting owners).
        """
        recorder = _DecisionRecorder()
        _stub_speaking(monkeypatch, recorder)
        adapter = _adapter(response={
            "channels": ["555"], "echo_channels": ["555"], "mode": "enforce",
        })
        ambient = _message(content="ambient chatter")

        await adapter._dispatch_discord_message(ambient)

        assert recorder.calls == 1
        notices = _notices(adapter)
        assert len(notices) == 1  # the echo IS the notice line there
        assert notices[0][0] == "555"
        assert notices[0][1].startswith("🔎 jev response-gate user message from Alice: ")
        assert "allow" in notices[0][1]
        # Unpinged plain notice: never a Discord reply.
        assert "reply_to" not in notices[0][2]
        assert notices[0][2]["metadata"]["non_conversational_history"] is True

    @pytest.mark.asyncio
    async def test_echo_channel_missing_credential_posts_nothing(self, monkeypatch):
        """No HTTP request was made, so no echo diagnostic may claim one."""
        recorder = _DecisionRecorder()
        _stub_speaking(monkeypatch, recorder)
        adapter = _adapter(
            response={
                "channels": ["555"], "echo_channels": ["555"], "mode": "enforce",
            },
            credential=None,
        )
        ambient = _message(content="ambient chatter")

        dispatched = await adapter._dispatch_discord_message(ambient)

        assert recorder.calls == 0
        # Not even a "gate error (missing_credential)" line: that outcome
        # never touched the judge, and a notice may only report real requests.
        assert adapter.send.await_args_list == []

    @pytest.mark.asyncio
    async def test_notice_send_failure_never_changes_the_decision(self, monkeypatch, caplog):
        recorder = _DecisionRecorder()
        _stub_speaking(monkeypatch, recorder)
        adapter = _adapter(response={"channels": ["555"], "mode": "enforce"})
        adapter.send = AsyncMock(side_effect=RuntimeError("discord broke"))
        ambient = _message(content="ambient chatter")

        with caplog.at_level(logging.WARNING):
            dispatched = await adapter._dispatch_discord_message(ambient)

        assert dispatched is True  # the allow verdict stands despite the failed notice
        assert recorder.calls == 1
        assert any("jev notice send failed" in record.message for record in caplog.records)


class TestReactionGateNotices:
    @pytest.mark.asyncio
    async def test_react_decision_posts_react_notice(self, monkeypatch):
        recorder = _ChoiceRecorder(probabilities=REACT_FIRE)
        _stub_choice(monkeypatch, recorder)
        adapter = _adapter(
            reaction={"enabled": True, "channels": ["555"]},
            response={"channels": ["555"], "mode": "enforce"},
        )
        speaking = _DecisionRecorder(scores={
            "addresses_bot": 0.01, "continues_bot_thread": 0.02,
            "joins_bot_thread": 0.01, "noise": 0.9,
        })
        _stub_speaking(monkeypatch, speaking)
        ambient = _message(content="nice one!")

        await adapter._dispatch_discord_message(ambient)
        await _drain(adapter)

        assert recorder.calls == 1
        assert ambient.add_reaction.await_count == 1
        lines = _notice_lines(adapter)
        assert "🔎 jev reaction-gate user message from Alice: react 🔥 (abstain 0.20)" in lines
        # The speaking gate consulted the same message: its notice is there too.
        assert any(line.startswith("🔎 jev response-gate ") for line in lines)

    @pytest.mark.asyncio
    async def test_abstention_is_visible(self, monkeypatch):
        probabilities = {
            "👍": 0.05, "❤️": 0.05, "😂": 0.05, "🎉": 0.05, "😢": 0.03, "😮": 0.03,
            "🔥": 0.02, "🤔": 0.02, "None": 0.50, "Other": 0.20,
        }
        recorder = _ChoiceRecorder(probabilities=probabilities)
        _stub_choice(monkeypatch, recorder)
        adapter = _adapter(reaction={"enabled": True, "channels": ["555"]})
        ambient = _message(content="anyone up for ranked tonight")

        await adapter._dispatch_discord_message(ambient)
        await _drain(adapter)

        assert ambient.add_reaction.await_count == 0
        assert "🔎 jev reaction-gate user message from Alice: no reaction (abstain 0.70)" in _notice_lines(adapter)

    @pytest.mark.asyncio
    async def test_judge_failure_posts_failed_notice_and_no_reaction(self, monkeypatch):
        recorder = _ChoiceRecorder(error="timeout")
        _stub_choice(monkeypatch, recorder)
        adapter = _adapter(reaction={"enabled": True, "channels": ["555"]})
        ambient = _message(content="nice one!")

        await adapter._dispatch_discord_message(ambient)
        await _drain(adapter)

        assert ambient.add_reaction.await_count == 0
        assert "🔎 jev reaction-gate user message from Alice: failed (timeout)" in _notice_lines(adapter)

    @pytest.mark.asyncio
    async def test_notice_send_failure_keeps_the_reaction(self, monkeypatch, caplog):
        recorder = _ChoiceRecorder(probabilities=REACT_FIRE)
        _stub_choice(monkeypatch, recorder)
        adapter = _adapter(reaction={"enabled": True, "channels": ["555"]})
        ambient = _message(content="nice one!")
        real_send = adapter.send

        async def flaky_send(chat_id, content, *args, **kwargs):
            if (kwargs.get("metadata") or {}).get("jev_notice"):
                raise RuntimeError("discord broke")
            return await real_send(chat_id, content, *args, **kwargs)

        adapter.send = flaky_send
        with caplog.at_level(logging.WARNING):
            await adapter._dispatch_discord_message(ambient)
            await _drain(adapter)

        assert ambient.add_reaction.await_count == 1  # decision unaffected
        assert any("jev notice send failed" in record.message for record in caplog.records)


class TestNoRecursion:
    @pytest.mark.asyncio
    async def test_notice_never_re_enters_the_gates(self, monkeypatch):
        speaking = _DecisionRecorder()
        choice = _ChoiceRecorder(probabilities=REACT_FIRE)
        _stub_speaking(monkeypatch, speaking)
        _stub_choice(monkeypatch, choice)
        adapter = _adapter(
            reaction={"enabled": True, "channels": ["555"]},
            response={"channels": ["555"], "mode": "enforce"},
        )
        ambient = _message(content="ambient chatter")

        await adapter._dispatch_discord_message(ambient)
        await _drain(adapter)

        # Exactly one consult per gate for the human message — the two notice
        # sends the consults produced never became new candidates.
        assert speaking.calls == 1
        assert choice.calls == 1
        assert len(_notices(adapter)) == 2

    @pytest.mark.asyncio
    async def test_inbound_notice_replay_never_consults_the_judge(self, monkeypatch):
        """A notice line arriving back inbound (relay, edit, history tooling) is bot
        chatter: no classifier request, no dispatch — the notice loop never feeds itself."""
        speaking = _DecisionRecorder()
        choice = _ChoiceRecorder(probabilities=REACT_FIRE)
        _stub_speaking(monkeypatch, speaking)
        _stub_choice(monkeypatch, choice)
        adapter = _adapter(
            reaction={"enabled": True, "channels": ["555"]},
            response={"channels": ["555"], "mode": "enforce"},
        )
        inbound_notice = _message(
            content="🔎 jev response-gate user message from Alice: allow "
                    "(addresses_bot=0.91 continues_bot_thread=0.12 joins_bot_thread=0.03 noise=0.05)",
            author=SimpleNamespace(id=BOT_ID, name="hermes", display_name=BOT_NAME, bot=True),
        )

        await adapter._dispatch_discord_message(inbound_notice)

        assert speaking.calls == 0
        assert choice.calls == 0
        adapter._handle_message.assert_not_awaited()
        assert _notices(adapter) == []


    @pytest.mark.asyncio
    async def test_inbound_goal_intent_status_replay_never_consults_the_judge(self, monkeypatch):
        """The fenced goal-intent status replaying inbound (relay, edit,
        history tooling) is bot chatter like any notice: no request, no
        dispatch — even though it carries no ``🔎 jev`` prefix line."""
        speaking = _DecisionRecorder()
        choice = _ChoiceRecorder(probabilities=REACT_FIRE)
        _stub_speaking(monkeypatch, speaking)
        _stub_choice(monkeypatch, choice)
        adapter = _adapter(
            reaction={"enabled": True, "channels": ["555"]},
            response={"channels": ["555"], "mode": "enforce"},
        )
        inbound_status = _message(
            content="```\n🔍 Goal Intent: Task (0.90)\n```",
            author=SimpleNamespace(id=BOT_ID, name="hermes", display_name=BOT_NAME, bot=True),
        )

        await adapter._dispatch_discord_message(inbound_status)

        assert speaking.calls == 0
        assert choice.calls == 0
        adapter._handle_message.assert_not_awaited()
        assert _notices(adapter) == []

    @pytest.mark.asyncio
    async def test_human_authored_notice_copy_is_a_fresh_human_message(self, monkeypatch):
        """A human pasting/quoting a notice line is fresh human speech, not
        autonomous recursion: provenance (bot author, trusted non-conversational
        metadata), never content, decides exclusion — so this message IS
        consulted once and its own notice follows. No prefix suppression
        exists, and none may be added.
        """
        speaking = _DecisionRecorder()
        choice = _ChoiceRecorder(probabilities=REACT_FIRE)
        _stub_speaking(monkeypatch, speaking)
        _stub_choice(monkeypatch, choice)
        adapter = _adapter(
            reaction={"enabled": True, "channels": ["555"]},
            response={"channels": ["555"], "mode": "enforce"},
        )
        human_copy = _message(
            content="🔎 jev response-gate user message from Alice: failed (http_502)",
        )

        await adapter._dispatch_discord_message(human_copy)
        await _drain(adapter)

        assert speaking.calls == 1
        assert choice.calls == 1
        assert len(_notices(adapter)) == 2


class TestFormatterIsolation:
    """A formatter failure anywhere (cache hashing, resolution, build, render) is a
    diagnostic failure: it must never escape into — or change — the gate decision,
    the dispatch verdict or the reaction the notice was reporting."""

    def _break_resolution(self, monkeypatch):
        import gateway.jev_notice as jn

        def _boom():
            raise OSError("cache path resolution broke")

        monkeypatch.setattr(jn, "_cached_binary_path", _boom)
        monkeypatch.setattr(jn, "_bridge_binary", None)
        monkeypatch.setattr(jn, "_bridge_failed_at", None)

    @pytest.mark.asyncio
    async def test_resolution_failure_never_changes_dispatch(self, monkeypatch):
        self._break_resolution(monkeypatch)
        recorder = _DecisionRecorder()
        _stub_speaking(monkeypatch, recorder)
        adapter = _adapter(response={"channels": ["555"], "mode": "enforce"})
        ambient = _message(content="ambient chatter")

        dispatched = await adapter._dispatch_discord_message(ambient)

        assert dispatched is True  # the allow verdict stands, no exception escapes
        assert recorder.calls == 1
        assert _notices(adapter) == []

    @pytest.mark.asyncio
    async def test_resolution_failure_never_changes_the_reaction(self, monkeypatch):
        self._break_resolution(monkeypatch)
        recorder = _ChoiceRecorder(probabilities=REACT_FIRE)
        _stub_choice(monkeypatch, recorder)
        adapter = _adapter(reaction={"enabled": True, "channels": ["555"]})
        ambient = _message(content="nice one!")

        await adapter._dispatch_discord_message(ambient)
        await _drain(adapter)

        assert ambient.add_reaction.await_count == 1  # the 🔥 decision stands
        assert _notices(adapter) == []


class TestEventLoopResponsiveness:
    """The Go build/render subprocess and the bridge lock are blocking work: the
    async notice path must keep the event loop responsive while a build stalls."""

    @pytest.mark.asyncio
    async def test_blocked_build_does_not_stall_the_loop(self, monkeypatch):
        import gateway.jev_notice as jn

        build_started = threading.Event()
        release_build = threading.Event()

        def _blocked_resolve():
            build_started.set()
            assert release_build.wait(30), "test never released the build"
            return None

        monkeypatch.setattr(jn, "_resolve_go_binary", _blocked_resolve)
        monkeypatch.setattr(jn, "_bridge_binary", None)
        monkeypatch.setattr(jn, "_bridge_failed_at", None)
        recorder = _DecisionRecorder()
        _stub_speaking(monkeypatch, recorder)
        adapter = _adapter(response={"channels": ["555"], "mode": "enforce"})
        ambient = _message(content="ambient chatter")

        task = asyncio.ensure_future(adapter._dispatch_discord_message(ambient))
        # Let the dispatch reach the formatter bridge (no wall-clock sleeps:
        # yield until the controlled build actually starts).
        while not build_started.is_set():
            await asyncio.sleep(0)

        # The loop must stay responsive while the build is blocked elsewhere.
        loop = asyncio.get_running_loop()
        start = loop.time()

        async def _heartbeat():
            for _ in range(200):
                await asyncio.sleep(0)

        await asyncio.wait_for(_heartbeat(), timeout=2.0)
        assert loop.time() - start < 2.0

        release_build.set()
        dispatched = await asyncio.wait_for(task, timeout=30)
        assert dispatched is True
        assert recorder.calls == 1


class TestNoticeHistoryExclusion:
    """Notices are excluded from model-history recovery: the metadata marks them
    non-conversational, they never become the last-self-message boundary, and a
    cold history scan (recovery) filters them out by ID and by notice prefix."""

    @pytest.mark.asyncio
    async def test_notice_metadata_marks_history_non_conversational(self, monkeypatch):
        recorder = _DecisionRecorder()
        _stub_speaking(monkeypatch, recorder)
        adapter = _adapter(response={"channels": ["555"], "mode": "enforce"})
        ambient = _message(content="ambient chatter")

        await adapter._dispatch_discord_message(ambient)

        notices = _notices(adapter)
        assert notices, "a real judge request must post its notice"
        metadata = notices[0][2]["metadata"]
        assert metadata["jev_notice"] is True
        assert metadata["non_conversational_history"] is True

    @pytest.mark.asyncio
    async def test_real_send_never_becomes_history_boundary(self, monkeypatch, tmp_path):
        """Real adapter.send (only the Discord transport object is faked): the
        notice's message ID is marked non-conversational and never recorded as
        the conversation's last-self-message boundary."""
        recorder = _DecisionRecorder()
        _stub_speaking(monkeypatch, recorder)
        adapter = _adapter(response={"channels": ["555"], "mode": "enforce"})
        # Re-wire the real send seam: a fake connected channel instead of a
        # mocked adapter.send, so the tracker/boundary logic in send() runs.
        del adapter.send  # undo the _adapter() I/O mock; the real send() runs
        notice_msg = SimpleNamespace(id=7001)

        async def _record_skip(_self, _reply, result, *_a, **_kw):  # delivery ledger is I/O
            return result

        monkeypatch.setattr(
            DiscordAdapter, "_record_response_async", _record_skip,
        )
        channel = _Channel(555)
        adapter._client = SimpleNamespace(
            user=adapter._client.user,
            get_channel=lambda _cid: channel,
            fetch_channel=AsyncMock(return_value=channel),
            close=AsyncMock(),
        )
        channel.send = AsyncMock(return_value=notice_msg)
        ambient = _message(content="ambient chatter")

        dispatched = await adapter._dispatch_discord_message(ambient)

        assert dispatched is True
        assert channel.send.await_count == 1
        notice_id = str(notice_msg.id)
        assert notice_id in adapter._nonconversational_messages
        # The notice is NOT the conversation boundary for history recovery.
        assert adapter._last_self_message_id.get("555") != notice_id

    @pytest.mark.asyncio
    async def test_recovered_history_drops_notices_and_keeps_conversation(
        self, monkeypatch, tmp_path,
    ):
        """Cold history recovery: a notice sitting in the channel (sent before the
        process restarted, so only the persisted tracker/prefix can exclude it)
        neither truncates the window nor leaks into the model's context."""
        notice_text = (
            "🔎 jev response-gate user message from Alice: failed (http_502)"
        )
        adapter = _adapter(response={"channels": ["555"], "mode": "enforce"})
        bot_author = adapter._client.user

        def _msg(msg_id, content, author):
            return SimpleNamespace(
                id=msg_id, content=content, clean_content=content,
                attachments=[], author=author, type=discord.MessageType.default,
            )

        # Newest first: the notice is the newest message in the channel.
        messages = [
            _msg(7001, notice_text, bot_author),
            _message(content="hello there, still need the context"),  # id 42, human
        ]

        class _HistoryChannel(_Channel):
            def history(self, **_kwargs):
                async def _gen():
                    for entry in messages:
                        yield entry
                return _gen()

        channel = _HistoryChannel(555)
        context = await adapter._fetch_channel_context(
            channel, SimpleNamespace(id=9999),
        )

        assert "hello there" in context
        assert "🔎 jev" not in context
        assert "failed (http_502)" not in context


def _write_config(home, text):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")


class TestJevNoticesSwitch:
    """``display.jev_notices: false`` posts no notice on any lane; decisions are unchanged."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("echo, error, dispatched", [
        (False, None, True),         # response lane, enforce allow
        (False, "http_502", False),  # response lane, fail-closed deny
        (True, None, False),         # echo lane drops normal dispatch silently
    ])
    async def test_every_lane_stays_silent(self, tmp_path, monkeypatch, echo, error, dispatched):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_config(tmp_path, "display:\n  jev_notices: false\n")
        speaking, choice = _DecisionRecorder(error=error), _ChoiceRecorder(probabilities=REACT_FIRE)
        _stub_speaking(monkeypatch, speaking)
        _stub_choice(monkeypatch, choice)
        response = {"channels": ["555"], "mode": "enforce", "echo_channels": ["555"] if echo else []}
        adapter = _adapter(reaction={"enabled": True, "channels": ["555"]}, response=response)
        adapter._send_jev_notice = AsyncMock()
        ambient = _message(content="ambient")

        assert await adapter._dispatch_discord_message(ambient) is dispatched
        await _drain(adapter)

        assert (speaking.calls, choice.calls, ambient.add_reaction.await_count) == (1, 1, 1)
        adapter._send_jev_notice.assert_not_awaited()


class TestJevNoticesFollowTheOwningProfile:
    """Under multiplexing the switch is read from the profile that owns the message."""

    @pytest.fixture
    def mux(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        from gateway.config import GatewayConfig, Platform
        from gateway.pairing import PairingStore
        from gateway.profile_routing import parse_profile_routes
        from gateway.run import GatewayRunner
        from plugins.platforms.discord.hub import _HubProfileResolver

        home = tmp_path / "hh"
        _write_config(home, "display:\n  jev_notices: true\n")
        _write_config(home / "profiles" / "winnie", "display:\n  jev_notices: false\n")
        monkeypatch.setenv("HERMES_HOME", str(home))
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=True)
        runner.config.profile_routes = parse_profile_routes([
            {"name": "winnie-room", "platform": "discord", "profile": "winnie", "chat_id": "777"},
        ])
        runner.pairing_store, runner.pairing_stores = PairingStore(profile="default"), {}
        runner._primary_profile_name = "default"
        rig = SimpleNamespace(**{
            name: _adapter(response={"channels": ["555", "777"], "mode": "enforce"})
            for name in ("primary", "winnie", "hub")
        })
        rig.winnie.set_owner_profile("winnie")
        rig.primary.gateway_runner = rig.winnie.gateway_runner = runner
        # A hub-side adapter attached by winnie's gateway: its resolver only stamps source.profile.
        rig.hub.gateway_runner = _HubProfileResolver(
            SimpleNamespace(_adapter_profiles={"k": {"winnie"}}, _adapter_routes={}), "k")
        runner.adapters = {Platform.DISCORD: rig.primary}
        runner._profile_adapters = {"winnie": {Platform.DISCORD: rig.winnie}}
        served = [("default", home), ("winnie", home / "profiles" / "winnie")]
        with patch("hermes_cli.profiles.profiles_to_serve", return_value=served), \
                patch("hermes_cli.profiles.get_profile_dir",
                      side_effect=lambda n: home if n == "default" else home / "profiles" / n), \
                patch("hermes_cli.profiles.profile_exists", return_value=True):
            yield rig

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bot, channel, posts", [
        ("primary", 555, True),   # default bot, default profile: notices on
        ("winnie", 555, False),   # winnie's own bot: winnie's switch is off
        ("primary", 777, False),  # default bot routed to winnie: the routed profile decides
        ("hub", 555, False),      # hub-side adapter attached for winnie
    ])
    async def test_switch_is_read_from_the_routed_profile(self, mux, monkeypatch, bot, channel, posts):
        recorder = _DecisionRecorder()
        _stub_speaking(monkeypatch, recorder)
        adapter = getattr(mux, bot)

        await adapter._dispatch_discord_message(_message(channel=_Channel(channel)))

        assert recorder.calls == 1
        assert bool(_notices(adapter)) is posts
