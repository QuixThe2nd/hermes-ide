"""Citation extraction and provenance validation.

Validation proves **URL provenance only**: every source URL emitted in the
report must exist in the job's evidence ledger. It does NOT prove that the
cited page semantically supports the claim it is attached to — that limitation
is stated in the plugin README and in ``result`` metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Set
from urllib.parse import urlsplit, urlunsplit

# Markdown links ``[label](https://…)``, reference links, and bare http(s)
# URLs. Parentheses are allowed *inside* the URL (Wikipedia-style
# ``Foo_(bar)``); the Markdown link closer is split off afterwards by paren
# balance (:func:`_strip_prose_tail`), not by the character class.
_URL_RE = re.compile(
    r"""(?:\]\(\s*<?)?(https?://[^\s<>"'\]]+)""",
    re.IGNORECASE,
)

# Trailing punctuation that is prose, not part of the URL. ``)`` is absent on
# purpose: a balanced ``)`` can be the URL's own final character.
_TRAILING_PUNCT = ".,;:!?]}'\">’”》"

_SCHEME_HTTP = re.compile(r"^https?://", re.IGNORECASE)

_REFERENCE_DEFINITION_RE = re.compile(
    r"(?m)^ {0,3}\[(?P<label>[^]\n]+)\]:[ \t]*"
    r"<?(?P<url>https?://[^\s<>\"'\]]+)[^\n]*(?:\n|$)",
    re.IGNORECASE,
)
_REFERENCE_USE_RE = re.compile(
    r"!?\[(?P<text>[^]\n]+)\](?:\[(?P<label>[^]\n]*)\])?"
)


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


def _reference_key(label: str) -> str:
    return " ".join(label.split()).casefold()


def _strip_unledgered_references(text: str, allowed: Set[str]) -> str:
    """Remove bad definitions and turn only their references into plain text."""
    known_labels: Set[str] = set()
    unknown_labels: Set[str] = set()

    def definition(match: re.Match) -> str:
        key = _reference_key(match.group("label"))
        raw = _strip_prose_tail(match.group("url"))
        if normalize_url(raw) in allowed:
            known_labels.add(key)
            return match.group(0)
        unknown_labels.add(key)
        return ""

    stripped = _REFERENCE_DEFINITION_RE.sub(definition, text)
    unresolved = unknown_labels - known_labels
    if not unresolved:
        return stripped

    def usage(match: re.Match) -> str:
        label = match.group("label")
        if label is None:
            # An inline link is handled by the URL pass, not as a shortcut reference.
            if stripped[match.end():match.end() + 1] == "(":
                return match.group(0)
            label = match.group("text")
        if _reference_key(label or match.group("text")) not in unresolved:
            return match.group(0)
        return match.group("text")

    return _REFERENCE_USE_RE.sub(usage, stripped)


def _inline_label_start(text: str, close: int) -> int:
    depth = 0
    for index in range(close, -1, -1):
        if text[index] == "]":
            depth += 1
        elif text[index] == "[":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _inline_link_end(text: str, start: int) -> int:
    """Find the destination's closing paren, ignoring quoted/parenthesized titles."""
    quote = ""
    nested = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "\n":
            return -1
        if quote:
            if char == quote and text[index - 1] != "\\":
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "(":
            nested += 1
        elif char == ")":
            if nested:
                nested -= 1
            else:
                return index + 1
    return -1


def remove_unledgered_urls(report_text: str, evidence_urls: Iterable[str]) -> str:
    """Remove unsupported URL destinations without removing supported report prose.

    Inline and reference links become their visible label; bare URLs are removed.
    Ledger-backed source text is left byte-for-byte unchanged. The caller must run
    :func:`validate_citations` on the returned text before publishing it.
    """
    allowed = {normalize_url(url) for url in evidence_urls if url}
    text = _strip_unledgered_references(report_text or "", allowed)
    pieces: List[str] = []
    cursor = 0

    for match in _URL_RE.finditer(text):
        if match.start() < cursor:
            continue  # Already removed with an enclosing inline link.
        raw = _strip_prose_tail(match.group(1))
        if normalize_url(raw) in allowed:
            continue

        url_start = match.start(1)
        url_end = url_start + len(raw)
        start, end, replacement = url_start, url_end, ""
        if match.group(0).startswith("]("):
            label_start = _inline_label_start(text, match.start())
            link_end = _inline_link_end(text, url_end)
            if label_start >= 0 and link_end >= 0:
                start = label_start - 1 if text[label_start - 1:label_start] == "!" else label_start
                end = link_end
                replacement = text[label_start + 1:match.start()]
        elif text[url_start - 1:url_start] == "<" and text[url_end:url_end + 1] == ">":
            start, end = url_start - 1, url_end + 1
            if text[end:end + 1] in (".", ",", ";", ":", "!", "?"):
                if text[start - 1:start] in (" ", "\t"):
                    start -= 1
        elif text[url_end:url_end + 1] in (".", ",", ";", ":", "!", "?"):
            # Avoid leaving a space before sentence punctuation after a bare URL.
            if text[url_start - 1:url_start] in (" ", "\t"):
                start -= 1

        pieces.append(text[cursor:start])
        pieces.append(replacement)
        cursor = end

    pieces.append(text[cursor:])
    return "".join(pieces)


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
