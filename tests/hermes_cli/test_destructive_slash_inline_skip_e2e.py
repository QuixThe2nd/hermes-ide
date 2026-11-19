"""Destructive CLI session slashes run immediately — no confirm modal.

``/new``, ``/reset``, and the old skip-token spellings (``/reset now``,
``/new --yes ...``) call ``new_session`` without consulting the modal.
Skip tokens are ordinary title text now; there is no confirm to bypass.
"""

from __future__ import annotations

from types import SimpleNamespace

from cli import HermesCLI


def _make_cli_stub():
    new_session_calls = []
    modal_calls = []

    def _capture_new_session(title=None, silent=False):
        new_session_calls.append({"title": title, "silent": silent})

    def _record_modal(**kw):
        modal_calls.append(kw)
        raise AssertionError("destructive slash must not open a confirm modal")

    self_ = SimpleNamespace(
        _app=None,
        _prompt_text_input_modal=_record_modal,
        new_session=_capture_new_session,
        console=SimpleNamespace(clear=lambda: None),
        compact=False,
        model="stub-model",
        session_id="stub-session",
        enabled_toolsets=[],
        _pending_title=None,
        _session_db=None,
    )
    self_._slash_handler = HermesCLI._slash_handler
    self_._cmd_new = HermesCLI._cmd_new.__get__(self_, type(self_))
    self_.process_command = HermesCLI.process_command.__get__(self_, type(self_))
    return self_, new_session_calls, modal_calls


def test_new_and_reset_run_without_confirm_modal():
    self_, calls, modal_calls = _make_cli_stub()

    assert not hasattr(HermesCLI, "_confirm_destructive_slash")
    assert not hasattr(HermesCLI, "_split_destructive_skip")

    assert self_.process_command("/reset") is True
    assert self_.process_command("/reset now") is True
    assert self_.process_command("/new --yes My Session") is True
    assert self_.process_command("/new My Session") is True

    assert modal_calls == []
    assert [c["title"] for c in calls] == [
        None,
        "now",
        "--yes My Session",
        "My Session",
    ]
