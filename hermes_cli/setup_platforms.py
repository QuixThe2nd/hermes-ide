"""Messaging-platform setup wizards (webhooks and the ``hermes setup
gateway`` flow. setup.py re-exports the public names, and tests monkeypatch prompt/print/env
helpers on hermes_cli.setup, so those are imported lazily per function."""

import contextlib
import logging
import re
from pathlib import Path

logger = logging.getLogger("hermes_cli.setup")

_RULE = "━" * 50



def _profile_name_from_hermes_home(hermes_home) -> str | None:
    """Return the active profile name when HERMES_HOME is a profile dir."""
    return hermes_home.name if hermes_home.parent.name == "profiles" else None



def declines_reconfigure(label: str, question: str, *env_vars: str) -> bool:
    """True when any of ``env_vars`` is already set and the user does NOT want to reconfigure.

    Shared by the core wizards and every platform plugin's ``interactive_setup`` so the
    "already configured? Reconfigure? [y/N]" gate has one wording and one default.
    """
    from hermes_cli.setup import get_env_value, print_info, prompt_yes_no
    if not any(get_env_value(v) for v in env_vars):
        return False
    print_info(f"{label}: already configured")
    return not prompt_yes_no(question, False)


def save_prompted(env_var: str, question: str, *, password: bool = False, success_msg: str | None = None,
                   skip_msg: str | None = None, transform=None) -> str:
    """Prompt, persist the (optionally transformed) answer when non-empty, and report either way.

    ``success_msg`` may reference ``{value}``. Returns the raw answer ("" when skipped).
    """
    from hermes_cli.setup import print_success, print_warning, prompt, save_env_value
    value = prompt(question, password=password)
    if value:
        save_env_value(env_var, transform(value) if transform else value)
        if success_msg:
            print_success(success_msg.format(value=value))
    elif skip_msg:
        print_warning(skip_msg)
    return value


def _save_allowlist(env_var: str, users: str, success_msg: str) -> None:
    """Strip spaces, persist the allowlist, and confirm."""
    from hermes_cli.setup import print_success, save_env_value
    save_env_value(env_var, users.replace(" ", ""))
    print_success(success_msg)


def _prompt_allowlist(env_var: str, question: str, success_msg: str, open_msg: str, preset: str | None = None) -> str:
    """Persist ``preset`` (or the prompted answer) as an allowlist, warning when it stays open."""
    from hermes_cli.setup import print_info, prompt
    users = prompt(question) if preset is None else preset
    if users:
        _save_allowlist(env_var, users, success_msg)
    else:
        print_info(open_msg)
    return users.replace(" ", "")


def _save_port(env_var: str, value: str, default: str) -> None:
    """Persist ``value`` as an int port; warn (keeping ``default``) when it isn't one."""
    from hermes_cli.setup import print_success, print_warning, save_env_value
    if not value:
        return
    try:
        save_env_value(env_var, str(int(value)))
        print_success(f"Webhook port set to {value}")
    except ValueError:
        print_warning(f"Invalid port number, using default {default}")







# _setup_slack and _write_slack_manifest_and_instruct moved to the slack plugin:
# plugins/platforms/slack/adapter.py::interactive_setup (registered via setup_fn and dispatched through the
# plugin path). #41112 / #3823.
def _setup_webhooks():
    """Configure webhook integration."""
    from hermes_cli.setup import _info, print_header, print_success, print_warning, prompt, save_env_value
    print_header("Webhooks")
    if declines_reconfigure("Webhooks", "Reconfigure webhooks?", "WEBHOOK_ENABLED"):
        return
    print()
    print_warning("⚠  Webhook and SMS platforms require exposing gateway ports to the")
    print_warning("   internet. For security, run the gateway in a sandboxed environment")
    print_warning("   (Docker, VM, etc.) to limit blast radius from prompt injection.")
    print()
    _info("   Full guide: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/webhooks/", None)
    _save_port("WEBHOOK_PORT", prompt("Webhook port (default 8644)"), "8644")
    save_prompted("WEBHOOK_SECRET", "Global HMAC secret (shared across all routes)", password=True,
                   success_msg="Webhook secret saved",
                   skip_msg="No secret set — you must configure per-route secrets in config.yaml")
    save_env_value("WEBHOOK_ENABLED", "true")
    print()
    print_success("Webhooks enabled! Next steps:")
    from hermes_constants import display_hermes_home as _dhh
    _info(f"   1. Define webhook routes in {_dhh()}/config.yaml",
          "   2. Point your service (GitHub, GitLab, etc.) at:",
          "      http://your-server:8644/webhooks/<route-name>", None,
          "   Route configuration guide:",
          "   https://hermes-agent.nousresearch.com/docs/user-guide/messaging/webhooks/#configuring-routes",
          None,
          # Printed twice upstream; kept verbatim for output parity.
          "   Open config in your editor:  hermes config edit",
          "   Open config in your editor:  hermes config edit")


def _is_progress(status: str) -> bool:
    """A platform counts as configured unless its status says otherwise."""
    s = status.lower()
    return not (s == "not configured" or s.startswith(("partially", "plugin disabled")))


def _restart_running_gateway(any_messaging: bool, supports_systemd: bool) -> None:
    """Already running: offer a restart only when this pass may have changed platform config —
    a restart interrupts any active session, so it stays behind a prompt."""
    from hermes_cli.setup import print_error, prompt_yes_no
    from hermes_cli.gateway import (
        systemd_restart, launchd_restart, UserSystemdUnavailableError, SystemScopeRequiresRootError,
        _system_scope_wizard_would_need_root, _print_system_scope_remediation,
    )
    import platform as _platform
    if supports_systemd and _system_scope_wizard_would_need_root():
        _print_system_scope_remediation("restart")
        return
    if not (any_messaging and prompt_yes_no("  Restart the gateway to pick up changes?", True)):
        return
    try:
        if supports_systemd:
            systemd_restart()
        elif _platform.system() == "Darwin":
            launchd_restart()
        elif _platform.system() == "Windows":
            from hermes_cli import gateway_windows
            gateway_windows.restart()
    except UserSystemdUnavailableError as e:
        print_error("  Restart failed — user systemd not reachable:")
        for line in str(e).splitlines():
            print(f"  {line}")
    except SystemScopeRequiresRootError as e:
        # Defense in depth: a race (unit file appearing mid-run) can slip past the pre-check;
        # this used to sys.exit(1) the whole wizard.
        print_error(f"  Restart failed: {e}")
        _print_system_scope_remediation("restart")
    except Exception as e:
        print_error(f"  Restart failed: {e}")


def setup_gateway(config: dict):
    """Configure messaging platform integrations."""
    from hermes_cli.setup import _info, print_header, print_info, print_success, prompt_checklist
    from hermes_cli.gateway import _all_platforms, _platform_status, _configure_platform
    print_header("Messaging Platforms")
    _info("Connect to messaging platforms to chat with Hermes from anywhere.",
          "Toggle with Space, confirm with Enter.", None)
    platforms = _all_platforms()

    # Build checklist, pre-selecting already-configured platforms.
    statuses = [_platform_status(plat) for plat in platforms]
    items = [f"{plat['emoji']} {plat['label']}  ({status})" for plat, status in zip(platforms, statuses)]
    pre_selected = [i for i, status in enumerate(statuses) if status == "configured"]
    selected = prompt_checklist("Select platforms to configure:", items, pre_selected)
    if not selected:
        print_info("No platforms selected. Run 'hermes setup gateway' later to configure.")
    for idx in selected or ():
        _configure_platform(platforms[idx])

    # Any platform (built-in or plugin) configured in this pass — via ``_platform_status`` so
    # plugin platforms like IRC are counted without another hard-coded env-var list.
    any_messaging = any(_is_progress(_platform_status(p)) for p in _all_platforms())
    if any_messaging:
        print()
        print_info(_RULE)
        print_success("Messaging platforms configured!")

    # Gateway service setup runs UNCONDITIONALLY — a gateway with zero platforms is a supported
    # mode (cron keeps running; adapters come up once tokens are added via `hermes import` /
    # `hermes setup gateway`). Gating it on messaging config left install-then-import machines
    # with cron jobs and bot tokens but no process to serve them.
    from hermes_cli.gateway import _is_service_running, supports_systemd_services, ensure_gateway_service
    supports_systemd = supports_systemd_services()
    print()
    if _is_service_running():
        _restart_running_gateway(any_messaging, supports_systemd)
    else:
        # Not running: install (if needed) and start, no questions asked.
        ensure_gateway_service(context="setup")
    print_info(_RULE)
