#!/usr/bin/env python3
"""AIAgent: the tool-calling agent runner (conversation loop, tool execution, session lifecycle).

    from run_agent import AIAgent
    agent = AIAgent(base_url="http://localhost:30000/v1", model="claude-opus-4-20250514")
    response = agent.run_conversation("Tell me about the latest Python updates")
"""

# hermes_bootstrap must be the very first import (UTF-8 stdio on Windows; no-op on POSIX).
try:
    import hermes_bootstrap  # noqa: F401
except ModuleNotFoundError:
    pass  # partial `hermes update` — only skips the Windows UTF-8 stdio setup

import json
import logging
logger = logging.getLogger(__name__)
import os
import re
import sys
import time
import threading
import uuid
import warnings
from typing import List, Dict, Any, Optional, Callable
from datetime import datetime
from pathlib import Path

from hermes_constants import get_hermes_home


def _launch_cwd_for_session(source: str) -> Optional[str]:
    """cwd to stamp on a new session row (``hermes -c`` / ``--resume``), or None.

    Only local CLI sessions record one: gateway/cron/remote backends (non-"local" ``TERMINAL_ENV``) have no
    stable host cwd for the agent's tools.
    """
    if source != "cli" or (os.environ.get("TERMINAL_ENV") or "local").strip().lower() not in ("", "local"):
        return None
    try:
        return os.getcwd()
    except OSError:  # cwd was unlinked out from under us
        return None


def _session_source_for_agent(platform: Optional[str]) -> str:
    try:
        from gateway.session_context import get_session_env

        source = get_session_env("HERMES_SESSION_SOURCE", "")
    except Exception:
        source = os.environ.get("HERMES_SESSION_SOURCE", "")
    return str(source or "").strip() or platform or "cli"


def _gateway_origin_json(agent: "AIAgent") -> Optional[str]:
    """Gateway routing ``origin_json`` for a session row; None when the agent carries no gateway identity.

    Mirrors ``SessionSource.to_dict()`` so state.db consumers see the same fields ``record_gateway_session_peer`` writes.
    """
    chat_id = getattr(agent, "_chat_id", None)
    session_key = getattr(agent, "_gateway_session_key", None)
    user_id = getattr(agent, "_user_id", None)
    if not (chat_id or session_key or user_id):
        return None
    origin: Dict[str, Any] = {
        "platform": getattr(agent, "platform", None) or "", "chat_id": chat_id,
        "chat_name": getattr(agent, "_chat_name", None), "chat_type": getattr(agent, "_chat_type", None) or "dm",
        "user_id": user_id, "user_name": getattr(agent, "_user_name", None), "thread_id": getattr(agent, "_thread_id", None),
    }
    if getattr(agent, "_user_id_alt", None):
        origin["user_id_alt"] = agent._user_id_alt
    profile = getattr(agent, "_profile_name", None)
    if not profile:
        try:
            from hermes_cli.profiles import get_active_profile_name
            profile = get_active_profile_name()
        except Exception:
            profile = None
        if profile == "default":
            profile = None
    if profile:
        origin["profile"] = profile
    try:
        return json.dumps(origin)
    except Exception:
        return None


from agent.iteration_budget import IterationBudget
from hermes_cli.env_loader import load_hermes_dotenv
from hermes_cli.config_defaults import DEFAULT_MAX_TURNS
from hermes_cli.timeouts import (
    get_provider_request_timeout,
    get_provider_stale_timeout,
)

_hermes_home = get_hermes_home()  # read by agent_init via _ra()._hermes_home
_loaded_env_paths = load_hermes_dotenv(hermes_home=_hermes_home, project_env=Path(__file__).parent / '.env')
for _env_path in _loaded_env_paths:
    logger.info("Loaded environment variables from %s", _env_path)
if not _loaded_env_paths:
    logger.info("No .env file found. Using system environment variables.")


from model_tools import get_toolset_for_tool
from tools.terminal_tool_lifecycle import cleanup_vm, get_active_env
from tools.interrupt import set_interrupt as _set_interrupt
from tools.browser_tool_lifecycle import cleanup_browser

from agent.memory_provider import is_trivial_prompt
from agent.client_lifecycle import ClientLifecycleMixin
from agent.stream_delivery import StreamDeliveryMixin
from agent.status_output import StatusOutputMixin
from agent.api_request_hooks import ApiRequestHooksMixin
from agent.api_error_summary import ApiErrorSummaryMixin
from agent.interrupt_control import InterruptControlMixin
from agent.turn_explainers import TurnExplainersMixin
from agent.activity_tracking import ActivityTrackingMixin
from agent.rate_limit_credits import RateLimitCreditsMixin
from agent.session_persistence import SessionPersistenceMixin
# Fork-local copies of persistence/flush helpers (kept in this module; they shadow the
# compact SessionPersistenceMixin bodies in the MRO) reference these decomposed
# siblings' helpers — import them here so the names resolve.
from agent.session_persistence import _safe_session_filename_component  # noqa: F401
from agent.context_compressor import (  # noqa: F401
    COMPRESSED_SUMMARY_METADATA_KEY, ContextCompressor, _DB_PERSISTED_MARKER, user_originated_turn_view)
from agent.memory_manager import sanitize_context  # noqa: F401
from agent.redact import redact_sensitive_text  # noqa: F401
from agent.usage_pricing import normalize_usage  # noqa: F401
from agent.interrupt_compat import request_hard_interrupt  # noqa: F401
from agent.compression_facade import CompressionFacadeMixin
from agent.turn_facade import TurnFacadeMixin
from agent.vision_message_prep import VisionMessagePrepMixin
from agent.reasoning_params import ReasoningParamsMixin
from agent.lazy_forward import forward as _forward, forward_static as _forward_static
from agent.session_activity import ActivityProvenance
from agent.model_metadata import is_local_endpoint
from agent.message_sanitization import (
    coalesce_tool_call_id as _sanitize_coalesce_tool_call_id,
    deterministic_call_id as _codex_deterministic_call_id,
    uniquify_tool_call_ids as _sanitize_uniquify_tool_call_ids,
)
from agent.codex_responses_adapter import (
    _derive_responses_function_call_id as _codex_derive_responses_function_call_id,
    _split_responses_tool_id as _codex_split_responses_tool_id,
    _summarize_user_message_for_log,
)
from agent.tool_guardrails import (
    ToolGuardrailDecision,
    append_toolguard_guidance,
    toolguard_synthetic_result,
)
from agent.tool_result_classification import (
    FILE_MUTATING_TOOL_NAMES as _FILE_MUTATING_TOOLS,
    file_mutation_result_landed,
)
from agent.trajectory import (
    convert_scratchpad_to_think,
    save_trajectory as _save_trajectory_to_file,
)
from agent.tool_dispatch_helpers import (
    _should_parallelize_tool_batch,  # noqa: F401  # re-exported for tests that `from run_agent import _should_parallelize_tool_batch`
    _is_destructive_command,  # noqa: F401  # re-exported for tests that access `run_agent._is_destructive_command`
    _extract_parallel_scope_path,  # noqa: F401  # re-exported for tests that `from run_agent import _extract_parallel_scope_path`
    _paths_overlap,  # noqa: F401  # re-exported for tests that `from run_agent import _paths_overlap`
    _is_multimodal_tool_result,
    _multimodal_text_summary,
    _append_subdir_hint_to_multimodal,  # noqa: F401  # re-exported for tests that `from run_agent import _append_subdir_hint_to_multimodal`
    _extract_file_mutation_targets,
    _extract_landed_file_mutation_paths,
    _extract_error_preview,
    _trajectory_normalize_msg,  # noqa: F401  # re-exported for tests that `from run_agent import _trajectory_normalize_msg`
)
from utils import atomic_json_write, base_url_host_matches, base_url_hostname, env_float, is_truthy_value, model_forces_max_completion_tokens


# Internal flags that mark a message as ephemeral empty-response/prefill
# recovery scaffolding: the synthetic assistant "(empty)" turn and user nudge
# injected after an empty response, the terminal "(empty)" sentinel, and the
# thinking-only prefill placeholder. These exist only to drive the next API
# retry; the in-memory loop pops them before appending the real response.
# Persistence must mirror that, otherwise an append-only flush can commit them
# to the session store and a resumed session replays synthetic "(empty)"/nudge
# turns as if they were genuine context.
_EPHEMERAL_SCAFFOLDING_FLAGS = (
    "_empty_recovery_synthetic",
    "_empty_terminal_sentinel",
    "_thinking_prefill",
    # verify-on-stop and pre_verify nudges append a synthetic user nudge to
    # keep the agent going one more turn before it can claim completion.
    # The nudge exists only to drive the verification loop; persisting it
    # poisons the resumed transcript and breaks prompt-prefix cache reuse
    # on later turns. The assistant candidate is NOT synthetic — it is
    # persisted and emitted as an interim message (#65919).
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
    # pre_turn_end end-of-turn nudge: same synthetic-user-nudge shape
    "_pre_turn_end_synthetic",
    # kanban worker stop-guard: narrated exit without kanban_complete/block
    "_kanban_stop_synthetic",
    # dropped tool-call re-prompt pair (finish_reason=tool_calls with an
    # empty tool_calls array): the interim narration-only assistant turn
    # and the "issue the actual tool call now" user nudge exist only to
    # drive the bounded retry. Persisting them would replay the internal
    # retry instruction as user-authored context on resume.
    "_dropped_toolcall_nudge",
)


def _is_ephemeral_scaffolding(msg: Any) -> bool:
    """Return True when ``msg`` is internal recovery scaffolding that must never
    be persisted to the durable transcript (SQLite session store or JSON log)."""
    return isinstance(msg, dict) and any(
        msg.get(flag) for flag in _EPHEMERAL_SCAFFOLDING_FLAGS
    )


_MAX_TOOL_WORKERS = 8


# Spawn the OpenRouter pre-warm thread once per process, not per AIAgent (gateway thread leak).
_openrouter_prewarm_done = threading.Event()


def _quietly(fn: Callable, *args, **kwargs) -> None:
    """Run one teardown step, swallowing any exception so sibling steps still run."""
    try:
        fn(*args, **kwargs)
    except Exception:
        pass


def _call_engine_hook(engine: Any, hook: str, *args, **kwargs) -> None:
    """Invoke an optional context-engine lifecycle hook; failures are logged, never raised."""
    if not hasattr(engine, hook):
        return
    try:
        getattr(engine, hook)(*args, **kwargs)
    except Exception as exc:
        logger.debug("context engine %s during transition: %s", hook, exc)


def _positive_int(value: Any) -> Optional[int]:
    """``value`` when it is a real positive int (bools excluded), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _review_should_defer(agent: Any, task_cfg: Optional[Dict[str, Any]]) -> bool:
    """True when an automatic background review targets the managed local runtime under ``defer: auto``."""
    from agent.review_idle_queue import defer_mode, review_targets_managed_local
    return defer_mode(task_cfg) == "auto" and review_targets_managed_local(agent, task_cfg)


def _review_queue_key(agent: Any) -> str:
    return str(getattr(agent, "session_id", None) or id(agent))


def _notify_context_engine_session_end(agent: Any, messages: Optional[list]) -> None:
    """Tell the context engine the session ended (flush DAG, close DBs) at the same lifecycle moment as the
    memory manager, so per-session engine state never leaks into the next session."""
    engine = getattr(agent, "context_compressor", None)
    if engine:
        _quietly(lambda: engine.on_session_end(agent.session_id or "", messages or []))


def _pool_may_recover_from_rate_limit(pool) -> bool:
    """Wait for credential-pool rotation (True) or fall back to ``fallback_model`` (False) after a 429.

    Rotation only helps when the pool has somewhere to go; a single-credential pool would retry the same quota.

    See issues #11314 and #13636.
    """
    return pool is not None and pool.has_available() and len(pool.entries()) > 1


class _StreamErrorEvent(Exception):
    """Provider error synthesized from a standalone Responses ``type=error`` SSE frame (Codex-style backends).

    Gives ``_summarize_api_error`` / the entitlement detector the familiar ``.body`` / ``.status_code`` shape.
    """

    def __init__(self, message: str, *, code: Optional[str] = None, param: Optional[str] = None,
                 status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.message, self.code, self.param, self.status_code = message, code, param, status_code
        # OpenAI SDK-shaped body so _extract_api_error_context / _summarize_api_error / classify_api_error pick it up.
        self.body: Dict[str, Any] = {"error": {"message": message, "code": code, "param": param, "type": "error"}}


class AIAgent(
    ClientLifecycleMixin, StreamDeliveryMixin, StatusOutputMixin, ApiRequestHooksMixin, ApiErrorSummaryMixin,
    InterruptControlMixin, TurnExplainersMixin, ActivityTrackingMixin, RateLimitCreditsMixin,
    SessionPersistenceMixin, CompressionFacadeMixin, TurnFacadeMixin, VisionMessagePrepMixin, ReasoningParamsMixin,
):
    """AI Agent with tool calling capabilities."""

    _TOOL_CALL_ARGUMENTS_CORRUPTION_MARKER = (
        "[hermes-agent: tool call arguments were corrupted in this session and "
        "have been dropped to keep the conversation alive. See issue #15236.]"
    )

    @property
    def base_url(self) -> str:
        return self._base_url

    @base_url.setter
    def base_url(self, value: str) -> None:
        self._base_url = value
        self._base_url_lower = value.lower() if value else ""
        self._base_url_hostname = base_url_hostname(value)

    def __init__(
        self,
        base_url: str = None, api_key: str = None, provider: str = None, api_mode: str = None,
        acp_command: str = None, acp_args: list[str] | None = None, command: str = None, args: list[str] | None = None,
        model: str = "",
        max_iterations: int = DEFAULT_MAX_TURNS,  # Default turn budget (shared with subagents); unlimited spellings → sys.maxsize
        tool_delay: float = None,  # Deprecated: accepted for compatibility, ignored
        enabled_toolsets: List[str] = None,
        disabled_toolsets: List[str] = None,
        save_trajectories: bool = False,
        verbose_logging: bool = False,
        quiet_mode: bool = False,
        tool_progress_mode: str = "all",
        ephemeral_system_prompt: str = None,
        log_prefix_chars: int = 100,
        log_prefix: str = "",
        providers_allowed: List[str] = None,
        providers_ignored: List[str] = None,
        providers_order: List[str] = None,
        provider_sort: str = None,
        provider_require_parameters: bool = False,
        provider_data_collection: str = None,
        openrouter_min_coding_score: Optional[float] = None,
        session_id: str = None,
        tool_progress_callback: callable = None, tool_start_callback: callable = None,
        tool_complete_callback: callable = None, thinking_callback: callable = None,
        reasoning_callback: callable = None, clarify_callback: callable = None,
        read_terminal_callback: callable = None, read_preview_callback: callable = None,
        drive_preview_callback: callable = None, read_window_below_callback: callable = None,
        setup_mcp_callback: callable = None, tour_callback: callable = None, step_callback: callable = None,
        stream_delta_callback: callable = None, interim_assistant_callback: callable = None,
        tool_gen_callback: callable = None, status_callback: callable = None,
        notice_callback: callable = None, notice_clear_callback: callable = None,
        event_callback: Optional[Callable[[str, dict], None]] = None,
        reaction_callback: Optional[Callable[[str], None]] = None,
        max_tokens: int = None, reasoning_config: Dict[str, Any] = None, service_tier: str = None,
        request_overrides: Dict[str, Any] = None, prefill_messages: List[Dict[str, Any]] = None,
        platform: str = None, user_id: str = None, user_id_alt: str = None, user_name: str = None,
        chat_id: str = None, chat_name: str = None, chat_type: str = None, thread_id: str = None,
        gateway_session_key: str = None,
        skip_context_files: bool = False, load_soul_identity: bool = False,
        skip_memory: bool = False, skip_background_review: bool = False,
        session_db=None, parent_session_id: str = None,
        iteration_budget: "IterationBudget" = None, run_budget_seconds: Optional[float] = None,
        fallback_model: Dict[str, Any] = None, credential_pool=None,
        checkpoints_enabled: bool = False, checkpoint_max_snapshots: int = 20,
        checkpoint_max_total_size_mb: int = 500, checkpoint_max_file_size_mb: int = 10,
        pass_session_id: bool = False, requested_provider: str = None,
        capabilities: Dict[str, bool] | None = None,
    ):
        """Forwarder — see ``agent.agent_init.init_agent`` (same keyword parameters, minus ``tool_delay``)."""
        init_kwargs = {k: v for k, v in locals().items() if k not in ("self", "tool_delay")}
        if tool_delay is not None:
            warnings.warn("tool_delay is deprecated and ignored; sequential tool calls "
                          "no longer sleep between executions.", DeprecationWarning, stacklevel=2)
        from agent.agent_init import init_agent
        init_agent(self, **init_kwargs)

    def _get_session_db_for_recall(self):
        """SessionDB for recall, opening the default state DB when no ``session_db`` was passed so the
        advertised ``session_search`` tool stays usable."""
        # Persistence-isolated forks (background review) must not lazily open the canonical state DB —
        # that would re-arm the flush to write the fork's harness turn into the user's real session.
        if getattr(self, "_persist_disabled", False):
            return None
        if self._session_db is not None:
            return self._session_db
        try:
            from hermes_state_registry import acquire

            self._session_db = acquire()
            self._owns_session_db = True  # we opened it, so close() must release it
            return self._session_db
        except Exception:
            logger.debug("SessionDB unavailable for recall", exc_info=True)
            return None

    def _session_row_model_config(self) -> Any:
        """``model_config`` for the session row: the init config plus the live YOLO bypass.

        The row is created lazily on the first turn, so this is the only chance to record a pre-first-turn
        /yolo toggle for ``hermes --resume``.
        """
        model_config = self._session_init_model_config
        try:
            from tools.approval import is_session_yolo_enabled
            if is_session_yolo_enabled(self.session_id):
                model_config = dict(model_config or {})
                model_config["yolo_mode"] = True
        except Exception:
            pass
        return model_config

    def _ensure_db_session(self) -> None:
        """Create the session DB row on first use; a transient failure leaves it to retry next turn."""
        if getattr(self, "_persist_disabled", False) or self._session_db_created or not self._session_db:
            return
        source = _session_source_for_agent(self.platform)
        try:
            # Persist the profile name explicitly, including "default": profile-keyed consumers treat NULL
            # as unowned.
            try:
                from hermes_cli.profiles import get_active_profile_name
                profile_for_session = get_active_profile_name()
            except Exception:
                # Persist the profile name EXPLICITLY, including "default". NULL used to stand in for the
                # default profile, but the #94724 legacy-owner backfill already stamps literal "default"
                # onto old rows, and profile-keyed consumers (sidebar scope matching,
                # @session:<profile>/<id> deep links) treat NULL as unowned — rows minted NULL after the
                # one-shot backfill vanished from the sidebar (#99222).
                profile_for_session = None
            # Carry the gateway routing identity: when the gateway SessionStore degraded to JSONL (corrupt
            # state.db) this lazy create is the ONLY durable write, and an identity-less row is unrecoverable.
            self._session_db.create_session(
                session_id=self.session_id, source=source, model=self.model,
                model_config=self._session_row_model_config(), system_prompt=self._cached_system_prompt,
                user_id=getattr(self, "_user_id", None), session_key=getattr(self, "_gateway_session_key", None),
                chat_id=getattr(self, "_chat_id", None), chat_type=getattr(self, "_chat_type", None),
                thread_id=getattr(self, "_thread_id", None),
                display_name=getattr(self, "_chat_name", None) or getattr(self, "_user_name", None),
                origin_json=_gateway_origin_json(self), parent_session_id=self._parent_session_id,
                cwd=_launch_cwd_for_session(source), profile_name=profile_for_session,
            )
            self._session_db_created = True
        except Exception as e:
            # Transient failure (e.g. SQLite lock): _session_db_created stays False so the next turn retries.
            logger.warning("Session DB creation failed (will retry next turn): %s", e)

    def _transition_context_engine_session(
        self, *, old_session_id: Optional[str] = None, new_session_id: Optional[str] = None,
        previous_messages: Optional[list] = None, carry_over_context: bool = False, reset_engine: bool = True,
        **extra_context,
    ) -> None:
        """Drive the context engine's session transition: on_session_end → on_session_reset → on_session_start
        → carry_over_new_session_context. Each hook is optional (the built-in compressor only resets)."""
        engine = getattr(self, "context_compressor", None)
        if not engine:
            return
        if old_session_id and previous_messages is not None:
            _call_engine_hook(engine, "on_session_end", old_session_id, previous_messages)
        if reset_engine:
            _call_engine_hook(engine, "on_session_reset")

        should_start = bool(old_session_id or previous_messages is not None or carry_over_context or extra_context)
        target_session_id = new_session_id or getattr(self, "session_id", "") or ""
        if should_start and target_session_id and hasattr(engine, "on_session_start"):
            start_context = {
                "old_session_id": old_session_id, "carry_over_context": carry_over_context,
                "platform": _session_source_for_agent(getattr(self, "platform", None)),
                "model": getattr(self, "model", ""), "context_length": getattr(engine, "context_length", None),
                "conversation_id": getattr(self, "_gateway_session_key", None), **extra_context,
            }
            start_context = {k: v for k, v in start_context.items() if v not in (None, "")}
            _call_engine_hook(engine, "on_session_start", target_session_id, **start_context)
        if carry_over_context and old_session_id and target_session_id:
            _call_engine_hook(engine, "carry_over_new_session_context", old_session_id, target_session_id)

    def reset_session_state(self, previous_messages: Optional[list] = None, old_session_id: Optional[str] = None,
                            carry_over_context: bool = False):
        """Reset session-scoped token/cost counters and compressor state for a fresh session.

        With ``previous_messages`` / ``old_session_id`` / ``carry_over_context`` the context engine gets the
        full transition lifecycle instead of a bare reset.
        """
        for counter in (
            "session_total_tokens", "session_input_tokens", "session_output_tokens", "session_prompt_tokens",
            "session_completion_tokens", "session_cache_read_tokens", "session_cache_write_tokens",
            "session_reasoning_tokens", "session_api_calls",
        ):
            setattr(self, counter, 0)
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "unknown"
        # Per-turn timing accumulators. The conversation loop mutates these
        # in-place during a turn; the AIAgent.run_conversation forwarder
        # copies them onto the result dict so the gateway footer always
        # surfaces real values regardless of which return path executed.
        self._turn_api_time = 0.0
        self._turn_tool_time = 0.0
        self.session_cost_source = "none"

        # Session boundary: the usage anchor describes the OLD transcript; fall back to full estimation.
        self._usage_anchor = None
        self._turn_base_usage_anchor = None
        # The workspace snapshot is pinned per session (agent/system_prompt.py::_coding_parts); a
        # /new, /resume or /branch on the same agent must re-snapshot at its own session start.
        self._frozen_workspace_snapshot = None

        # Turn counter (added after reset_session_state was first written — #2635)
        self._user_turn_count = 0
        # Who wrote the current turn. build_turn_context() sets it at the start of every turn.
        self._turn_author = None
        # Copilot x-initiator: True for the first API call of a user turn, False for tool-loop follow-ups.
        self._is_user_initiated_turn = False

        self._transition_context_engine_session(
            old_session_id=old_session_id, new_session_id=getattr(self, "session_id", None),
            previous_messages=previous_messages, carry_over_context=carry_over_context, reset_engine=True,
        )

        # Reset-only switches (/new, /resume, /branch) change session_id before this call; rebind the
        # built-in compressor's session-keyed cooldown state when no full start hook ran.
        engine = getattr(self, "context_compressor", None)
        target_session_id = getattr(self, "session_id", "") or ""
        if (engine is not None and hasattr(engine, "bind_session_state") and target_session_id
                and target_session_id != getattr(engine, "_session_id", "")):
            try:
                engine.bind_session_state(getattr(self, "_session_db", None), target_session_id)
            except Exception as exc:
                logger.debug("context engine bind_session_state during reset: %s", exc)

    @staticmethod
    def _effective_lmstudio_context_length(config_context_length: Optional[int], runtime_context_length: Any) -> Optional[int]:
        """Return a safe context budget from explicit intent and verified runtime."""
        explicit = _positive_int(config_context_length)
        runtime = _positive_int(getattr(runtime_context_length, "context_length", runtime_context_length))
        if bool(getattr(runtime_context_length, "rejected", False)) or (
            bool(getattr(runtime_context_length, "load_attempted", False)) and runtime is None
        ):
            return None
        if runtime is not None and explicit is not None:
            return min(runtime, explicit)
        return runtime if runtime is not None else explicit

    @staticmethod
    def _lmstudio_load_was_unverified(load_result: Any) -> bool:
        """Return true when a management load was rejected or unverifiable."""
        return bool(getattr(load_result, "rejected", False)) or (
            bool(getattr(load_result, "load_attempted", False)) and getattr(load_result, "context_length", None) is None
        )

    def _ensure_lmstudio_runtime_loaded(self, config_context_length: Optional[int] = None) -> Any:
        """Preload LM Studio unless configured to rely on JIT loading."""
        if (self.provider or "").strip().lower() != "lmstudio":
            return None
        if (getattr(self, "lmstudio_load_mode", "explicit") or "explicit").strip().lower() == "jit":
            logger.debug("LM Studio explicit preload skipped: lmstudio_load_mode=jit")
            return None
        from hermes_cli.models_local import ensure_lmstudio_model_loaded

        if config_context_length is None:
            config_context_length = getattr(self, "_config_context_length", None)
        return ensure_lmstudio_model_loaded(
            self.model, self.base_url, getattr(self, "api_key", ""), config_context_length, return_load_result=True,
        )

    switch_model = _forward("agent.agent_runtime_helpers", "switch_model")

    def _disable_codex_reasoning_replay(self, messages: Optional[List[Dict[str, Any]]] = None) -> Dict[str, int]:
        """On HTTP 400 ``invalid_encrypted_content``: disable Responses reasoning replay and pop
        ``codex_reasoning_items`` from every assistant message. Returns ``{"messages", "items"}`` counts."""
        stripped_messages = stripped_items = 0
        for msg in (messages if isinstance(messages, list) else []):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            items = msg.pop("codex_reasoning_items", None)
            if isinstance(items, list) and items:
                stripped_messages += 1
                stripped_items += len(items)
        self._codex_reasoning_replay_enabled = False
        return {"messages": stripped_messages, "items": stripped_items}

    _stream_diag_init = _forward_static("agent.stream_diag", "stream_diag_init")
    _stream_diag_capture_response = _forward("agent.stream_diag", "stream_diag_capture_response")
    _flatten_exception_chain = _forward_static("agent.stream_diag", "flatten_exception_chain")

    def _is_provider_stream_parse_error(self, error: BaseException) -> bool:
        """True for a malformed Anthropic event-stream frame (surfaced by the SDK as a plain ``ValueError``);
        that is wire trouble, not local validation, so it follows the truncated-JSON retry path."""
        return (getattr(self, "api_mode", None) == "anthropic_messages" and isinstance(error, ValueError)
                and not isinstance(error, (UnicodeEncodeError, json.JSONDecodeError))
                and "expected ident at line" in str(error).strip().lower())

    _log_stream_retry = _forward("agent.stream_diag", "log_stream_retry")
    _emit_stream_drop = _forward("agent.stream_diag", "emit_stream_drop")

    def _emit_auxiliary_failure(self, task: str, exc: BaseException) -> None:
        """Surface a compact warning for failed auxiliary work."""
        try:
            detail = self._summarize_api_error(exc)
        except Exception:
            detail = str(exc)
        detail = (detail or exc.__class__.__name__).strip()
        if len(detail) > 220:
            detail = detail[:217].rstrip() + "..."
        self._emit_warning(f"⚠ Auxiliary {task} failed: {detail}")

    def _current_main_runtime(self) -> Dict[str, str]:
        """Return the live main runtime for session-scoped auxiliary routing."""
        return {key: getattr(self, key, "") or "" for key in ("model", "provider", "base_url", "api_key", "api_mode", "auth_mode")}

    _check_compression_model_feasibility = _forward("agent.conversation_compression", "check_compression_model_feasibility")
    _replay_compression_warning = _forward("agent.conversation_compression", "replay_compression_warning")

    def _hostname_for(self, base_url: Optional[str]) -> str:
        """Hostname of ``base_url``, or of the agent's own base URL when None."""
        if base_url is not None:
            return base_url_hostname(base_url)
        return getattr(self, "_base_url_hostname", "") or base_url_hostname(getattr(self, "_base_url_lower", ""))

    def _is_direct_openai_url(self, base_url: str = None) -> bool:
        """Return True when a base URL targets OpenAI's native API."""
        return self._hostname_for(base_url) == "api.openai.com"

    def _is_azure_openai_url(self, base_url: str = None) -> bool:
        """True when a base URL targets Azure OpenAI (standard client, but NO Responses API support)."""
        url = str(base_url).lower() if base_url is not None else (getattr(self, "_base_url_lower", "") or "")
        return base_url_host_matches(url, "openai.azure.com")

    def _is_github_copilot_url(self, base_url: str = None) -> bool:
        """Return True when a base URL targets GitHub Copilot's OpenAI-compatible API."""
        hostname = self._hostname_for(base_url)
        return bool(hostname) and (hostname == "api.githubcopilot.com" or hostname.endswith(".githubcopilot.com"))

    def _resolved_api_call_timeout(self) -> float:
        """Per-call request timeout: per-model ``timeout_seconds`` > provider ``request_timeout_seconds`` >
        ``HERMES_API_TIMEOUT`` > 1800s."""
        cfg = get_provider_request_timeout(self.provider, self.model)
        return cfg if cfg is not None else env_float("HERMES_API_TIMEOUT", 1800.0)

    def _resolved_api_call_stale_timeout_base(self) -> tuple[float, bool]:
        """Base non-stream stale timeout: per-model ``stale_timeout_seconds`` > provider-wide >
        ``HERMES_API_CALL_STALE_TIMEOUT`` > reasoning floor > 90s.

        Returns ``(seconds, uses_implicit_default)``; the implicit flag lets callers auto-disable the detector
        for local endpoints only when the user configured nothing.
        """
        cfg = get_provider_stale_timeout(self.provider, self.model)
        if cfg is not None:
            return cfg, False
        env_timeout = os.getenv("HERMES_API_CALL_STALE_TIMEOUT")
        if env_timeout is not None:
            return float(env_timeout), False
        # Reasoning-model floor (cloud gateways idle-kill mid-think); not "implicit" so the local-endpoint
        # short-circuit does not disable stale detection here.
        from agent.reasoning_timeouts import get_reasoning_stale_timeout_floor
        reasoning_floor = get_reasoning_stale_timeout_floor(self.model)
        if reasoning_floor is not None:
            return reasoning_floor, False
        return 90.0, True

    def _compute_non_stream_stale_timeout(self, api_payload: Any) -> float:
        """Effective non-stream stale timeout for ``api_payload`` (an ``api_kwargs`` dict or legacy ``messages``
        list), scaled by estimated context size and capped by the run budget."""
        stale_base, uses_implicit_default = self._resolved_api_call_stale_timeout_base()
        base_url = getattr(self, "_base_url", None) or self.base_url or ""
        if uses_implicit_default and base_url and is_local_endpoint(base_url):
            return float("inf")

        from agent.chat_completion_helpers import estimate_request_context_tokens
        est_tokens = estimate_request_context_tokens(api_payload)
        timeout = max(stale_base, 240.0) if est_tokens > 100_000 else max(stale_base, 150.0) if est_tokens > 50_000 else stale_base
        # Run-budget cap: an implicit stale timeout is capped at half the remaining budget (>= 60s) so one
        # hung call cannot eat the run. Never raises the timeout; explicit user config still wins.
        run_budget = getattr(self, "run_budget_seconds", None)
        started = getattr(self, "_run_budget_started_at", None)
        if run_budget and started and not self._stale_timeout_is_explicit():
            remaining = float(run_budget) - (time.time() - started)
            timeout = min(timeout, max(60.0, remaining * 0.5))
        return timeout

    def _stale_timeout_is_explicit(self) -> bool:
        """True when the user explicitly configured the stale timeout (config or env var); implicit values
        (reasoning floors, the 90s default) yield to the run-budget cap, explicit ones never do."""
        return (get_provider_stale_timeout(self.provider, self.model) is not None
                or os.getenv("HERMES_API_CALL_STALE_TIMEOUT") is not None)

    def _codex_silent_hang_hint(self, model: Optional[str] = None) -> Optional[str]:
        """Actionable hint when the request matches a known Codex silent-reject shape (currently the ``gpt-5.5``
        family: connection accepted, no events, no error), else None. Makes the stale timeout actionable."""
        if self.api_mode != "codex_responses":
            return None
        from agent.codex_responses_adapter import classify_responses_route

        if not classify_responses_route(self).is_codex_backend:
            return None
        eff_model = (model if model is not None else self.model) or ""
        # Match the gpt-5.5 family at word boundaries (bare, -codex, vendor-prefixed) but not gpt-5.50.
        if not re.search(r"(?:^|[/\-_])gpt-5\.5(?:$|[\-_])", eff_model.lower()):
            return None
        return (
            f"Codex backend appears to be silently rejecting {eff_model!r} "
            "on chatgpt.com/backend-api/codex (no stream events, no error). "
            "This is a known backend-side pattern that has affected ChatGPT "
            "Plus accounts intermittently. "
            "Workaround: try `gpt-5.4` on the same OAuth profile, or `gpt-5.3-codex`, "
            "or switch to a different model/provider in your fallback chain. "
            "Some ChatGPT Codex accounts do not support `gpt-5.4-codex`. "
            "See hermes-agent#21444 for symptom history."
        )

    def _is_openrouter_url(self) -> bool:
        """Return True when the base URL targets OpenRouter."""
        return base_url_host_matches(self._base_url_lower, "openrouter.ai")

    def _is_copilot_url(self) -> bool:
        """Return True when the base URL targets GitHub Copilot or GitHub Models."""
        return any(base_url_host_matches(self._base_url_lower, h) for h in ("api.githubcopilot.com", "models.github.ai"))

    def _is_copilot_provider(self) -> bool:
        """True when the active provider is GitHub Copilot under any alias (``copilot`` / ``github-copilot`` /
        ``github``) or by base URL; a bare equality check would silently skip credential recovery."""
        return (self.provider or "").strip().lower() in {"copilot", "github-copilot", "github"} or self._is_copilot_url()

    def _is_codex_backend(self) -> bool:
        """Return True for the ChatGPT OAuth Codex Responses backend."""
        return (getattr(self, "api_mode", None) == "codex_responses"
                and getattr(self, "_base_url_hostname", "") == "chatgpt.com"
                and "/backend-api/codex" in (getattr(self, "_base_url_lower", "") or ""))

    _anthropic_prompt_cache_policy = _forward("agent.agent_runtime_helpers", "anthropic_prompt_cache_policy")
    _direct_native_anthropic_tool_cache_capability = _forward("agent.agent_runtime_helpers", "_direct_native_anthropic_tool_cache_capability")

    @staticmethod
    def _model_requires_responses_api(model: str) -> bool:
        """True for GPT-5.x, which OpenAI and OpenRouter reject on /v1/chat/completions
        (``unsupported_api_for_model``)."""
        return model.lower().rsplit("/", 1)[-1].startswith("gpt-5")  # strip vendor prefix ("openai/gpt-5.4")

    @staticmethod
    def _provider_model_requires_responses_api(model: str, *, provider: Optional[str] = None) -> bool:
        """Return True when this provider/model pair should use Responses API."""
        from hermes_cli.providers import is_actual_route
        normalized_provider = (provider or "").strip().lower()
        # Nous serves GPT-5.x via chat completions (its /v1/responses returns 404); generic custom endpoints
        # may relay GPT-5 without full Responses semantics — only direct OpenAI/xAI URLs auto-upgrade.
        if normalized_provider in ("nous", "custom") or is_actual_route(provider):
            return False
        if normalized_provider == "copilot":
            try:
                from hermes_cli.models import _should_use_copilot_responses_api
                return _should_use_copilot_responses_api(model)
            except Exception:
                pass  # fall back to the generic GPT-5 rule
        return AIAgent._model_requires_responses_api(model)

    def _max_tokens_param(self, value: int) -> dict:
        """``max_completion_tokens`` for newer OpenAI families (and Azure / Copilot serving them), else
        ``max_tokens``. URL-first, then model-name fallback for third-party endpoints fronting those models."""
        if (self._is_direct_openai_url() or self._is_azure_openai_url() or self._is_github_copilot_url()
                or model_forces_max_completion_tokens(self.model)):
            return {"max_completion_tokens": value}
        return {"max_tokens": value}

    @staticmethod
    def _requested_output_cap_from_api_kwargs(api_kwargs: Any) -> Optional[int]:
        """Extract the outgoing response token cap from a prepared request."""
        if not isinstance(api_kwargs, dict):
            return None
        for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
            try:
                value = int(api_kwargs.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    def _has_content_after_think_block(self, content: str) -> bool:
        """True when text remains after stripping reasoning blocks (reasoning-only output is retried)."""
        return bool(content) and bool(self._strip_think_blocks(content).strip())

    _strip_think_blocks = _forward("agent.agent_runtime_helpers", "strip_think_blocks")

    @staticmethod
    def _has_natural_response_ending(content: str) -> bool:
        """Heuristic: does visible assistant text look intentionally finished?"""
        stripped = (content or "").rstrip()
        if not stripped:
            return False
        last = stripped[-1]
        # Closing punctuation/brackets, a fenced-code close, or an emoji (Misc Symbols, Dingbats, Emoticons, ...).
        return stripped.endswith("```") or last in '.!?:)"\']}。！？：）】」』》^' or ord(last) >= 0x1F300

    def _is_ollama_glm_backend(self) -> bool:
        """Ollama-hosted GLM models misreport finish_reason='stop'. Matches only explicit Ollama signatures
        (port 11434, "ollama" in URL, provider ollama), never arbitrary local proxies; excludes Ollama Cloud
        (``ollama.com`` / ``:cloud``), which reports faithfully — rewriting it would manufacture truncations.

        Crucially it does NOT match arbitrary local/private endpoints (LiteLLM/sglang/vLLM/LM Studio
        proxies, Tailscale boxes), which report finish_reason correctly and were the source of #13971's
        false-positive truncation continuations.
        Two signatures identify it: the ``ollama.com`` host (provider ``ollama-cloud``) and the ``:cloud``
        model suffix (cloud generation proxied through a local 11434 endpoint, #98406). Applying the
        stop→length rewrite to them manufactures false truncations and causes the continuation nudge to
        consume the model's output budget on the next retry, making further false-positives more likely.
        """
        model_lower = (self.model or "").lower()
        provider_lower = (self.provider or "").lower()
        if "glm" not in model_lower and provider_lower != "zai":
            return False
        base = self._base_url_lower
        # Ollama Cloud (hosted service or :cloud proxy) forwards finish_reason faithfully — do not rewrite.
        if "ollama.com" in base or ":cloud" in model_lower:
            return False
        if "ollama" in base or ":11434" in base:
            return True
        return provider_lower == "ollama"

    def _should_treat_stop_as_truncated(self, finish_reason: str, assistant_message, messages: Optional[list] = None) -> bool:
        """Detect conservative stop->length misreports for Ollama-hosted GLM models."""
        if finish_reason != "stop" or self.api_mode != "chat_completions" or not self._is_ollama_glm_backend():
            return False
        if not any(isinstance(msg, dict) and msg.get("role") == "tool" for msg in (messages or [])):
            return False
        if assistant_message is None or getattr(assistant_message, "tool_calls", None):
            return False
        content = getattr(assistant_message, "content", None)
        if not isinstance(content, str):
            return False
        visible_text = self._strip_think_blocks(content).strip()
        if len(visible_text) < 20 or not re.search(r"\s", visible_text):
            return False
        return not self._has_natural_response_ending(visible_text)

    _looks_like_codex_intermediate_ack = _forward("agent.agent_runtime_helpers", "looks_like_codex_intermediate_ack")
    _extract_reasoning = _forward("agent.agent_runtime_helpers", "extract_reasoning")
    _cleanup_task_resources = _forward("agent.chat_completion_helpers", "cleanup_task_resources")

    # Background memory/skill review — prompts live in agent.background_review.
    from agent.background_review import _MEMORY_REVIEW_PROMPT, _SKILL_REVIEW_PROMPT, _COMBINED_REVIEW_PROMPT
    _summarize_background_review_actions = _forward_static("agent.background_review", "summarize_background_review_actions")

    def _spawn_background_review(self, messages_snapshot: List[Dict], review_memory: bool = False,
                                 review_skills: bool = False, focus: Optional[str] = None, explicit: bool = False) -> None:
        """Post-turn review entry point: decide WHEN, then spawn.

        A review whose runtime is the MANAGED LOCAL llama-server is queued for machine idle (``defer: auto``)
        instead of hitting the user's GPU mid-session; everything else spawns immediately. ``explicit``
        (/refine) is never deferred but does not touch the ``focus``-keyed delegate/enabled gates.
        """
        # Gates run at enqueue/spawn time; the idle dispatcher re-checks `enabled` at dispatch time.
        if focus is None and getattr(self, "_delegate_depth", 0) > 0:
            return
        task_cfg = None
        if focus is None:
            from agent.background_review import load_background_review_settings
            enabled, task_cfg = load_background_review_settings()
            if not enabled:
                return

        # Structural clone at the single chokepoint: the fork sanitizes in place, and a shallow copy would
        # alias the live history's nested tool_calls/content.
        # Structural clone at the single chokepoint every review path (automatic, /refine, idle-queue
        # deferral) goes through. See #100795.
        from agent.turn_finalizer import _clone_background_review_messages
        kwargs = dict(messages_snapshot=_clone_background_review_messages(messages_snapshot),
                      review_memory=review_memory, review_skills=review_skills, focus=focus, task_cfg=task_cfg,
                      explicit=explicit)
        if focus is None and not explicit and _review_should_defer(self, task_cfg):
            from agent.review_idle_queue import QUEUE
            QUEUE.enqueue(self, _review_queue_key(self), kwargs)
            return
        self._spawn_background_review_now(**kwargs)

    def _spawn_background_review_now(self, messages_snapshot: List[Dict], review_memory: bool = False,
                                     review_skills: bool = False, focus: Optional[str] = None,
                                     task_cfg: Optional[Dict[str, Any]] = None, _requeue_attempts: int = 0,
                                     explicit: bool = False) -> None:
        """Spawn the background memory/skill review thread.

        ``threading.Thread`` is constructed here so tests patching ``run_agent.threading.Thread`` keep working.
        ``focus`` is /refine steering text; ``task_cfg`` is the pre-loaded config block (None on direct calls).
        ``explicit`` (/refine) forks under the ``refine_review`` write origin, keeping the full
        memory operation set. A deferred review preempted by a live turn is requeued (bounded)
        rather than lost.
        """
        from agent.background_review import (
            finish_background_review_run, prepare_background_review_run, spawn_background_review_thread,
        )
        from tools.thread_context import propagate_context_to_thread

        review_run = prepare_background_review_run(self)
        if review_run is None:
            return
        try:
            target, _prompt = spawn_background_review_thread(
                self, messages_snapshot, review_memory=review_memory, review_skills=review_skills,
                focus=focus, task_cfg=task_cfg, review_run=review_run, explicit=explicit,
            )

            def _target_with_requeue() -> None:
                target()
                self._maybe_requeue_preempted_review(review_run, dict(
                    messages_snapshot=messages_snapshot, review_memory=review_memory, review_skills=review_skills,
                    focus=focus, task_cfg=task_cfg, _requeue_attempts=_requeue_attempts + 1,
                    explicit=explicit))

            # Carry the active profile into the review thread so MEMORY.md / skill review writes land in the
            # right profile.
            threading.Thread(target=propagate_context_to_thread(_target_with_requeue), daemon=True, name="bg-review").start()
        except Exception:
            finish_background_review_run(self, review_run)
            raise

    _REVIEW_REQUEUE_MAX_ATTEMPTS = 3

    def _maybe_requeue_preempted_review(self, review_run, kwargs) -> None:
        """Requeue a deferred-mode review that a live turn cancelled.

        Only for automatic reviews on the managed local runtime; bounded attempts stop a busy box cycling
        forever.
        """
        try:
            # Not cancelled == ran to completion (or was never admitted).
            if not review_run.cancel_requested.is_set() or kwargs.get("focus") is not None:
                return
            if kwargs.get("_requeue_attempts", 0) > self._REVIEW_REQUEUE_MAX_ATTEMPTS:
                logger.info("Preempted background review dropped after %d requeues", self._REVIEW_REQUEUE_MAX_ATTEMPTS)
                return
            if not _review_should_defer(self, kwargs.get("task_cfg")):
                return
            from agent.review_idle_queue import QUEUE
            # kwargs carries the incremented _requeue_attempts through the queue so the cap survives.
            QUEUE.enqueue(self, _review_queue_key(self), dict(kwargs))
        except Exception:  # noqa: BLE001 — requeue is best-effort
            logger.debug("Preempted-review requeue failed", exc_info=True)

    def _build_memory_write_metadata(
        self,
        *,
        write_origin: Optional[str] = None,
        execution_context: Optional[str] = None,
        task_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Forwarder — see ``agent.background_review.build_memory_write_metadata``."""
        from agent.background_review import build_memory_write_metadata
        return build_memory_write_metadata(
            self,
            write_origin=write_origin,
            execution_context=execution_context,
            task_id=task_id,
            tool_call_id=tool_call_id,
        )

    def _apply_persist_user_message_override(self, messages: List[Dict]) -> None:
        """Rewrite the current-turn user message before persistence/return.

        Some call paths need an API-only user-message variant without letting
        that synthetic text leak into persisted transcripts or resumed session
        history. When an override is configured for the active turn, mutate the
        in-memory messages list in place so both persistence and returned
        history stay clean.  A paired timestamp override preserves the platform
        event time as message metadata, rather than embedding it in content.
        """
        idx = getattr(self, "_persist_user_message_idx", None)
        override = getattr(self, "_persist_user_message_override", None)
        timestamp = getattr(self, "_persist_user_message_timestamp", None)
        platform_id = getattr(self, "_persist_user_message_platform_id", None)
        if idx is None or (
            override is None and timestamp is None and platform_id is None
        ):
            return
        if 0 <= idx < len(messages):
            msg = messages[idx]
            if isinstance(msg, dict) and msg.get("role") == "user":
                # Text-only call paths may pass a synthetic API-facing prompt
                # and a cleaner transcript string separately. Before the API
                # call, a plain-text override must not replace native image/audio
                # blocks. A list override, however, is the original clean
                # multimodal payload (for example before a queued /model note)
                # and must replace the API-local list once the turn is final.
                # Preflight compaction can re-anchor this index at a message
                # whose content was MERGED with the compaction summary
                # (merge-summary-into-tail).  That is not an accident:
                # ``reanchor_current_turn_user_idx`` falls back to the last
                # user row precisely BECAUSE the merge rewrote the content and
                # the exact-match lookup misses.  Overwriting it with the clean
                # text would drop the summary from the continuation history the
                # next turn is built from — the same hazard the DB-write twin
                # below already refuses (see the sibling guard in
                # ``_flush_messages_to_session_db_unlocked``).
                if (
                    override is not None
                    and not msg.get(COMPRESSED_SUMMARY_METADATA_KEY)
                    and (
                        not isinstance(msg.get("content"), list)
                        or isinstance(override, list)
                    )
                ):
                    msg["content"] = override
                if timestamp is not None:
                    msg["timestamp"] = timestamp
                # Platform-side message id (e.g. the Discord/Telegram message
                # id) — metadata, load-bearing for restart drain-window
                # recovery dedup: it lets a recovery pass ask
                # ``has_platform_message_id`` whether an interrupted turn
                # already reached the transcript. Stamped here in addition to
                # ``build_turn_context`` so it survives the override path.
                if platform_id is not None:
                    msg["platform_message_id"] = platform_id

    def _persist_session(self, messages: List[Dict], conversation_history: List[Dict] = None):
        """Save session state to both JSON log and SQLite on any exit path.

        Ensures conversations are never lost, even on errors or early returns.

        Trailing empty-response scaffolding is dropped from the live list in
        place (it is ephemeral junk the real transcript should shed). The
        persist user-message *override* is NOT applied here — it is resolved
        inside ``_flush_messages_to_session_db`` and written only to the DB row,
        never mutating the live message list used by the API call (#48677 is
        thus closed for every persist caller, not just this one).
        """
        # Scaffolding removal mutates the live list (desired — ephemeral
        # retry/failure sentinels must not survive into the real transcript).
        # Close and turn-start persistence can run on separate CLI threads; the
        # marker test-and-append below must be one critical section or both can
        # observe the same unmarked dict and write duplicate durable rows.
        from agent.agent_runtime_helpers import note_turn_persisted

        persist_lock = getattr(self, "_session_persist_lock", None)

        def _persist_and_drain() -> None:
            self._drop_trailing_empty_response_scaffolding(messages)
            self._session_messages = messages
            self._save_session_log(messages)
            self._flush_messages_to_session_db(messages, conversation_history)
            # Drain async token-accounting deltas at every persist point (turn
            # finalize + error exits) so a crash after this line loses at most
            # the in-flight API call's delta. Cheap no-op when nothing queued.
            if self._session_db is not None:
                self._session_db.flush_token_counts()
            note_turn_persisted(self)

        if persist_lock is None:
            _persist_and_drain()
            return

        with persist_lock:
            _persist_and_drain()

    def _drop_trailing_empty_response_scaffolding(self, messages: List[Dict]) -> None:
        """Remove private empty-response retry/failure scaffolding from transcript tails.

        Also rewinds past any trailing tool-result / assistant(tool_calls) pair
        that the failed iteration left hanging. Without this, the tail ends at
        a raw ``tool`` message and the next user turn lands as
        ``...tool, user, user`` — a protocol-invalid sequence that most
        providers silently reject (returns empty content), causing the
        empty-retry loop to fire forever. (issue number to be backfilled once filed)
        """
        # Pass 1: strip the flagged scaffolding messages themselves.
        dropped_scaffolding = False
        while (
            messages
            and isinstance(messages[-1], dict)
            and (
                messages[-1].get("_empty_recovery_synthetic")
                or messages[-1].get("_empty_terminal_sentinel")
            )
        ):
            messages.pop()
            dropped_scaffolding = True

        # Pass 2: if we stripped scaffolding, rewind through any trailing
        # tool-result messages plus the assistant(tool_calls) message that
        # produced them. This preserves role alternation so the next user
        # message follows a user or assistant message, not an orphan tool
        # result. Only runs when scaffolding was actually present — normal
        # conversation tails (real tool loops mid-progress) are untouched.
        if not dropped_scaffolding:
            return

        # Drop any trailing tool-result messages
        while (
            messages
            and isinstance(messages[-1], dict)
            and messages[-1].get("role") == "tool"
        ):
            messages.pop()

        # Drop the assistant message that issued the tool calls, if the tail
        # now ends in an assistant-with-tool_calls (the pair that owned the
        # just-popped tool results). Without this, the tail is
        # ``assistant(tool_calls=...)`` with no tool answers, which some
        # providers also reject.
        if (
            messages
            and isinstance(messages[-1], dict)
            and messages[-1].get("role") == "assistant"
            and messages[-1].get("tool_calls")
        ):
            messages.pop()

    def _repair_message_sequence(self, messages: List[Dict]) -> int:
        """Forwarder — see ``agent.agent_runtime_helpers.repair_message_sequence``."""
        from agent.agent_runtime_helpers import repair_message_sequence
        return repair_message_sequence(self, messages)

    def _flush_messages_to_session_db(
        self,
        messages: List[Dict],
        conversation_history: Optional[List[Dict]] = None,
    ):
        """Serialize direct and turn-boundary session flushes per agent."""
        persist_lock = getattr(self, "_session_persist_lock", None)
        if persist_lock is None:
            return self._flush_messages_to_session_db_unlocked(messages, conversation_history)
        with persist_lock:
            return self._flush_messages_to_session_db_unlocked(messages, conversation_history)

    def _flush_messages_to_session_db_unlocked(
        self,
        messages: List[Dict],
        conversation_history: Optional[List[Dict]] = None,
        _adoption_budget: int = 1,
    ):
        """Persist any un-flushed messages to the SQLite session store.

        Deduplicates via an intrinsic ``_DB_PERSISTED_MARKER`` stamped on each
        written message dict, so repeated calls (from multiple exit paths) only
        write truly new messages — preventing the duplicate-write bug (#860)
        without relying on positional slices that can drift after
        message-sequence repair, and without a retained ``id(msg)`` set that
        CPython could alias onto a freed-then-reused address (#50372). The
        ``_flushed_db_message_ids`` attribute is now only a one-shot seed
        (translated to markers, then cleared each flush), not a persisted set.

        Note: the marker is stamped on the live/shared conversation dict, which
        correctly makes re-persistence idempotent across turns. No code path
        edits a persisted message's content/role in place expecting a re-write
        (in-place compaction resets the seed and re-diffs by identity).
        """
        # Persistence-isolated agents (e.g. the background skill/memory review
        # fork) must NEVER write into the canonical session store. The fork
        # shares the parent's session_id for prompt-cache warmth, so any write
        # here would land its harness turn ("Review the conversation above and
        # update the skill library…") inside the user's real session history,
        # where the next live turn re-reads it as an instruction and the agent
        # "becomes" the curator. Hard-stop before any DB touch.
        if getattr(self, "_persist_disabled", False):
            return None
        if not self._session_db:
            return None
        # Persist user-message override (#48677 chokepoint): historically this
        # mutated the live `messages` list in place, which — on the early
        # crash-resilience persist that runs BEFORE the API call is built —
        # stripped observed group-chat context off the live user message and
        # silently dropped it. Instead, resolve the override here and apply it
        # ONLY to the value written to the DB (see the write loop below); the
        # live dict is never mutated, so every caller (early persist, mid-loop
        # flush, /resume, /branch) is protected uniformly. Timestamp override is
        # metadata and is likewise applied only to the written row.
        _ov_idx = getattr(self, "_persist_user_message_idx", None)
        _ov_content = getattr(self, "_persist_user_message_override", None)
        _ov_timestamp = getattr(self, "_persist_user_message_timestamp", None)
        try:
            # Retry row creation if the earlier attempt failed transiently.
            if not self._session_db_created:
                self._ensure_db_session()
            # Positional flushing used to slice at
            # max(len(conversation_history), _last_flushed_db_idx). That
            # assumes the live `messages` list is the original history plus a
            # new tail. repair_message_sequence can shrink/merge the history
            # copy before the final flush, making len(conversation_history)
            # larger than len(messages); the slice is then empty and delivered
            # assistant responses never reach state.db (#46053).
            #
            # Track persistence with an intrinsic per-message marker rather than
            # id(msg). `messages` is a shallow copy of `conversation_history`, so
            # history dicts are skipped by identity, and new dicts appended
            # during this turn are written once even if repair compacts the list
            # around them. Unlike an id()-keyed set, a marker bound to the dict
            # cannot be aliased onto a freed-then-reused address, so a real turn
            # can never be silently skipped (see _DB_PERSISTED_MARKER).
            #
            # `self._flushed_db_message_ids` is still honoured as a *one-shot*
            # seed: external callers (gateway shutdown, tests) populate it with
            # {id(m) for m in already_persisted} immediately before the flush,
            # while those objects are alive — so the ids are valid at that
            # instant. We translate the seed into durable markers and then clear
            # the set, so stale ids can never accumulate across turns and alias a
            # future message.
            current_session_id = getattr(self, "session_id", None)
            flushed_session_id = getattr(self, "_flushed_db_message_session_id", None)
            if flushed_session_id != current_session_id or self._last_flushed_db_idx == 0:
                seed_ids = set()
            else:
                seed_ids = getattr(self, "_flushed_db_message_ids", None)
                if not isinstance(seed_ids, set):
                    seed_ids = set()
            self._flushed_db_message_session_id = current_session_id
            history_ids = {
                id(item) for item in (conversation_history or [])
                if isinstance(item, dict)
            }

            # Bounded scan: skip the longest identity-matched prefix of the
            # list snapshot taken at the end of the previous successful flush.
            # Every message in that snapshot was already given its final
            # disposition (written+stamped, stamped as durable history, or
            # skipped as ephemeral scaffolding / non-dict), and no code path
            # pops _DB_PERSISTED_MARKER from a live dict in place (compression
            # strips markers on fresh copies, which breaks identity here and
            # forces a full re-scan). Identity match ⇒ identical skip decision,
            # so starting after the matched prefix is behavior-preserving.
            _scan_start = 0
            _prev_prefix = getattr(self, "_db_flush_scan_prefix", None)
            if isinstance(_prev_prefix, list):
                _limit = min(len(_prev_prefix), len(messages))
                while (
                    _scan_start < _limit
                    and messages[_scan_start] is _prev_prefix[_scan_start]
                    and bool(messages[_scan_start].get(_DB_PERSISTED_MARKER))
                ):
                    _scan_start += 1

            # Collect this flush's new rows and write them in ONE transaction
            # at the end of the scan (see append_messages_batch).
            _batch_rows: List[Dict[str, Any]] = []
            _batch_msgs: List[Dict] = []
            for _msg_idx in range(_scan_start, len(messages)):
                msg = messages[_msg_idx]
                if not isinstance(msg, dict):
                    continue
                # Never write ephemeral recovery scaffolding to the session
                # store. The flush is append-only (it only advances
                # _last_flushed_db_idx via identity tracking), so a synthetic
                # message committed by a mid-turn persist cannot be un-written
                # when the end-of-turn drop removes it from the in-memory list —
                # the resumed transcript would then replay synthetic
                # "(empty)"/nudge/thinking-prefill turns as if they were genuine
                # context. Skip regardless of position: an answered nudge leaves
                # the synthetic pair buried mid-list, not just at the tail.
                if _is_ephemeral_scaffolding(msg):
                    continue
                if msg.get(_DB_PERSISTED_MARKER):
                    continue
                # Already-durable messages: either carried over from the loaded
                # history copy, or seeded by a caller. Stamp them so future
                # flushes skip them without consulting any id() set again.
                if id(msg) in history_ids or id(msg) in seed_ids:
                    msg[_DB_PERSISTED_MARKER] = True
                    continue
                role = msg.get("role", "unknown")
                content = msg.get("content")
                # api_content sidecar: the exact bytes sent to the API when
                # they differ from the clean content (stamped by the turn
                # prologue for prefetch/plugin injections). Written verbatim
                # so replay can reproduce the sent prefix byte-for-byte.
                _row_api_content = msg.get("api_content")
                if not isinstance(_row_api_content, str):
                    _row_api_content = None
                _row_timestamp = msg.get("timestamp")
                # Apply the persist override to THIS row's written values only
                # (never to the live dict). A multimodal override is a complete
                # clean replacement for an API-local noted payload. Preserve the
                # historical text-only guard for a list payload, though: a plain
                # text override must not erase its image/audio transcript summary.
                # The close safety-net may flush a shortened snapshot while
                # turn setup still owns its staged CLI dict. In that shape the
                # normal turn index refers to the full history, not this list;
                # preserve the API-local override by recognizing the same dict.
                pending_cli_message = getattr(self, "_pending_cli_user_message", None)
                is_current_turn_user = (
                    _ov_idx == _msg_idx or msg is pending_cli_message
                )
                if is_current_turn_user and msg.get("role") == "user":
                    # Preflight compaction can re-anchor the override index at
                    # a message whose content was MERGED with the compaction
                    # summary (merge-summary-into-tail). Overwriting that with
                    # the clean gateway text would silently drop the summary
                    # from the durable transcript. The wire is already
                    # consistent — the merge popped the sidecar and the merged
                    # content is what gets sent — so keep it.
                    if (
                        _ov_content is not None
                        and (not isinstance(content, list) or isinstance(_ov_content, list))
                        and not msg.get(COMPRESSED_SUMMARY_METADATA_KEY)
                    ):
                        # The live content is what the API call sends; the
                        # override is the cleaned transcript value. If they
                        # differ and no injection already stamped the sidecar,
                        # keep the sent bytes in api_content so replay matches
                        # the wire (#48677 divergence, closed for the cache
                        # prefix too).
                        if (
                            _row_api_content is None
                            and isinstance(content, str)
                            and content != _ov_content
                        ):
                            _row_api_content = content
                        content = _ov_content
                    if _ov_timestamp is not None:
                        _row_timestamp = _ov_timestamp
                # Store the sidecar only when it actually differs.
                if _row_api_content == content:
                    _row_api_content = None
                # Load-time sanitize divergence: get_messages_as_conversation
                # replays user/assistant rows through
                # ``sanitize_context(content).strip()``, so content that
                # sanitize would rewrite (echoed/pasted <memory-context>
                # fences or system notes) replays different bytes after a
                # session reload even though THIS turn sent it verbatim.
                # Capture the sent bytes in the sidecar so a reloaded session
                # replays what was actually on the wire. Compared in wire form
                # (both sides .strip()-ed — the api_messages build strips
                # every outgoing content string) so plain surrounding
                # whitespace doesn't grow redundant sidecars.
                if (
                    _row_api_content is None
                    and role in ("user", "assistant")
                    and isinstance(content, str)
                    and content
                    and sanitize_context(content).strip() != content.strip()
                ):
                    _row_api_content = content
                # Persist multimodal tool results as their text summary only —
                # base64 images would bloat the session DB and aren't useful
                # for cross-session replay.
                if _is_multimodal_tool_result(content):
                    content = _multimodal_text_summary(content)
                elif isinstance(content, list):
                    # List of OpenAI-style content parts: strip images, keep text.
                    _txt = []
                    for p in content:
                        if isinstance(p, dict) and p.get("type") == "text":
                            _txt.append(str(p.get("text", "")))
                        elif isinstance(p, dict) and p.get("type") in {"image", "image_url", "input_image"}:
                            _txt.append("[screenshot]")
                    content = "\n".join(_txt) if _txt else None
                tool_calls_data = None
                if hasattr(msg, "tool_calls") and isinstance(msg.tool_calls, list) and msg.tool_calls:
                    tool_calls_data = [
                        {"name": tc.function.name, "arguments": tc.function.arguments}
                        for tc in msg.tool_calls
                    ]
                elif isinstance(msg.get("tool_calls"), list):
                    tool_calls_data = msg["tool_calls"]
                _row = {
                    "role": role,
                    "content": content,
                    "tool_name": msg.get("tool_name"),
                    "tool_calls": tool_calls_data,
                    "tool_call_id": msg.get("tool_call_id"),
                    "finish_reason": msg.get("finish_reason"),
                    # Reasoning/codex fields are role-gated (assistant-only)
                    # inside _insert_message_rows — pass through untouched.
                    "reasoning": msg.get("reasoning"),
                    "reasoning_content": msg.get("reasoning_content"),
                    "reasoning_details": msg.get("reasoning_details"),
                    "codex_reasoning_items": msg.get("codex_reasoning_items"),
                    "codex_message_items": msg.get("codex_message_items"),
                    "_compressed_summary": bool(msg.get(COMPRESSED_SUMMARY_METADATA_KEY)),
                    "timestamp": _row_timestamp,
                    "api_content": _row_api_content,
                    # Standalone reference handoffs are always hidden, even
                    # when the summarized transcript contained a user turn —
                    # otherwise they occupy the active user slot in
                    # retry/undo/session dispatch (#80622). Merge-into-tail
                    # carriers keep prior visibility rules so preserved tail
                    # content stays readable.
                    "display_kind": (
                        "hidden"
                        if (
                            msg.get(COMPRESSED_SUMMARY_METADATA_KEY)
                            and user_originated_turn_view(msg) is None
                            and (
                                ContextCompressor.classify_summary_content(
                                    msg.get("content")
                                )
                                == "standalone"
                                or not msg.get(
                                    "_compressed_summary_has_user_turn"
                                )
                            )
                        )
                        else msg.get("display_kind")
                    ),
                    "display_metadata": msg.get("display_metadata"),
                    # Platform-side message id (e.g. the Discord/Telegram
                    # message id). _insert_message_rows reads it off the row
                    # dict; load-bearing for restart drain-window recovery
                    # dedup via has_platform_message_id.
                    "platform_message_id": msg.get("platform_message_id"),
                }
                if isinstance(msg.get("_row_id"), int):
                    _row["_row_id"] = msg["_row_id"]
                _batch_rows.append(_row)
                _batch_msgs.append(msg)
            # One transaction for the whole turn's new rows (typically 3-8
            # messages): one BEGIN IMMEDIATE / commit — and, off WAL, one
            # fsync — instead of one per row. All-or-nothing pairs exactly
            # with the marker stamping below: on failure NO rows landed and
            # NO markers were stamped, so the next flush re-scans and
            # re-writes the whole tail (same recovery contract as before,
            # minus the partial-prefix case that could double-pay counters).
            if _batch_rows:
                self._session_db.append_messages_batch(
                    session_id=self.session_id,
                    messages=_batch_rows,
                    compression_lock_holder=getattr(
                        self, "_active_compression_lock_holder", None
                    ),
                    turn_lease_holder=getattr(
                        self, "_active_session_turn_lease_holder", None
                    ),
                    turn_lease_ttl_seconds=getattr(
                        self, "_active_session_turn_lease_ttl_seconds", 300.0
                    )
                    or 300.0,
                )
                from agent.transcript_repair import sync_flushed_message_markers

                sync_flushed_message_markers(_batch_msgs, _batch_rows)
            # The intrinsic markers are now the sole source of truth. Reset the
            # one-shot seed so no id() outlives this flush to alias a message
            # allocated next turn at a recycled address.
            self._flushed_db_message_ids = set()
            self._last_flushed_db_idx = len(messages)
            # Snapshot for the bounded scan above — only on full success, so
            # a partially-processed list can never be treated as settled.
            self._db_flush_scan_prefix = messages[:]
            return True
        except Exception as e:
            # Force a full re-scan on the next flush: an exception mid-loop
            # leaves messages with mixed dispositions.
            self._db_flush_scan_prefix = None
            # This is the one place the underlying SQLite error is visible
            # before it is swallowed into a bare ``False`` — classify it here
            # so the turn-end explanation can distinguish lock contention
            # ("storage was busy, send it again") from disk-full/read-only.
            from hermes_state import (
                CompressionSessionClosedError,
                StateDbCorruptError,
                StateDbReplacedError,
                classify_persistence_error,
                divert_session_transcript_jsonl,
            )

            self._last_persistence_error_cause = classify_persistence_error(e)
            if isinstance(e, (StateDbReplacedError, StateDbCorruptError)):
                # Replaced generation or quarantined (structurally corrupt)
                # handle: SQLite will not take this batch again, so keep it
                # on disk instead of only in RAM.
                try:
                    divert_session_transcript_jsonl(
                        getattr(self, "session_id", "") or "",
                        _batch_rows,
                    )
                except Exception:
                    logger.warning(
                        "JSONL divert failed after state.db %s for %s",
                        self._last_persistence_error_cause,
                        getattr(self, "session_id", None),
                        exc_info=True,
                    )
            if isinstance(e, CompressionSessionClosedError):
                # Compression race: another path rotated this session while
                # this turn was still writing against it. The store resolves
                # the continuation chain transitively via the canonical API
                # ``get_compression_tip`` (bounded walk, excludes branch/
                # delegate/tool children, prefers live children over stale
                # closed siblings such as ``ws_orphan_reap``). Adopt the tip
                # ONLY when it is a different row AND still live, and retry
                # the flush exactly once (adoption budget) — a second
                # closed-parent write must fail closed, never loop. The tip
                # walk returns the input id when no continuation exists, so
                # ``tip == session_id`` means fail closed.
                if _adoption_budget > 0:
                    old_id = self.session_id
                    tip = None
                    try:
                        tip = self._session_db.get_compression_tip(old_id)
                    except Exception as tip_exc:
                        logger.warning(
                            "compression tip lookup failed for %s: %s",
                            old_id,
                            tip_exc,
                        )
                    if tip and tip != old_id:
                        tip_row = None
                        try:
                            tip_row = self._session_db.get_session(tip)
                        except Exception:
                            tip_row = None
                        if tip_row is not None and tip_row.get("ended_at") is None:
                            logger.warning(
                                "Adopted live compression tip %s for closed "
                                "session %s; retrying flush once",
                                tip,
                                old_id,
                            )
                            self.session_id = tip
                            self._flushed_db_message_ids = set()
                            self._last_flushed_db_idx = 0
                            self._compression_adoption_failed = False
                            return self._flush_messages_to_session_db_unlocked(
                                messages,
                                conversation_history,
                                _adoption_budget=0,
                            )
                # No live tip (or budget exhausted): fail closed — never guess
                # a target session. The per-turn diagnostic flag lets the
                # turn-completion explanation name compression rotation
                # instead of the historical (misleading) full-disk advice.
                self._compression_adoption_failed = True
                logger.warning("Session DB append_message failed: %s", e)
                return False
            logger.warning("Session DB append_message failed: %s", e)
            return False

    def _get_messages_up_to_last_assistant(self, messages: List[Dict]) -> List[Dict]:
        """
        Get messages up to (but not including) the last assistant turn.
        
        This is used when we need to "roll back" to the last successful point
        in the conversation, typically when the final assistant message is
        incomplete or malformed.
        
        Args:
            messages: Full message list
            
        Returns:
            Messages up to the last complete assistant turn (ending with user/tool message)
        """
        if not messages:
            return []
        
        # Find the index of the last assistant message
        last_assistant_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant":
                last_assistant_idx = i
                break
        
        if last_assistant_idx is None:
            # No assistant message found, return all messages
            return messages.copy()
        
        # Return everything up to (not including) the last assistant message
        return messages[:last_assistant_idx]

    def _format_tools_for_system_message(self) -> str:
        """Forwarder — see ``agent.system_prompt.format_tools_for_system_message``."""
        from agent.system_prompt import format_tools_for_system_message
        return format_tools_for_system_message(self)

    def _convert_to_trajectory_format(self, messages: List[Dict[str, Any]], user_query: str, completed: bool) -> List[Dict[str, Any]]:
        """Forwarder — see ``agent.agent_runtime_helpers.convert_to_trajectory_format``."""
        from agent.agent_runtime_helpers import convert_to_trajectory_format
        return convert_to_trajectory_format(self, messages, user_query, completed)

    def _save_trajectory(self, messages: List[Dict[str, Any]], user_query: str, completed: bool):
        """
        Save conversation trajectory to JSONL file.
        
        Args:
            messages (List[Dict]): Complete message history
            user_query (str): Original user query
            completed (bool): Whether the conversation completed successfully
        """
        if not self.save_trajectories:
            return
        
        trajectory = self._convert_to_trajectory_format(messages, user_query, completed)
        _save_trajectory_to_file(trajectory, self.model, completed)

    @staticmethod
    def _is_entitlement_failure(
        error_context: Optional[Dict[str, Any]],
        status_code: Optional[int],
    ) -> bool:
        """Detect subscription/entitlement 403s that masquerade as auth failures.

        Returned True only when the body text matches a known entitlement
        shape AND the status is 401/403.  Refreshing an OAuth token cannot
        fix an unsubscribed account, so callers should surface the error
        instead of looping the credential pool.

        Current matches:
          * xAI OAuth: "do not have an active Grok subscription" /
            "out of available resources" / "does not have permission" + "grok"

        Disambiguator for xAI (#29344): the same ``code`` text ("The caller
        does not have permission to execute the specified operation") is
        returned for BOTH an unsubscribed account AND a stale OAuth access
        token.  xAI ships an explicit signal in the ``error`` field that
        tells the two apart: a ``[WKE=unauthenticated:...]`` suffix (and/or
        the ``OAuth2 access token could not be validated`` phrasing) means
        the credentials failed validation — that's recoverable by refreshing
        the token, NOT by surfacing an entitlement message.  When either
        signal is present we return False eagerly so the credential-pool
        refresh path runs, letting long-running TUI sessions recover from
        stale tokens without an exit/reopen cycle.

        Extend here for new providers as we discover them (Anthropic's
        Claude Max OAuth entitlement errors look distinct enough today that
        the existing 1M-context-beta branch handles them; revisit if other
        subscription tiers start producing the same loop signature).
        """
        if status_code not in {401, 403, None}:
            return False
        if not isinstance(error_context, dict):
            return False
        # Build a single lowercase haystack covering every field shape the
        # body might land in.  ``_extract_api_error_context`` normalises to
        # ``message``/``reason``, but callers (and the test suite) may also
        # hand us the raw body with ``code``/``error`` keys; cover both so
        # the WKE disambiguator below fires regardless of entry point.
        message = str(error_context.get("message") or "").lower()
        reason = str(error_context.get("reason") or "").lower()
        code = str(error_context.get("code") or "").lower()
        err = str(error_context.get("error") or "").lower()
        haystack = f"{message} {reason} {code} {err}"
        if not haystack.strip():
            return False
        # xAI's authoritative disambiguator for "stale token" vs
        # "unsubscribed account".  Both conditions share the same
        # permission-denied ``code`` text; only one carries this suffix.
        # Bail out before the entitlement keyword checks so a stale OAuth
        # token routes through the credential-refresh path instead of the
        # surface-error-as-entitlement path.  See #29344 for the long-
        # running TUI failure mode this closes.
        if "[wke=unauthenticated:" in haystack:
            return False
        if "oauth2 access token could not be validated" in haystack:
            return False
        if "do not have an active grok subscription" in haystack:
            return True
        if "out of available resources" in haystack and "grok" in haystack:
            return True
        if "does not have permission" in haystack and "grok" in haystack:
            return True
        return False

    @staticmethod
    def _decorate_xai_entitlement_error(detail: str) -> str:
        """Append a neutral hint when xAI's OAuth surface returns the
        permission-denied 403.

        xAI's ``/v1/responses`` endpoint replies to several distinct failure
        modes with the SAME body::

            {"code": "The caller does not have permission to execute the
             specified operation", "error": "You have either run out of
             available resources or do not have an active Grok subscription.
             Manage subscriptions at https://grok.com/?_s=usage or subscribe
             at https://grok.com/supergrok"}

        That body covers several real causes we cannot distinguish without
        more info from xAI.  The most common (and least obvious) one is
        that **X Premium+ does NOT include API access** — only standalone
        SuperGrok subscribers can use Hermes against xai-oauth.  Lots of
        users see Grok in their X app, assume it works here too, and hit
        this 403 with no idea why.  Lead the hint with that.

        Other possible causes:
          * No Grok subscription at all
          * SuperGrok tier doesn't include the requested model (e.g.
            grok-4.3 may need a higher tier)
          * Monthly quota exhausted (the ``?_s=usage`` URL hints at this)

        Surface the raw xAI text verbatim and point at
        https://grok.com/?_s=usage where the user can see WHICH applies.

        Matched once per detail string — won't double-decorate if the
        upstream already concatenated the same text.
        """
        if not detail:
            return detail
        lower = detail.lower()
        is_entitlement = (
            "do not have an active grok subscription" in lower
            or ("out of available resources" in lower and "grok" in lower)
            or ("does not have permission" in lower and "grok" in lower)
        )
        if not is_entitlement:
            return detail
        hint = (
            " — xAI rejected this OAuth account. NOTE: X Premium+ does NOT "
            "include xAI API access — only standalone SuperGrok subscribers "
            "can use this provider. Other possible causes: no Grok "
            "subscription, your tier doesn't include this model, or your "
            "quota is exhausted. Check https://grok.com/?_s=usage to see "
            "which, or run `/model` to switch providers."
        )
        # Idempotency: detect prior decoration by a substring unique to the
        # hint (not present in xAI's own body text).
        if "X Premium+ does NOT include" in detail:
            return detail
        return f"{detail}{hint}"

    @staticmethod
    def _coerce_api_error_detail(value: Any) -> str:
        """Return a display-safe string for structured provider error fields."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("message", "detail", "error", "code", "type"):
                nested = value.get(key)
                if isinstance(nested, str) and nested.strip():
                    return nested
            for key in ("message", "detail", "error", "code", "type"):
                if key in value:
                    nested_detail = AIAgent._coerce_api_error_detail(value[key])
                    if nested_detail:
                        return nested_detail
            try:
                return json.dumps(value, ensure_ascii=False, sort_keys=True)
            except TypeError:
                return str(value)
        if isinstance(value, (list, tuple)):
            parts = [
                AIAgent._coerce_api_error_detail(item)
                for item in value
            ]
            return "; ".join(part for part in parts if part)
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _summarize_api_error(error: Exception) -> str:
        """Extract a human-readable one-liner from an API error.

        Handles Cloudflare HTML error pages (502, 503, etc.) by pulling the
        <title> tag instead of dumping raw HTML. Network/DNS failures are
        translated into an offline hint, including when an SDK wraps the
        original OS error. Falls back to a truncated str(error) otherwise.
        """
        raw = str(error)

        # Linux, macOS, and Windows use different low-level messages when DNS
        # cannot resolve the provider while the device is offline. SDKs often
        # wrap that OSError in a generic "Connection error", so inspect the
        # exception chain before showing the top-level message to the user.
        network_resolution_markers = (
            "temporary failure in name resolution",
            "name or service not known",
            "nodename nor servname provided, or not known",
            "getaddrinfo failed",
            "no address associated with hostname",
            "network is unreachable",
        )
        current: Optional[BaseException] = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if any(
                marker in str(current).lower()
                for marker in network_resolution_markers
            ):
                return (
                    "Hermes can't reach the model provider. You may be offline. "
                    "Check your internet connection and try again."
                )
            current = current.__cause__ or current.__context__

        if (
            isinstance(error, ValueError)
            and "expected ident at line" in raw.lower()
        ):
            return f"Malformed provider streaming response: {raw[:300]}"

        # Cloudflare / proxy HTML pages: grab the <title> for a clean summary
        if "<!DOCTYPE" in raw or "<html" in raw:
            m = re.search(r"<title[^>]*>([^<]+)</title>", raw, re.IGNORECASE)
            title = m.group(1).strip() if m else "HTML error page (title not found)"
            # Also grab Cloudflare Ray ID if present
            ray = re.search(r"Cloudflare Ray ID:\s*<strong[^>]*>([^<]+)</strong>", raw)
            ray_id = ray.group(1).strip() if ray else None
            status_code = getattr(error, "status_code", None)
            parts = []
            if status_code:
                parts.append(f"HTTP {status_code}")
            parts.append(title)
            if ray_id:
                parts.append(f"Ray {ray_id}")
            return " — ".join(parts)

        # GeminiAPIError (agent/gemini_native_adapter.py) already composes a
        # clean one-liner and may have appended actionable guidance (free-tier
        # 429, legacy Standard-key 401). Prefer its message over re-extracting
        # the raw response body below, which would strip that guidance.
        if type(error).__name__ == "GeminiAPIError":
            return redact_sensitive_text(raw[:1000])

        # JSON body errors from OpenAI/Anthropic SDKs
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            msg = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else body.get("message")
            if msg:
                status_code = getattr(error, "status_code", None)
                prefix = f"HTTP {status_code}: " if status_code else ""
                msg = AIAgent._coerce_api_error_detail(msg)
                return AIAgent._decorate_xai_entitlement_error(f"{prefix}{msg[:300]}")

        # SDK may leave body empty while httpx still has the payload (#36109).
        # Redact before returning: the raw provider/proxy error body is
        # attacker-influenced and may echo Authorization / x-api-key / request
        # JSON, which would otherwise leak into final_response + logs (this path
        # widens exposure vs the old empty-body "HTTP 400" string).
        response = getattr(error, "response", None)
        if response is not None:
            try:
                snippet = (getattr(response, "text", None) or "").strip()
            except Exception:
                snippet = ""
            if snippet:
                status_code = getattr(error, "status_code", None)
                prefix = f"HTTP {status_code}: " if status_code else ""
                try:
                    payload = json.loads(snippet)
                except (json.JSONDecodeError, TypeError):
                    payload = None
                if isinstance(payload, dict):
                    err = payload.get("error")
                    if isinstance(err, dict) and err.get("message"):
                        return redact_sensitive_text(f"{prefix}{str(err['message'])[:300]}")
                    if payload.get("message"):
                        return redact_sensitive_text(f"{prefix}{str(payload['message'])[:300]}")
                return redact_sensitive_text(f"{prefix}{snippet[:300]}")

        # Fallback: truncate the raw string but give more room than 200 chars
        status_code = getattr(error, "status_code", None)
        prefix = f"HTTP {status_code}: " if status_code else ""
        return AIAgent._decorate_xai_entitlement_error(f"{prefix}{raw[:500]}")

    def _mask_api_key_for_logs(self, key: Any) -> Optional[str]:
        # Azure Foundry Entra ID bearer providers are callables — never
        # invoke them in log paths; identify the auth surface instead.
        if callable(key) and not isinstance(key, str):
            return "<entra-id-bearer>"
        if not key:
            return None
        if len(key) <= 12:
            return "***"
        return f"{key[:8]}...{key[-4:]}"

    def _clean_error_message(self, error_msg: str) -> str:
        """
        Clean up error messages for user display, removing HTML content and truncating.
        
        Args:
            error_msg: Raw error message from API or exception
            
        Returns:
            Clean, user-friendly error message
        """
        if not error_msg:
            return "Unknown error"
            
        # Remove HTML content (common with CloudFlare and gateway error pages)
        if error_msg.strip().startswith('<!DOCTYPE html') or '<html' in error_msg:
            return "Service temporarily unavailable (HTML error page returned)"
            
        # Remove newlines and excessive whitespace
        cleaned = ' '.join(error_msg.split())
        
        # Truncate if too long
        if len(cleaned) > 150:
            cleaned = cleaned[:150] + "..."
            
        return cleaned

    @staticmethod
    def _extract_api_error_context(error: Exception) -> Dict[str, Any]:
        """Forwarder — see ``agent.agent_runtime_helpers.extract_api_error_context``."""
        from agent.agent_runtime_helpers import extract_api_error_context
        return extract_api_error_context(error)

    def _usage_summary_for_api_request_hook(self, response: Any) -> Optional[Dict[str, Any]]:
        """Token buckets for ``post_api_request`` plugins (no raw ``response`` object)."""
        if response is None:
            return None
        raw_usage = getattr(response, "usage", None)
        if not raw_usage:
            return None
        from dataclasses import asdict

        cu = normalize_usage(raw_usage, provider=self.provider, api_mode=self.api_mode)
        summary = asdict(cu)
        summary.pop("raw_usage", None)
        summary["prompt_tokens"] = cu.prompt_tokens
        summary["total_tokens"] = cu.total_tokens
        return summary

    @staticmethod
    def _hook_payload_max_chars() -> int:
        raw = os.getenv("HERMES_PLUGIN_PAYLOAD_MAX_CHARS", "50000")
        try:
            return max(1000, int(raw))
        except (TypeError, ValueError):
            return 50000

    @staticmethod
    def _is_sensitive_hook_key(key: Any) -> bool:
        if not isinstance(key, str):
            return False
        lowered = key.lower().replace("-", "_")
        exact = {
            "api_key",
            "authorization",
            "proxy_authorization",
            "cookie",
            "set_cookie",
        }
        return lowered in exact or lowered.endswith("_api_key")

    @classmethod
    def _hook_jsonable(
        cls,
        value: Any,
        *,
        depth: int = 0,
        max_depth: int = 8,
        max_string: int = 8000,
        max_sequence: int = 200,
    ) -> Any:
        if depth > max_depth:
            return f"<{type(value).__name__} depth limit>"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if len(value) > max_string:
                return value[:max_string] + f"...[truncated {len(value) - max_string} chars]"
            return value
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"
        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for idx, (key, item) in enumerate(value.items()):
                if idx >= max_sequence:
                    out["_truncated_items"] = len(value) - max_sequence
                    break
                str_key = str(key)
                if cls._is_sensitive_hook_key(str_key):
                    out[str_key] = "<redacted>"
                else:
                    out[str_key] = cls._hook_jsonable(
                        item,
                        depth=depth + 1,
                        max_depth=max_depth,
                        max_string=max_string,
                        max_sequence=max_sequence,
                    )
            return out
        if isinstance(value, (list, tuple, set)):
            seq = list(value)
            out = [
                cls._hook_jsonable(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_string=max_string,
                    max_sequence=max_sequence,
                )
                for item in seq[:max_sequence]
            ]
            if len(seq) > max_sequence:
                out.append({"_truncated_items": len(seq) - max_sequence})
            return out
        try:
            if hasattr(value, "model_dump"):
                try:
                    # warnings=False: pydantic's serializer UserWarnings on
                    # generic-union SDK models (Anthropic ParsedMessage etc.)
                    # would otherwise leak to the terminal mid-response.
                    dumped = value.model_dump(mode="json", warnings=False)
                except TypeError:
                    try:
                        dumped = value.model_dump(mode="json")
                    except TypeError:
                        dumped = value.model_dump()
                return cls._hook_jsonable(
                    dumped,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_string=max_string,
                    max_sequence=max_sequence,
                )
        except Exception:
            pass
        try:
            from dataclasses import asdict, is_dataclass
            if is_dataclass(value):
                return cls._hook_jsonable(
                    asdict(value),
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_string=max_string,
                    max_sequence=max_sequence,
                )
        except Exception:
            pass
        if isinstance(value, SimpleNamespace):
            return cls._hook_jsonable(
                vars(value),
                depth=depth + 1,
                max_depth=max_depth,
                max_string=max_string,
                max_sequence=max_sequence,
            )
        if hasattr(value, "__dict__"):
            try:
                public_attrs = {
                    k: v
                    for k, v in vars(value).items()
                    if not str(k).startswith("_")
                }
                return cls._hook_jsonable(
                    public_attrs,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_string=max_string,
                    max_sequence=max_sequence,
                )
            except Exception:
                pass
        return str(value)[:max_string]

    @classmethod
    def _sanitize_hook_payload(cls, value: Any) -> Any:
        payload = cls._hook_jsonable(value)
        limit = cls._hook_payload_max_chars()
        try:
            encoded = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            return str(payload)[:limit]
        if len(encoded) <= limit:
            return payload
        payload = cls._hook_jsonable(value, max_string=1000, max_sequence=50)
        try:
            encoded = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            return str(payload)[:limit]
        if len(encoded) <= limit:
            return payload
        return {
            "_truncated": True,
            "original_type": type(value).__name__,
            "preview": encoded[:limit],
        }

    def _api_request_payload_for_hook(self, api_kwargs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        body = {
            key: value
            for key, value in (api_kwargs or {}).items()
            if key not in {"timeout", "http_client"}
        }
        return self._sanitize_hook_payload(
            {
                "method": "POST",
                "body": body,
            }
        )

    def _api_response_payload_for_hook(
        self,
        response: Any,
        assistant_message: Any,
        *,
        finish_reason: Optional[str],
    ) -> Dict[str, Any]:
        # ``tool_calls`` is the raw list of provider SDK objects (e.g.
        # OpenAI ``ChatCompletionMessageToolCall``).  We deliberately hand
        # the raw objects to ``_sanitize_hook_payload`` and rely on
        # ``_hook_jsonable`` to normalise them via ``model_dump`` /
        # ``__dict__`` / dataclass introspection — a future refactor of
        # the sanitiser MUST preserve that capability or hook subscribers
        # will receive opaque ``str(obj)`` blobs here.
        tool_calls = getattr(assistant_message, "tool_calls", None) or []
        return self._sanitize_hook_payload(
            {
                "model": getattr(response, "model", None),
                "finish_reason": finish_reason,
                "assistant_message": {
                    "role": getattr(assistant_message, "role", "assistant"),
                    "content": getattr(assistant_message, "content", None),
                    "tool_calls": tool_calls,
                },
                "usage": self._usage_summary_for_api_request_hook(response),
            }
        )

    def _invoke_api_request_error_hook(
        self,
        *,
        task_id: str,
        turn_id: str,
        api_request_id: str,
        api_call_count: int,
        api_start_time: float,
        api_kwargs: Optional[Dict[str, Any]],
        error_type: str,
        error_message: str,
        status_code: Optional[int] = None,
        retry_count: Optional[int] = None,
        max_retries: Optional[int] = None,
        retryable: Optional[bool] = None,
        reason: Optional[str] = None,
    ) -> None:
        # Lazy module import (not from-import) so tests can replace lifecycle
        # dispatch at this call site. After first call the import is a
        # ``sys.modules`` dict lookup, so retries don't repay any real cost.
        try:
            from hermes_cli import lifecycle as _lifecycle

            if not _lifecycle.has_hook("api_request_error"):
                return
            ended_at = time.time()
            _lifecycle.invoke_hook(
                "api_request_error",
                task_id=task_id,
                turn_id=turn_id,
                api_request_id=api_request_id,
                session_id=self.session_id or "",
                platform=self.platform or "",
                model=self.model,
                provider=self.provider,
                base_url=self.base_url,
                api_mode=self.api_mode,
                api_call_count=api_call_count,
                api_duration=ended_at - api_start_time,
                started_at=api_start_time,
                ended_at=ended_at,
                status_code=status_code,
                retry_count=retry_count,
                max_retries=max_retries,
                retryable=retryable,
                reason=reason,
                error={
                    "type": error_type,
                    "message": error_message,
                },
                request=self._api_request_payload_for_hook(api_kwargs),
            )
        except Exception:
            pass

    def _dump_api_request_debug(
        self,
        api_kwargs: Dict[str, Any],
        *,
        reason: str,
        error: Optional[Exception] = None,
    ) -> Optional[Path]:
        """Forwarder — see ``agent.agent_runtime_helpers.dump_api_request_debug``."""
        from agent.agent_runtime_helpers import dump_api_request_debug
        return dump_api_request_debug(self, api_kwargs, reason=reason, error=error)

    @staticmethod
    def _clean_session_content(content: str) -> str:
        """Convert REASONING_SCRATCHPAD to think tags and clean up whitespace."""
        if not content:
            return content
        content = convert_scratchpad_to_think(content)
        content = re.sub(r'\n+(<think>)', r'\n\1', content)
        content = re.sub(r'(</think>)\n+', r'\1\n', content)
        return content.strip()

    @staticmethod
    def _redact_message_content(content):
        """Apply secret redaction to message content (str or list-of-parts).

        Handles both plain-string content and the OpenAI/Anthropic multimodal
        shape where ``content`` is a list of ``{"type": "text", "text": ...}``
        / ``{"type": "image_url", ...}`` / ``{"type": "input_text", "content": ...}``
        parts. Image / binary parts are left untouched; only text fields are
        passed through ``redact_sensitive_text``.

        Respects ``HERMES_REDACT_SECRETS`` via ``redact_sensitive_text`` —
        when disabled the helper is effectively a no-op.
        """
        if content is None:
            return content
        if isinstance(content, str):
            return redact_sensitive_text(content)
        if isinstance(content, list):
            redacted = []
            for part in content:
                if isinstance(part, dict):
                    part = dict(part)
                    if isinstance(part.get("text"), str):
                        part["text"] = redact_sensitive_text(part["text"])
                    if isinstance(part.get("content"), str):
                        part["content"] = redact_sensitive_text(part["content"])
                redacted.append(part)
            return redacted
        return content

    def _save_session_log(self, messages: List[Dict[str, Any]] = None):
        """Optional per-session JSON snapshot writer.

        Gated by ``sessions.write_json_snapshots`` (default False).  state.db
        is the canonical message store; this writer exists only for users
        whose external tooling consumes ``~/.hermes/sessions/session_{sid}.json``
        directly.  When the flag is off this is a fast no-op.

        When enabled, rewrites the snapshot after every persistence point with
        the full message list (assistant content normalized via
        ``_clean_session_content`` to convert REASONING_SCRATCHPAD to think
        tags).  The truncation guard ("don't overwrite a larger log with
        fewer messages") is preserved so resume + branch don't clobber a
        fuller existing snapshot.
        """
        if not getattr(self, "_session_json_enabled", False):
            return
        messages = messages or self._session_messages
        if not messages:
            return

        # Re-derive the target path each call so /branch and /compress
        # session-id changes land in the right file without any re-point
        # bookkeeping at the call sites.  Sanitize the session ID into a
        # single traversal-free path segment — session IDs can come from
        # untrusted input (X-Hermes-Session-Id header) and must not escape
        # the sessions directory.
        try:
            safe_sid = _safe_session_filename_component(self.session_id)
            log_file = self.logs_dir / f"session_{safe_sid}.json"
        except Exception:
            return

        try:
            cleaned = []
            for msg in messages:
                # Mirror the SQLite flush: ephemeral recovery scaffolding is
                # internal retry state, never durable transcript content.
                if _is_ephemeral_scaffolding(msg):
                    continue
                if msg.get("role") == "assistant" and msg.get("content"):
                    msg = dict(msg)
                    msg["content"] = self._clean_session_content(msg["content"])
                # Defence-in-depth: redact credentials from every message
                # content before persistence. Catches PATs / API keys / Bearer
                # tokens that may have leaked into assistant responses, tool
                # output, or user paste. Respects HERMES_REDACT_SECRETS via
                # redact_sensitive_text — no-op when disabled. (#19798, #19845)
                if "content" in msg:
                    msg = dict(msg)
                    msg["content"] = self._redact_message_content(msg.get("content"))
                cleaned.append(msg)

            # Guard: never overwrite a larger session log with fewer messages.
            # Protects against data loss when a resumed agent starts with
            # partial history and would otherwise clobber the full JSON log.
            if log_file.exists():
                try:
                    existing = json.loads(log_file.read_text(encoding="utf-8"))
                    existing_count = existing.get("message_count", len(existing.get("messages", [])))
                    if existing_count > len(cleaned):
                        logging.debug(
                            "Skipping session log overwrite: existing has %d messages, current has %d",
                            existing_count, len(cleaned),
                        )
                        return
                except Exception:
                    pass  # corrupted existing file — allow the overwrite

            entry = {
                "session_id": self.session_id,
                "model": self.model,
                "base_url": self.base_url,
                "platform": self.platform,
                "session_start": self.session_start.isoformat(),
                "last_updated": datetime.now().isoformat(),
                "system_prompt": redact_sensitive_text(self._cached_system_prompt or ""),
                "tools": self.tools or [],
                "message_count": len(cleaned),
                "messages": cleaned,
            }

            atomic_json_write(
                log_file,
                entry,
                indent=2,
                default=str,
            )

        except Exception as e:
            if self.verbose_logging:
                logging.warning(f"Failed to save session log: {e}")


    def interrupt(
        self,
        message: Optional[str] = None,
        *,
        hard_cancel: bool = False,
        tool_reason: Optional[str] = None,
        require_generation: Optional[int] = None,
    ) -> bool:
        """
        Request the agent to interrupt its current tool-calling loop.
        
        Call this from another thread (e.g., input handler, message receiver)
        to gracefully stop the agent and process a new message.
        
        Also signals long-running tool executions (e.g. terminal commands)
        to terminate early, so the agent can respond immediately.
        
        Args:
            message: Optional new message that triggered the interrupt.
                     If provided, the agent will include this in its response context.
            hard_cancel: Mark this as an explicit stop rather than a redirect or
                         incoming-message interrupt. Compression may honor this
                         atomic signal even while ordinary interrupts are
                         masked. With a generation claim in play, the
                         destructive compression-fence cancellation is
                         deferred until after the claim survives, so a
                         declined abort never cancels a legitimate pending
                         compression.
            tool_reason: Trusted fixed category safe to expose in tool output.
                         Arbitrary diagnostic or caller text belongs in message.
            require_generation: Optional activity-generation claim (#95663).
                         When set, the interrupt is published only if the
                         turn's activity generation still equals this value
                         at the final mutation edge. The claim is RESERVED
                         under the activity lock — ``_touch_activity``
                         invalidates the reservation the instant real
                         progress lands — survives every blocking boundary in
                         between (including the compression commit fence),
                         and is CONSUMED in ONE lock critical section
                         together with the first observable publication
                         (``_interrupt_requested`` / ``_interrupt_message`` /
                         ``_tool_interrupt_reason`` and the hard-cancel
                         event). If the turn resumed in the window, the call
                         abandons itself without publishing anything (no
                         flag, no hard-cancel event, no tool signal).

        Returns:
            True when the interrupt was published, False when a
            ``require_generation`` claim no longer matched the live activity
            clock and the call was abandoned without publishing.
        
        Example (CLI):
            # In a separate input thread:
            if user_typed_something:
                agent.interrupt(user_input)
        
        Example (Messaging):
            # When new message arrives for active session:
            if session_has_running_agent:
                running_agent.interrupt(new_message.text)
        """
        if require_generation is not None:
            # RESERVE the abort's generation claim under the SAME lock
            # `_touch_activity` stamps the clock with. Real progress
            # invalidates the reservation the instant it lands, and the
            # claim is CONSUMED at the final mutation edge — after every
            # blocking boundary — in ONE critical section with the first
            # observable publication. A resumed turn therefore abandons
            # the abort instead of being hard-cancelled by a stale proof.
            with self._liveness_activity_lock():
                if (
                    getattr(self, "_turn_liveness_activity_generation", 0)
                    != require_generation
                ):
                    return False
                self._turn_liveness_abort_claim = require_generation

        # A hard stop and redirect share one lock so /stop cannot race with an
        # accepted correction and accidentally turn itself into a retry.
        def _wait_for_compression_commit() -> None:
            # Pre-claim half of hard-cancel admission (#99758 P1): wait out
            # a commit that ALREADY crossed its boundary, so the interrupt
            # is published only after the in-flight SessionDB mutation has
            # finished — but mutate NOTHING. Cancelling a pending commit is
            # a destructive, irreversible fence mutation (``begin_commit``
            # refuses a cancelled fence forever), so it must not run while
            # a generation claim can still be vetoed: an abort that declines
            # after the fence was cancelled would have killed the recovered
            # turn's legitimate pending compression. The destructive half
            # runs in _cancel_pending_compression_commit(), only after the
            # claim survived the final mutation edge.
            fence = vars(self).get("_active_compression_commit_fence")
            if fence is None:
                return
            if not getattr(fence, "commit_in_flight", False):
                # No commit crossed its boundary — nothing to wait out,
                # and calling cancel_before_commit here WOULD cancel the
                # pending commit (the production fence's
                # cancel_before_commit sets _cancelled whenever no commit
                # has started). Skip it; the destructive half handles it.
                return
            cancel_before_commit = getattr(
                type(fence), "cancel_before_commit", None
            )
            if callable(cancel_before_commit):
                try:
                    # A commit is in flight (it holds the fence lock
                    # through finish_commit), so this call blocks until
                    # the commit finishes and returns False WITHOUT
                    # setting _cancelled — the started-commit branch of
                    # the production fence never cancels.
                    cancel_before_commit(fence)
                except Exception:
                    logger.debug(
                        "Compression hard-cancel fence wait failed",
                        exc_info=True,
                    )

        def _cancel_pending_compression_commit() -> None:
            # Destructive half of hard-cancel admission (#99758 P1): runs
            # only AFTER the generation claim survived the final mutation
            # edge, so an abort that declines can never leave the active
            # compression fence cancelled. Waiting for an in-flight commit
            # already happened in _wait_for_compression_commit(); if a
            # commit crossed its boundary in between, it can no longer be
            # fence-cancelled (it owns the fence until finish_commit and
            # completes on its own), so only a still-pending commit is
            # cancelled here.
            fence = vars(self).get("_active_compression_commit_fence")
            if fence is None:
                return
            if getattr(fence, "commit_in_flight", False):
                return
            cancel_before_commit = getattr(
                type(fence), "cancel_before_commit", None
            )
            if callable(cancel_before_commit):
                try:
                    # Marks the fence cancelled (or waits out a commit
                    # that started between the wait above and now) without
                    # setting the hard-stop Event, which was already
                    # published at the final claim edge.
                    cancel_before_commit(fence)
                except Exception:
                    logger.debug(
                        "Compression hard-cancel fence admission failed",
                        exc_info=True,
                    )

        def _publish_interrupt_state() -> None:
            self._interrupt_requested = True
            self._interrupt_message = message
            self._tool_interrupt_reason = tool_interrupt_reason
            if hard_cancel:
                _hard_event = getattr(
                    self, "_hard_interrupt_requested", None
                )
                if _hard_event is not None:
                    _hard_event.set()

        def _consume_claim_and_publish_first_state() -> bool:
            # Final mutation edge: when a generation claim is in play,
            # claim consumption and the FIRST observable interrupt
            # publication are ONE activity-lock critical section — the
            # same lock `_touch_activity` stamps the clock with. The
            # generation winner is therefore total: either the claim
            # survives and the interrupt state commits under the lock
            # BEFORE any later activity stamp, or the stamp landed first
            # and the abort declines without publishing anything. (A
            # consume-then-release-then-publish split would let a turn
            # that resumed in the consume→publication window be
            # hard-cancelled by an already-consumed claim.)
            if require_generation is None:
                # No claim to race the activity clock against: publish
                # WITHOUT touching the liveness lock. ``AIAgent``
                # stand-ins used by unrelated suites (e.g. the
                # start-order gate `_Stub`) do not carry the liveness
                # seam, and an unconditional
                # ``_liveness_activity_lock()`` acquisition here
                # regresses them with AttributeError.
                _publish_interrupt_state()
                return True
            with self._liveness_activity_lock():
                if (
                    getattr(self, "_turn_liveness_abort_claim", None)
                    != require_generation
                ):
                    return False
                self._turn_liveness_abort_claim = None
                _publish_interrupt_state()
            return True

        # Keep tool cancellation attribution separate from _interrupt_message:
        # ordinary interrupts may carry the user's full next message, which
        # must not be copied into tool output.
        tool_interrupt_reason = (
            (tool_reason or "explicit stop requested")
            if hard_cancel
            else ("user sent a new message" if message else "user interrupt")
        )

        _redirect_lock = getattr(self, "_pending_redirect_lock", None)
        if _redirect_lock is not None:
            with _redirect_lock:
                # The (potentially blocking) in-flight-commit wait runs
                # BEFORE the atomic claim/publication edge; the redirect
                # lock is still held across it, exactly as before, so /stop
                # cannot race with an accepted correction. The destructive
                # pending-commit cancellation runs AFTER the claim survives
                # (#99758 P1) so a declined abort can never cancel the
                # recovered turn's legitimate compression.
                if hard_cancel:
                    _wait_for_compression_commit()
                if not _consume_claim_and_publish_first_state():
                    return False
                if hard_cancel:
                    _cancel_pending_compression_commit()
                self._pending_redirect = None
        else:
            if hard_cancel:
                _wait_for_compression_commit()
            if not _consume_claim_and_publish_first_state():
                return False
            if hard_cancel:
                _cancel_pending_compression_commit()
            self._pending_redirect = None

        # Codex app-server owns its model/tool loop and watches a private
        # interrupt event rather than Hermes' per-thread flag.
        if getattr(self, "api_mode", None) == "codex_app_server":
            _codex_session = getattr(self, "_codex_session", None)
            _request_interrupt = getattr(_codex_session, "request_interrupt", None)
            if callable(_request_interrupt):
                try:
                    _request_interrupt()
                except Exception:
                    logger.debug(
                        "Failed to interrupt Codex app-server turn",
                        exc_info=True,
                    )

        # A cron turn performs its API request on the conversation thread to
        # avoid the nested interrupt-worker deadlock.  Unlike the normal worker
        # path, its client is registered here so this cross-thread interrupt can
        # still shut down the active sockets promptly.
        _abort_active_request = getattr(self, "_active_request_abort", None)
        if callable(_abort_active_request):
            try:
                _abort_active_request("interrupt_abort")
            except Exception:
                logger.debug("Failed to abort active inline request", exc_info=True)
        # Signal all tools to abort any in-flight operations immediately.
        # Scope the interrupt to this agent's execution thread so other
        # agents running in the same process (gateway) are not affected.
        if self._execution_thread_id is not None:
            _set_interrupt(
                True,
                self._execution_thread_id,
                reason=tool_interrupt_reason,
            )
            self._interrupt_thread_signal_pending = False
        else:
            # The interrupt arrived before run_conversation() finished
            # binding the agent to its execution thread. Defer the tool-level
            # interrupt signal until startup completes instead of targeting
            # the caller thread by mistake.
            self._interrupt_thread_signal_pending = True
        # Fan out to concurrent-tool worker threads.  Those workers run tools
        # on their own tids (ThreadPoolExecutor workers), so `is_interrupted()`
        # inside a tool only sees an interrupt when their specific tid is in
        # the `_interrupted_threads` set.  Without this propagation, an
        # already-running concurrent tool (e.g. a terminal command hung on
        # network I/O) never notices the interrupt and has to run to its own
        # timeout.  See `_run_tool` for the matching entry/exit bookkeeping.
        # `getattr` fallback covers test stubs that build AIAgent via
        # object.__new__ and skip __init__.
        _tracker = getattr(self, "_tool_worker_threads", None)
        _tracker_lock = getattr(self, "_tool_worker_threads_lock", None)
        if _tracker is not None and _tracker_lock is not None:
            with _tracker_lock:
                _worker_tids = list(_tracker)
            for _wtid in _worker_tids:
                try:
                    _set_interrupt(True, _wtid, reason=tool_interrupt_reason)
                except Exception:
                    pass
        # Propagate interrupt to any running child agents (subagent delegation)
        with self._active_children_lock:
            children_copy = list(self._active_children)
        for child in children_copy:
            try:
                if hard_cancel:
                    request_hard_interrupt(
                        child,
                        message,
                        tool_reason=tool_interrupt_reason,
                    )
                else:
                    child.interrupt(message)
            except Exception as e:
                logger.debug("Failed to propagate interrupt to child agent: %s", e)
        if not self.quiet_mode:
            print("\n⚡ Interrupt requested" + (f": '{message[:40]}...'" if message and len(message) > 40 else f": '{message}'" if message else ""))
        return True

    def hard_interrupt(
        self,
        message: Optional[str] = None,
        *,
        tool_reason: Optional[str] = None,
    ) -> None:
        """Request an explicit stop while preserving ``interrupt()`` ABI.

        Frontends can feature-detect this method and fall back to the legacy
        ``interrupt()`` signature for synthetic or third-party agents.
        """
        # Deliberately bypass dynamic dispatch: subclasses written against the
        # legacy interrupt(message=None) ABI may override interrupt without the
        # newer keyword-only hard_cancel argument.
        AIAgent.interrupt(
            self,
            message,
            hard_cancel=True,
            tool_reason=tool_reason,
        )

    def clear_interrupt(self, *, preserve_redirect: bool = False) -> bool:
        """Clear the interrupt request and per-thread tool signal.

        ``preserve_redirect`` is used only by the conversation loop after it
        intentionally cancels a model request to rebuild that same logical
        turn. Public hard-stop paths keep the default and clear everything.
        """
        _redirect_lock = getattr(self, "_pending_redirect_lock", None)
        if _redirect_lock is not None:
            with _redirect_lock:
                if preserve_redirect and not self._pending_redirect:
                    return False
                self._interrupt_requested = False
                self._interrupt_message = None
                self._tool_interrupt_reason = None
                getattr(self, "_hard_interrupt_requested", threading.Event()).clear()
                if not preserve_redirect:
                    self._pending_redirect = None
        else:
            if preserve_redirect and not getattr(self, "_pending_redirect", None):
                return False
            self._interrupt_requested = False
            self._interrupt_message = None
            self._tool_interrupt_reason = None
            getattr(self, "_hard_interrupt_requested", threading.Event()).clear()
            if not preserve_redirect:
                self._pending_redirect = None
        self._interrupt_thread_signal_pending = False
        if self._execution_thread_id is not None:
            _set_interrupt(False, self._execution_thread_id)
        # Also clear any concurrent-tool worker thread bits.  Tracked
        # workers normally clear their own bit on exit, but an explicit
        # clear here guarantees no stale interrupt can survive a turn
        # boundary and fire on a subsequent, unrelated tool call that
        # happens to get scheduled onto the same recycled worker tid.
        # `getattr` fallback covers test stubs that build AIAgent via
        # object.__new__ and skip __init__.
        _tracker = getattr(self, "_tool_worker_threads", None)
        _tracker_lock = getattr(self, "_tool_worker_threads_lock", None)
        if _tracker is not None and _tracker_lock is not None:
            with _tracker_lock:
                _worker_tids = list(_tracker)
            for _wtid in _worker_tids:
                try:
                    _set_interrupt(False, _wtid)
                except Exception:
                    pass
        # A hard interrupt supersedes any pending /steer — the steer was
        # meant for the agent's next tool-call iteration, which will no
        # longer happen. Drop it instead of surprising the user with a
        # late injection on the post-interrupt turn.
        _steer_lock = getattr(self, "_pending_steer_lock", None)
        if _steer_lock is not None:
            with _steer_lock:
                self._pending_steer = None
        return True

    def steer(self, text: str) -> bool:
        """
        Inject a user message into the next tool result without interrupting.

        Unlike interrupt(), this does NOT stop the current tool call. The
        text is stashed and the agent loop appends it to the LAST tool
        result's content once the current tool batch finishes. The model
        sees the steer as part of the tool output on its next iteration.

        Thread-safe: callable from gateway/CLI/TUI threads. Multiple calls
        before the drain point concatenate with newlines.

        Args:
            text: The user text to inject. Empty strings are ignored.

        Returns:
            True if the steer was accepted, False if the text was empty.
        """
        if not text or not text.strip():
            return False
        cleaned = text.strip()
        _lock = getattr(self, "_pending_steer_lock", None)
        if _lock is None:
            # Test stubs that built AIAgent via object.__new__ skip __init__.
            # Fall back to direct attribute set; no concurrent callers expected
            # in those stubs.
            existing = getattr(self, "_pending_steer", None)
            self._pending_steer = (existing + "\n" + cleaned) if existing else cleaned
            return True
        with _lock:
            if self._pending_steer:
                self._pending_steer = self._pending_steer + "\n" + cleaned
            else:
                self._pending_steer = cleaned
        return True

    def redirect(self, text: str) -> bool:
        """Redirect the active turn without converting it into a new task.

        During a normal Hermes model request this cancels only that request;
        the conversation loop retains completed messages/tool results, records
        the displayed partial reasoning as plain assistant context, appends the
        correction as a real user message, and retries. During tool execution
        it degrades to ``steer()`` so the tool can finish at a safe boundary.
        Codex app-server has a native ``turn/steer`` operation and uses it
        directly instead of cancelling.

        Returns ``False`` when there is no live turn or the text is empty, so
        surfaces can fall back to their existing next-turn queue.
        """
        if not text or not text.strip():
            return False
        cleaned = text.strip()

        # Codex owns its internal reasoning/tool loop, so use its first-class
        # active-turn steering protocol rather than interrupting the subprocess.
        if getattr(self, "api_mode", None) == "codex_app_server":
            _codex_session = getattr(self, "_codex_session", None)
            _native_steer = getattr(_codex_session, "request_steer", None)
            if callable(_native_steer):
                _redirect_lock = getattr(self, "_pending_redirect_lock", None)
                if _redirect_lock is not None:
                    with _redirect_lock:
                        if self._interrupt_requested:
                            return False
                elif self._interrupt_requested:
                    return False
                try:
                    return bool(_native_steer(cleaned))
                except Exception:
                    logger.debug("Codex app-server turn/steer failed", exc_info=True)
                    return False

        # Never kill a tool merely to deliver conversational guidance. The
        # existing steer drain puts it on the final tool result before the next
        # model decision, including delegate_agent children.
        if getattr(self, "_executing_tools", False):
            return self.steer(cleaned)

        _model_active = getattr(self, "_model_request_active", None)
        _redirect_lock = getattr(self, "_pending_redirect_lock", None)
        if _redirect_lock is None:
            if _model_active is None or not _model_active.is_set():
                return False
            existing = getattr(self, "_pending_redirect", None)
            if self._interrupt_requested and not existing:
                return False
            self._pending_redirect = (
                f"{existing}\n\n[Additional user correction]\n{cleaned}"
                if existing
                else cleaned
            )
            self._interrupt_requested = True
            self._interrupt_message = None
        else:
            with _redirect_lock:
                if _model_active is None or not _model_active.is_set():
                    # The response completed before we acquired the state lock.
                    # Reject so the surface queues a new turn.
                    return False
                if self._interrupt_requested and not self._pending_redirect:
                    return False
                if self._pending_redirect:
                    self._pending_redirect = (
                        f"{self._pending_redirect}\n\n"
                        f"[Additional user correction]\n{cleaned}"
                    )
                else:
                    self._pending_redirect = cleaned
                self._interrupt_requested = True
                self._interrupt_message = None

        # Interrupt only the model request. Do not fan out to tool workers or
        # child agents as interrupt() does.
        _execution_thread_id = getattr(self, "_execution_thread_id", None)
        if _execution_thread_id is not None:
            _set_interrupt(True, _execution_thread_id)
            self._interrupt_thread_signal_pending = False
        else:
            self._interrupt_thread_signal_pending = True
        _abort_active_request = getattr(self, "_active_request_abort", None)
        if callable(_abort_active_request):
            try:
                _abort_active_request("redirect_abort")
            except Exception:
                logger.debug("Failed to abort request for redirect", exc_info=True)
        return True

    def _has_pending_redirect(self) -> bool:
        """Return whether an active-turn redirect is waiting to be applied."""
        _redirect_lock = getattr(self, "_pending_redirect_lock", None)
        if _redirect_lock is None:
            return bool(getattr(self, "_pending_redirect", None))
        with _redirect_lock:
            return bool(self._pending_redirect)

    def _drain_pending_redirect(self) -> Optional[str]:
        """Return and clear pending active-turn correction text."""
        _redirect_lock = getattr(self, "_pending_redirect_lock", None)
        if _redirect_lock is None:
            text = getattr(self, "_pending_redirect", None)
            self._pending_redirect = None
            return text
        with _redirect_lock:
            text = self._pending_redirect
            self._pending_redirect = None
        return text

    def _drain_pending_steer(self) -> Optional[str]:
        """Return the pending steer text (if any) and clear the slot.

        Safe to call from the agent execution thread after appending tool
        results. Returns None when no steer is pending.
        """
        _lock = getattr(self, "_pending_steer_lock", None)
        if _lock is None:
            text = getattr(self, "_pending_steer", None)
            self._pending_steer = None
            return text
        with _lock:
            text = self._pending_steer
            self._pending_steer = None
        return text

    def register_steer_delivery_listener(self, callback) -> bool:
        """Register a one-shot notification for real steer injection.

        The callback is invoked as ``callback(steer_text)`` exactly when a
        pending steer is injected into the model's context at one of the two
        mid-run drain points (pre-API-call injection and the post-tool-batch
        injection). It is NOT invoked when a drained steer is put back for a
        later attempt, nor for the end-of-turn leftover drain that hands the
        text to the caller as a next-turn user message.

        Listeners are expected to be one-shot (unregister themselves when
        fired); callers that outlive the run must unregister their callback
        so it cannot fire on a later turn reusing this agent object.

        Returns:
            True if the callback was registered, False if it is not callable.
        """
        if not callable(callback):
            return False
        listeners = getattr(self, "_steer_delivery_listeners", None)
        if listeners is None:
            # Test stubs that built AIAgent via object.__new__ skip __init__.
            listeners = []
            try:
                self._steer_delivery_listeners = listeners
            except Exception:
                return False
        _lock = getattr(self, "_steer_delivery_listeners_lock", None)
        if _lock is not None:
            with _lock:
                listeners.append(callback)
        else:
            listeners.append(callback)
        return True

    def unregister_steer_delivery_listener(self, callback) -> None:
        """Remove a previously registered steer-delivery listener (no-op if absent)."""
        listeners = getattr(self, "_steer_delivery_listeners", None)
        if not listeners:
            return
        _lock = getattr(self, "_steer_delivery_listeners_lock", None)
        try:
            if _lock is not None:
                with _lock:
                    listeners.remove(callback)
            else:
                listeners.remove(callback)
        except ValueError:
            pass  # never registered / already unregistered

    def _notify_steer_delivery_listeners(self, steer_text: str) -> None:
        """Notify listeners that ``steer_text`` is now in the model's context.

        Called only from the two real mid-run injection sites.  Thread-safe:
        the listener list is snapshotted under the lock (the post-tool-batch
        site can run on a concurrent-tool worker thread) and each callback is
        wrapped so a broken listener can never break the agent loop.  No-op —
        a single attribute check — when no listeners are registered.
        """
        listeners = getattr(self, "_steer_delivery_listeners", None)
        if not listeners:
            return
        _lock = getattr(self, "_steer_delivery_listeners_lock", None)
        if _lock is not None:
            with _lock:
                snapshot = list(listeners)
        else:
            snapshot = list(listeners)
        for _cb in snapshot:
            try:
                _cb(steer_text)
            except Exception:
                logger.debug("Steer delivery listener failed", exc_info=True)

    def _record_file_mutation_result(
        self,
        tool_name: str,
        args: Dict[str, Any],
        result: Any,
        is_error: bool,
    ) -> None:
        """Record a ``write_file`` / ``patch`` outcome for the turn-end verifier.

        On failure, store ``{path: {error_preview, tool}}`` entries.  On
        success, remove any prior failure entries for the same paths (the
        model recovered within the turn).  Silently no-ops if the per-turn
        state dict hasn't been initialised yet (e.g. a tool dispatched
        outside ``run_conversation``).
        """
        if tool_name not in _FILE_MUTATING_TOOLS:
            return
        state = getattr(self, "_turn_failed_file_mutations", None)
        if state is None:
            return
        targets = _extract_file_mutation_targets(tool_name, args)
        if not targets:
            return
        landed = file_mutation_result_landed(tool_name, result)
        if landed:
            landed_paths = _extract_landed_file_mutation_paths(tool_name, args, result)
            changed = getattr(self, "_turn_file_mutation_paths", None)
            if changed is not None:
                changed.update(landed_paths)
            # Feed the checkpoint agent-write ledger so /rollback's safe mode
            # can tell Hermes-authored content from later user hand-edits.
            mgr = getattr(self, "_checkpoint_mgr", None)
            if mgr is not None and getattr(mgr, "enabled", False):
                for _p in landed_paths:
                    try:
                        mgr.record_agent_write(_p)
                    except Exception:
                        pass
        if is_error and not landed:
            preview = _extract_error_preview(result)
            for path in targets:
                # Keep the FIRST error we saw for a given path unless we
                # later see success.  A repeated failure with a different
                # message shouldn't silently overwrite the original.
                if path not in state:
                    state[path] = {
                        "tool": tool_name,
                        "error_preview": preview,
                    }
        else:
            for path in targets:
                state.pop(path, None)

    def _file_mutation_verifier_enabled(self) -> bool:
        """Check whether the per-turn file-mutation verifier footer is on.

        Config path: ``display.file_mutation_verifier`` (bool, default True).
        ``HERMES_FILE_MUTATION_VERIFIER`` env var overrides config.  Exposed
        as a method so tests can patch a single seam without reaching into
        the private ``_turn_failed_file_mutations`` state dict.

        The config lookup is read once per agent and cached (mirroring
        ``_credits_notices_enabled``) — the footer gate runs at the end of
        every turn, and a config flip applying on the next session is fine.
        The env-var override stays authoritative on every call and is never
        cached, so tests and operators can still flip it at runtime.
        """
        try:
            import os as _os
            env = _os.environ.get("HERMES_FILE_MUTATION_VERIFIER")
            if env is not None:
                return env.strip().lower() not in {"0", "false", "no", "off"}
            cached = getattr(self, "_file_mutation_verifier_enabled_cache", None)
            if cached is not None:
                return cached
            # Read from the persisted config.yaml so gateway and CLI share
            # the same setting.  Import lazily to avoid a startup-time cycle.
            try:
                from hermes_cli.config import load_config as _load_config
                _cfg = _load_config() or {}
            except Exception:
                _cfg = {}
            _display = _cfg.get("display") if isinstance(_cfg, dict) else None
            if isinstance(_display, dict) and "file_mutation_verifier" in _display:
                enabled = bool(_display.get("file_mutation_verifier"))
            else:
                enabled = True  # safe default: verifier on
            self._file_mutation_verifier_enabled_cache = enabled
            return enabled
        except Exception:
            pass
        return True  # safe default: verifier on

    # Bare absolute / home / Windows-drive file paths in a footer line.
    # Anchors mirror the gateway's ``extract_local_files`` bare-path
    # detector so that anything the gateway WOULD auto-attach is wrapped
    # in inline-code backticks here first (the extractor skips paths inside
    # `code` spans).  Defense-in-depth: even if a future error message
    # echoes a credential path (config.yaml, .env, auth.json) into the
    # user-facing footer, it can never be matched as a deliverable bare
    # path and silently uploaded to a messaging channel (#35584).
    _FOOTER_PATH_RE = re.compile(
        r"(?<![/:\w.`])(?:~/|/|[A-Za-z]:[/\\])(?:[\w.\-]+[/\\])*[\w.\-]+\.[\w]+",
    )

    @classmethod
    def _neutralize_footer_paths(cls, text: str) -> str:
        """Wrap bare file paths in backticks so they aren't auto-delivered.

        The gateway's ``extract_local_files`` scans response text for bare
        absolute/home paths ending in a deliverable extension and uploads
        any that exist on disk as native attachments — but it explicitly
        skips paths inside inline-code (`` `...` ``) spans.  Backticking
        every path the footer renders defeats that auto-detection while
        keeping the path fully human-readable.  Paths already wrapped in a
        backtick (the negative lookbehind excludes a preceding `` ` ``) are
        left untouched so we never double-wrap.
        """
        if not text:
            return text
        return cls._FOOTER_PATH_RE.sub(lambda m: f"`{m.group(0)}`", text)

    @classmethod
    def _format_file_mutation_failure_footer(cls, failed: Dict[str, Dict[str, Any]]) -> str:
        """Render the per-turn failed-mutation dict as a user-facing footer.

        Displays up to 10 paths with their first error preview, then a
        count of any additional failures.  Returns an empty string when
        the dict is empty so callers can concatenate unconditionally.

        Every file path that reaches the user-facing text — both the bullet
        path and any path echoed inside the tool's error preview — is
        backtick-wrapped via ``_neutralize_footer_paths`` so the gateway's
        bare-path media extractor can never auto-attach a protected file
        (e.g. ``~/.hermes/config.yaml``) to a messaging channel (#35584).
        """
        if not failed:
            return ""
        lines = [
            "⚠️ File-mutation verifier: "
            f"{len(failed)} file(s) were NOT modified this turn despite any "
            "wording above that may suggest otherwise. Run `git status` or "
            "`read_file` to confirm."
        ]
        shown = 0
        for path, info in failed.items():
            if shown >= 10:
                break
            preview = (info.get("error_preview") or "").strip()
            tool = info.get("tool") or "patch"
            if preview:
                lines.append(f"  • `{path}` — [{tool}] {preview}")
            else:
                lines.append(f"  • `{path}` — [{tool}] failed")
            shown += 1
        remaining = len(failed) - shown
        if remaining > 0:
            lines.append(f"  • … and {remaining} more")
        # Neutralize any path the preview text echoed (the bullet path is
        # already backticked above; the lookbehind keeps it from being
        # double-wrapped).
        return cls._neutralize_footer_paths("\n".join(lines))

    def _turn_completion_explainer_enabled(self) -> bool:
        """Check whether the end-of-turn completion explainer footer is on.

        Config path: ``display.turn_completion_explainer`` (bool, default
        True).  ``HERMES_TURN_COMPLETION_EXPLAINER`` env var overrides
        config.  Exposed as a method so tests can patch a single seam,
        mirroring ``_file_mutation_verifier_enabled``.

        The config lookup is read once per agent and cached (mirroring
        ``_credits_notices_enabled``) — the gate runs at the end of every
        turn, and a config flip applying on the next session is fine.
        The env-var override stays authoritative on every call and is never
        cached, so tests and operators can still flip it at runtime.
        """
        try:
            import os as _os
            env = _os.environ.get("HERMES_TURN_COMPLETION_EXPLAINER")
            if env is not None:
                return env.strip().lower() not in {"0", "false", "no", "off"}
            cached = getattr(self, "_turn_completion_explainer_enabled_cache", None)
            if cached is not None:
                return cached
            # Read from the persisted config.yaml so gateway and CLI share
            # the same setting.  Import lazily to avoid a startup-time cycle.
            try:
                from hermes_cli.config import load_config as _load_config
                _cfg = _load_config() or {}
            except Exception:
                _cfg = {}
            _display = _cfg.get("display") if isinstance(_cfg, dict) else None
            if isinstance(_display, dict) and "turn_completion_explainer" in _display:
                enabled = bool(_display.get("turn_completion_explainer"))
            else:
                enabled = True  # safe default: explainer on
            self._turn_completion_explainer_enabled_cache = enabled
            return enabled
        except Exception:
            pass
        return True  # safe default: explainer on

    @staticmethod
    def _format_turn_completion_explanation(
        turn_exit_reason: str, persistence_cause: Optional[str] = None
    ) -> str:
        """Render a user-facing explanation for an abnormal turn ending.

        Maps the internal ``turn_exit_reason`` to a short, actionable
        message so a turn that produced no usable assistant reply (empty
        content after retries, a partial/truncated stream, a still-pending
        tool result, or an iteration/budget limit) is never silent from
        the UI's perspective — the symptom users report in #34452.

        ``persistence_cause`` refines the ``session_persistence_failed``
        wording (see ``classify_persistence_error``): lock contention gets
        "storage was busy, send it again" instead of the disk-space advice,
        which was a misdiagnosis for that failure mode. It is optional and
        ignored for every other reason, so one-argument callers keep the
        exact behavior they had before.

        Returns an empty string for reasons that are NOT abnormal (e.g.
        a normal ``text_response(...)`` exit), so callers can concatenate
        or substitute unconditionally without warning on healthy turns
        like a terse ``Done.``.
        """
        if not turn_exit_reason:
            return ""
        reason = str(turn_exit_reason)

        # Normal completion — stay quiet.  ``text_response(...)`` is the
        # healthy terminal; anything that produced a real reply is fine.
        if reason.startswith("text_response"):
            return ""

        # A terminal gateway-restart control is contractually silent: the
        # restart confirmation/drain/comeback UI is the only lifecycle output
        # for that outcome. Never fabricate an "abnormal ending" message.
        if reason == "gateway_restart_queued":
            return ""

        prefix = "⚠️ No reply: "
        if reason == "empty_response_exhausted":
            return (
                prefix
                + "the model returned empty content after retries and any "
                "fallback providers. Try `continue`, switch model/provider, "
                "or inspect the tool output above."
            )
        if reason == "all_retries_exhausted_no_response":
            return (
                prefix
                + "all API retries were exhausted before a response was "
                "produced (provider errors / rate limits). Try `continue` "
                "or switch provider."
            )
        if reason == "partial_stream_recovery":
            return (
                prefix
                + "streaming stopped early and only a partial response was "
                "recovered. Send `continue` to resume from where it stopped."
            )
        if reason == "fallback_prior_turn_content":
            return (
                prefix
                + "no new content was produced this turn; showing recovered "
                "prior context. Send `continue` to retry."
            )
        if reason == "interrupted_during_api_call":
            return (
                prefix
                + "the request was interrupted mid-call before a reply was "
                "received. Send `continue` to retry."
            )
        if reason == "budget_exhausted":
            return (
                prefix
                + "the per-turn iteration/cost budget was exhausted before a "
                "final answer. Send `continue` to keep going."
            )
        if reason == "ollama_runtime_context_too_small":
            return (
                prefix
                + "the local model's context window was too small to finish. "
                "Increase the context size or use a larger model."
            )
        if reason.startswith("max_iterations_reached"):
            return (
                prefix
                + "the maximum tool-iteration limit was reached before a "
                "final answer. Send `continue` to keep going, or raise "
                "`max_iterations`."
            )
        if reason.startswith("error_near_max_iterations"):
            return (
                prefix
                + "an error occurred near the iteration limit before a final "
                "answer. Check the tool output above, then send `continue`."
            )
        if reason.startswith("repeated_outer_errors"):
            return (
                prefix
                + "the turn kept failing with repeated errors and was stopped "
                "early instead of retrying forever. Check the errors above, "
                "then send `continue` to retry."
            )
        if reason == "pending_tool_result":
            return (
                prefix
                + "the turn stopped while a tool result was still pending and "
                "the model produced no follow-up text. Send `continue` to "
                "let it summarize."
            )
        if reason == "session_persistence_failed":
            cause = persistence_cause or "unknown"
            if cause == "compression":
                return (
                    prefix
                    + "the turn was stopped because another process was "
                    "compressing this session. Your message should already be "
                    "saved — please send it again after compression completes."
                )
            if cause == "compression_closed":
                return (
                    prefix
                    + "the turn was stopped because this session was rotated "
                    "by context compression and its live continuation could "
                    "not be adopted. The storage itself is healthy — refresh "
                    "the client (or start a new turn) so it picks up the new "
                    "session id, then send your message again."
                )
            if cause == "turn_lease":
                return (
                    prefix
                    + "the turn was stopped because another Hermes process "
                    "took over this session. Your reply was not saved — wait "
                    "for the other process to finish, then send your message "
                    "again."
                )
            if cause == "locked":
                return (
                    prefix
                    + "the turn was stopped because session storage was busy "
                    "(another Hermes process was writing to the state "
                    "database). Your message should already be saved — "
                    "please send it again in a moment."
                )
            if cause == "replaced":
                return (
                    prefix
                    + "the turn was stopped because the state database file "
                    "was replaced underneath this process. Do not run "
                    "`hermes doctor --fix` or in-place FTS repair — stop "
                    "the process, restore the intended state.db, then "
                    "restart. Unwritten messages were diverted to "
                    "sessions/<session_id>.jsonl and, on the gateway, "
                    "pending_messages/pending-*.json."
                )
            if cause == "corrupt":
                from hermes_state import _default_db_path

                # Copy-pasteable, so name the real store (profiles /
                # HERMES_HOME do not live under ~/.hermes).
                db_path = _default_db_path()
                return (
                    prefix
                    + "the turn was stopped because the state database "
                    "reported structural corruption (the transcript would "
                    "have been lost on restart). Freeing disk space will "
                    "not help. Recovery options:\n"
                    "1. Run `hermes doctor --fix`\n"
                    "2. Stop the gateway, then recover with:\n"
                    f"   hermes sessions recover --source {db_path} "
                    "--inspect-only\n"
                    "   (if it reports recoverable) hermes sessions recover "
                    f"--source {db_path} --output recovered-state.db\n"
                    "   — recovery snapshots the damaged file first; do NOT "
                    "run `sqlite3 ... \".recover\"` against the live "
                    "state.db, a vulnerable sqlite3 CLI can corrupt it "
                    "further\n"
                    "3. Restore from a backup in ~/.hermes/backups/\n"
                    "Then send your message again."
                )
            if cause == "disk":
                return (
                    prefix
                    + "the turn was stopped because session storage could not "
                    "be written (the transcript would have been lost on "
                    "restart). This is often a full disk — free some space "
                    "(or fix state.db permissions), then send your message "
                    "again."
                )
            return (
                prefix
                + "the turn was stopped because session storage could not be "
                "written (the transcript would have been lost on restart). "
                "Check the state database health (`hermes doctor`), then "
                "send your message again."
            )
        # Unknown/diagnostic-only reasons (e.g. "unknown", guardrail_halt
        # which already surfaces its own message) — don't second-guess.
        return ""

    def _apply_pending_steer_to_tool_results(self, messages: list, num_tool_msgs: int) -> None:
        """Forwarder — see ``agent.agent_runtime_helpers.apply_pending_steer_to_tool_results``."""
        from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
        return apply_pending_steer_to_tool_results(self, messages, num_tool_msgs)

    def _liveness_activity_lock(self) -> "threading.Lock":
        """Shared lock for the activity clock and its generation counter.

        ``_touch_activity`` stamps the clock under this lock; the turn
        liveness watchdog (``agent/turn_liveness.py``) samples and commits
        under the same lock, so a stall observation can never abort a turn
        that resumed between the sample and the commit (#95663 review).
        Created lazily so ``AIAgent.__new__``-based test doubles keep
        working.
        """
        _lock = getattr(self, "_turn_liveness_activity_lock", None)
        if _lock is None:
            _lock = threading.Lock()
            self._turn_liveness_activity_lock = _lock
        return _lock

    def _touch_activity(
        self,
        desc: str,
        *,
        provenance: Optional[ActivityProvenance] = None,
        force_persist: bool = False,
    ) -> None:
        """Update the last-activity timestamp and description (thread-safe).

        The clock stamp is synchronized on ``_liveness_activity_lock`` and
        bumps a monotonic generation counter, so concurrent readers (the
        turn liveness watchdog, #95548) can bind their stall observation to
        the exact ``(generation, timestamp)`` pair they sampled and
        revalidate it at the commit point.

        Also bridges to the kanban board's heartbeat fields when this
        process is a dispatcher-spawned worker (HERMES_KANBAN_TASK set),
        so the dispatcher watchdog doesn't reclaim an actively-running
        worker as stale (#31752). Bridge is rate-limited (60s) and
        best-effort — it never raises into the agent loop.

        Separately, rate-limits a durable SessionDB activity projection
        (``last_activity_at`` + bounded description/provenance) so
        CLI/Gateway consumers share one observation source (#72016 / #72039).

        ``provenance`` defaults to ``unknown`` (the ordinary agent activity
        clock). Named values are for special writers (e.g. compression);
        ordinary call sites should leave the default.

        ``force_persist`` bypasses the 60s SessionDB rate limit so a
        terminal stamp (e.g. compression completed) is not dropped.
        """
        from agent.session_activity import (
            bound_activity_description,
            normalize_activity_provenance,
            reset_session_activity_persist_window,
        )

        # Lazy per-instance lock (inline so bare doubles like
        # types.SimpleNamespace fixtures keep working — they bind
        # _touch_activity without the class, so they cannot call
        # self._liveness_activity_lock(); see
        # tests/run_agent/test_session_activity_persist.py).
        _clock_lock = getattr(self, "_turn_liveness_activity_lock", None)
        if _clock_lock is None:
            _clock_lock = threading.Lock()
            self._turn_liveness_activity_lock = _clock_lock
        with _clock_lock:
            self._turn_liveness_activity_generation = (
                getattr(self, "_turn_liveness_activity_generation", 0) + 1
            )
            now_ts = time.time()
            bounded_desc = bound_activity_description(desc)
            prev_desc = getattr(self, "_last_activity_desc", "")
            self._last_activity_ts = now_ts
            self._last_activity_desc = bounded_desc
            self._last_activity_provenance = normalize_activity_provenance(provenance)
            # Phase clock: when the CURRENT wait started. Liveness heartbeats
            # re-stamp the SAME description every ~30s; those refreshes must
            # not restart the clock, or the gateway phase heartbeat line
            # ("⏳ <noun> <elapsed>") shows 0–30s forever. A new phase opens
            # only when the normalized description changes (cosmetic case /
            # whitespace differences are the same phase; provenance is not
            # part of the comparison — any writer can open a new phase).
            if " ".join(bounded_desc.split()).lower() != " ".join(
                (prev_desc or "").split()
            ).lower():
                self._phase_started_ts = now_ts
            # Real progress invalidates any reserved abort claim. A watchdog
            # interrupt that is still in flight (e.g. parked inside the
            # compression commit fence) must abandon itself at the final
            # mutation edge instead of publishing against a generation the
            # turn has already left behind.
            self._turn_liveness_abort_claim = None
        if os.environ.get("HERMES_KANBAN_TASK"):
            try:
                from tools.kanban_tools import (
                    heartbeat_current_worker_from_env,
                    inject_new_comments_from_env,
                )
                heartbeat_current_worker_from_env()
                # Fold any new operator notes into the running turn (OUT-OF-BAND
                # steer) so the user can talk to a live task without a restart.
                inject_new_comments_from_env(self)
            except Exception:
                # Never let the bridge break the agent loop.  The function
                # already swallows exceptions internally; this outer guard
                # covers import-time failures (kanban_tools unavailable,
                # etc.) on niche deployment surfaces.
                pass
        if force_persist:
            reset_session_activity_persist_window(self)
        self._persist_session_activity_if_due()

    def _persist_session_activity_if_due(self) -> None:
        """Best-effort durable activity heartbeat for SessionDB consumers.

        Cadence is pinned by SESSION_ACTIVITY_HEARTBEAT_MIN_INTERVAL_SECONDS
        (>=30s per session, config-independent — see agent/session_activity.py).
        The write rides the standard SessionDB ``_execute_write`` patience
        path via ``touch_session_activity``. Fail-open: a failed heartbeat
        write must NEVER raise into the agent loop (swallow + debug-log).
        """
        session_id = getattr(self, "session_id", None)
        session_db = getattr(self, "_session_db", None)
        if not session_id or session_db is None:
            return
        touch = getattr(session_db, "touch_session_activity", None)
        if not callable(touch):
            return
        from agent.session_activity import (
            SESSION_ACTIVITY_HEARTBEAT_MIN_INTERVAL_SECONDS,
            normalize_activity_provenance,
        )

        now_mono = time.monotonic()
        last_mono = getattr(self, "_session_activity_last_persist_mono", 0.0)
        if (now_mono - last_mono) < SESSION_ACTIVITY_HEARTBEAT_MIN_INTERVAL_SECONDS:
            return
        self._session_activity_last_persist_mono = now_mono
        try:
            touch(
                session_id,
                getattr(self, "_last_activity_ts", None),
                description=getattr(self, "_last_activity_desc", None),
                provenance=normalize_activity_provenance(
                    getattr(self, "_last_activity_provenance", None)
                ),
            )
        except Exception:
            # Never let durable heartbeat I/O break the agent loop. The
            # heartbeat is an observation-only projection; the next due
            # window retries naturally.
            logger.debug(
                "session activity heartbeat write failed (ignored)",
                exc_info=True,
            )

    def _reset_activity_labels_after_turn(self) -> None:
        """Drop mid-turn activity labels once the turn is no longer running.

        Keeps ``_last_activity_ts`` so idle/watchdog clocks stay continuous
        across interrupt-recursive turns (#15654) and between turns. Clears
        description + provenance so idle cached agents / SessionDB listings
        do not keep advertising the last mid-turn stamp (e.g. compression
        or tool execution) after the turn ended (#72039). Also closes the
        phase clock — the next stamp opens a fresh phase instead of letting
        the heartbeat keep timing a wait that already ended.
        """
        from agent.session_activity import ActivityProvenance

        self._last_activity_desc = ""
        self._last_activity_provenance = ActivityProvenance.UNKNOWN
        self._phase_started_ts = None
        session_id = getattr(self, "session_id", None)
        session_db = getattr(self, "_session_db", None)
        if not session_id or session_db is None:
            return
        clear = getattr(session_db, "clear_session_activity_labels", None)
        if not callable(clear):
            return
        try:
            clear(session_id)
        except Exception:
            # Never let durable cleanup I/O break turn teardown.
            pass

    def _capture_rate_limits(self, http_response: Any) -> None:
        """Parse x-ratelimit-* headers from an HTTP response and cache the state.

        Called after each streaming API call.  The httpx Response object is
        available on the OpenAI SDK Stream via ``stream.response``.
        """
        if http_response is None:
            return
        headers = getattr(http_response, "headers", None)
        if not headers:
            return
        try:
            from agent.rate_limit_tracker import parse_rate_limit_headers
            state = parse_rate_limit_headers(headers, provider=self.provider)
            if state is not None:
                self._rate_limit_state = state
        except Exception:
            pass  # Never let header parsing break the agent loop

    def get_rate_limit_state(self):
        """Return the last captured RateLimitState, or None."""
        return self._rate_limit_state

    def _capture_anthropic_response_headers(self, http_response: Any) -> None:
        """Capture out-of-band state from Anthropic Messages response headers.

        The Anthropic SDK's aggregated ``Message`` drops HTTP headers. Portal
        (and other providers) put rate-limit and credits state there — the same
        families the OpenAI-wire streaming path captures via
        ``stream.response``. Fail-open: each capture swallows its own errors.
        """
        self._capture_rate_limits(http_response)
        self._capture_credits(http_response)

    def _capture_credits(self, http_response: Any) -> None:
        """Parse x-nous-credits-* headers, cache CreditsState, fire threshold notices.

        Fail-open throughout — header issues never break the agent loop. The PARSE is
        swallowed (any error → treated as a miss → keep last-known). The notice
        EVALUATION/EMIT is a SEPARATE block that WARNS on failure (R1-M2): a bug in the
        depletion-notice path must not vanish silently under the parse swallow.
        """
        # Dev test fixture (HERMES_DEV_CREDITS_FIXTURE): inject a chosen notice state
        # each turn for repeatable testing, bypassing real headers. Throwaway scaffolding.
        try:
            from agent.credits_tracker import dev_fixture_credits_state
            _fixture = dev_fixture_credits_state()
        except Exception:
            _fixture = None
        if _fixture is not None:
            self._credits_state = _fixture
            if self._credits_session_start_micros is None:
                self._credits_session_start_micros = _fixture.remaining_micros
            _latch = getattr(self, "_credits_latch", None)
            if isinstance(_latch, dict):
                # Only seen_below_90 — never seen_grant_unspent (priming it would
                # fire grant_spent on a fixture's first observation, the exact
                # every-session nag the gate exists to prevent).
                _latch["seen_below_90"] = True  # let warn90 fire without a real crossing
            _used = _fixture.used_fraction
            logger.info(
                "credits ▸ [FIXTURE] remaining=%d (%s) · paid=%s · denom=%s · used=%s "
                "(real headers bypassed — `echo clear` / unset HERMES_DEV_CREDITS_FIXTURE to restore)",
                _fixture.remaining_micros,
                _fixture.remaining_usd or "?",
                _fixture.paid_access,
                _fixture.denominator_kind,
                ("%.0f%%" % (_used * 100)) if _used is not None else "n/a",
            )
            self._emit_credits_notices()
            return
        if http_response is None:
            return
        headers = getattr(http_response, "headers", None)
        if not headers:
            return
        _dev = is_truthy_value(os.environ.get("HERMES_DEV_CREDITS"))

        # ── Parse (fail-open → miss; never overwrite good state with None) ──
        try:
            from agent.credits_tracker import parse_credits_headers
            state = parse_credits_headers(headers, provider=self.provider)
        except Exception:
            return  # parse error → treat as a miss, keep last-known
        if state is None:
            if _dev:
                logger.info(
                    "credits ▸ response had no valid x-nous-credits-* headers "
                    "(miss — producer off / non-Nous path / >TTL stale)"
                )
            return

        # retain-last-known: only overwrite on a fresh valid parse
        self._credits_state = state
        # Latch session-start remaining the first time we ever see a header
        if self._credits_session_start_micros is None:
            self._credits_session_start_micros = state.remaining_micros
        if _dev:
            # HERMES_DEV_CREDITS: stream each capture to agent.log — watch live with
            # `hermes logs -f` (grep 'credits ▸'). Dev-only; silent for normal users.
            spent = self.get_credits_spent_micros()
            used = state.used_fraction
            logger.info(
                "credits ▸ remaining=%d (%s) · paid=%s · denom=%s · used=%s "
                "· Δspent=%s · age=%s%s",
                state.remaining_micros,
                state.remaining_usd or "?",
                state.paid_access,
                state.denominator_kind,
                ("%.0f%%" % (used * 100)) if used is not None else "n/a",
                ("%.1f¢" % (spent / 10000)) if spent is not None else "n/a",
                ("%.0fs" % state.age_seconds) if state.age_seconds != float("inf") else "n/a",
                (" · disabled=%s" % state.disabled_reason) if state.disabled_reason else "",
            )

        # Threshold notices — shared with the cold-start seed (see _emit_credits_notices).
        self._emit_credits_notices()

    def _emit_credits_notices(self) -> None:
        """Run the threshold policy on the current credits state and emit notices.

        Shared by the warm path (_capture_credits) and the L3 cold-start seed, so a
        session that opens already depleted warns immediately — not only after the first
        inference header. Runs only when a notice consumer is bound (messaging binds none
        → state still cached for /usage, no policy). WARNS on failure rather than
        swallowing (R1-M2): a depletion-path bug must not vanish silently. Emits clears
        FIRST, then shows (so depleted lands last in a latest-wins slot).
        """
        if getattr(self, "notice_callback", None) is None and getattr(self, "notice_clear_callback", None) is None:
            return
        if not self._credits_notices_enabled():
            return
        state = getattr(self, "_credits_state", None)
        if state is None:
            return
        try:
            from agent.credits_tracker import evaluate_credits_notices, is_free_tier_model, new_credits_latch
            latch = getattr(self, "_credits_latch", None)
            if latch is None:
                latch = self._credits_latch = new_credits_latch()
            # Free-model gate: a depleted account on a free model can still
            # inference, so the depleted error banner is suppressed. Local-data
            # only (":free" suffix, "stealth/" prefix + pricing-cache peek) —
            # never a network call.
            model_is_free = is_free_tier_model(
                getattr(self, "model", "") or "",
                getattr(self, "base_url", "") or "",
            )
            to_show, to_clear = evaluate_credits_notices(state, latch, model_is_free=model_is_free)
            for key in to_clear:        # clears FIRST …
                self._emit_notice_clear(key)
            for notice in to_show:      # … then shows (depleted lands last in a latest-wins slot)
                self._emit_notice(notice)
        except Exception:
            logger.warning("credits notice evaluation/emit failed", exc_info=True)

    def _credits_notices_enabled(self) -> bool:
        """Whether credits notices are enabled (config display.credits_notices).

        Read once per agent and cached — the policy runs after every API
        response, and the setting governs UI noise, not correctness, so a
        config flip applying on the next session is fine.  Fail-open True
        (preserve current behaviour) on any config error.
        """
        cached = getattr(self, "_credits_notices_enabled_cache", None)
        if cached is not None:
            return cached
        enabled = True
        try:
            from hermes_cli.config import load_config as _load_config
            _cfg = _load_config() or {}
            _display = _cfg.get("display") if isinstance(_cfg, dict) else None
            if isinstance(_display, dict) and "credits_notices" in _display:
                enabled = bool(_display.get("credits_notices"))
        except Exception:
            enabled = True
        self._credits_notices_enabled_cache = enabled
        return enabled

    def get_credits_state(self):
        """Return the last captured CreditsState, or None."""
        return self._credits_state

    def get_credits_spent_micros(self):
        """Session-cumulative micros spent = first_seen_remaining - current_remaining. None if no data."""
        if self._credits_session_start_micros is None or self._credits_state is None:
            return None
        return self._credits_session_start_micros - self._credits_state.remaining_micros

    def _check_openrouter_cache_status(self, http_response: Any) -> None:
        """Read X-OpenRouter-Cache-Status from response headers and log it.

        Increments ``_or_cache_hits`` on HIT so callers can report savings.
        """
        if http_response is None:
            return
        headers = getattr(http_response, "headers", None)
        if not headers:
            return
        try:
            status = headers.get("x-openrouter-cache-status")
            if not status:
                return
            if status.upper() == "HIT":
                self._or_cache_hits += 1
                logger.info("OpenRouter response cache HIT (total: %d)", self._or_cache_hits)
            else:
                logger.debug("OpenRouter response cache %s", status.upper())
        except Exception:
            pass  # Never let header parsing break the agent loop

    def get_activity_summary(self) -> dict:
        """Diagnostic snapshot: ``last_activity_*`` plus the short aliases gateway and delegate readers use."""
        from agent.session_activity import build_activity_snapshot

        provenance = getattr(self, "_last_activity_provenance", None)
        return build_activity_snapshot(
            last_activity_at=getattr(self, "_last_activity_ts", None),
            last_activity_description=getattr(self, "_last_activity_desc", None) or "",
            last_activity_provenance=provenance if provenance is not None else ActivityProvenance.UNKNOWN,
            phase_started_at=getattr(self, "_phase_started_ts", None),
            extra={
                "current_tool": self._current_tool, "api_call_count": self._api_call_count,
                "max_iterations": self.max_iterations, "budget_used": self.iteration_budget.used,
                "budget_max": self.iteration_budget.max_total,
            },
        )

    def shutdown_memory_provider(self, messages: list = None) -> None:
        """Shut down the memory provider and context engine at session end (idempotent: gateway cleanup and
        ``close()`` may both call it)."""
        if getattr(self, "_memory_provider_shutdown", False):
            return
        self._memory_provider_shutdown = True
        if self._memory_manager:
            try:
                self._memory_manager.on_session_end(messages or [])
            except Exception as e:
                logger.warning("Memory provider on_session_end failed during shutdown: %s", e, exc_info=True)
            _quietly(lambda: self._memory_manager.shutdown_all())
        _notify_context_engine_session_end(self, messages)

    def commit_memory_session(self, messages: list = None) -> None:
        """Flush end-of-session extraction on session_id rotation (/new, compression) without tearing providers
        down."""
        if self._memory_manager:
            _quietly(lambda: self._memory_manager.on_session_end(messages or []))
        _notify_context_engine_session_end(self, messages)

    def _sync_external_memory_for_turn(self, *, original_user_message: Any, final_response: Any, interrupted: bool,
                                       messages: list | None = None) -> None:
        """Mirror a completed turn into external memory providers (``sync_all`` + ``queue_prefetch_all``).

        Uses ``original_user_message`` (``user_message`` may carry injected skill content). Interrupted turns
        are skipped: partial output is not durable truth. Best-effort — an offline backend never blocks.

        A partial assistant output, an aborted tool chain, or a mid-stream reset is not durable
        conversational truth — mirroring it into an external memory backend pollutes future recall with
        state the user never saw completed. The prefetch is gated on the same flag: the user's next message
        is almost certainly a retry of the same intent, and a prefetch keyed on the interrupted turn would
        fire against stale context. See #15218.
        """
        if interrupted or not (self._memory_manager and final_response and original_user_message):
            return
        # Flatten multimodal parts to text (newline-joined for memory).
        user_text = _summarize_user_message_for_log(original_user_message, sep="\n")
        response_text = _summarize_user_message_for_log(final_response, sep="\n")
        if not (user_text and response_text):
            return
        try:
            sync_kwargs = {"session_id": self.session_id or "", **({"messages": messages} if messages is not None else {})}
            # Stashed by build_turn_context() for this turn, None on a human turn.
            turn_author = getattr(self, "_turn_author", None)
            if turn_author is not None:
                sync_kwargs["turn_author"] = turn_author
            self._memory_manager.sync_all(user_text, response_text, **sync_kwargs)
            # Sibling of the build_turn_context() prefetch gate: don't key recall on zero-signal prompts.
            if not is_trivial_prompt(user_text):
                self._memory_manager.queue_prefetch_all(user_text, session_id=self.session_id or "")
        except Exception:
            pass

    def release_clients(self) -> None:
        """Release LLM clients and child agents WITHOUT tearing down session tool state (gateway cache
        eviction: the session may resume on the same task_id, so processes, sandbox, browser, computer-use and
        memory provider are kept). Idempotent; distinct from ``close()``."""
        self._close_active_children(soft=True)
        # Retire (don't hard-close) the shared client: eviction runs on the gateway memory-manager thread,
        # and a cross-thread close can release TLS FDs under a still-unwinding worker.
        _quietly(self._drop_shared_client, lambda c: self._retire_shared_openai_client(c, reason="cache_evict"))
        self._close_request_clients("cache_evict")

    def close(self) -> None:
        """Release every resource this agent holds (idempotent); each phase is guarded so one failure never
        blocks the rest."""
        # close() is the hard owner boundary; shutdown_memory_provider() is idempotent so gateway pre-calls
        # never double-extract.
        session_messages = getattr(self, "_session_messages", None)
        _quietly(self.shutdown_memory_provider, session_messages if isinstance(session_messages, list) else None)
        self._close_task_resources(getattr(self, "session_id", None) or "")
        self._close_active_children(soft=False)
        _quietly(self._drop_shared_client, lambda c: self._close_openai_client(c, reason="agent_close", shared=True))
        self._close_request_clients("agent_close")
        _quietly(self._close_codex_session)
        # Free conversation history proactively: callers may still hold the closed agent. The DB-flush
        # settled-prefix snapshot and the streamed-text accumulator are shadow copies of the same transcript;
        # on a closed delegate child they were the only remaining owners, pinning its history in the parent heap.
        self._session_messages = []
        self._db_flush_scan_prefix = None
        self._streamed_assistant_text_parts = []
        _quietly(self._trim_process_memory)
        _quietly(self._finalize_owned_session_row)

    # -- close()/release_clients() phases -------------------------------------------------------------

    def _close_active_children(self, *, soft: bool) -> None:
        """Detach and close per-turn child agents; ``soft`` releases their clients first, falling back to close()."""
        try:
            with self._active_children_lock:
                children = list(self._active_children)
                self._active_children.clear()
        except Exception:
            return
        for child in children:
            if soft:
                try:
                    child.release_clients()
                    continue
                except Exception:
                    pass
            _quietly(lambda: child.close())

    def _drop_shared_client(self, close_fn: Callable[[Any], None]) -> None:
        """Hand the shared OpenAI/httpx client to ``close_fn`` and clear the attribute."""
        # Retire the OpenAI/httpx client to release sockets immediately. #70773: eviction runs on the
        # gateway's memory-manager thread — a cross-thread hard close of the shared client can release TLS
        # FDs under a still-unwinding worker (FD-recycle → SQLite corruption). Retirement shuts the pooled
        # sockets down (the memory/socket win we want here) and lets GC release the FDs once no thread holds
        # them.
        client = getattr(self, "client", None)
        if client is not None:
            close_fn(client)
            self.client = None

    def _close_request_clients(self, reason: str) -> None:
        """Drop the cached per-request wire clients (reused across sequential LLM calls)."""
        _quietly(self._close_cached_request_openai_client, reason=reason)
        _quietly(self._close_cached_request_anthropic_client, reason=reason)

    def _close_codex_session(self) -> None:
        """Close the Codex app-server session (else the child keeps running); the attribute is cleared BEFORE
        close() so a concurrent reader can't grab a half-closed session."""
        codex_session = getattr(self, "_codex_session", None)
        if codex_session is not None:
            self._codex_session = None
            codex_session.close()

    @staticmethod
    def _trim_process_memory() -> None:
        """Return freed heap pages to the OS on glibc; safe no-op elsewhere."""
        from hermes_cli.mem_trim import trim_memory
        trim_memory(force=True, reason="agent close")

    def _finalize_owned_session_row(self) -> None:
        """End the session row unless ownership was handed forward (compression helpers, review forks sharing
        the parent's id; end_session() is first-reason-wins), then release the SQLite handle ONLY when this
        agent owns it — a dedicated handle left open pins its fds and token-writer thread for the process
        lifetime. The owner flag is cleared first so close() stays idempotent."""
        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "session_id", None)
        if getattr(self, "_end_session_on_close", True) and session_db and session_id:
            _quietly(lambda: session_db.end_session(session_id, "agent_close"))
        if getattr(self, "_owns_session_db", False) and session_db is not None:
            self._owns_session_db = False
            # Shared instances no-op on close(); release the refcount so the registry closes on the last caller.
            # See #90837.
            from hermes_state_registry import release_or_close
            release_or_close(session_db)

    def _hydrate_todo_store(self, history: List[Dict[str, Any]]) -> None:
        """Replay the most recent todo tool response (the gateway builds a fresh AIAgent per message). Only
        results paired with an earlier assistant ``todo`` call count — a forged bare ``role: tool`` message
        must not seed the store (GHSA-5g4g-6jrg-mw3g)."""
        found = self._latest_todo_response(history)
        if found is not None:
            last_todo_response, last_todo_revision = found
            # Restore only when history carries a newer revision than the store holds; empty lists are an
            # authoritative clear.
            try:
                history_revision = max(0, int(last_todo_revision or 0))
            except (TypeError, ValueError):
                history_revision = 1
            if history_revision > int(self._todo_store.snapshot().get("revision", 0) or 0):
                self._todo_store.restore(last_todo_response, revision=history_revision)
                if not self.quiet_mode:
                    self._vprint(f"{self.log_prefix}📋 Restored {len(last_todo_response)} todo item(s) from history")
        _set_interrupt(False)

    def _latest_todo_response(self, history: List[Dict[str, Any]]) -> Optional[tuple]:
        """Walk history backwards for the newest paired, size-bounded todo result → ``(todos, revision)``."""
        from tools.todo_tool import MAX_TODO_RESULT_CHARS

        for idx in range(len(history) - 1, -1, -1):
            msg = history[idx]
            content = msg.get("content", "")
            if msg.get("role") != "tool" or not isinstance(content, str) or not self._tool_response_matches_todo_call(history, idx):
                continue
            if len(content) > MAX_TODO_RESULT_CHARS:
                logger.warning("Skipping oversized todo tool response during hydration: "
                               "session=%s chars=%d", self.session_id or "none", len(content))
                continue
            if '"todos"' not in content:  # cheap pre-filter before json.loads
                continue
            try:
                data = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                continue
            if "todos" in data and isinstance(data["todos"], list):
                return data["todos"], data.get("revision", 1)
        return None

    @classmethod
    def _tool_response_matches_todo_call(cls, history: List[Dict[str, Any]], tool_index: int) -> bool:
        """True when the nearest prior assistant message issued a ``todo`` call with this ``tool_call_id``; a
        ``user``/``system`` boundary or missing id means unpaired → must not hydrate."""
        tool_call_id = history[tool_index].get("tool_call_id") if 0 <= tool_index < len(history) else None
        if not tool_call_id:
            return False
        for prior in reversed(history[:tool_index]):
            role = prior.get("role")
            if role == "assistant":
                return cls._assistant_has_todo_tool_call(prior, tool_call_id)
            if role in {"user", "system"}:
                return False
        return False

    @classmethod
    def _assistant_has_todo_tool_call(cls, assistant_msg: Dict[str, Any], tool_call_id: str) -> bool:
        """True when the assistant message issued a ``todo`` call with this id."""
        tool_calls = assistant_msg.get("tool_calls")
        return isinstance(tool_calls, list) and any(
            cls._get_tool_call_id_static(tc) == tool_call_id and cls._get_tool_call_name_static(tc) == "todo"
            for tc in tool_calls
        )

    @property
    def is_interrupted(self) -> bool:
        """Check if an interrupt has been requested."""
        return self._interrupt_requested

    _build_system_prompt = _forward("agent.system_prompt", "build_system_prompt")

    # Call ID of a tool_call entry (dict or object); policy owner: ``message_sanitization.coalesce_tool_call_id``.
    _get_tool_call_id_static = staticmethod(_sanitize_coalesce_tool_call_id)

    @staticmethod
    def _get_tool_call_name_static(tc) -> str:
        """Function name of a tool_call entry (dict or object); Gemini requires it on every ``role: tool`` message."""
        if isinstance(tc, dict):
            fn = tc.get("function")
            return (fn.get("name", "") or "") if isinstance(fn, dict) else ""
        return getattr(getattr(tc, "function", None), "name", "") or ""

    _VALID_API_ROLES = frozenset({"system", "user", "assistant", "tool", "function", "developer"})
    _sanitize_api_messages = _forward_static("agent.agent_runtime_helpers", "sanitize_api_messages")

    @staticmethod
    def _is_thinking_only_assistant(msg: Dict[str, Any], *, drop_codex_reasoning_items: bool = True) -> bool:
        """True if ``msg`` is an assistant turn whose only payload is reasoning (no text, no tool_calls).

        Providers converting reasoning to thinking blocks reject it (400 "final block cannot be thinking"), so
        the turn is dropped from the API copy; the transcript keeps the reasoning block.
        """
        if not isinstance(msg, dict) or msg.get("role") != "assistant" or msg.get("tool_calls"):
            return False
        # Prefill stubs are thinking-only by construction; checked before content inspection since
        # repair_empty_non_final_messages may have healed content.
        if msg.get("_thinking_prefill"):
            return True
        if AIAgent._content_has_real_payload(msg.get("content")):
            return False
        # A native compaction checkpoint makes a carrier never thinking-only, regardless of api_mode or
        # reasoning field. Checked above every reasoning branch so no carrier shape is dropped.
        # The checkpoint is the server-side stand-in for already-pruned history and exists in exactly one
        # place; the codex_responses adapter also surfaces commentary text via msg["reasoning"], so the
        # string branch below would otherwise drop a carrier before the sidecar is ever inspected. See
        # #82108.
        from agent.native_compaction import has_compaction_checkpoint

        if has_compaction_checkpoint(msg.get("codex_reasoning_items")):
            return False
        reasoning = msg.get("reasoning_content") or msg.get("reasoning")
        rd = msg.get("reasoning_details")
        if (isinstance(reasoning, str) and reasoning.strip()) or (isinstance(rd, list) and rd):
            return True
        # Codex Responses keeps encrypted reasoning under a separate key; only real items count as
        # thinking-only, empty/junk lists fall through to generic empty-turn handling.
        codex_items = msg.get("codex_reasoning_items")
        if drop_codex_reasoning_items and isinstance(codex_items, list):
            return any(isinstance(item, dict) and item.get("type") == "reasoning" for item in codex_items)
        return False

    @staticmethod
    def _content_has_real_payload(content: Any) -> bool:
        """True when assistant ``content`` carries anything beyond (redacted) thinking blocks / whitespace."""
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    if block:  # non-empty non-dict string etc.
                        return True
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if isinstance(text, str) and text.strip():
                        return True
                elif btype not in {"thinking", "redacted_thinking"}:
                    return True  # tool_use, image, document, etc. — real payload
            return False
        return content is not None and content != ""

    _drop_thinking_only_and_merge_users = _forward_static("agent.agent_runtime_helpers", "drop_thinking_only_and_merge_users")

    @staticmethod
    def _cap_delegate_agent_calls(tool_calls: list) -> list:
        """Truncate excess delegate_agent calls to max_concurrent_children.

        The delegate_tool caps the task list inside a single call, but the
        model can emit multiple separate delegate_agent tool_calls in one
        turn.  This truncates the excess, preserving all non-delegate calls.

        Returns the original list if no truncation was needed.
        """
        from tools.delegate_tool import _get_max_concurrent_children
        max_children = _get_max_concurrent_children()
        # Both spellings count: a legacy ``delegate_task`` call is the same spawn.
        delegate_count = sum(
            1 for tc in tool_calls
            if tc.function.name in ("delegate_agent", "delegate_task")
        )
        if delegate_count <= max_children:
            return tool_calls
        kept_delegates, truncated = 0, []
        for tc in tool_calls:
            if tc.function.name in ("delegate_agent", "delegate_task"):
                if kept_delegates < max_children:
                    truncated.append(tc)
                    kept_delegates += 1
            else:
                truncated.append(tc)
        logger.warning(
            "Truncated %d excess delegate_agent call(s) to enforce "
            "max_concurrent_children=%d limit",
            delegate_count - max_children, max_children,
        )
        return truncated

    # Upstream's pre-rename alias (agent/turn_tool_round.py still calls this spelling).
    _cap_delegate_task_calls = _cap_delegate_agent_calls

    @staticmethod
    def _deduplicate_tool_calls(tool_calls: list) -> list:
        """Drop duplicate (tool_name, arguments) pairs in one turn (first wins). Valid JSON arguments are
        canonicalized so key order/whitespace can't evade dedup; returns the original list when nothing was removed."""
        seen, unique = set(), []
        for tc in tool_calls:
            arguments = tc.function.arguments
            try:
                arguments = json.dumps(json.loads(arguments), separators=(",", ":"), sort_keys=True)
            except (TypeError, ValueError):
                pass
            key = (tc.function.name, arguments)
            if key in seen:
                logger.warning("Removed duplicate tool call: %s", tc.function.name)
                continue
            seen.add(key)
            unique.append(tc)
        return unique if len(unique) < len(tool_calls) else tool_calls

    # Distinct ids per assistant turn, in place (policy owner: ``message_sanitization``). Collisions get a
    # deterministic ``<id>_d<n>`` suffix — never uuid4, for prompt-cache prefix stability.
    _uniquify_tool_call_ids = staticmethod(_sanitize_uniquify_tool_call_ids)

    _repair_tool_call = _forward("agent.agent_runtime_helpers", "repair_tool_call")
    _invalidate_system_prompt = _forward("agent.system_prompt", "invalidate_system_prompt")

    # Codex Responses id policy (agent.codex_responses_adapter): deterministic call ids when the API omits one
    # (random UUIDs would break the provider prompt cache), split stored ids, derive valid ``fc_`` ids.
    _deterministic_call_id = staticmethod(_codex_deterministic_call_id)
    _split_responses_tool_id = staticmethod(_codex_split_responses_tool_id)
    _derive_responses_function_call_id = staticmethod(_codex_derive_responses_function_call_id)

    _interruptible_api_call = _forward("agent.chat_completion_helpers", "interruptible_api_call")
    _interruptible_streaming_api_call = _forward("agent.chat_completion_helpers", "interruptible_streaming_api_call")
    _try_activate_fallback = _forward("agent.chat_completion_helpers", "try_activate_fallback")

    def _has_pending_fallback(self) -> bool:
        """Whether a fallback provider remains (mirrors ``try_activate_fallback``'s guard) — gates the
        "trying fallback..." status so we never announce one that won't be attempted.

        See #17446.
        """
        return getattr(self, "_fallback_index", 0) < len(getattr(self, "_fallback_chain", None) or [])

    _restore_primary_runtime = _forward("agent.agent_runtime_helpers", "restore_primary_runtime")
    _try_recover_primary_transport = _forward("agent.agent_runtime_helpers", "try_recover_primary_transport")
    _build_api_kwargs = _forward("agent.chat_completion_helpers", "build_api_kwargs")

    def _set_tool_guardrail_halt(self, decision: ToolGuardrailDecision) -> None:
        """Record the first guardrail decision that should stop this turn."""
        if decision.should_halt and self._tool_guardrail_halt_decision is None:
            self._tool_guardrail_halt_decision = decision

    def _toolguard_controlled_halt_response(self, decision: ToolGuardrailDecision) -> str:
        return (
            f"I stopped retrying {decision.tool_name or 'a tool'} because it hit the tool-call guardrail "
            f"({decision.code}) after {decision.count} repeated non-progressing "
            "attempts. The last tool result explains the blocker; the next step is "
            "to change strategy instead of repeating the same call."
        )

    def _append_guardrail_observation(self, tool_name: str, function_args: dict, function_result: str, *,
                                      failed: bool, tool_call_id: str = "") -> str:
        decision = self._tool_guardrails.after_call(tool_name, function_args, function_result, failed=failed)
        # Identical-call stall guards observe the RAW result (before the per-call loop suffix) and are applied
        # at result construction so tool results stay append-only / cache-safe.
        stall_notice = result_stub = None
        if self._stall_guards_enabled():
            try:
                observation = self._tool_guardrails.observe_call(
                    tool_name, function_args, function_result if isinstance(function_result, str) else None,
                    tool_call_id=tool_call_id, failed=failed,
                )
                stall_notice, result_stub = observation.notice, observation.stub
            except Exception as exc:
                logger.debug("stall-guard identical-call observation failed: %s", exc)
        # Result-reference stubbing: a 2nd+ identical call with a byte-identical FRESH result enters
        # context as a short stub. Not a cache — the tool ran; only plain-string results are stubbed.
        if result_stub and isinstance(function_result, str):
            function_result = result_stub
        if decision.action in {"warn", "halt"}:
            function_result = append_toolguard_guidance(function_result, decision)
        if decision.should_halt:
            self._set_tool_guardrail_halt(decision)
        else:
            # observe_call may have raised the identical-call streak halt (hard_stop_enabled, tool-agnostic).
            streak_halt = self._tool_guardrails.halt_decision
            if streak_halt is not None and streak_halt.code == "identical_call_streak_halt":
                function_result = append_toolguard_guidance(function_result, streak_halt)
                self._set_tool_guardrail_halt(streak_halt)
        if stall_notice:
            function_result = (function_result or "") + "\n\n" + stall_notice
        return function_result

    def _stall_guards_enabled(self) -> bool:
        """Config gate for the runtime anti-stall guards (agent.stall_guards)."""
        return bool(getattr(self, "_stall_guards", True))

    def _guardrail_block_result(self, decision: ToolGuardrailDecision) -> str:
        self._set_tool_guardrail_halt(decision)
        return toolguard_synthetic_result(decision)

    def _execute_tool_calls(self, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0) -> None:
        """Execute the assistant's tool calls and append results to ``messages``.

        The segment planner splits the batch into runs of parallel-safe calls (read-only, non-overlapping file
        targets, opted-in MCP) separated by sequential barriers, run in emission order.
        """
        tool_calls = assistant_message.tool_calls
        args = (assistant_message, messages, effective_task_id, api_call_count)
        self._executing_tools = True  # allow _vprint during tool execution even with stream consumers
        try:
            if len(tool_calls) <= 1:
                return self._execute_tool_calls_sequential(*args)

            from agent.tool_dispatch_helpers import _plan_tool_batch_segments
            active_env = get_active_env(effective_task_id)
            exec_cwd = Path(active_env.cwd) if active_env is not None and active_env.cwd else None
            segments = _plan_tool_batch_segments(tool_calls, execution_cwd=exec_cwd)
            if len(segments) == 1:
                run = self._execute_tool_calls_concurrent if segments[0][0] == "parallel" else self._execute_tool_calls_sequential
                return run(*args)
            from agent.tool_executor import execute_tool_calls_segmented
            return execute_tool_calls_segmented(self, *args, segments=segments)
        finally:
            self._executing_tools = False

    def _dispatch_delegate_agent(self, function_args: dict) -> str:
        """Single call site for delegate_agent dispatch.

        New DELEGATE_TASK_SCHEMA fields only need to be added here to reach all
        invocation paths (concurrent, sequential, inline).
        """
        from tools.delegate_tool import (
            _strip_model_hidden_task_fields,
            delegate_agent as _delegate_agent,
        )
        # Uniform delegation lifecycle: an explicit `background` argument
        # passes through exactly as the model wrote it. Omitted is forwarded
        # as None (NOT defaulted to False) — the handler resolves it through
        # the one shared capability-aware default: detached where the session
        # can receive a late completion, blocking otherwise.
        return _delegate_agent(
            goal=function_args.get("goal"),
            context=function_args.get("context"),
            tasks=_strip_model_hidden_task_fields(function_args.get("tasks")),
            max_iterations=function_args.get("max_iterations"),
            role=function_args.get("role"),
            background=function_args.get("background"),
            action=function_args.get("action"),
            subagent_id=function_args.get("subagent_id"),
            message=function_args.get("message"),
            parent_agent=self,
        )

    # Upstream's pre-rename alias (inline_tool_executors/tool_executor still call this spelling).
    _dispatch_delegate_task = _dispatch_delegate_agent

    _invoke_tool = _forward("agent.agent_runtime_helpers", "invoke_tool")

    @staticmethod
    def _wrap_verbose(label: str, text: str, indent: str = "     ") -> str:
        """Word-wrap verbose tool output to the terminal width (each existing line separately), continuation
        lines indented."""
        import shutil, textwrap
        wrap_width = max(40, shutil.get_terminal_size((120, 24)).columns - len(indent))
        out_lines: list[str] = []
        for raw_line in text.split("\n"):
            if len(raw_line) <= wrap_width:
                out_lines.append(raw_line)
            else:
                out_lines.extend(textwrap.wrap(raw_line, width=wrap_width, break_long_words=True, break_on_hyphens=False) or [raw_line])
        return f"{indent}{label}" + ("\n" + indent).join(out_lines)

    _execute_tool_calls_concurrent = _forward("agent.tool_executor", "execute_tool_calls_concurrent")
    _execute_tool_calls_sequential = _forward("agent.tool_executor", "execute_tool_calls_sequential")
    _handle_max_iterations = _forward("agent.chat_completion_helpers", "handle_max_iterations")

    def _conversation_root_id(self) -> Optional[str]:
        """Resolve the stable conversation id for Portal usage attribution.

        Returns the session-lineage ROOT id rather than the current segment
        id, so one user-facing conversation keeps a single ``conversation=``
        tag across context-compression rotation (`/new` starts a genuinely
        new lineage). Delegate subagents resolve through their
        ``_parent_session_id`` so an entire delegation tree tags as the
        parent conversation.

        Best-effort: falls back to the raw session id when the session DB
        is unavailable or the lineage walk fails.
        """
        sid = getattr(self, "session_id", None)
        if not sid:
            return None
        # Subagents may not have a DB row yet on their first turn; walking
        # from the parent id still lands on the right root.
        start = getattr(self, "_parent_session_id", None) or sid
        db = getattr(self, "_session_db", None)
        if db is not None:
            try:
                root = db.get_conversation_root(start)
                if root:
                    return root
            except Exception:
                logger.debug("Conversation root lineage walk failed", exc_info=True)
        return start

    def run_conversation(
        self,
        user_message: Any,
        system_message: str = None,
        conversation_history: List[Dict[str, Any]] = None,
        task_id: str = None,
        stream_callback: Optional[callable] = None,
        persist_user_message: Optional[Any] = None,
        persist_user_timestamp: Optional[float] = None,
        persist_user_display_kind: Optional[str] = None,
        persist_user_display_metadata: Optional[Dict[str, Any]] = None,
        persist_user_platform_id: Optional[str] = None,
        moa_config: Optional[dict[str, Any]] = None,
        continue_interrupted_turn: bool = False,
        turn_author: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Forwarder — see ``agent.conversation_loop.run_conversation``."""
        # A review deliberately shares this agent's session_id for prompt-cache
        # parity. Fence review startup or interrupt an admitted request, then
        # await that request's exit before opening any live-turn Relay or task
        # instrumentation for the same session. Foreground priority is retained
        # if the review does not acknowledge within the bounded deadline (#84423).
        from agent.background_review import cancel_background_review_for_live_turn

        cancel_background_review_for_live_turn(self)

        # Turn liveness for the deferred-review idle queue: a queued review
        # must not dispatch into the settle gap between two quick prompts.
        # Marked inside the try below so the balancing note_turn_finished in
        # its finally covers every exit; the actual start-mark happens as the
        # first statement of the try.
        from agent.review_idle_queue import QUEUE as _review_queue

        from agent.aux_accounting import (
            reset_accounting_context,
            set_accounting_context,
        )
        from agent import relay_runtime
        from agent.conversation_loop import run_conversation
        from agent.portal_tags import (
            reset_affinity_scope,
            reset_conversation_context,
            set_affinity_scope,
            set_conversation_context,
        )
        from agent.prompt_cache_scope import declared_conversation_scope_safe
        from hermes_cli.observability.relay_shared_metrics import (
            finish_task_run,
            start_task_run,
        )
        from agent.subagent_lifecycle import bind_subagent_parent
        effective_task_id = task_id or str(uuid.uuid4())
        session_id = str(getattr(self, "session_id", None) or "")
        task_context = {
            "session_id": session_id,
            "task_id": effective_task_id,
            "platform": getattr(self, "platform", None) or "",
        }
        relay_turn_id = (
            f"{session_id or 'session'}:{effective_task_id}:{uuid.uuid4().hex[:8]}"
        )
        self._relay_pending_turn_id = relay_turn_id
        relay_parent_session_id = (
            str(getattr(self, "_parent_session_id", None) or "")
            if task_context["platform"] == "subagent"
            else ""
        )
        relay_lease = None
        relay_turn = None
        durable_turn_lease = None
        durable_turn_lease_stop = None
        durable_turn_lease_refresh = None
        durable_turn_liveness_watchdog = None
        # Handles on the shared periodic scheduler thread (one per process,
        # agent/periodic_scheduler.py) instead of 1-2 daemon threads per turn.
        durable_turn_timer_handles = []
        durable_turn_lease_activity_lock = threading.Lock()
        durable_turn_lease_turn_active = False
        durable_turn_lease_interrupt_message = None
        token = None
        # Initialized alongside `token`: the turn-lease timeout/interrupt
        # early returns leave the try block before set_affinity_scope() runs,
        # and the finally reads this name unconditionally (UnboundLocalError
        # otherwise — the 4 red cross-process lease tests on PR #97158).
        affinity_token = None
        acct_token = None
        task_started = False
        task_finished = False
        relay_outcome = "failed"

        def _stop_durable_turn_lease_refresher() -> None:
            nonlocal durable_turn_lease_turn_active
            with durable_turn_lease_activity_lock:
                durable_turn_lease_turn_active = False
                if durable_turn_lease_stop is not None:
                    durable_turn_lease_stop.set()

        def _clear_durable_turn_lease_interrupt() -> None:
            """Clear only the interrupt admitted by this turn's refresher."""
            message = durable_turn_lease_interrupt_message
            if not message:
                return

            def _clear_if_owned() -> None:
                if getattr(self, "_interrupt_message", None) != message:
                    return
                self._interrupt_requested = False
                self._interrupt_message = None
                getattr(self, "_hard_interrupt_requested", threading.Event()).clear()
                self._interrupt_thread_signal_pending = False
                if self._execution_thread_id is not None:
                    _set_interrupt(False, self._execution_thread_id)

            redirect_lock = getattr(self, "_pending_redirect_lock", None)
            if redirect_lock is None:
                _clear_if_owned()
            else:
                with redirect_lock:
                    _clear_if_owned()

        try:
            _review_queue.note_turn_started()
            # Serialize the full load -> run -> flush region across Hermes
            # processes. Gateway's asyncio lease closes alias routing inside one
            # process; this durable lease covers Desktop, CLI resume, gateway,
            # and background delivery processes sharing state.db (#84234).
            _turn_db = getattr(self, "_session_db", None)
            _durable_session_exists = False
            if _turn_db is not None and session_id:
                try:
                    _durable_session_exists = _turn_db.get_session(session_id) is not None
                except Exception:
                    # A locked / non-WAL read is not proof the row is absent.
                    # Treating probe failure as "fresh session" skipped the
                    # lease this block exists to take and ran fail-open on
                    # the exact contention point (#84234). Acquire (or fail
                    # closed if acquire itself cannot) rather than start
                    # load/run/flush unsynchronized. get_session returns
                    # None — it does not raise — when the row is missing.
                    logger.warning(
                        "Could not check durable session before turn lease; "
                        "will acquire rather than run without serialization",
                        exc_info=True,
                    )
                    _durable_session_exists = True
            if (
                _turn_db is not None
                and session_id
                and not getattr(self, "_persist_disabled", False)
                # A fresh session id is process-unique and has no durable
                # transcript to race over. More importantly, subagent/new-turn
                # callers may intentionally supply an in-memory seed before the
                # row exists; reloading an absent row would erase that seed.
                and _durable_session_exists
                # Test doubles and third-party DB shims may accept arbitrary
                # MagicMock attributes without implementing the protocol. Check
                # the concrete type so only real implementations opt in.
                and callable(
                    getattr(type(_turn_db), "acquire_session_turn_lease", None)
                )
            ):
                # Resumed agents also defer their create check until the turn
                # prologue. We just proved this row exists, so suppress the
                # redundant create attempt after acquiring it.
                self._session_db_created = True
                _durable_holder = (
                    f"pid={os.getpid()}:turn={relay_turn_id}:platform="
                    f"{task_context['platform'] or 'unknown'}"
                )
                _lease_ttl = 300.0
                _lease_waited = False

                def _on_session_turn_lease_wait(elapsed: float) -> None:
                    nonlocal _lease_waited
                    _lease_waited = True
                    if elapsed < 1.0:
                        self._emit_status(
                            "⏳ Another Hermes process is using this session; "
                            "waiting for it to finish before starting your turn..."
                        )
                    else:
                        self._emit_status(
                            "⏳ Still waiting for the other Hermes process on "
                            f"this session ({int(elapsed)}s)..."
                        )

                if not _turn_db.acquire_session_turn_lease(
                    session_id,
                    _durable_holder,
                    ttl_seconds=_lease_ttl,
                    wait_seconds=1800.0,
                    on_wait=_on_session_turn_lease_wait,
                    should_abort=lambda: getattr(self, "_interrupt_requested", False),
                ):
                    if getattr(self, "_interrupt_requested", False):
                        logger.info(
                            "session turn lease wait aborted by interrupt: %s",
                            session_id,
                        )
                        relay_outcome = "cancelled"
                        interrupt_msg = (
                            "Stopped waiting for another Hermes process on "
                            "this session. Your message was not processed."
                        )
                        interrupt_result = {
                            "final_response": interrupt_msg,
                            "messages": list(conversation_history or []),
                            "api_calls": 0,
                            "completed": False,
                            "interrupted": True,
                        }
                        interrupt_message = getattr(
                            self, "_interrupt_message", None
                        )
                        if interrupt_message:
                            interrupt_result["interrupt_message"] = (
                                interrupt_message
                            )
                        # Conversation-loop finalizer never runs on this
                        # early return. Clear so a cached agent cannot
                        # fail-close the next turn as interrupted.
                        try:
                            self.clear_interrupt()
                        except Exception:
                            self._interrupt_requested = False
                            self._interrupt_message = None
                        return interrupt_result
                    # Fail closed like gateway TurnLeaseTimeoutError: do not
                    # enter load/run/flush, and surface a resend notice instead
                    # of a bare TimeoutError that looks like a hang.
                    timeout_msg = (
                        "⏳ Another Hermes process kept this session busy too "
                        "long. Your message was not processed - wait for the "
                        "other process to finish, then send it again."
                    )
                    logger.error(
                        "session turn lease wait timed out for %s",
                        session_id,
                    )
                    try:
                        self._emit_warning(timeout_msg)
                    except Exception:
                        logger.debug(
                            "Failed to emit session turn lease timeout warning",
                            exc_info=True,
                        )
                    relay_outcome = "timed_out"
                    return {
                        "final_response": timeout_msg,
                        "messages": list(conversation_history or []),
                        "api_calls": 0,
                        "completed": False,
                        "failed": True,
                        "error": f"session_turn_lease_timeout:{session_id}",
                    }

                # Assign only after admission so finally release cannot target a
                # holder string that never owned the row. Persist paths read
                # the agent attr so a late flush after reclaim is fenced in
                # the same SQLite write transaction as the transcript insert.
                durable_turn_lease = _durable_holder
                self._active_session_turn_lease_holder = _durable_holder
                self._active_session_turn_lease_ttl_seconds = _lease_ttl
                if _lease_waited:
                    self._emit_status(
                        "Session is free; loading the latest transcript..."
                    )

                # The holder may have compressed and rotated the session while
                # this process waited. Resolve and reload only AFTER admission;
                # a caller-provided in-memory snapshot is necessarily stale.
                # Skip when acquisition was immediate — no other process held
                # the lease, so the in-memory history is current and reloading
                # would only cause an unnecessary prompt cache miss.
                if _lease_waited:
                    latest_session_id = _turn_db.resolve_resume_session_id(session_id)
                    if latest_session_id:
                        self.session_id = latest_session_id
                        task_context["session_id"] = latest_session_id
                    conversation_history = _turn_db.get_messages_as_conversation(
                        self.session_id,
                        repair_alternation=True,
                        include_row_ids=True,
                    )

                # Long model/tool/compression turns outlive a fixed TTL. Refresh
                # on the shared periodic scheduler; holder-qualified UPDATE and DELETE fence a
                # late refresher/release from a successor lease.
                durable_turn_lease_stop = threading.Event()
                _lease_refresh_interval = float(
                    getattr(self, "_session_turn_lease_refresh_interval", 60.0)
                )

                # ── Turn liveness watchdog (#95548) ─────────────────────
                # The durable lease refresher keeps the lease alive for as
                # long as the turn runs, so lease renewal is NOT evidence of
                # progress. A turn that stalls silently (observed #95548: no
                # tool execution, no API call, no persisted message for 9+
                # minutes after a slow model response + desktop WS
                # disconnect) would otherwise renew its lease forever, look
                # "active", and never be force-aborted.
                #
                # The watchdog policy (config resolution, sampling state
                # machine, polling mechanics) lives in agent/turn_liveness.py;
                # this block is only the integration seam: resolve the
                # config.yaml settings, wire the commit/deactivate callbacks
                # that own turn-lease state, and schedule the poll.
                try:
                    from hermes_cli.config import (
                        load_config_readonly as _liveness_load_config,
                    )
                    _liveness_config = _liveness_load_config() or {}
                except Exception:
                    _liveness_config = {}
                from agent import turn_liveness

                _liveness_timeout, _liveness_poll = (
                    turn_liveness.resolve_turn_liveness_settings(_liveness_config)
                )

                def _interrupt_turn(message: str) -> None:
                    # Lease-loss interrupts fire UNCONDITIONALLY (no
                    # require_generation claim): losing the durable lease
                    # means this process no longer owns the session, so
                    # the turn must stop regardless of activity-clock
                    # progress. The generation-claim machinery is the
                    # liveness watchdog's only — its stalls can be
                    # spuriously stale, a lost lease cannot.
                    nonlocal durable_turn_lease_interrupt_message
                    with durable_turn_lease_activity_lock:
                        if (
                            durable_turn_lease_stop.is_set()
                            or not durable_turn_lease_turn_active
                        ):
                            return
                        durable_turn_lease_interrupt_message = message
                        try:
                            self.interrupt(message, hard_cancel=True)
                        except Exception:
                            self._interrupt_requested = True
                            self._interrupt_message = message

                def _commit_turn_liveness_abort(
                    snapshot: "turn_liveness.ActivitySnapshot",
                    message: str,
                ) -> bool:
                    """Commit point for the watchdog's stall observation.

                    Revalidates the observed ``(generation, timestamp)`` pair
                    under the SAME lock ``_touch_activity`` stamps the clock
                    with, so a turn that resumed while the stall was being
                    logged/emitted is never hard-cancelled (#95663 review):
                    it continues and its lease keeps renewing. Returns False
                    when the observation is stale (watchdog keeps sampling)
                    or the turn is already winding down.

                    Round-3 (#95663): the revalidated generation is carried
                    into the interrupt path as a claim
                    (``require_generation``); ``interrupt`` reserves it,
                    consumes it and publishes the first interrupt state in
                    ONE activity-lock critical section (round-6) and
                    abandons the abort when it went stale.

                    Round-4 (#95663): if ``interrupt`` raises, the abort
                    declines FAIL-CLOSED — the exceptional path must not
                    convert the inability to validate/publish the claim
                    through the normal path into unconditional interrupt
                    authority. No interrupt state is mutated here; the
                    watchdog keeps sampling while the turn (which may have
                    resumed) continues.
                    """
                    nonlocal durable_turn_lease_interrupt_message
                    with self._liveness_activity_lock():
                        current_generation = getattr(
                            self, "_turn_liveness_activity_generation", 0
                        )
                        if (
                            current_generation,
                            getattr(self, "_last_activity_ts", None),
                        ) != (snapshot.generation, snapshot.activity_ts):
                            return False
                    with durable_turn_lease_activity_lock:
                        if (
                            durable_turn_lease_stop.is_set()
                            or not durable_turn_lease_turn_active
                        ):
                            return False
                    try:
                        published = self.interrupt(
                            message,
                            hard_cancel=True,
                            require_generation=current_generation,
                        )
                    except Exception:
                        # Round-4 (#95663): fail closed. An exceptional
                        # interrupt path must not turn the inability to
                        # validate/publish the generation claim into
                        # unconditional abort authority — declining keeps
                        # the watchdog sampling while the turn (which may
                        # have resumed) continues.
                        logger.debug(
                            "Turn liveness abort interrupt raised; "
                            "declining the abort",
                            exc_info=True,
                        )
                        published = False
                    if published is False:
                        # The generation claim went stale between the
                        # revalidation above and the hammer: real progress
                        # landed in the window, so the abort abandons itself
                        # and the watchdog keeps sampling while the turn
                        # (and its lease) continue.
                        return False
                    with durable_turn_lease_activity_lock:
                        durable_turn_lease_interrupt_message = message
                    return True

                def _deactivate_turn_after_liveness_abort() -> None:
                    """Stop lease renewal after a committed liveness abort.

                    A wedge the hard interrupt cannot unwind must not keep
                    the lease alive forever (the issue's "lease keeps
                    renewing" masking); TTL expiry then lets stale-turn
                    cleanup reclaim the row.
                    """
                    nonlocal durable_turn_lease_turn_active
                    with durable_turn_lease_activity_lock:
                        durable_turn_lease_stop.set()
                        durable_turn_lease_turn_active = False

                def _turn_is_active() -> bool:
                    with durable_turn_lease_activity_lock:
                        return durable_turn_lease_turn_active

                def _refresh_durable_turn_lease():
                    # One periodic tick on the shared scheduler thread every
                    # _lease_refresh_interval; returning False stops it.
                    if durable_turn_lease_stop.is_set():
                        return False
                    try:
                        if not _turn_db.refresh_session_turn_lease(
                            getattr(self, "session_id", None) or session_id,
                            durable_turn_lease,
                            ttl_seconds=_lease_ttl,
                        ):
                            # finally sets the stop event then releases.
                            # A late holder-fenced miss after that cancel
                            # wait must not hard-interrupt the next turn.
                            if durable_turn_lease_stop.is_set():
                                return False
                            logger.error(
                                "Lost session turn lease while turn is active: %s",
                                getattr(self, "session_id", None) or session_id,
                            )
                            _interrupt_turn(
                                "Session turn lease lost; stopping to protect "
                                "the transcript."
                            )
                            return False
                    except Exception:
                        if durable_turn_lease_stop.is_set():
                            return False
                        logger.warning(
                            "Failed to refresh session turn lease: %s",
                            getattr(self, "session_id", None) or session_id,
                            exc_info=True,
                        )
                        _interrupt_turn(
                            "Session turn lease could not be refreshed; "
                            "stopping to protect the transcript."
                        )
                        return False

                durable_turn_lease_refresh = _refresh_durable_turn_lease
                if _liveness_timeout is not None:
                    durable_turn_liveness_watchdog = turn_liveness.TurnLivenessWatchdog(
                        self,
                        session_id=getattr(self, "session_id", None) or session_id,
                        timeout_s=_liveness_timeout,
                        poll_s=_liveness_poll,
                        stop_event=durable_turn_lease_stop,
                        activity_lock=self._liveness_activity_lock(),
                        is_turn_active=_turn_is_active,
                        commit_abort=_commit_turn_liveness_abort,
                        deactivate_turn=_deactivate_turn_after_liveness_abort,
                    )


            relay_lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
                profile_key=relay_runtime.current_profile_key(),
                session_id=task_context["session_id"],
                platform=task_context["platform"],
                parent_session_id=relay_parent_session_id,
                model=str(getattr(self, "model", None) or ""),
            )
            relay_turn = relay_runtime.SESSION_COORDINATOR.begin_turn(
                relay_lease,
                turn_id=relay_turn_id,
                task_id=effective_task_id,
            )
            # Keep existing tests and external relay-runtime shims that return
            # a minimal turn object compatible with the new opt-out flag.
            if getattr(relay_turn, "relay_enabled", True):
                start_task_run(
                    **task_context,
                    parent_session_id=getattr(self, "_parent_session_id", None) or "",
                )
                task_started = True
            # Publish the conversation id for ambient Nous Portal tagging. Every
            # LLM call made inside this turn — main loop, compression, vision,
            # web_extract, session_search, MoA slots, background-review forks
            # (which copy this Context into their thread) — inherits the
            # ``conversation=<root>`` tag with zero per-call-site plumbing.
            token = set_conversation_context(self._conversation_root_id())
            # Routing/affinity scope for the same turn — the conversation the
            # HOST declared, when it declared one. Providers fall back to the
            # attribution id above when it is unset, so this changes nothing
            # for a host that keeps one session id per conversation (#96811).
            affinity_token = set_affinity_scope(
                declared_conversation_scope_safe(self)
            )
            # Publish the session accounting handles the same way so auxiliary
            # calls record their token usage into session_model_usage (task
            # dimension) — the fix for aux spend being invisible in analytics
            # (issue #23270).
            acct_token = set_accounting_context(
                getattr(self, "_session_db", None),
                getattr(self, "session_id", None),
            )
            from agent.auxiliary_client import scoped_runtime_main

            # The outer token restores the caller's Context even though turn setup
            # replaces the value with the live runtime after fallback restoration.
            # Keep the scope local instead of storing ContextVar tokens on the agent,
            # which may be observed from another thread.
            from tools.tool_status import tool_status_scope

            with bind_subagent_parent(self), tool_status_scope(getattr(self, "_emit_status", None)), scoped_runtime_main({}):
                try:
                    if durable_turn_lease_refresh is not None:
                        with durable_turn_lease_activity_lock:
                            durable_turn_lease_turn_active = True
                        # Stamp the activity clock at turn entry (#95663
                        # review): a real agent keeps ``_last_activity_ts``
                        # across turns (idle time between turns is normal —
                        # ``_reset_activity_labels_after_turn`` preserves
                        # it by design), so without this stamp the liveness
                        # watchdog would measure idle from the PREVIOUS
                        # turn and force-abort a just-started turn on its
                        # first poll whenever the agent had been idle longer
                        # than the watchdog bound.
                        self._touch_activity("starting new turn")
                        from agent.periodic_scheduler import schedule as _schedule_periodic

                        durable_turn_timer_handles.append(
                            _schedule_periodic(
                                durable_turn_lease_refresh, _lease_refresh_interval
                            )
                        )
                        if durable_turn_liveness_watchdog is not None:
                            durable_turn_timer_handles.append(
                                durable_turn_liveness_watchdog.schedule()
                            )
                    result = run_conversation(
                        self,
                        user_message,
                        system_message,
                        conversation_history,
                        effective_task_id,
                        stream_callback,
                        persist_user_message,
                        persist_user_timestamp=persist_user_timestamp,
                        persist_user_display_kind=persist_user_display_kind,
                        persist_user_display_metadata=persist_user_display_metadata,
                        persist_user_platform_id=persist_user_platform_id,
                        moa_config=moa_config,
                        continue_interrupted_turn=continue_interrupted_turn,
                        turn_author=turn_author,
                    )
                finally:
                    # The lease remains held through relay/task finalization, but
                    # those post-loop steps must not receive a late refresh
                    # interrupt that poisons the next turn on a cached agent.
                    _stop_durable_turn_lease_refresher()
                    # Interrupt clear is deferred to after thread join in the
                    # outer finally: a refresher firing between stop and join
                    # would otherwise set an interrupt that survives the clear.
            terminal = result if isinstance(result, dict) else {}
            if terminal.get("interrupted") is True:
                relay_outcome = "cancelled"
            elif terminal.get("failed") is True:
                relay_outcome = "failed"
            else:
                relay_outcome = "success"
            relay_runtime.SESSION_COORDINATOR.finish_logical_calls(
                relay_turn,
                outcome=relay_outcome,
            )
            if task_started:
                task_finished = True
                finish_task_run(**task_context, result=result)
            # Footer timing breakdown: early-exit paths (max-iteration,
            # guardrail halt) return a result dict without api_time/tool_time.
            # Copy the per-turn accumulators so the gateway footer always
            # surfaces real values regardless of which return path executed.
            # setdefault: values already set by finalize_turn win. Only inject
            # when real time was measured — zero-valued additions would break
            # the legacy result shape for callers that never timed anything.
            if isinstance(result, dict):
                _api_t = getattr(self, "_turn_api_time", 0.0) or 0.0
                _tool_t = getattr(self, "_turn_tool_time", 0.0) or 0.0
                if _api_t or _tool_t:
                    result.setdefault("api_time", _api_t)
                    result.setdefault("tool_time", _tool_t)
            return result
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, InterruptedError)) or (
                type(exc).__name__ == "CancelledError"
            ):
                relay_outcome = "cancelled"
            elif isinstance(exc, TimeoutError):
                relay_outcome = "timed_out"
            if relay_turn is not None:
                relay_runtime.SESSION_COORDINATOR.finish_logical_calls(
                    relay_turn,
                    outcome=relay_outcome,
                )
            if task_started and not task_finished:
                task_finished = True
                finish_task_run(**task_context, error=exc)
            raise
        finally:
            try:
                if relay_turn is not None:
                    relay_runtime.SESSION_COORDINATOR.end_turn(
                        relay_turn,
                        outcome=relay_outcome,
                    )
            finally:
                try:
                    if relay_lease is not None:
                        relay_runtime.SESSION_COORDINATOR.release_conversation(
                            relay_lease
                        )
                finally:
                    _stop_durable_turn_lease_refresher()
                    # wait=1.0 mirrors the old thread join(timeout=1.0): an
                    # in-flight tick on the scheduler thread finishes first.
                    for _durable_handle in durable_turn_timer_handles:
                        _durable_handle.cancel(wait=1.0)
                    # Clear any interrupt the refresher may have fired between
                    # the inner stop and this cancel. Must run AFTER it so a
                    # late interrupt does not survive into the next turn.
                    _clear_durable_turn_lease_interrupt()
                    if durable_turn_lease is not None:
                        try:
                            _turn_db.release_session_turn_lease(
                                session_id, durable_turn_lease
                            )
                        except Exception:
                            logger.error(
                                "Failed to release session turn lease: %s",
                                session_id,
                                exc_info=True,
                            )
                        if (
                            getattr(self, "_active_session_turn_lease_holder", None)
                            == durable_turn_lease
                        ):
                            self._active_session_turn_lease_holder = None
                            self._active_session_turn_lease_ttl_seconds = None
                    # Always clear mid-turn labels when the turn exits — including
                    # interrupted early returns that skip finalize_turn. Keep ts.
                    try:
                        self._reset_activity_labels_after_turn()
                    except Exception:
                        pass
                    if getattr(self, "_relay_pending_turn_id", None) == relay_turn_id:
                        self._relay_pending_turn_id = None
                    if acct_token is not None:
                        reset_accounting_context(acct_token)
                    if token is not None:
                        reset_conversation_context(token)
                    if affinity_token is not None:
                        reset_affinity_scope(affinity_token)
                    # Balance the note_turn_started above — every exit path
                    # lands here, so the idle queue's live-turn count cannot
                    # leak upward and starve deferred reviews.
                    try:
                        _review_queue.note_turn_finished()
                    except Exception:
                        pass



_BASIC_TOOLSETS = {"web", "terminal", "vision", "creative", "reasoning"}
_COMPOSITE_TOOLSETS = {"research", "development", "analysis", "content_creation", "full_stack"}
_LIST_TOOLS_USAGE = """
💡 Usage Examples:
  # Use predefined toolsets
  python run_agent.py --enabled_toolsets=research --query='search for Python news'
  python run_agent.py --enabled_toolsets=development --query='debug this code'
  python run_agent.py --enabled_toolsets=safe --query='analyze without terminal'

  # Combine multiple toolsets
  python run_agent.py --enabled_toolsets=web,vision --query='analyze website'

  # Disable toolsets
  python run_agent.py --disabled_toolsets=terminal --query='no command execution'

  # Run with trajectory saving enabled
  python run_agent.py --save_trajectories --query='your question here'"""




def _print_tool_listing() -> None:
    """``--list_tools``: print toolsets (basic / composite / scenario / legacy), every tool, and usage examples."""
    from model_tools import get_all_tool_names, get_available_toolsets
    from toolsets import get_all_toolsets, get_toolset_info

    print("📋 Available Tools & Toolsets:")
    print("-" * 50)
    print("\n🎯 Predefined Toolsets (New System):")
    print("-" * 40)
    basic_toolsets, composite_toolsets, scenario_toolsets = [], [], []
    for name in get_all_toolsets():
        info = get_toolset_info(name)
        if info:
            bucket = basic_toolsets if name in _BASIC_TOOLSETS else composite_toolsets if name in _COMPOSITE_TOOLSETS else scenario_toolsets
            bucket.append((name, info))
    print("\n📌 Basic Toolsets:")
    for name, info in basic_toolsets:
        print(f"  • {name:15} - {info['description']}")
        print(f"    Tools: {', '.join(info['resolved_tools']) if info['resolved_tools'] else 'none'}")
    print("\n📂 Composite Toolsets (built from other toolsets):")
    for name, info in composite_toolsets:
        print(f"  • {name:15} - {info['description']}")
        print(f"    Includes: {', '.join(info['includes']) if info['includes'] else 'none'}")
        print(f"    Total tools: {info['tool_count']}")
    print("\n🎭 Scenario-Specific Toolsets:")
    for name, info in scenario_toolsets:
        print(f"  • {name:20} - {info['description']}")
        print(f"    Total tools: {info['tool_count']}")
    print("\n📦 Legacy Toolsets (for backward compatibility):")
    for name, info in get_available_toolsets().items():
        print(f"  {'✅' if info['available'] else '❌'} {name}: {info['description']}")
        if not info["available"]:
            print(f"    Requirements: {', '.join(info['requirements'])}")
    all_tools = get_all_tool_names()
    print(f"\n🔧 Individual Tools ({len(all_tools)} available):")
    for tool_name in sorted(all_tools):
        print(f"  📌 {tool_name} (from {get_toolset_for_tool(tool_name)})")
    print(_LIST_TOOLS_USAGE)


def _parse_toolset_arg(raw: Optional[str], label: str) -> Optional[List[str]]:
    """Comma-separated toolset CLI arg → list (echoed), or None when absent."""
    if not raw:
        return None
    names = [t.strip() for t in raw.split(",")]
    print(f"{label}: {names}")
    return names


def _save_sample_trajectory(agent: "AIAgent", result: dict, user_query: str, model: str) -> None:
    """``--save_sample``: write one trajectory (same format as batch_runner) to a UUID-named JSON file."""
    sample_filename = f"sample_{str(uuid.uuid4())[:8]}.json"
    entry = {
        "conversations": agent._convert_to_trajectory_format(result['messages'], user_query, result['completed']),
        "timestamp": datetime.now().isoformat(), "model": model, "completed": result['completed'], "query": user_query,
    }
    try:
        with open(sample_filename, "w", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, indent=2))
        print(f"\n💾 Sample trajectory saved to: {sample_filename}")
    except Exception as e:
        print(f"\n⚠️ Failed to save sample: {e}")


def main(
    query: str = None,
    model: str = "",
    api_key: str = None,
    base_url: str = "",
    max_turns: int = DEFAULT_MAX_TURNS,
    enabled_toolsets: str = None,
    disabled_toolsets: str = None,
    list_tools: bool = False,
    save_trajectories: bool = False,
    save_sample: bool = False,
    verbose: bool = False,
    log_prefix_chars: int = 20
):
    """
    Main function for running the agent directly.

    Args:
        query (str): Natural language query for the agent. Defaults to Python 3.13 example.
        model (str): Model name to use (OpenRouter format: provider/model). Defaults to anthropic/claude-
        sonnet-4.6.
        api_key (str): API key for authentication. Uses OPENROUTER_API_KEY env var if not provided.
        base_url (str): Base URL for the model API. Defaults to https://openrouter.ai/api/v1
        max_turns (int): Maximum number of API call iterations. Defaults to
                         DEFAULT_MAX_TURNS (256); explicit values are used exactly.
        enabled_toolsets (str): Comma-separated list of toolsets to enable. Supports predefined
                              toolsets (e.g., "research", "development", "safe").
                              Multiple toolsets can be combined: "web,vision"
        disabled_toolsets (str): Comma-separated list of toolsets to disable (e.g., "terminal")
        list_tools (bool): Just list available tools and exit
        save_trajectories (bool): Save conversation trajectories to JSONL files (appends to
        trajectory_samples.jsonl). Defaults to False.
        save_sample (bool): Save a single trajectory sample to a UUID-named JSONL file for inspection.
        Defaults to False.
        verbose (bool): Enable verbose logging for debugging. Defaults to False.
        log_prefix_chars (int): Number of characters to show in log previews for tool calls/responses.
        Defaults to 20.

    Toolset Examples:
        - "research": Web search, extract, crawl + vision tools
    """
    print("🤖 AI Agent with Tool Calling")
    print("=" * 50)
    if list_tools:
        return _print_tool_listing()

    enabled_toolsets_list = _parse_toolset_arg(enabled_toolsets, "🎯 Enabled toolsets")
    disabled_toolsets_list = _parse_toolset_arg(disabled_toolsets, "🚫 Disabled toolsets")
    if save_trajectories:
        print("💾 Trajectory saving: ENABLED")
        print("   - Successful conversations → trajectory_samples.jsonl")
        print("   - Failed conversations → failed_trajectories.jsonl")

    try:
        agent = AIAgent(
            base_url=base_url, model=model, api_key=api_key, max_iterations=max_turns,
            enabled_toolsets=enabled_toolsets_list, disabled_toolsets=disabled_toolsets_list,
            save_trajectories=save_trajectories, verbose_logging=verbose, log_prefix_chars=log_prefix_chars,
        )
    except RuntimeError as e:
        print(f"❌ Failed to initialize agent: {e}")
        return

    user_query = query if query is not None else ("Tell me about the latest developments in Python 3.13 and what new features "
                                                  "developers should know about. Please search for current information and try it out.")
    print(f"\n📝 User Query: {user_query}")
    print("\n" + "=" * 50)

    result = agent.run_conversation(user_query)

    print("\n" + "=" * 50 + "\n📋 CONVERSATION SUMMARY\n" + "=" * 50)
    print(f"✅ Completed: {result['completed']}\n📞 API Calls: {result['api_calls']}\n💬 Messages: {len(result['messages'])}")
    if result['final_response']:
        print("\n🎯 FINAL RESPONSE:\n" + "-" * 30 + "\n" + result['final_response'])
    if save_sample:
        _save_sample_trajectory(agent, result, user_query, model)
    print("\n👋 Agent execution completed!")


if __name__ == "__main__":
    import fire
    fire.Fire(main)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from types import SimpleNamespace  # noqa: F401,E402
import asyncio  # noqa: F401,E402
import base64  # noqa: F401,E402
import copy  # noqa: F401,E402
import hashlib  # noqa: F401,E402
import tempfile  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'COMPRESSED_SUMMARY_METADATA_KEY': ('agent.context_compressor', 'COMPRESSED_SUMMARY_METADATA_KEY'),
    'ContextCompressor': ('agent.context_compressor', 'ContextCompressor'),
    'DEFAULT_AGENT_IDENTITY': ('agent.prompt_builder', 'DEFAULT_AGENT_IDENTITY'),
    'FailoverReason': ('agent.error_classifier', 'FailoverReason'),
    'OpenAI': ('agent.process_bootstrap', 'OpenAI'),
    'atomic_json_write': ('utils', 'atomic_json_write'),
    'build_context_files_prompt': ('agent.prompt_builder', 'build_context_files_prompt'),
    'build_environment_hints': ('agent.prompt_builder', 'build_environment_hints'),
    'build_skills_system_prompt': ('agent.prompt_builder', 'build_skills_system_prompt'),
    'check_toolset_requirements': ('model_tools', 'check_toolset_requirements'),
    'convert_scratchpad_to_think': ('agent.trajectory', 'convert_scratchpad_to_think'),
    'estimate_request_tokens_rough': ('agent.model_metadata', 'estimate_request_tokens_rough'),
    'file_mutation_result_landed': ('agent.tool_result_classification', 'file_mutation_result_landed'),
    'flatten_message_text': ('agent.message_content', 'flatten_message_text'),
    'get_tool_definitions': ('model_tools', 'get_tool_definitions'),
    'handle_function_call': ('model_tools', 'handle_function_call'),
    'is_truthy_value': ('utils', 'is_truthy_value'),
    'jittered_backoff': ('agent.retry_utils', 'jittered_backoff'),
    'load_soul_md': ('agent.prompt_builder', 'load_soul_md'),
    'normalize_usage': ('agent.usage_pricing', 'normalize_usage'),
    'redact_sensitive_text': ('agent.redact', 'redact_sensitive_text'),
    'request_hard_interrupt': ('agent.interrupt_compat', 'request_hard_interrupt'),
    'sanitize_context': ('agent.memory_manager', 'sanitize_context'),
    'user_originated_turn_view': ('agent.context_compressor', 'user_originated_turn_view'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
