"""Adapter wiring coverage for the opt-in Jev choice reaction gate.

The reaction gate is a dispatch SIDE EFFECT with its own scope, independent of
the speaking gate's verdict: an ambient message the speaking gate denies is
still consulted and reacted to, with no session or text reply; the loop/privacy
exclusions (own bot, system events, DMs, ignored channels, unauthorized
humans) are never consulted; each message id reacts at most once across live
and recovered delivery — including past registry capacity, where in-flight ids
are never evicted; the judge's evidence carries the conversation's earlier
messages (recorded at intake, never the candidate itself) plus this bot's own
delivered final replies; judge errors never block an explicit reply; disconnect
cancels in-flight consultations; and a gate that is off or credential-less
never calls the judge. Only the two judge transports (the HTTP sockets) are
stubbed — dispatch, admission prefilters, the runtimes and the config load are
the real production paths.
"""

import asyncio
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

discord = pytest.importorskip("discord")

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

#: A distribution that reacts with 🔥 (None+Other = 0.20 < 0.5).
REACT_FIRE = {
    "👍": 0.00, "❤️": 0.00, "😂": 0.00, "🎉": 0.00,
    "😢": 0.00, "😮": 0.00, "🔥": 0.80, "🤔": 0.00,
    "None": 0.10, "Other": 0.10,
}

#: A distribution that reacts with the multi-glyph entry 👉👈 (None+Other = 0.20 < 0.5).
REACT_POINT = {"👉👈": 0.80, "🔥": 0.00, "None": 0.10, "Other": 0.10}


@pytest.fixture(autouse=True)
def _clean_gate_env(monkeypatch):
    """Keep the dev shell's channel/user gates out of scope resolution (see test_discord_gate_isolation)."""
    for var in GATE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield
    for var in GATE_ENV_VARS:
        os.environ.pop(var, None)


class _ChoiceRecorder:
    """Socket-level stub for JevChoiceClient._request: records, then answers or raises."""

    def __init__(self, answer=None, error=None, delay=0.0):
        self.calls = 0
        self.answer = answer
        self.error = error
        self.delay = delay
        self.hold = None  # optional asyncio.Event: park every call until it is set
        self.questions = None  # the last request body offered to the judge
        self.states = []  # every request's candidate + evidence, in call order

    async def __call__(self, state, *, chat_id=None, questions=None):
        self.calls += 1
        self.states.append({
            "candidate": dict(state["candidate"]),
            "recent_messages": [dict(entry) for entry in state["recent_messages"]],
        })
        self.questions = questions
        if self.hold is not None:
            await self.hold.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise ResponseGateError(self.error, "wiring test stub")
        if self.answer is not None:
            return {"answers": {"reaction": {"type": "choice", "probabilities": dict(self.answer)}}}
        # Default: a flat distribution over exactly the offered options.
        options = list((questions or {}).get("reaction", {}).get("criteria", {}))
        share = 1.0 / len(options)
        return {"answers": {"reaction": {"type": "choice", "probabilities": {
            option: share for option in options
        }}}}


@pytest.fixture
def choice_judge(monkeypatch):
    """Stub the choice transport on the subclass only (the speak client keeps its own)."""
    recorder = _ChoiceRecorder()
    monkeypatch.setattr(JevChoiceClient, "_request", recorder)
    return recorder


def _set_speaking_gate_verdict(monkeypatch, score: float) -> None:
    """Stub the SPEAKING gate's transport; its decide/threshold path stays real."""

    async def fake_request(_self, state, *, chat_id=None, questions=None):
        return {"answers": {"should_reply": {"type": "noul", "noul": score}}}

    monkeypatch.setattr(JevDecisionClient, "_request", fake_request)


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


def _reaction_adapter(
    *, reaction=None, response=None, credential="test-credential",
    allowed_user_ids=None,
) -> DiscordAdapter:
    """Real adapter built through the production init seams, minus connect()."""
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
    adapter.send = AsyncMock()
    adapter._is_pairing_approved_user = lambda uid, **_: False
    # The connect()-time snapshot seam (see test_discord_gate_isolation): string ids.
    adapter._allowed_user_ids = {
        str(uid) for uid in (allowed_user_ids if allowed_user_ids is not None else (HUMAN_ID,))
    }
    adapter._text_batch_delay_seconds = 0
    return adapter


async def _drain(adapter) -> list:
    """Await every in-flight reaction task spawned so far (deterministic)."""
    tasks = list(adapter._reaction_gate_tasks)
    if tasks:
        await asyncio.gather(*tasks)
    return tasks


def _reactions(message) -> list:
    return [call.args for call in message.add_reaction.await_args_list]


class TestDeniedBySpeakingGateStillReacted:
    """The reaction gate runs on messages the speaking gate drops — as a side effect only."""

    @pytest.mark.asyncio
    async def test_enforce_denied_ambient_consulted_and_reacted_without_reply(self, monkeypatch, choice_judge):
        choice_judge.answer = REACT_FIRE
        _set_speaking_gate_verdict(monkeypatch, 0.31)  # below the 0.8 enforce threshold
        adapter = _reaction_adapter(
            reaction={"enabled": True, "channels": ["555"]},
            response={"channels": ["555"], "mode": "enforce", "threshold": 0.8},
        )
        ambient = _message(content="ambient chatter, no mention of anyone")

        dispatched = await adapter._dispatch_discord_message(ambient)
        await _drain(adapter)

        assert dispatched is False  # the speaking gate denied the reply...
        assert adapter._handle_message.await_count == 0  # ...no session or dispatch...
        assert adapter.send.await_count == 0  # ...and no text reply anywhere.
        assert choice_judge.calls == 1  # ...but the reaction judge was consulted once...
        assert _reactions(ambient) == [("🔥",)]  # ...and reacted with the argmax emoji.


class TestNeverConsulted:
    """Loop/privacy exclusions: no consultation, no task, no reaction."""

    @pytest.mark.asyncio
    async def test_own_bot_message(self, choice_judge):
        adapter = _reaction_adapter(reaction={"enabled": True, "channels": ["555"]})
        own = _message(
            author=SimpleNamespace(id=BOT_ID, name="hermes", display_name=BOT_NAME, bot=True),
        )
        # Loop guard is identity/equality against the connected client's user.
        assert own.author == adapter._client.user

        await adapter._dispatch_discord_message(own)
        tasks = await _drain(adapter)

        assert choice_judge.calls == 0 and not _reactions(own) and not tasks

    @pytest.mark.asyncio
    async def test_system_lifecycle_type(self, choice_judge):
        adapter = _reaction_adapter(reaction={"enabled": True, "channels": ["555"]})
        join = _message(msg_type=discord.MessageType.new_member, content="")

        await adapter._dispatch_discord_message(join)
        tasks = await _drain(adapter)

        assert choice_judge.calls == 0 and not _reactions(join) and not tasks

    @pytest.mark.asyncio
    async def test_dm_channel(self, choice_judge):
        adapter = _reaction_adapter(reaction={"enabled": True, "channels": ["555"]})
        # A real DMChannel instance (isinstance-exact) without running discord.py's
        # __init__; the subclass exists so the fake can carry plain attributes.
        class FakeDM(discord.DMChannel):
            pass

        dm = object.__new__(FakeDM)
        dm.id = 990
        private = _message(channel=dm, content="just between us")

        await adapter._dispatch_discord_message(private)
        tasks = await _drain(adapter)

        assert choice_judge.calls == 0 and not _reactions(private) and not tasks

    @pytest.mark.asyncio
    async def test_ignored_channel(self, monkeypatch, choice_judge):
        monkeypatch.setenv("DISCORD_IGNORED_CHANNELS", "559")
        adapter = _reaction_adapter(reaction={"enabled": True, "channels": ["555"]})
        ignored = _message(channel=_Channel(559), content="not for this bot")

        await adapter._dispatch_discord_message(ignored)
        tasks = await _drain(adapter)

        assert choice_judge.calls == 0 and not _reactions(ignored) and not tasks

    @pytest.mark.asyncio
    async def test_unauthorized_author(self, choice_judge):
        adapter = _reaction_adapter(reaction={"enabled": True, "channels": ["555"]})
        outsider = _message(
            author=SimpleNamespace(id=222, name="eve", display_name="Eve", bot=False),
        )

        await adapter._dispatch_discord_message(outsider)
        tasks = await _drain(adapter)

        assert choice_judge.calls == 0 and not _reactions(outsider) and not tasks
        assert adapter._handle_message.await_count == 0


class TestOnceOnlyAcrossDeliveryPaths:
    """One message id — however it arrives — means one consultation, one reaction."""

    @pytest.mark.asyncio
    async def test_live_then_recovered_duplicate(self, choice_judge):
        choice_judge.answer = REACT_FIRE
        adapter = _reaction_adapter(reaction={"enabled": True, "channels": ["555"]})
        duplicate = _message(msg_id=200, content="delivered twice")

        await adapter._dispatch_discord_message(duplicate)
        await adapter._dispatch_recovered_message(duplicate)
        await _drain(adapter)

        assert choice_judge.calls == 1
        assert _reactions(duplicate) == [("🔥",)]


class TestJudgeErrorNeverBlocksReplies:
    @pytest.mark.asyncio
    async def test_error_means_no_reaction_but_reply_path_intact(self, choice_judge):
        choice_judge.error = "timeout"
        adapter = _reaction_adapter(reaction={"enabled": True, "channels": ["555"]})
        explicit = _message(
            content="status?", mentions=[adapter._client.user], msg_id=210,
        )

        dispatched = await adapter._dispatch_discord_message(explicit)
        await _drain(adapter)

        assert dispatched is True  # the explicit reply still dispatched...
        assert adapter._handle_message.await_count == 1  # ...through the real handler seam...
        assert choice_judge.calls == 1  # ...while the judge WAS asked...
        assert not _reactions(explicit)  # ...and its failure meant no reaction.


class TestDisconnectCleansUp:
    @pytest.mark.asyncio
    async def test_in_flight_consults_cancelled_and_state_cleared(self, choice_judge):
        choice_judge.delay = 30.0  # hold the consultation open past the teardown
        adapter = _reaction_adapter(reaction={"enabled": True, "channels": ["555"]})
        await adapter._dispatch_discord_message(_message(msg_id=230, content="slow judge"))
        in_flight = list(adapter._reaction_gate_tasks)
        await asyncio.sleep(0)  # let the task start and reach the stubbed socket
        assert in_flight  # the consultation is genuinely in flight

        adapter._release_platform_lock = lambda: None
        await adapter.disconnect()

        assert all(task.cancelled() or task.done() for task in in_flight)
        assert not adapter._reaction_gate_tasks
        assert not adapter._reaction_gate_seen
        assert adapter._reaction_gate is None


class TestGateOffOrMissingCredential:
    """Nothing is armed → the judge is never called."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reaction",
        [
            None,  # block omitted entirely
            {"enabled": False, "channels": ["555"]},
            {"enabled": True, "channels": []},
        ],
        ids=["block-omitted", "enabled-false", "no-channels"],
    )
    async def test_gate_off_zero_judge_calls(self, choice_judge, reaction):
        adapter = _reaction_adapter(reaction=reaction)
        assert adapter._reaction_gate is None
        message = _message(content="no gate armed")

        await adapter._dispatch_discord_message(message)
        tasks = await _drain(adapter)

        assert choice_judge.calls == 0 and not _reactions(message) and not tasks

    @pytest.mark.asyncio
    async def test_missing_credential_fails_closed(self, choice_judge):
        adapter = _reaction_adapter(
            reaction={"enabled": True, "channels": ["555"]}, credential=None,
        )
        # Enabled with no key: the runtime exists (so scope is honored) but has
        # no client to consult — active-yet-silent, never a quiet disable.
        assert adapter._reaction_gate is not None
        assert adapter._reaction_gate.client is None

        message = _message(content="no key in this profile")
        await adapter._dispatch_discord_message(message)
        await _drain(adapter)

        assert choice_judge.calls == 0
        assert not _reactions(message)


class TestMultiGlyphEntryReactions:
    """A whitelist entry of several glyphs: ONE judge option, one reaction per glyph.

    The entry is offered to the judge whole (it is an opaque option string), and the
    winning entry is expanded into its grapheme clusters at the reaction layer only:
    👉👈 → the two reactions 👉 then 👈, while a compound emoji (one cluster) keeps
    the single-reaction behavior byte-for-byte. The per-cluster API calls are
    independent — one failing is logged and never skips the rest or touches dispatch.
    """

    @pytest.mark.asyncio
    async def test_two_glyph_entry_reacts_each_glyph_in_order(self, choice_judge):
        choice_judge.answer = REACT_POINT
        adapter = _reaction_adapter(
            reaction={"enabled": True, "channels": ["555"], "emojis": ["👉👈", "🔥"]},
        )
        message = _message(content="you two, huddle up!")

        await adapter._dispatch_discord_message(message)
        await _drain(adapter)

        assert choice_judge.calls == 1  # ONE consultation for the whole entry...
        assert _reactions(message) == [("👉",), ("👈",)]  # ...then 👉 strictly before 👈.

    @pytest.mark.asyncio
    async def test_compound_emoji_entry_stays_one_reaction(self, choice_judge):
        choice_judge.answer = {"🤦‍♂️": 0.80, "🔥": 0.00, "None": 0.10, "Other": 0.10}
        adapter = _reaction_adapter(
            reaction={"enabled": True, "channels": ["555"], "emojis": ["🤦‍♂️", "🔥"]},
        )
        message = _message(content="he did WHAT again")

        await adapter._dispatch_discord_message(message)
        await _drain(adapter)

        assert choice_judge.calls == 1
        # The ZWJ compound is one grapheme cluster: one call, the whole compound.
        assert _reactions(message) == [("🤦‍♂️",)]

    @pytest.mark.asyncio
    async def test_multi_glyph_entry_is_one_criterion_in_the_question(self, choice_judge):
        """The built question offers 👉👈 as ONE option — never its glyphs separately."""
        adapter = _reaction_adapter(
            reaction={"enabled": True, "channels": ["555"], "emojis": ["👉👈", "🔥"]},
        )
        message = _message(content="any pointers?")

        await adapter._dispatch_discord_message(message)
        await _drain(adapter)

        assert choice_judge.calls == 1
        reaction_question = choice_judge.questions["reaction"]
        # Exactly the two configured entries plus the fixed abstentions — the
        # multi-glyph entry appears once, whole, as one criterion key.
        assert list(reaction_question["criteria"]) == ["👉👈", "🔥", "None", "Other"]
        assert reaction_question["criteria"]["👉👈"] == (
            "A 👉👈 reaction from this assistant fits this message."
        )

    @pytest.mark.asyncio
    async def test_first_cluster_failure_still_attempts_the_rest(self, choice_judge, caplog):
        choice_judge.answer = REACT_POINT
        adapter = _reaction_adapter(
            reaction={"enabled": True, "channels": ["555"], "emojis": ["👉👈", "🔥"]},
        )
        # An explicit mention: its dispatch must survive the reaction failure.
        message = _message(
            content="look at this", mentions=[adapter._client.user], msg_id=260,
            add_reaction=AsyncMock(side_effect=[Exception("HTTP 500"), None]),
        )

        with caplog.at_level(logging.INFO, logger="plugins.platforms.discord.adapter"):
            dispatched = await adapter._dispatch_discord_message(message)
            await _drain(adapter)

        assert dispatched is True  # dispatch is unaffected...
        assert adapter._handle_message.await_count == 1  # ...the reply still goes out...
        assert choice_judge.calls == 1  # ...one consultation...
        assert _reactions(message) == [("👉",), ("👈",)]  # ...BOTH clusters attempted.
        # The per-cluster outcome tokens land in the log (ids only, no content):
        reaction_lines = [
            record.message for record in caplog.records
            if " reaction_gate channel=" in record.message
        ]
        assert len(reaction_lines) == 1
        assert "outcome=react_partial:👉👈[👉:failed,👈:added]" in reaction_lines[0]

    @pytest.mark.asyncio
    async def test_all_clusters_succeed_logs_the_whole_entry(self, choice_judge, caplog):
        """The single-cluster outcome format is unchanged when every cluster lands."""
        choice_judge.answer = REACT_FIRE
        adapter = _reaction_adapter(reaction={"enabled": True, "channels": ["555"]})
        message = _message(content="shipped!", msg_id=261)

        with caplog.at_level(logging.INFO, logger="plugins.platforms.discord.adapter"):
            await adapter._dispatch_discord_message(message)
            await _drain(adapter)

        assert _reactions(message) == [("🔥",)]
        reaction_lines = [
            record.message for record in caplog.records
            if " reaction_gate channel=" in record.message
        ]
        assert len(reaction_lines) == 1
        assert "outcome=react:🔥" in reaction_lines[0]


class TestEvidenceAcrossRapidMessages:
    """Candidates are buffered at INTAKE, so a rapid successor's consult sees them.

    Two eligible messages arrive in one conversation faster than the judge answers
    the first: the second consult's request evidence must contain the first message
    (the old post-evaluation buffering lost exactly this case), while neither
    consult ever sees its own message as its own history.
    """

    @pytest.mark.asyncio
    async def test_second_consult_sees_first_message_neither_sees_itself(self, choice_judge):
        choice_judge.answer = REACT_FIRE
        choice_judge.delay = 0.25  # both consultations stay open past both intakes
        adapter = _reaction_adapter(reaction={"enabled": True, "channels": ["555"]})
        channel = _Channel(555)
        first = _message(channel=channel, msg_id=301, content="first rapid message")
        second = _message(channel=channel, msg_id=302, content="second rapid message")

        await adapter._dispatch_discord_message(first)
        await adapter._dispatch_discord_message(second)  # while the first judge call is open
        await _drain(adapter)

        assert choice_judge.calls == 2
        first_state, second_state = choice_judge.states
        assert first_state["candidate"]["content"] == "first rapid message"
        assert second_state["candidate"]["content"] == "second rapid message"
        # The successor's evidence CONTAINS the still-being-judged predecessor...
        assert [m["content"] for m in second_state["recent_messages"]] == ["first rapid message"]
        assert second_state["recent_messages"][0]["author"] == "Alice"
        # ...and neither consult judges itself (the candidate is excluded by id).
        assert [m["content"] for m in first_state["recent_messages"]] == []
        assert all(
            m["content"] != "second rapid message" for m in second_state["recent_messages"]
        )


class TestSentFinalReachesReactionEvidence:
    """This bot's delivered final replies are reaction evidence (send seam)."""

    @pytest.mark.asyncio
    async def test_observed_bot_final_is_in_the_next_consults_evidence(self, choice_judge):
        adapter = _reaction_adapter(reaction={"enabled": True, "channels": ["555"]})
        channel = _Channel(555)
        # The same hook the send path calls for a delivered final; the speaking gate
        # is off here, so this also pins that the reaction evidence does not depend
        # on it.
        adapter._response_gate_observe_sent(channel, "here is the fix you asked for")

        followup = _message(channel=channel, msg_id=310, content="thanks, that fixed it!")
        await adapter._dispatch_discord_message(followup)
        await _drain(adapter)

        assert choice_judge.calls == 1
        recent = choice_judge.states[0]["recent_messages"]
        assert [m["content"] for m in recent] == ["here is the fix you asked for"]
        assert recent[0]["author"] == BOT_NAME


class TestInFlightIdsSurviveRegistryCapacity:
    """Registry eviction skips claimed-but-unfinished ids; finished ones age out.

    With the judge blocked and the registry over capacity, the oldest in-flight id
    must NOT be evicted — its duplicate delivery still consults at most once and
    reacts at most once — while a finished id is evicted as before, so its duplicate
    may re-consult once the id has aged out.
    """

    @pytest.mark.asyncio
    async def test_duplicate_of_oldest_in_flight_id_consults_once(self, choice_judge):
        choice_judge.answer = REACT_FIRE
        adapter = _reaction_adapter(reaction={"enabled": True, "channels": ["555"]})
        adapter._REACTION_GATE_MAX_SEEN = 2  # instance override: tiny registry
        channel = _Channel(555)
        finished = _message(channel=channel, msg_id=490, content="already judged")
        await adapter._dispatch_discord_message(finished)
        await _drain(adapter)  # its consultation is done: evictable again

        choice_judge.hold = asyncio.Event()  # block every further consultation
        oldest = _message(channel=channel, msg_id=491, content="oldest in flight")
        middle = _message(channel=channel, msg_id=492, content="middle in flight")
        newest = _message(channel=channel, msg_id=493, content="newest in flight")
        await adapter._dispatch_discord_message(oldest)
        await adapter._dispatch_discord_message(middle)  # evicts the finished id 490
        await adapter._dispatch_discord_message(newest)  # nothing evictable: over capacity
        # A duplicate of the oldest IN-FLIGHT id arrives while the registry is over
        # capacity: the id was never evicted, so it consults at most once.
        await adapter._dispatch_recovered_message(oldest)
        # The finished id 490 WAS evicted by 492's claim: its duplicate re-consults.
        duplicate_finished = _message(channel=channel, msg_id=490, content="already judged")
        await adapter._dispatch_discord_message(duplicate_finished)

        choice_judge.hold.set()  # release every held consultation
        await _drain(adapter)

        # 490 (first pass), 491, 492, 493, then 490 again after aging out — the
        # in-flight duplicate of 491 added none.
        assert choice_judge.calls == 5
        assert _reactions(oldest) == [("🔥",)]  # exactly one reaction, one consult
        assert _reactions(middle) == [("🔥",)] and _reactions(newest) == [("🔥",)]
        assert _reactions(finished) == [("🔥",)]
        assert _reactions(duplicate_finished) == [("🔥",)]  # the aged-out id re-consulted
