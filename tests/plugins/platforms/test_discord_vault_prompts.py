"""Discord vault modal contracts against the real discord.py dependency."""

from types import SimpleNamespace
from typing import Any, cast

import pytest


def test_real_discord_text_input_cleanup_clears_submitted_values():
    discord = pytest.importorskip(
        "discord", reason="real discord.py is required for this contract"
    )
    assert getattr(discord, "__file__", None), (
        "the gateway discord fake is not real discord.py"
    )

    from plugins.platforms.discord.vault_prompts import DiscordVaultSaveModal

    assert discord.ui.TextInput.value.fset is None
    request = cast(
        Any,
        SimpleNamespace(modal_custom_id="vault-save-submit:test", request_id="test"),
    )
    modal = DiscordVaultSaveModal(request)
    identifier = "synthetic-real-discord-user@example.test"
    password = "synthetic-real-discord-password"
    cast(Any, modal.identifier)._refresh_state(None, {"value": identifier})
    cast(Any, modal.password)._refresh_state(None, {"value": password})

    modal._clear_fields()

    assert modal.identifier.value == ""
    assert modal.password.value == ""
    serialized = str(modal.to_dict())
    assert identifier not in serialized
    assert password not in serialized
