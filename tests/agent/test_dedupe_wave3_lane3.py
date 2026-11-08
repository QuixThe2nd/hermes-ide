"""Anti-regression guard for the wave-3 lane-3 shadowed-method consolidation.

The September restore left method bodies on both a runtime-winning class and one of its
mixins. Each listed pair now has one class-body definition, on the class whose behavior
was already live. The copied release-checksum parser also has one canonical owner.
"""

from agent.activity_tracking import ActivityTrackingMixin
from agent.api_request_hooks import ApiRequestHooksMixin
from agent.interrupt_control import InterruptControlMixin
from agent.proxy_sources import iron_proxy
from agent.rate_limit_credits import RateLimitCreditsMixin
from agent.secret_sources import bitwarden
from agent.session_persistence import SessionPersistenceMixin
from agent.turn_explainers import TurnExplainersMixin
from agent.turn_facade import TurnFacadeMixin
from gateway.platforms.access_policy_mixin import OwnAccessPolicyMixin
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.api_server_openai_routes import OpenAICompatRoutesMixin
from gateway.platforms.whatsapp_cloud import WhatsAppCloudAdapter
from gateway.platforms.whatsapp_common import WhatsAppBehaviorMixin
from gateway.stream_consumer import GatewayStreamConsumer
from gateway.stream_consumer_transport import StreamTransportMixin
from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
from run_agent import AIAgent

_CONSOLIDATED = (
    (AIAgent, ActivityTrackingMixin, (
        "_liveness_activity_lock", "_reset_activity_labels_after_turn", "_touch_activity",
    )),
    (AIAgent, ApiRequestHooksMixin, ("_invoke_api_request_error_hook",)),
    (AIAgent, InterruptControlMixin, (
        "_drain_pending_redirect", "_drain_pending_steer", "_has_pending_redirect",
        "clear_interrupt", "hard_interrupt", "interrupt", "redirect", "steer",
    )),
    (AIAgent, RateLimitCreditsMixin, (
        "_capture_credits", "_capture_rate_limits", "_check_openrouter_cache_status",
        "_credits_notices_enabled", "_emit_credits_notices",
    )),
    (AIAgent, SessionPersistenceMixin, (
        "_apply_persist_user_message_override", "_convert_to_trajectory_format",
        "_drop_trailing_empty_response_scaffolding", "_dump_api_request_debug",
        "_flush_messages_to_session_db", "_flush_messages_to_session_db_unlocked",
        "_format_tools_for_system_message", "_get_messages_up_to_last_assistant",
        "_persist_session", "_repair_message_sequence",
    )),
    (AIAgent, TurnExplainersMixin, (
        "_file_mutation_verifier_enabled", "_record_file_mutation_result",
        "_turn_completion_explainer_enabled",
    )),
    (AIAgent, TurnFacadeMixin, ("run_conversation",)),
    (GatewayStreamConsumer, StreamTransportMixin, (
        "_edit_existing", "_first_send", "_try_fresh_final",
    )),
    (WhatsAppBehaviorMixin, OwnAccessPolicyMixin, ("_entry_matches", "_live_dm_allow_from")),
    (APIServerAdapter, OpenAICompatRoutesMixin, ("_handle_chat_completions", "_handle_responses")),
    (WhatsAppCloudAdapter, WhatsAppBehaviorMixin, ("_effective_reply_prefix",)),
)


def test_shadowed_methods_have_one_live_class_definition():
    for live_class, former_dead_class, names in _CONSOLIDATED:
        for name in names:
            assert name not in vars(former_dead_class), (
                f"{former_dead_class.__name__}.{name} reintroduced a shadowed twin"
            )
            assert name in vars(live_class), f"{live_class.__name__}.{name} was lost"
            assert callable(getattr(live_class, name, None)), name
            owner = next(cls for cls in live_class.__mro__ if name in vars(cls))
            assert owner is live_class, f"{name} resolves through {owner.__name__} instead"


def test_bridge_keeps_its_non_shadowed_reply_prefix_implementation():
    """Removing the Cloud twin must not remove the Baileys adapter's prefix behavior."""
    assert "_effective_reply_prefix" in vars(WhatsAppAdapter)
    assert callable(WhatsAppAdapter._effective_reply_prefix)


def test_checksum_parser_is_shared_from_bitwarden(tmp_path):
    assert iron_proxy._expected_sha256 is bitwarden._expected_sha256
    assert iron_proxy._expected_sha256.__module__ == bitwarden.__name__

    checksum = tmp_path / "checksums.txt"
    digest = "a" * 64
    checksum.write_text(f"{digest}  artifact.tar.gz\n", encoding="utf-8")
    assert iron_proxy._expected_sha256(checksum, "artifact.tar.gz") == digest
