"""Tests for agent/display.py — build_tool_preview() and inline diff previews."""

import json
import pytest
from unittest.mock import MagicMock

import agent.display as display_module
from agent.display import (
    build_tool_preview,
    capture_local_edit_snapshot,
    describe_tool_call,
    extract_edit_diff,
    get_cute_tool_message,
    prepare_tool_preview,
    redact_tool_args_for_display,
    set_tool_preview_max_len,
    _render_inline_unified_diff,
    _summarize_rendered_diff_sections,
    render_edit_diff_with_delta,
)
from tools.delegate_lane_specs import LANE_TOOL_NAMES


@pytest.fixture(autouse=True)
def reset_tool_preview_max_len():
    set_tool_preview_max_len(0)
    yield
    set_tool_preview_max_len(0)


def test_cute_tool_message_falls_back_when_renderer_raises(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("cosmetic failure")

    monkeypatch.setattr(display_module, "_get_cute_tool_message", _boom)

    assert get_cute_tool_message("web_extract", {"urls": []}, 0.25) == (
        "┊ ⚡ web_extra completed  0.2s"
    )


class TestBuildToolPreview:
    """Tests for build_tool_preview defensive handling and normal operation."""

    def test_none_args_returns_none(self):
        """PR #453: None args should not crash, should return None."""
        assert build_tool_preview("terminal", None) is None

    def test_empty_dict_returns_none(self):
        """Empty dict has no keys to preview."""
        assert build_tool_preview("terminal", {}) is None








    def test_browser_type_preview_redacts_api_key(self):
        secret = "sk-proj-ABCD1234567890EFGH"
        result = build_tool_preview("browser_type", {"ref": "@e3", "text": secret})
        assert result is not None
        assert secret not in result
        assert "sk-pro" in result and "..." in result

    def test_browser_type_preview_keeps_normal_text(self):
        text = "hello world search query"
        result = build_tool_preview("browser_type", {"ref": "@e3", "text": text})
        assert result is not None
        assert text in result

    def test_browser_type_display_args_redact_api_key(self):
        secret = "ghp_ABCDEFGHIJ1234567890"
        safe_args = redact_tool_args_for_display(
            "browser_type", {"ref": "@e3", "text": secret}
        )
        assert secret not in str(safe_args)
        assert safe_args["ref"] == "@e3"
        assert safe_args["text"].startswith("ghp_AB")


class TestDescribeToolCallBrowserInteractions:
    """describe_tool_call — optional description arg on browser interaction tools."""

    def test_browser_click_uses_model_description_for_progress(self):
        phrase = describe_tool_call(
            "browser_click",
            {"ref": "@e5", "description": "clicking the submit button"},
        )
        assert phrase == "Clicking the submit button"

    def test_browser_click_fallback_without_description(self):
        phrase = describe_tool_call("browser_click", {"ref": "@e5"})
        assert phrase == "Clicking page element @e5"

    def test_browser_type_fallback_hides_typed_text(self):
        phrase = describe_tool_call(
            "browser_type", {"ref": "@e3", "text": "hunter2"}
        )
        assert phrase == "Typing into page element @e3"
        assert "hunter2" not in phrase

    def test_browser_press_fallback_without_description(self):
        phrase = describe_tool_call("browser_press", {"key": "Enter"})
        assert phrase == "Pressing a key"

    def test_browser_type_uses_description_without_leaking_text(self):
        phrase = describe_tool_call(
            "browser_type",
            {
                "ref": "@e3",
                "text": "hunter2",
                "description": "entering the login password",
            },
        )
        assert phrase == "Entering the login password"
        assert "hunter2" not in phrase












    def test_delegate_agent_batch_preview_respects_max_len(self):
        result = build_tool_preview(
            "delegate_agent",
            {"tasks": [{"goal": "A" * 80}, {"goal": "B" * 80}]},
            max_len=30,
        )
        assert result == "2 tasks: AAAAAAAAAAAAAAAAAA..."
        assert len(result) == 30

    def test_false_like_args_zero(self):
        """Non-dict falsy values should return None, not crash."""
        assert build_tool_preview("terminal", 0) is None
        assert build_tool_preview("terminal", "") is None
        assert build_tool_preview("terminal", []) is None

    @pytest.mark.parametrize("max_len", [1, 2, 3, 4])
    def test_tiny_max_len_never_exceeded(self, max_len):
        """max_len is a hard cap on every preview path — dedicated builder (terminal), generic
        fallback key (web_search), and the cute head-truncated path (#9439)."""
        from agent.display import _cute_path, set_tool_preview_max_len
        long = "abcdefghijklmnopqrstuvwxyz"
        for tool, args in (("terminal", {"command": long}), ("web_search", {"query": long})):
            preview = build_tool_preview(tool, args, max_len=max_len)
            assert preview and len(preview) <= max_len, (tool, preview)
        set_tool_preview_max_len(max_len)
        try:
            assert len(_cute_path("/" + long + "/file.py")) <= max_len
        finally:
            set_tool_preview_max_len(0)


class TestDescribeToolCallSkillAndDelegateDescription:
    """describe_tool_call — optional description on skill_view, skill_manage, delegate_agent."""

    def test_skill_view_composes_name_and_reason(self):
        phrase = describe_tool_call(
            "skill_view", {"name": "fork", "description": "check ship rules"}
        )
        assert phrase == "Reading skill to check ship rules"

    def test_skill_view_name_only_unchanged(self):
        assert describe_tool_call("skill_view", {"name": "fork"}) == "Reading skill fork"

    def test_skill_view_reason_only(self):
        assert describe_tool_call(
            "skill_view", {"name": "x", "description": "why load"}
        ) == "Reading skill to why load"
        assert describe_tool_call("skill_view", {"description": "why only"}) == "Reading skill to why only"

    def test_skill_manage_operations_with_reason(self):
        phrase = describe_tool_call(
            "skill_manage",
            {"operations": [{"action": "patch", "name": "x"}], "description": "fix the deploy rule"},
        )
        assert phrase == "Editing skill x to fix the deploy rule"

    def test_skill_manage_without_reason_unchanged(self):
        assert describe_tool_call(
            "skill_manage", {"operations": [{"action": "patch", "name": "x"}]}
        ) == "Editing skill x"
        assert describe_tool_call("skill_manage", {"operations": [], "description": ""}) == "Updating skills"

    def test_delegate_agent_claude_with_reason(self):
        phrase = describe_tool_call(
            "delegate_agent",
            {
                "backend": "claude",
                "goal": "/goal tests pass",
                "description": "implementing retry gate",
                "workdir": "/w",
            },
        )
        assert phrase == "Delegated to Claude agent: implementing retry gate"

    def test_delegate_agent_claude_without_reason_unchanged(self):
        phrase = describe_tool_call(
            "delegate_agent", {"backend": "claude", "goal": "g", "workdir": "/w"}
        )
        assert phrase == "Delegating to a Claude agent in /w"

    def test_delegate_agent_cursor_with_reason(self):
        phrase = describe_tool_call(
            "delegate_agent",
            {"backend": "cursor", "goal": "g", "description": "cloud fix"},
        )
        assert phrase == "Delegated to Cursor agent: cloud fix"

class TestDescribeDelegateLanePhrase:
    """describe_tool_call — progress phrases for the delegate_<lane> tools."""

    def test_lane_with_description_names_lane_and_reason(self):
        phrase = describe_tool_call(
            "delegate_backend_dev",
            {
                "task": "wire the retry gate",
                "workdir": "/repo",
                "description": "implementing the retry gate",
            },
        )
        assert phrase == "Delegated to backend dev lane: implementing the retry gate"

    def test_lane_without_description_uses_workdir(self):
        phrase = describe_tool_call(
            "delegate_ui_dev", {"task": "polish layout", "workdir": "/repo"}
        )
        assert phrase == "Delegating to the ui dev lane in /repo"

    def test_lane_without_description_or_workdir(self):
        assert describe_tool_call("delegate_client_dev", {"task": "t"}) == (
            "Delegating to the client dev lane"
        )

    @pytest.mark.parametrize("tool_name", LANE_TOOL_NAMES)
    def test_every_lane_tool_has_a_phrase(self, tool_name):
        # Registered from the lane spec table: no lane falls back to the raw
        # tool name ("Delegate_backend_dev") as its progress phrase.
        assert describe_tool_call(tool_name, {"task": "t"}) is not None
        assert not describe_tool_call(tool_name, {"task": "t"}).startswith("delegate_")

    def test_task_and_context_never_render(self):
        phrase = describe_tool_call(
            "delegate_reverse_engineer",
            {
                "task": "decompile the firmware blob at 0x4000",
                "context": "secret evidence body",
                "workdir": "/bin",
            },
        )
        assert phrase == "Delegating to the reverse engineer lane in /bin"
        assert "firmware" not in phrase
        assert "evidence" not in phrase

    def test_composed_phrase_wins_over_raw_description_passthrough(self):
        # A lane call carrying `description` must keep its lane attribution;
        # the generic progressive passthrough must not swallow it.
        phrase = describe_tool_call(
            "delegate_backend_dev", {"task": "t", "description": "migrating the schema"}
        )
        assert phrase == "Delegated to backend dev lane: migrating the schema"


class TestDescribeToolCallMemoryDescription:
    """describe_tool_call — optional description on memory (composed with derived label)."""

    def test_memory_single_op_composes_description(self):
        phrase = describe_tool_call(
            "memory",
            {"action": "replace", "target": "memory", "old_text": "x",
             "description": "save the deploy rule"},
        )
        assert phrase == "Replacing a memory (memory) to save the deploy rule"

    def test_memory_batch_composes_description(self):
        phrase = describe_tool_call(
            "memory",
            {"target": "memory", "operations": [{"action": "add", "content": "a"},
                                                {"action": "remove", "old_text": "b"},
                                                {"action": "add", "content": "c"}],
             "description": "consolidate stale notes"},
        )
        assert phrase == "Updating memories (3 operations) (memory) to consolidate stale notes"

    def test_memory_batch_none_target_composes_description(self):
        phrase = describe_tool_call(
            "memory",
            {"operations": [{"action": "add", "content": "a"}], "description": "record a fact"},
        )
        assert phrase == "Updating memories (1 operation) to record a fact"

    def test_memory_without_description_unchanged(self):
        assert describe_tool_call(
            "memory", {"action": "add", "target": "memory", "content": "x"}
        ) == "Saving a memory (memory)"
        assert describe_tool_call(
            "memory", {"target": "user", "operations": [{"action": "add", "content": "a"}]}
        ) == "Updating memories (1 operation) (user)"


class TestDescribeToolCallDiscordDescription:
    """describe_tool_call — optional description on discord / discord_admin (composed with derived label)."""

    def test_discord_fetch_messages_composes_description(self):
        phrase = describe_tool_call(
            "discord",
            {
                "action": "fetch_messages",
                "channel_id": "123",
                "description": "find the deploy report",
            },
        )
        assert phrase == (
            "Fetching recent Discord messages in channel 123 to find the deploy report"
        )

    def test_discord_description_already_prefixed_with_to_not_doubled(self):
        phrase = describe_tool_call(
            "discord",
            {
                "action": "fetch_messages",
                "channel_id": "123",
                "description": "to find the deploy report",
            },
        )
        assert phrase == (
            "Fetching recent Discord messages in channel 123 to find the deploy report"
        )

    def test_discord_fetch_messages_without_description_unchanged(self):
        phrase = describe_tool_call(
            "discord", {"action": "fetch_messages", "channel_id": "123"}
        )
        assert phrase == "Fetching recent Discord messages in channel 123"

    def test_discord_search_members_composes_description(self):
        phrase = describe_tool_call(
            "discord",
            {
                "action": "search_members",
                "query": "deploy",
                "description": "find the on-call engineer",
            },
        )
        assert phrase == (
            "Searching Discord members for deploy to find the on-call engineer"
        )

    def test_discord_create_thread_composes_description(self):
        phrase = describe_tool_call(
            "discord",
            {
                "action": "create_thread",
                "name": "deploy-thread",
                "description": "track the rollout",
            },
        )
        assert phrase == "Creating Discord thread deploy-thread to track the rollout"

    def test_discord_admin_pin_message_composes_description(self):
        phrase = describe_tool_call(
            "discord_admin",
            {
                "action": "pin_message",
                "message_id": "456",
                "description": "keep the report visible",
            },
        )
        assert phrase == "Pinning a Discord message 456 to keep the report visible"

    def test_discord_unknown_action_with_description_falls_back(self):
        assert (
            describe_tool_call(
                "discord",
                {"action": "bogus_action", "description": "check something"},
            )
            == "Checking something"
        )

    def test_discord_missing_action_with_description_falls_back(self):
        assert (
            describe_tool_call("discord", {"description": "check something"})
            == "Checking something"
        )


class TestDescribeToolCallFileDescriptions:
    """patch/write_file describe the basename and purpose instead of exposing the full path."""

    @pytest.mark.parametrize(
        ("tool_name", "args", "expected"),
        [
            ("patch", {"path": "/tmp/gpu.yaml", "description": "update the GPU mapping"}, "Editing gpu.yaml to update the GPU mapping"),
            ("write_file", {"path": "/tmp/gpu.yaml", "content": "x", "description": "update the GPU mapping"}, "Writing gpu.yaml to update the GPU mapping"),
        ],
    )
    def test_edit_description_wins_over_path(self, tool_name, args, expected):
        phrase = describe_tool_call(tool_name, args)
        assert phrase == expected
        assert "/tmp/gpu.yaml" not in phrase
        assert "gpu.yaml" in phrase

    @pytest.mark.parametrize(
        ("tool_name", "args", "expected"),
        [
            ("patch", {"path": "/tmp/gpu.yaml"}, "Editing /tmp/gpu.yaml"),
            ("patch", {"path": "/tmp/gpu.yaml", "description": "  "}, "Editing /tmp/gpu.yaml"),
            ("write_file", {"path": "/tmp/gpu.yaml", "content": "x"}, "Writing /tmp/gpu.yaml"),
        ],
    )
    def test_edit_path_fallback_without_description(self, tool_name, args, expected):
        assert describe_tool_call(tool_name, args) == expected

    @pytest.mark.parametrize(
        ("tool_name", "description", "expected"),
        [
            ("patch", "to update the GPU mapping", "Editing gpu.yaml to update the GPU mapping"),
            ("write_file", "To update the GPU mapping", "Writing gpu.yaml To update the GPU mapping"),
        ],
    )
    def test_edit_description_does_not_double_to(self, tool_name, description, expected):
        phrase = describe_tool_call(tool_name, {"path": "/tmp/gpu.yaml", "description": description})
        assert phrase == expected

    @pytest.mark.parametrize(
        ("tool_name", "args", "expected"),
        [
            ("patch", {"description": "update the GPU mapping"}, "Editing to update the GPU mapping"),
            ("write_file", {"path": "  ", "description": "update the GPU mapping"}, "Writing to update the GPU mapping"),
        ],
    )
    def test_edit_description_without_path_stays_purpose_only(self, tool_name, args, expected):
        assert describe_tool_call(tool_name, args) == expected


class TestPrepareToolPreview:
    def test_recovers_and_describes_truncated_url(self):
        url = "https://example.com/a/very/long/path/to/a/page"
        set_tool_preview_max_len(20)

        preview = prepare_tool_preview(
            "web_extract",
            {"urls": [url]},
            fallback=url[:17] + "...",
            max_len=20,
        )

        assert preview.text == url[:17] + "..."
        assert preview.truncated is True
        assert preview.url == url

    def test_untruncated_url_has_no_link_target(self):
        url = "https://example.com/page"
        preview = prepare_tool_preview(
            "browser_navigate", None, fallback=url, max_len=40
        )

        assert preview.text == url
        assert preview.truncated is False
        assert preview.url is None

    def test_truncated_non_url_has_no_link_target(self):
        preview = prepare_tool_preview(
            "web_search",
            {"query": "how to parse a URL"},
            fallback="how to parse a URL",
            max_len=12,
        )

        assert preview.truncated is True
        assert preview.url is None


class TestCuteToolMessagePreviewLength:


    def test_search_files_preview_uses_positive_configured_limit_not_default(self):
        set_tool_preview_max_len(80)
        pattern = "function.formatToolCall.context.preview.compactPreview.maxLength.truncate"

        line = get_cute_tool_message("search_files", {"pattern": pattern}, 0.1)

        assert pattern in line
        assert "..." not in line





    def test_browser_type_cute_message_redacts_api_key(self):
        secret = "sk-proj-ABCD1234567890EFGH"
        line = get_cute_tool_message(
            "browser_type",
            {"ref": "@password", "text": secret},
            0.1,
            result='{"success": true, "typed": "sk-pro...EFGH"}',
        )

        assert secret not in line
        assert "sk-pro" in line

    def test_browser_type_cute_message_keeps_normal_text(self):
        text = "hello world"
        line = get_cute_tool_message(
            "browser_type",
            {"ref": "@search", "text": text},
            0.1,
            result='{"success": true, "typed": "hello world"}',
        )

        assert text in line


class TestEditDiffPreview:



    def test_extract_edit_diff_uses_local_snapshot_for_write_file(self, tmp_path):
        target = tmp_path / "note.txt"
        target.write_text("old\n", encoding="utf-8")

        snapshot = capture_local_edit_snapshot("write_file", {"path": str(target)})

        target.write_text("new\n", encoding="utf-8")

        diff = extract_edit_diff(
            "write_file",
            '{"bytes_written": 4}',
            function_args={"path": str(target)},
            snapshot=snapshot,
        )

        assert diff is not None
        assert "--- a/" in diff
        assert "+++ b/" in diff
        assert "-old" in diff
        assert "+new" in diff



    def test_render_edit_diff_with_delta_handles_renderer_errors(self, monkeypatch):
        printer = MagicMock()

        monkeypatch.setattr("agent.display._summarize_rendered_diff_sections", MagicMock(side_effect=RuntimeError("boom")))

        rendered = render_edit_diff_with_delta(
            "patch",
            '{"diff": "--- a/x\\n+++ b/x\\n"}',
            print_fn=printer,
        )

        assert rendered is False
        assert printer.call_count == 0


    def test_summarize_rendered_diff_sections_limits_file_count(self):
        diff = "".join(
            f"--- a/file{i}.py\n+++ b/file{i}.py\n+line{i}\n"
            for i in range(8)
        )

        rendered = _summarize_rendered_diff_sections(diff, max_files=3, max_lines=50)

        assert any("a/file0.py" in line for line in rendered)
        assert any("a/file1.py" in line for line in rendered)
        assert any("a/file2.py" in line for line in rendered)
        assert not any("a/file7.py" in line for line in rendered)
        assert "additional file" in rendered[-1]


class TestBuildToolLabel:
    """Friendly human-phrased tool labels for built-in tools."""

    @pytest.fixture(autouse=True)
    def _enable_friendly(self):
        from agent.display import set_friendly_tool_labels
        set_friendly_tool_labels(True)
        yield
        set_friendly_tool_labels(True)

    def test_web_search_uses_for_connector(self):
        from agent.display import build_tool_label
        label = build_tool_label("web_search", {"query": "weather in NYC"})
        assert label == 'Searching the web for weather in NYC'

    def test_web_extract_reads_url(self):
        from agent.display import build_tool_label
        label = build_tool_label("web_extract", {"urls": ["https://example.com/page"]})
        assert label is not None
        assert label.startswith("Reading ")
        assert "example.com/page" in label







    def test_disabled_falls_back_to_preview(self):
        from agent.display import (
            build_tool_label,
            build_tool_preview,
            set_friendly_tool_labels,
        )
        set_friendly_tool_labels(False)
        args = {"query": "weather in NYC"}
        label = build_tool_label("web_search", args)
        # With the feature off, must match the raw preview exactly
        assert label == build_tool_preview("web_search", args)
        assert "Searching the web" not in (label or "")



class TestBuildStatusPhrase:
    """build_status_phrase — live working-state text for Slack's status line."""



    def test_verb_only_when_args_none(self):
        # live_status: "verb" mode passes args=None to suppress previews.
        from agent.display import build_status_phrase
        assert build_status_phrase("terminal", None) == "is running…"
        assert build_status_phrase("read_file", None) == "is reading…"



    def test_caps_length_for_slack_status_line(self):
        from agent.display import build_status_phrase
        phrase = build_status_phrase(
            "terminal", {"command": "x" * 300}, max_len=49
        )
        assert phrase is not None and len(phrase) <= 49
        assert phrase.endswith("…")


    def test_respects_friendly_labels_toggle(self):
        from agent.display import build_status_phrase, set_friendly_tool_labels
        set_friendly_tool_labels(False)
        try:
            assert build_status_phrase("terminal", {"command": "ls"}) is None
        finally:
            set_friendly_tool_labels(True)


class TestDescribeToolCallTerminalFallback:
    def test_missing_description_describes_command(self):
        phrase = describe_tool_call("terminal", {"command": "git status"})

        assert "git status" in phrase
        assert phrase != "Running a terminal command"

    def test_missing_description_redacts_command_secrets(self):
        secret = "sk-live-abc123secret"
        phrase = describe_tool_call(
            "terminal",
            {"command": f'curl -H "Authorization: Bearer {secret}"'},
        )

        assert secret not in phrase
        assert "***" in phrase

    def test_description_wins_over_command(self):
        phrase = describe_tool_call(
            "terminal",
            {"command": "git status", "description": "checking git status"},
        )

        assert phrase == "Checking git status"

    def test_no_command_or_description_uses_generic_phrase(self):
        assert describe_tool_call("terminal", {}) == "Running a terminal command"


class TestDescribeToolCallProgressiveLabels:
    """Raw ``description`` passthrough renders with a present-progressive first word.

    Models write descriptions imperatively; users should read "Inspecting…", not
    "Inspect…". Composed purpose clauses ("Editing X to Y") stay infinitive.
    """

    @pytest.mark.parametrize(
        ("description", "expected"),
        [
            ("Inspect dedup tests and live drain call site", "Inspecting dedup tests and live drain call site"),
            ("Identify live drain path and legacy first-send", "Identifying live drain path and legacy first-send"),
            ("Check batch composition and dedup reset sites", "Checking batch composition and dedup reset sites"),
            ("check git status", "Checking git status"),
            ("run the test suite", "Running the test suite"),
            ("checking git status", "Checking git status"),
            ("Reading the config", "Reading the config"),
        ],
    )
    def test_raw_description_becomes_progressive(self, description, expected):
        phrase = describe_tool_call("terminal", {"command": "x", "description": description})
        assert phrase == expected

    @pytest.mark.parametrize(
        ("tool_name", "args", "expected"),
        [
            # Composed infinitive purpose clauses stay exactly as today.
            ("patch", {"path": "/tmp/gpu.yaml", "description": "update the GPU mapping"}, "Editing gpu.yaml to update the GPU mapping"),
            ("skill_view", {"name": "fork", "description": "check ship rules"}, "Reading skill to check ship rules"),
            ("delegate_agent", {"backend": "claude", "goal": "g", "description": "implementing retry gate", "workdir": "/w"}, "Delegated to Claude agent: implementing retry gate"),
        ],
    )
    def test_composed_purpose_clauses_unchanged(self, tool_name, args, expected):
        assert describe_tool_call(tool_name, args) == expected

    @pytest.mark.parametrize(
        ("description", "expected"),
        [
            # Non-verb first words, digits/symbols, acronyms: left alone (then capitalised).
            ("the deploy config needs updating", "The deploy config needs updating"),
            ("GPU mapping note", "GPU mapping note"),
            ("2fa rollout check", "2fa rollout check"),
            ("API rate limits look fine", "API rate limits look fine"),
        ],
    )
    def test_leave_alone_first_words(self, description, expected):
        assert describe_tool_call("terminal", {"command": "x", "description": description}) == expected


class TestDescribeToolCallSessionSearchPurpose:
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (
                {"query": "ezEIGEN", "description": "find where we left the deploy"},
                "Searching past sessions for ezEIGEN to find where we left the deploy",
            ),
            (
                {"description": "recall the deploy discussion"},
                "Searching past sessions to recall the deploy discussion",
            ),
            ({"query": "ezEIGEN"}, "Searching past sessions for ezEIGEN"),
            ({}, "Searching past sessions"),
        ],
    )
    def test_composes_query_and_purpose(self, args, expected):
        assert describe_tool_call("session_search", args) == expected

    def test_recalling_purpose_inverts_to_recall(self):
        assert describe_tool_call(
            "session_search",
            {"description": "recalling the last deploy discussion"},
        ) == "Searching past sessions to recall the last deploy discussion"


class TestDescribeToolCallCronPurpose:
    @pytest.mark.parametrize("tool_name", ["cronjob_manage", "cronjob"])
    def test_remove_composes_unresolved_id_and_purpose(self, tool_name):
        assert describe_tool_call(
            tool_name,
            {
                "action": "remove",
                "job_id": "d3b3945f166d",
                "description": "clean up the stale backup monitor",
            },
        ) == "Removing cron job d3b3945f166d to clean up the stale backup monitor"

    @pytest.mark.parametrize("tool_name", ["cronjob_manage", "cronjob"])
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            ({"action": "list"}, "Listing cron jobs"),
            (
                {"action": "list", "description": "audit the backup schedule"},
                "Listing cron jobs to audit the backup schedule",
            ),
            ({"action": "unknown"}, "Managing cron jobs"),
            (
                {"action": "unknown", "description": "inspect the job action"},
                "Managing cron jobs to inspect the job action",
            ),
        ],
    )
    def test_existing_labels_only_gain_a_supplied_purpose(self, tool_name, args, expected):
        assert describe_tool_call(tool_name, args) == expected

    def test_remove_resolves_name_from_real_active_profile_store(self, tmp_path, monkeypatch):
        from cron.jobs import create_job

        home = tmp_path / "active-profile"
        monkeypatch.setenv("HERMES_HOME", str(home))
        job = create_job(
            prompt="private prompt payload",
            schedule="every 1h",
            name="NAS backup monitor",
            script="private-script.py",
            no_agent=True,
        )

        phrase = describe_tool_call(
            "cronjob_manage", {"action": "remove", "job_id": job["id"]}
        )

        assert phrase == "Removing cron job NAS backup monitor"
        assert job["id"] not in phrase
        assert job["prompt"] not in phrase
        assert job["script"] not in phrase
        assert job["schedule_display"] not in phrase

    @pytest.mark.parametrize("stored_job", [None, {"name": ""}, {"name": "   "}])
    def test_missing_or_empty_stored_name_falls_back_to_id(self, stored_job, monkeypatch):
        monkeypatch.setattr("cron.jobs.get_job", lambda _job_id: stored_job)
        assert describe_tool_call(
            "cronjob_manage", {"action": "remove", "job_id": "d3b3945f166d"}
        ) == "Removing cron job d3b3945f166d"

    def test_store_lookup_error_never_escapes_display(self, monkeypatch):
        def _raise_lookup_error(_job_id):
            raise RuntimeError("store unavailable")

        monkeypatch.setattr("cron.jobs.get_job", _raise_lookup_error)
        assert describe_tool_call(
            "cronjob_manage", {"action": "remove", "job_id": "d3b3945f166d"}
        ) == "Removing cron job d3b3945f166d"

    def test_explicit_name_does_not_need_store_lookup(self, monkeypatch):
        def _unexpected_lookup(_job_id):
            raise AssertionError("an explicit name must win without a store read")

        monkeypatch.setattr("cron.jobs.get_job", _unexpected_lookup)
        assert describe_tool_call(
            "cronjob_manage",
            {"action": "remove", "job_id": "d3b3945f166d", "name": "NAS backup monitor"},
        ) == "Removing cron job NAS backup monitor"


class TestDescribeToolCallGerundPurposes:
    @pytest.mark.parametrize(
        ("tool_name", "args", "expected"),
        [
            (
                "skill_view",
                {"name": "x", "description": "checking fork ship rules"},
                "Reading skill to check fork ship rules",
            ),
            (
                "discord",
                {
                    "action": "fetch_messages",
                    "channel_id": "123",
                    "description": "Finding the deploy report",
                },
                "Fetching recent Discord messages in channel 123 to find the deploy report",
            ),
            (
                "write_file",
                {"path": "/a/b.md", "description": "Updating the GPU note"},
                "Writing b.md to update the GPU note",
            ),
        ],
    )
    def test_progressive_reason_becomes_infinitive(self, tool_name, args, expected):
        assert describe_tool_call(tool_name, args) == expected

    def test_explicit_to_progressive_reason_is_also_normalized(self):
        assert describe_tool_call(
            "skill_view", {"name": "x", "description": "to Checking fork ship rules"}
        ) == "Reading skill to check fork ship rules"
