"""The viewer's first viewport: original prompt vs. live tail.

These tests execute the real inline ``<script>`` from
``plugins/claude_viewer/viewer/ui.html`` under node (see
``viewer_ui_harness.mjs`` for the DOM stub) — they do not read or regex the
source. Two first-load contracts are pinned:

* a run whose ``/api/head`` carries a non-empty ``prompt`` opens with the
  ``.prompt-card`` in view inside the ``#transcript`` scroller, never having
  visited the tail, and streamed events stay offscreen behind the new-lines
  pill until ``G`` / a pill click re-enters tail-follow;
* a cold reload with a ``#<run-stem>`` hash selects and loads that run — not
  the auto-select default — and reveals its prompt card;
* a run without a prompt keeps following the tail on first render.

A mutation test additionally proves the deep-link checks bite: the harness
fixture serves a second run the no-hash fallback would pick, so disabling
``runFromHash()`` must fail the deep-link scenario.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
UI_HTML = _REPO / "plugins" / "claude_viewer" / "viewer" / "ui.html"
HARNESS = Path(__file__).resolve().parent / "viewer_ui_harness.mjs"

_requires_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="requires node on PATH to execute the inline viewer script",
)

_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)


class _BalanceProbe(HTMLParser):
    """Stack-based tag-balance probe: collects every parse anomaly."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(
                f"</{tag}> closes {self.stack[-1] if self.stack else 'nothing'}"
            )
        else:
            self.stack.pop()


@pytest.fixture(scope="module")
def verdicts() -> dict:
    """Run every scenario through the real inline script in one node call."""
    run = subprocess.run(
        ["node", str(HARNESS), str(UI_HTML)],
        cwd=_REPO,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    assert run.returncode == 0, (
        "viewer UI harness failed:\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    return json.loads(run.stdout)


def _check(all_verdicts: dict, scenario: str, name: str) -> None:
    entry = all_verdicts.get(scenario, {}).get(name)
    assert entry is not None, (
        f"harness produced no verdict for {scenario}/{name}: {all_verdicts}"
    )
    assert entry["pass"], f"{scenario}/{name}: {entry['detail']}"


# ── prompt-bearing first load ──────────────────────────────────────────


@_requires_node
def test_prompt_run_opens_on_the_prompt_card(verdicts: dict) -> None:
    """The first viewport aims the #transcript scroller at .prompt-card."""
    _check(verdicts, "prompt_first_load", "prompt_card_is_in_view")
    _check(verdicts, "prompt_first_load", "viewport_is_not_at_bottom")


@_requires_node
def test_prompt_run_never_flashes_the_tail_first(verdicts: dict) -> None:
    """Tail-follow is dropped BEFORE the first flush, not corrected after.

    Catches the flash-then-jump-back shape: scrollToBottom() during the
    first batch followed by a reveal still ends on the prompt card, so only
    the write timeline (no bottom write before the reveal) can see it.
    """
    _check(verdicts, "prompt_first_load", "no_tail_flash_before_reveal")


@_requires_node
def test_prompt_run_does_not_page_older_history(verdicts: dict) -> None:
    """Landing near the top must not trip the scroll-up history pager."""
    _check(verdicts, "prompt_first_load", "no_history_page_from_reveal")


@_requires_node
def test_streamed_events_stay_behind_the_pill(verdicts: dict) -> None:
    """Post-reveal tail events render offscreen, advertised by the pill."""
    _check(verdicts, "prompt_first_load", "tail_events_stay_offscreen")
    _check(verdicts, "prompt_first_load", "pill_advertises_new_lines")


@_requires_node
def test_bottom_jump_paths_re_enter_tail_follow(verdicts: dict) -> None:
    """G and the pill click jump to the tail; follow resumes from there."""
    _check(verdicts, "prompt_first_load", "key_g_jumps_to_tail")
    _check(verdicts, "prompt_first_load", "tail_follow_resumes_after_jump")
    _check(verdicts, "prompt_first_load", "scrolled_up_keeps_new_events_offscreen")
    _check(verdicts, "prompt_first_load", "pill_click_jumps_to_tail")


@_requires_node
def test_deep_linked_reload_shows_the_prompt_again(verdicts: dict) -> None:
    """A fresh load with a #<run-stem> hash routes to that run and reveals
    its prompt card — the deep-link/share-URL path, from cold state.

    The harness fixture serves a second, active run that the no-hash
    auto-select fallback would pick instead, so these checks only pass when
    the hash itself routed the selection."""
    _check(verdicts, "prompt_deep_link_reload", "hashed_run_was_loaded")
    _check(verdicts, "prompt_deep_link_reload", "hash_routed_to_the_run")
    _check(verdicts, "prompt_deep_link_reload", "prompt_card_is_in_view")
    _check(verdicts, "prompt_deep_link_reload", "viewport_is_not_at_bottom")


@_requires_node
def test_deep_link_checks_bite_when_hash_routing_is_disabled(tmp_path: Path) -> None:
    """Disabling runFromHash() must fail the deep-link scenario.

    Mutation proof for the checks above: the harness fixture's auto-select
    fallback run differs from the hashed run, so replacing the real
    ``const hashed = runFromHash();`` assignment with a disabled one has to
    make the deep-link scenario exit nonzero. The needle must occur exactly
    once — if ui.html drifts so the mutation no longer applies cleanly, fail
    loudly instead of silently skipping the proof."""
    source = UI_HTML.read_text(encoding="utf-8")
    needle = "const hashed = runFromHash();"
    count = source.count(needle)
    assert count == 1, f"expected exactly one {needle!r} in ui.html, found {count}"

    mutated = tmp_path / "ui_hash_routing_disabled.html"
    mutated.write_text(
        source.replace(needle, "const hashed = null;"), encoding="utf-8"
    )

    run = subprocess.run(
        ["node", str(HARNESS), str(mutated), "prompt_deep_link_reload"],
        cwd=_REPO,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    assert run.returncode != 0, (
        "deep-link scenario stayed green with hash routing disabled:\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    scenario = json.loads(run.stdout).get("prompt_deep_link_reload", {})
    for name in ("hashed_run_was_loaded", "hash_routed_to_the_run"):
        entry = scenario.get(name)
        assert entry is not None, f"no verdict for {name}: {scenario}"
        assert entry["pass"] is False, f"{name} unexpectedly passed: {entry['detail']}"


# ── prompt-less first load keeps tail-follow ───────────────────────────


@_requires_node
def test_no_prompt_run_keeps_following_the_tail(verdicts: dict) -> None:
    """Without a prompt the first render lands on (and keeps) the tail."""
    _check(verdicts, "no_prompt_tail_follow", "first_render_lands_at_bottom")
    _check(verdicts, "no_prompt_tail_follow", "pill_hidden_after_first_render")
    _check(verdicts, "no_prompt_tail_follow", "tail_stays_followed")


# ── the file the browser actually loads stays loadable ─────────────────


def test_ui_html_parses_without_tag_errors() -> None:
    """The edited markup stays balanced: zero tag-balance parse errors."""
    probe = _BalanceProbe()
    probe.feed(UI_HTML.read_text(encoding="utf-8"))
    probe.close()
    assert probe.problems == [], f"unbalanced markup: {probe.problems}"
    assert probe.stack == [], f"unclosed tags: {probe.stack}"
