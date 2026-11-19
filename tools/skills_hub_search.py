"""Skills Hub discovery: the source router and parallel/unified search across
source adapters.

Split out of ``tools/skills_hub.py``; hub state (cache dir, ``TapsManager``, JSON
cache reads) is still read from there at call time.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from tools.skills_hub_clawhub import ClawHubSource
from tools.skills_hub_github import GitHubAuth, GitHubSource, _filter_results_by_provider, _provider_filter_of
from tools.skills_hub_models import SkillMeta, SkillSource, TRUST_RANK, _dedupe_by_trust
from tools.skills_hub_official import OptionalSkillSource
from tools.skills_hub_skillssh import SkillsShSource
from tools.skills_hub_sources import BrowseShSource, LobeHubSource, UrlSource, WellKnownSkillSource

# Log-record parity with the origin module.
logger = logging.getLogger("tools.skills_hub")


def create_source_router(auth: Optional[GitHubAuth] = None) -> List[SkillSource]:
    """All configured source adapters, in priority order."""
    from tools.skills_hub import TapsManager
    if auth is None:
        auth = GitHubAuth()
    return [
        OptionalSkillSource(auth=auth),   # official optional skills (highest priority)
        SkillsShSource(auth=auth),
        WellKnownSkillSource(),
        UrlSource(),                      # direct HTTP(S) URL to a SKILL.md
        GitHubSource(auth=auth, extra_taps=TapsManager().list_taps()),
        ClawHubSource(),
        LobeHubSource(),
        BrowseShSource(),                 # browse.sh site-specific browser skills
    ]


def _search_one_source(
    src: SkillSource, query: str, limit: int, provider_filter: str = "",
) -> Tuple[str, List[SkillMeta]]:
    """Search a single source.  Runs in a thread for parallelism."""
    try:
        # GitHubSource mixes providers in one catalog. Narrow before its top-N
        # cut so another provider cannot crowd every requested match out.
        if provider_filter and isinstance(src, GitHubSource):
            return src.source_id(), src.search(query, limit=limit, provider_filter=provider_filter)
        return src.source_id(), src.search(query, limit=limit)
    except Exception as e:
        logger.debug("Search failed for %s: %s", src.source_id(), e)
        return src.source_id(), []


def _select_active_sources(sources: List[SkillSource], source_filter: str) -> List[SkillSource]:
    """Sources to query for ``source_filter``.

    A provider filter (nvidia/openai/...) is not a source id — the data lives
    in the github source under ``extra.provider`` — so it selects like
    "all". Mixed-provider sources narrow before their top-N cut; the walker
    cuts every source's results. "official" is always queried alongside an
    explicit source filter.
    """
    effective = "all" if _provider_filter_of(source_filter) else source_filter
    return [
        src for src in sources
        if effective == "all" or src.source_id() in (effective, "official")
    ]


def _fan_out(
    active: List[SkillSource], query: str, per_source_limits: Dict[str, int], provider_filter: str,
    deadline: float, on_source_done: Optional[Any], all_results: List[SkillMeta],
    source_counts: Dict[str, int], timed_out_ids: List[str],
) -> None:
    """Query ``active`` in parallel until ``deadline`` (monotonic), merging into the accumulators."""
    from concurrent.futures import as_completed
    from tools.daemon_pool import DaemonThreadPoolExecutor

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        timed_out_ids.extend(src.source_id() for src in active)
        return
    # Not a ``with`` block: its shutdown(wait=True) would block on a slow source
    # (ClawHub) for minutes and defeat ``overall_timeout``. Daemon workers so an
    # abandoned source cannot block interpreter exit either.
    pool = DaemonThreadPoolExecutor(max_workers=min(len(active), 8))
    futures = {
        pool.submit(
            _search_one_source, src, query, per_source_limits.get(src.source_id(), 50), provider_filter,
        ): src.source_id()
        for src in active
    }
    try:
        for fut in as_completed(futures, timeout=remaining):
            try:
                sid, results = fut.result(timeout=0)
                if provider_filter:
                    # One owner for the merged provider cut: sources that cannot
                    # filter per-tap (official, url, ...) are narrowed here, so
                    # every caller (CLI, TUI gateway, dashboard) sees one rule.
                    results = _filter_results_by_provider(results, provider_filter)
                source_counts[sid] = len(results)
                all_results.extend(results)
                if on_source_done:
                    on_source_done(sid, len(results))
            except Exception:
                pass
    except TimeoutError:
        late = [futures[f] for f in futures if not f.done()]
        timed_out_ids.extend(late)
        if late:
            logger.debug("Skills browse timed out waiting for: %s", ", ".join(late))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def parallel_search_sources(
    sources: List[SkillSource], query: str = "", per_source_limits: Optional[Dict[str, int]] = None,
    source_filter: str = "all", overall_timeout: float = 30, on_source_done: Optional[Any] = None,
) -> Tuple[List[SkillMeta], Dict[str, int], List[str]]:
    """Search all sources in parallel with an overall timeout.

    Returns ``(all_results, source_counts, timed_out_ids)``. *on_source_done*
    is an optional ``(source_id, count) -> None`` progress callback. Under a
    provider filter every source's results are narrowed before they are
    counted and merged, so callers need no provider logic of their own.
    """
    per_source_limits = per_source_limits or {}
    active = _select_active_sources(sources, source_filter)
    provider_filter = _provider_filter_of(source_filter)
    all_results: List[SkillMeta] = []
    source_counts: Dict[str, int] = {}
    timed_out_ids: List[str] = []
    if not active:
        return all_results, source_counts, timed_out_ids

    deadline = time.monotonic() + overall_timeout
    _fan_out(active, query, per_source_limits, provider_filter, deadline, on_source_done,
             all_results, source_counts, timed_out_ids)
    return all_results, source_counts, timed_out_ids


def unified_search(query: str, sources: List[SkillSource],
                   source_filter: str = "all", limit: int = 10) -> List[SkillMeta]:
    """Search all sources (in parallel) and merge results."""
    all_results, _, _ = parallel_search_sources(sources, query=query, source_filter=source_filter, overall_timeout=30)
    deduped = _dedupe_by_trust(all_results)
    # Stable-sort by trust before truncating so the limit cut never drops a
    # builtin/official entry because a high-volume community source finished
    # first; insertion order is preserved within each rank.
    deduped.sort(key=lambda r: -TRUST_RANK.get(r.trust_level, 0))
    return deduped[:limit]
