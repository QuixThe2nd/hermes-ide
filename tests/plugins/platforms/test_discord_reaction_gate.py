"""Client and policy level coverage for the opt-in Jev choice reaction gate.

The pure decision rule (:func:`choose_reaction`), the strict answer validation
(:meth:`JevChoiceClient._validate_probabilities`), the ``choice`` question the
client offers, and the config surfaces that arm the gate
(:class:`ReactionGateConfig.from_dict` plus the production loader bridge for
both config.yaml spellings) are pinned here. Adapter-level wiring lives in
``test_discord_reaction_gate_wiring.py``; only the HTTP socket is stubbed.
"""

from types import SimpleNamespace

import pytest

from gateway.config import (
    DEFAULT_REACTION_EMOJIS,
    Platform,
    PlatformConfig,
    ReactionGateConfig,
    load_gateway_config,
)
from plugins.platforms.discord.reaction_gate import (
    NONE_OPTION,
    OTHER_OPTION,
    JevChoiceClient,
    build_reaction_criteria,
    build_reaction_instructions,
    choose_reaction,
    split_reaction_clusters,
)
from plugins.platforms.discord.response_gate import (
    ResponseGateError,
    build_conversation_state,
)

#: Small custom whitelist so every crafted number below stays exact and readable.
EMOJIS = ("👍", "❤️", "😂")

FIXED_NONE_TEXT = "No reaction is appropriate"
FIXED_OTHER_TEXT = "A reaction is appropriate but none of the whitelisted emojis fits"

#: Valid distribution over exactly the offered options; sums to exactly 1.0.
BASE_DISTRIBUTION = {"👍": 0.50, "❤️": 0.30, "😂": 0.00, NONE_OPTION: 0.15, OTHER_OPTION: 0.05}


def _choice_client(*, emojis=EMOJIS, criteria=None) -> JevChoiceClient:
    """A real client; only its transport is ever stubbed, never its validation."""
    return JevChoiceClient(
        credential="test-credential", model="typesafe/jev-1.13", timeout_seconds=3.0,
        emojis=emojis, criteria=build_reaction_criteria(emojis, criteria),
        instructions=build_reaction_instructions(),
    )


def _choice_answer(probabilities) -> dict:
    return {"answers": {"reaction": {"type": "choice", "probabilities": probabilities}}}


def _conversation_state(content: str = "thanks, that fixed it!") -> dict:
    channel = SimpleNamespace(id=555, name="general", parent=None, parent_id=None)
    message = SimpleNamespace(
        id=42, content=content, channel=channel,
        author=SimpleNamespace(id=7, name="tek", display_name="Tek"),
        reference=None, attachments=[], mentions=[],
    )
    return build_conversation_state(
        message, channel=channel, bot_name="Winnie", bot_id=1,
        recent=[{"author": "Tek", "content": "earlier chatter"}], context_chars=8000,
    )


class TestChooseReactionBoundary:
    """One pure rule: react iff P(None)+P(Other) is STRICTLY below one half."""

    @pytest.mark.parametrize(
        "none_p, other_p, expected",
        [
            (0.250, 0.249, "👍"),  # 0.499 — strictly below the boundary: react
            (0.250, 0.250, None),  # 0.500 — exactly at it: no reaction
            (0.251, 0.250, None),  # 0.501 — above it: no reaction
        ],
    )
    def test_strict_half_boundary(self, none_p, other_p, expected):
        probabilities = {
            "👍": 0.40, "❤️": 0.10, "😂": 0.00, NONE_OPTION: none_p, OTHER_OPTION: other_p,
        }
        assert choose_reaction(probabilities, EMOJIS) == expected

    def test_individually_top_abstention_still_reacts(self):
        """None may top the distribution alone; only the SUM is compared."""
        probabilities = {
            "👍": 0.31, "❤️": 0.20, "😂": 0.00, NONE_OPTION: 0.30, OTHER_OPTION: 0.19,
        }  # None=0.30 > every emoji, but None+Other = 0.49 < 0.5.
        assert choose_reaction(probabilities, EMOJIS) == "👍"

    def test_exact_tie_resolves_to_first_listed_emoji(self):
        probabilities = {"👍": 0.40, "❤️": 0.40, "😂": 0.00, NONE_OPTION: 0.10, OTHER_OPTION: 0.10}
        assert choose_reaction(probabilities, EMOJIS) == "👍"
        # Same numbers, whitelist reordered: the tie follows the config order,
        # not any hardcoded preference.
        assert choose_reaction(probabilities, ("😂", "❤️", "👍")) == "❤️"

    def test_missing_option_keys_count_as_zero(self):
        # No emoji key at all: every emoji scores 0 and the first-listed wins.
        assert choose_reaction({NONE_OPTION: 0.4, OTHER_OPTION: 0.05}, EMOJIS) == "👍"
        # A missing emoji scores 0 but cannot beat a listed one.
        assert choose_reaction({"❤️": 0.20, NONE_OPTION: 0.1, OTHER_OPTION: 0.1}, EMOJIS) == "❤️"
        # Missing abstention keys mean zero abstention mass: still a reaction.
        assert choose_reaction({"👍": 0.30, "❤️": 0.10, "😂": 0.05}, EMOJIS) == "👍"


class TestSplitReactionClusters:
    """One whitelist entry → one reaction per grapheme cluster (the reaction add-on).

    A multi-glyph entry splits into its glyphs in string order; every compound that
    is ONE grapheme cluster stays ONE reaction. The splitter is conservative: when
    unsure whether code points belong together it keeps them together, so a missing
    rule can only produce too FEW reactions, never half of a compound emoji.
    """

    @pytest.mark.parametrize(
        "entry, clusters",
        [
            ("👉👈", ["👉", "👈"]),  # the motivating multi-glyph entry: two clusters
            ("🤦‍♂️", ["🤦‍♂️"]),  # ZWJ compound: one
            ("❤️", ["❤️"]),  # VS16: one
            ("🫱🏻‍🫲🏽", ["🫱🏻‍🫲🏽"]),  # skin tones + ZWJ handshake: one
            ("1️⃣", ["1️⃣"]),  # keycap (digit + VS16 + U+20E3): one
            ("🇦🇺", ["🇦🇺"]),  # regional-indicator pair: one
            ("👍", ["👍"]),  # plain single glyph: one
            ("👍👎", ["👍", "👎"]),  # two plain glyphs: two
        ],
        ids=[
            "pointing-pair-two", "zwj-man-one", "vs16-heart-one", "skin-tone-handshake-one",
            "keycap-one", "flag-pair-one", "single-glyph-one", "two-glyphs-two",
        ],
    )
    def test_matrix(self, entry, clusters):
        assert split_reaction_clusters(entry) == clusters

    def test_compounds_split_at_their_boundaries(self):
        # Compounds keep their shape wherever they sit inside a longer entry.
        assert split_reaction_clusters("🤦‍♂️👍🇦🇺") == ["🤦‍♂️", "👍", "🇦🇺"]
        assert split_reaction_clusters("👉🤦‍♂️👈") == ["👉", "🤦‍♂️", "👈"]
        # Two flags in a row are two clusters, not one four-indicator blob.
        assert split_reaction_clusters("🇦🇺🇦🇺") == ["🇦🇺", "🇦🇺"]
        # A ZWJ family with skin tones is still exactly one cluster.
        assert split_reaction_clusters("🤦‍♂️🤦‍♂️") == ["🤦‍♂️", "🤦‍♂️"]

    def test_empty_and_none_are_safe(self):
        assert split_reaction_clusters("") == []
        assert split_reaction_clusters(None) == []

    def test_split_is_lossless_and_in_order(self):
        """Concatenating the clusters reproduces the entry exactly, order included."""
        for entry in ("👉👈", "🤦‍♂️", "🫱🏻‍🫲🏽", "1️⃣", "👍👎", "🇦🇺"):
            clusters = split_reaction_clusters(entry)
            assert "".join(clusters) == entry
            assert all(clusters)  # no empty cluster can ever reach Discord


class TestValidateProbabilities:
    """The judge must answer in exactly the shape we offered, or it is not read."""

    @pytest.mark.parametrize(
        "label, response, reason",
        [
            ("no answers object", {}, "schema_mismatch"),
            ("answers not an object", {"answers": "nope"}, "schema_mismatch"),
            ("no reaction answer", {"answers": {}}, "schema_mismatch"),
            ("answer not choice-typed", {"answers": {"reaction": {"type": "noul", "noul": 0.9}}}, "schema_mismatch"),
            ("probabilities missing", {"answers": {"reaction": {"type": "choice"}}}, "schema_mismatch"),
            ("probabilities not an object", {"answers": {"reaction": {"type": "choice", "probabilities": [0.5]}}}, "schema_mismatch"),
            ("unknown extra option", _choice_answer({**BASE_DISTRIBUTION, "🙈": 0.01}), "schema_mismatch"),
            ("missing offered option", _choice_answer(
                {k: v for k, v in BASE_DISTRIBUTION.items() if k != "😂"}
            ), "schema_mismatch"),
            ("boolean probability", _choice_answer({**BASE_DISTRIBUTION, "👍": True}), "schema_mismatch"),
            ("string probability", _choice_answer({**BASE_DISTRIBUTION, "👍": "0.5"}), "schema_mismatch"),
            ("nan probability", _choice_answer({**BASE_DISTRIBUTION, "👍": float("nan")}), "schema_mismatch"),
            ("infinite probability", _choice_answer({**BASE_DISTRIBUTION, "👍": float("inf")}), "schema_mismatch"),
            ("probability above one", _choice_answer({**BASE_DISTRIBUTION, "👍": 1.40}), "out_of_range"),
            ("negative probability", _choice_answer({**BASE_DISTRIBUTION, "👍": -0.10}), "out_of_range"),
            ("incoherent sum", _choice_answer(
                {k: v / 2 for k, v in BASE_DISTRIBUTION.items()}
            ), "incoherent_distribution"),
        ],
    )
    def test_malformed_answer_fails_closed(self, label, response, reason):
        client = _choice_client()
        with pytest.raises(ResponseGateError) as exc_info:
            client._validate_probabilities(response)
        assert exc_info.value.reason == reason

    def test_valid_distribution_is_returned_as_floats(self):
        client = _choice_client()
        validated = client._validate_probabilities(_choice_answer(BASE_DISTRIBUTION))
        assert validated == {k: pytest.approx(v) for k, v in BASE_DISTRIBUTION.items()}
        assert all(isinstance(value, float) for value in validated.values())

    def test_rounding_slack_keeps_a_coherent_distribution_valid(self):
        # Sum is 0.99 — inside the documented float/rounding tolerance.
        probabilities = {**BASE_DISTRIBUTION, NONE_OPTION: 0.14}
        assert sum(probabilities.values()) == pytest.approx(0.99)
        client = _choice_client()
        assert client._validate_probabilities(_choice_answer(probabilities)) is not None


class TestChoiceQuestionShape:
    """What the judge is offered: one ``choice`` question over a fixed option set."""

    @pytest.mark.asyncio
    async def test_question_covers_whitelist_plus_fixed_abstentions(self, monkeypatch):
        captured: list[dict] = []

        async def fake_request(_self, state, *, chat_id=None, questions=None):
            captured.append({"state": state, "questions": questions})
            return _choice_answer(BASE_DISTRIBUTION)

        monkeypatch.setattr(JevChoiceClient, "_request", fake_request)
        client = _choice_client(criteria={"❤️": "Fits love and support."})

        decision = await client.choose(_conversation_state(), chat_id="555")

        assert len(captured) == 1
        question = captured[0]["questions"]
        assert list(question) == ["reaction"]  # exactly one question, fixed key
        reaction = question["reaction"]
        assert reaction["type"] == "choice"
        assert set(reaction["criteria"]) == set(EMOJIS) | {NONE_OPTION, OTHER_OPTION}
        # Whitelisted emoji descriptions are overridable per config...
        assert reaction["criteria"]["❤️"] == "Fits love and support."
        # ...while the abstention wording is part of the decision contract.
        assert reaction["criteria"][NONE_OPTION] == FIXED_NONE_TEXT
        assert reaction["criteria"][OTHER_OPTION] == FIXED_OTHER_TEXT
        assert reaction["instructions"] == build_reaction_instructions()
        # The decision reads the distribution: argmax emoji, abstain mass recorded.
        assert decision.emoji == "👍"
        assert decision.reason == "react"
        assert decision.abstain_sum == pytest.approx(0.20)

    def test_reserved_options_not_overridable_via_criteria(self):
        """A smuggled override for None/Other cannot move the fixed wording."""
        criteria = build_reaction_criteria(
            EMOJIS, {NONE_OPTION: "hacked", OTHER_OPTION: "hacked too", "😂": "Fits joy."},
        )
        assert criteria[NONE_OPTION] == FIXED_NONE_TEXT
        assert criteria[OTHER_OPTION] == FIXED_OTHER_TEXT
        assert criteria["😂"] == "Fits joy."


class TestReactionGateConfigFromDict:
    """Strict load-time validation: bad values refuse to load, never clamp."""

    def test_full_block_loads_typed_and_active(self):
        config = ReactionGateConfig.from_dict({
            "enabled": True,
            "channels": [123456, "lounge"],  # YAML reads bare ids as ints
            "emojis": ["👍", "🔥"],
            "criteria": {"👍": "Fits a thumbs-up."},
            "timeout_seconds": 2.5,
            "context_messages": 5,
            "context_chars": 4096,
            "model": "typesafe/jev-1.13",
            "decisions_url": "http://127.0.0.1:9000/decisions",
        })
        assert config.active is True
        assert config.channels == ("123456", "lounge")
        assert config.emojis == ("👍", "🔥")
        assert config.criteria == {"👍": "Fits a thumbs-up."}
        assert config.timeout_seconds == 2.5
        assert config.context_messages == 5
        assert config.context_chars == 4096
        assert config.decisions_url == "http://127.0.0.1:9000/decisions"

    def test_defaults_when_omitted(self):
        config = ReactionGateConfig.from_dict({"enabled": True, "channels": [1]})
        assert config.active is True
        assert config.emojis == DEFAULT_REACTION_EMOJIS
        assert config.criteria == {}
        assert config.model == "typesafe/jev-1.13"
        assert config.timeout_seconds == 3.0
        assert config.context_messages == 10
        assert config.context_chars == 8000
        assert config.decisions_url is None

    def test_omitted_block_is_off(self):
        config = ReactionGateConfig.from_dict(None)
        assert config.enabled is False
        assert config.active is False

    def test_not_a_mapping_refuses_to_load(self):
        with pytest.raises(ValueError):
            ReactionGateConfig.from_dict(["enabled"])

    @pytest.mark.parametrize(
        "label, overrides",
        [
            ("unknown provider", {"provider": "grok"}),
            ("empty emoji list", {"emojis": []}),
            ("reserved None whitelisted", {"emojis": ["👍", "None"]}),
            ("reserved Other whitelisted", {"emojis": ["Other"]}),
            ("wildcard channels", {"channels": ["*"]}),
            ("non-loopback decisions_url", {"decisions_url": "https://evil.example/decisions"}),
            ("oversized emoji list", {"emojis": [f"e{i:02d}" for i in range(ReactionGateConfig.MAX_EMOJIS + 1)]}),
            ("oversized emoji entry", {"emojis": ["👍", "x" * (ReactionGateConfig.MAX_EMOJI_CHARS + 1)]}),
        ],
    )
    def test_invalid_block_refuses_to_load(self, label, overrides):
        block = {"enabled": True, "channels": ["555"], **overrides}
        with pytest.raises(ValueError):
            ReactionGateConfig.from_dict(block)

    @pytest.mark.parametrize(
        "label, overrides",
        [
            ("unknown provider", {"provider": "grok"}),
            ("empty emoji list", {"emojis": []}),
            ("reserved None whitelisted", {"emojis": ["👍", "None"]}),
            ("wildcard channels", {"channels": ["*"]}),
            ("non-loopback decisions_url", {"decisions_url": "https://evil.example/decisions"}),
        ],
    )
    def test_invalid_block_loads_as_off_on_the_platform(self, label, overrides):
        """PlatformConfig contains the error: the gate stays off, the platform loads."""
        block = {"enabled": True, "channels": ["555"], **overrides}
        platform = PlatformConfig.from_dict({"enabled": True, "reaction_gate": block})
        assert platform.reaction_gate is None

    def test_to_dict_round_trip(self):
        config = ReactionGateConfig.from_dict({
            "enabled": True, "channels": [321], "emojis": ["👍", "🔥"],
            "criteria": {"🔥": "Fits fire."}, "timeout_seconds": 2.0,
            "context_messages": 4, "context_chars": 2048,
            "decisions_url": "http://127.0.0.1:9000/decisions",
        })
        assert ReactionGateConfig.from_dict(config.to_dict()) == config


class TestConfigLoaderBridge:
    """Both config.yaml spellings deliver an armed gate through the real loader."""

    @staticmethod
    def _load(tmp_path, monkeypatch, document: str):
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(document, encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(home))
        return load_gateway_config().platforms[Platform.DISCORD]

    def test_top_level_discord_block_delivers_active_gate(self, tmp_path, monkeypatch):
        platform = self._load(
            tmp_path, monkeypatch,
            "discord:\n"
            "  enabled: true\n"
            "  reaction_gate:\n"
            "    enabled: true\n"
            "    channels:\n"
            "      - 123456\n"
            "      - lounge\n"
            "    emojis:\n"
            "      - 👍\n"
            "      - 🔥\n",
        )
        gate = platform.reaction_gate
        assert gate is not None and gate.active
        assert gate.channels == ("123456", "lounge")
        assert gate.emojis == ("👍", "🔥")

    def test_nested_gateway_platforms_block_delivers_active_gate(self, tmp_path, monkeypatch):
        platform = self._load(
            tmp_path, monkeypatch,
            "gateway:\n"
            "  platforms:\n"
            "    discord:\n"
            "      enabled: true\n"
            "      reaction_gate:\n"
            "        enabled: true\n"
            "        channels:\n"
            "          - 123456\n",
        )
        gate = platform.reaction_gate
        assert gate is not None and gate.active
        assert gate.channels == ("123456",)
