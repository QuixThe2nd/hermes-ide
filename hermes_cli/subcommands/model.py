"""``hermes model`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_model_parser(subparsers, *, cmd_model: Callable) -> None:
    """Attach the ``model`` subcommand to ``subparsers``."""
    model_parser = subparsers.add_parser(
        "model", help="Select default model and provider",
        description="Interactively select your inference provider and default model")
    model_parser.add_argument(
        "--refresh", action="store_true",
        help="Wipe the model picker disk cache and re-fetch every provider's live /v1/models list.")
    model_parser.add_argument(
        "--no-browser", action="store_true",
        help="Print the OAuth sign-in URL instead of opening a browser automatically.")
    model_parser.add_argument(
        "--timeout", type=float, default=None,
        help="OAuth sign-in timeout in seconds (provider default when omitted).")
    model_parser.set_defaults(func=cmd_model)
