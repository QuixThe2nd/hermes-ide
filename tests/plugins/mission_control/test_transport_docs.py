"""The shipped help and the server route contract must describe the
transport the code actually runs.

Every composer turn is admitted as a run on the core API server
(POST /v1/runs, profile-scoped and authenticated), and every clarify
card is that run's mid-turn question bridged back through the core's
per-session clarify routes. It was not always so: turns were once
spawned as ``hermes chat --oneshot`` CLI subprocesses, and when the
transport switched the prose kept describing the dead one. These
checks pin the two surfaces a clean install actually reads — the
``hermes mission_control --help`` description registered by the
plugin and the server module's route-contract docstring — to the live
transport, and hold the line module-wide: an "oneshot" mention may
survive only where it explicitly contrasts the retired CLI behavior
(those explanatory comments are deliberate history, asserted below),
never as a claim about how a turn runs today.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = REPO_ROOT / "plugins" / "mission_control"
SERVER_PY = PLUGIN_DIR / "server.py"

# Claims that describe the retired CLI transport as if it were the
# current one — the exact wording the stale docstrings carried. None
# may return to the help description or the route contract.
STALE_CLAIMS = (
    "oneshot",                 # the retired transport's own name
    "hermes chat",             # ...its two command spellings
    "hermes --resume",
    "through the hermes cli",
    "stdout",                  # ...and its plumbing vocabulary
    "stderr",
    "exit status",
    "exit-code",
    "tracked and terminated",  # the retired child-process registry
)

# The only legal survivors in server.py: sentences that explicitly
# contrast the old CLI transport with the current run transport
# (whitespace-normalized, so a cue may wrap across source lines).
CONTRAST_CUES = (
    "instead of spawning the oneshot cli",
    "never as an oneshot cli",
    "oneshot cli cap had",
    "oneshot era",
)


class _Ctx:
    """The slice of the plugin registration context these tests need."""

    def __init__(self):
        self.entry = None

    def register_cli_command(self, **kwargs):
        self.entry = kwargs

    def register_hook(self, *args, **kwargs):
        pass


def _registered_description() -> str:
    """The help description a clean install prints for the command."""
    import plugins.mission_control as plugin

    ctx = _Ctx()
    plugin.register(ctx)
    assert ctx.entry is not None
    return ctx.entry["description"]


def _server_docstring() -> str:
    """The route-contract docstring, read without importing the module."""
    return ast.get_docstring(ast.parse(SERVER_PY.read_text(
        encoding="utf-8")))


def test_help_description_names_the_core_run_transport():
    description = _registered_description().lower()
    assert "post /v1/runs" in description
    assert "clarify" in description
    for claim in STALE_CLAIMS:
        assert claim not in description, claim


def test_route_contract_docstring_names_the_core_run_transport():
    doc = _server_docstring().lower()
    assert "post /v1/runs" in doc
    # The clarify half of the transport is the bridged core card route.
    assert "/api/sessions/{id}/clarify" in doc
    for claim in STALE_CLAIMS:
        assert claim not in doc, claim


def test_oneshot_mentions_only_as_explicit_contrasts():
    lines = SERVER_PY.read_text(encoding="utf-8").splitlines()
    hits = [i for i, line in enumerate(lines) if "oneshot" in line]
    assert hits, "the historical contrast comments went missing too"
    for i in hits:
        window = " ".join(
            " ".join(lines[max(0, i - 2):i + 3]).split()).lower()
        assert any(cue in window for cue in CONTRAST_CUES), (
            "server.py line %d mentions the retired oneshot transport "
            "outside an explicit historical contrast: %r"
            % (i + 1, lines[i]))


def test_old_cli_bug_contrast_survives():
    src = SERVER_PY.read_text(encoding="utf-8").lower()
    # Why the CLI transport was retired is deliberate history: its -q
    # callback auto-answered the questions the clarify card now serves.
    assert "auto-answers instead" in src
    assert "never as an oneshot cli subprocess" in src
