"""Anti-regression guard: byte-identical GatewayRunner/TurnRunner duplicates stay consolidated.

History: the Sep-2026 mixin split (run.py -> run_*.py) was clobbered by a repo-restore, leaving
both the extracted mixin definitions AND the monolith copies in ``gateway/run.py``. The class-body
copies in ``run.py`` shadowed their byte-identical mixin counterparts via the MRO. They were
consolidated (wave 1: the byte-identical clusters) — this test fails if any listed pair is
re-introduced, i.e. if a method here appears in ``GatewayRunner.__dict__`` (or the ``TurnRunner``
class bodies) while the mixin already defines it.
"""

import gateway.run as run_mod
from gateway.run_adapters import GatewayAdapterLifecycleMixin
from gateway.run_agent_cache import GatewayAgentCacheMixin
from gateway.run_busy import GatewayBusySessionMixin
from gateway.run_config_loaders import GatewayConfigLoadersMixin
from gateway.run_inbound import GatewayInboundMixin
from gateway.run_shutdown import GatewayShutdownMixin
from gateway.run_startup import GatewayStartupMixin
from gateway.run_topics import GatewayTopicThreadsMixin
from gateway.run_turn_runner import TurnRunner as ExtractedTurnRunner, _TurnRunnerCore

# (method name, module-level mixin class) pairs that were byte-identical duplicates of
# GatewayRunner class-body definitions in gateway/run.py.
CONSOLIDATED_MIXIN_METHODS = (
    ("_request_clean_exit", GatewayAdapterLifecycleMixin),
    ("_retain_background_task", GatewayAdapterLifecycleMixin),
    ("_goal_still_active_for_session", GatewayBusySessionMixin),
    ("_lookup_session_id_under_store_lock", GatewayBusySessionMixin),
    ("_demote_interrupt", GatewayBusySessionMixin),
    ("_try_agent_verb", GatewayBusySessionMixin),
    ("_agent_has_active_subagents", GatewayBusySessionMixin),
    ("_session_has_compression_in_flight", GatewayBusySessionMixin),
    ("_steer_running_agent", GatewayBusySessionMixin),
    ("_steer_active_subagents", GatewayBusySessionMixin),
    ("_busy_steer_command", GatewayBusySessionMixin),
    ("_resolve_busy_steer_or_redirect", GatewayBusySessionMixin),
    ("_hm_text_only", GatewayInboundMixin),
    ("_hm_busy_steer", GatewayInboundMixin),
    ("_clear_plugin_message_injector", GatewayInboundMixin),
    ("_busy_profile_name_for_source", GatewayConfigLoadersMixin),
    ("_ensure_hosted_room_worker", GatewayStartupMixin),
    ("_start_free_tier_bootstrap", GatewayStartupMixin),
    ("_stop_systemd_watchdog", GatewayShutdownMixin),
    ("wait_for_shutdown", GatewayShutdownMixin),
    ("_telegram_topic_root_lobby_message", GatewayTopicThreadsMixin),
    ("_telegram_topic_root_new_message", GatewayTopicThreadsMixin),
    ("_telegram_topic_help_text", GatewayTopicThreadsMixin),
    ("_current_session_run_generation", GatewayAgentCacheMixin),
    ("_is_session_run_current", GatewayAgentCacheMixin),
    ("_set_pending_turn_sidecar_notes", GatewayAgentCacheMixin),
)


def test_consolidated_methods_defined_exactly_once():
    """Each consolidated method lives on its mixin only — not re-defined on GatewayRunner.

    A definition in ``GatewayRunner.__dict__`` would silently shadow the mixin copy (MRO),
    re-creating the duplicate-method drift this consolidation removed.
    """
    shadowed = [
        name for name, mixin in CONSOLIDATED_MIXIN_METHODS
        if name in run_mod.GatewayRunner.__dict__
    ]
    assert not shadowed, (
        f"GatewayRunner re-defines mixin-owned methods {shadowed}; "
        "the mixin definition must stay the single definition"
    )
    missing = [
        (name, mixin.__name__) for name, mixin in CONSOLIDATED_MIXIN_METHODS
        if name not in mixin.__dict__
    ]
    assert not missing, f"mixin definitions disappeared: {missing}"
    # The consolidated methods must still resolve on the composed runner.
    for name, _mixin in CONSOLIDATED_MIXIN_METHODS:
        assert callable(getattr(run_mod.GatewayRunner, name, None)), name


def test_turn_runner_constructor_defined_exactly_once():
    """TurnRunner.__init__ exists only on _TurnRunnerCore; neither TurnRunner class re-defines it."""
    assert "__init__" not in vars(run_mod.TurnRunner)
    assert "__init__" not in vars(ExtractedTurnRunner)
    assert "__init__" in vars(_TurnRunnerCore)
    # Both shapes construct through the shared constructor.
    sentinel_runner, sentinel_ctx = object(), object()
    for cls in (run_mod.TurnRunner, ExtractedTurnRunner):
        instance = cls(sentinel_runner, sentinel_ctx)
        assert instance._runner is sentinel_runner
        assert instance._ctx is sentinel_ctx
