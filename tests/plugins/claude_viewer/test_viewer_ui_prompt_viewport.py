"""The viewer's first viewport: original prompt vs. live tail.

These tests execute the real inline ``<script>`` from
``plugins/claude_viewer/viewer/ui.html`` under node (see
``viewer_ui_harness.mjs`` for the DOM stub) — they do not read or regex the
source. Two first-load contracts are pinned:

* a run whose ``/api/head`` carries a non-empty ``prompt`` opens with the
  ``.prompt-card`` in view inside the ``#transcript`` scroller, never having
  visited the tail, and streamed events stay offscreen behind the new-lines
  pill until ``G`` / a pill click re-enters tail-follow;
* the initial head batch is history, never live output: the new-lines pill
  stays hidden after the reveal and counts only genuinely streamed lines;
* a cold reload with a ``#<run-stem>`` hash selects and loads that run — not
  the auto-select default — and reveals its prompt card;
* a run without a prompt keeps following the tail on first render, even when
  it is selected right after a prompt-bearing run in the same session;
* the initial load is completion-driven, not timer-driven: with
  ``requestAnimationFrame`` stalled 250 ms (hidden/background/busy tab) the
  prompt-first contract above still holds — the reveal waits for the actual
  head flush instead of a fixed delay.

Load-identity contracts are pinned as well:

* an empty first page schedules no flush and settles without one: the load
  goes straight to the tail cursor and the tail long-poll instead of hanging
  on a head flush that never comes;
* render coalescing: when a prior scheduled frame already exists before the
  current run's head scheduling, the current generation's head-flush waiter
  still resolves when that shared frame paints;
* overlapping A→B→A selections with deferred head responses resolved in an
  adversarial order before the delayed frame: only the latest load renders,
  reveals, and starts exactly one tail loop. Filename equality cannot tell
  the stale A load apart from the current one — only the per-load generation
  token can.

Snapshot/cursor contracts:

* ``/api/head`` carries ``tail_offset``, the tail cursor captured with the
  head snapshot; the tail loop starts there and no size probe fires. Events
  appended after the head snapshot but before a delayed flush still arrive
  exactly once through tail polling — never skipped (a later size probe
  would jump past them) and never duplicated;
* a legacy head payload without ``tail_offset`` falls back to exactly one
  size probe and follows from there.

History-paging load identity:

* a stale A1 history response landing mid-A→B→A2 must not prepend into A2,
  flush (releasing A2's head waiter early), adjust the viewport, or stand
  down A2's history-loading state: only A2 renders/reveals/polls, and A2's
  waiter is released only by A2's own head flush.

Mutation tests prove the checks bite, each an exact destructive edit of the
current shipped ui.html (never a moving ``HEAD`` baseline, so the suite
survives the fix being committed): disabling ``runFromHash()`` fails the
deep-link scenario; replacing the completion-driven head-flush wait with the
pre-fix fixed 120 ms reveal timer fails prompt-first at a 250 ms frame stall
while passing undelayed; removing the loadHead generation rechecks fails the
A→B→A overlap; removing the post-flush recheck alone fails the tail-loop
count; anchoring the tail at a post-flush size probe (the pre-fix shape)
fails the head→tail gap scenario; and removing the loadOlder generation
guard fails the stale-history A→B→A scenario. A guard test keeps this file
free of moving-HEAD source extraction.
"""

from __future__ import annotations

import json
import os
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
def test_initial_history_is_never_counted_as_new(verdicts: dict) -> None:
    """The head batch must not feed the new-lines pill in prompt-first mode.

    Pins the exact count: after the reveal the pill is hidden, and two
    streamed tail lines read "2 new lines" — not head-size + 2."""
    _check(verdicts, "prompt_first_load", "initial_head_not_counted_as_new")
    _check(verdicts, "prompt_first_load", "pill_advertises_new_lines")


@_requires_node
def test_bottom_jump_paths_re_enter_tail_follow(verdicts: dict) -> None:
    """G and the pill click jump to the tail; follow resumes from there."""
    _check(verdicts, "prompt_first_load", "key_g_jumps_to_tail")
    _check(verdicts, "prompt_first_load", "tail_follow_resumes_after_jump")
    _check(verdicts, "prompt_first_load", "scrolled_up_keeps_new_events_offscreen")
    _check(verdicts, "prompt_first_load", "pill_click_jumps_to_tail")


# ── delayed requestAnimationFrame (hidden/background/busy tab) ─────────

_RAF_DELAY_ENV = "VIEWER_UI_RAF_DELAY_MS"


def _run_prompt_first_at_raf_delay(html: Path, delay_ms: int) -> subprocess.CompletedProcess:
    """One harness run of prompt_first_load with a stalled animation frame."""
    return subprocess.run(
        ["node", str(HARNESS), str(html), "prompt_first_load"],
        cwd=_REPO,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
        env={**os.environ, _RAF_DELAY_ENV: str(delay_ms)},
    )


@_requires_node
def test_prompt_first_load_survives_a_delayed_animation_frame() -> None:
    """A 250 ms rAF stall must not break the prompt-first contract.

    Regression for the confirmed race where loadHead() revealed on a fixed
    120 ms timer while the head flush was still queued behind a stalled
    requestAnimationFrame: the reveal then clamped to scrollTop=0 (prompt not
    in view), tripped the scroll-up history pager, and the head batch fell
    out of the initial-history window. The repair is completion-driven — the
    reveal awaits the actual head flush — so the full scenario must stay
    green at 250 ms."""
    run = _run_prompt_first_at_raf_delay(UI_HTML, 250)
    assert run.returncode == 0, (
        "prompt-first load broke with a 250 ms rAF stall:\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    delayed = json.loads(run.stdout)
    # The exact behavioral pins from the race report: prompt visible, no
    # unintended history request, initial history not counted, and exactly
    # two later live lines.
    _check(delayed, "prompt_first_load", "prompt_card_is_in_view")
    _check(delayed, "prompt_first_load", "no_history_page_from_reveal")
    _check(delayed, "prompt_first_load", "initial_head_not_counted_as_new")
    _check(delayed, "prompt_first_load", "pill_advertises_new_lines")


_HEAD_FLUSH_WAIT = "      if (headFlushed) await headFlushed;\n"
_FIXED_TIMER_REVEAL = (
    "      await new Promise((r) => setTimeout(r, 120));"
    "   // pre-fix shape: reveal on a fixed timer\n"
)


@_requires_node
def test_delayed_frame_bites_on_fixed_timer_reveal_mutation(tmp_path: Path) -> None:
    """Destructive baseline: the pre-fix reveal shape fails at 250 ms rAF.

    The confirmed race was a reveal on a fixed 120 ms timer while the head
    flush was still queued behind a stalled requestAnimationFrame. Mutating
    the shipped UI back to that exact shape — one precise line swap, nothing
    else — must fail prompt-first at 250 ms *for behavior*: the named checks
    fail, not a generic timeout or invalid JSON. The same mutated file must
    pass the same checks undelayed, so the 250 ms failure isolates the race
    rather than a broken mutation. The baseline is derived from the current
    source, so it stays valid after the fix is committed."""
    source = UI_HTML.read_text(encoding="utf-8")
    count = source.count(_HEAD_FLUSH_WAIT)
    assert count == 1, f"expected exactly one head-flush wait in ui.html, found {count}"
    mutated = tmp_path / "ui_fixed_timer_reveal.html"
    mutated.write_text(source.replace(_HEAD_FLUSH_WAIT, _FIXED_TIMER_REVEAL), encoding="utf-8")

    undelayed = _run_prompt_first_at_raf_delay(mutated, 0)
    assert undelayed.returncode == 0, (
        "fixed-timer mutation broke prompt-first even without a frame stall:\n"
        f"stdout:\n{undelayed.stdout}\nstderr:\n{undelayed.stderr}"
    )
    undelayed_verdicts = json.loads(undelayed.stdout)
    for name in ("prompt_card_is_in_view", "no_history_page_from_reveal"):
        _check(undelayed_verdicts, "prompt_first_load", name)

    delayed = _run_prompt_first_at_raf_delay(mutated, 250)
    assert delayed.returncode != 0, (
        "fixed-timer reveal mutation stayed green with a 250 ms rAF stall:\n"
        f"stdout:\n{delayed.stdout}\nstderr:\n{delayed.stderr}"
    )
    scenario = json.loads(delayed.stdout).get("prompt_first_load", {})
    assert "harness_error" not in scenario or any(
        name != "harness_error" and not entry["pass"]
        for name, entry in scenario.items()
    ), f"mutation failed only via harness error, not behavior: {scenario}"
    for name in ("prompt_card_is_in_view", "no_history_page_from_reveal"):
        entry = scenario.get(name)
        assert entry is not None, f"no verdict for {name}: {scenario}"
        assert entry["pass"] is False, (
            f"{name} unexpectedly passed on the fixed-timer mutation: {entry['detail']}"
        )


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


@_requires_node
def test_no_prompt_run_after_prompt_run_follows_the_tail(verdicts: dict) -> None:
    """Follow mode must not leak across run selections in one session.

    Same-VM sequential scenario: a prompt-bearing run opens off-tail, then a
    no-prompt run is selected from the sidebar. The second run has to land on
    and keep following its tail with no new-lines pill — a fresh-VM no-prompt
    scenario alone cannot see the inherited off-tail state."""
    _check(verdicts, "prompt_then_no_prompt_switch", "prompt_run_starts_off_tail")
    _check(verdicts, "prompt_then_no_prompt_switch", "no_prompt_run_lands_at_tail")
    _check(verdicts, "prompt_then_no_prompt_switch", "no_prompt_run_has_no_pill")
    _check(verdicts, "prompt_then_no_prompt_switch", "no_prompt_run_follows_tail")


# ── empty head: the load settles without any flush ─────────────────────


@_requires_node
def test_empty_head_settles_into_tail_polling(verdicts: dict) -> None:
    """An empty first page schedules no flush, so there is nothing to wait
    for: the load must go straight from the head snapshot's tail cursor to
    exactly one tail long-poll (no size probe) instead of hanging on a head
    flush that never comes (settle() in the harness times out loudly if it
    does)."""
    _check(verdicts, "empty_head_settles", "empty_head_starts_tail_polling")
    _check(verdicts, "empty_head_settles", "empty_head_has_no_prompt_card")
    _check(verdicts, "empty_head_settles", "empty_head_hides_the_pill")


@_requires_node
def test_empty_head_then_follows_live_tail(verdicts: dict) -> None:
    """Live lines arriving after the empty head render and follow normally."""
    _check(verdicts, "empty_head_settles", "tail_follows_after_empty_head")


# ── overlapping loads: load-generation identity ────────────────────────


def _run_scenarios(
    html: Path, scenario_names: list[str], raf_delay_ms: int | None = None
) -> subprocess.CompletedProcess:
    """One harness run of the named scenarios, optionally with a stalled rAF."""
    env = None
    if raf_delay_ms is not None:
        env = {**os.environ, _RAF_DELAY_ENV: str(raf_delay_ms)}
    return subprocess.run(
        ["node", str(HARNESS), str(html), *scenario_names],
        cwd=_REPO,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
        env=env,
    )


def _assert_failed_for_behavior(scenario: dict, *names: str) -> None:
    """The named checks must exist and have failed — a generic harness error
    or a missing verdict does not count as the scenario biting."""
    for name in names:
        entry = scenario.get(name)
        assert entry is not None, f"no verdict for {name}: {scenario}"
        assert entry["pass"] is False, (
            f"{name} unexpectedly passed: {entry['detail']}"
        )


@_requires_node
def test_coalesced_render_resolves_the_current_generation(verdicts: dict) -> None:
    """A prior scheduled frame exists before the current head scheduling:
    the current run's head batch coalesces onto it (no second frame) and the
    current generation's head-flush waiter still resolves when it paints."""
    _check(verdicts, "coalesced_head_flush", "current_head_coalesced_onto_prior_frame")
    _check(verdicts, "coalesced_head_flush", "current_generation_rendered")
    _check(verdicts, "coalesced_head_flush", "current_generation_revealed")
    _check(verdicts, "coalesced_head_flush", "coalesced_load_polled_once_without_size_probe")
    _check(verdicts, "coalesced_head_flush", "coalesced_load_hides_the_pill")


@_requires_node
def test_superseded_aba_overlap_only_the_latest_load_survives(verdicts: dict) -> None:
    """A→B→A with deferred heads resolved adversarially before the delayed
    frame: stale A1 (same filename as current A2) and stale B must not
    render, reveal, or poll — only A2 does, exactly once, starting its tail
    at the head snapshot cursor with no size probe."""
    _check(verdicts, "superseded_aba_overlap", "current_head_coalesced_onto_prior_frame")
    _check(verdicts, "superseded_aba_overlap", "only_latest_a_renders")
    _check(verdicts, "superseded_aba_overlap", "only_latest_a_reveals")
    _check(verdicts, "superseded_aba_overlap", "exactly_one_tail_loop")
    _check(verdicts, "superseded_aba_overlap", "no_size_probe_when_head_carries_cursor")
    _check(verdicts, "superseded_aba_overlap", "tail_started_from_head_cursor")
    _check(verdicts, "superseded_aba_overlap", "no_stale_error_marker")
    _check(verdicts, "superseded_aba_overlap", "pill_stays_hidden")


@_requires_node
def test_overlap_scenarios_survive_a_delayed_animation_frame() -> None:
    """The overlap, coalescing, empty-head, gap, and stale-history scenarios
    must all stay green with requestAnimationFrame stalled 250 ms — the load
    sequence is completion-driven and snapshot-anchored, so a delayed frame
    cannot reorder it or skip gap events."""
    names = [
        "superseded_aba_overlap",
        "coalesced_head_flush",
        "empty_head_settles",
        "head_tail_gap",
        "stale_history_aba",
    ]
    run = _run_scenarios(UI_HTML, names, raf_delay_ms=250)
    assert run.returncode == 0, (
        "overlap scenarios broke with a 250 ms rAF stall:\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    delayed = json.loads(run.stdout)
    _check(delayed, "superseded_aba_overlap", "only_latest_a_renders")
    _check(delayed, "superseded_aba_overlap", "exactly_one_tail_loop")
    _check(delayed, "coalesced_head_flush", "current_generation_rendered")
    _check(delayed, "empty_head_settles", "empty_head_starts_tail_polling")
    _check(delayed, "head_tail_gap", "gap_events_rendered_exactly_once")
    _check(delayed, "stale_history_aba", "only_a2_renders_after_stale_history")


_GENERATION_RECHECK_FETCH = (
    "      if (gen !== state.loadGen) return;"
    "   // generation recheck: /api/head fetch\n"
)
_GENERATION_RECHECK_PAYLOAD = (
    "      if (gen !== state.loadGen) return;"
    "   // generation recheck: /api/head payload\n"
)
_GENERATION_RECHECK_FLUSH = (
    "      if (gen !== state.loadGen) return;"
    "   // generation recheck: head flush painted\n"
)


def _ui_without_lines(tmp_path: Path, name: str, removals: list[str]) -> Path:
    """ui.html with the given lines deleted; each needle must occur exactly
    once so a drift in ui.html fails loudly instead of skipping the proof."""
    source = UI_HTML.read_text(encoding="utf-8")
    for needle in removals:
        count = source.count(needle)
        assert count == 1, f"expected exactly one {needle!r} in ui.html, found {count}"
        source = source.replace(needle, "")
    mutated = tmp_path / name
    mutated.write_text(source, encoding="utf-8")
    return mutated


@_requires_node
def test_overlap_bites_without_generation_bound_resolver_ownership(tmp_path: Path) -> None:
    """Removing the head-response generation rechecks must fail the overlap.

    Those rechecks are what keep a stale load from claiming the
    generation-bound head-flush waiter: without them the superseded B load
    proceeds, overwrites the current A load's waiter (leaving it hung), and
    schedules its own batch — the transcript then contains B's lines on top
    of A's, which ``only_latest_a_renders`` catches."""
    mutated = _ui_without_lines(
        tmp_path,
        "ui_no_generation_recheck_on_head_response.html",
        [_GENERATION_RECHECK_FETCH, _GENERATION_RECHECK_PAYLOAD],
    )
    run = _run_scenarios(mutated, ["superseded_aba_overlap"])
    assert run.returncode != 0, (
        "overlap scenario stayed green without the head-response generation "
        f"rechecks:\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    scenario = json.loads(run.stdout).get("superseded_aba_overlap", {})
    _assert_failed_for_behavior(scenario, "only_latest_a_renders")


@_requires_node
def test_overlap_bites_without_generation_recheck_after_head_flush(tmp_path: Path) -> None:
    """Removing the post-flush generation recheck must fail the overlap.

    The B selection releases the parked stale A1 waiter on purpose; the
    recheck after the wait is what stops A1's continuation from revealing and
    starting a tail loop on behalf of the newer A2 load (A1 even polls under
    whatever file is selected by then). Without it the stale continuation
    fires a second long-poll, which ``exactly_one_tail_loop`` catches."""
    mutated = _ui_without_lines(
        tmp_path,
        "ui_no_generation_recheck_after_head_flush.html",
        [_GENERATION_RECHECK_FLUSH],
    )
    run = _run_scenarios(mutated, ["superseded_aba_overlap"])
    assert run.returncode != 0, (
        "overlap scenario stayed green without the post-flush generation "
        f"recheck:\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    scenario = json.loads(run.stdout).get("superseded_aba_overlap", {})
    _assert_failed_for_behavior(scenario, "exactly_one_tail_loop")


@_requires_node
def test_overlap_bites_without_any_load_generation_rechecks(tmp_path: Path) -> None:
    """Destructive baseline: stripping every loadHead generation recheck
    reproduces the pre-generation-fix UI, where filename equality was all
    that told loads apart — and fails the overlap for behavior.

    Stale B proceeds past the head response and paints into A2's buffer
    (``only_latest_a_renders``); stale A1, released by the B selection,
    continues into a second tail loop (``exactly_one_tail_loop``). The
    baseline is derived from the current source, so it stays valid after the
    fix is committed."""
    mutated = _ui_without_lines(
        tmp_path,
        "ui_no_generation_rechecks.html",
        [
            _GENERATION_RECHECK_FETCH,
            _GENERATION_RECHECK_PAYLOAD,
            _GENERATION_RECHECK_FLUSH,
        ],
    )
    run = _run_scenarios(mutated, ["superseded_aba_overlap"])
    assert run.returncode != 0, (
        "overlap scenario stayed green with every load-generation recheck "
        f"removed:\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    scenario = json.loads(run.stdout).get("superseded_aba_overlap", {})
    assert "harness_error" not in scenario or any(
        name != "harness_error" and not entry["pass"]
        for name, entry in scenario.items()
    ), f"mutation failed only via harness error, not behavior: {scenario}"
    _assert_failed_for_behavior(
        scenario, "only_latest_a_renders", "exactly_one_tail_loop"
    )


# ── head→tail gap: the tail starts at the head snapshot's cursor ────────


@_requires_node
def test_gap_events_arrive_exactly_once_through_tail_polling(verdicts: dict) -> None:
    """Events appended after the /api/head snapshot but before the delayed
    flush all arrive exactly once via tail polling — anchored at the
    snapshot's tail_offset, with no later size probe that could skip them."""
    _check(verdicts, "head_tail_gap", "tail_started_from_head_cursor")
    _check(verdicts, "head_tail_gap", "no_size_probe_when_head_carries_cursor")
    _check(verdicts, "head_tail_gap", "gap_events_rendered_exactly_once")


_TAIL_CURSOR_BRANCH = (
    "      if (typeof data.tail_offset === 'number') {\n"
    "        state.tailOffset = data.tail_offset;\n"
    "      } else {\n"
    "        state.tailOffset = await getFileSize(file);\n"
    "        if (gen !== state.loadGen) return;"
    "   // generation recheck: file-size probe\n"
    "      }\n"
)
_TAIL_CURSOR_PROBE_ONLY = (
    "      state.tailOffset = await getFileSize(file);\n"
    "      if (gen !== state.loadGen) return;   // generation recheck: file-size probe\n"
)


@_requires_node
def test_gap_bites_when_tail_anchors_at_a_later_size_probe(tmp_path: Path) -> None:
    """Mutation: always probe the size after the flush (the pre-fix shape).

    The probe returns a size captured after the gap events landed, so the
    tail starts past them and they are skipped forever: the gap scenario must
    exit nonzero with the cursor, probe-count, and exactly-once checks
    failing for behavior — not via a timeout (the scenario quiesces and
    counts instead of waiting for rows that never come)."""
    source = UI_HTML.read_text(encoding="utf-8")
    count = source.count(_TAIL_CURSOR_BRANCH)
    assert count == 1, f"expected exactly one tail-cursor branch in ui.html, found {count}"
    mutated = tmp_path / "ui_tail_anchored_at_size_probe.html"
    mutated.write_text(
        source.replace(_TAIL_CURSOR_BRANCH, _TAIL_CURSOR_PROBE_ONLY), encoding="utf-8"
    )

    run = _run_scenarios(mutated, ["head_tail_gap"])
    assert run.returncode != 0, (
        "gap scenario stayed green with the tail anchored at a later size "
        f"probe:\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    scenario = json.loads(run.stdout).get("head_tail_gap", {})
    _assert_failed_for_behavior(
        scenario,
        "tail_started_from_head_cursor",
        "no_size_probe_when_head_carries_cursor",
        "gap_events_rendered_exactly_once",
    )


@_requires_node
def test_legacy_head_without_cursor_falls_back_to_one_size_probe(verdicts: dict) -> None:
    """Compatibility: a head payload without tail_offset uses the old size
    probe exactly once and follows the tail from the probed offset."""
    _check(verdicts, "legacy_head_fallback", "legacy_head_triggers_one_size_probe")
    _check(verdicts, "legacy_head_fallback", "legacy_tail_starts_from_probed_offset")
    _check(verdicts, "legacy_head_fallback", "legacy_tail_still_follows")


# ── stale history page surviving A→B→A ──────────────────────────────────


@_requires_node
def test_stale_history_page_cannot_corrupt_aba(verdicts: dict) -> None:
    """A1's deferred history response lands mid-A→B→A2, before A2's held
    flush: it must not paint, must not release A2's head waiter (no early
    reveal, no early tail), and afterwards only A2 has rendered, revealed,
    and polled — exactly once each."""
    _check(verdicts, "stale_history_aba", "stale_history_did_not_paint_before_a2_flush")
    _check(verdicts, "stale_history_aba", "a2_waiter_not_released_by_stale_history")
    _check(verdicts, "stale_history_aba", "only_a2_renders_after_stale_history")
    _check(verdicts, "stale_history_aba", "a2_revealed_after_own_flush")
    _check(verdicts, "stale_history_aba", "a2_tail_polls_exactly_once")


_HISTORY_RECHECK_FETCH = (
    "      if (gen !== state.loadGen) return;   // generation recheck: history fetch\n"
)
_HISTORY_RECHECK_PAYLOAD = (
    "      if (gen !== state.loadGen) return;   // generation recheck: history payload\n"
)


@_requires_node
def test_stale_history_bites_without_the_history_generation_guard(tmp_path: Path) -> None:
    """Mutation: remove the loadOlder generation guard.

    Filename equality cannot tell the stale A1 history load apart from the
    current A2 run, so without the guard the stale response prepends into
    A2's buffer and calls a synchronous flushRender() that releases A2's
    head-flush waiter: the stale rows paint ahead of A2's held frame and A2
    reveals early. Both checks must fail for behavior."""
    mutated = _ui_without_lines(
        tmp_path,
        "ui_no_history_generation_guard.html",
        [_HISTORY_RECHECK_FETCH, _HISTORY_RECHECK_PAYLOAD],
    )
    run = _run_scenarios(mutated, ["stale_history_aba"])
    assert run.returncode != 0, (
        "stale-history scenario stayed green without the history generation "
        f"guard:\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    scenario = json.loads(run.stdout).get("stale_history_aba", {})
    assert "harness_error" not in scenario or any(
        name != "harness_error" and not entry["pass"]
        for name, entry in scenario.items()
    ), f"mutation failed only via harness error, not behavior: {scenario}"
    _assert_failed_for_behavior(
        scenario,
        "stale_history_did_not_paint_before_a2_flush",
        "a2_waiter_not_released_by_stale_history",
        "only_a2_renders_after_stale_history",
    )


# ── suite hygiene: no moving-HEAD baselines ──────────────────────────────


def test_suite_has_no_moving_head_source_extraction() -> None:
    """This file must not derive test baselines from git HEAD (or any branch
    position): such baselines pass only while a fix is uncommitted and
    self-invalidate once it lands. Needles are built by concatenation so the
    guard does not match its own source."""
    source = Path(__file__).read_text(encoding="utf-8")
    forbidden = [
        "git" + " show",      # extracting a committed blob as a baseline
        "HEAD" + ":",         # head-colon-path blob syntax
        "HEAD" + "^",         # parent-of-head positioning
        "HEAD" + "~",         # ancestor-of-head positioning
    ]
    for needle in forbidden:
        assert needle not in source, (
            f"moving-HEAD source extraction found in this test file: {needle!r}; "
            "baselines must be exact destructive mutations of the shipped UI"
        )


# ── the file the browser actually loads stays loadable ─────────────────


def test_ui_html_parses_without_tag_errors() -> None:
    """The edited markup stays balanced: zero tag-balance parse errors."""
    probe = _BalanceProbe()
    probe.feed(UI_HTML.read_text(encoding="utf-8"))
    probe.close()
    assert probe.problems == [], f"unbalanced markup: {probe.problems}"
    assert probe.stack == [], f"unclosed tags: {probe.stack}"
