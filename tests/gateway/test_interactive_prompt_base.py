"""Tests for the shared interactive-prompt formatting cores in BasePlatformAdapter.

Covers ``_format_choice_page`` (picker pagination core) and byte-parity of the
rewired adapters (feishu/matrix) against their historical inline formatting.
"""

from gateway.platforms.base import BasePlatformAdapter


def _bare(cls):
    """Bare instance without running __init__ (documented test pattern)."""
    return object.__new__(cls)


class _DefaultAdapter(BasePlatformAdapter):
    """Concrete subclass using only base-class template attrs."""

    async def connect(self):  # pragma: no cover - not used
        pass

    async def disconnect(self):  # pragma: no cover - not used
        pass

    async def get_chat_info(self, chat_id):  # pragma: no cover - not used
        return {}

    async def send(self, *a, **k):  # pragma: no cover - not used
        raise NotImplementedError



class TestFormatChoicePage:
    def test_single_page_no_page_info(self):
        opts, meta = BasePlatformAdapter._format_choice_page([1, 2, 3], 0, 10)
        assert opts == [1, 2, 3]
        assert meta["page_info"] == ""
        assert meta["total_pages"] == 1
        assert meta["page"] == 0


    def test_page_clamped_high(self):
        opts, meta = BasePlatformAdapter._format_choice_page(list(range(25)), 99, 10)
        assert meta["page"] == 2
        assert opts == list(range(20, 25))
        assert meta["page_info"] == " (21–25 of 25)"


class TestAdapterParity:
    """Rewired adapters produce byte-identical text vs their historical inline code."""


    def test_choice_page_pagination_parity(self):
        """_format_choice_page matches the old _build_*_keyboard arithmetic."""

        def old(options, page, page_size):
            total = len(options)
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = max(0, min(page, total_pages - 1))
            start = page * page_size
            end = min(start + page_size, total)
            page_info = f" ({start + 1}–{end} of {total})" if total_pages > 1 else ""
            return options[start:end], page, total_pages, page_info

        for n in (0, 1, 8, 9, 10, 25):
            options = list(range(n))
            for page in (-3, 0, 1, 2, 99):
                for per in (8, 10):
                    o_opts, o_page, o_tp, o_info = old(options, page, per)
                    n_opts, meta = BasePlatformAdapter._format_choice_page(
                        options, page, per
                    )
                    assert n_opts == o_opts
                    assert meta["page"] == o_page
                    assert meta["total_pages"] == o_tp
                    assert meta["page_info"] == o_info
