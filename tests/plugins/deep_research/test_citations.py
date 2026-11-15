"""Citation extraction: balanced parentheses survive Markdown link parsing.

``[source](https://example.org/wiki/Foo_(bar))`` must cite the full URL, and
that URL must validate against the ledger entry the fetch actually recorded.
Provenance-only validation is unchanged — parens fix the *extraction*, not the
strictness.
"""

from __future__ import annotations

from plugins.deep_research import citations
from plugins.deep_research.citations import (
    extract_report_urls,
    normalize_url,
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


class TestUnledgeredUrlRemoval:
    UNKNOWN_URL = "https://example.com/not-in-the-ledger"

    def test_markdown_link_drops_only_the_unledgered_link(self) -> None:
        report = f"Keep [unledgered]({self.UNKNOWN_URL}) and [ledgered]({PAREN_URL})."

        cleaned = citations.remove_unledgered_urls(report, [PAREN_URL])

        assert cleaned == f"Keep unledgered and [ledgered]({PAREN_URL})."

    def test_bare_urls_leave_surrounding_prose_and_punctuation_intact(self) -> None:
        report = (
            "Before https://example.org/invented, after; "
            "(see https://example.net/missing). End"
        )

        cleaned = citations.remove_unledgered_urls(report, [PAREN_URL])

        assert cleaned == "Before , after; (see ). End"

    def test_balanced_url_parentheses_are_not_mistaken_for_prose(self) -> None:
        unknown = "https://example.com/wiki/Foo_(bar)"
        report = f"Drop [wiki]({unknown}) and ({unknown})."

        cleaned = citations.remove_unledgered_urls(report, [PAREN_URL])

        assert cleaned == "Drop wiki and ()."

    def test_removing_the_only_citation_still_fails_validation(self) -> None:
        cleaned = citations.remove_unledgered_urls(
            f"Only [unledgered]({self.UNKNOWN_URL}).", [PAREN_URL]
        )

        verdict = validate_citations(cleaned, [PAREN_URL])

        assert cleaned == "Only unledgered."
        assert verdict.ok is False
        assert verdict.cited_urls == []
        assert any("no citations" in error for error in verdict.errors)
