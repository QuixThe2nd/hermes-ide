"""Line-level dedup of per-turn memory-context injection.

Covers dedup_memory_context_lines() / extract_injected_memory_lines() in
agent/memory_manager.py: providers re-return the full memory profile every
turn, and prior turns' <memory-context> blocks replay byte-for-byte from
api_content sidecars, so lines already visible in history must not be
injected again.
"""

from agent.memory_manager import (
    build_memory_context_block,
    dedup_memory_context_lines,
    extract_injected_memory_lines,
)


def _msg_with_injection(payload: str) -> dict:
    """A historical user row whose api_content sidecar carries an injection."""
    return {
        "role": "user",
        "content": "some user text",
        "api_content": "some user text\n\n" + build_memory_context_block(payload),
    }


PROFILE_V1 = (
    "## User Representation\n"
    "\n"
    "## Explicit Observations\n"
    "\n"
    "[2026-06-17 12:32:14] user is reachable on Discord.\n"
    "[2026-06-30 04:18:56] user prefers concise communication.\n"
)


class TestExtractInjectedMemoryLines:
    def test_collects_lines_from_api_content_sidecar(self):
        msgs = [_msg_with_injection(PROFILE_V1)]
        seen = extract_injected_memory_lines(msgs)
        assert "[2026-06-17 12:32:14] user is reachable on Discord." in seen
        assert "[2026-06-30 04:18:56] user prefers concise communication." in seen

    def test_collects_from_plain_content(self):
        block = build_memory_context_block("line one\nline two")
        msgs = [{"role": "user", "content": "text\n\n" + block}]
        seen = extract_injected_memory_lines(msgs)
        assert {"line one", "line two"} <= seen

    def test_skips_multimodal_and_malformed_rows(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "user"},
            "not-a-dict",
            None,
        ]
        assert extract_injected_memory_lines(msgs) == set()

    def test_ignores_messages_without_memory_context(self):
        msgs = [{"role": "user", "content": "just text, no injection"}]
        assert extract_injected_memory_lines(msgs) == set()


class TestDedupMemoryContextLines:
    def test_identical_payload_returns_empty(self):
        history = [_msg_with_injection(PROFILE_V1)]
        assert dedup_memory_context_lines(PROFILE_V1, history) == ""

    def test_only_new_lines_survive(self):
        history = [_msg_with_injection(PROFILE_V1)]
        new_payload = PROFILE_V1 + "[2026-07-05 09:48:09] user stores projects in ~/projects.\n"
        out = dedup_memory_context_lines(new_payload, history)
        assert "[2026-07-05 09:48:09] user stores projects in ~/projects." in out
        assert "reachable on Discord" not in out
        assert "prefers concise" not in out

    def test_header_with_surviving_payload_is_kept(self):
        history = [_msg_with_injection(PROFILE_V1)]
        new_payload = PROFILE_V1 + "[2026-07-05 09:48:09] new fact.\n"
        out = dedup_memory_context_lines(new_payload, history)
        assert "## Explicit Observations" in out

    def test_fully_deduped_section_header_is_pruned(self):
        history = [_msg_with_injection("## Alpha\n\nfact a\n")]
        payload = "## Alpha\n\nfact a\n\n## Beta\n\nfact b\n"
        out = dedup_memory_context_lines(payload, history)
        assert "## Alpha" not in out
        assert "## Beta" in out
        assert "fact b" in out

    def test_within_payload_duplicates_collapse(self):
        payload = "fact a\nfact a\nfact b\n"
        out = dedup_memory_context_lines(payload, [])
        assert out == "fact a\nfact b"

    def test_order_preserved(self):
        payload = "zeta\nalpha\nomega\n"
        out = dedup_memory_context_lines(payload, [])
        assert out == "zeta\nalpha\nomega"

    def test_whitespace_difference_still_dedupes(self):
        history = [_msg_with_injection("fact a\n")]
        out = dedup_memory_context_lines("   fact a   \nfact b\n", history)
        assert "fact a" not in out
        assert "fact b" in out

    def test_blank_runs_collapse_and_outer_blanks_strip(self):
        # A dropped line triggers the filter path; blank runs around it
        # collapse and outer blanks strip.
        history = [_msg_with_injection("fact b\n")]
        payload = "\n\nfact a\n\n\n\nfact b\n\n\nfact c\n\n"
        out = dedup_memory_context_lines(payload, history)
        assert out == "fact a\n\nfact c"

    def test_no_drops_returns_payload_untouched(self):
        # Nothing filtered: payload passes through as-is (trimmed), no
        # reformatting of untouched context.
        payload = "\n\nfact a\n\n\n\nfact b\n\n"
        out = dedup_memory_context_lines(payload, [])
        assert out == payload.strip()

    def test_empty_input_returns_empty(self):
        assert dedup_memory_context_lines("", []) == ""
        assert dedup_memory_context_lines("   \n  ", []) == ""

    def test_multiple_historical_blocks_all_count(self):
        msgs = [
            _msg_with_injection("fact a\n"),
            _msg_with_injection("fact b\n"),
        ]
        out = dedup_memory_context_lines("fact a\nfact b\nfact c\n", msgs)
        assert out == "fact c"

    def test_no_history_returns_payload_unchanged_in_substance(self):
        out = dedup_memory_context_lines(PROFILE_V1, [])
        for line in PROFILE_V1.splitlines():
            if line.strip():
                assert line.strip() in out
