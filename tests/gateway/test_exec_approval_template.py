"""Invariants for the exec-approval template method on BasePlatformAdapter.

The choice set (once / session / always / deny, minus the persistent tiers under a smart deny)
used to be re-derived in every button adapter and needed three separate "same fix × N adapters"
sweeps. It now comes from one place; adapters only render it.
"""

from typing import Any, Dict

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, ExecApprovalPrompt, SendResult
from gateway.run_turn_runner import _renders_exec_approval_buttons


class _Plain(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    async def send(self, *a: Any, **k: Any) -> SendResult:
        return SendResult(success=True)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {}


class _Buttons(_Plain):
    def __init__(self):
        super().__init__()
        self.prompts: list = []

    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        self.prompts.append(prompt)
        return SendResult(success=True, message_id="m1")


"""Invariants for the exec-approval template surface on BasePlatformAdapter.

``send_exec_approval`` is a deprecated no-op in this fork (no human approval prompts), but the
runner still needs to distinguish adapters that would render buttons from plain ones.
"""

from typing import Any, Dict

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, ExecApprovalPrompt, SendResult
from gateway.run_turn_runner import _renders_exec_approval_buttons


class _Plain(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    async def send(self, *a: Any, **k: Any) -> SendResult:
        return SendResult(success=True)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {}


class _Buttons(_Plain):
    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        return SendResult(success=True, message_id="m1")


def test_runner_only_offers_buttons_to_adapters_that_render_them():
    """Plain adapters get the text ``/approve`` prompt; the base send_exec_approval must not make
    the runner believe every adapter has buttons (its default reports failure and the runner would
    log a spurious fallback on every approval)."""
    assert _renders_exec_approval_buttons(_Buttons) is True
    assert _renders_exec_approval_buttons(_Plain) is False

    class _DuckTyped:
        async def send_exec_approval(self, *a, **k):
            return SendResult(success=True)

    assert _renders_exec_approval_buttons(_DuckTyped) is True
    assert _renders_exec_approval_buttons(object) is False
