"""Gateway-side relay for browser-vault save-login prompts over the Discord hub.

The live Discord adapter in the gateway process is ``DiscordHubClientAdapter``:
it owns no discord.py transport, so the save-login button/modal runs in the hub
process and is reached through the hub RPC lane (the picker callbacks use the
same lane). This module holds ONLY the gateway half — no discord.py import, so
``hub_client`` stays importable without the dependency.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from agent.async_utils import safe_schedule_threadsafe

#: Public adapter RPC the hub dispatches to (``DiscordAdapter.vault_save_login_prompt``).
HUB_VAULT_PROMPT_RPC_METHOD = "vault_save_login_prompt"

#: How long the tool worker blocks on one hub-owned prompt. The hub's modal expires
#: after 300s, its worker adds a 5s slack, and the publish step is bounded at 10s —
#: this wait must cover ALL of that plus socket margin. A short default RPC timeout
#: here would cancel live prompts (the hub async ``_call`` lane itself has none).
#: Mirrors ``_VAULT_PROMPT_TIMEOUT_SECONDS`` in ``vault_prompts.py``, which cannot be
#: imported in this process because it pulls in discord.py.
_HUB_VAULT_PROMPT_WAIT_SECONDS = 320.0


@dataclass
class _PendingHubPrompt:
    """One in-flight RPC and the event its tool worker thread blocks on."""

    request_id: str
    event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    settled: bool = False
    answer: Optional[Dict[str, str]] = field(default=None, repr=False)
    rpc_future: Optional[Any] = field(default=None, repr=False)

    def settle(self, answer: Any) -> bool:
        """Wake the waiter exactly once; a late or duplicate answer is wiped."""
        with self.lock:
            if self.settled:
                if isinstance(answer, dict):
                    answer.clear()
                return False
            self.settled = True
            if isinstance(answer, dict):
                self.answer = answer
            else:
                self.answer = None
                if isinstance(answer, dict):
                    answer.clear()
            self.event.set()
            return True


class DiscordHubVaultPromptBinding:
    """Turn-scoped ``prompt(origin, host)`` / ``close()`` owner for hub-client turns.

    ``prompt`` runs on the tool worker thread: it schedules the hub RPC on the
    gateway loop (``safe_schedule_threadsafe``, the same pattern as
    ``DiscordVaultPromptBinding``) and blocks the worker on a ``threading.Event``
    until the hub submits, cancels, expires, errors, or the connection drops.
    The submission dict is handed only to the waiting vault callback; it never
    enters chat, model context, history, or logs.
    """

    def __init__(self, adapter, *, source, identity, session_key: str, loop,
                 chat_id=None, metadata: Optional[Dict[str, Any]] = None):
        self.adapter = adapter
        self.loop = loop
        self._session_key = str(session_key)
        self._chat_id = chat_id
        self._metadata = dict(metadata or {})
        self._source_data = (
            source.to_dict() if hasattr(source, "to_dict") else dict(source or {})
        )
        self._identity_data = {
            "runtime_profile": str(getattr(identity, "runtime_profile", "") or ""),
            "runtime_home": str(getattr(identity, "runtime_home", "") or ""),
            "transport_profile": str(getattr(identity, "transport_profile", "") or ""),
            "authorization_home": str(getattr(identity, "authorization_home", "") or ""),
        }
        self._lock = threading.RLock()
        self._closed = False
        self._pending: Dict[str, _PendingHubPrompt] = {}

    def prompt(self, origin: str, host: str) -> Optional[Dict[str, str]]:
        adapter = self.adapter
        with self._lock:
            if (
                self._closed
                or self.loop is None
                or self.loop.is_closed()
                or not getattr(adapter, "_running", False)
            ):
                return None
            pending = _PendingHubPrompt(request_id=secrets.token_urlsafe(24))
            self._pending[pending.request_id] = pending
        future = safe_schedule_threadsafe(
            self._request(pending, origin=str(origin or ""), host=str(host or "")),
            self.loop,
            log_message="Discord hub vault prompt scheduling failed",
        )
        if future is None:
            return self._abandon(pending)
        pending.rpc_future = future
        if not pending.event.wait(timeout=_HUB_VAULT_PROMPT_WAIT_SECONDS):
            # The hub keeps its own authoritative modal timeout; this backstop only
            # covers a lost reply, and cancels the RPC so no waiter lingers.
            return self._abandon(pending)
        with pending.lock:
            answer = pending.answer
            pending.answer = None
        with self._lock:
            self._pending.pop(pending.request_id, None)
        return answer

    def close(self) -> None:
        """Fail closed every still-blocked worker (scope teardown is idempotent)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending_list = list(self._pending.values())
            self._pending.clear()
        for pending in pending_list:
            self._abandon(pending)

    def _abandon(self, pending: _PendingHubPrompt) -> None:
        pending.settle(None)
        future = pending.rpc_future
        if future is not None:
            future.cancel()
        with self._lock:
            self._pending.pop(pending.request_id, None)
        return None

    async def _request(self, pending: _PendingHubPrompt, *, origin: str, host: str) -> None:
        answer: Any = None
        try:
            result = await self.adapter._call("rpc", {
                "token_key": self.adapter._token_key,
                "method": HUB_VAULT_PROMPT_RPC_METHOD,
                "kwargs": {
                    "source": dict(self._source_data),
                    "identity": dict(self._identity_data),
                    "session_key": self._session_key,
                    "chat_id": self._chat_id,
                    "metadata": dict(self._metadata),
                    "origin": origin,
                    "host": host,
                },
            })
        except asyncio.CancelledError:
            pending.settle(None)
            raise
        except Exception:
            # Disconnects and hub errors fail closed. RPC fields are never logged.
            pending.settle(None)
            return
        if isinstance(result, dict):
            answer = result.get("answer")
        pending.settle(answer)
