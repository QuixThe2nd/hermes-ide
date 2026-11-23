"""Unit tests for scripts/divergence_badges.py — formatting and marker replacement.

No network: only the pure helpers are exercised. These intentionally do NOT
freeze the live repo's current percentages (no change-detector assertions).
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

_SPEC = importlib.util.spec_from_file_location(
    "divergence_badges", Path(__file__).resolve().parents[2] / "scripts" / "divergence_badges.py"
)
db = importlib.util.module_from_spec(_SPEC)
sys.modules["divergence_badges"] = db
_SPEC.loader.exec_module(db)


@pytest.mark.parametrize(
    "value,digits,expected",
    [
        (55.4, 0, "55"),  # .4 rounds down at 0 digits
        (22.4, 0, "22"),
        (39.6, 0, "40"),  # .6 rounds up
        (54.5, 0, "55"),  # half-up, not banker's (banker's would give 54)
        (4.31, 1, "4.3"),
        (3.446, 1, "3.4"),  # truncation-prone case rounds, not floors
        (4.35, 1, "4.4"),  # half-up at 1 decimal (banker's would give 4.3)
    ],
)
def test_round_half_up(value, digits, expected):
    assert str(db.round_half_up(value, digits)) == expected


def test_format_pct_half_up():
    assert db.format_pct(0.554) == "55"
    assert db.format_pct(0.224) == "22"
    assert db.format_pct(0.396) == "40"


def test_ratio_line_deleted_per_added():
    # removed 4 for every 1 added, tree 55 -> 33 = 40% smaller
    line = db.format_ratio_line(removed=200, added=50, original=200, current=110)
    assert line == "4.0 deleted per 1 added, 45% smaller"


def test_ratio_line_added_per_deleted():
    # added 4.3 per deleted, tree 100 -> 133 = 33% larger
    line = db.format_ratio_line(removed=10, added=43, original=100, current=133)
    assert line == "4.3 added per 1 deleted, 33% larger"


def test_ratio_line_half_up_ratio():
    # 3.446 deleted per 1 added must be 3.4, not 3.5/3.3
    line = db.format_ratio_line(removed=1723, added=500, original=2000, current=777)
    assert line.startswith("3.4 deleted per 1 added")


def test_ratio_line_unchanged():
    assert db.format_ratio_line(0, 0, 100, 100) == "unchanged"


def test_is_test_path_and_code_path():
    assert db.is_test_path("tests/agent/test_x.py")
    assert db.is_test_path("src/test/foo.py")
    assert db.is_test_path("test_foo.py")
    assert db.is_test_path("pkg/foo_test.py")
    assert db.is_test_path("pkg/foo.test.ts")
    assert db.is_test_path("pkg/foo.spec.js")
    assert not db.is_test_path("agent/turn_loop.py")
    assert db.is_code_path("agent/turn_loop.py")
    assert db.is_code_path("ui-tui/src/app.tsx")
    assert db.is_code_path("scripts/run.sh")
    assert not db.is_code_path("README.md")
    assert not db.is_code_path("tests/agent/test_x.py")  # tests are not code here


def test_replace_block_preserves_surroundings():
    doc = "before\n<!-- divergence-badges:start -->\nOLD\n<!-- divergence-badges:end -->\nafter"
    out = db.replace_block(doc, "<!-- divergence-badges:start -->\nNEW\n<!-- divergence-badges:end -->")
    assert out == "before\n<!-- divergence-badges:start -->\nNEW\n<!-- divergence-badges:end -->\nafter"


def test_replace_block_requires_markers_in_order():
    with pytest.raises(ValueError):
        db.replace_block("no markers here", "block")


def test_render_block_is_idempotent():
    ours = {"pct_gone": 40, "pct_new": 10, "ratio_line": "2.0 deleted per 1 added, 40% smaller"}
    theirs = {"pct_gone": 5, "pct_new": 2, "ratio_line": "1.5 added per 1 deleted, 3% larger"}
    block = db.render_block(ours, theirs)
    once = db.replace_block(db.replace_block("x\n" + "\n".join(db.README_MARKERS) + "\ny", block), block)
    assert once.count("<!-- divergence-badges:start -->") == 1
    assert "**hermes-ide**" in block and "**upstream hermes**" in block
    assert "img.shields.io/badge" in block
    assert block.startswith(db.README_MARKERS[0]) and block.endswith(db.README_MARKERS[1])

def test_badge_percent_is_encoded_once():
    # A raw "%" is an invalid percent-escape and shields.io returns 400.
    url = db._badge("delta", "4.4 deleted per 1 added, 44% smaller", "blue")
    assert "44%25_smaller" in url
    assert "44%_smaller" not in url
    assert "%2525" not in url
    side = db.render_side(
        "hermes-ide",
        {"pct_gone": 57, "pct_new": 23, "ratio_line": "4.4 deleted per 1 added, 44% smaller"},
        "blue",
    )
    assert "57%25" in side and "23%25" in side
    assert "57%2525" not in side


_REPO = Path(__file__).resolve().parents[2]
_BADGE_WORKFLOW = _REPO / ".github" / "workflows" / "divergence-badges.yml"


def _badge_workflow():
    return yaml.safe_load(_BADGE_WORKFLOW.read_text(encoding="utf-8-sig"))


def _badge_step_runs(workflow):
    """run: blocks of the steps that invoke divergence_badges.py."""
    runs = []
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "divergence_badges.py" in str(step.get("run", "")):
                runs.append(step["run"])
    return runs


def test_workflow_computes_on_fetched_tip_not_checkout():
    """Every badge-compute step must fetch origin/main and land on it BEFORE
    the script runs. The badge commit used to be parented on the checkout-time
    SHA; once main moved, every push died with "! [rejected] main -> main
    (fetch first)". The README edit is the only local change, so the fix is
    fetch + hard-reset onto the tip + recompute — never a rebase.
    """
    workflow = _badge_workflow()
    runs = _badge_step_runs(workflow)
    assert runs, "no step invokes divergence_badges.py"
    for run in runs:
        compute = run.index("divergence_badges.py")
        assert run.index("git fetch origin main") < compute
        assert run.index("git reset --hard origin/main") < compute
        # the stale edit is dropped before the reset so it can never be lost by it
        assert run.index("git checkout -- README.md") < run.index("git reset --hard origin/main")


def test_workflow_push_retry_is_bounded_and_fails_loudly():
    for run in _badge_step_runs(_badge_workflow()):
        assert "for attempt in 1 2 3" in run  # bounded, not infinite
        assert "::error::" in run  # exhausted retries annotate the job, visibly
        assert re.search(r"\bexit 1\b", run)
        # success paths exit inside the loop; nothing falls through after it
        assert run.rstrip().endswith("exit 1")


def test_workflow_keeps_trigger_and_push_prerequisites():
    workflow = _badge_workflow()
    # `on:` parses as the YAML 1.1 boolean True; the badge commit must not retrigger
    assert workflow[True]["push"]["paths-ignore"] == ["README.md"]
    assert "workflow_dispatch" in workflow[True]
    assert workflow["permissions"] == {"contents": "write"}  # GITHUB_TOKEN may push
    job = workflow["jobs"]["refresh"]
    assert job["timeout-minutes"]
    for step in job["steps"]:
        uses = step.get("uses", "")
        if uses:
            assert re.fullmatch(r"[0-9a-f]{40}", uses.split("@", 1)[1]), f"unpinned action: {uses}"

