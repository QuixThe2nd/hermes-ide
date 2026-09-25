"""MemoryManager strips slash-skill scaffolding for every provider.

When a user invokes a /skill or /bundle, Hermes expands the turn into a
model-facing message that embeds the full skill body. Feeding that verbatim to
memory providers pollutes their stores/embeddings with prompt scaffolding
instead of what the user actually asked. The strip lives once in MemoryManager
so it covers the whole provider fan-out — not per backend.

See: agent.skill_commands.extract_user_instruction_from_skill_message and
MemoryManager._strip_skill_scaffolding.
"""

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from agent.skill_commands import extract_user_instruction_from_skill_message


_SINGLE_SKILL_TURN = (
    '[IMPORTANT: The user has invoked the "skill-creator" skill, indicating they want '
    "you to follow its instructions. The full skill content is loaded below.]\n\n"
    "# Skill Creator\n\n"
    "Large skill body that must not be searched or embedded.\n\n"
    "The user has provided the following instruction alongside the skill invocation: "
    "make a skill for release triage"
)

_BUNDLE_TURN = (
    '[IMPORTANT: The user has invoked the "backend-dev" skill bundle, '
    "loading 2 skills together. Treat every skill below as active guidance for this turn.]\n\n"
    "Bundle: backend-dev\n"
    "Skills loaded: test-driven-development, code-review\n\n"
    "User instruction: fix the failing retrieval test\n\n"
    '[Loaded as part of the "backend-dev" skill bundle.]\n\n'
    "Large bundled skill body that must not be searched or embedded."
)

_BARE_SKILL_TURN = (
    '[IMPORTANT: The user has invoked the "skill-creator" skill, indicating they want '
    "you to follow its instructions. The full skill content is loaded below.]\n\n"
    "# Skill Creator\n\n"
    "Large skill body, no user instruction."
)


class _RecordingProvider(MemoryProvider):
    """Captures exactly what user text each fan-out method received."""

    _name = "recording"

    def __init__(self):
        self.prefetched = []
        self.queued = []
        self.synced = []

    @property
    def name(self) -> str:
        return self._name

    def initialize(self, session_id: str = "", **kwargs) -> None:
        pass

    def is_available(self) -> bool:
        return True

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query, *, session_id: str = "") -> str:
        self.prefetched.append(query)
        return ""

    def queue_prefetch(self, query, *, session_id: str = "") -> None:
        self.queued.append(query)

    def sync_turn(self, user_content, assistant_content, *, session_id: str = "", messages=None) -> None:
        self.synced.append(user_content)

    def get_tool_schemas(self):
        return []


def _manager_with_recorder():
    mgr = MemoryManager()
    provider = _RecordingProvider()
    mgr.add_provider(provider)
    return mgr, provider


class TestExtractUserInstruction:
    def test_non_string_returns_none(self):
        assert extract_user_instruction_from_skill_message(None) is None
        assert extract_user_instruction_from_skill_message(123) is None
        assert extract_user_instruction_from_skill_message([{"text": "hi"}]) is None



    def test_bundle_with_instruction(self):
        assert (
            extract_user_instruction_from_skill_message(_BUNDLE_TURN)
            == "fix the failing retrieval test"
        )




class TestMemoryManagerStripsScaffolding:

    def test_prefetch_all_skips_bare_skill(self):
        mgr, provider = _manager_with_recorder()
        result = mgr.prefetch_all(_BARE_SKILL_TURN)
        assert result == ""
        assert provider.prefetched == []

    def test_queue_prefetch_all_strips_bundle(self):
        mgr, provider = _manager_with_recorder()
        mgr.queue_prefetch_all(_BUNDLE_TURN)
        mgr.flush_pending(timeout=5.0)
        assert provider.queued == ["fix the failing retrieval test"]



    def test_sync_all_skips_bare_skill(self):
        mgr, provider = _manager_with_recorder()
        mgr.sync_all(_BARE_SKILL_TURN, "Done.")
        mgr.flush_pending(timeout=5.0)
        assert provider.synced == []


_DISCORD_BLOCK = (
    "[Triggering message id: `{msg_id}` — use as "
    "`message_id` for reply/react/pin via the discord tools.]\n\n"
)


def _discord_sender_wrap(body: str, msg_id: str = "123456", name: str = "parsayazdani") -> str:
    return _DISCORD_BLOCK.format(msg_id=msg_id) + f"[{name}] {body}"


class TestMemoryManagerGatewayThenSkillComposition:
    """Gateway scaffolding is stripped before skill extraction; the two
    normalizations compose at the shared MemoryManager boundary."""

    def test_prefetch_all_strips_gateway_then_extracts_skill(self):
        mgr, provider = _manager_with_recorder()
        wrapped = _discord_sender_wrap(_SINGLE_SKILL_TURN)
        mgr.prefetch_all(wrapped)
        assert provider.prefetched == ["make a skill for release triage"]

    def test_queue_prefetch_all_strips_gateway_before_provider(self):
        mgr, provider = _manager_with_recorder()
        query = "what did we decide about the postgres migration?"
        mgr.queue_prefetch_all(_discord_sender_wrap(query))
        mgr.flush_pending(timeout=5.0)
        assert provider.queued == [query]

    def test_sync_all_strips_gateway_before_provider(self):
        mgr, provider = _manager_with_recorder()
        query = "what did we decide about the postgres migration?"
        mgr.sync_all(_discord_sender_wrap(query), "We decided to defer.")
        mgr.flush_pending(timeout=5.0)
        assert provider.synced == [query]

    def test_standalone_bare_label_skill_turn_preserves_label(self):
        # No trusted anchor: the bare label is preserved byte-for-byte, and
        # because it now leads the text, the skill extractor's startswith
        # match no longer fires — the turn passes through verbatim. That is
        # the accepted preserve-direction trade-off; every real gateway
        # flow (Discord block anchor, Slack envelope form) still strips
        # first, so extraction works where the scaffolding is provenance-
        # trusted.
        mgr, provider = _manager_with_recorder()
        text = "[parsayazdani] " + _SINGLE_SKILL_TURN
        mgr.sync_all(text, "Done.")
        mgr.flush_pending(timeout=5.0)
        assert provider.synced == [text]

