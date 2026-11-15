"""Citation extraction and provenance validation.

Validation proves **URL provenance only**: every source URL emitted in the
report must exist in the job's evidence ledger. It does NOT prove that the
cited page semantically supports the claim it is attached to — that limitation
is stated in the plugin README and in ``result`` metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Set, Tuple
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

_LIST_ITEM_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+(?P<content>.*?)\s*$")
_ATX_HEADING_RE = re.compile(
    r"^ {0,3}(?P<marks>#{1,6})[ \t]+(?P<title>.*?)[ \t]*#*[ \t]*$"
)
_SOURCE_ENTRY_PUNCT = " \t,;:.!-—–()<>"


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


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _matching_open_bracket(text: str, close_bracket: int) -> Optional[int]:
    depth = 0
    for index in range(close_bracket, -1, -1):
        if _is_escaped(text, index):
            continue
        if text[index] == "]":
            depth += 1
        elif text[index] == "[":
            depth -= 1
            if depth == 0:
                return index
    return None


def _matching_close_paren(text: str, open_paren: int) -> Optional[int]:
    depth = 0
    for index in range(open_paren, len(text)):
        if _is_escaped(text, index):
            continue
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _inline_link_span(
    text: str, match: re.Match, url_end: int
) -> Optional[Tuple[int, int, str]]:
    """Return the complete inline-link span and its visible label."""
    url_start = match.start(1)
    prefix = text[match.start():url_start]
    if prefix.startswith("]("):
        close_bracket = match.start()
    else:
        angle_prefix = re.search(r"\]\(\s*<$", text[:url_start])
        if angle_prefix is None:
            return None
        close_bracket = angle_prefix.start()

    open_bracket = _matching_open_bracket(text, close_bracket)
    if open_bracket is None:
        return None
    close_paren = _matching_close_paren(text, close_bracket + 1)
    end = close_paren + 1 if close_paren is not None else url_end
    start = open_bracket
    if start > 0 and text[start - 1] == "!" and not _is_escaped(text, start - 1):
        start -= 1
    return start, end, text[open_bracket + 1:close_bracket]


def _strip_unknown_from_line(text: str, allowed: Set[str], *, keep_labels: bool) -> str:
    pieces: List[str] = []
    cursor = 0
    for match in _URL_RE.finditer(text):
        raw = _strip_prose_tail(match.group(1))
        if not raw:
            continue
        normalized = normalize_url(raw)
        if not normalized or normalized in allowed:
            continue

        url_start = match.start(1)
        url_end = url_start + len(raw)
        link = _inline_link_span(text, match, url_end)
        if link is not None:
            start, end, label = link
        else:
            start, end, label = url_start, url_end, ""
            if start > 0 and text[start - 1] == "<" and text[end:end + 1] == ">":
                start -= 1
                end += 1
        if start < cursor:
            continue
        pieces.append(text[cursor:start])
        if keep_labels:
            pieces.append(label)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _is_unknown_only_source_entry(line: str, allowed: Set[str]) -> bool:
    item = _LIST_ITEM_RE.match(line)
    if item is None:
        return False
    content = item.group("content")
    cited = extract_report_urls(content)
    if not cited or any(url in allowed for url in cited):
        return False
    remainder = _strip_unknown_from_line(content, allowed, keep_labels=False)
    return not remainder.strip(_SOURCE_ENTRY_PUNCT)


def strip_unknown_citations(report_text: str, evidence_urls: Iterable[str]) -> str:
    """Remove unledgered citations from a corrected report without losing prose.

    Unknown inline links become their visible labels; bare URLs and autolinks
    disappear. Under a ``Sources`` heading, list items containing only unknown
    citations are pruned instead of leaving labels that look like sources.
    """
    if not report_text:
        return report_text
    allowed = {normalize_url(url) for url in evidence_urls if url}
    output: List[str] = []
    sources_heading_level: Optional[int] = None

    for raw_line in report_text.splitlines(keepends=True):
        body = raw_line.rstrip("\r\n")
        ending = raw_line[len(body):]
        heading = _ATX_HEADING_RE.match(body)
        if heading is not None:
            level = len(heading.group("marks"))
            title = heading.group("title").strip().rstrip(":").strip().casefold()
            if title == "sources":
                sources_heading_level = level
            elif sources_heading_level is not None and level <= sources_heading_level:
                sources_heading_level = None

        if sources_heading_level is not None and _is_unknown_only_source_entry(body, allowed):
            continue
        cleaned = _strip_unknown_from_line(body, allowed, keep_labels=True)
        output.append(cleaned + ending)
    return "".join(output)


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
