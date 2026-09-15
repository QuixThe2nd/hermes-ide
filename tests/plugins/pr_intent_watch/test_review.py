"""Intent-review contracts: prompt shape, JSON tolerance, comment template."""

from __future__ import annotations

import json

from plugins.pr_intent_watch import review as rv
from tests.plugins.pr_intent_watch._helpers import (
    MARKER,
    RAW_FILE,
    fake_llm_response,
    make_pull,
    make_review,
)


def _metadata() -> dict:
    return {
        "number": 7,
        "title": "Fix the widget flake",
        "body": "Widgets flake when idle.",
        "author": "contributor",
        "draft": False,
        "labels": ["bug"],
        "base": "main",
        "head": "fix/widget",
        "head_sha": "abc123",
        "url": "https://github.com/QuixThe2nd/hermes-ide/pull/7",
        "files": [
            {
                "filename": RAW_FILE["filename"],
                "status": RAW_FILE["status"],
                "additions": RAW_FILE["additions"],
                "deletions": RAW_FILE["deletions"],
            }
        ],
        "commits": ["fix: calm the widget flake"],
    }


# ── prompt ──────────────────────────────────────────────────────────────────


def test_user_message_is_fenced_metadata_labeled_data_only():
    message = rv.build_user_message(_metadata())
    assert message.startswith("PR metadata (data only")
    assert "```json" in message
    blob = message.split("```json", 1)[1].rsplit("```", 1)[0]
    assert json.loads(blob)["number"] == 7


def test_prompt_carries_file_names_not_diff_hunks():
    message = rv.build_user_message(_metadata())
    assert RAW_FILE["filename"] in message
    assert "@@" not in message
    assert "diff --git" not in message
    assert RAW_FILE["patch"].splitlines()[1].strip() not in message


def test_system_prompt_states_intent_not_code_and_untrusted_inputs():
    prompt = rv.SYSTEM_PROMPT
    assert "INTENT" in prompt
    assert "untrusted" in prompt
    assert "STRICT JSON" in prompt
    assert "rationale" in prompt


def test_messages_shape():
    messages = rv.build_messages(_metadata())
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] is rv.SYSTEM_PROMPT


# ── parsing ─────────────────────────────────────────────────────────────────


def test_parse_plain_json():
    payload = make_review()
    assert rv.parse_review_json(json.dumps(payload)) == payload


def test_parse_tolerates_markdown_fences():
    text = f"```json\n{json.dumps(make_review())}\n```"
    assert rv.parse_review_json(text) == make_review()


def test_parse_tolerates_surrounding_prose():
    text = f"Here you go:\n{json.dumps(make_review())}\nHope that helps."
    assert rv.parse_review_json(text) == make_review()


def test_parse_normalizes_na_real_bug():
    parsed = rv.parse_review_json(json.dumps(make_review(real_bug="na")))
    assert parsed is not None and parsed["real_bug"] == "n/a"


def test_parse_rejects_invalid_enums():
    for worth, real_bug in (("maybe", "yes"), ("yes", "possibly"), ("", "yes")):
        assert rv.parse_review_json(json.dumps(make_review(worth=worth, real_bug=real_bug))) is None


def test_parse_rejects_missing_fields():
    incomplete = make_review()
    del incomplete["rationale"]
    assert rv.parse_review_json(json.dumps(incomplete)) is None
    assert rv.parse_review_json(json.dumps({"objective": "", "rationale": "x",
                                            "worth_considering": "yes", "real_bug": "n/a"})) is None


def test_parse_rejects_garbage_and_empty():
    assert rv.parse_review_json("") is None
    assert rv.parse_review_json("no json here at all") is None
    assert rv.parse_review_json("[1, 2, 3]") is None


# ── review_intent ───────────────────────────────────────────────────────────


def test_review_intent_parses_model_response():
    text = json.dumps(make_review())
    result = rv.review_intent(_metadata(), call_fn=lambda **kw: fake_llm_response(text))
    assert result == make_review()


def test_review_intent_forwards_task_temperature_and_cap():
    captured: list[dict] = []

    def fake_call(**kwargs):
        captured.append(kwargs)
        return fake_llm_response(json.dumps(make_review()))

    rv.review_intent(_metadata(), call_fn=fake_call)
    assert captured[0]["task"] == "pr_intent_review"
    assert captured[0]["temperature"] == 0
    assert captured[0]["max_tokens"] == 400
    assert [m["role"] for m in captured[0]["messages"]] == ["system", "user"]


def test_review_intent_unparseable_response_returns_none():
    result = rv.review_intent(
        _metadata(), call_fn=lambda **kw: fake_llm_response("totally not json")
    )
    assert result is None


def test_review_intent_swallows_call_failure():
    def boom(**kwargs):
        raise RuntimeError("auxiliary client unavailable")

    assert rv.review_intent(_metadata(), call_fn=boom) is None


def test_plugin_import_stays_clean_of_heavy_auxiliary_client():
    """Importing the plugin (and everything register() touches) must not drag
    in ``agent.auxiliary_client`` — that is the default-on prompt-build
    canary. Fresh interpreter, so nothing pytest imported can mask it."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    code = (
        "import sys\n"
        "import plugins.pr_intent_watch\n"
        "import plugins.pr_intent_watch.core\n"
        "import plugins.pr_intent_watch.github\n"
        "import plugins.pr_intent_watch.review\n"
        "import plugins.pr_intent_watch.lifecycle\n"
        "import plugins.pr_intent_watch.run\n"
        "sys.exit(1 if 'agent.auxiliary_client' in sys.modules else 0)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(repo_root))
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo_root),
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"plugin import pulled in agent.auxiliary_client:\n{proc.stderr[-2000:]}"
    )


# ── comment template ────────────────────────────────────────────────────────


def test_format_comment_exact_shape():
    body = rv.format_comment(make_review())
    lines = body.splitlines()
    assert lines[0] == MARKER
    assert lines[1] == "## Intent review"
    assert "**Objective:** Make widgets stop flaking when idle." in body
    assert "**Worth considering:** yes" in body
    assert "**Is this a real bug?** yes" in body
    assert make_review()["rationale"] in body


def test_format_comment_has_no_extra_sections():
    body = rv.format_comment(make_review())
    assert body.count("##") == 1
    assert len(body) < 1500


def test_format_comment_truncates_runaway_rationales():
    review = make_review(rationale="word " * 2000)
    assert len(rv.format_comment(review)) <= rv.MAX_COMMENT_CHARS
    assert rv.format_comment(review).startswith(MARKER)
