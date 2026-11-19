"""Gateway slash commands that switch or tune the model route: /model, /codex-runtime.

Split out of ``gateway/slash_commands.py``; bound onto ``GatewayRunner`` through
``GatewayModelCommandsMixin``. Origin internals are imported lazily inside the bodies to avoid
the import cycle. The pure logic is canonical in ``internal/modelcmd`` (Go); this module
calls its Python twin ``hermes_cli/modelcmd.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from typing import Any, Dict, Optional

from agent.i18n import t
from gateway.platforms.event import MessageEvent

logger = logging.getLogger("gateway.run")  # log-record parity with gateway/run.py


@dataclasses.dataclass
class _ModelSwitchContext:
    """Everything a /model switch needs beyond the target: current route + persistence policy."""

    session_key: str
    source: Any
    config_path: Any
    persist_global: bool
    one_turn: bool = False
    restore_snapshot: Optional[dict] = None
    current_model: str = ""
    current_provider: str = "openrouter"
    current_base_url: str = ""
    current_api_key: str = ""
    user_provs: Any = None
    custom_provs: Any = None
    excluded_provs: list = dataclasses.field(default_factory=list)

    def read_config(self) -> None:
        """Fill the current route from ``config_path``; fail-open to the defaults."""
        from gateway.run import _load_gateway_config
        try:
            cfg = _load_gateway_config(config_path=self.config_path)
            if not cfg:
                return
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, dict):
                self.current_model = model_cfg.get("default", "")
                self.current_provider = model_cfg.get("provider", self.current_provider)
                self.current_base_url = model_cfg.get("base_url", "")
            self.user_provs = cfg.get("providers")
            try:
                from hermes_cli.config import get_compatible_custom_providers
                self.custom_provs = get_compatible_custom_providers(cfg)
            except Exception:
                self.custom_provs = cfg.get("custom_providers")
            excl = cfg.get("model_catalog", {}).get("excluded_providers")
            if isinstance(excl, list):
                self.excluded_provs = excl
        except Exception:
            pass

    def apply_override(self, override: dict) -> None:
        """A session /model override supersedes the configured route."""
        if override:
            self.current_model = override.get("model", self.current_model)
            self.current_provider = override.get("provider", self.current_provider)
            self.current_base_url = override.get("base_url", self.current_base_url)
            self.current_api_key = override.get("api_key", self.current_api_key)


class GatewayModelCommandsMixin:
    """Model-route slash commands (/model, /codex-runtime)."""

    # ----------------------------------------------------------------- /model

    async def _handle_model_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /model under the switch lock BEFORE the first await, so concurrent
        commands commit in issue order (see ``_model_switch_lock``)."""
        async with self._model_switch_lock():
            return await self._handle_model_command_locked(event)

    async def _handle_model_command_locked(self, event: MessageEvent) -> Optional[str]:
        from gateway.run import _hermes_home
        from hermes_cli.model_switch import parse_model_switch_args, resolve_persist_behavior

        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(event.source)
        request = parse_model_switch_args(event.get_command_args().strip())  # single-owner parser
        if request.errors:
            return f"❌ {request.error_messages()[0]}"  # gateway decoration over canonical copy
        if request.reasoning_effort:
            return "❌ /model --reasoning is not available here; switch first, then set the effort."
        if request.force_refresh:  # bust the disk cache so the listing shows live data
            with contextlib.suppress(Exception):
                from hermes_cli.models import clear_provider_models_cache
                clear_provider_models_cache()
        source = event.source
        session_key = self._session_key_for_source(source)
        ctx = _ModelSwitchContext(
            session_key=session_key, source=source,
            config_path=(profile_home or _hermes_home) / "config.yaml",
            persist_global=resolve_persist_behavior(
                request.is_global, request.is_session, is_once=request.is_once,
                explicit_provider=request.explicit_provider),
            one_turn=request.is_once,
            restore_snapshot=self._snapshot_session_model_override(session_key) if request.is_once else None)
        ctx.read_config()
        ctx.apply_override(self._session_model_overrides.get(session_key, {}))
        if not request.target and not request.explicit_provider:
            return await self._model_listing_reply(event, ctx, profile_home)
        result, error = await self._perform_model_switch(ctx, request.target, request.explicit_provider)
        if error is not None:
            return error
        return await self._commit_model_switch_locked(result, ctx, source=source, picker=False)

    async def _perform_model_switch(self, ctx: _ModelSwitchContext, raw_input: str, explicit_provider):
        """Resolve a /model switch off-loop. Returns ``(result, None)`` or ``(None, error_text)``."""
        from hermes_cli.model_switch import switch_model

        # Off-loop: switch_model() can hit a synchronous models.dev fetch (15s) on a cold cache.
        result = await asyncio.to_thread(
            switch_model, raw_input=raw_input, current_provider=ctx.current_provider,
            current_model=ctx.current_model, current_base_url=ctx.current_base_url,
            current_api_key=ctx.current_api_key, is_global=ctx.persist_global,
            explicit_provider=explicit_provider, user_providers=ctx.user_provs,
            custom_providers=ctx.custom_provs)
        if not result.success:
            return None, t("gateway.model.error_prefix", error=result.error_message)
        return result, None

    def _switch_cached_agent_model(self, result, ctx: _ModelSwitchContext) -> Optional[str]:
        """In-place swap on the cached agent; returns the error reply when it failed. The agent
        rolls back to the OLD model/client and re-raises; the commit (DB, override, eviction,
        config) is aborted so the next message doesn't rebuild a broken agent."""
        cached_agent = self._cached_agent_for(ctx.session_key)
        if cached_agent is None:
            return None
        try:
            cached_agent.switch_model(
                new_model=result.new_model, new_provider=result.target_provider,
                api_key=result.api_key, base_url=result.base_url, api_mode=result.api_mode,
                capabilities=getattr(result, "runtime_capabilities", None))
        except Exception as exc:
            logger.warning("In-place model switch failed for cached agent: %s", exc)
            return t(
                "gateway.model.error_prefix",
                error=f"Model switch to {result.new_model} failed ({exc}); staying on {ctx.current_model}.")
        return None

    async def _record_model_switch(
        self, result, ctx: _ModelSwitchContext, *, source, one_turn: bool, picker: bool
    ) -> Optional[str]:
        """Persist a committed switch: session DB row, next-turn note, override map, config
        write-through. Returns the warning for a ``--global`` switch whose ``config.yaml`` write
        or stale-override cleanup failed (the switch then stays a session override), else ``None``."""
        from hermes_cli.model_switch import format_model_for_display, persist_model_selection

        # Session DB row so the dashboard shows the updated model (#34850).
        _sess_db = getattr(self, "_session_db", None)
        if _sess_db is not None:
            try:
                _sess_entry = await self.async_session_store.get_or_create_session(source)
                await _sess_db.update_session_model(
                    _sess_entry.session_id, result.new_model, provider=result.target_provider)
            except Exception as exc:
                logger.debug("Failed to persist model switch to DB: %s", exc)
        # Prepended to the next user message (a mid-history system message would break prompt
        # caching); display form strips opaque vendor prefixes.
        if not hasattr(self, "_pending_model_notes"):
            self._pending_model_notes: Dict[str, str] = {}
        self._pending_model_notes[ctx.session_key] = (
            f"[Note: model was just switched from {format_model_for_display(ctx.current_model)} to "
            f"{format_model_for_display(result.new_model)} "
            f"via {result.provider_label or result.target_provider}. "
            f"{'This override applies to the next turn only. ' if one_turn else ''}"
            f"Adjust your self-identification accordingly.]"
        )
        self._session_model_overrides[ctx.session_key] = {
            "model": result.new_model, "provider": result.target_provider, "api_key": result.api_key,
            "base_url": result.base_url, "api_mode": result.api_mode,
            "request_overrides": dict(result.request_overrides or {}),
            "capabilities": dict(result.runtime_capabilities or {}),
        }
        if one_turn:
            # A repeated --once before the turn runs must keep the EARLIEST snapshot: the later
            # command's snapshot is the first temporary model, not the user's standing override.
            self._claim_one_turn_restore(ctx.session_key, ctx.restore_snapshot)
        elif not picker and hasattr(self, "_pending_one_turn_model_restores"):
            self._pending_one_turn_model_restores.pop(ctx.session_key, None)
        # A --global switch has ONE durable authority: config.yaml. On success drop the session
        # override — a redundant copy would shadow later global changes after a restart (#100314);
        # on failure keep it (the switch survives as session-only).
        global_error: Optional[str] = None
        if ctx.persist_global:
            try:
                await asyncio.to_thread(persist_model_selection, result, ctx.config_path)
            except Exception as e:
                logger.warning("Failed to persist model switch: %s", e)
                global_error = f"config.yaml not updated ({str(e) or type(e).__name__})"
            else:
                try:
                    await self.async_session_store.set_model_override(ctx.session_key, None)
                except Exception as e:
                    global_error = f"saved to config.yaml, but the stale session override was not cleared ({e})"
                else:
                    self._session_model_overrides.pop(ctx.session_key, None)
        elif not one_turn:
            # Write-through so a session override survives a restart; --once must NOT (#29923).
            try:
                await self.async_session_store.set_model_override(
                    ctx.session_key, self._session_model_overrides[ctx.session_key])
            except Exception:
                logger.debug("Failed to persist session model override", exc_info=True)
        self._evict_cached_agent(ctx.session_key)  # next turn builds fresh from the override
        return global_error


    async def _commit_model_switch(
        self, result, ctx: _ModelSwitchContext, *, source, picker: bool = False
    ) -> str:
        """Entry for the picker callback, which fires outside ``_handle_model_command`` and takes
        the switch lock itself; the typed path already holds it."""
        async with self._model_switch_lock():
            return await self._commit_model_switch_locked(result, ctx, source=source, picker=picker)

    async def _commit_model_switch_locked(
        self, result, ctx: _ModelSwitchContext, *, source, picker: bool
    ) -> str:
        one_turn = False if picker else ctx.one_turn  # a picker pick never carries --once
        error = self._switch_cached_agent_model(result, ctx)
        if error is not None:
            return error
        global_error = await self._record_model_switch(result, ctx, source=source, one_turn=one_turn, picker=picker)
        # Confirmation via the Go-canonical twin (display form shortens vendor IDs); the
        # context length is best-effort.
        from hermes_cli.model_switch import format_model_for_display, resolve_display_context_length_async
        from hermes_cli.modelcmd import confirmation_record, render_confirmation

        ctx_len = 0
        with contextlib.suppress(Exception):
            ctx_len = await resolve_display_context_length_async(
                result.new_model, result.target_provider, model_info=result.model_info) or 0
        return render_confirmation(confirmation_record(
            format_model_for_display(result.new_model),
            result.provider_label or result.target_provider,
            tokens=ctx_len, warning=result.warning_message or "",
            scope="once" if one_turn else ("global" if ctx.persist_global else "session"),
            global_error=global_error or ""))

    def _model_switch_lock(self) -> asyncio.Lock:
        """Runner-wide lock over a /model command's read-resolve-commit: slash commands bypass the
        busy guard, so a second /model otherwise interleaves with the first's awaits (#100314).
        Lazy: the runner is built without __init__."""
        lock = self.__dict__.get("_model_switch_lock_obj")
        if lock is None:
            lock = self.__dict__["_model_switch_lock_obj"] = asyncio.Lock()
        return lock

    async def _send_model_picker(self, event: MessageEvent, source, adapter, session_key: str, listing_kwargs: dict, on_model_selected) -> bool:
        """Send the interactive /model picker; False when nothing was sent (text fallback).
        *source* is session-key-normalized so the picker's thread metadata lands where the next
        turn reads. Rows are trimmed via the twin's ``build_picker_rows`` (Go PickerOptions)."""
        from hermes_cli.model_switch_providers import list_picker_providers
        from hermes_cli.modelcmd import build_picker_rows
        try:  # off-loop: listing can hit a synchronous HTTP fetch on a stale cache (#41289)
            providers = await asyncio.to_thread(
                list_picker_providers, max_models=50, include_moa=True, **listing_kwargs)
        except Exception:
            providers = []
        providers = build_picker_rows(
            providers, current_provider=listing_kwargs.get("current_provider", ""))
        if not providers:
            return False
        result = await adapter.send_model_picker(
            chat_id=source.chat_id, providers=providers,
            current_model=listing_kwargs["current_model"],
            current_provider=listing_kwargs["current_provider"], session_key=session_key,
            on_model_selected=on_model_selected,
            metadata=self._thread_metadata_for_source(source, self._reply_anchor_for_event(event)))
        return bool(result.success)

    async def _model_listing_reply(
        self, event: MessageEvent, ctx: _ModelSwitchContext, profile_home
    ) -> Optional[str]:
        """``/model`` with no args: interactive picker where supported, else the Go-canonical text
        listing (current model, authed providers, usage)."""
        from hermes_cli.modelcmd import listing_record, render_model_listing
        from hermes_cli.model_switch_providers import list_authenticated_providers
        from hermes_cli.providers import get_label

        listing_kwargs = dict(
            current_provider=ctx.current_provider, current_base_url=ctx.current_base_url,
            current_model=ctx.current_model, user_providers=ctx.user_provs,
            custom_providers=ctx.custom_provs, excluded_providers=ctx.excluded_provs)
        adapter = self._delivery_adapter_for(ctx.source)
        if adapter is not None and getattr(type(adapter), "send_model_picker", None) is not None:
            async def _picker_switch(model_id: str, provider_slug: str) -> str:
                # The picker callback binds the raw event source (pre-normalization).
                result, error = await self._perform_model_switch(ctx, model_id, provider_slug)
                if error is not None:
                    return error
                return await self._commit_model_switch(result, ctx, source=event.source, picker=True)

            async def _on_model_selected(_chat_id: str, model_id: str, provider_slug: str) -> str:
                if profile_home is None:
                    return await _picker_switch(model_id, provider_slug)
                from gateway.run import _profile_runtime_scope
                with _profile_runtime_scope(profile_home):
                    return await _picker_switch(model_id, provider_slug)

            if await self._send_model_picker(event, ctx.source, adapter, ctx.session_key, listing_kwargs, _on_model_selected):
                return None  # Picker sent — adapter handles the response

        try:  # off-loop: listing can hit a stale-cache HTTP fetch
            providers = await asyncio.to_thread(list_authenticated_providers, max_models=5, **listing_kwargs)
        except Exception:
            providers = []
        return render_model_listing(listing_record(
            ctx.current_model or "unknown",
            get_label(ctx.current_provider) or ctx.current_provider or "unknown",
            providers))

    # -------------------------------------------------------------- /codex-runtime

    async def _handle_codex_runtime_command(self, event: MessageEvent) -> str:
        """Handle /codex-runtime; a real change evicts the cached agent so the new api_mode applies
        on the next message (avoids prompt-cache invalidation mid-session)."""
        from hermes_cli import codex_runtime_switch as crs

        new_value, errors = crs.parse_args(event.get_command_args().strip() if event else "")
        if errors:
            return "❌ " + "\n❌ ".join(errors)
        try:
            from hermes_cli.config import load_config, save_config
        except Exception as exc:
            return f"❌ Could not load config: {exc}"
        result = crs.apply(
            load_config(), new_value, persist_callback=(save_config if new_value is not None else None),
        )
        if result.success and new_value is not None and result.requires_new_session:
            try:
                self._evict_cached_agent(self._session_key_for_source(event.source))
            except Exception:
                logger.debug("could not evict cached agent after codex-runtime change", exc_info=True)
        return f"{'✓' if result.success else '✗'} {result.message}"
