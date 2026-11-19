"""Shared in-process Discord stubs for the vault save-login prompt tests.

Copies of the channel / interaction / store doubles used by
``tests/gateway/test_discord_vault_prompts.py`` so the hub-client and hub-RPC
vault test files can drive the real modal machinery without discord.py
networking. Synthetic credential values only.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

ORIGIN = "https://example.test"
PASSWORD = "synthetic-discord-vault-password"
IDENTIFIER = "synthetic-user@example.test"
REAL_TURN_TIMEOUT = 30


class RecordingStore:
    def __init__(self):
        self.items = []

    def add_item(self, kind, label, payload, *, origin):
        self.items.append({
            "kind": kind,
            "label": label,
            "payload": dict(payload),
            "origin": origin,
        })
        return SimpleNamespace(id="vault_synthetic_login")


class SentMessage:
    def __init__(self, channel, payload):
        self.id = 9000 + len(channel.sent)
        self.channel = channel
        self.payload = payload
        self.edits = []

    async def edit(self, **kwargs):
        self.payload.update(kwargs)
        self.edits.append(kwargs)
        return self


class Channel:
    def __init__(self, channel_id=4242):
        self.id = channel_id
        self.client = None
        self.sent = []
        self.prompt_available = asyncio.Event()
        self.interactions = []

    async def send(self, **kwargs):
        message = SentMessage(self, kwargs)
        self.sent.append(message)
        self.prompt_available.set()
        return message


class InteractionResponse:
    def __init__(self):
        self.messages = []
        self.modal = None
        self.modal_opened = asyncio.Event()

    async def send_modal(self, modal):
        self.modal = modal
        self.modal_opened.set()

    async def send_message(self, content, *, ephemeral=False, **kwargs):
        self.messages.append({"content": content, "ephemeral": ephemeral, **kwargs})


class Interaction:
    def __init__(self, channel, user_id, *, message=None, custom_id="", client=None):
        self.user = SimpleNamespace(id=int(user_id), roles=[])
        self.channel = channel
        self.channel_id = channel.id
        self.message = message
        self.client = client if client is not None else getattr(channel, "client", None)
        self.data = {"custom_id": custom_id} if custom_id else {}
        self.response = InteractionResponse()
        channel.interactions.append(self)


class RejectIngestion:
    """Any modal submission reaching conversation ingestion is a contract failure."""

    def __init__(self):
        self.called = False

    async def __call__(self, _event):
        self.called = True
        raise AssertionError("a vault modal submission reached conversation ingestion")


async def close_client():
    return None


def identity_dict(identity):
    """The owner identity fields exactly as the gateway relays them over the hub RPC."""
    return {
        "runtime_profile": str(identity.runtime_profile or "default"),
        "runtime_home": str(identity.runtime_home or ""),
        "transport_profile": str(identity.transport_profile or "default"),
        "authorization_home": str(identity.authorization_home or ""),
    }


async def open_modal(rig, prompt_message, *, user_id="777", channel=None, client=None,
                     custom_id=None):
    view = prompt_message.payload["view"]
    button = next(item for item in view.children if "open" in item.label.lower())
    interaction = Interaction(
        channel or rig.channel,
        user_id,
        message=prompt_message,
        custom_id=button.custom_id if custom_id is None else custom_id,
        client=client,
    )
    await button.callback(interaction)
    return interaction, interaction.response.modal


def populate_modal(modal, identifier=IDENTIFIER, password=PASSWORD, interaction=None):
    modal.identifier._refresh_state(interaction, {"value": identifier})
    modal.password._refresh_state(interaction, {"value": password})


async def submit_modal(rig, modal, prompt_message, *, identifier=IDENTIFIER,
                       password=PASSWORD, user_id="777", channel=None, client=None,
                       custom_id=None):
    interaction = Interaction(
        channel or rig.channel,
        user_id,
        message=prompt_message,
        custom_id=modal.custom_id if custom_id is None else custom_id,
        client=client,
    )
    populate_modal(modal, identifier, password, interaction)
    await modal.on_submit(interaction)
    return interaction


async def cancel_prompt(rig, prompt_message, *, user_id="777"):
    view = prompt_message.payload["view"]
    cancel = next(item for item in view.children if item.label == "Cancel")
    interaction = Interaction(
        rig.channel, user_id, message=prompt_message, custom_id=cancel.custom_id
    )
    await cancel.callback(interaction)
    return interaction


def assert_prompt_registries_empty(adapter):
    assert adapter._vault_prompt_bindings == {}
    assert adapter._vault_prompt_requests == {}
    assert adapter._vault_prompt_sessions == {}
