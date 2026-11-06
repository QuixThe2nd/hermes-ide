"""Agent cache, session model overrides, turn leases, run generations and conversation-scope reset
for GatewayRunner (MRO mixin). ``gateway.run`` internals are imported lazily inside method bodies
(import cycle), so ``patch("gateway.run.X")`` keeps intercepting them at call time."""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import threading
import time
from contextlib import nullcontext, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agent.interrupt_compat import _accepts_keyword, request_hard_interrupt
from gateway.config import Platform
from gateway.session import SessionSource, build_session_context_prompt
from gateway.run_shutdown import _log_suppressed
from hermes_cli.config import cfg_get
from hermes_cli.local_runtime.endpoint import LLAMACPP_ALIASES

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner  # noqa: F401
    from gateway.run_turn_runner import TurnRunner  # noqa: F401

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")

# Override fields layered onto runtime kwargs when non-None (partial overrides don't clobber defaults).
_OVERRIDE_APPLY_KEYS = (
    "provider", "requested_provider", "api_key", "base_url", "api_mode", "credential_pool", "capabilities", "max_tokens",
)


def _first_agent(entry: Any) -> Any:
    """Unwrap a cache entry (``(agent, sig, ...)`` tuple or bare agent) to its agent."""
    return entry[0] if isinstance(entry, tuple) and entry else entry


def _tuple_agent(entry: Any) -> Any:
    """Agent of a ``(agent, sig, ...)`` cache tuple; None for any other entry shape."""
    return entry[0] if isinstance(entry, tuple) and entry else None


class GatewayAgentCacheMixin:
    """Agent cache, session model overrides, turn leases, run generations and conversation-scope reset for GatewayRunner."""

    @classmethod
    def _extract_cache_busting_config(cls, user_config: dict | None) -> dict:
        """Pull values that must bust the cached agent.

        Returns a flat dict keyed by 'section.key'.  Missing config keys and
        non-dict sections yield None values, which still contribute to the
        signature (so 'absent' vs 'present-and-null' differ).

        The live tool registry generation is included too.  MCP reloads and
        dynamic MCP tool-list changes mutate the registry without necessarily
        changing config.yaml.  Cached AIAgent instances freeze their tool
        schemas at construction time, so a registry generation change must
        rebuild the agent before the next turn.
        """
        out: Dict[str, Any] = {}
        cfg = user_config if isinstance(user_config, dict) else {}
        for section, key in cls._CACHE_BUSTING_CONFIG_KEYS:
            section_val = cfg.get(section)
            if section == "checkpoints" and isinstance(section_val, bool):
                # Preserve legacy ``checkpoints: true`` behavior.  A live
                # toggle must still rebuild the cached agent.
                out[f"{section}.{key}"] = section_val if key == "enabled" else None
            elif isinstance(section_val, dict):
                out[f"{section}.{key}"] = section_val.get(key)
            else:
                out[f"{section}.{key}"] = None
        try:
            from tools.registry import registry

            out["tools.registry_generation"] = getattr(registry, "_generation", None)
        except Exception:
            out["tools.registry_generation"] = None

        # Honcho identity-mapping keys live in honcho.json, not user_config.
        # Only read that file when Honcho is the active memory provider.
        provider = cfg_get(cfg, "memory", "provider")
        if isinstance(provider, str) and provider.lower() == "honcho":
            out.update(cls._extract_honcho_cache_busting_config())
        else:
            out.update(cls._empty_honcho_cache_busting_config())

        return out

    # Kept for the process lifetime: loading a provider imports its plugin module, and this runs on every inbound message.
    _MEMORY_IDENTITY_PROVIDER_MEMO: dict[str, Any] = {}

    @classmethod
    def _memory_provider_identity_signature(cls, provider_name: Any) -> dict[str, Any]:
        """The active memory provider's ``identity_signature()``. ``{}`` when there is no provider,
        it fails to load, or the hook raises."""
        if not isinstance(provider_name, str) or not provider_name.strip():
            return {}
        name = provider_name.strip()
        try:
            instance = cls._MEMORY_IDENTITY_PROVIDER_MEMO.get(name)
            if instance is None:
                from plugins.memory import load_memory_provider
                instance = load_memory_provider(name, register_skills=False)
                if instance is None:
                    return {}
                cls._MEMORY_IDENTITY_PROVIDER_MEMO[name] = instance
            signature = instance.identity_signature()
            return dict(signature) if isinstance(signature, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _agent_config_signature(
        model: str,
        runtime: dict,
        enabled_toolsets: list,
        ephemeral_prompt: str,
        cache_keys: dict | None = None,
        user_id: str | None = None,
        user_id_alt: str | None = None,
        skip_context_files: bool = False,
    ) -> str:
        """Compute a stable string key from agent config values.

        When this signature changes between messages, the cached AIAgent is
        discarded and rebuilt.  When it stays the same, the cached agent is
        reused — preserving the frozen system prompt and tool schemas for
        prompt cache hits.

        ``cache_keys`` is an optional flat dict of additional config values
        that should invalidate the cache when they change.  Callers pass
        the output of ``_extract_cache_busting_config(user_config)`` so
        edits to model.context_length / compression.* in config.yaml are
        picked up on the next gateway message without a manual restart.

        ``user_id`` and ``user_id_alt`` are the runtime user identities
        carried by the current message's gateway source.  They participate
        in the cache key because the Honcho memory provider freezes them
        into ``HonchoSessionManager`` at first-message init (see
        ``plugins/memory/honcho/__init__.py::_do_session_init``).  Without
        them in the signature, a shared-thread session_key (one in which
        ``build_session_key`` intentionally omits the participant ID,
        e.g. ``thread_sessions_per_user=False``) would reuse the cached
        AIAgent across distinct users, causing the second user's messages
        to be attributed to the first user's resolved Honcho peer.  This
        broke #27371's per-user-peer contract in multi-user gateways.
        Per-user agent rebuilds in shared threads trade prompt-cache
        warmth for correct memory attribution.
        """
        import hashlib, json as _j

        # Fingerprint the FULL credential string instead of using a short
        # prefix. OAuth/JWT-style tokens frequently share a common prefix
        # (e.g. "eyJhbGci"), which can cause false cache hits across auth
        # switches if only the first few characters are considered.
        _api_key = str(runtime.get("api_key", "") or "")
        _api_key_fingerprint = hashlib.sha256(_api_key.encode()).hexdigest() if _api_key else ""

        _cache_keys_sorted = sorted((cache_keys or {}).items())

        blob = _j.dumps(
            [
                model,
                _api_key_fingerprint,
                runtime.get("base_url", ""),
                runtime.get("provider", ""),
                runtime.get("requested_provider", ""),
                runtime.get("api_mode", ""),
                sorted((runtime.get("capabilities") or {}).items()),
                sorted(enabled_toolsets) if enabled_toolsets else [],
                # reasoning_config excluded — it's set per-message on the
                # cached agent and doesn't affect system prompt or tools.
                ephemeral_prompt or "",
                _cache_keys_sorted,
                str(user_id or ""),
                str(user_id_alt or ""),
                # skip_context_files changes the agent's frozen system prompt
                # (context files in vs out) — a toggled config edit must
                # rebuild the cached agent, not silently reuse it.
                bool(skip_context_files),
            ],
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def _session_model_override(self, session_key: str) -> Optional[dict]:
        """Current in-memory /model override for ``session_key`` (None when absent)."""
        state = self._peek_session_state(session_key)
        return state.conversation.model_override if state else None

    def _rehydrate_session_model_override(self, session_key: str) -> None:
        """Lazily restore a persisted /model override after a gateway restart.

        ``_session_model_overrides`` is in-memory only, so before persistence
        a restart silently reverted every session to the global default model.
        The non-secret parts (model/provider/base_url) are written through to
        the session store when /model runs (and cleared on /new); here we read
        them back on first use and re-resolve credentials via the normal
        runtime provider resolution — api_key is never persisted to disk.

        No-op when an in-memory override already exists (live state wins),
        when the store has nothing persisted (e.g. the user ran /new, which
        clears both the in-memory dict and the persisted field), or when the
        persisted override is the retired ``openrouter/stealth/ox-alpha``
        route (scrubbed from config by the v41 migration; a persisted
        override must not resurrect a route whose every request now fails).
        """
        from gateway.run import _resolve_runtime_agent_kwargs_for_provider
        _rehydrate_state = self._peek_session_state(session_key)
        if (
            _rehydrate_state is not None
            and _rehydrate_state.conversation.model_override is not None
        ):
            return
        store = getattr(self, "session_store", None)
        if store is None:
            return
        try:
            persisted = store.get_model_override(session_key)
        except Exception:
            logger.debug(
                "Failed to read persisted session model override", exc_info=True
            )
            return
        if not persisted:
            return
        # Retired Ox Alpha route: the v41 config migration scrubbed
        # openrouter/stealth/ox-alpha from config.yaml because every request
        # on it now fails. A /model override persisted while the route was
        # live must not resurrect it over the migrated (promoted) config —
        # skip the rehydrate and keep the configured default. Exact-route
        # match only: a custom provider/base_url serving the same model id
        # is a different, still-valid route and keeps rehydrating.
        from hermes_cli.fallback_config import is_retired_ox_alpha_route

        if is_retired_ox_alpha_route(
            persisted.get("provider"),
            persisted.get("model"),
            persisted.get("base_url"),
        ):
            logger.info(
                "Skipping rehydrate of retired Ox Alpha /model override for "
                "session=%s; using the migrated config route",
                session_key,
            )
            return
        override: Dict[str, Any] = {
            "model": persisted.get("model"),
            "provider": persisted.get("provider"),
            "base_url": persisted.get("base_url"),
        }
        provider = persisted.get("provider")
        if provider:
            # Re-resolve credentials for the persisted provider. On failure
            # (e.g. credentials were removed since the switch) keep the
            # credential-less override — _resolve_session_agent_runtime falls
            # back to env-based resolution and applies model/provider on top.
            try:
                runtime = _resolve_runtime_agent_kwargs_for_provider(provider)
                override["api_key"] = runtime.get("api_key")
                override["api_mode"] = runtime.get("api_mode")
                override["credential_pool"] = runtime.get("credential_pool")
                override["request_overrides"] = dict(
                    runtime.get("request_overrides") or {}
                )
                override["requested_provider"] = runtime.get("requested_provider")
                override["capabilities"] = dict(runtime.get("capabilities") or {})
                override["max_tokens"] = runtime.get("max_tokens")
                if not override.get("base_url"):
                    override["base_url"] = runtime.get("base_url")
            except Exception:
                logger.debug(
                    "Credential re-resolution failed for persisted override "
                    "(provider=%s); using credential-less override",
                    provider, exc_info=True,
                )
        self._session_state(session_key).conversation.model_override = override
        logger.info(
            "Rehydrated persisted /model override for session=%s: model=%s provider=%s",
            session_key, override.get("model"), provider or "",
        )

    def _apply_session_model_override(
        self, session_key: str, model: str, runtime_kwargs: dict
    ) -> tuple:
        """Apply /model session overrides if present, returning (model, runtime_kwargs).

        The gateway /model command stores per-session overrides in
        ``_session_model_overrides``.  These must take precedence over
        config.yaml defaults so the switched model is actually used for
        subsequent messages.  Fields with ``None`` values are skipped so
        partial overrides don't clobber valid config defaults.
        """
        from gateway.run import _credential_pool_for_provider
        _apply_state = self._peek_session_state(session_key)
        override = _apply_state.conversation.model_override if _apply_state else None
        if not override:
            return model, runtime_kwargs
        model = override.get("model", model)
        for key in (
            "provider",
            "requested_provider",
            "api_key",
            "base_url",
            "api_mode",
            "credential_pool",
            "capabilities",
            "max_tokens",
        ):
            val = override.get(key)
            if val is not None:
                runtime_kwargs[key] = val
        # request_overrides reflects the switched-to provider; apply whenever
        # the override recorded it (even as None) so switching to a provider
        # without configured overrides clears a stale value left by the
        # default provider's runtime resolution.
        if "request_overrides" in override:
            override_request_overrides = override.get("request_overrides")
            if isinstance(override_request_overrides, dict) and override_request_overrides:
                runtime_kwargs["request_overrides"] = dict(override_request_overrides)
            else:
                runtime_kwargs["request_overrides"] = override_request_overrides
        if (
            runtime_kwargs.get("api_key")
            and runtime_kwargs.get("credential_pool") is None
            and override.get("provider")
        ):
            runtime_kwargs["credential_pool"] = _credential_pool_for_provider(
                override.get("provider")
            )
        return model, runtime_kwargs

    def _snapshot_session_model_override(self, session_key: str) -> dict:
        """Capture a gateway session override before a one-turn switch."""
        _snap_state = self._peek_session_state(session_key)
        override = _snap_state.conversation.model_override if _snap_state else None
        return {
            "had_override": override is not None,
            "override": dict(override) if override is not None else None,
        }

    def _claim_one_turn_restore(self, session_key: str, snapshot: Optional[dict] = None) -> None:
        """Arm the one-shot restore snapshot for ``/model --once`` / ``/moa``. A repeated one-shot
        command before the turn runs keeps the EARLIEST snapshot: the later command's snapshot is
        the first temporary model, not the user's standing override. Pass *snapshot* when the
        caller captured the pre-switch state earlier (``/model --once`` applies its override before
        arming); omit it to snapshot now."""
        conv = self._session_state(session_key).conversation
        if not conv.one_turn_restore:
            conv.one_turn_restore = dict(snapshot) if snapshot is not None else self._snapshot_session_model_override(session_key)

    def _restore_session_model_override(self, session_key: str, snapshot: dict) -> None:
        """Restore the session override captured before a one-turn switch."""
        if not session_key:
            return
        if snapshot.get("had_override"):
            self._session_state(session_key).conversation.model_override = dict(
                snapshot.get("override") or {}
            )
        else:
            _rst_state = self._peek_session_state(session_key)
            if _rst_state is not None:
                _rst_state.conversation.model_override = None
        self._evict_cached_agent(session_key)

    def _is_intentional_model_switch(self, session_key: str, agent: Any, config_model: str) -> bool:
        """True when *agent* running a model other than *config_model* is deliberate: a /model session
        override names that model, or the Nous gateway moved the session off the ``nous/welcome``
        alias that *config_model* still carries (``anon_auth.apply_model_switch``)."""
        _ims_state = self._peek_session_state(session_key)
        override = _ims_state.conversation.model_override if _ims_state else None
        if override is not None and override.get("model") == agent.model:
            return True
        # Exactly the recorded move (alias -> backing): a later fallback onto some other model is
        # ordinary drift and still evicts.
        return getattr(agent, "_nous_model_switch", None) == (config_model, agent.model)

    def _release_running_agent_state(
        self,
        session_key: str,
        *,
        run_generation: Optional[int] = None,
    ) -> bool:
        """Pop ALL per-running-agent state entries for ``session_key``.

        Replaces ad-hoc ``del self._running_agents[key]`` calls scattered
        across the gateway.  Those sites had drifted: some popped only
        ``_running_agents``; some also ``_running_agents_ts``; only one
        path also cleared ``_busy_ack_ts``.  Each missed entry was a
        small, persistent leak — a (str_key → float) tuple per session
        per gateway lifetime.

        Use this at every site that ends a running turn, regardless of
        cause (normal completion, /stop, /reset, /resume, sentinel
        cleanup, stale-eviction).  Per-session state that PERSISTS
        across turns (``_session_model_overrides``, ``_voice_mode``,
        ``_pending_approvals``, ``_update_prompt_pending``) is NOT
        touched here — those have their own lifecycles.

        When ``run_generation`` is provided, only clear the slot if that
        generation is still current for the session.  This prevents an
        older async run whose generation was bumped by /stop or /new from
        clobbering a newer run's state during its own unwind.  Returns
        True when the slot was cleared, False when an ownership guard
        blocked it.
        """
        if not session_key:
            return False
        if run_generation is not None and not self._is_session_run_current(
            session_key, run_generation
        ):
            return False
        state = self._peek_session_state(session_key)
        if state is not None:
            lease = state.turn.lease
            if lease is not None:
                try:
                    lease.release()
                except Exception:
                    logger.debug(
                        "Failed to release active session slot", exc_info=True
                    )
            # One structured reset instead of the old drifting pop-list
            # (agent / started_ts / lease / busy_ack_ts).  Turn-lease tokens
            # are deliberately NOT cleared here — _release_turn_lease owns
            # them (#64934).
            # Drop any unfired one-shot steer-delivery listener while
            # ``state.turn.agent`` is still reachable, so it can never fire
            # on a later turn that reuses the same agent object.
            self._drop_steer_delivered_listener(session_key)
            state.turn.clear()
        # Turn boundary: a running-agent slot was just released.  Persist the
        # new (lower) in-flight count so the dashboard readout stays current
        # between lifecycle transitions.  Preserves gateway_state (see
        # _persist_active_agents).
        self._persist_active_agents()
        return True

    def _drop_turn_slot(self, session_key: str, *, run_generation: Optional[int] = None) -> None:
        """Release the running-agent slot and evict the cached instance (/stop, eviction, reaper).
        ``_interrupt_requested`` is cleared only by the turn finalizer, so on a hung/still-draining
        run the flag would survive and silently kill the session's NEXT message (interrupted=True,
        api_calls=0, empty response); the next message rebuilds from history while the old agent
        keeps its flag so a hung drain still dies (#44212). With ``run_generation`` (the post-bump
        value ``_interrupt_running_turn`` returns), the release is generation-guarded: an async
        path awaits between bump and release, so a successor claiming the slot in that window must
        not have its sentinel/lease wiped by the displaced path's tail. Then sweep lease tokens
        from generations OLDER than the current one: a hung evicted turn's finalizer may never run,
        and each such generation would otherwise pin its token (and its ``_SessionLease``) forever.
        Identity-checked + idempotent, so a live successor's token is never affected."""
        self._release_running_agent_state(session_key, run_generation=run_generation)
        self._evict_cached_agent(session_key)
        state = self._peek_session_state(session_key)
        registry = getattr(self, "_turn_leases", None)
        if state is None or registry is None:
            return
        current = int(state.persistent.run_generation or 0)
        tokens = state.turn.lease_tokens
        for gen in [g for g in tokens if int(g) < current]:
            token = tokens.pop(gen)
            try:
                registry.release(token)
            except Exception:
                logger.debug("Failed to release displaced turn lease gen %s for %s", gen, session_key,
                             exc_info=True)

    def _held_turn_lease(self, session_key: str, run_generation: int):
        """Return ``(registry, lease_tokens)`` when ``session_key`` holds a lease token for
        ``run_generation``, else None. Callers ``get``/``pop`` the token from the map themselves."""
        registry = getattr(self, "_turn_leases", None)
        state = self._peek_session_state(session_key) if session_key and registry is not None else None
        tokens = state.turn.lease_tokens if state is not None else None
        if tokens is None or run_generation not in tokens:
            return None
        return registry, tokens

    def _release_turn_lease(self, session_key: str, run_generation: int) -> bool:
        """Release the turn lease acquired by (``session_key``, ``run_generation``).

        Companion to the acquisition in ``_handle_message_with_agent``
        (#64934). The token map is keyed by (routing key, run generation), so
        this can only ever free the lease its own turn acquired — a stale
        unwind whose generation was bumped by /stop or /new pops ITS token,
        and the registry's identity check refuses it if a newer turn already
        holds the lease. Idempotent and safe for bare test runners built via
        ``object.__new__`` (getattr defaults).
        """
        if not session_key:
            return False
        registry = getattr(self, "_turn_leases", None)
        state = self._peek_session_state(session_key)
        if state is None or registry is None:
            return False
        turn = state.turn
        if turn.lease_token is None or turn.lease_generation != run_generation:
            return False
        token = turn.lease_token
        turn.lease_token = None
        turn.lease_generation = None
        try:
            return registry.release(token)
        except Exception:
            logger.debug("Failed to release turn lease", exc_info=True)
            return False

    def _rebind_turn_lease(
        self, session_key: str, run_generation: int, new_session_id: str
    ) -> bool:
        """Follow a mid-turn session_id rotation with the held turn lease.

        Compression (session-hygiene pre-compression or the agent's own
        compressor) can rotate ``session_entry.session_id`` while this turn
        is in flight. The turn's flush targets the NEW id, so the
        serialization boundary must follow it — otherwise an alias routing
        key resolving the new id (topic tip-walk onto the fresh child) could
        start a concurrent turn the lease never sees (#64934 rotation-alias
        window). Call at every site that reassigns session_entry.session_id
        mid-turn. Fail-open no-op when there is no held token.
        """
        if not session_key or not new_session_id:
            return False
        registry = getattr(self, "_turn_leases", None)
        state = self._peek_session_state(session_key)
        if state is None or registry is None:
            return False
        turn = state.turn
        if turn.lease_token is None or turn.lease_generation != run_generation:
            return False
        try:
            return registry.rebind(turn.lease_token, new_session_id)
        except Exception:
            logger.debug("Failed to rebind turn lease", exc_info=True)
            return False

    def _clear_conversation_scope(self, session_key: str, *, reason: str) -> None:
        """Clear ALL conversation-scoped per-session state for ``session_key``.

        THE single conversation-boundary funnel. Call this — and nothing
        else — whenever a session_key crosses a conversation boundary:
        /new, /resume, auto-reset (idle/daily/suspended), expiry
        finalization, and the compression-exhausted auto-reset.

        Why a funnel: these boundaries used to each carry a hand-copied
        pop-list of the per-session dicts, and the lists drifted every time
        a new dict was added (#48031, #58403, #10702, #35809 were all
        "boundary X forgot dict Y" bugs — e.g. /new cleared the /model
        override but not the /model --once restore snapshot). Adding a new
        conversation-scoped dict now means adding its attribute name to
        _CONVERSATION_SCOPED_STATE below; every boundary picks it up
        automatically.

        Scope rules:
        - Conversation-scoped (cleared here): model/reasoning overrides,
          one-turn restore snapshots, pending model notes, last-resolved
          model cache, queued follow-up events, and the boundary security
          state (approvals, /yolo, slash-confirm, update prompts).
        - Turn-scoped (NOT cleared here): _running_agents/_ts, slot leases,
          turn-lease tokens — owned by _release_running_agent_state and the
          dispatch finally.
        - Idle agent-cache eviction is NOT a conversation boundary: the
          session is still alive and a resumed turn rebuilds from these
          overrides. Only true boundaries call this.

        Safe on bare test runners built via ``object.__new__`` (every
        access is getattr-guarded).
        """
        from gateway.run import _CONVERSATION_SCOPED_STATE
        if not session_key:
            return
        # Structural clear: every conversation-scoped field resets in one
        # call — no per-attribute pop-list to drift.
        state = self._peek_session_state(session_key)
        if state is not None:
            state.conversation.clear()
        # Legacy plain-dict stores still registered in
        # _CONVERSATION_SCOPED_STATE (not yet folded into SessionState),
        # e.g. _pending_model_notes.  SessionState-backed names resolve to
        # MutableMapping views (not dict), so the isinstance(dict) guard
        # skips them — already handled above.
        for attr in _CONVERSATION_SCOPED_STATE:
            store = getattr(self, attr, None)
            if isinstance(store, dict):
                store.pop(session_key, None)
        self._clear_session_boundary_security_state(session_key)
        logger.debug(
            "Cleared conversation scope for %s (%s)", session_key, reason
        )

    def _clear_session_boundary_security_state(self, session_key: str) -> None:
        """Clear per-session control state that must not survive a boundary switch."""
        if not session_key:
            return

        pending_skills_reload_notes = getattr(
            self, "_pending_skills_reload_notes", None
        )
        if isinstance(pending_skills_reload_notes, dict):
            pending_skills_reload_notes.pop(session_key, None)

        # Queued forced-resume user text belongs to THIS conversation only:
        # a boundary switch must never deliver it into the next one.
        pending_resume_user_text = getattr(self, "_pending_resume_user_text", None)
        if isinstance(pending_resume_user_text, dict):
            pending_resume_user_text.pop(session_key, None)

        _sec_state = self._peek_session_state(session_key)
        if _sec_state is not None:
            _sec_state.persistent.approvals = None
            _sec_state.persistent.update_prompt_pending = False

        try:
            from tools import slash_confirm as _slash_confirm_mod
        except Exception:
            _slash_confirm_mod = None
        if _slash_confirm_mod is not None:
            try:
                _slash_confirm_mod.clear(session_key)
            except Exception as e:
                logger.debug(
                    "Failed to clear slash-confirm state for session boundary %s: %s",
                    session_key,
                    e,
                )

        try:
            from tools.approval import clear_session as _clear_approval_session
        except Exception:
            return

        try:
            _clear_approval_session(session_key)
        except Exception as e:
            logger.debug(
                "Failed to clear approval state for session boundary %s: %s",
                session_key,
                e,
            )

    def _begin_session_run_generation(self, session_key: str) -> int:
        """Claim a fresh, monotonically increasing run generation token (NEVER reset): a late result
        from a worker /stop or /new invalidated is recognized and dropped."""
        if not session_key:
            return 0
        persistent = self._session_state(session_key).persistent
        # Monotonic by design (#28686): incremented here, NEVER reset.
        persistent.run_generation = int(persistent.run_generation) + 1
        return persistent.run_generation

    def _invalidate_session_run_generation(self, session_key: str, *, reason: str = "") -> int:
        """Invalidate any in-flight run token for ``session_key``."""
        generation = self._begin_session_run_generation(session_key)
        if reason:
            logger.info(
                "Invalidated run generation for %s → %d (%s)",
                session_key,
                generation,
                reason,
            )
        return generation

    def _current_session_run_generation(self, session_key: str) -> int:
        """Current run generation for ``session_key`` (0 when the key tracks no run)."""
        state = self._peek_session_state(session_key)
        return int(state.persistent.run_generation) if state is not None else 0

    def _is_session_run_current(self, session_key: str, generation: int) -> bool:
        """Return True when ``generation`` is still current for ``session_key``."""
        if not session_key:
            return True
        return self._current_session_run_generation(session_key) == int(generation)

    def _bind_adapter_run_generation(
        self,
        adapter: Any,
        session_key: str,
        generation: int | None,
    ) -> None:
        """Bind a gateway run generation to the adapter's active-session event."""
        if not adapter or not session_key or generation is None:
            return
        try:
            interrupt_event = getattr(adapter, "_active_sessions", {}).get(session_key)
            if interrupt_event is not None:
                setattr(interrupt_event, "_hermes_run_generation", int(generation))
        except Exception:
            pass

    def _interrupt_running_turn(
        self, session_key: str, *, interrupt_reason: str, invalidation_reason: str, tool_reason: str | None = None,
    ) -> int:
        """Sync core shared by /stop, /new and eviction: request a hard interrupt on the in-flight
        agent, invalidate its run generation, and reap the tool processes that turn spawned.
        ``tool_reason`` names a system issuer (eviction); ``None`` keeps the user attribution of /stop and /new.
        Returns the post-bump generation."""
        from contextvars import copy_context
        from gateway.run import _AGENT_PENDING_SENTINEL, _reap_gateway_turn_processes, request_hard_interrupt
        state = self._peek_session_state(session_key)
        running_agent = state.turn.agent if state else None
        _process_task_id, _process_baseline = "", None
        if running_agent and running_agent is not _AGENT_PENDING_SENTINEL:
            # A raising interrupt implementation must not leave the slot unroutable: the generation
            # bump and release below are the cleanup that matters.
            with _log_suppressed(logging.WARNING, "Failed to interrupt running agent for %s; continuing",
                                 session_key, exc_info=True):
                request_hard_interrupt(running_agent, interrupt_reason, tool_reason=tool_reason)
            _process_task_id = getattr(running_agent, "_gateway_turn_process_task_id", "")
            _process_baseline = getattr(running_agent, "_gateway_turn_process_baseline", None)
        # Bump the generation BEFORE scheduling the reap thread and capture the post-bump value:
        # task_id is session-scoped, so a replacement turn spawning before the reap runs bumps it
        # again and the closure sees a stale generation and skips — the replacement's own baseline
        # covers its cleanup, so nothing stays unreaped.
        _generation_at_interrupt = self._invalidate_session_run_generation(session_key, reason=invalidation_reason)
        if _process_task_id and _process_baseline is not None:
            threading.Thread(
                target=copy_context().run,
                args=(_reap_gateway_turn_processes, _process_task_id, _process_baseline),
                kwargs={
                    "source": "gateway_turn_interrupt",
                    "is_still_current": lambda: self._is_session_run_current(session_key, _generation_at_interrupt),
                },
                name=f"gateway-turn-reaper-{_process_task_id[:12]}",
                daemon=True,
            ).start()
        return _generation_at_interrupt

    async def _interrupt_and_clear_session(
        self,
        session_key: str,
        source: SessionSource,
        *,
        interrupt_reason: str,
        invalidation_reason: str,
        release_running_state: bool = True,
    ) -> None:
        """Interrupt the current run and clear queued session state consistently."""
        from gateway.run import _AGENT_PENDING_SENTINEL, _reap_gateway_turn_processes
        if not session_key:
            return
        _iac_state = self._peek_session_state(session_key)
        running_agent = _iac_state.turn.agent if _iac_state else None
        _process_task_id = ""
        _process_baseline = None
        if running_agent and running_agent is not _AGENT_PENDING_SENTINEL:
            request_hard_interrupt(running_agent, interrupt_reason)
            _process_task_id = getattr(
                running_agent, "_gateway_turn_process_task_id", ""
            )
            _process_baseline = getattr(
                running_agent, "_gateway_turn_process_baseline", None
            )
        # Bump the generation *before* scheduling the reap thread and capture
        # the post-bump value: task_id is session-scoped (task_id ==
        # session_id), so if a replacement turn claims this session and
        # spawns its own process before the reap thread actually runs, that
        # claim bumps the generation again. The closure below then sees a
        # stale generation and skips — the replacement turn's own baseline
        # covers its own cleanup, so nothing is left permanently unreaped.
        _generation_at_interrupt = self._invalidate_session_run_generation(
            session_key, reason=invalidation_reason
        )
        if _process_task_id and _process_baseline is not None:
            threading.Thread(
                target=_reap_gateway_turn_processes,
                args=(_process_task_id, _process_baseline),
                kwargs={
                    "source": "gateway_turn_interrupt",
                    "is_still_current": lambda: self._is_session_run_current(
                        session_key, _generation_at_interrupt
                    ),
                },
                name=f"gateway-turn-reaper-{_process_task_id[:12]}",
                daemon=True,
            ).start()
        adapter = self._adapter_for_source(source)
        interrupt_session_activity = getattr(
            type(adapter), "interrupt_session_activity", None
        )
        if adapter and callable(interrupt_session_activity):
            metadata = self._thread_metadata_for_source(source)
            try:
                params = inspect.signature(interrupt_session_activity).parameters
                accepts_metadata = "metadata" in params or any(
                    param.kind is inspect.Parameter.VAR_KEYWORD
                    for param in params.values()
                )
            except (TypeError, ValueError):
                accepts_metadata = False
            if accepts_metadata:
                await adapter.interrupt_session_activity(
                    session_key, source.chat_id, metadata=metadata
                )
            else:
                await adapter.interrupt_session_activity(session_key, source.chat_id)
        if adapter and hasattr(adapter, "get_pending_message"):
            adapter.get_pending_message(session_key)  # consume and discard
        if _iac_state is not None:
            _iac_state.persistent.pending_command_text = None
        if release_running_state:
            self._release_running_agent_state(session_key)
            # Evict the cached agent: ``_interrupt_requested`` is only
            # cleared by the turn finalizer, so on a hung or still-draining
            # run the flag survives the lock release and kills the session's
            # NEXT message at the top of the tool loop (interrupted=True,
            # api_calls=0, empty response — silently swallowed, #44212).
            # Evicting mirrors the /new and /model paths: the next message
            # rebuilds the agent from session history, while the old agent
            # object keeps its interrupt flag so a hung drain still dies
            # when it unblocks.
            self._evict_cached_agent(session_key)

    async def _refresh_agent_cache_message_count(
        self, session_key: str, session_id: Optional[str]
    ) -> None:
        """Re-baseline a cached agent's stored message_count after THIS turn.

        The cross-process coherence guard (#45966) compares the session's
        on-disk ``message_count`` against the count snapshotted next to the
        cached agent, and rebuilds the agent on a mismatch.  But the snapshot
        is taken at agent-BUILD time — before this turn writes its own user +
        assistant (+ tool) rows — and the cache entry is never rewritten on a
        reuse.  So without this re-baseline, THIS process's own turn would
        grow ``message_count`` and the very next turn would see a mismatch
        and rebuild the agent — every turn, for every conversation — silently
        destroying the per-conversation prompt caching the cache exists to
        protect.

        Call this once a turn has completed and the agent has flushed its
        rows to the SessionDB.  It snapshots the now-current count (which
        includes this process's own writes) so the guard only fires when a
        DIFFERENT process changes the transcript out from under us.  The
        ``_sig`` is left untouched; only the count element is refreshed, and
        only when the same agent is still cached (no rebuild/eviction raced
        in between).  Fail-safe: any DB error leaves the snapshot as-is, which
        at worst costs one unnecessary rebuild on the next turn.

        When the cache entry records a ``session_id`` (4-tuple form, #54947)
        that differs from the current ``session_id`` — meaning the cache
        was built for a DIFFERENT conversation under the same ``session_key``
        — the snapshot is intentionally left untouched.  Overwriting it with
        the current session's count would corrupt the original conversation's
        baseline and cause the next switch back to fire the cross-process
        guard spuriously.  Fail-safe: the legacy 3-tuple shape (no
        ``session_id``) is still re-baselined as before.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL
        if self._session_db is None or not session_id:
            return
        _cache_lock = getattr(self, "_agent_cache_lock", None)
        _cache = getattr(self, "_agent_cache", None)
        if not _cache_lock or _cache is None:
            return
        try:
            _sess_row = await self._session_db.get_session(session_id)
            _live = _sess_row.get("message_count", 0) if _sess_row else None
        except Exception:
            return
        if _live is None:
            return
        with _cache_lock:
            cached = _cache.get(session_key)
            # Only re-baseline a live 3-tuple entry; skip pending sentinels,
            # legacy 2-tuples (they intentionally opt out of the guard), and
            # the case where the entry was evicted/rebuilt mid-turn.
            if (
                isinstance(cached, tuple)
                and len(cached) > 2
                and cached[0] is not _AGENT_PENDING_SENTINEL
            ):
                # If the snapshot was taken for a different session_id
                # (same session_key, different conversation), leave the
                # snapshot alone — the current session_id's count belongs
                # to a different DB row (#54947).
                _snapshot_sid = cached[3] if len(cached) > 3 else None
                if _snapshot_sid is not None and _snapshot_sid != session_id:
                    return
                if cached[2] != _live:
                    if _snapshot_sid is None:
                        # Legacy 3-tuple: preserve the original 3-element
                        # shape so existing entries stay compatible with
                        # callers that index ``cached[2]`` directly.
                        _cache[session_key] = (cached[0], cached[1], _live)
                    else:
                        _cache[session_key] = (
                            cached[0], cached[1], _live, _snapshot_sid,
                        )

    def _set_pending_turn_sidecar_notes(self, session_key: str, notes: List[str]) -> None:
        """Stage per-turn must-deliver notes for the next agent run (one-shot)."""
        if not session_key or not notes:
            return
        self._session_state(session_key).conversation.sidecar_notes = list(notes)

    def _consume_pending_turn_sidecar_notes(self, session_key: str) -> List[str]:
        if not session_key:
            return []
        state = self._peek_session_state(session_key)
        if state is None:
            return []
        staged = state.conversation.sidecar_notes
        state.conversation.sidecar_notes = []
        return list(staged) if isinstance(staged, list) else []

    def _voice_channel_sidecar_note(self, event, source: SessionSource, session_key: str) -> Optional[str]:
        """Return a ``[Voice channel now: ...]`` note when VC state changed.

        Compares the live Discord voice-channel context against the last
        value delivered for this session and returns a note only on change
        (including leaving the channel).  Unchanged state returns ``None`` so
        the per-turn member/speaking serialization cannot churn the prompt.
        """
        if source.platform != Platform.DISCORD:
            return None
        adapter = self.adapters.get(Platform.DISCORD)
        guild_id = self._get_guild_id(event)
        if not (guild_id and adapter and hasattr(adapter, "get_voice_channel_context")):
            return None
        try:
            vc_now = adapter.get_voice_channel_context(guild_id) or ""
        except Exception:
            logger.debug("voice-channel context read failed", exc_info=True)
            return None
        vc_prev = None
        if session_key:
            _vc_state = self._session_state(session_key)
            vc_prev = _vc_state.conversation.vc_last
            _vc_state.conversation.vc_last = vc_now
        if vc_now == (vc_prev if vc_prev is not None else ""):
            return None
        if not vc_now:
            return "[Voice channel now: not connected to a voice channel]"
        return f"[Voice channel now: {vc_now}]"

    def _pinned_session_context_prompt(
        self, context, redact_pii: bool, session_key: Optional[str]
    ) -> str:
        """Return the session-context prompt, pinned per session.

        Key hit → the pinned bytes are reused VERBATIM (immunizes the
        composed system prompt against renderer nondeterminism); key miss →
        re-render ``build_session_context_prompt`` and re-pin (a legitimate
        cache bust: rename, topic edit, /setnotify, redact_pii flip, ...).
        """
        _eph_key = self._ephemeral_change_key(context, redact_pii)
        _eph_pin = None
        if session_key:
            _pin_state = self._peek_session_state(session_key)
            _eph_pin = _pin_state.conversation.ephemeral_pin if _pin_state else None
        if _eph_pin is not None and _eph_pin[0] == _eph_key:
            return _eph_pin[1]
        text = build_session_context_prompt(context, redact_pii=redact_pii)
        if session_key:
            self._session_state(session_key).conversation.ephemeral_pin = (
                _eph_key,
                text,
            )
        return text

    @staticmethod
    def _ephemeral_change_key(context, redact_pii: bool) -> str:
        """Hash the exact inputs ``build_session_context_prompt`` renders.

        This key decides when the pinned per-session context-prompt bytes are
        reused verbatim vs re-rendered.  The maintained invariant (guarded by
        the parity test in tests/gateway/test_prompt_tail_freeze.py): any
        input whose change alters the rendered bytes MUST appear here —
        omission means a stale pinned prompt (cosmetic staleness); inclusion
        of an extra field only costs a spurious re-render.
        """
        import hashlib

        src = context.source
        platform = src.platform.value if src.platform else ""

        discord_ids: tuple = ()
        discord_tools = ""
        if src.platform == Platform.DISCORD:
            from gateway.session import _discord_tools_loaded

            discord_tools = "1" if _discord_tools_loaded() else "0"
            discord_ids = (
                str(src.guild_id or ""),
                str(src.parent_chat_id or ""),
                str(src.thread_id or ""),
                str(src.chat_id or ""),
                # Only PRESENCE is rendered (the id itself is delivered
                # per-turn in the user message) — keying on the value would
                # re-render every message for zero byte change.
                "1" if src.message_id else "0",
            )

        # Slack renders a capability-aware platform note gated on
        # _slack_tools_loaded() — the gate state must appear in the key
        # (same parity contract as the Discord gate above) so a config /
        # MCP-registration flip re-renders once instead of serving a
        # stale pinned note for the rest of the session.
        slack_tools = ""
        if src.platform == Platform.SLACK:
            from gateway.session import _slack_tools_loaded

            slack_tools = "1" if _slack_tools_loaded() else "0"

        try:
            from hermes_constants import display_hermes_home

            home_display = str(display_hermes_home())
        except Exception:
            home_display = ""

        # Active-mission digest (missions plugin): the rendered session
        # context includes an "Active Mission" section for chats with a
        # bound mission, so its state must appear in this key (parity
        # contract with tests/gateway/test_prompt_tail_freeze.py) or a
        # started/closed mission would leave a stale pinned prompt.
        mission_digest = ""
        try:
            from plugins.missions import find_active_mission_for_chat

            _m_chat = str(src.chat_id or src.user_id or "")
            if _m_chat:
                _mission = find_active_mission_for_chat(_m_chat)
                if _mission:
                    mission_digest = hashlib.sha256(
                        json.dumps(
                            {
                                "id": str(_mission.get("mission_id") or ""),
                                "status": str(_mission.get("status") or ""),
                                "goal": str(_mission.get("goal") or ""),
                                "persona": str(_mission.get("persona_instructions") or ""),
                                "name": str(_mission.get("chat_name") or ""),
                                "chat_type": str(_mission.get("chat_type") or ""),
                            },
                            sort_keys=True,
                        ).encode("utf-8")
                    ).hexdigest()
        except Exception:
            mission_digest = ""

        key_tuple = (
            platform,
            str(src.chat_id or ""),
            str(src.thread_id or ""),
            str(src.chat_type or ""),
            str(src.chat_name or ""),
            str(src.chat_topic or ""),
            str(src.user_name or ""),
            str(src.user_id or ""),
            str(getattr(src, "profile", None) or ""),
            bool(context.shared_multi_user_session),
            discord_ids,
            discord_tools,
            slack_tools,
            tuple(p.value for p in context.connected_platforms),
            bool(redact_pii),
            home_display,
            mission_digest,
        )
        return hashlib.sha256(repr(key_tuple).encode("utf-8")).hexdigest()

    def _evict_cached_agent(self, session_key: str) -> None:
        """Remove a cached agent for a session (called on /new, /model, etc).

        Pops the entry AND soft-releases the evicted agent's LLM client
        pool so the httpx connection (sockets + held buffers) is freed
        promptly rather than waiting on CPython GC — AIAgent holds
        reference cycles (callbacks, tool state) that delay refcount
        collection, so a manual release is required to keep gateway RSS
        flat across many /new, /model, undo and reset operations (#29298,
        same leak class as #25315).

        The release is soft (``release_clients()``): it frees the client
        pool and per-turn child subagents but PRESERVES the session's
        terminal sandbox, browser daemon, and tracked bg processes (keyed
        on task_id), because the session may resume with a freshly-built
        agent.  Call sites that want a hard teardown (true conversation
        boundaries like /new) already call ``_cleanup_agent_resources``
        before evicting; ``release_clients`` is idempotent and safe to
        run again after that (the client is already None).

        Cleanup runs on a daemon thread so we never block holding
        ``_agent_cache_lock`` on slow socket teardown — mirrors the
        cap-enforcer and idle-sweeper paths.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL
        # Prompt-stability state rides the agent-cache lifecycle: a fresh
        # agent must re-render its session-context bytes (the pin) and re-see
        # the current voice-channel state once.
        _evict_state = self._peek_session_state(session_key)
        if _evict_state is not None:
            _evict_state.conversation.ephemeral_pin = None
            _evict_state.conversation.vc_last = None

        _lock = getattr(self, "_agent_cache_lock", None)
        evicted = None
        if _lock:
            with _lock:
                evicted = self._agent_cache.pop(session_key, None)
        else:
            _cache = getattr(self, "_agent_cache", None)
            if _cache is not None:
                evicted = _cache.pop(session_key, None)

        agent = evicted[0] if isinstance(evicted, tuple) and evicted else evicted
        if agent is None or agent is _AGENT_PENDING_SENTINEL:
            return

        # Don't tear down an agent that's actively mid-turn — its client,
        # sandbox and child subagents are in use by the running request.
        running_ids = {
            id(a)
            for _, a in self._running_agent_items()
            if a is not None and a is not _AGENT_PENDING_SENTINEL
        }
        if id(agent) in running_ids:
            return

        try:
            threading.Thread(
                target=self._release_evicted_agent_soft,
                args=(agent,),
                daemon=True,
                name=f"agent-evict-{str(session_key)[:24]}",
            ).start()
        except Exception:
            # If we can't spawn a thread (interpreter shutdown), release
            # inline as a best-effort fallback.
            try:
                self._release_evicted_agent_soft(agent)
            except Exception:
                pass

    def _spawn_release_thread(self, target, args: tuple, name: str, *, inline_fallback: bool,
                              session_key: Optional[str] = None) -> None:
        """Run a release on a daemon thread. ``inline_fallback`` runs it inline (best-effort) when no
        thread can start (interpreter shutdown); otherwise a spawn failure propagates, as on main.
        The thread runs inside the owning profile's scope (see ``_run_release_in_profile_scope``)."""
        import contextvars
        ctx = contextvars.copy_context()
        try:
            threading.Thread(target=ctx.run, args=(self._run_release_in_profile_scope, target, args, session_key),
                             daemon=True, name=name).start()
        except Exception:
            if not inline_fallback:
                raise
            with suppress(Exception):
                ctx.run(self._run_release_in_profile_scope, target, args, session_key)

    def _run_release_in_profile_scope(self, target, args: tuple, session_key: Optional[str]) -> None:
        """Call ``target(*args)`` under the profile that OWNS ``session_key``.

        Threads start with an EMPTY context, so a bare thread would commit end-of-session memory
        (provider ``on_session_end`` reads credentials/home at call time) under the launch profile.
        And the LRU-cap eviction runs inside the REQUESTING turn, whose agent may belong to another
        profile — so "some scope is present" is not enough either. The owner comes from the session
        key: a named profile's home, else the DEFAULT profile (``agent:main:`` keys), which is the
        root Hermes dir even when the gateway was launched under a named profile. Its scope is
        entered unless the current one already is the owner's."""
        from agent.secret_scope import current_secret_scope, is_multiplex_active
        scope = nullcontext()
        if is_multiplex_active():
            from gateway.run import _profile_runtime_scope
            from hermes_constants import get_default_hermes_root, get_hermes_home, hermes_home_key
            owner = None
            store = getattr(self, "session_store", None)
            if session_key and store is not None:
                try:
                    owner = store._profile_home_for_key(session_key)
                except Exception:
                    logger.warning("Could not resolve the owning profile for %s; releasing under the default profile",
                                   session_key, exc_info=True)
            owner_home = Path(owner) if owner else get_default_hermes_root()
            if current_secret_scope() is None or hermes_home_key(get_hermes_home()) != hermes_home_key(owner_home):
                scope = _profile_runtime_scope(owner_home)
        with scope:
            target(*args)

    def _commit_memory_before_soft_evict(self, agent: Any, key: str) -> None:
        """Commit the live transcript to memory providers before resource-only eviction."""
        # No external memory provider (``_memory_manager`` None) — nothing to commit.
        if agent is None or not hasattr(agent, "commit_memory_session") or getattr(agent, "_memory_manager", None) is None:
            return
        try:
            messages = getattr(agent, "_session_messages", None)
            agent.commit_memory_session(messages if isinstance(messages, list) else None)
            logger.debug(
                "Committed on_session_end extraction before soft-evicting "
                "session=%s (resource eviction)", key,
            )
        except Exception as _e:
            logger.debug("Pre-evict memory commit failed for %s: %s", key, _e)

    def _commit_then_release_soft(self, agent: Any, key: str) -> None:
        """Commit end-of-session memory (if warranted), then soft-release — on the daemon eviction
        thread. Order matters: commit needs the live memory manager before ``release_clients``."""
        self._commit_memory_before_soft_evict(agent, key)
        self._release_evicted_agent_soft(agent)

    def _release_evicted_agent_soft(self, agent: Any) -> None:
        """Soft cleanup for cache-evicted agents: unlike _cleanup_agent_resources, the session may
        resume, so terminal sandbox, browser daemon and bg processes outlive the AIAgent instance."""
        if agent is None:
            return
        with suppress(Exception):
            if hasattr(agent, "release_clients"):
                agent.release_clients()
            else:
                # Older agent instance (shouldn't happen in practice) — legacy full-close path.
                self._cleanup_agent_resources(agent)
        # Free conversation history — tens of MB of tool output on heavy 100+-tool-call sessions.
        # release_clients() preserves session tool state for resume, but the message list is rebuilt from
        # persisted session JSON on the next turn, so dropping it here is safe.
        if hasattr(agent, "_session_messages"):
            agent._session_messages = []
        # _db_flush_scan_prefix (run_agent.py, stamped on every successful flush) is a shallow copy
        # sharing every message dict of the flushed transcript, so leaving it pins the multi-MB strings
        # this eviction frees. Pressure-evictable agents have flushed by definition, so it's populated.
        if hasattr(agent, "_db_flush_scan_prefix"):
            agent._db_flush_scan_prefix = None

    def _agent_cache_bounds(self):
        """Operator-configured agent-cache bounds, resolved once per process (lazily, not in
        ``__init__``, so ``__new__``-constructed test / slash-command runners work too)."""
        from gateway.run import _load_gateway_config
        bounds = getattr(self, "_agent_cache_bounds_cache", None)
        if bounds is None:
            from gateway.agent_cache_pressure import resolve_agent_cache_bounds
            try:
                bounds = resolve_agent_cache_bounds(_load_gateway_config())
            except Exception as _e:
                logger.debug("Agent cache bounds config read failed: %s", _e)
                # Resolve from an empty config rather than bare AgentCacheBounds(): the dataclass default
                # has memory_high_mb=None (pressure pass OFF) but an *absent* section means "auto" — a
                # transient config read failure must not permanently disable the OOM valve.
                bounds = resolve_agent_cache_bounds({})
            self._agent_cache_bounds_cache = bounds
        return bounds

    def _agent_cache_cap(self) -> int:
        """Effective LRU cap — the configured override, else the default."""
        from gateway.run import _AGENT_CACHE_MAX_SIZE
        return self._agent_cache_bounds().max_size or _AGENT_CACHE_MAX_SIZE

    def _agent_cache_idle_ttl(self) -> float:
        """Effective idle TTL in seconds — configured override, else default."""
        from gateway.run import _AGENT_CACHE_IDLE_TTL_SECS
        return self._agent_cache_bounds().idle_ttl_secs or _AGENT_CACHE_IDLE_TTL_SECS

    def _sweep_agent_cache_under_pressure(self) -> int:
        """Shed cached transcripts once the gateway heap nears its budget; returns count evicted.

        The LRU cap counts entries and the idle sweep counts seconds; neither knows one cached agent
        pins a full ``_session_messages`` transcript (tens of MB), so RSS climbs until the cgroup
        throttles. Above the anonymous-RSS budget this soft-evicts LRU agents (transcript rebuilt
        from the persisted session next turn). Never touched: agents mid-turn, the most recently
        used sessions, and transcripts not yet on disk.

        A gateway serving many chats can hold every warm transcript for the TTL window.
        Pressure eviction bounds that heap before the cgroup throttles and SIGTERM can
        no longer flush inside systemd's stop timeout (#80764).
        """
        from gateway.run import _AGENT_PENDING_SENTINEL
        from gateway.agent_cache_pressure import (
            plan_pressure_evictions, read_anon_rss_mb, transcript_persistence_caught_up
        )
        bounds = self._agent_cache_bounds()
        _cache = getattr(self, "_agent_cache", None)
        _lock = getattr(self, "_agent_cache_lock", None)
        # Nothing cached — whatever is using the heap, it isn't us, and warning about it every tick
        # would point at the wrong subsystem.
        if not bounds.memory_high_mb or not _cache or _lock is None:
            return 0
        rss_mb = read_anon_rss_mb()
        if rss_mb is None or rss_mb < bounds.memory_high_mb:
            return 0
        running_ids = self._running_agent_ids()

        def _is_live(agent: Any) -> bool:
            return agent is not None and agent is not _AGENT_PENDING_SENTINEL and id(agent) not in running_ids

        def _is_evictable(key: str, agent: Any) -> bool:
            return _is_live(agent) and transcript_persistence_caught_up(agent)

        with _lock:
            ordered = [(key, _first_agent(entry)) for key, entry in _cache.items()]
            plan = plan_pressure_evictions(
                ordered, is_evictable=_is_evictable, max_evictions=bounds.max_evictions_per_pass,
                protect_recent=bounds.protect_recent,
            )
            for key, _ in plan:
                _cache.pop(key, None)
        if not plan:
            _mid_turn = sum(1 for _, a in ordered if a is not None and id(a) in running_ids)
            _unflushed = sum(1 for _, a in ordered if _is_live(a) and not transcript_persistence_caught_up(a))
            logger.warning(
                "Agent cache pressure: anon RSS %dMB over budget %dMB but no "
                "evictable session (%d cached, %d mid-turn, %d blocked on "
                "un-flushed persistence)%s",
                rss_mb, bounds.memory_high_mb, len(ordered), _mid_turn, _unflushed,
                (
                    " — transcripts are not reaching the session DB "
                    "(session persistence disabled or failing?); the memory "
                    "valve cannot shed sessions until they persist."
                    if _unflushed and not _mid_turn
                    else " — memory will keep climbing until those turns finish."
                ),
            )
            return 0
        evicted_count = len(plan)
        logger.warning(
            "Agent cache pressure: anon RSS %dMB over budget %dMB — evicting %d LRU session(s): %s",
            rss_mb, bounds.memory_high_mb, evicted_count, ", ".join(key for key, _ in plan),
        )
        try:
            threading.Thread(target=self._release_pressure_batch, args=(plan,), daemon=True,
                             name="agent-cache-pressure").start()
        except Exception:
            # Thread spawn failed (interpreter shutdown): release inline, unguarded (as on main).
            self._release_pressure_batch(plan)
        # _release_pressure_batch drains `plan` in place (so the trim runs with no lingering agent
        # refs) — len(plan) is 0 once the daemon thread finishes, hence the pre-captured count.
        return evicted_count

    def _release_pressure_batch(self, plan: List[tuple]) -> None:
        """Release a pressure-evicted batch sequentially on one daemon thread, then ``malloc_trim`` so
        RSS actually falls. The plan is drained (``pop`` + ``del``), not iterated, so no local
        reference pins evicted agents during ``gc.collect`` + trim (else the valve over-evicts)."""
        while plan:
            key, agent = plan.pop(0)  # FIFO — evict LRU-first order preserved
            try:
                # Pressure sweeps run from the unscoped housekeeping watcher: enter each owner's scope.
                self._run_release_in_profile_scope(self._commit_then_release_soft, (agent, key), key)
            except Exception as _e:
                logger.debug("Pressure release failed for %s: %s", key, _e)
            del agent
        with suppress(Exception):
            from hermes_cli.mem_trim import trim_memory
            trim_memory(force=True, reason="agent_cache_pressure")

    def _enforce_agent_cache_cap(self) -> None:
        """Evict oldest cached agents past the LRU cap (requires _agent_cache_lock); cleanup on a
        daemon thread. Mid-turn agents are SKIPPED, so the cache may stay over cap until the next
        insert."""
        _cache = getattr(self, "_agent_cache", None)
        # OrderedDict.popitem(last=False) pops oldest; plain dict lacks the arg so skip enforcement
        # if a test fixture swapped the cache type.
        if _cache is None or not hasattr(_cache, "move_to_end"):
            return
        # Snapshot of agent instances mid-turn, keyed by id() so lookup is O(1) and independent of
        # AIAgent.__eq__ (which MagicMock overrides in tests).
        running_ids = self._running_agent_ids()
        # Walk LRU → MRU; only the first (size - cap) LRU positions are candidates. An active slot is
        # SKIPPED rather than evicting a newer entry — that would penalise a fresh session (no cache
        # history) to protect a long-running one. Cache may stay over cap until the next insert.
        cap = self._agent_cache_cap()
        candidates = [(key, _tuple_agent(_cache.get(key))) for key in list(_cache.keys())[:max(0, len(_cache) - cap)]]
        evict_plan = [(key, agent) for key, agent in candidates if agent is None or id(agent) not in running_ids]
        for key, _ in evict_plan:
            _cache.pop(key, None)
        remaining_over_cap = len(_cache) - cap
        if remaining_over_cap > 0:
            logger.warning(
                "Agent cache over cap (%d > %d); %d excess slot(s) held by "
                "mid-turn agents — will re-check on next insert.",
                len(_cache), cap, remaining_over_cap,
            )
        for key, agent in evict_plan:
            logger.info("Agent cache at cap; evicting LRU session=%s (cache_size=%d)", key, len(_cache))
            if agent is not None:
                # Commit end-of-session memory, then soft-release, both on the daemon thread so the
                # (possibly network-bound) provider call never blocks the held cache lock.
                self._spawn_release_thread(self._commit_then_release_soft, (agent, key), f"agent-cache-evict-{key[:24]}",
                                           inline_fallback=False, session_key=key)

    def _sweep_idle_cached_agents(self) -> int:
        """Evict cached agents idle past the idle TTL (lock acquired internally; cleanup on daemon
        threads; mid-turn agents SKIPPED); returns the number evicted."""
        _cache = getattr(self, "_agent_cache", None)
        _lock = getattr(self, "_agent_cache_lock", None)
        if _cache is None or _lock is None:
            return 0
        now = time.time()
        idle_ttl = self._agent_cache_idle_ttl()
        to_evict: List[tuple] = []
        running_ids = self._running_agent_ids()
        with _lock:
            for key, entry in list(_cache.items()):
                agent = _tuple_agent(entry)
                if agent is None or id(agent) in running_ids:
                    continue  # mid-turn — don't tear it down
                last_activity = getattr(agent, "_last_activity_ts", None)
                if last_activity is None or (now - last_activity) <= idle_ttl:
                    continue
                to_evict.append((key, agent))
            for key, _ in to_evict:
                _cache.pop(key, None)
        for key, agent in to_evict:
            logger.info("Agent cache idle-TTL evict: session=%s (idle=%.0fs)", key, now - getattr(agent, "_last_activity_ts", now))
            self._spawn_release_thread(self._commit_then_release_soft, (agent, key), f"agent-cache-idle-{key[:24]}",
                                       inline_fallback=False, session_key=key)
        return len(to_evict)
