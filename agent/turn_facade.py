"""Remaining ``AIAgent`` chat and Codex-turn forwarding mixins.

The ``run_conversation`` implementation lives on ``AIAgent``; its admission machinery is in
``agent.turn_facade_lease`` and the loop itself is in ``agent.conversation_loop``.
"""
from typing import Optional

from agent.lazy_forward import forward as _forward


class TurnFacadeMixin:
    """chat() and the Codex-turn forwarder."""

    def chat(self, message: str, stream_callback: Optional[callable] = None) -> str:
        """Final response string of one turn; ``stream_callback`` receives each text delta."""
        return self.run_conversation(message, stream_callback=stream_callback)["final_response"]

    _run_codex_app_server_turn = _forward("agent.codex_runtime", "run_codex_app_server_turn")
