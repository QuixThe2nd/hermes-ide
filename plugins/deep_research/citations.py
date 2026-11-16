"""Citation extraction and provenance validation.

Validation proves **URL provenance only**: every source URL emitted in the
report must exist in the job's evidence ledger. It does NOT prove that the
cited page semantically supports the claim it is attached to — that limitation
is stated in the plugin README and in ``result`` metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, NamedTuple, Set, Tuple
from urllib.parse import urlsplit, urlunsplit

# Markdown links ``[label](https://…)``, reference links, and bare http(s)
# URLs. Parentheses are allowed *inside* the URL (Wikipedia-style
# ``Foo_(bar)``); the Markdown link closer is split off afterwards by paren
# balance (:func:`_strip_prose_tail`), not by the character class.
_URL_RE = re.compile(
    r"""(?:\]\(\s*)?(https?://[^\s<>"'\]]+)""",
    re.IGNORECASE,
)

# Trailing punctuation that is prose, not part of the URL. ``)`` is absent on
# purpose: a balanced ``)`` can be the URL's own final character.
_TRAILING_PUNCT = ".,;:!?]}'\">’”》"

_SCHEME_HTTP = re.compile(r"^https?://", re.IGNORECASE)

# A reference-definition line ``[id]: url`` — the URL is the line's payload.
_REF_DEF_BEFORE_RE = re.compile(r"^\s{0,3}\[[^\]]*\]:\s*$")
# Only punctuation/whitespace may follow the URL of a reference definition.
_NOISE_TAIL_RE = re.compile(r"^[\s.,;:!?)\]}'\"’”》]*$")
# Residue that carries no content once its URL(s) are stripped: list bullets,
# numbering, and punctuation.
_ITEM_RESIDUE_RE = re.compile(r"^[\s\-*+•·>0-9.,;:!?()\[\]{}>'\"’”》_#&]*$")


def _strip_unbalanced_parens(url: str) -> str:
    """Drop trailing ``)`` characters the URL's own ``(``s cannot account for.

    In ``[source](https://example.org/wiki/Foo_(bar))`` the regex match
    greedily includes the Markdown link closer; the URL itself ends at the
    paren that balances its only ``(``.
    """
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1]
    return url


def _strip_prose_tail(url: str) -> str:
    """Trim prose punctuation from a matched URL, preserving balanced parens."""
    candidate = (url or "").strip()
    while True:
        trimmed = _strip_unbalanced_parens(candidate.rstrip(_TRAILING_PUNCT))
        if trimmed == candidate:
            return trimmed
        candidate = trimmed


@dataclass
class CitationReport:
    """Outcome of validating a draft report against the evidence set."""

    ok: bool
    cited_urls: List[str] = field(default_factory=list)
    unknown_urls: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def normalize_url(url: str) -> str:
    """Canonicalize a URL for provenance matching.

    Strips the fragment (never content-bearing for provenance), drops a default
    port, lowercases scheme and host, and removes a bare trailing slash on the
    root path. Query strings and paths are compared verbatim — over-normalizing
    would let two distinct sources collapse into one.
    """
    candidate = _strip_prose_tail(url)
    if not _SCHEME_HTTP.match(candidate):
        return candidate.lower()
    try:
        parts = urlsplit(candidate)
        # urlsplit lowercases hostname and strips userinfo/port for us.
        host = (parts.hostname or "").lower()
        port = parts.port  # ValueError on a malformed port
    except ValueError:
        return candidate.lower()
    scheme = parts.scheme.lower()
    default_port = (scheme, port) in (("http", 80), ("https", 443))
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def extract_report_urls(text: str) -> List[str]:
    """Every http(s) URL a reader could take as a source, in order of appearance."""
    urls: List[str] = []
    seen: Set[str] = set()
    for match in _URL_RE.finditer(text or ""):
        raw = _strip_prose_tail(match.group(1))
        if not raw:
            continue
        normalized = normalize_url(raw)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
    return urls


def validate_citations(
    report_text: str,
    evidence_urls: Iterable[str],
    *,
    require_one: bool = True,
) -> CitationReport:
    """Check that every URL in ``report_text`` is backed by the evidence ledger.

    Fail-closed rules:
    - any URL in the report that is not in the ledger → unknown (invented or
      unevidenced source);
    - a non-empty report with zero citations → error (a research report must
      cite at least one fetched source).
    """
    allowed = {normalize_url(url) for url in evidence_urls if url}
    cited = extract_report_urls(report_text)
    unknown = [url for url in cited if url not in allowed]

    errors: List[str] = []
    if unknown:
        preview = ", ".join(unknown[:5])
        more = f" (+{len(unknown) - 5} more)" if len(unknown) > 5 else ""
        errors.append(
            "report cites URL(s) absent from the evidence ledger: " f"{preview}{more}"
        )
    if require_one and (report_text or "").strip() and not cited:
        errors.append("report contains no citations; a research report must cite fetched sources")

    return CitationReport(
        ok=not errors,
        cited_urls=cited,
        unknown_urls=unknown,
        errors=errors,
    )


# -- mechanical strip of unledgered URLs ---------------------------------


class _Edit(NamedTuple):
    """One span rewrite: ``line[start:end]`` becomes ``replacement``."""

    start: int
    end: int
    replacement: str
    url: str  # normalized, for the caller's audit trail


def strip_unledgered_urls(
    report_text: str, evidence_urls: Iterable[str]
) -> Tuple[str, List[str]]:
    """Mechanically remove URLs that :func:`validate_citations` would reject.

    The salvage step after the correction pass: same extraction and
    normalization as validation, so the returned text re-validates with zero
    unknown URLs. Unledgered inline links keep their label as plain text;
    autolinks and bare URLs are deleted; a reference-definition line whose
    payload URL is unledgered loses the whole line, as does a list item left
    holding only bullets, numbering, or punctuation. Markdown table rows are
    never deleted — only the URL spans inside them — so cell counts hold.
    Ledgered URLs are left byte-identical, including paren-bearing and
    normalize-equal spellings. Returns ``(new_text, removed)`` with ``removed``
    the normalized URLs in order of first appearance.
    """
    allowed = {normalize_url(url) for url in evidence_urls if url}
    text = report_text or ""
    removed: List[str] = []
    while True:
        # A label can itself contain a URL, so stripping can expose new
        # unledgered matches; repeat until a pass removes nothing.
        text, pass_removed = _strip_pass(text, allowed)
        if not pass_removed:
            return text, removed
        for url in pass_removed:
            if url not in removed:
                removed.append(url)


def _strip_pass(text: str, allowed: Set[str]) -> Tuple[str, List[str]]:
    """One line-by-line removal pass."""
    kept: List[str] = []
    removed: List[str] = []
    for raw_line in text.splitlines(keepends=True):
        line, ending = _split_ending(raw_line)
        edits = _unledgered_edits(line, allowed)
        if not edits:
            kept.append(raw_line)
            continue
        removed.extend(edit.url for edit in edits)
        if not _is_table_row(line) and _line_reduced_to_residue(line, edits):
            # Dropping the line takes its own newline with it; the final line
            # has none, so it swallows the previous line's instead.
            if not ending and kept and kept[-1].endswith("\n"):
                kept[-1] = kept[-1][:-1]
            continue
        kept.append(_apply_edits(line, edits) + ending)
    return "".join(kept), removed


def _unledgered_edits(line: str, allowed: Set[str]) -> List[_Edit]:
    """Non-overlapping rewrite spans for the line's unledgered URL matches."""
    edits: List[_Edit] = []
    covered_until = 0
    for match in _URL_RE.finditer(line):
        url = _strip_prose_tail(match.group(1))
        if not url:
            continue
        edit = _removal_span(line, match, url)
        if edit.url in allowed:
            continue
        if edit.start < covered_until:
            continue  # swallowed by an earlier edit; the next pass takes it
        covered_until = edit.end
        edits.append(edit)
    return edits


def _removal_span(line: str, match: "re.Match", url: str) -> _Edit:
    """What to remove for one URL occurrence, keeping any label text."""
    url_start = match.start(1)
    url_end = url_start + len(url)
    normalized = normalize_url(url)
    if line[match.start() : url_start].startswith("]("):
        # Inline Markdown link ``[label](url)``: the label stays as text.
        open_bracket = line.rfind("[", 0, match.start())
        if open_bracket != -1:
            return _Edit(
                open_bracket,
                _link_closer(line, url_start, url_end),
                line[open_bracket + 1 : match.start()],
                normalized,
            )
    if line[url_start - 1 : url_start] == "<" and line[url_end : url_end + 1] == ">":
        return _Edit(url_start - 1, url_end + 1, "", normalized)  # autolink
    return _Edit(url_start, url_end, "", normalized)  # bare URL


def _link_closer(line: str, url_start: int, url_end: int) -> int:
    """Index just past the ``)`` closing the inline link's destination."""
    depth = 1  # the ``(`` immediately before the destination
    for index in range(url_start, len(line)):
        if line[index] == "(":
            depth += 1
        elif line[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return url_end  # unclosed destination: bound the edit at the URL


def _line_reduced_to_residue(line: str, edits: List[_Edit]) -> bool:
    """True when the line carries no content once its URL removals apply."""
    start, end = edits[0].start, edits[0].end
    if _REF_DEF_BEFORE_RE.match(line[:start]) and _NOISE_TAIL_RE.match(line[end:]):
        return True  # ``[id]: unledgered-url`` — the URL was the payload
    return _ITEM_RESIDUE_RE.match(_apply_edits(line, edits)) is not None


def _is_table_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def _split_ending(raw_line: str) -> Tuple[str, str]:
    """Split a ``splitlines(keepends=True)`` piece into content and ending."""
    if raw_line.endswith("\r\n"):
        return raw_line[:-2], "\r\n"
    if raw_line.endswith("\n"):
        return raw_line[:-1], "\n"
    return raw_line, ""


def _apply_edits(line: str, edits: List[_Edit]) -> str:
    parts: List[str] = []
    cursor = 0
    for edit in edits:
        parts.append(line[cursor : edit.start])
        parts.append(edit.replacement)
        cursor = edit.end
    parts.append(line[cursor:])
    return "".join(parts)
