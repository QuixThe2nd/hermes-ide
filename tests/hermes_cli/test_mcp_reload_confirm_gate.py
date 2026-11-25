"""Contract: /reload-mcp requires affirmative one-time consent.

Reloading the MCP tool set invalidates the provider prompt cache for the
active session — expensive on long-context / high-reasoning models — so the
reload only runs after an explicit "Approve Once" answer to the slash-confirm
modal.

The legacy ``approvals.mcp_reload_confirm`` config gate (and the whole
``approvals`` config section) was retired with the approval subsystem in
a51d5ec8d47 (#663), after the human-approval gates themselves went in
d0906d4e30b (#639).  Consent is now per invocation inside
``cli_info_mixin._confirm_and_reload_mcp``: it never reads config, so no
config state can bypass or pre-answer the prompt.  These tests pin that
surviving surface through the real slash dispatch path.
"""

import pytest

from unittest.mock import MagicMock, patch

from cli import HermesCLI


def _make_cli():
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj._app = None
    cli_obj._last_invalidate = 0.0
    cli_obj._command_running = False
    cli_obj._command_status = ""
    return cli_obj


def _run_reload_mcp(cli_obj, raw_answer):
    """Dispatch /reload-mcp with the confirm modal answering ``raw_answer``.

    Returns (reload_mock, modal_mock); both are already consumed.
    """
    reload_mock = MagicMock()
    modal_mock = MagicMock(return_value=raw_answer)
    with patch.object(cli_obj, "_reload_mcp", reload_mock), \
         patch.object(cli_obj, "_prompt_text_input_modal", modal_mock), \
         patch.object(cli_obj, "_invalidate"):
        assert cli_obj.process_command("/reload-mcp")
    return reload_mock, modal_mock


class TestAffirmativeConsent:
    @pytest.mark.parametrize("answer", ["1", "once", "yes"])
    def test_affirmative_choice_reloads_under_busy_state(self, answer, capsys):
        cli_obj = _make_cli()
        reload_mock = MagicMock()
        seen = {}
        reload_mock.side_effect = lambda: seen.update(
            running=cli_obj._command_running, status=cli_obj._command_status)
        with patch.object(cli_obj, "_reload_mcp", reload_mock), \
             patch.object(cli_obj, "_prompt_text_input_modal", return_value=answer), \
             patch.object(cli_obj, "_invalidate") as invalidate_mock:
            assert cli_obj.process_command("/reload-mcp")

        assert reload_mock.call_count == 1
        # The reload runs inside the busy-command indicator, with the
        # slow-command status for /reload-mcp, and the busy state is cleared
        # once it finishes.
        assert seen == {"running": True, "status": "Reloading MCP servers..."}
        assert cli_obj._command_running is False
        assert cli_obj._command_status == ""
        assert invalidate_mock.call_count == 2  # enter + exit repaints
        assert "⏳ Reloading MCP servers..." in capsys.readouterr().out

    def test_each_invocation_asks_again(self):
        """Consent is one-time by design: a second /reload-mcp prompts again
        (there is no "always approve" persistence left to rely on)."""
        cli_obj = _make_cli()
        reload_mock = MagicMock()
        with patch.object(cli_obj, "_reload_mcp", reload_mock), \
             patch.object(cli_obj, "_prompt_text_input_modal", side_effect=["1", "2"]), \
             patch.object(cli_obj, "_invalidate"):
            assert cli_obj.process_command("/reload-mcp")
            assert cli_obj.process_command("/reload-mcp")

        assert reload_mock.call_count == 1


class TestRefusalBlocksReload:
    @pytest.mark.parametrize("answer", ["2", "cancel", "no"])
    def test_refusal_does_not_reload(self, answer, capsys):
        cli_obj = _make_cli()
        reload_mock, _ = _run_reload_mcp(cli_obj, answer)

        assert reload_mock.call_count == 0
        out = capsys.readouterr().out
        assert "cancelled" in out
        assert "⏳" not in out
        assert cli_obj._command_running is False

    def test_no_input_does_not_reload(self, capsys):
        cli_obj = _make_cli()
        reload_mock, _ = _run_reload_mcp(cli_obj, None)

        assert reload_mock.call_count == 0
        assert "no input" in capsys.readouterr().out

    def test_unknown_choice_does_not_reload(self, capsys):
        cli_obj = _make_cli()
        reload_mock, _ = _run_reload_mcp(cli_obj, "banana")

        assert reload_mock.call_count == 0
        assert "Unrecognized choice 'banana'" in capsys.readouterr().out


class TestRetiredConfigCannotBypass:
    def test_obsolete_approval_config_does_not_skip_consent(self, capsys):
        """``approvals.mcp_reload_confirm: false`` in the loaded config is
        inert: the handler still consults the modal and still refuses to
        reload without an affirmative answer."""
        cli_obj = _make_cli()
        legacy_cfg = {"approvals": {"mcp_reload_confirm": False}}
        reload_mock = MagicMock()
        modal_mock = MagicMock(return_value=None)
        with patch.object(cli_obj, "_reload_mcp", reload_mock), \
             patch.object(cli_obj, "_prompt_text_input_modal", modal_mock), \
             patch("cli.load_cli_config", return_value=legacy_cfg), \
             patch.object(cli_obj, "_invalidate"):
            assert cli_obj.process_command("/reload-mcp")

        assert modal_mock.call_count == 1
        assert reload_mock.call_count == 0
        assert "no input" in capsys.readouterr().out
