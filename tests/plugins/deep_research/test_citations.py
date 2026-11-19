"""Citation extraction: balanced parentheses survive Markdown link parsing.

``[source](https://example.org/wiki/Foo_(bar))`` must cite the full URL, and
that URL must validate against the ledger entry the fetch actually recorded.
Provenance-only validation is unchanged — parens fix the *extraction*, not the
strictness.
"""

from __future__ import annotations

from plugins.deep_research.citations import (
    extract_report_urls,
    normalize_url,
    strip_safe_unledgered_urls,
    validate_citations,
)

PAREN_URL = "https://example.org/wiki/Foo_(bar)"


class TestBalancedParentheses:
    def test_markdown_link_with_parens_in_the_url_extracts_it_intact(self) -> None:
        assert extract_report_urls(f"[source]({PAREN_URL})") == [PAREN_URL]

    def test_nested_balanced_parens_extract_intact(self) -> None:
        url = "https://example.org/wiki/Foo_(bar_(baz))"
        assert extract_report_urls(f"[source]({url})") == [url]

    def test_only_the_markdown_closer_is_trimmed(self) -> None:
        # The URL keeps its own balanced parens; the Markdown closer goes.
        assert extract_report_urls("[s](https://example.org/x(y)z)") == [
            "https://example.org/x(y)z"
        ]

    def test_bare_url_before_a_prose_paren_is_trimmed(self) -> None:
        assert extract_report_urls("(see https://example.org/a) for details") == [
            "https://example.org/a"
        ]

    def test_trailing_sentence_punctuation_is_still_trimmed(self) -> None:
        assert extract_report_urls("See https://example.org/a.") == ["https://example.org/a"]

    def test_unclosed_paren_inside_a_bare_url_is_kept(self) -> None:
        # A URL may legitimately contain "(" with no ")" at all.
        assert extract_report_urls("see https://example.org/wiki/Foo_(bar for details") == [
            "https://example.org/wiki/Foo_(bar"
        ]


class TestParenProvenance:
    def test_paren_url_validates_against_its_ledger_entry(self) -> None:
        report = f"A cited claim. [source]({PAREN_URL})\n"
        verdict = validate_citations(report, [PAREN_URL])
        assert verdict.ok, verdict.errors
        assert verdict.cited_urls == [PAREN_URL]
        assert verdict.unknown_urls == []

    def test_paren_url_absent_from_the_ledger_still_fails_closed(self) -> None:
        verdict = validate_citations(f"[source]({PAREN_URL})", ["https://example.org/other"])
        assert verdict.ok is False
        assert verdict.unknown_urls == [PAREN_URL]

    def test_redirect_shaped_urls_normalize_identically_on_both_sides(self) -> None:
        # The ledger side (evidence record) and the report side (Markdown) must
        # agree through normalize_url even when the URL ends in a balanced paren.
        assert normalize_url(PAREN_URL) == PAREN_URL
        # An unbalanced trailing paren is prose, not URL.
        assert normalize_url("https://example.org/a)") == "https://example.org/a"


class TestStripSafeUnledgeredUrls:
    LEDGERED = "https://example.org/primary"

    def test_sources_item_with_only_unledgered_url_is_dropped(self) -> None:
        text = "## Sources\n\n- https://example.com/homepage\n- kept\n"
        out = strip_safe_unledgered_urls(text, [self.LEDGERED])
        assert "https://example.com/homepage" not in out
        assert "- kept" in out

    def test_mixed_sources_item_keeps_ledgered_url_and_label(self) -> None:
        text = f"## Sources\n\n- label [s]({self.LEDGERED}) plus https://example.net/x\n"
        out = strip_safe_unledgered_urls(text, [self.LEDGERED])
        assert self.LEDGERED in out and "label" in out
        assert "https://example.net/x" not in out

    def test_body_line_with_only_invented_url_is_unchanged(self) -> None:
        text = f"A body claim on [fake](https://example.com/i-made-this-up) alone.\n"
        assert strip_safe_unledgered_urls(text, [self.LEDGERED]) == text

    def test_table_row_loses_only_the_unledgered_url(self) -> None:
        row = f"| a | [good]({self.LEDGERED}) and https://example.org/redundant-row |\n"
        out = strip_safe_unledgered_urls(row, [self.LEDGERED])
        assert self.LEDGERED in out
        assert "https://example.org/redundant-row" not in out

    def test_notes_heading_ends_the_summary_section(self) -> None:
        text = (
            "## Sources\n\n- https://example.com/homepage\n\n"
            "## Notes\n\n- https://example.com/also-fake\n"
        )
        out = strip_safe_unledgered_urls(text, [self.LEDGERED])
        assert "https://example.com/homepage" not in out
        assert "https://example.com/also-fake" in out
