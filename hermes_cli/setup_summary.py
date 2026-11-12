"""Setup-completion summary (tool availability + "Setup Complete!" banner). setup.py names are
resolved through the module object so test patches on ``hermes_cli.setup.<name>`` take effect."""

import logging
from tools import tool_backend_helpers
from hermes_cli import nous_subscription

logger = logging.getLogger("hermes_cli.setup")

_WEB_MISSING = ("EXA_API_KEY, PARALLEL_API_KEY, FIRECRAWL_API_KEY/FIRECRAWL_API_URL, TAVILY_API_KEY, "
                "PERPLEXITY_API_KEY, KEENABLE_API_KEY, or SEARXNG_URL")

_DONE_BANNER = (
    "┌─────────────────────────────────────────────────────────┐",
    "│              ✓ Setup Complete!                          │",
    "└─────────────────────────────────────────────────────────┘")
# (command, description) rows; the description carries its own alignment padding.
_EDIT_WIZARD_ROWS = (
    ("hermes setup", "          Re-run the full wizard"), ("hermes setup model", "    Change model/provider"),
    ("hermes setup terminal", " Change terminal backend"), ("hermes setup gateway", "  Configure messaging"),
    ("hermes setup tools", "    Configure tool providers"))
_EDIT_CONFIG_ROWS = (
    ("hermes config", "         View current settings"), ("hermes config edit", "    Open config in your editor"),
    ("hermes config set <key> <value>", ""))
_READY_ROWS = (
    ("hermes", "              Start chatting"), ("hermes gateway", "      Start messaging gateway"),
    ("hermes doctor", "       Check for issues"))


def _first_available_plugin_provider(registry: str, skip: str = None):
    """display_name of the first plugin-registered provider in ``agent.<registry>`` that reports
    available (fail-soft: any error means none), skipping ``skip``."""
    try:
        import importlib
        from hermes_cli.plugins import _ensure_plugins_discovered
        _ensure_plugins_discovered()
        for provider in importlib.import_module(f"agent.{registry}").list_providers():
            if provider.name == skip:
                continue
            try:
                if provider.is_available():
                    return provider.display_name
            except Exception:
                continue
    except Exception:
        pass
    return None


# ---- tool_status row builders: each takes (config, subscription_features) and returns
# a (name, available, hint) row or None (row omitted). Evaluated in _TOOL_ROW_BUILDERS order.

def _vision_row(config, feats):
    # Use the same runtime resolver as the actual vision tools.
    try:
        from agent.auxiliary_client import get_available_vision_backends
        ok = bool(get_available_vision_backends())
    except Exception:
        ok = False
    return ("Vision (image analysis)", ok, None if ok else "run 'hermes setup' to configure")


def _managed_or_provider_row(feature, name: str, managed_label: str, missing_hint: str):
    """Row for a Nous-manageable feature: managed > available (with provider) > missing hint."""
    if feature.managed_by_nous:
        return (f"{name} ({managed_label})", True, None)
    if feature.available:
        return (f"{name} ({feature.current_provider})" if feature.current_provider else name, True, None)
    return (name, False, missing_hint)


def _web_row(config, feats):
    # Web tools (Exa, Parallel, Firecrawl, Tavily, or Keenable)
    return _managed_or_provider_row(feats.web, "Web Search & Extract", "Nous subscription", _WEB_MISSING)


def _browser_row(config, feats):
    from tools.browser_camofox import get_camofox_url
    configured = bool(get_camofox_url())
    return ("Browser Automation (Camofox)", configured, None if configured else "set CAMOFOX_URL")


def _image_gen_row(config, feats):
    # FAL (direct or via Nous), or any plugin-registered provider (OpenAI, etc.)
    if feats.image_gen.managed_by_nous:
        return ("Image Generation (Nous subscription)", True, None)
    if feats.image_gen.available:
        return ("Image Generation", True, None)
    # Probe plugin-registered providers so OpenAI-only setups don't show as "missing FAL_KEY".
    backend = _first_available_plugin_provider("image_gen_registry", skip="fal")
    if backend:
        return (f"Image Generation ({backend})", True, None)
    return ("Image Generation", False, "FAL_KEY or OPENAI_API_KEY")


def _video_gen_row(config, feats):
    # Opt-in via `hermes tools` → Video Generation. Only show the row when a plugin reports
    # available so we don't badger users who don't care about video gen with a "missing" line.
    if feats.video_gen.managed_by_nous:
        return ("Video Generation (FAL via Nous subscription)", True, None)
    backend = _first_available_plugin_provider("video_gen_registry")
    return (f"Video Generation ({backend})", True, None) if backend else None


def _modal_row(config, feats):
    if feats.modal.managed_by_nous:
        return ("Modal Execution (Nous subscription)", True, None)
    if _setup.cfg_get(config, "terminal", "backend") == "modal":
        if feats.modal.direct_override:
            return ("Modal Execution (direct Modal)", True, None)
        return ("Modal Execution", False, "run 'hermes setup terminal'")
    if tool_backend_helpers.managed_nous_tools_enabled() and feats.nous_auth_present:
        return ("Modal Execution (optional via Nous subscription)", True, None)
    return None


def _home_assistant_row(config, feats):
    return ("Smart Home (Home Assistant)", True, None) if _setup.get_env_value("HASS_TOKEN") else None


def _skills_hub_row(config, feats):
    ok = bool(_setup.get_env_value("GITHUB_TOKEN"))
    return ("Skills Hub (GitHub)", ok, None if ok else "GITHUB_TOKEN")


def _always_on_rows(config, feats):
    # Terminal (system deps met), task planning (in-memory), skills (bundled + user-created).
    return [("Terminal/Commands", True, None), ("Task Planning (todo)", True, None),
            ("Skills (view, create, edit)", True, None)]


_TOOL_ROW_BUILDERS = (
    _vision_row, _web_row, _browser_row, _image_gen_row, _video_gen_row,
    _modal_row, _home_assistant_row, _skills_hub_row, _always_on_rows)


def _print_cmd_rows(rows):
    """Print (command, description) rows as '   <green cmd><desc>'."""
    for cmd, desc in rows:
        print(f"   {_setup.color(cmd, _setup.Colors.GREEN)}{desc}")


def _print_section_header(title):
    print(_setup.color("─" * 60, _setup.Colors.DIM), end="\n\n")
    print(_setup.color(title, _setup.Colors.CYAN, _setup.Colors.BOLD), end="\n\n")


def _print_setup_summary(config: dict, hermes_home):
    """Print the setup completion summary."""
    from hermes_constants import display_hermes_home as _dhh
    # Provider readiness — the one thing setup must produce. A user who cancelled the API-key
    # prompt mid-wizard used to exit "successfully" with NO working model; say so loudly.
    try:
        from hermes_cli.auth import resolve_provider
        resolve_provider()
    except Exception:
        print()
        _setup.print_warning("No inference provider is configured — Hermes cannot chat yet.")
        _setup._info("  Finish this one step with either of:",
              "    hermes model            (pick any provider/model)",
              "    hermes setup --portal   (Nous Portal OAuth, no API key)")

    print()
    _setup.print_header("Tool Availability Summary")

    tool_status = []
    subscription_features = nous_subscription.get_nous_subscription_features(config)
    for build in _TOOL_ROW_BUILDERS:
        row = build(config, subscription_features)
        tool_status.extend(row if isinstance(row, list) else [] if row is None else [row])

    available_count = sum(1 for _, avail, _ in tool_status if avail)
    _setup._info(f"{available_count}/{len(tool_status)} tool categories available:", None)
    for name, available, missing_var in tool_status:
        print(f"   {_setup.color('✓', _setup.Colors.GREEN)} {name}" if available else
              f"   {_setup.color('✗', _setup.Colors.RED)} {name} "
              f"{_setup.color(f'(missing {missing_var})', _setup.Colors.DIM)}")
    print()

    if available_count < len(tool_status):
        _setup.print_warning("Some tools are disabled. Run 'hermes setup tools' to configure them,")
        _setup.print_warning(f"or edit {_dhh()}/.env directly to add the missing API keys.")
        print()

    print()
    for line in _DONE_BANNER:
        print(_setup.color(line, _setup.Colors.GREEN))
    print()
    print(_setup.color(f"📁 All your files are in {_dhh()}/:", _setup.Colors.CYAN, _setup.Colors.BOLD), end="\n\n")
    for label, value in (("Settings:", f"  {_setup.get_config_path()}"), ("API Keys:", f"  {_setup.get_env_path()}"),
                         ("Data:", f"      {hermes_home}/cron/, sessions/, logs/")):
        print(f"   {_setup.color(label, _setup.Colors.YELLOW)}{value}")
    print()

    _print_section_header("📝 To edit your configuration:")
    _print_cmd_rows(_EDIT_WIZARD_ROWS)
    print()
    _print_cmd_rows(_EDIT_CONFIG_ROWS)
    print("                          Set a specific value\n\n   Or edit the files directly:")
    for path in (_setup.get_config_path(), _setup.get_env_path()):
        print(f"   {_setup.color(f'nano {path}', _setup.Colors.DIM)}")
    print()

    _print_section_header("🚀 Ready to go!")
    _print_cmd_rows(_READY_ROWS)
    print()


import hermes_cli.setup as _setup  # noqa: E402  (bottom: hermes_cli.setup imports this module)
