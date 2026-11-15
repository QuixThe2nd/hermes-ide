# RCA: interruption placeholder finalized as an answer

## Incident

- Date: 2026-11-15 16:01:07 AEDT.
- Discord thread: `1571381324525211700`.
- Agent session: `20261115_152825_98abe7c1`.
- Visible result: `[response interrupted]` was persisted and delivered as a completed answer.
- The gateway goal judge subsequently returned `continue` and started a new agent turn at 16:01:32.

## Evidence

Read-only inspection of `/root/.hermes/logs/agent.log` lines 4322-4338 established this order:

1. The Responses call completed after 29.5 seconds with 17 output tokens.
2. The agent ended with `text_response(finish_reason=stop)` and `response_len=22`.
3. The 22-character response was delivered normally.
4. Restart handling remained deferred; it did not interrupt this response.
5. The goal judge returned `continue`, and the gateway injected a goal-continuation user message.

The `post_api_request` record at `/root/.hermes/logs/cache-trace/trace.jsonl.2` line 968 identifies the active path as `api_mode=codex_responses`, with `finish_reason=stop`, 22 assistant-content characters, and zero tool calls. It contains no `InterruptedError`, `response.incomplete`, or terminal-status warning for this call.

A read-only query of `/root/.hermes/state.db` found three earlier hidden correction rows with empty visible `content` and `api_content='[response interrupted]'`, followed by message `1006376` with visible `content='[response interrupted]'`, `api_content=NULL`, and `finish_reason='stop'`. The visible row therefore passed through ordinary final-response persistence, not the hidden correction-row path.

## Root cause

`_normalize_codex_response()` treated a completed, tool-call-free Responses result containing only the reserved internal interruption placeholder as an ordinary nonempty answer and returned `finish_reason='stop'`. The active conversation loop consequently finalized it. The placeholder is reserved for non-final transcript repair and interruption scaffolding; it is not a substantive answer and must not cross the final-response boundary.

The gateway goal judge was downstream recovery, not the cause. Restart cancellation was also not the direct cause: normal completion and delivery preceded the deferred-restart messages.

The raw provider SSE payload is unavailable. The evidence proves that the normalized runtime response contained the placeholder and was accepted as final; it does not prove whether the model echoed earlier replay scaffolding or a provider component synthesized the text.

## Fix

`agent/codex_responses_adapter.py` now classifies an exact placeholder-only, tool-call-free Responses result as `finish_reason='incomplete'`. This reaches the executing conversation path through `CodexResponsesTransport.normalize_response()` and reuses `continue_codex_incomplete()` rather than introducing another retry mechanism.

The existing continuation stays within the same `AIAgent.run_conversation()` call, retains the original session and prompt-cache key, and is capped at three provider calls. Persistent placeholders return the existing typed partial result (`completed=False`, `partial=True`) instead of a completed placeholder answer.

Precedence is unchanged:

- failed/cancelled responses still raise before placeholder classification;
- structured tool calls still produce `tool_calls`;
- content filtering still produces `content_filter`;
- ordinary text and text merely mentioning the placeholder still produce `stop`;
- explicit interrupt handling, stream-retirement fences, and hidden correction rows are unchanged.

## Regression evidence

- `red-focused.log`: the isolated unfixed regression made one provider call and returned `[response interrupted]` with `finish_reason=stop`. Earlier entries also record the test-runner bootstrap failure and an initial false-positive harness run caused by the optional `inbox_sparks` pre-turn-end hook; the final regression disables that unrelated hook.
- Final post-rebase focused verification: eight tests pass with no retries. Four conversation tests cover two-call same-run recovery, the three-call persistent-placeholder bound, cancellation between the retained placeholder row and continuation, and terminal `response.failed` precedence. The recovery test also asserts that streaming emits the transient placeholder followed by the recovered answer; the fatal control preserves the original `server_error: provider terminal failure` and never enters incomplete continuation.
- `tests/gateway/test_codex_interruption_goal.py` passes through real `GatewayRunner._run_agent()`, `AIAgent`, Codex SSE normalization, async session storage, goal persistence, `_run_post_turn_hooks()`, and `GoalManager.evaluate_after_turn()`. Provider I/O is a deterministic local SSE stream and `judge_goal` is stubbed; turn execution and goal scheduling are not. The test observes two provider calls in one gateway turn with stable instructions and `prompt_cache_key`, one judgement containing only the recovered answer, persisted `status="done"` and `last_verdict="done"`, and no queued interruption continuation.
- Full post-rebase Codex verification: 53 adapter tests and 72 conversation tests pass. The same three unrelated existing failures remain: two compression fixtures are never invoked, and `test_codex_truncated_tool_call_is_retried_with_boosted_output_budget` stops at the existing truncated-tool refusal. Their fixtures contain no placeholder and do not enter the new classification branch; the baseline reproduction is in `remaining-codex-diagnostics.log`.
- Adjacent interrupt/finalizer/single-writer/incomplete-budget and gateway goal-verdict suites pass. Isolated reruns also pass for goal continuation drain (two tests) and goal automation (21 tests). An earlier combined run's setup error and retry were caused by giving the per-file test runner one shared `--basetemp`; final runs use a unique basetemp per file.
- Normal usage-terminated streaming and abrupt-drop controls pass (two tests). Compilation of the adapter, adapter tests, conversation tests, and gateway integration test succeeds; `git diff --check` is clean.

No database migration, deployed-data rewrite, service restart, or gateway goal-judge change is involved.
