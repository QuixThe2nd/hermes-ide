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


class TestUnledgeredUrlCleanup:
    def test_unledgered_mixed_syntax_preserves_prose_and_ledger_links(self) -> None:
        unknown = "https://example.org/wiki/Unfetched_(source)"
        text = (
            f'[keep]({PAREN_URL}) and [drop]({unknown} "optional title")\n'
            f"Keep {PAREN_URL}; remove {unknown}.\n"
            "[keep ref][good] and [drop ref][bad].\n"
            f"[good]: {PAREN_URL}\n"
            f'[bad]: {unknown} "optional title"\n'
        )

        cleaned = citations.remove_unledgered_urls(text, [PAREN_URL])

        assert cleaned == (
            f"[keep]({PAREN_URL}) and drop\n"
            f"Keep {PAREN_URL}; remove.\n"
            "[keep ref][good] and drop ref.\n"
            f"[good]: {PAREN_URL}\n"
        )
        verdict = validate_citations(cleaned, [PAREN_URL])
        assert verdict.ok, verdict.errors

    def test_unledgered_only_citations_leave_no_citations(self) -> None:
        unknown = "https://example.org/not-fetched"

        cleaned = citations.remove_unledgered_urls(
            f"Report citing [visible text]({unknown}) and {unknown}.", [PAREN_URL]
        )

        assert cleaned == "Report citing visible text and."
        verdict = validate_citations(cleaned, [PAREN_URL])
        assert verdict.ok is False
        assert verdict.cited_urls == []
        assert any("no citations" in error for error in verdict.errors)
