"""Session model restore, runtime snapshot/restore, and codex runtime handling for the interactive CLI

Mixin split out of ``cli.py``; bound onto ``HermesCLI`` via the MRO. cli.py-internal
symbols are imported LAZILY inside each method (``from cli import ...``) — the mixin
never imports ``cli`` at module load time (import cycle).

Tests drive the restore paths with bare stubs lacking most mixin methods, so shared steps are
module-level functions taking ``cli`` and siblings are called as ``HermesCLI.<name>(self, ...)``.
"""

from __future__ import annotations

import copy
import sys

from rich.markup import escape as _escape


# CLI-level fields describing the active model route; snapshotted before a one-turn
# override and restored wholesale on rollback. ``reasoning_config`` rides along because it is
# resolved per model: a failed swap must not leave the new model's effort behind with the old
# route.
_RUNTIME_FIELDS = (
    "model", "provider", "requested_provider", "_explicit_api_key", "_explicit_base_url",
    "api_key", "base_url", "api_mode", "reasoning_config")


def _runtime_fields(cli) -> dict:
    return {key: getattr(cli, key, None) for key in _RUNTIME_FIELDS}


def _resolve_cli_reasoning(cli) -> None:
    """Re-resolve the CLI-level ``reasoning_config`` for ``cli.model`` through the shared chokepoint
    (per-model ``reasoning_overrides`` > global ``agent.reasoning_effort``). Startup resolves it once
    for the launch model; every path that moves ``cli.model`` must call this BEFORE the agent branch,
    because a lazily built agent inherits this field — an always-thinking model then goes out with
    the launch model's effort and 400s (#112921, #96012). ``agent.switch_model`` re-resolves its own
    copy for the live-agent path."""
    from cli import CLI_CONFIG
    from hermes_constants import resolve_reasoning_config
    # getattr: tests drive /new unbound on a SimpleNamespace without ``model`` (blank -> config default).
    cli.reasoning_config = resolve_reasoning_config(CLI_CONFIG, getattr(cli, "model", None) or "")


def stored_session_route(session_meta, *, current_model, current_provider):
    """The route a resumed session should run on, or ``None`` when the stored one is absent or
    already current. Returns ``(model, provider, base_url, api_mode, provider_changed)``; the
    canonical row reader is ``SessionDB.session_gateway_runtime`` (``model_config.gateway_runtime``,
    else the TUI's top-level keys). Bare ``custom`` is healed because the CLI resolve path
    hard-fails on it (the TUI gateway keeps it when a base_url exists)."""
    stored_model = str((session_meta or {}).get("model") or "").strip()
    if not stored_model:
        return None
    from hermes_state import SessionDB as _SessionDB
    runtime = _SessionDB.session_gateway_runtime(session_meta)
    base_url = runtime.get("base_url") or None
    provider = _heal_bare_custom_provider(runtime.get("provider") or None, base_url=base_url, model=stored_model)
    # Retired Ox Alpha route: the v41 migration scrubbed it from config.yaml because every
    # request on it now fails. A session row persisted while the route was live must not
    # resurrect it over the migrated (promoted) config — resume on the ambient model.
    # Exact-route match only: a custom provider/base_url serving the same model id is a
    # different, still-valid route and keeps restoring normally.
    from hermes_cli.fallback_config import is_retired_ox_alpha_route

    if is_retired_ox_alpha_route(provider, stored_model, base_url):
        return None
    provider_changed = bool(provider) and provider != current_provider
    if stored_model == current_model and not provider_changed:
        return None
    api_mode = runtime.get("api_mode") or None
    # A row's api_mode/base_url were written for whichever model the session last ran. Providers that
    # pick the wire per model (OpenCode Zen/Go, Copilot, Nous) re-derive both from the stored model, or a
    # resumed opencode-go session keeps a MiniMax-era anthropic_messages route for a chat_completions
    # model (#96066).
    from hermes_cli.model_switch import model_derived_api_mode
    derived = model_derived_api_mode(provider or "", stored_model)
    if derived is not None:
        from hermes_cli.models import normalize_opencode_base_url
        api_mode = derived
        base_url = normalize_opencode_base_url(provider, api_mode, base_url) or None
    return stored_model, provider, base_url, api_mode, provider_changed


def _heal_bare_custom_provider(provider, *, base_url, model):
    """Bare ``custom`` is a billing class, not a routable identity: persisting/restoring it makes a
    later resume hard-fail once the config default leaves the custom endpoint. Recover the durable
    ``custom:<name>`` menu key from the endpoint, else drop the provider (None)."""
    if str(provider or "").strip().lower() != "custom":
        return provider
    try:
        # Heal bare "custom" persisted by older builds / gateway turns: it's the resolved billing class, not
        # a routable identity. (Stricter than the TUI gateway's recovery, which keeps bare "custom" when a
        # base_url exists — the CLI's resolve path would hard-fail on it, #14676.)
        from hermes_cli.runtime_provider import canonical_custom_identity
        return canonical_custom_identity(base_url=base_url or None, model=model or None) or None
    except Exception:
        return None


class CLIModelSwitchMixin:
    """Session model restore, runtime snapshot/restore, and codex runtime handling for the interactive CLI"""

    def _normalize_model_for_provider(self, resolved_provider: str) -> bool:
        """Normalize provider-specific model IDs and routing."""
        from cli import _split_model_config_default
        current_model = str(self.model or "").strip()
        if isinstance(self.model, dict):
            current_model, _ = _split_model_config_default(self.model)
        changed = False

        def _adopt(canonical, notice) -> None:
            """Adopt ``canonical`` when it differs; ``notice(new)`` builds the warning text."""
            nonlocal current_model, changed
            if canonical and canonical != current_model:
                if not self._model_is_default:
                    self._console_print(f"[yellow]⚠️  {notice(canonical)}[/]")
                self.model = canonical
                current_model = canonical
                changed = True

        def _adopt_with_mode(normalize, api_mode_of, notice) -> bool:
            """Provider families that also own the wire protocol: adopt id, then sync api_mode."""
            nonlocal changed
            try:
                _adopt(normalize(current_model), notice)
                resolved_mode = api_mode_of(current_model)
                if resolved_mode != self.api_mode:
                    self.api_mode = resolved_mode
                    changed = True
            except Exception:
                pass
            return changed

        try:
            from hermes_cli.model_normalize import (
                _AGGREGATOR_PROVIDERS, normalize_model_for_provider)
            if resolved_provider not in _AGGREGATOR_PROVIDERS:
                _adopt(
                    normalize_model_for_provider(current_model, resolved_provider),
                    lambda new: (
                        f"Normalized model '{current_model}' to '{new}' for {resolved_provider}."))
        except Exception:
            pass

        if resolved_provider == "copilot":
            from hermes_cli.models import copilot_model_api_mode, normalize_copilot_model_id
            return _adopt_with_mode(
                lambda m: normalize_copilot_model_id(m, api_key=self.api_key),
                lambda m: copilot_model_api_mode(m, api_key=self.api_key),
                lambda new: f"Normalized Copilot model '{current_model}' to '{new}'.")

        from hermes_cli.models import opencode_provider_family
        if opencode_provider_family(resolved_provider) is not None:
            from hermes_cli.models import normalize_opencode_model_id, opencode_model_api_mode
            return _adopt_with_mode(
                lambda m: normalize_opencode_model_id(resolved_provider, m),
                lambda m: opencode_model_api_mode(resolved_provider, m),
                lambda new: (
                    f"Stripped provider prefix from '{current_model}'; "
                    f"using '{new}' for {resolved_provider}."))

        if resolved_provider != "openai-codex":
            return changed

        # 1. Strip provider prefix ("openai/gpt-5.4" → "gpt-5.4")
        if "/" in current_model:
            slug = current_model.split("/", 1)[1]
            if not self._model_is_default:
                self._console_print(
                    f"[yellow]⚠️  Stripped provider prefix from '{current_model}'; "
                    f"using '{slug}' for OpenAI Codex.[/]")
            self.model = slug
            current_model = slug
            changed = True

        # 2. Replace untouched default with a Codex model
        if self._model_is_default:
            from hermes_cli.codex_models import DEFAULT_CODEX_MODELS, get_codex_model_ids

            fallback_model = DEFAULT_CODEX_MODELS[0]
            try:
                available = get_codex_model_ids(access_token=self.api_key if self.api_key else None)
                if available:
                    fallback_model = available[0]
            except Exception:
                pass
            if current_model != fallback_model:
                self.model = fallback_model
                changed = True
        return changed

    def _restore_session_model(self, session_meta: dict, *, quiet: bool = False) -> None:
        """Restore model/provider from the session DB row on every resume path.

        Skips when no model is recorded or the CLI got an explicit ``-m`` (user intent wins).
        A different stored provider gets its credentials re-resolved — the ambient ``api_key``
        must not be sent to the session's endpoint; on failure the ambient credentials are kept
        so the session still opens (the first turn surfaces the auth error).
        """
        from cli import logger
        if not (session_meta or {}).get("model") or getattr(self, "_explicit_model_override", False):
            return
        route = stored_session_route(session_meta, current_model=self.model, current_provider=self.provider)
        if route is None:
            return
        stored_model, stored_provider, stored_base_url, stored_api_mode, provider_changed = route
        from hermes_cli.local_runtime.endpoint import LLAMACPP_ALIASES
        managed = str(stored_provider or "").strip().lower() in LLAMACPP_ALIASES
        self.model = stored_model
        if stored_provider:
            self.provider = stored_provider
            self.requested_provider = stored_provider
            if stored_base_url and not managed:
                self.base_url = stored_base_url
            if stored_api_mode:
                self.api_mode = stored_api_mode
        if managed and not (getattr(self, "_explicit_base_url", None) and not provider_changed):
            # The supervisor owns the live port: last boot's loopback URL (an ephemeral fallback when
            # 18434 was busy) must not pin the resume onto a dead endpoint. A launch-time --base-url
            # for this same provider is user intent and keeps winning.
            self._explicit_api_key = None
            self._explicit_base_url = None
            try:
                from hermes_cli.runtime_provider import resolve_runtime_provider
                resolved = resolve_runtime_provider(requested=stored_provider, target_model=self.model or None)
                if resolved.get("api_key"):
                    self.api_key = resolved["api_key"]
                    self._credential_pool = resolved.get("credential_pool")
                if resolved.get("base_url"):
                    self.base_url = resolved["base_url"]
                if not stored_api_mode and resolved.get("api_mode"):
                    self.api_mode = resolved["api_mode"]
            except Exception:
                if stored_base_url:
                    self.base_url = stored_base_url
                logger.debug(
                    "Credential re-resolution for resumed session provider "
                    "%s failed; keeping ambient credentials",
                    stored_provider, exc_info=True)
        elif provider_changed:
            # Launch-time explicit overrides belong to the AMBIENT provider and would poison
            # _ensure_runtime_credentials for the restored one. api_key is never persisted to
            # the session DB — runtime provider resolution owns credentials.
            self._explicit_api_key = None
            self._explicit_base_url = stored_base_url
            try:
                from hermes_cli.runtime_provider import resolve_runtime_provider
                resolved = resolve_runtime_provider(requested=stored_provider, target_model=self.model or None)
                if resolved.get("api_key"):
                    self.api_key = resolved["api_key"]
                    self._credential_pool = resolved.get("credential_pool")
                if not stored_base_url and resolved.get("base_url"):
                    self.base_url = resolved["base_url"]
                if not stored_api_mode and resolved.get("api_mode"):
                    self.api_mode = resolved["api_mode"]
            except Exception:
                logger.debug(
                    "Credential re-resolution for resumed session provider "
                    "%s failed; keeping ambient credentials",
                    stored_provider, exc_info=True)
        _resolve_cli_reasoning(self)
        # Mid-chat /resume swaps the live agent; on startup --resume _init_agent picks up
        # self.model / self.provider / self.reasoning_config.
        if self.agent is not None:
            try:
                self.agent.switch_model(
                    new_model=self.model, new_provider=self.provider, api_key=self.api_key or "",
                    base_url=self.base_url or "", api_mode=self.api_mode or "")
            except Exception:
                logger.debug("In-place agent model swap on resume failed", exc_info=True)
        msg = f"Model restored from session: {stored_model}"
        if stored_provider:
            msg += f" ({stored_provider})"
        if quiet:
            print(msg, file=sys.stderr)
        else:
            self._console_print(f"[dim]{_escape(msg)}[/dim]")

    def _snapshot_model_runtime(self) -> dict:
        """Capture current CLI and agent model runtime for one-turn restore."""
        agent = getattr(self, "agent", None)
        # ``reasoning_config`` is a mutable dict: deepcopy it so a later in-place edit cannot alias the snapshot.
        return {
            **_runtime_fields(self),
            "reasoning_config": copy.deepcopy(getattr(self, "reasoning_config", None)),
            "agent_primary_runtime": copy.deepcopy(
                getattr(agent, "_primary_runtime", None)
            ) if agent is not None else None}

    def _restore_model_runtime_snapshot(self, snapshot: dict | None) -> None:
        """Restore a model runtime captured before a one-turn override."""
        from cli import logger
        if not snapshot:
            return
        for key in _RUNTIME_FIELDS:
            if key in snapshot:
                setattr(self, key, snapshot.get(key))

        agent = getattr(self, "agent", None)
        if agent is None:
            return
        if "reasoning_config" in snapshot:
            agent.reasoning_config = snapshot["reasoning_config"]
        primary = snapshot.get("agent_primary_runtime")
        if primary and hasattr(agent, "_restore_primary_runtime"):
            try:
                agent._primary_runtime = copy.deepcopy(primary)
                agent._fallback_activated = True
                agent._rate_limited_until = 0
                if agent._restore_primary_runtime():
                    return
            except Exception:
                logger.debug("CLI one-turn model restore via primary runtime failed", exc_info=True)
        if hasattr(agent, "switch_model"):
            try:
                agent.switch_model(
                    new_model=snapshot.get("model", ""), new_provider=snapshot.get("provider", ""),
                    api_key=snapshot.get("api_key", ""), base_url=snapshot.get("base_url", ""),
                    api_mode=snapshot.get("api_mode", ""),
                    capabilities=snapshot.get("capabilities"))
                if "reasoning_config" in snapshot:
                    agent.reasoning_config = snapshot["reasoning_config"]
            except Exception as exc:
                logger.warning("CLI one-turn model restore failed: %s", exc)

    @staticmethod
    def _compute_model_picker_viewport(
        selected: int, scroll_offset: int, n: int, term_rows: int, reserved_below: int = 6,
        panel_chrome: int = 6, min_visible: int = 3) -> tuple[int, int]:
        """Resolve (scroll_offset, visible) for a scrolling list panel's viewport
        (picker/palette). ``reserved_below`` matches the approval/clarify panels (input, status
        bar, separators); ``panel_chrome`` is borders + blanks + hint row. The offset slides to
        keep ``selected`` on screen."""
        max_visible = max(min_visible, term_rows - reserved_below - panel_chrome)
        if n <= max_visible:
            return 0, n
        visible = max_visible
        if selected < scroll_offset:
            scroll_offset = selected
        elif selected >= scroll_offset + visible:
            scroll_offset = selected - visible + 1
        return max(0, min(scroll_offset, n - visible)), visible

    def _handle_codex_runtime(self, cmd_original: str) -> None:
        """Handle /codex-runtime — toggle the codex app-server runtime opt-in.

        Usage:
            /codex-runtime                       — show current state
            /codex-runtime auto                  — Hermes default (chat_completions)
            /codex-runtime codex_app_server      — hand turns to codex subprocess
            /codex-runtime on / off              — synonyms for the above
        """
        from cli import _cprint
        from hermes_cli import codex_runtime_switch as crs

        parts = cmd_original.split(None, 1)
        new_value, errors = crs.parse_args(parts[1].strip() if len(parts) > 1 else "")
        if errors:
            for err in errors:
                _cprint(f"❌ {err}")
            return
        try:
            from hermes_cli.config import load_config, save_config
        except Exception as exc:
            _cprint(f"❌ could not load config: {exc}")
            return
        result = crs.apply(
            load_config(), new_value,
            persist_callback=(save_config if new_value is not None else None))
        prefix = "✓" if result.success else "✗"
        for line in result.message.splitlines():
            _cprint(f"  {prefix} {line}" if line.startswith("openai_runtime") else f"    {line}")
        if result.success and result.requires_new_session:
            _cprint("    Tip: `/new` starts a new session immediately.")
