"""web_extract helpers: URL validation, provider resolution, cache-aware dispatch.

Order of controls (each is a gate, never skipped by a cache hit): secret-URL
refusal -> SSRF filter (in web_tools.web_extract_tool) -> provider resolution
(strict selection) -> per-URL website policy -> disk cache -> vendor call with
one-shot keyless rescue. Logs under the origin (tools.web_tools) logger.
"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from tools.tool_backend_helpers import selection_error, selection_exists
from tools.url_safety import normalize_url_for_request
from tools.web_tools_rescue import _rescue_eligible, _rescue_extract

logger = logging.getLogger("tools.web_tools")

_NO_RESULT_ERROR = "Extract backend returned no result for this URL"
_DEFAULT_EXTRACT_TIMEOUT_S = 120.0
_EXTRACT_BACKENDS_HINT = "firecrawl, tavily, keenable, exa, or parallel."
_INVALID_ITEM_ERROR = (
    "Invalid URL item at index {}: expected a URL string or an object with a string 'url' or 'href' field"
)


def _web_extract_url(value: Any) -> Optional[str]:
    """URL from a model-supplied extract item (str, or dict with ``url``/``href``); None if unusable.

    Models sometimes forward a whole search result instead of its URL, hence the dict form. Never
    stringify arbitrary objects into misleading fetch targets.
    """
    if isinstance(value, dict):
        value = value.get("url") or value.get("href")
    return (value.strip() or None) if isinstance(value, str) else None


def _disabled_plugin_error(capability: str, disabled_key: str) -> str:
    """Error text when the configured backend's bundled plugin is disabled in config."""
    vendor = disabled_key.split("/", 1)[-1]
    return (
        f"web.{capability}_backend is set to '{vendor}', but its plugin ('{disabled_key}') is disabled "
        f"in config. Re-enable it with `hermes plugins enable {disabled_key}` "
        "(or remove it from plugins.disabled)."
    )


def _no_provider_error(capability: str, fallback: str) -> str:
    """Error when no provider resolved: point at a disabled bundled plugin if that is the real cause."""
    from agent.web_search_registry import _disabled_web_plugin_for
    disabled_key = _disabled_web_plugin_for(capability=capability)
    return _disabled_plugin_error(capability, disabled_key) if disabled_key else fallback


def _strict_selection_error(capability: str, backend: str) -> str:
    """Error for a stored-but-unregistered backend: name the disabled plugin, else the bad selection.
    Strict selection never silently switches to whatever the availability walk finds."""
    failure = f"no registered web {capability} provider has that name"
    return _no_provider_error(capability, selection_error("web", f"'{backend}'", failure))


def _result_entry(url: str, error: Optional[str]) -> Dict[str, Any]:
    return {"url": url, "title": "", "content": "", "error": error}


def _extract_error_json(error: str) -> str:
    return json.dumps({"success": False, "error": error}, ensure_ascii=False)


def _refuse_all(error: str):
    """Whole-call refusal tuple for ``_validate_extract_urls`` (exfiltration prevention)."""
    return None, None, None, json.dumps({"success": False, "error": error})


def _merge_in_order(
    total: int, fixed: Dict[int, dict], fetch_positions: List[int], fetch_urls: List[str], results: List[dict]
) -> List[dict]:
    """Rebuild a ``total``-long result list: *fixed* entries by position, fetched *results* at
    *fetch_positions* (a short provider list yields ``_NO_RESULT_ERROR`` entries for the rest)."""
    merged = dict(fixed)
    for pos, position in enumerate(fetch_positions):
        missing = _result_entry(fetch_urls[pos], _NO_RESULT_ERROR)
        merged[position] = results[pos] if pos < len(results) else missing
    return [merged[i] for i in range(total)]


def _validate_extract_urls(urls: List[Any]):
    """Normalize model-supplied items and block URLs carrying secrets (percent-encoded forms are unquoted
    and checked too). Returns ``(normalized_urls, normalized_indices, invalid_urls, blocked_json)``;
    ``blocked_json`` is a whole-call refusal (exfiltration prevention) or None."""
    from agent.redact import _PREFIX_RE
    from urllib.parse import unquote

    normalized_urls, normalized_indices, invalid_urls = [], [], {}
    for index, item in enumerate(urls):
        _url = _web_extract_url(item)
        if _url is None:
            invalid_urls[index] = _result_entry("", _INVALID_ITEM_ERROR.format(index))
            continue
        normalized_url = normalize_url_for_request(_url)
        if any(_PREFIX_RE.search(c) for c in (_url, unquote(_url), normalized_url, unquote(normalized_url))):
            return _refuse_all(
                "Blocked: URL contains what appears to be an API key or token. "
                "Secrets must not be sent in URLs."
            )
        normalized_urls.append(normalized_url)
        normalized_indices.append(index)
    return normalized_urls, normalized_indices, invalid_urls, None


def _resolve_extract_provider(backend: str):
    """Resolve the extract provider for *backend*; returns ``(provider, error_json)``.

    A registered search-only backend is a typed error (never a silent switch). An unregistered name with
    a stored web selection is a strict-selection error; with no selection, fall through to the walk.
    """
    from agent.web_search_registry import get_active_extract_provider, get_provider as _wsp_get_provider
    provider = _wsp_get_provider(backend) if backend else None
    if provider is not None and provider.supports_extract():
        return provider, None
    if provider is not None:
        return None, _extract_error_json(
            f"{provider.display_name} is a search-only backend and cannot extract URL content. "
            "Set web.extract_backend to " + _EXTRACT_BACKENDS_HINT
        )
    if backend and selection_exists("web"):
        return None, _extract_error_json(_strict_selection_error("extract", backend))
    provider = get_active_extract_provider()
    if provider is None:
        fallback = "No web extract provider configured. Set web.extract_backend to " + _EXTRACT_BACKENDS_HINT
        return None, _extract_error_json(_no_provider_error("extract", fallback))
    return provider, None


def _extract_timeout_seconds() -> float:
    """Wall-clock cap for one provider ``extract()`` dispatch (``web.extract_timeout``, default 120s).

    A hanging backend (server keeps the response open without finishing) otherwise stalls the
    tool call indefinitely. 0 or a negative value disables the cap.
    """
    from tools.web_tools import _load_web_config
    try:
        return float(_load_web_config().get("extract_timeout", _DEFAULT_EXTRACT_TIMEOUT_S))
    except (TypeError, ValueError):
        return _DEFAULT_EXTRACT_TIMEOUT_S


async def _dispatch_extract(provider, fetch_urls: List[str], format: Optional[str]) -> List[dict]:
    """Call ``provider.extract`` (async or sync-in-thread), with one-shot keyless rescue.

    Rescue fires on a raised exception — including a dispatch timeout — or when the WHOLE batch
    failed (backend outage, not per-page problems). Rescued batches are never cached.
    """
    import inspect
    from tools.web_result_cache import extract_cache_put
    timeout = _extract_timeout_seconds()
    try:
        if inspect.iscoroutinefunction(provider.extract):
            coro = provider.extract(fetch_urls, format=format)
        else:  # sync extract() runs in a thread so network I/O never blocks the loop
            coro = asyncio.to_thread(provider.extract, fetch_urls, format=format)
        if timeout > 0:
            results = await asyncio.wait_for(coro, timeout=timeout)
        else:
            results = await coro
    except asyncio.TimeoutError as exc:  # hanging backend — bounded, never a stalled tool call
        logger.warning("web_extract provider '%s' timed out after %.0fs for %d URL(s)",
                       provider.name, timeout, len(fetch_urls))
        failed = [_result_entry(u, f"Extract timed out after {timeout:.0f}s via {provider.name}")
                  for u in fetch_urls]
        if not _rescue_eligible(provider):
            return failed
        return await asyncio.to_thread(_rescue_extract, provider.name, fetch_urls, failed)
    except Exception as exc:  # noqa: BLE001 — candidate for rescue
        if not _rescue_eligible(provider):
            raise
        failed = [_result_entry(u, str(exc)) for u in fetch_urls]
        return await asyncio.to_thread(_rescue_extract, provider.name, fetch_urls, failed)
    if results and all(r.get("error") for r in results) and _rescue_eligible(provider):
        return await asyncio.to_thread(_rescue_extract, provider.name, fetch_urls, results)

    # Cache each successful fetch under the REQUESTED url it reports as its own — never by list
    # position: providers omit failed URLs or return successes out of request order, and a positional
    # write filed one page's text under another URL's key for the whole TTL. ``metadata.sourceURL``
    # counts because Keenable/Firecrawl put the requested URL there when ``url`` is the redirect target.
    # An entry naming no requested URL is served but not cached (a miss re-fetches; a mis-key poisons).
    requested = set(fetch_urls)
    for fetched in results:
        meta = fetched.get("metadata")
        source = meta.get("sourceURL") if isinstance(meta, dict) else None
        url = next((u for u in (fetched.get("url"), source) if u in requested), None)
        _content = fetched.get("raw_content", "") or fetched.get("content", "")
        if url and _content and not fetched.get("error"):
            extract_cache_put(url, _content, fetched.get("title", ""), format=format, provider=provider.name)
    return results


async def _extract_safe_urls(provider, safe_urls: List[str], format: Optional[str]) -> List[dict]:
    """Serve cache hits, fetch the rest, and merge back in ``safe_urls`` order.

    The disk cache (tools/web_result_cache.py) sits AFTER the secret-URL gate, SSRF gate, and provider
    resolution, and is gated per-URL on the website policy — a hit skips only the vendor call, never a
    control; policy-blocked URLs are cache misses. Keys include provider and format, so switching either
    within the TTL never serves the other's content."""
    from tools.web_result_cache import extract_cache_get
    from tools.website_policy import check_website_access as _check_site
    cached_results, fetch_urls, fetch_positions = {}, [], []
    for position, url in enumerate(safe_urls):
        try:
            _policy_block = _check_site(url)
        except Exception:  # noqa: BLE001 — policy errors fail open like dispatch
            _policy_block = None
        hit = extract_cache_get(url, format=format, provider=provider.name) if _policy_block is None else None
        if hit is not None:
            cached_results[position] = hit
        else:
            fetch_urls.append(url)
            fetch_positions.append(position)

    if not fetch_urls:
        return [cached_results[i] for i in range(len(safe_urls))]
    logger.info("Web extract via %s: %d URL(s)", provider.name, len(fetch_urls))
    results = await _dispatch_extract(provider, fetch_urls, format)
    if not cached_results:
        return results
    return _merge_in_order(len(safe_urls), cached_results, fetch_positions, fetch_urls, results)

# --- Per-URL fallback retry for returned transient errors ----------------------

# A provider timeout ("Scrape timed out after 60s", "request timeout") or an HTTP
# 5xx ("500 Server Error") is a transient page-level failure: the backend is
# alive, this one fetch blew up. Word boundaries keep "5000" or a "/p500/" URL
# fragment from masquerading as a status code.
_RETRY_TIMEOUT_RE = re.compile(r"\b(?:time[-\s]?out|timed[-\s]?out)\b", re.IGNORECASE)
_RETRY_HTTP_5XX_RE = re.compile(r"\b(?:5\d{2}|5xx)\b", re.IGNORECASE)


def _url_host(url: str) -> str:
    """Hostname of *url* for per-domain diagnostics; the raw string when unparsable."""
    try:
        return urlparse(url).hostname or url
    except ValueError:
        return url


def _policy_or_ssrf_refusal(entry: dict) -> bool:
    """True for website-policy / SSRF refusals — intentional outcomes a fallback
    provider must never re-fetch (that would route blocked content through
    another vendor)."""
    if entry.get("blocked_by_policy"):
        return True
    error = str(entry.get("error") or "").lower()
    return error.startswith("blocked:") or "blocked by website policy" in error


def _retryable_extract_error(entry: dict) -> bool:
    """Transient per-URL failures worth one retry on the fallback chain.

    Provider timeouts and HTTP 5xx only. Ordinary page errors (404, not-found,
    auth) and policy/SSRF refusals are final — retrying them burns a second
    vendor call to fail the same way."""
    error = str(entry.get("error") or "")
    if not error or _policy_or_ssrf_refusal(entry):
        return False
    return bool(_RETRY_TIMEOUT_RE.search(error) or _RETRY_HTTP_5XX_RE.search(error))


async def _retry_extract_errors_on_fallbacks(
    results: List[dict], fetch_urls: List[str], format: Optional[str], primary, _wsp_get_provider
) -> Tuple[List[dict], bool]:
    """Retry only the transient (timeout / HTTP 5xx) entries of a RETURNED primary
    extract on the configured ``web.extract_fallbacks`` chain.

    A returned error is not a dead backend (that path raises and falls back
    whole-batch in ``web_extract_tool``), so only the failing URLs are re-sent,
    through the same availability walk as ``_run_extract_fallbacks``. Successful
    fallback entries are merged back BY URL (providers may reorder or answer
    with the redirect target — ``metadata.sourceURL`` counts too); every other
    entry, including the original error when the retry also fails, is returned
    untouched.

    Returns ``(results, fallback_served)`` — ``fallback_served`` is True only
    when a fallback actually served a body, so the caller can keep fallback
    text out of the primary provider's cache.
    """
    retry: List[Tuple[int, str]] = []  # (position into results/fetch_urls, requested url)
    for position, entry in enumerate(results):
        if position >= len(fetch_urls) or not isinstance(entry, dict):
            continue
        if not entry.get("error") or not _retryable_extract_error(entry):
            continue
        url = entry.get("url")
        if url not in fetch_urls:
            url = fetch_urls[position]
        retry.append((position, url))

    if not retry:
        return results, False

    primary_name = getattr(primary, "name", "primary")
    for _position, url in retry:
        logger.warning(
            "web_extract: %s hit a timeout/5xx on %s — retrying that URL on the extract fallback chain",
            primary_name, _url_host(url),
        )

    from tools.web_tools import _run_extract_fallbacks  # late facade import — no cycle

    retry_urls = [url for _position, url in retry]
    try:
        retried = await _run_extract_fallbacks(
            retry_urls, format, primary,
            RuntimeError(f"{primary_name} returned a transient per-URL error for {len(retry_urls)} URL(s)"),
            _wsp_get_provider=_wsp_get_provider,
        )
    except Exception:  # noqa: BLE001 — a failed retry keeps the primary's original error
        logger.warning(
            "web_extract: fallback retry failed for %d timed-out/5xx URL(s); keeping original errors",
            len(retry_urls),
        )
        return results, False

    served: Dict[str, dict] = {}
    for entry in retried:
        if not isinstance(entry, dict) or entry.get("error"):
            continue
        meta = entry.get("metadata")
        source = meta.get("sourceURL") if isinstance(meta, dict) else None
        for url in (entry.get("url"), source):
            if url in retry_urls:
                served[url] = entry
                break
    if not served:
        return results, False

    merged = list(results)
    for position, url in retry:
        if url in served and merged[position].get("error"):
            merged[position] = served[url]
    return merged, True
