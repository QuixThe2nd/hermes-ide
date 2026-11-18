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
    async def test_echo_channel_posts_judge_line_not_a_second_notice(self, monkeypatch):
        recorder = _DecisionRecorder()
        _stub_speaking(monkeypatch, recorder)
        adapter = _adapter(response={
            "channels": ["555"], "echo_channels": ["555"], "mode": "enforce",
        })
        ambient = _message(content="ambient chatter")

        await adapter._dispatch_discord_message(ambient)

        assert recorder.calls == 1
        assert _notices(adapter) == []  # the echo IS the notice line there
        all_sends = [call.args[1] for call in adapter.send.await_args_list]
        assert len([text for text in all_sends if "allow" in text or "deny" in text]) == 1

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
