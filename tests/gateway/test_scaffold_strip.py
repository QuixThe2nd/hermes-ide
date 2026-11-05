"""Strip provider-proxy interim scaffolding from human-facing gateway finals."""

from gateway.run import _sanitize_gateway_final_response, _strip_provider_scaffolding

REAL_PROSE = "Here is the answer you asked for."


def test_memory_injection_card_stripped_prose_preserved():
    raw = (
        "🧠 Injecting Memory\n"
        "```\n"
        "MEMORY PROVIDER: Hindsight (session notes)\n"
        "§ USER PROFILE\n"
        "════════════════════════════════\n"
        "```\n"
        f"{REAL_PROSE}\n"
    )
    expected = REAL_PROSE
    assert _strip_provider_scaffolding(raw) == expected
    assert _sanitize_gateway_final_response("discord", raw) == expected


def test_reasoning_header_and_fenced_block_stripped():
    raw = (
        "💭 **Reasoning:**\n"
        "```\n"
        "First I considered the user's question.\n"
        "Then I checked the docs.\n"
        "_... (20 more lines)_\n"
        "```\n"
        f"{REAL_PROSE}\n"
    )
    expected = REAL_PROSE
    assert _strip_provider_scaffolding(raw) == expected
    assert _sanitize_gateway_final_response("discord", raw) == expected


def test_progress_accumulation_fence_stripped():
    raw = (
        "```\n"
        "📖 Reading skill hermes-agent-dev\n"
        "💻 Merge the documentation PR when green\n"
        "📋 Updating tasks: finish scaffold strip\n"
        "⏳ Scheduling cron job for nightly sync\n"
        "```\n"
        f"{REAL_PROSE}\n"
    )
    expected = REAL_PROSE
    assert _strip_provider_scaffolding(raw) == expected
    assert _sanitize_gateway_final_response("discord", raw) == expected


def test_standalone_injecting_time_stripped():
    raw = f"Before.\n🕐 Injecting time\nAfter."
    expected = "Before.\nAfter."
    assert _strip_provider_scaffolding(raw) == expected
    assert _sanitize_gateway_final_response("discord", raw) == expected


def test_served_footer_stripped_when_scaffold_present():
    footer = "glm-5.3-flash · 11% · ~ · 34s · 2 calls"
    raw = (
        "🕐 Injecting time\n"
        f"{REAL_PROSE}\n"
        f"{footer}\n"
    )
    expected = REAL_PROSE
    assert _strip_provider_scaffolding(raw) == expected
    assert _sanitize_gateway_final_response("discord", raw) == expected


def test_served_footer_kept_on_clean_message():
    raw = f"{REAL_PROSE}\n" "glm-5.3-flash · 11% · ~ · 34s · 2 calls\n"
    assert _strip_provider_scaffolding(raw) == raw
    assert _sanitize_gateway_final_response("discord", raw) == raw


def test_clean_message_byte_identical():
    raw = REAL_PROSE
    assert _strip_provider_scaffolding(raw) == raw
    assert _sanitize_gateway_final_response("discord", raw) == raw


def test_genuine_code_fence_untouched():
    raw = (
        "Use this snippet:\n"
        "```python\n"
        "print('hello')\n"
        "x = 1 + 2\n"
        "```\n"
        f"{REAL_PROSE}\n"
    )
    assert _strip_provider_scaffolding(raw) == raw
    assert _sanitize_gateway_final_response("discord", raw) == raw


def test_unclosed_fence_untouched():
    raw = (
        "Prefix.\n"
        "```\n"
        "MEMORY PROVIDER: Hindsight\n"
        "§ USER PROFILE\n"
        "📖 Reading skill foo\n"
    )
    assert _strip_provider_scaffolding(raw) == raw
    assert _sanitize_gateway_final_response("discord", raw) == raw


def test_mixed_scaffold_stripped_prose_kept():
    footer = "glm-5.3-flash · 11% · ~ · 34s · 2 calls"
    raw = (
        "🧠 Injecting Memory\n"
        "```\n"
        "MEMORY PROVIDER: Hindsight\n"
        "§ USER PROFILE\n"
        "════════════════════════════════\n"
        "```\n"
        "💭 **Reasoning:**\n"
        "```\n"
        "Thinking step one.\n"
        "_... (20 more lines)_\n"
        "```\n"
        "```\n"
        "📖 Reading skill docs\n"
        "💻 Merge the documentation PR\n"
        "```\n"
        "🕐 Injecting time\n"
        f"{REAL_PROSE}\n"
        f"{footer}\n"
    )
    expected = REAL_PROSE
    assert _strip_provider_scaffolding(raw) == expected
    assert _sanitize_gateway_final_response("discord", raw) == expected


def test_local_surface_passes_scaffold_through():
    raw = (
        "🧠 Injecting Memory\n"
        "```\n"
        "MEMORY PROVIDER: Hindsight\n"
        "```\n"
        f"{REAL_PROSE}\n"
    )
    assert _sanitize_gateway_final_response("local", raw) == raw


def test_prose_starting_with_injecting_not_stripped():
    prose = "Injecting dependencies is done via constructor injection."
    raw = (
        "🕐 Injecting time\n"
        f"{prose}\n"
    )
    expected = prose
    assert _strip_provider_scaffolding(raw) == expected
    assert _sanitize_gateway_final_response("discord", raw) == expected


def test_kept_fence_preserves_internal_blank_lines():
    fence_body = (
        "```python\n"
        "def a():\n"
        "    pass\n"
        "\n"
        "\n"
        "def b():\n"
        "    pass\n"
        "```"
    )
    raw = f"🕐 Injecting time\n{fence_body}\n{REAL_PROSE}\n"
    result = _strip_provider_scaffolding(raw)
    assert fence_body in result
    assert REAL_PROSE in result


def test_kept_fence_preserves_footer_shaped_line_inside():
    footer = "glm-5.3-flash · 11% · ~ · 34s · 2 calls"
    fence_body = f"```\n{footer}\nprint('ok')\n```"
    raw = f"🕐 Injecting time\n{fence_body}\n{REAL_PROSE}\n"
    result = _strip_provider_scaffolding(raw)
    assert fence_body in result
    assert REAL_PROSE in result
