"""Per-session incognito command and inbound history gating.

``/incognito`` starts a NEW incognito session exactly like ``/new`` (fresh session id, empty
history) and then marks the new routing entry's metadata ``incognito=True`` before replying;
``/incognito status`` only reports. The flag rides ``SessionEntry.metadata`` (state.db
gateway_routing plus the legacy sessions.json mirror), so it survives gateway restarts without
changing global config. It is never changed mid-session: ``/new`` creates a fresh metadata-empty
entry and therefore returns to normal mode.

Consumers:
- ``run_turn_runner._run_conversation_with_approval`` copies the flag onto ``agent._incognito``
  before every turn; turn-context gates external recall/prefetch/indicators, completed-turn
  sync/queued prefetch are suppressed, and the memory manager skips provider
  sync/end-of-session/pre-compress extraction while the flag is set.
- Inbound text preparation drops unbounded ``channel_context`` and ``reply_to_text`` unless an
  adapter proves the block bounded with process-local event flags.
- Discord thread backfill is bounded inclusively to the thread starter; non-thread incognito
  history is omitted. Built-in MEMORY.md/USER.md prompt injection is unchanged.
"""

from __future__ import annotations

from gateway.platforms.base import EphemeralReply
from gateway.platforms.event import MessageEvent

INCOGNITO_METADATA_KEY = "incognito"


class GatewayIncognitoMixin:
    """Runner surface for incognito state, command handling, and inbound gating."""

    def _is_session_incognito(self, source) -> bool:
        """Whether the current routing entry for this source is explicitly incognito."""
        store = getattr(self, "session_store", None)
        if store is None:
            return False
        try:
            session_key = self._session_key_for_source(source)
            return store.get_session_metadata(session_key, INCOGNITO_METADATA_KEY, False) is True
        except Exception:
            return False

    def _apply_incognito_inbound_gate(self, event: MessageEvent) -> None:
        """Drop history/reply blocks whose adapter did not prove an incognito-safe bound."""
        source = getattr(event, "source", None)
        if source is None or not self._is_session_incognito(source):
            return
        if not getattr(event, "_incognito_context_bounded", False):
            event.channel_context = None
        if not getattr(event, "_incognito_reply_bounded", False):
            event.reply_to_text = None

    async def _handle_incognito_command(self, event: MessageEvent):
        """Start a new incognito session: the /new reset path, then flag the new entry.

        ``status`` only reports; any other argument rides through to the reset path like
        ``/new``'s optional ``[name]`` title argument."""
        from agent.i18n import t

        if (event.get_command_args() or "").strip().lower() == "status":
            return EphemeralReply(
                t("gateway.incognito.status_on") if self._is_session_incognito(event.source)
                else t("gateway.incognito.status_off")
            )

        reply = await self._handle_reset_command(event)
        session_key = self._session_key_for_source(event.source)
        persisted = await self.async_session_store.set_session_metadata(
            session_key, INCOGNITO_METADATA_KEY, True,
        )
        if not persisted:
            return EphemeralReply(t("gateway.incognito.write_failed"))
        body = getattr(reply, "text", reply)
        return EphemeralReply(f"{body}\n\n{t('gateway.incognito.note')}")
