"""Silent audit records must not replace useful continuity output (#104541)."""
import os

from cron import jobs
from cron.scheduler_prompt import _inject_context_from


def test_silent_audits_preserve_latest_payload(tmp_path):
    with jobs.use_cron_store(tmp_path):
        directory = jobs.get_cron_output_dir() / "abcdef"
        directory.mkdir(parents=True)
        header = "# Cron Job: " + "long name " * 80 + "\n\n**Job ID:** abcdef\n**Run Time:** now\n"
        payload = header + "**Mode:** no_agent (script)\n\n---\n\n**Status:** silent but useful payload\n"
        records = [payload, header + "**Mode:** monitor\n**Status:** no_change (agent run suppressed)\n",
                   header + "**Mode:** no_agent (script)\n**Status:** silent (empty output)\n",
                   header + "\nScript gate returned `wakeAgent=false` — agent skipped.\n", ""]
        for index, text in enumerate(records):
            path = directory / f"{index}.md"
            path.write_text(text, encoding="utf-8")
            os.utime(path, (index + 1, index + 1))
        for source in ("self", "abcdef"):
            prompt, injected = _inject_context_from({"id": "abcdef", "context_from": [source]}, "next")
            assert injected and "silent but useful payload" in prompt
            assert "agent skipped" not in prompt and "agent run suppressed" not in prompt
        assert len(list(directory.glob("*.md"))) == len(records)


def test_audit_only_history_is_empty_but_errors_remain_context(tmp_path):
    with jobs.use_cron_store(tmp_path):
        directory = jobs.get_cron_output_dir() / "abcdef"
        directory.mkdir(parents=True)
        path = directory / "audit.md"
        path.write_text("# Cron Job: monitor\n**Status:** no_change (agent run suppressed)\n", encoding="utf-8")
        job = {"id": "abcdef", "context_from": ["self"]}
        assert _inject_context_from(job, "next") == ("next", False)
        path.write_text("# Cron Job: monitor\n**Status:** monitor source failed\n\nConnection refused\n", encoding="utf-8")
        prompt, injected = _inject_context_from(job, "next")
        assert injected and "Connection refused" in prompt


def test_context_from_prefers_response_over_prompt_dump(tmp_path):
    """Agent run docs dump the assembled prompt first; continuity needs ## Response."""
    with jobs.use_cron_store(tmp_path):
        directory = jobs.get_cron_output_dir() / "abcdef"
        directory.mkdir(parents=True)
        report = "Both Merchant Centers, 29 Sep. Unchanged from 28 Sep."
        text = (
            "# Cron Job: Daily both Merchant Center follow-up\n\n"
            "**Job ID:** abcdef\n**Run Time:** now\n\n"
            "## Prompt\n\nSKILL DUMP " + ("x" * 9000) + "\n\n"
            f"## Response\n\n{report}\n"
        )
        (directory / "run.md").write_text(text, encoding="utf-8")
        prompt, injected = _inject_context_from(
            {"id": "abcdef", "context_from": ["self"]}, "next")
        assert injected and report in prompt
        assert "SKILL DUMP" not in prompt
        assert "truncated" not in prompt


def test_context_from_skips_empty_response_for_older_report(tmp_path):
    """An empty/tokenizer-only ## Response is not useful continuity."""
    with jobs.use_cron_store(tmp_path):
        directory = jobs.get_cron_output_dir() / "abcdef"
        directory.mkdir(parents=True)
        older = (
            "# Cron Job: Daily\n\n**Job ID:** abcdef\n\n"
            "## Prompt\n\nold skill\n\n"
            "## Response\n\nOlder useful report\n"
        )
        newer = (
            "# Cron Job: Daily\n\n**Job ID:** abcdef\n\n"
            "## Prompt\n\nnew skill " + ("y" * 9000) + "\n\n"
            "## Response\n\n<|eos|>\n"
        )
        old_path = directory / "1.md"
        new_path = directory / "2.md"
        old_path.write_text(older, encoding="utf-8")
        new_path.write_text(newer, encoding="utf-8")
        os.utime(old_path, (1, 1))
        os.utime(new_path, (2, 2))
        prompt, injected = _inject_context_from(
            {"id": "abcdef", "context_from": ["self"]}, "next")
        assert injected and "Older useful report" in prompt
        assert "new skill" not in prompt


def test_context_from_uses_last_response_heading(tmp_path):
    """A quoted ## Response inside ## Prompt must not steal the payload."""
    with jobs.use_cron_store(tmp_path):
        directory = jobs.get_cron_output_dir() / "abcdef"
        directory.mkdir(parents=True)
        text = (
            "# Cron Job: Daily\n\n**Job ID:** abcdef\n\n"
            "## Prompt\n\nDo not use this:\n## Response\n\n decoy report\n\n"
            "## Response\n\nReal owner report\n"
        )
        (directory / "run.md").write_text(text, encoding="utf-8")
        prompt, injected = _inject_context_from(
            {"id": "abcdef", "context_from": ["self"]}, "next")
        assert injected and "Real owner report" in prompt
        assert "decoy report" not in prompt


def test_context_from_truncates_oversized_response_body(tmp_path):
    with jobs.use_cron_store(tmp_path):
        directory = jobs.get_cron_output_dir() / "abcdef"
        directory.mkdir(parents=True)
        body = "z" * 10000
        text = (
            "# Cron Job: Daily\n\n**Job ID:** abcdef\n\n"
            "## Prompt\n\nshort prompt\n\n"
            f"## Response\n\n{body}\n"
        )
        (directory / "run.md").write_text(text, encoding="utf-8")
        prompt, injected = _inject_context_from(
            {"id": "abcdef", "context_from": ["self"]}, "next")
        assert injected and "truncated" in prompt
        assert "z" * 10000 not in prompt
        assert "short prompt" not in prompt
