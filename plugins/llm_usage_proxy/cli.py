"""CLI for ``hermes llm_usage_proxy {status,enable,disable,reconcile,serve}``.

Key-manager mode adds the ``keys``/``callers``/``manage-keys`` verb groups:
the provider API keys live in one root-only store this CLI writes, and the
callers get local tokens to present to the proxy instead. Nothing here prints
a key or a token back except the one moment a caller token is minted.
"""

from __future__ import annotations

import argparse
import sys

from plugins.auto_update.platform import detect_install_scope, platform_supported
from plugins.llm_usage_proxy.config import (
    CONFIG_SECTION,
    load_llm_usage_proxy_config,
    manage_keys_enabled,
    plugin_explicitly_disabled,
)
from plugins.llm_usage_proxy.lifecycle import reconcile_proxy_on_load
from plugins.llm_usage_proxy.probe import probe_port_state
from plugins.llm_usage_proxy.systemd import (
    ProbeOutcome,
    ReconcileResult,
    format_status,
    profile_identity,
    probe_service_is_active,
    probe_service_is_enabled,
    reconcile_service,
    service_unit_path,
)


def _effective_enabled() -> bool:
    return not plugin_explicitly_disabled() and load_llm_usage_proxy_config()["enabled"]


def _management_failed(result: ReconcileResult, *, want_enabled: bool) -> bool:
    """True when reconcile did not achieve the intended proxy state.

    A port already held by a non-verifying listener is deliberately NOT a
    failure: this profile's proxy is not the one serving it, and adopting or
    fighting it would break the other owner. Stand-down is the correct
    outcome, so only a genuine failure of *this profile's* unit counts — that
    is what keeps a coexisting proxy from failing gateway start.
    """
    if not result.supported:
        return True
    if not result.enabled_known or not result.service_active_known:
        return True
    operational = [
        w
        for w in result.warnings
        if w.startswith("failed to") or w == "usage proxy unit enabled but not active"
    ]
    if want_enabled and result.port.occupied:
        return bool(operational)
    if want_enabled:
        if not result.enabled or not result.service_active:
            return True
        return bool(operational)
    if result.enabled or result.service_active:
        return True
    return bool(operational)


def _routing_state() -> dict:
    try:
        from hermes_cli.llm_usage_routes import routing_state

        return routing_state()
    except Exception:
        return {"active": False, "reason": "routing seam unavailable"}


def _live_result(cfg: dict, *, environ=None) -> ReconcileResult:
    """Assemble a status-only result without touching unit files."""
    scope = detect_install_scope()
    warnings: list[str] = []
    supported = bool(platform_supported() and scope is not None)
    if not supported:
        return ReconcileResult(
            supported=False,
            scope=None,
            changed=False,
            enabled=False,
            service_active=False,
            unit_installed=False,
            port=probe_port_state(
                cfg["port"],
                expect_identity=profile_identity(),
            ),
        )
    enabled_probe = probe_service_is_enabled(scope)
    active_probe = probe_service_is_active(scope)
    if enabled_probe.outcome == ProbeOutcome.QUERY_FAILED:
        warnings.append(
            f"failed to query enabled state: {enabled_probe.detail or 'unknown error'}"
        )
    if active_probe.outcome == ProbeOutcome.QUERY_FAILED:
        warnings.append(
            f"failed to query active state: {active_probe.detail or 'unknown error'}"
        )
    return ReconcileResult(
        supported=True,
        scope=scope,
        changed=False,
        enabled=enabled_probe.as_bool,
        service_active=active_probe.as_bool,
        unit_installed=service_unit_path(scope).is_file(),
        port=probe_port_state(
            cfg["port"],
            expect_identity=profile_identity(),
        ),
        warnings=tuple(warnings),
        enabled_known=enabled_probe.known,
        service_active_known=active_probe.known,
        identity=profile_identity(),
    )


def cmd_status() -> int:
    if _owner_policy_or_none() is not None:
        # Shared-owner mode: this profile runs no unit of its own, so the
        # systemd-oriented status block would only mislead.
        return _print_owner_status()
    cfg = load_llm_usage_proxy_config()
    result = _live_result(cfg)
    print(format_status(result, cfg=cfg, routing=_routing_state()))
    print(f"  Config enabled: {'yes' if cfg['enabled'] else 'no'}")
    if plugin_explicitly_disabled():
        print("  Explicit disable: yes (config/plugins.disabled)")
    if not cfg["enabled"]:
        print(
            "  Routing is off via explicit opt-out (enabled: false or the"
            " plugins deny-list). `hermes llm_usage_proxy enable` writes"
            " llm_usage_proxy.enabled: true into this profile's config."
        )
    return 0


def _print_owner_status() -> int:
    """Status block for shared-owner mode (no systemd involvement)."""
    from plugins.llm_usage_proxy.owner import (
        caller_token,
        describe_policy,
        owner_policy,
        verify_owner,
    )

    policy = owner_policy()
    if policy is None:
        print("Shared owner: not configured (per-profile mode).")
        print(
            "  Set one with `hermes llm_usage_proxy owner set --endpoint"
            " http://127.0.0.1:PORT --identity DIGEST [--enforce]`."
        )
        return 0
    print(f"Shared owner: {describe_policy(policy)}")
    cfg = load_llm_usage_proxy_config()
    print(f"  Config enabled: {'yes' if cfg['enabled'] else 'no'}")
    if plugin_explicitly_disabled():
        print("  Explicit disable: yes (config/plugins.disabled)")
    state = _routing_state()
    payload, detail = verify_owner(
        policy["endpoint"], expect_identity=str(policy.get("identity") or "")
    )
    if payload is None:
        print(f"  Verification: FAILED ({detail})")
        if policy.get("enforce"):
            print("  Enforced routing fails closed until the owner verifies.")
        else:
            print(
                "  Routing stands down (traffic direct and unmetered) until"
                " the owner verifies."
            )
        print(
            f"  Routing: {'active' if state.get('active') else 'inactive'}"
            f" — {state.get('reason') or 'no reason recorded'}"
        )
        return 1
    routes = payload.get("routes") or {}
    print(f"  Verification: ok — adopted {_plural(len(routes), 'route')}")
    for name in sorted(routes):
        print(f"    {name} -> {routes[name]}")
    print(
        f"  Routing: {'active' if state.get('active') else 'inactive'}"
        f" — {state.get('reason') or 'no reason recorded'}"
    )
    if caller_token():
        print("  Caller token: present (HERMES_USAGE_PROXY_CALLER_TOKEN)")
    else:
        print(
            "  Caller token: MISSING — set HERMES_USAGE_PROXY_CALLER_TOKEN in"
            " this profile's .env to a token minted on the owner with"
            " `hermes llm_usage_proxy callers create <name>`."
        )
    return 0


def cmd_owner(args: argparse.Namespace) -> int:
    from plugins.llm_usage_proxy.owner import owner_endpoint_parts

    action = getattr(args, "owner_command", None)
    if action == "set":
        endpoint = str(args.endpoint or "").strip().rstrip("/")
        identity = str(args.identity or "").strip()
        if owner_endpoint_parts(endpoint) is None:
            print(
                f"Invalid owner endpoint {endpoint!r}: must be a loopback"
                " origin with an explicit port (e.g. http://127.0.0.1:8790)."
            )
            return 2
        if not identity:
            print(
                "Missing --identity: a shared owner must be named by its"
                " identity digest (the `Identity:` line of"
                " `hermes llm_usage_proxy status` on the owner profile)."
            )
            return 2
        _save_section_flag("owner_endpoint", endpoint)
        _save_section_flag("owner_identity", identity)
        _save_section_flag("enforce", bool(args.enforce))
        reconcile_proxy_on_load()
        rc = _print_owner_status()
        if rc == 0:
            print(
                "Shared-owner routing configured. This profile installs no"
                " proxy service; every routed model call goes to the owner,"
                " presenting only this profile's caller token."
            )
        return rc
    if action == "clear":
        _delete_section_keys("owner_endpoint", "owner_identity", "enforce")
        try:
            from hermes_cli.llm_usage_routes import clear_route_table

            clear_route_table("shared owner cleared from config")
        except Exception:
            pass
        print(
            "Shared owner cleared; in-process routing stood down. This"
            " profile is back in per-profile mode — run"
            " `hermes llm_usage_proxy reconcile` (or `enable`) to install and"
            " route through a proxy of its own, if wanted."
        )
        return 0
    if action == "status":
        return _print_owner_status()
    print("usage: hermes llm_usage_proxy owner {set,clear,status}")
    return 2


def _save_enabled_flag(enabled: bool) -> None:
    _save_section_flag("enabled", enabled)


def _save_section_flag(key: str, value: object) -> None:
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    section = dict(cfg.get(CONFIG_SECTION) or {})
    section[key] = value
    cfg[CONFIG_SECTION] = section
    save_config(cfg)


def _delete_section_keys(*keys: str) -> None:
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    section = dict(cfg.get(CONFIG_SECTION) or {})
    for key in keys:
        section.pop(key, None)
    cfg[CONFIG_SECTION] = section
    save_config(cfg)


def _owner_policy_or_none():
    from plugins.llm_usage_proxy.owner import owner_policy

    try:
        return owner_policy()
    except Exception:
        return None


def _reconcile_after_config_change() -> ReconcileResult:
    result = reconcile_proxy_on_load()
    if result is None:
        result = reconcile_service(
            load_llm_usage_proxy_config(), enabled=_effective_enabled()
        )
    return result


def _manage_keys_hint(cfg: dict) -> None:
    if manage_keys_enabled(cfg):
        return
    print(
        "  Key-manager mode is off for this profile: the proxy is not"
        " injecting keys and accepts any caller. Turn it on with"
        " `hermes llm_usage_proxy manage-keys on`."
    )


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _read_key_value(raw: str) -> str:
    """``--key -`` reads one key from stdin so it never sits in argv/history."""
    if raw != "-":
        return raw.strip()
    line = sys.stdin.readline()
    if not line:
        raise ValueError("--key - read no key from stdin")
    return line.strip()


def _format_key_store() -> str:
    from plugins.llm_usage_proxy.server import KeyStore

    store = KeyStore()
    described = store.describe()
    lines = [f"Key store: {store.path}"]
    routes = described["routes"]
    callers = described["callers"]
    oauth = described.get("oauth") or {}
    if not routes and not callers and not oauth:
        lines.append("  (empty — no provider keys, no caller tokens)")
        return "\n".join(lines)
    if routes:
        lines.append(
            f"  Provider keys: {_plural(len(routes), 'route')}"
            " (fingerprint only — the key itself is never shown)"
        )
        for name, fingerprints in routes.items():
            lines.append(f"    {name}: {_plural(len(fingerprints), 'key')}")
            for index, fingerprint in enumerate(fingerprints, start=1):
                lines.append(f"      #{index} {fingerprint}")
    else:
        lines.append("  Provider keys: none")
    if oauth:
        lines.append(
            f"  OAuth-managed routes: {_plural(len(oauth), 'route')}"
            " (the proxy obtains and refreshes these via the canonical auth"
            " store; no key is stored here)"
        )
        for name, provider in sorted(oauth.items()):
            lines.append(f"    {name}: {provider}")
    if callers:
        lines.append(f"  Caller tokens: {_plural(len(callers), 'caller')}")
        for name, fingerprint in callers.items():
            lines.append(f"    {name} {fingerprint}")
    else:
        lines.append(
            "  Caller tokens: none — the proxy accepts any caller"
            " and attributes nothing"
        )
    return "\n".join(lines)


def cmd_keys(args: argparse.Namespace) -> int:
    from plugins.llm_usage_proxy.server import KeyStore, is_route_name

    action = getattr(args, "keys_command", None)
    store = KeyStore()
    try:
        if action == "set":
            route = args.route
            if not is_route_name(route):
                print(f"Invalid route name {route!r} (lowercase letters, digits, -).")
                return 2
            keys = [_read_key_value(value) for value in (args.key or [])]
            if not keys:
                print("Nothing to set: pass --key VALUE (repeatable) or --key -.")
                return 2
            stored = store.set_route_keys(route, keys)
            print(
                f"{route}: stored {_plural(len(stored), 'provider key')}"
                " (rotation order = the order shown by `keys list`)."
            )
        elif action == "list":
            print(_format_key_store())
            _manage_keys_hint(load_llm_usage_proxy_config())
            return 0
        elif action == "remove":
            route = args.route
            if args.index is None:
                if store.remove_route(route):
                    print(f"{route}: removed the route and all of its keys.")
                else:
                    print(f"{route}: no keys stored for that route.")
                    return 1
            else:
                removed = store.remove_route_key(route, int(args.index))
                if removed is None:
                    print(
                        f"{route}: no key #{args.index}. `keys list` numbers the"
                        " keys of a route from 1."
                    )
                    return 1
                print(f"{route}: removed key #{args.index}.")
        elif action == "set-oauth":
            route = args.route
            if not is_route_name(route):
                print(f"Invalid route name {route!r} (lowercase letters, digits, -).")
                return 2
            from hermes_cli.llm_proxy_oauth import OAUTH_PROVIDERS

            provider = str(args.provider or "").strip()
            if provider not in OAUTH_PROVIDERS:
                print(
                    f"Unknown OAuth provider {provider!r}; known:"
                    f" {', '.join(OAUTH_PROVIDERS)}."
                )
                return 2
            store.set_oauth_route(route, provider)
            print(
                f"{route}: OAuth-managed via {provider}. The proxy now"
                " obtains and refreshes upstream OAuth itself through the"
                " canonical auth store; callers present only their caller"
                " token."
            )
        elif action == "remove-oauth":
            route = args.route
            if store.remove_oauth_route(route):
                print(
                    f"{route}: OAuth management removed; the route falls"
                    " back to stored static keys or caller credentials."
                )
            else:
                print(f"{route}: no OAuth mapping stored for that route.")
                return 1
        else:
            print(
                "usage: hermes llm_usage_proxy keys"
                " {set,list,remove,set-oauth,remove-oauth}"
            )
            return 2
    except ValueError as exc:
        print(f"Failed: {exc}")
        return 2
    _manage_keys_hint(load_llm_usage_proxy_config())
    return 0


def cmd_callers(args: argparse.Namespace) -> int:
    from plugins.llm_usage_proxy.server import KeyStore

    action = getattr(args, "callers_command", None)
    store = KeyStore()
    try:
        if action == "create":
            token, rotated = store.create_caller(args.name)
            print(f"Caller token for {args.name}:")
            print(f"  {token}")
            print(
                "Shown once — the key store keeps the token itself, and only"
                " its fingerprint is ever printed again."
                + (" A previous token for this caller was replaced." if rotated else "")
            )
        elif action == "list":
            print(_format_key_store())
            _manage_keys_hint(load_llm_usage_proxy_config())
            return 0
        elif action == "remove":
            if store.remove_caller(args.name):
                print(f"{args.name}: caller token revoked.")
            else:
                print(f"{args.name}: no such caller.")
                return 1
        else:
            print("usage: hermes llm_usage_proxy callers {create,list,remove}")
            return 2
    except ValueError as exc:
        print(f"Failed: {exc}")
        return 2
    _manage_keys_hint(load_llm_usage_proxy_config())
    return 0


def cmd_manage_keys(args: argparse.Namespace) -> int:
    if _owner_policy_or_none() is not None:
        print(
            "This profile routes through a shared owner and runs no proxy of"
            " its own — key-manager mode belongs to the owner profile. Run"
            " this command there instead."
        )
        return 1
    want = str(args.state).strip().lower() in {"on", "true", "yes", "enable", "1"}
    _save_section_flag("manage_keys", want)
    result = _reconcile_after_config_change()
    print(format_status(result, routing=_routing_state()))
    if _management_failed(result, want_enabled=_effective_enabled()):
        return 1
    if want:
        print(
            "Key-manager mode on: the unit now runs with --manage-keys and"
            " reads provider keys from the key store. Clients must present a"
            " caller token; each ledger row names the caller. Routes with no"
            " stored key keep passing the client's credential through."
        )
    else:
        print(
            "Key-manager mode off: the proxy stopped injecting keys and"
            " accepts any caller again."
        )
    return 0


def cmd_enable() -> int:
    if _owner_policy_or_none() is not None:
        # Shared-owner mode needs no systemd unit on this profile: enabling
        # just (re)activates routing through the configured owner.
        _save_enabled_flag(True)
        reconcile_proxy_on_load()
        rc = _print_owner_status()
        if rc == 0:
            print(
                "Shared-owner routing enabled. Newly built model clients"
                " route through the owner; a gateway restart rebuilds any"
                " clients created before this change."
            )
        return rc
    if not platform_supported():
        print(
            "The bundled LLM usage proxy requires Linux with systemd; "
            "nothing was installed."
        )
        return 1
    _save_enabled_flag(True)
    result = reconcile_proxy_on_load()
    if result is None:
        result = reconcile_service(load_llm_usage_proxy_config(), enabled=True)
    print(format_status(result, routing=_routing_state()))
    if _management_failed(result, want_enabled=True):
        return 1
    print(
        "LLM usage proxy installed and started. Newly built model clients"
        " route through it; clients built before this change keep their"
        " direct transports until they are rebuilt (a gateway restart"
        " guarantees it)."
    )
    return 0


def cmd_disable() -> int:
    if _owner_policy_or_none() is not None:
        _save_enabled_flag(False)
        reconcile_proxy_on_load()
        print(
            "Shared-owner routing disabled; in-process routing stood down."
            " The owner proxy itself is untouched — it belongs to another"
            " profile."
        )
        return 0
    _save_enabled_flag(False)
    result = reconcile_proxy_on_load()
    if result is None:
        result = reconcile_service(load_llm_usage_proxy_config(), enabled=False)
    if result is not None and _management_failed(result, want_enabled=False):
        print(format_status(result, routing=_routing_state()))
        return 1
    print(
        "LLM usage proxy disabled; service stopped and in-process routing"
        " stood down. Routed requests stop immediately; clients that were"
        " talking to the proxy will reconnect directly on their next"
        " request."
    )
    return 0


def cmd_reconcile() -> int:
    if _owner_policy_or_none() is not None:
        # Re-verify the owner and re-adopt its route table; there is no unit
        # to rewrite on this side.
        reconcile_proxy_on_load()
        return _print_owner_status()
    result = reconcile_proxy_on_load()
    if result is None:
        result = reconcile_service(
            load_llm_usage_proxy_config(), enabled=_effective_enabled()
        )
    print(format_status(result, routing=_routing_state()))
    if not result.supported and platform_supported():
        return 1
    if _management_failed(result, want_enabled=_effective_enabled()):
        return 1
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the proxy in the foreground (no systemd required)."""
    from plugins.llm_usage_proxy.routes import build_route_table
    from plugins.llm_usage_proxy.server import main as server_main

    cfg = load_llm_usage_proxy_config()
    port = int(getattr(args, "port", None) or 0) or int(cfg["port"])
    argv = ["--port", str(port), "--identity", profile_identity()]
    if manage_keys_enabled(cfg):
        argv.append("--manage-keys")
    for name, base in sorted(build_route_table(cfg).items()):
        argv.extend(("--upstream", f"{name}={base}"))
    return server_main(argv)


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="llm_usage_proxy_command")
    subs.add_parser("status", help="Show proxy bind, SQLite path, and unit state")
    subs.add_parser("enable", help="Install and start the bundled proxy service")
    subs.add_parser("disable", help="Stop and disable the bundled proxy service")
    subs.add_parser(
        "reconcile",
        help="Rewrite the systemd unit idempotently (respects explicit disable)",
    )
    serve = subs.add_parser(
        "serve", help="Run the proxy in the foreground (no systemd)"
    )
    serve.add_argument("--port", type=int, default=None)

    keys = subs.add_parser(
        "keys",
        help="Manage the provider API keys the proxy injects (key-manager mode)",
    )
    keys_subs = keys.add_subparsers(dest="keys_command")
    keys_set = keys_subs.add_parser(
        "set", help="Set the provider keys for one route (replaces the list)"
    )
    keys_set.add_argument("route", help="Route name as shown by `keys list`/`status`")
    keys_set.add_argument(
        "--key",
        action="append",
        default=[],
        metavar="VALUE",
        help=(
            "Provider API key (repeatable for rotation; order is the rotation"
            " order). Pass `-` to read one key from stdin instead of argv."
        ),
    )
    keys_list = keys_subs.add_parser(
        "list", help="List routes and key fingerprints (never the keys)"
    )
    keys_remove = keys_subs.add_parser(
        "remove", help="Remove one key (--index) or a whole route"
    )
    keys_remove.add_argument("route", help="Route name")
    keys_remove.add_argument(
        "--index",
        type=int,
        default=None,
        metavar="N",
        help="1-based key number as shown by `keys list`; omit to drop the route",
    )
    keys_set_oauth = keys_subs.add_parser(
        "set-oauth",
        help=(
            "Mark a route OAuth-managed: the proxy obtains and refreshes the"
            " upstream OAuth credential itself via the canonical auth store"
        ),
    )
    keys_set_oauth.add_argument("route", help="Route name as shown by `keys list`/`status`")
    keys_set_oauth.add_argument(
        "--provider",
        required=True,
        help="OAuth provider, e.g. openai-codex or xai-oauth",
    )
    keys_remove_oauth = keys_subs.add_parser(
        "remove-oauth", help="Stop OAuth-managing a route"
    )
    keys_remove_oauth.add_argument("route", help="Route name")

    owner = subs.add_parser(
        "owner",
        help=(
            "Shared-owner mode: route this profile's model calls through a"
            " central proxy it does not run"
        ),
    )
    owner_subs = owner.add_subparsers(dest="owner_command")
    owner_set = owner_subs.add_parser(
        "set", help="Name the central proxy (endpoint + identity digest)"
    )
    owner_set.add_argument(
        "--endpoint",
        required=True,
        help="Loopback origin of the central proxy, e.g. http://127.0.0.1:8790",
    )
    owner_set.add_argument(
        "--identity",
        required=True,
        help=(
            "Expected identity digest of the central proxy (the `Identity:`"
            " line of `hermes llm_usage_proxy status` on the owner profile)"
        ),
    )
    owner_set.add_argument(
        "--enforce",
        action="store_true",
        help=(
            "Fail closed: model inference that cannot go through the owner"
            " errors instead of going direct"
        ),
    )
    owner_subs.add_parser(
        "clear", help="Remove the shared-owner config and stand routing down"
    )
    owner_subs.add_parser(
        "status", help="Verify the configured owner and show adopted routes"
    )

    callers = subs.add_parser(
        "callers",
        help="Manage the local caller tokens clients present to the proxy",
    )
    callers_subs = callers.add_subparsers(dest="callers_command")
    callers_create = callers_subs.add_parser(
        "create", help="Mint a caller token (printed once)"
    )
    callers_create.add_argument(
        "name", help="Name recorded in the ledger for whoever holds this token"
    )
    callers_subs.add_parser("list", help="List callers and token fingerprints")
    callers_remove = callers_subs.add_parser(
        "remove", help="Revoke a caller token"
    )
    callers_remove.add_argument("name", help="Caller name")

    manage = subs.add_parser(
        "manage-keys",
        help="Turn key-manager mode on or off (adds --manage-keys to the unit)",
    )
    manage.add_argument("state", help="on | off")


def llm_usage_proxy_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "llm_usage_proxy_command", None)
    if sub == "status":
        return cmd_status()
    if sub == "enable":
        return cmd_enable()
    if sub == "disable":
        return cmd_disable()
    if sub == "reconcile":
        return cmd_reconcile()
    if sub == "serve":
        return cmd_serve(args)
    if sub == "keys":
        return cmd_keys(args)
    if sub == "callers":
        return cmd_callers(args)
    if sub == "manage-keys":
        return cmd_manage_keys(args)
    if sub == "owner":
        return cmd_owner(args)
    print(
        "usage: hermes llm_usage_proxy"
        " {status,enable,disable,reconcile,serve,keys,callers,manage-keys,owner}"
    )
    return 2
