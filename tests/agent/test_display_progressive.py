"""Tests for agent/display_progressive.py — progressive_phrase() spelling rules."""

import pytest

import agent.display_progressive as display_progressive
from agent.display_progressive import progressive_phrase


_PURPOSE_VERB_FORMS = [
    ("recall", "recalling"),
    ("diagnose", "diagnosing"),
    ("remember", "remembering"),
    ("understand", "understanding"),
    ("pick", "picking"),
    ("choose", "choosing"),
    ("decide", "deciding"),
    ("continue", "continuing"),
    ("reconcile", "reconciling"),
    ("grab", "grabbing"),
    ("dedupe", "deduping"),
]


class TestPurposeProgressiveVerbs:
    @pytest.mark.parametrize(("verb", "progressive"), _PURPOSE_VERB_FORMS)
    def test_progressive_phrase(self, verb, progressive):
        assert progressive_phrase(f"{verb} the thing") == f"{progressive} the thing"
        assert progressive_phrase(
            f"{verb.capitalize()} the thing"
        ) == f"{progressive.capitalize()} the thing"

    @pytest.mark.parametrize(("verb", "progressive"), _PURPOSE_VERB_FORMS)
    def test_base_verb_phrase(self, verb, progressive):
        assert display_progressive.base_verb_phrase(
            f"{progressive} the thing"
        ) == f"{verb} the thing"


class TestProgressivePhraseSpelling:
    """Verb → present participle spelling, straight from the curated tables."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("run the suite", "running the suite"),
            ("stop the gateway", "stopping the gateway"),
            ("set the env", "setting the env"),
            ("get the logs", "getting the logs"),
            ("be kind", "being kind"),
            ("lie down", "lying down"),
            ("tie the knot", "tying the knot"),
            ("see the diff", "seeing the diff"),
            ("refer to the docs", "referring to the docs"),
            ("make it so", "making it so"),
            ("write the file", "writing the file"),
            ("use the tool", "using the tool"),
            ("create the dir", "creating the dir"),
            ("save the output", "saving the output"),
            ("update the config", "updating the config"),
            ("fix the bug", "fixing the bug"),
            ("add a test", "adding a test"),
            ("list the jobs", "listing the jobs"),
            ("open the file", "opening the file"),
            ("visit the page", "visiting the page"),
            ("edit the yaml", "editing the yaml"),
            ("verify the fix", "verifying the fix"),
            ("try again", "trying again"),
            ("apply the patch", "applying the patch"),
            ("pull main", "pulling main"),
            ("push the branch", "pushing the branch"),
            ("copy the file", "copying the file"),
            ("move the dir", "moving the dir"),
            ("debug the crash", "debugging the crash"),
            ("rebuild the index", "rebuilding the index"),
        ],
    )
    def test_spelling(self, text, expected):
        assert progressive_phrase(text) == expected

    def test_preserves_leading_capital(self):
        assert progressive_phrase("Check git status") == "Checking git status"

    def test_already_progressive_untouched(self):
        assert progressive_phrase("checking git status") == "checking git status"
        assert progressive_phrase("Reading the config") == "Reading the config"


class TestProgressivePhraseLeaveAlone:
    """Conservative guardrails: only curated imperative verbs transform."""

    @pytest.mark.parametrize(
        "text",
        [
            "the deploy config",
            "GPU mapping note",
            "4 spaces only",
            "/etc/hosts edit",
            "API rate limits",
            "PR review",
            "running the suite",  # ends in -ing already
            "inspecting things",
        ],
    )
    def test_unchanged(self, text):
        assert progressive_phrase(text) == text

    def test_only_first_word_examined(self):
        # "run" transforms; the later "set" must not.
        assert progressive_phrase("run the set script") == "running the set script"

    def test_unknown_verb_left_alone(self):
        assert progressive_phrase("obliterate the cache") == "obliterate the cache"

    def test_empty_and_whitespace(self):
        assert progressive_phrase("") == ""
        assert progressive_phrase("   ") == "   "

    def test_single_word(self):
        assert progressive_phrase("check") == "checking"
        assert progressive_phrase("Check") == "Checking"


class TestProgressivePhraseNounToolOpeners:
    """Tool-name / noun openers must not be rewritten to false participles."""

    def test_git_status_unchanged(self):
        assert progressive_phrase("Git status") == "Git status"

    def test_git_log_unchanged(self):
        assert progressive_phrase("git log for the branch") == "git log for the branch"

    @pytest.mark.parametrize(
        "text",
        [
            "Stat the socket file",
            "stat the inode",
            "Chmod the script",
            "chmod the file",
            "Chown the path",
            "chown the tree",
            "Version the artifact",
            "version the build",
            "Wedge the rollout",
            "wedge the gap",
        ],
    )
    def test_removed_verb_openers_unchanged(self, text):
        assert progressive_phrase(text) == text


class TestProgressivePhraseConsonantDoubling:
    """Kept verbs whose participle doubles the final consonant."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("cut the wire", "cutting the wire"),
            ("debug the crash", "debugging the crash"),
            ("get the logs", "getting the logs"),
            ("log the event", "logging the event"),
            ("map the pins", "mapping the pins"),
            ("pin the ref", "pinning the ref"),
            ("refer to the docs", "referring to the docs"),
            ("remap the table", "remapping the table"),
            ("run the suite", "running the suite"),
            ("set the env", "setting the env"),
            ("skip the step", "skipping the step"),
            ("spot the leak", "spotting the leak"),
            ("stop the gateway", "stopping the gateway"),
            ("strip the prefix", "stripping the prefix"),
            ("swap the entries", "swapping the entries"),
            ("tag the build", "tagging the build"),
            ("unpin the package", "unpinning the package"),
            ("embed the asset", "embedding the asset"),
            ("flag the row", "flagging the row"),
            ("grep the logs", "grepping the logs"),
            ("plan the migration", "planning the migration"),
            ("quit the session", "quitting the session"),
            ("rescan the tree", "rescanning the tree"),
            ("reset the counter", "resetting the counter"),
            ("scan the folder", "scanning the folder"),
            ("split the string", "splitting the string"),
            ("submit the form", "submitting the form"),
            ("transfer the file", "transferring the file"),
            ("trim the buffer", "trimming the buffer"),
            ("unset the flag", "unsetting the flag"),
        ],
    )
    def test_consonant_doubling(self, text, expected):
        assert progressive_phrase(text) == expected


class TestBaseVerbPhrase:
    """Progressive purpose openers invert through the same curated verb tables."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("checking fork ship rules", "check fork ship rules"),
            ("Finding the deploy report", "find the deploy report"),
            ("Updating the GPU note", "update the GPU note"),
            ("running the suite", "run the suite"),
            ("making it so", "make it so"),
            ("lying down", "lie down"),
            ("tying the knot", "tie the knot"),
            ("seeing the diff", "see the diff"),
            ("checking", "check"),
        ],
    )
    def test_base_spelling(self, text, expected):
        assert display_progressive.base_verb_phrase(text) == expected

    def test_every_curated_progressive_round_trips_to_its_base(self):
        for verb in sorted(display_progressive._PROGRESSIVE_VERBS):
            progressive = progressive_phrase(verb)
            assert display_progressive.base_verb_phrase(progressive) == verb

    @pytest.mark.parametrize(
        "text",
        [
            "obliterating the cache",
            "the deploy config",
            "GPU mapping note",
            "4 spaces only",
            "API rate limits",
            "READING the config",
            "checking?",
            "   ",
            "",
        ],
    )
    def test_unknown_and_non_verb_openers_pass_through(self, text):
        assert display_progressive.base_verb_phrase(text) == text

    def test_only_first_word_is_inverted(self):
        assert display_progressive.base_verb_phrase(
            "checking the running job"
        ) == "check the running job"
