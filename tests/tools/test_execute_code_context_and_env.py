"""Context propagation and child-environment contracts for execute_code workers."""

from __future__ import annotations

import concurrent.futures
import contextvars
import threading

from tools.thread_context import propagate_context_to_thread


def test_helper_propagates_contextvar_and_sudo_callback():
    from tools import terminal_tool as terminal

    probe: contextvars.ContextVar[str] = contextvars.ContextVar("worker_probe", default="unset")
    probe.set("parent-value")
    sentinel = object()
    terminal.set_sudo_password_callback(sentinel)
    try:
        seen = {}

        def worker():
            seen["probe"] = probe.get()
            seen["callback"] = terminal._get_sudo_password_callback()

        thread = threading.Thread(target=propagate_context_to_thread(worker))
        thread.start()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert seen == {"probe": "parent-value", "callback": sentinel}
    finally:
        terminal.set_sudo_password_callback(None)


def test_helper_clears_callbacks_on_teardown():
    from tools import terminal_tool as terminal

    sentinel = object()
    terminal.set_sudo_password_callback(sentinel)
    try:
        seen = {}

        def first():
            seen["during"] = terminal._get_sudo_password_callback()

        def second():
            seen["after"] = terminal._get_sudo_password_callback()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(propagate_context_to_thread(first)).result(timeout=5)
            executor.submit(second).result(timeout=5)

        assert seen["during"] is sentinel
        assert seen["after"] is None
    finally:
        terminal.set_sudo_password_callback(None)


def test_env_scrub_hermes_allowlist_and_secret_blocks():
    from tools.code_execution_env import _scrub_child_env

    env = {
        "HERMES_HOME": "/h", "HERMES_PROFILE": "p",
        "HERMES_CONFIG": "/c.yaml", "HERMES_ENV": "/e",
        "HERMES_DELEGATED_CHILD_CONTEXT": "1",
        "HERMES_BASE_URL": "https://x", "HERMES_INTERACTIVE": "1",
        "HERMES_CUSTOM_TASK": "t_parent",
        "SENTRY_DSN": "https://a@s.io/1", "SLACK_WEBHOOK": "https://h/x",
        "OPENAI_API_KEY": "sk", "GITHUB_TOKEN": "ghp",
        "PATH": "/usr/bin", "RANDOM_X": "y",
    }
    out = _scrub_child_env(env, is_passthrough=lambda _: False, is_windows=False)

    for kept in (
        "HERMES_HOME", "HERMES_PROFILE", "HERMES_CONFIG", "HERMES_ENV",
        "HERMES_DELEGATED_CHILD_CONTEXT", "PATH",
    ):
        assert kept in out, f"{kept} should be kept"
    for dropped in (
        "HERMES_BASE_URL", "HERMES_INTERACTIVE", "HERMES_CUSTOM_TASK",
        "SENTRY_DSN", "SLACK_WEBHOOK", "OPENAI_API_KEY", "GITHUB_TOKEN", "RANDOM_X",
    ):
        assert dropped not in out, f"{dropped} should be dropped"


def test_env_scrub_passthrough_overrides_secret_block():
    from tools.code_execution_env import _scrub_child_env

    out = _scrub_child_env(
        {"MY_SERVICE_DSN": "value"},
        is_passthrough=lambda key: key == "MY_SERVICE_DSN",
        is_windows=False,
    )

    assert out.get("MY_SERVICE_DSN") == "value"


def test_env_scrub_no_log_when_nothing_dropped(caplog):
    import logging

    from tools.code_execution_env import _scrub_child_env

    with caplog.at_level(logging.DEBUG, logger="tools.code_execution_tool"):
        _scrub_child_env(
            {"HERMES_HOME": "/h", "PATH": "/usr/bin"},
            is_passthrough=lambda _: False,
            is_windows=False,
        )
    assert "dropped" not in "\n".join(record.getMessage() for record in caplog.records)
