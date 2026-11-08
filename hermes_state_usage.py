"""Token/usage accounting mixin for SessionDB: the coalescing background token writer,
per-model usage rows, and billing-route columns. Writer thread state lives on the instance."""

from __future__ import annotations

import atexit
import contextlib
import logging
import threading
import time
import weakref
from typing import Any, Dict, List, Optional, Tuple

# caplog tests pin the "hermes_state" logger name.
logger = logging.getLogger("hermes_state")

_TOKEN_COUNTERS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")


def _token_update_sql(delta: bool) -> str:
    """``UPDATE sessions`` for one usage report: *delta* adds to the stored counters (CLI
    per-call path), otherwise sets them (gateway cumulative path). Cost/route columns
    COALESCE-fill either way (statement text is pinned by the SQL trace harness)."""
    def add(col: str) -> str:  # "col + ?" / "COALESCE(col, 0) + ?" in delta mode, bare "?" otherwise
        return f"{col} + ?" if delta else "?"
    def add0(col: str) -> str:
        return f"COALESCE({col}, 0) + ?" if delta else "?"
    counters = "".join(f"                   {c} = {add(c)},\n" for c in _TOKEN_COUNTERS)
    estimated = "COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0)" if delta else "COALESCE(?, 0)"
    return (
        "UPDATE sessions SET\n" + counters
        + f"""                   estimated_cost_usd = {estimated},
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE {add0("actual_cost_usd")}
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?),
                   api_call_count = {add0("api_call_count")}
                   WHERE id = ?"""
    )


_TOKEN_UPDATE_ABSOLUTE_SQL = _token_update_sql(delta=False)
_TOKEN_UPDATE_DELTA_SQL = _token_update_sql(delta=True)

_MODEL_USAGE_UPSERT_SQL = """INSERT INTO session_model_usage (
                   session_id, model, billing_provider, billing_base_url, billing_mode,
                   task, api_call_count, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, reasoning_tokens,
                   estimated_cost_usd, actual_cost_usd, cost_status, cost_source,
                   first_seen, last_seen
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, model, billing_provider, billing_base_url, billing_mode, task)
               DO UPDATE SET
                   api_call_count = api_call_count + excluded.api_call_count,
                   input_tokens = input_tokens + excluded.input_tokens,
                   output_tokens = output_tokens + excluded.output_tokens,
                   cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
                   cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens,
                   reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens,
                   estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd,
                   actual_cost_usd = actual_cost_usd + excluded.actual_cost_usd,
                   cost_status = COALESCE(excluded.cost_status, cost_status),
                   cost_source = COALESCE(excluded.cost_source, cost_source),
                   last_seen = excluded.last_seen"""


# Kwargs forwarded verbatim from update_token_counts / record_auxiliary_usage into
# _record_model_usage (the per-route attribution row).
_MODEL_USAGE_FIELDS = frozenset((
    "model", "billing_provider", "billing_base_url", "billing_mode", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "estimated_cost_usd",
    "actual_cost_usd", "cost_status", "cost_source", "api_call_count"))


class SessionUsageMixin:
    """Coalesced token writer, per-model usage rows, billing route."""

    def _apply_claimed_batch(self, batch) -> None:
        """Apply a batch whose ``busy`` flag the caller already claimed, then release."""
        try:
            self._apply_token_batch(batch)
        finally:
            with self._token_queue_cond:
                self._token_writer_busy = False
                self._token_queue_cond.notify_all()

    def auxiliary_usage_by_task(self, session_id: str) -> Dict[str, Dict[str, float]]:
        """Per-task auxiliary usage (``task != ''``: vision, compression, title_generation, ...) summed
        over the session's compression lineage. Aux calls bill to the id the turn STARTED with while
        compression mints child ids mid-turn, so a single-id read misses rows (#112848)."""
        if not session_id:
            return {}
        chain = self._session_lineage_root_to_tip(session_id)
        # An explicit ``/branch`` copy owns its spend: cut the walk at the nearest branch node so a
        # resumed branch never absorbs aux rows billed to its source (hermes_state_messages does the same).
        for i in range(len(chain) - 1, -1, -1):
            if self._is_explicit_branch_session(chain[i]):
                chain = chain[i:]
                break
        rows = self._read_all(
            f"""SELECT task,
                       COALESCE(SUM(api_call_count), 0) AS api_calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                       COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                       COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
                  FROM session_model_usage
                 WHERE session_id IN ({','.join('?' * len(chain))}) AND task != ''
                 GROUP BY task""",
            chain,
        )
        return {row["task"]: {k: row[k] for k in row.keys() if k != "task"} for row in rows}

