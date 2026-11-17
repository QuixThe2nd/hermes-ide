# Hindsight server semantics — source-backed (live container `hindsight`, read-only inspection, 2026-11-18)

Container image `hindsight-jev-proxy:nqhs_3bl-production`; paths under `/app/api/hindsight_api/`.

## 1. Sync retain ignores `operation_id`
`api/http.py` retain endpoint (~L10197-10285): `request.operation_id` is read ONLY in the
`if request.async_:` branch (single-strategy check + passed to `submit_async_retain`).
The sync branch calls `retain_batch_async(...)` with no operation id. A sync retain has
"no operation row and no ack" (comment at ~L10280, issue #4526) — it runs inline under
`run_task_cancellable_on_disconnect`.

## 2. Sync quota failure does NOT guarantee zero commits in general
`engine/retain/fact_extraction.py` L2740-2796 (`return_exceptions=True` gather):
- If ALL failed chunks are `ProviderRateLimitResetError` → raises
  `ProviderRateLimitResetError("Fact extraction deferred by provider quota: N/M chunks failed...")`
  with `retry_at = max(err.retry_at)`.
- "partial extraction is not acceptable" — the failing mini-batch commits NOTHING.
BUT `engine/retain/orchestrator.py` `_run_streaming_retain` (L2455+, docstring):
"Each mini-batch ... commits to the DB before the next batch starts."
=> earlier mini-batches of the SAME document may already be committed when the quota
error raises. Zero commits is guaranteed only when the item fits one mini-batch.

## 3. Replay is RESUMABLE, not duplicate (chunk-hash recovery)
`orchestrator.py` L2469-2555: before extraction, `recovery_chunk_hashes(document_id,
content_hash)` is probed; on match: "RECOVERY: found N already-committed chunks — will
skip matching and preserve existing data". Skipped when `force_reextract`. Works for BOTH
sync and async paths (shared streaming producer). Requires: same `document_id`, identical
content (sha256 over sanitized text). Unit-level facts of committed chunks are preserved.

## 4. Async submission is idempotent by caller-supplied `operation_id`
`engine/memory_engine.py` `submit_async_retain` (L22351+): docstring "Re-submitting with
the same id returns the original operation and creates no new work, so a client that
retries after a lost acknowledgement does not enqueue a duplicate." Primary-key
collision is the concurrency authority (unique-violation backstop). Constraint
(http.py): `operation_id` requires all items resolve to a single strategy (else 400).
Async batch with duplicate document_ids is rejected (400) — one document_id per item.

## 5. `POST /operations/{id}/retry` re-queues failed AND cancelled
`memory_engine.py` `retry_operation` (L21440+): single conditional UPDATE
`SET status='pending', error_message=NULL, next_retry_at=NULL, worker_id=NULL,
claimed_at=NULL, retry_count=0 WHERE status IN ('failed','cancelled')`. For
`batch_retain` parents: re-queues failed/cancelled children, revives parent when a
non-completed child exists. 409 otherwise. THE server-owned durable retry surface.

## 6. `DELETE /operations/{id}` (cancel) is cooperative
http.py docstring: "'pending' never starts; 'processing' is cancelled cooperatively:
row marked 'cancelled' immediately, worker stops at its next checkpoint, so work
already in flight may finish the batch it is on." => cancelled-after-processing can
have partial commits; `retry_operation` then resumes via chunk-hash recovery.
`DELETE /operations/{id}/delete` removes only TERMINAL records.

## 7. Worker auto-retry window is bounded (default 4 minutes)
`memory_engine.py` ~L4819: `retry_count < config.worker_max_retries` →
`RetryTaskAt(now + worker_task_retry_backoff_seconds)`, else re-raise → row `failed`.
Defaults: `worker_max_retries=3`, backoff 60s (config.py L5164, "4-minute total window").
=> a provider quota reset hours away lands the operation in `failed`; someone must
call `retry_operation` after the reset.

## 8. No automatic reaper for crashed `processing` rows
Cancel docstring: "This also clears operations stranded in 'processing' by a crashed
worker." Claim path (`db/ops.py` `claim_tasks`) only claims `status='pending'`.
Crash recovery = cancel (processing→cancelled) + retry_operation.

## Verdict for the review's question 3 (exactly-once for the original sync call)
NOT claimable: the first sync attempt has no operation id (server ignores it for sync)
and may partially commit earlier mini-batches before the quota raise. What IS supported:
- submission-level exactly-once for ASYNC replays (idempotent by caller operation_id);
- chunk-level no-duplicate-commits on replay (hash recovery), contingent on the client
  replaying the SAME document_id + byte-identical content;
- server-owned re-execution via retry_operation for failed/cancelled rows.
=> Client contract must be phrased "idempotent replay / resumable, no duplicate chunk
commits", never "exactly once". The server mechanisms DO support the durable-backlog
contract; no server changes required.
