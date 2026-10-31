"""Discord surface for the native secure-login entry flow (``gateway/secure_entry.py``).

A requester-only interactive prompt in the originating chat: only a click by
the initiating requester (exact ``interaction.user.id`` match — stricter than
the adapter-wide allowlist other Hermes views use, because this view's sole
purpose is to hand one human a bearer capability URL) mints the capability
and delivers the HTTPS form link EPHEMERALLY, in that same interaction. The
link never appears in a shared message, a model response, or a DM.

Reuses the adapter's existing prompt plumbing (``_send_prompt`` /
``_prompt_target_id``) — no framework changes, no parallel UI. The prompt's
buttons stay consistent with what the broker actually knows: after the form
link is sent the requester can still CANCEL (the pending request stays live
until redeemed/expired), and only a terminal state (cancel/timeout) disables
everything.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from agent.async_utils import safe_schedule_threadsafe
from gateway import secure_entry

try:
    import discord
except ImportError:  # pragma: no cover — view classes only exist with discord.py
    discord = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_PROMPT_TIMEOUT_S = 300  # absolute; matches the broker's hard TTL cap


class SecureEntryView(discord.ui.View):
    """Open-form / Cancel buttons, authorized for exactly the initiating requester."""

    def __init__(self, requester_user_id: str, request_id: str, base_origin: str, host: str):
        super().__init__(timeout=_PROMPT_TIMEOUT_S)
        self.requester_user_id = str(requester_user_id)
        self.request_id = request_id
        self.base_origin = base_origin
        self.host = host
        self.form_sent = False
        self.resolved = False

    def _authorized(self, interaction: "discord.Interaction") -> bool:
        return str(interaction.user.id) == self.requester_user_id

    def _open_button(self):
        return self.children[0] if self.children else None

    @discord.ui.button(label="🔐 Enter login securely", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: "discord.Interaction", _button):
        if self.form_sent or self.resolved:
            await interaction.response.send_message(
                "This prompt was already answered." if self.resolved
                else "Your secure form link was already sent — check this channel "
                     "(ephemeral message). Cancel below if you did not use it.",
                ephemeral=True)
            return
        if not self._authorized(interaction):
            # Unauthorized Discord users must never obtain the capability.
            await interaction.response.send_message("This prompt is not for you.", ephemeral=True)
            return
        capability = secure_entry.open_capability(self.request_id)
        if capability is None:
            self.resolved = True
            await interaction.response.send_message(
                "This save-login request is no longer active. Ask Hermes again.", ephemeral=True)
            await self._finish_prompt(interaction, footer="⌛ No longer active.")
            return
        url = secure_entry.form_url(self.base_origin, capability)
        # Ephemeral reply to the requester only, in the originating interaction.
        # If this send fails the minted capability must not stay usable.
        try:
            await interaction.response.send_message(
                f"One-time secure form for **{self.host}** (valid ≤ 5 minutes, single use):\n{url}\n"
                "Open it, enter your login once, and Hermes saves it to the encrypted vault. "
                "Do not share this link.",
                ephemeral=True)
        except Exception:
            secure_entry.cancel(self.request_id)
            self.form_sent = False
            try:
                await interaction.followup.send(
                    "Could not deliver the secure form link here. Ask Hermes again.", ephemeral=True)
            except Exception:
                pass
            return
        self.form_sent = True
        # The form is out, but Cancel must stay usable until the request is
        # actually redeemed/cancelled/expired: disable only the open button.
        open_btn = self.children[0] if self.children else None
        if open_btn is not None:
            open_btn.disabled = True
        await self._edit_prompt(interaction, footer="🔓 Secure form sent — visible only to you. "
                                                    "Cancel still works until you submit.")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_entry(self, interaction: "discord.Interaction", _button):
        if self.resolved:
            await interaction.response.send_message("This prompt was already answered.", ephemeral=True)
            return
        if not self._authorized(interaction):
            await interaction.response.send_message("This prompt is not for you.", ephemeral=True)
            return
        self.resolved = True
        # Bind the wording to the broker's ACTUAL outcome. cancel() returning
        # True means a live request was cancelled: it may be pre-submit, but it
        # also covers redeemed-but-unconsumed (the entry was received, yet the
        # save never happened) — the only truthful claim covering both is
        # "cancelled before saving". False means the request was already
        # consumed by the save (or expired): cancelling then cannot undo
        # anything, and claiming otherwise would be false exactly when it matters.
        cancelled = secure_entry.cancel(self.request_id)
        if cancelled:
            text = "🚫 Cancelled before saving."
        else:
            text = ("This save-login request is no longer active. If you already "
                    "submitted the form, Hermes received that entry and cancelling "
                    "now cannot undo it — check the chat for the save result.")
        await interaction.response.send_message(text, ephemeral=True)
        await self._finish_prompt(interaction, footer=("🚫 Cancelled before saving."
                                                        if cancelled else "⌛ No longer active."))

    async def _edit_prompt(self, interaction: "discord.Interaction", *, footer: str) -> None:
        """Edit the underlying prompt message over HTTP.

        Never a second ``interaction.response.*`` call — the interaction
        response was already consumed by the ephemeral reply, and Discord
        rejects a second response to the same interaction.
        """
        try:
            message = getattr(interaction, "message", None)
            if message is None:
                return
            embed = message.embeds[0] if message.embeds else None
            if embed is not None:
                embed.color = discord.Color.greyple()
                embed.set_footer(text=footer)
                await message.edit(embed=embed, view=self)
            else:
                await message.edit(view=self)
        except Exception:
            pass  # message deleted / too old to edit — the ephemeral reply already answered the user

    async def _finish_prompt(self, interaction: "discord.Interaction", *, footer: str) -> None:
        """Terminal state: disable every button and stop dispatching."""
        for child in self.children:
            child.disabled = True
        await self._edit_prompt(interaction, footer=footer)
        self.stop()

    async def on_timeout(self) -> None:
        self.resolved = True
        secure_entry.cancel(self.request_id)


def _build_prompt(channel, requester_user_id: str, request_id: str, base_origin: str, host: str):
    """``send_kwargs, view`` for ``DiscordAdapter._send_prompt``.

    The view MUST ride inside ``send_kwargs`` (``_send_prompt`` sends exactly
    ``channel.send(**send_kwargs)``) — returning it only as the second tuple
    element would send a prompt with no buttons at all.
    """
    embed = discord.Embed(
        title="🔐 Save login securely",
        description=(
            f"Hermes wants to save a login for **{host}**.\n"
            "Click below and a one-time secure form link — visible only to you — "
            "lets you enter the identifier and password once. The password never "
            "appears in this chat."),
        color=discord.Color.blurple())
    embed.set_footer(text="Valid for a few minutes · single use")
    view = SecureEntryView(requester_user_id, request_id, base_origin, host)
    return {"embed": embed, "view": view}, view


def deliver_secure_entry_prompt(adapter, loop, chat_id: str, metadata: dict,
                                requester_user_id: str, session_key: str,
                                origin: str, host: str, base_origin: str,
                                home: str = "", owner_token: str = "",
                                should_abort=None) -> Optional[Dict[str, str]]:
    """Called from the agent tool thread: send the requester-only prompt, then block
    until the HTTP form redeems (or timeout/cancel/turn end). Returns the secret
    dict for the ordinary vault save path — never persisted here."""
    if not home:
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home())

    req = secure_entry.create_request(
        home=home, session_key=session_key, requester_user_id=requester_user_id,
        origin=origin, host=host, owner_token=owner_token, should_abort=should_abort)
    if req is None:
        return None
    fut = safe_schedule_threadsafe(
        adapter._send_prompt(
            chat_id, metadata,
            lambda channel: _build_prompt(channel, requester_user_id, req.request_id, base_origin, host),
            fail_log="secure entry prompt"),
        loop)
    if fut is None:
        secure_entry.cancel(req.request_id)
        return None
    try:
        sent = fut.result(timeout=30)
    except Exception:
        secure_entry.cancel(req.request_id)
        return None
    if not getattr(sent, "success", False):
        # Delivery failed: fail closed, no usable unnoticed request left behind.
        secure_entry.cancel(req.request_id)
        return None
    return secure_entry.wait_for_result(req.request_id, _PROMPT_TIMEOUT_S + 15,
                                        should_abort=should_abort)
