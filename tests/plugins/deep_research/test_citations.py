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
    strip_unledgered_urls,
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


class TestUnledgeredUrlStripping:
    def test_only_unknown_markdown_and_bare_urls_are_removed(self) -> None:
        known = "https://example.org/evidence"
        unknown = "https://unseen.example/"
        text = (
            f"[Keep]({known}) stays; [homepage]({unknown}) becomes text. "
            f"Bare {unknown}index.html, and [again]({unknown})."
        )

        assert strip_unledgered_urls(text, [known]) == (
            f"[Keep]({known}) stays; homepage becomes text. Bare , and again."
        )

    def test_balanced_url_parens_and_prose_punctuation_are_preserved(self) -> None:
        text = f"See [entry]({PAREN_URL}), then {PAREN_URL}."

        assert strip_unledgered_urls(text, []) == "See entry, then ."

    def test_source_only_bare_url_list_items_leave_no_empty_bullets(self) -> None:
        known = "https://example.org/evidence"
        text = (
            "## Sources\n"
            f"- [Evidence]({known})\n"
            "- https://unseen.example/\n"
            "- https://unseen.example/index.html\n"
        )

        assert strip_unledgered_urls(text, [known]) == (
            "## Sources\n" f"- [Evidence]({known})\n"
        )

    def test_stripping_the_last_url_still_fails_citation_validation(self) -> None:
        sanitized = strip_unledgered_urls("Only [invented](https://unseen.example/).", [])

        verdict = validate_citations(sanitized, [])
        assert sanitized == "Only invented."
        assert verdict.ok is False
        assert verdict.errors == [
            "report contains no citations; a research report must cite fetched sources"
        ]
