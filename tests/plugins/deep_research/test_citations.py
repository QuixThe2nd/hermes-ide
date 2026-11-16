"""Citation extraction: balanced parentheses survive Markdown link parsing.

``[source](https://example.org/wiki/Foo_(bar))`` must cite the full URL, and
that URL must validate against the ledger entry the fetch actually recorded.
Provenance-only validation is unchanged — parens fix the *extraction*, not the
strictness. ``strip_unledgered_urls`` is the mechanical salvage step after the
correction pass: same extraction/normalization, so its output always
re-validates clean.
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


LEDGERED = "https://example.org/primary"
UNLEDGERED = "https://example.com/homepage"
UNLEDGERED_2 = "https://example.com/index"


class TestStripUnledgeredUrls:
    """Mechanical removal of unledgered URLs after the correction pass.

    Every test runs through ``_strip``, which asserts the load-bearing
    invariant: the stripped text re-validates with zero unknown URLs.
    """

    def _strip(self, text: str, ledger: list[str]) -> tuple[str, list[str]]:
        new_text, removed = strip_unledgered_urls(text, ledger)
        verdict = validate_citations(new_text, ledger)
        assert verdict.unknown_urls == []
        return new_text, removed

    def test_inline_markdown_link_keeps_label_drops_link(self) -> None:
        new_text, removed = self._strip(
            f"See [the docs]({UNLEDGERED}) for details.", [LEDGERED]
        )
        assert new_text == "See the docs for details."
        assert removed == [UNLEDGERED]

    def test_autolink_removed(self) -> None:
        new_text, removed = self._strip(f"Source: <{UNLEDGERED}>.", [LEDGERED])
        assert UNLEDGERED not in new_text
        assert "<" not in new_text
        assert removed == [UNLEDGERED]

    def test_bare_url_removed(self) -> None:
        new_text, removed = self._strip(f"Bad cite {UNLEDGERED} here.", [LEDGERED])
        assert new_text == "Bad cite  here."
        assert removed == [UNLEDGERED]

    def test_reference_definition_line_removed(self) -> None:
        text = f"[1]: {LEDGERED}\n[2]: {UNLEDGERED}\n[3]: {UNLEDGERED_2}"
        new_text, removed = self._strip(text, [LEDGERED])
        assert new_text == f"[1]: {LEDGERED}"
        assert removed == [UNLEDGERED, UNLEDGERED_2]

    def test_emptied_source_list_lines_removed_but_labelled_items_stay(self) -> None:
        text = (
            "## Sources\n"
            f"- {LEDGERED}\n"
            f"- {UNLEDGERED}\n"
            f"- [Homepage]({UNLEDGERED_2})\n"
            "- prose note with no URL\n"
        )
        new_text, removed = self._strip(text, [LEDGERED])
        assert new_text == (
            "## Sources\n"
            f"- {LEDGERED}\n"
            "- Homepage\n"
            "- prose note with no URL\n"
        )
        assert removed == [UNLEDGERED, UNLEDGERED_2]

    def test_numbered_item_left_empty_is_removed(self) -> None:
        new_text, removed = self._strip(f"1. {LEDGERED}\n2. {UNLEDGERED}", [LEDGERED])
        assert new_text == f"1. {LEDGERED}"
        assert removed == [UNLEDGERED]

    def test_table_rows_keep_their_shape_and_cell_count(self) -> None:
        text = (
            "| Claim | Evidence |\n"
            "| --- | --- |\n"
            f"| widgets | [a]({LEDGERED}) [dup]({UNLEDGERED}) |\n"
            f"| gizmos | [b]({UNLEDGERED_2}) |\n"
        )
        new_text, removed = self._strip(text, [LEDGERED])
        lines = new_text.splitlines()
        assert len(lines) == 4  # no row deleted
        assert lines[2] == f"| widgets | [a]({LEDGERED}) dup |"
        assert lines[3] == "| gizmos | b |"
        assert removed == [UNLEDGERED, UNLEDGERED_2]

    def test_bare_url_in_table_cell_keeps_cell_count(self) -> None:
        text = f"| source |\n| --- |\n| {UNLEDGERED} |\n"
        new_text, _removed = self._strip(text, [LEDGERED])
        cell_row = new_text.splitlines()[2]
        assert cell_row.count("|") == 2  # both delimiters survive
        assert UNLEDGERED not in cell_row

    def test_ledgered_urls_left_byte_identical(self) -> None:
        text = (
            f"A [wiki]({PAREN_URL}) cite.\n"
            f"B bare {LEDGERED}.\n"
            f"C autolink <{PAREN_URL}> end.\n"
        )
        new_text, removed = self._strip(text, [PAREN_URL, LEDGERED])
        assert new_text == text
        assert removed == []

    def test_variant_spelling_of_ledgered_url_not_stripped(self) -> None:
        for written in (f"{LEDGERED}#section", "https://EXAMPLE.org:443/primary"):
            new_text, removed = self._strip(f"cite {written} now", [LEDGERED])
            assert new_text == f"cite {written} now", written
            assert removed == [], written

    def test_text_without_urls_returned_unchanged(self) -> None:
        text = "plain text, no citations anywhere"
        assert strip_unledgered_urls(text, [LEDGERED]) == (text, [])

    def test_mixed_document_only_unledgered_parts_removed(self) -> None:
        text = (
            "# Research\n\n"
            f"Solid claim [1]({PAREN_URL}).\n\n"
            "| topic | source |\n| --- | --- |\n"
            f"| alpha | {UNLEDGERED} |\n\n"
            "## Sources\n\n"
            f"- <{LEDGERED}>\n"
            f"- {UNLEDGERED_2}\n"
        )
        new_text, removed = self._strip(text, [PAREN_URL, LEDGERED])
        assert PAREN_URL in new_text and LEDGERED in new_text
        assert UNLEDGERED not in new_text and UNLEDGERED_2 not in new_text
        assert new_text.count("## Sources") == 1
        assert removed == [UNLEDGERED, UNLEDGERED_2]
