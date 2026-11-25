"""Destructive CLI session slashes run immediately — no confirm modal.

/new calls new_session without consulting any confirmation modal; its old
skip-token spellings are ordinary title text now. /reset (the retired alias of
/new, e5b347e4989) is no longer a session command at all: real process_command
dispatch must leave the session untouched and report the unknown command
instead of mutating anything.
"""

from __future__ import annotations

from unittest.mock import patch

from cli import HermesCLI


def _make_cli():
    """A real HermesCLI (no __init__ side effects) with a live session to observe."""
    c = HermesCLI.__new__(HermesCLI)
    c.config = {}
    c.session_id = "stub-session"
    c.conversation_history = [{"role": "user", "content": "hello"}]

    def _record_modal(**kw):
        raise AssertionError("destructive slash must not open a confirm modal")

    c._prompt_text_input_modal = _record_modal
    return c


def test_confirm_machinery_stays_retired():
    assert not hasattr(HermesCLI, "_confirm_destructive_slash")
    assert not hasattr(HermesCLI, "_split_destructive_skip")


def test_new_runs_without_confirm_modal():
    c = _make_cli()
    with patch.object(HermesCLI, "new_session") as new_session:
        assert c.process_command("/new") is True
        assert c.process_command("/new My Session") is True
        assert c.process_command("/new --yes My Session") is True
    # Skip tokens are ordinary title text now; there is no confirm to bypass.
    assert [call.kwargs["title"] for call in new_session.call_args_list] == [
        None,
        "My Session",
        "--yes My Session",
    ]


def test_retired_reset_leaves_session_untouched():
    """/reset no longer resolves to a handler, so dispatch falls through the
    unregistered path: new_session is never called and the live session (id +
    history) is exactly what it was. The unknown-command notice is printed,
    not swallowed."""
    c = _make_cli()
    with patch.object(HermesCLI, "new_session") as new_session:
        assert c.process_command("/reset") is True
        assert c.process_command("/reset now") is True
    new_session.assert_not_called()
    assert c.session_id == "stub-session"
    assert c.conversation_history == [{"role": "user", "content": "hello"}]
