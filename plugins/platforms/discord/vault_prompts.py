"""Single-use Discord button/modal transport for browser-vault save-login."""

from __future__ import annotations

import asyncio
import secrets
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import discord

from agent.async_utils import safe_schedule_threadsafe


_VAULT_PROMPT_TIMEOUT_SECONDS = 300.0
_ACTIVE_STATES = frozenset({"pending", "awaiting_submission"})


class VaultPromptState(str, Enum):
    PENDING = "pending"
    AWAITING_SUBMISSION = "awaiting_submission"
    SUBMITTED = "submitted"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    DISCONNECTED = "disconnected"
    FAILED = "failed"


@dataclass(frozen=True)
class VaultPromptOwner:
    user_id: str
    channel_id: str
    session_key: str
    runtime_profile: str
    runtime_home: Path
    transport_profile: str
    authorization_home: Path

    @property
    def registry_key(self) -> tuple[str, str]:
        return (str(self.runtime_home), self.session_key)


@dataclass
class _VaultPromptRequest:
    request_id: str
    owner: VaultPromptOwner
    origin: str
    host: str
    transport: Any
    binding: "DiscordVaultPromptBinding"
    state: VaultPromptState = VaultPromptState.PENDING
    answer: Optional[Dict[str, str]] = field(default=None, repr=False)
    event: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    view: Optional["DiscordVaultSaveView"] = field(default=None, repr=False)
    modal: Optional["DiscordVaultSaveModal"] = field(default=None, repr=False)
    timeout_task: Optional[asyncio.Task] = None

    @property
    def active(self) -> bool:
        return self.state.value in _ACTIVE_STATES

    @property
    def open_custom_id(self) -> str:
        return f"vault-save-open:{self.request_id}"

    @property
    def cancel_custom_id(self) -> str:
        return f"vault-save-cancel:{self.request_id}"

    @property
    def modal_custom_id(self) -> str:
        return f"vault-save-submit:{self.request_id}"

    def clear_secrets(self) -> None:
        if self.answer is not None:
            self.answer.clear()
        self.answer = None
        if self.modal is not None:
            self.modal._clear_fields()


class DiscordVaultSaveModal(discord.ui.Modal):
    """The only path that returns modal values to the waiting vault callback."""

    def __init__(self, request: _VaultPromptRequest):
        super().__init__(
            title="Unmasked — Discord sees input", custom_id=request.modal_custom_id
        )
        self._request = request
        self.identifier = discord.ui.TextInput(
            label="Username or email (unmasked)",
            custom_id=f"vault-save-identifier:{request.request_id}",
            placeholder="Discord processes this input",
            required=True,
            max_length=320,
        )
        self.password = discord.ui.TextInput(
            label="Password — unmasked by Discord",
            custom_id=f"vault-save-password:{request.request_id}",
            placeholder="Local alternative: hermes vault add",
            required=True,
            max_length=4000,
        )
        self.add_item(self.identifier)
        self.add_item(self.password)

    def _clear_fields(self) -> None:
        for text_input in (self.identifier, self.password):
            # discord.py exposes value read-only; _refresh_state writes this backing field.
            text_input._value = None
            text_input.default = None

    async def _ack(self, interaction, content: str) -> None:
        try:
            await interaction.response.send_message(content, ephemeral=True)
        except Exception:
            # Discord owns delivery failures; no field value or raw interaction is logged.
            pass

    async def on_submit(self, interaction: discord.Interaction) -> None:
        request = self._request
        binding = request.binding
        answer: Optional[Dict[str, str]] = None
        try:
            if not await binding.authorize(
                request, interaction, request.modal_custom_id
            ):
                self._clear_fields()
                await self._ack(
                    interaction,
                    "This login prompt is not owned by you or is no longer active.",
                )
                return
            identifier = str(self.identifier.value or "").strip()
            password = str(self.password.value or "")
            self._clear_fields()
            if not identifier or not password:
                binding.finish(request, VaultPromptState.CANCELLED)
                await self._ack(
                    interaction,
                    "Both fields are required. This login prompt was closed without saving.",
                )
                return
            answer = {"identifier": identifier, "password": password}
            with binding.profile_scope(request.owner.runtime_home):
                accepted = binding.finish(
                    request, VaultPromptState.SUBMITTED, answer=answer
                )
            if not accepted:
                answer.clear()
                await self._ack(
                    interaction,
                    "This login prompt already closed. Nothing was submitted to Hermes.",
                )
                return
            await self._ack(
                interaction,
                "Submitted to Hermes for vault storage. No login values were posted to chat or model history.",
            )
        except Exception:
            self._clear_fields()
            if answer is not None:
                answer.clear()
            binding.finish(request, VaultPromptState.FAILED)
            await self._ack(
                interaction,
                "The login prompt failed closed. No values were retained by Hermes.",
            )


class DiscordVaultSaveView(discord.ui.View):
    def __init__(self, request: _VaultPromptRequest, timeout: float):
        super().__init__(timeout=timeout)
        self._request = request
        self._message = None
        open_button = discord.ui.Button(
            label="Open unmasked modal",
            style=discord.ButtonStyle.primary,
            custom_id=request.open_custom_id,
        )
        open_button.callback = self._open
        cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id=request.cancel_custom_id,
        )
        cancel_button.callback = self._cancel
        self.add_item(open_button)
        self.add_item(cancel_button)

    async def _ack(self, interaction, content: str) -> None:
        try:
            await interaction.response.send_message(content, ephemeral=True)
        except Exception:
            pass

    async def _open(self, interaction: discord.Interaction) -> None:
        request = self._request
        binding = request.binding
        try:
            if not await binding.authorize(
                request, interaction, request.open_custom_id
            ):
                await self._ack(
                    interaction,
                    "Only the requesting user can open this login prompt in its original channel.",
                )
                return
            if not binding.mark_modal_open(request):
                await self._ack(
                    interaction, "This login prompt already opened or closed."
                )
                return
            modal = DiscordVaultSaveModal(request)
            request.modal = modal
            await interaction.response.send_modal(modal)
        except Exception:
            binding.finish(request, VaultPromptState.FAILED)
            await self._ack(
                interaction,
                "The login prompt failed closed. No values were retained by Hermes.",
            )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        request = self._request
        try:
            if not await request.binding.authorize(
                request, interaction, request.cancel_custom_id
            ):
                await self._ack(
                    interaction,
                    "Only the requesting user can cancel this login prompt in its original channel.",
                )
                return
            if request.binding.finish(request, VaultPromptState.CANCELLED):
                await self._ack(
                    interaction,
                    "Login prompt cancelled. No values were sent to Hermes.",
                )
            else:
                await self._ack(interaction, "This login prompt is already closed.")
        except Exception:
            request.binding.finish(request, VaultPromptState.FAILED)
            await self._ack(
                interaction,
                "The login prompt failed closed. No values were retained by Hermes.",
            )

    async def on_timeout(self) -> None:
        self._request.binding.finish(self._request, VaultPromptState.EXPIRED)


class DiscordVaultPromptBinding:
    """Turn-scoped owner for one or more sequential save requests."""

    def __init__(
        self,
        adapter,
        *,
        source,
        identity,
        session_key: str,
        loop: asyncio.AbstractEventLoop,
        chat_id: Optional[str],
        metadata: Optional[Dict[str, Any]],
        authorize: Callable[[discord.Interaction], bool],
    ):
        from hermes_constants import get_hermes_home

        runtime_home = Path(identity.runtime_home or get_hermes_home())
        authorization_home = Path(identity.authorization_home or runtime_home)
        channel_id = str(
            (metadata or {}).get("thread_id") or chat_id or source.chat_id or ""
        )
        self.owner = VaultPromptOwner(
            user_id=str(source.user_id or ""),
            channel_id=channel_id,
            session_key=str(session_key),
            runtime_profile=str(identity.runtime_profile or "default"),
            runtime_home=runtime_home,
            transport_profile=str(identity.transport_profile or "default"),
            authorization_home=authorization_home,
        )
        self.adapter = adapter
        self.loop = loop
        self.metadata = dict(metadata or {})
        self.metadata["non_conversational"] = True
        self._authorize = authorize
        self._closed = False
        self._lock = threading.RLock()
        self._requests: Dict[str, _VaultPromptRequest] = {}
        self.binding_id = secrets.token_urlsafe(18)
        with adapter._vault_prompt_lock:
            adapter._vault_prompt_bindings[self.binding_id] = self

    @staticmethod
    def profile_scope(home: Path):
        from gateway.run import _profile_runtime_scope

        return _profile_runtime_scope(home)

    async def authorize(
        self, request: _VaultPromptRequest, interaction, custom_id: str
    ) -> bool:
        """Exact owner + channel + client + component check, then ordinary Discord admission."""
        user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "")
        channel_id = str(
            getattr(interaction, "channel_id", None)
            or getattr(getattr(interaction, "channel", None), "id", "")
            or ""
        )
        data = getattr(interaction, "data", None) or {}
        interaction_client = getattr(interaction, "client", None)
        if (
            request.transport is not self.adapter
            or user_id != request.owner.user_id
            or channel_id != request.owner.channel_id
            or data.get("custom_id") != custom_id
            or (
                interaction_client is not None
                and interaction_client is not self.adapter._client
            )
        ):
            return False
        with self.profile_scope(request.owner.authorization_home):
            return bool(self._authorize(interaction))

    def prompt(self, origin: str, host: str) -> Optional[Dict[str, str]]:
        with self._lock:
            if (
                self._closed
                or self.loop is None
                or self.loop.is_closed()
                or self.adapter._disconnecting
            ):
                return None
            request = _VaultPromptRequest(
                request_id=secrets.token_urlsafe(24),
                owner=self.owner,
                origin=origin,
                host=host,
                transport=self.adapter,
                binding=self,
            )
            self._requests[request.request_id] = request
        if not self._register_request(request):
            self.finish(request, VaultPromptState.CANCELLED)
            return None

        publish = safe_schedule_threadsafe(
            self._publish(request),
            self.loop,
            log_message="Discord vault prompt scheduling failed",
        )
        if publish is None:
            self.finish(request, VaultPromptState.FAILED)
            return None
        try:
            publish.result(timeout=10)
        except TimeoutError:
            # A late send is ambiguous; the independent owner timeout remains authoritative.
            pass
        except Exception:
            self.finish(request, VaultPromptState.FAILED)
            return None
        if not request.event.wait(
            timeout=max(1.0, _VAULT_PROMPT_TIMEOUT_SECONDS + 5.0)
        ):
            self.finish(request, VaultPromptState.EXPIRED)
        with request.lock:
            answer = (
                request.answer if request.state is VaultPromptState.SUBMITTED else None
            )
            request.answer = None
            with self._lock:
                self._requests.pop(request.request_id, None)
            return answer

    def _register_request(self, request: _VaultPromptRequest) -> bool:
        with self.adapter._vault_prompt_lock:
            if self._closed or not request.active:
                return False
            previous_id = self.adapter._vault_prompt_sessions.get(
                request.owner.registry_key
            )
            previous = (
                self.adapter._vault_prompt_requests.get(previous_id)
                if previous_id
                else None
            )
            self.adapter._vault_prompt_sessions[request.owner.registry_key] = (
                request.request_id
            )
            self.adapter._vault_prompt_requests[request.request_id] = request
        if previous is not None and previous is not request:
            previous.binding.finish(previous, VaultPromptState.CANCELLED)
        return True

    def _unregister_request(self, request: _VaultPromptRequest) -> None:
        with self.adapter._vault_prompt_lock:
            if self.adapter._vault_prompt_requests.get(request.request_id) is request:
                self.adapter._vault_prompt_requests.pop(request.request_id, None)
            if (
                self.adapter._vault_prompt_sessions.get(request.owner.registry_key)
                == request.request_id
            ):
                self.adapter._vault_prompt_sessions.pop(
                    request.owner.registry_key, None
                )

    async def _publish(self, request: _VaultPromptRequest) -> None:
        body = (
            f"Only the requesting user may open this one-time prompt for **{request.host}**.\n\n"
            "⚠️ **Discord text inputs are unmasked, and Discord processes your submission.** "
            "Use this only if that transport is acceptable for this password.\n"
            "Private local alternatives: `hermes vault add` or **Desktop → Settings → Passwords & Logins**.\n\n"
            "Hermes sends the fields directly to the existing vault callback, not through chat, the model, or history. "
            "Select **Open unmasked modal** to continue, **Cancel** to stop, or let this prompt expire in five minutes."
        )
        view = DiscordVaultSaveView(request, timeout=_VAULT_PROMPT_TIMEOUT_SECONDS)
        request.view = view

        def build(_channel):
            embed = discord.Embed(
                title=f"Save login for {request.host}",
                description=body,
                color=discord.Color.orange(),
            )
            content = self.adapter._self_contained_prompt_content(
                "🔐 **Save login — unmasked Discord input**", body
            )
            allowed_mentions = discord.AllowedMentions(
                users=False, roles=False, everyone=False, replied_user=False
            )
            return {
                "content": content,
                "embed": embed,
                "view": view,
                "allowed_mentions": allowed_mentions,
            }, view

        result = await self.adapter._send_prompt(
            request.owner.channel_id, self.metadata, build, fail_log="vault_save_login"
        )
        if not result.success:
            self.finish(request, VaultPromptState.FAILED)
            return
        await self.adapter._nonconversational_messages.mark_many([result.message_id])

        async def expire_after_timeout():
            try:
                await asyncio.sleep(_VAULT_PROMPT_TIMEOUT_SECONDS)
                self.finish(request, VaultPromptState.EXPIRED)
            except asyncio.CancelledError:
                pass

        with request.lock:
            if request.active:
                request.timeout_task = asyncio.create_task(expire_after_timeout())
                return
        self._render_terminal(request)

    def mark_modal_open(self, request: _VaultPromptRequest) -> bool:
        with request.lock:
            if request.state is not VaultPromptState.PENDING:
                return False
            request.state = VaultPromptState.AWAITING_SUBMISSION
            return True

    def finish(
        self,
        request: _VaultPromptRequest,
        state: VaultPromptState,
        *,
        answer: Optional[Dict[str, str]] = None,
    ) -> bool:
        if state.value in _ACTIVE_STATES:
            raise ValueError("terminal vault prompt state required")
        with request.lock:
            if not request.active:
                if answer is not None:
                    answer.clear()
                return False
            request.state = state
            if request.modal is not None:
                request.modal._clear_fields()
            if state is VaultPromptState.SUBMITTED and answer is not None:
                request.answer = answer
            else:
                request.clear_secrets()
                if answer is not None:
                    answer.clear()
            timeout_task = request.timeout_task
            request.timeout_task = None
            if state is not VaultPromptState.SUBMITTED:
                with self._lock:
                    self._requests.pop(request.request_id, None)
            self._unregister_request(request)
            request.event.set()
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if timeout_task is not None and timeout_task is not current_task:
            timeout_task.cancel()
        self._render_terminal(request)
        return True

    def _render_terminal(self, request: _VaultPromptRequest) -> None:
        view = request.view
        if view is None:
            return
        notices = {
            VaultPromptState.SUBMITTED: "Login submitted to Hermes; this prompt is closed.",
            VaultPromptState.CANCELLED: "Login prompt cancelled; no values were retained by Hermes.",
            VaultPromptState.EXPIRED: "Login prompt expired; no values were retained by Hermes.",
            VaultPromptState.DISCONNECTED: "Discord disconnected; the login prompt closed without saving.",
            VaultPromptState.FAILED: "Login prompt failed closed; no values were retained by Hermes.",
        }

        async def render():
            try:
                view.stop()
                for item in view.children:
                    item.disabled = True
                message = view._message
                if message is not None:
                    await message.edit(
                        content=notices[request.state], embed=None, view=view
                    )
            except Exception:
                # Terminal state and callback wakeup are already complete; edits are best effort.
                pass

        safe_schedule_threadsafe(
            render(),
            self.loop,
            log_message="Discord vault prompt terminal edit scheduling failed",
        )

    def close(self, state: VaultPromptState = VaultPromptState.CANCELLED) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            requests = list(self._requests.values())
            self._requests.clear()
        for request in requests:
            if request.active:
                self.finish(request, state)
            else:
                with request.lock:
                    request.clear_secrets()
        with self.adapter._vault_prompt_lock:
            if self.adapter._vault_prompt_bindings.get(self.binding_id) is self:
                self.adapter._vault_prompt_bindings.pop(self.binding_id, None)


def close_all_vault_prompts(adapter) -> None:
    """Wake every blocked worker before Discord transport teardown."""
    with adapter._vault_prompt_lock:
        bindings = list(adapter._vault_prompt_bindings.values())
    for binding in bindings:
        binding.close(VaultPromptState.DISCONNECTED)
    with adapter._vault_prompt_lock:
        adapter._vault_prompt_bindings.clear()
        adapter._vault_prompt_requests.clear()
        adapter._vault_prompt_sessions.clear()
