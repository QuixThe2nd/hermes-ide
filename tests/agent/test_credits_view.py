"""Removal contract: the credits/billing command surfaces are gone.

The old `/credits` and `/billing` commands (Nous portal top-up surfaces) were
removed with the portal purge; this pins that they stay removed.
"""

from __future__ import annotations


def test_credits_command_fully_removed():
    """`/credits` and the old `/billing` are gone entirely — not commands, not
    aliases."""
    from hermes_cli.commands import resolve_command, COMMAND_REGISTRY

    # Both old names resolve to nothing.
    assert resolve_command("credits") is None
    assert resolve_command("billing") is None
    # No standalone command for either remains in the registry.
    assert not any(c.name in ("credits", "billing") for c in COMMAND_REGISTRY)
    # And no command carries either as an alias.
    for c in COMMAND_REGISTRY:
        assert "credits" not in (c.aliases or ())
        assert "billing" not in (c.aliases or ())
