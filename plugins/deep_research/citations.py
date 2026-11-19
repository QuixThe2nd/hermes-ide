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
    r"""(?:\]\(\s*)?(https?://[^\s<>"'\]]+)""",
    re.IGNORECASE,
)

# Trailing punctuation that is prose, not part of the URL. ``)`` is absent on
# purpose: a balanced ``)`` can be the URL's own final character.
_TRAILING_PUNCT = ".,;:!?]}'\">’”》"

_SCHEME_HTTP = re.compile(r"^https?://", re.IGNORECASE)

# ATX heading: ``#{1,6}`` + space + title. A summary section (Sources /
# References / Bibliography) starts at such a heading and ends at the next one.
_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_SUMMARY_TITLE_RE = re.compile(r"^(sources|references|bibliography)\b", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")


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


def strip_safe_unledgered_urls(report_text: str, evidence_urls: Iterable[str]) -> str:
    """Remove leftover unledgered URLs that are safe to drop mechanically.

    After the correction pass, a report may still carry unledgered URLs that
    are provably redundant: a Sources-list item where every URL is unledgered
    (homepage filler), or an unledgered URL sitting beside a ledgered one on
    the same line/row. Those are stripped so the report can publish. A body
    line whose ONLY URLs are unledgered is a citation the report actually
    leans on — it is left byte-for-byte so validation still fails closed.
    """
    allowed = {normalize_url(url) for url in evidence_urls if url}
    lines = report_text.splitlines(keepends=True)
    out: List[str] = []
    in_summary = False
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        title = _ATX_HEADING_RE.match(stripped)
        if title:
            in_summary = bool(_SUMMARY_TITLE_RE.match(title.group(2)))
            out.append(lines[i])
            i += 1
            continue
        if in_summary and _LIST_ITEM_RE.match(lines[i]):
            block = [lines[i]]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if not nxt.strip() or _LIST_ITEM_RE.match(nxt) or _ATX_HEADING_RE.match(nxt.strip()):
                    break
                block.append(nxt)
                j += 1
            urls = extract_report_urls("".join(block))
            if urls and all(url not in allowed for url in urls):
                i = j  # drop the whole item
                continue
            out.extend(_strip_unledgered(line, allowed) for line in block)
            i = j
            continue
        out.append(_strip_unledgered(lines[i], allowed))
        i += 1
    return "".join(out)


def _strip_unledgered(line: str, allowed: Set[str]) -> str:
    """Drop unledgered URL occurrences from one line, but only when the line
    also cites at least one ledgered URL (the citation is redundant, not
    load-bearing). Ledgered URLs stay byte-for-byte."""
    urls = extract_report_urls(line)
    if not urls or not any(url in allowed for url in urls):
        return line
    for match in reversed(list(_URL_RE.finditer(line))):
        raw = _strip_prose_tail(match.group(1))
        if not raw or normalize_url(raw) in allowed:
            continue
        url_start = match.start(1)
        url_end = url_start + len(raw)
        if match.group(0) != match.group(1):
            # Markdown link destination: keep the label, lose the whole link.
            opener = line.rfind("[", 0, match.start())
            label = line[opener + 1 : match.start()] if opener != -1 else ""
            start = opener if opener != -1 else match.start()
            closer = 1 if line[url_end : url_end + 1] == ")" else 0
            line = line[:start] + label + line[url_end + closer :]
        elif line[url_start - 1 : url_start] == "<" and line[url_end : url_end + 1] == ">":
            line = line[: url_start - 1] + line[url_end + 1 :]  # <autolink>
        else:
            line = line[:url_start] + line[url_end:]  # bare URL text
    return line


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
