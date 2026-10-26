"""MemoryManager.recalled_count — the counted-heading metadata source.

``describe_recall`` renders the indicator status line; ``recalled_count``
summarizes the same provider counts as pure display metadata for the gateway's
counted injection heading (`🧠 Recalled N memories`). The contract these tests
lock: discrete counts sum across providers, providers without a count
contribute nothing (None stays None), failures are non-fatal, and a provider
whose indicator line is suppressed still contributes its count — the
``recall_indicator`` flag gates the status text, never the count.
"""
from typing import Optional

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider, RecallStatus


class _FakeProvider(MemoryProvider):
    """Provider with settable recall_status / recalled_count."""

    def __init__(self, name: str, status: Optional[RecallStatus] = None,
                 count: Optional[int] = None, *, raises: bool = False,
                 count_raises: bool = False):
        self._name = name
        self._status = status
        self._count = count
        self._raises = raises
        self._count_raises = count_raises

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str = "", **kwargs) -> None:
        pass

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, tool_name, args, **kwargs) -> str:
        return ""

    def recall_status(self) -> Optional[RecallStatus]:
        if self._raises:
            raise RuntimeError("boom")
        return self._status

    def recalled_count(self) -> Optional[int]:
        if self._count_raises:
            raise RuntimeError("count boom")
        if self._count is not None:
            return self._count
        status = self.recall_status()
        return status.count if status is not None else None


def test_no_providers_returns_none():
    assert MemoryManager().recalled_count() is None


def test_provider_without_count_returns_none():
    mgr = MemoryManager()
    mgr.add_provider(_FakeProvider("hindsight", status=None))
    assert mgr.recalled_count() is None


def test_provider_with_explicit_count():
    mgr = MemoryManager()
    mgr.add_provider(_FakeProvider("hindsight", count=3))
    assert mgr.recalled_count() == 3


def test_sums_discrete_counts_across_providers():
    mgr = MemoryManager()
    mgr.add_provider(_FakeProvider("builtin", status=RecallStatus("Notes", 2)))
    mgr.add_provider(_FakeProvider("hindsight", count=5))
    assert mgr.recalled_count() == 7


def test_count_zero_is_a_discrete_count():
    # count 0 rides through as an int; the GATEWAY heading gates on > 0.
    mgr = MemoryManager()
    mgr.add_provider(_FakeProvider("hindsight", count=0))
    assert mgr.recalled_count() == 0


def test_indicator_suppressed_provider_still_counts():
    # The Hindsight contract: recall_status() is None when recall_indicator
    # is off, but recalled_count() still reports the discrete count.
    mgr = MemoryManager()
    mgr.add_provider(_FakeProvider("hindsight", status=None, count=4))
    assert mgr.recalled_count() == 4


def test_default_derives_from_recall_status():
    # Providers that don't override recalled_count() get the base
    # implementation, which derives from recall_status().
    class _StatusOnlyProvider(_FakeProvider):
        recalled_count = MemoryProvider.recalled_count

    mgr = MemoryManager()
    mgr.add_provider(_StatusOnlyProvider("hindsight", status=RecallStatus("Hindsight", 6)))
    assert mgr.recalled_count() == 6


def test_failing_provider_is_skipped_not_fatal():
    mgr = MemoryManager()
    mgr.add_provider(_FakeProvider("builtin", raises=True, count_raises=True))
    mgr.add_provider(_FakeProvider("hindsight", count=1))
    assert mgr.recalled_count() == 1
