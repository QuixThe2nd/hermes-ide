"""CLI for ``hermes mission_control serve``."""

from __future__ import annotations

import argparse

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9136


def load_serve_defaults() -> dict:
    """Serve settings from the optional ``mission_control`` config section.

    Defaults live here in code (not in the core DEFAULT_CONFIG) so the
    plugin adds no core-file change and needs no config-version bump;
    ``load_config_readonly()`` deep-merges a user's config.yaml at read
    time and absent keys simply fall back. Only non-secret behavior
    settings belong here — a Discord token is a secret and is read only
    from the ``.env`` beside the profile database.
    """
    host = DEFAULT_HOST
    port = DEFAULT_PORT
    discord_sync = True
    try:
        from hermes_cli.config import cfg_get, load_config_readonly

        cfg = load_config_readonly() or {}
        raw_host = str(
            cfg_get(cfg, "mission_control", "host", default=DEFAULT_HOST)
        ).strip()
        if raw_host:
            host = raw_host
        try:
            port = int(
                cfg_get(cfg, "mission_control", "port", default=DEFAULT_PORT)
            )
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        if not 1 <= port <= 65535:
            port = DEFAULT_PORT
        discord_sync = bool(
            cfg_get(cfg, "mission_control", "discord_sync", default=True)
        )
    except Exception:
        pass
    return {"host": host, "port": port, "discord_sync": discord_sync}


def cmd_serve(args: argparse.Namespace) -> int:
    from plugins.mission_control import server

    defaults = load_serve_defaults()
    host = args.host if args.host else defaults["host"]
    port = args.port if args.port is not None else defaults["port"]
    argv = ["--host", host, "--port", str(port)]
    for extra in args.trusted_host or ():
        argv.extend(["--trusted-host", extra])
    if args.no_discord_sync or not defaults["discord_sync"]:
        argv.append("--no-discord-sync")
    server.main(argv)
    return 0


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="mission_control_command")
    serve = subs.add_parser(
        "serve",
        help="Serve the Mission Control web UI (loopback, unauthenticated)",
    )
    serve.add_argument(
        "--host",
        default=None,
        help="address to bind (default 127.0.0.1, loopback only). "
        "Binding a non-loopback address is explicit, UNAUTHENTICATED, "
        "and only safe on a trusted private network",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=None,
        help="listen port (default %d, or mission_control.port in "
        "config.yaml)" % DEFAULT_PORT,
    )
    serve.add_argument(
        "--no-discord-sync",
        action="store_true",
        help="disable the background Discord archive sync",
    )
    serve.add_argument(
        "--trusted-host",
        action="append",
        default=None,
        metavar="HOST",
        help="additional Host header value this server answers for "
        "(repeatable). By default only the bind address itself is "
        "trusted — plus, for a loopback or wildcard bind, the local "
        "machine's own addresses — and requests whose Host names "
        "anything else are refused with 421. Forwarded/X-Forwarded-* "
        "headers are never trusted",
    )


def mission_control_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "mission_control_command", None)
    if sub == "serve":
        return cmd_serve(args)
    print("usage: hermes mission_control serve")
    return 2
