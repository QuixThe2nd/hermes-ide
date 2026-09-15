"""The typed gateway-restart control outcome stays SILENT at the gateway.

A committed ``restart`` ends its calling agent turn with an intentionally
empty ``final_response`` and the typed result bit
``gateway_restart_queued=True``.  That emptiness is the contract: the restart
confirmation / drain / comeback UI is the only user-facing lifecycle output,
so no boundary may normalize it, fabricate prose for it, or deliver it.

These tests pin the seam at both levels:

* unit level — ``_is_gateway_restart_control_outcome`` and
  ``_exempt_from_empty_response_normalization`` answer only to the typed bit
  (and, for the exemption, to the existing forced-recovery control);
* turn level — the REAL ``TurnRunner.run_sync`` empty-response boundary keeps
  a restart-control turn result empty while an ordinary empty result still
  gets the normalizer's fabricated "no response was generated" prose.

The turn-level harness mirrors ``test_forced_resume_integration``: a stub
``AIAgent`` standing at the provider boundary returns the typed result dict,
so what reaches ``ctx.result_holder`` IS the turn result the delivery
boundaries consume.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.config import Platform
from gateway.run import (
    TurnRunner,
    _exempt_from_empty_response_normalization,
    _is_gateway_restart_control_outcome,
    _normalize_empty_agent_response,
)
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


# ---------------------------------------------------------------------------
# Unit level: the typed predicates
# ---------------------------------------------------------------------------


class TestGatewayRestartControlOutcome:
    def test_true_only_for_the_typed_bit(self):
        assert _is_gateway_restart_control_outcome(
            {"gateway_restart_queued": True}
        ) is True

    def test_false_for_everything_else(self):
        for not_control in (
            {},
            {"gateway_restart_queued": False},
            {"gateway_restart_queued": "yes"},
            {"final_response": ""},
            {"failed": True, "final_response": ""},
            None,
            "gateway_restart_queued",
            SimpleNamespace(gateway_restart_queued=True),
        ):
            assert _is_gateway_restart_control_outcome(not_control) is False


class TestEmptyResponseNormalizationExemption:
    def test_restart_control_outcome_is_exempt(self):
        assert (
            _exempt_from_empty_response_normalization(
                {"gateway_restart_queued": True, "final_response": ""}
            )
            is True
        )

    def test_forced_recovery_outcome_stays_exempt(self):
        assert (
            _exempt_from_empty_response_normalization(
                {"forced_recovery_control": True, "failed": True}
            )
            is True
        )

    def test_ordinary_results_are_not_exempt(self):
        # These MUST keep going through the fabricator — an ordinary silent
        # failure is indistinguishable from a crash, and the normalizer
        # exists to keep it from being swallowed (fix for #18765).
        for ordinary in (
            {"final_response": "", "api_calls": 1},
            {"final_response": "", "failed": True, "error": "boom"},
            {},
        ):
            assert _exempt_from_empty_response_normalization(ordinary) is False

    def test_normalizer_itself_still_fabricates_for_ordinary_empty(self):
        # Guard the contrast: without the typed bit, an empty completed turn
        # produces user-facing prose.
        prose = _normalize_empty_agent_response(
            {"final_response": "", "api_calls": 1}, "", history_len=3
        )
        assert prose.strip() != ""


# ---------------------------------------------------------------------------
# Turn level: the REAL TurnRunner empty-response boundary
# ---------------------------------------------------------------------------


class _RestartStubAgent:
    """Provider-boundary stub returning a typed turn result."""

    def __init__(self, **kwargs):
        self.model = kwargs["model"]
        self.session_id = kwargs["session_id"]
        self.context_compressor = SimpleNamespace(
            last_prompt_tokens=0, context_length=200_000
        )
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_reasoning_tokens = 0

    def run_conversation(self, message, **kwargs):
        result = {
            "final_response": "",
            "messages": [],
            "completed": True,
            "failed": False,
            "interrupted": False,
            "api_calls": 1,
            "tools": [],
        }
        if self._restart_control:
            result["gateway_restart_queued"] = True
        return result


class _RecordingStore:
    def __init__(self):
        self._entries = {}
        self.appended = []

    def append_to_transcript(self, session_id, message, skip_db=False):
        self.appended.append((session_id, message))

    def _save(self):
        pass


def _gateway_runner(store):
    runner = MagicMock()
    runner.config = SimpleNamespace(streaming=None)
    runner._provider_routing = {}
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._session_db = None
    runner._prefill_messages = None
    runner._pending_model_notes = {}
    runner._pending_skills_reload_notes = {}
    runner.session_store = store
    runner._get_system_prompt_for_channel.return_value = None
    runner._resolve_session_agent_runtime.return_value = ("test-model", {})
    runner._resolve_session_reasoning_config.return_value = None
    runner._resolve_session_service_tier.return_value = None
    runner._resolve_turn_agent_config.return_value = {
        "model": "test-model",
        "runtime": {},
    }
    runner._agent_config_signature.return_value = ("test-signature",)
    runner._extract_cache_busting_config.return_value = {}
    runner._refresh_fallback_model.return_value = None
    runner._consume_pending_native_image_paths.return_value = []
    runner._consume_pending_turn_sidecar_notes.return_value = []
    return runner


def _make_ctx(agent_cls, session_key="sk-restart"):
    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id="test-chat",
        user_id="test-user",
    )
    return TurnContext(
        source=source,
        message="restart the gateway",
        history=[],
        session_id="test-session",
        session_key=session_key,
        user_config={},
        AIAgent=agent_cls,
        resolve_display_setting=lambda *_args: False,
        _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )


def _run_restart_turn(agent_cls) -> dict:
    store = _RecordingStore()
    ctx = _make_ctx(agent_cls)
    # The processed turn result — what run_sync returns past the
    # empty-response boundary — is the dict the delivery layers consume.
    return TurnRunner(_gateway_runner(store), ctx).run_sync()


def test_restart_control_turn_result_stays_silent():
    """The TurnRunner must NOT let the empty-response fabricator turn the
    restart control's contractual silence into prose — and must carry the
    typed bit so downstream delivery boundaries can suppress too."""

    class _Agent(_RestartStubAgent):
        _restart_control = True

    result = _run_restart_turn(_Agent)

    assert result["final_response"] == ""
    assert result["gateway_restart_queued"] is True
    assert result.get("failed") is False


def test_ordinary_empty_turn_still_gets_normalized_prose():
    """Control: without the typed bit the same empty result IS normalized —
    the exemption is keyed on the bit, not on emptiness itself."""

    class _Agent(_RestartStubAgent):
        _restart_control = False

    result = _run_restart_turn(_Agent)

    assert result["final_response"].strip() != ""
    assert "no response was generated" in result["final_response"]
    assert not result.get("gateway_restart_queued")
