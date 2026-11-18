# Verification commands

<!-- no-tmp: ok — captured command transcript kept as review evidence, not authored text -->
All commands ran from the isolated checkout with `HERMES_TEST_ISOLATION=1`. `HERMES_PYTHON=/root/hermes-agent/.venv/bin/python` selects the available pytest environment without modifying it. `HOME=/tmp/hermes-interruption-test-home` keeps the checkout's location under `/root/.hermes` from triggering the suite's real-home I/O tripwire when the runner creates its isolated test home.

## Unfixed reproduction

```bash
<!-- no-tmp: ok — captured command transcript kept as review evidence, not authored text -->
env HOME=/tmp/hermes-interruption-test-home \
  HERMES_TEST_ISOLATION=1 \
<!-- no-tmp: ok — captured command transcript kept as review evidence, not authored text -->
  HERMES_HOME=/tmp/hermes-interruption-runtime-red \
  HERMES_PYTHON=/root/hermes-agent/.venv/bin/python \
  scripts/run_tests.sh tests/agent/test_run_agent_codex_responses.py \
  -k test_run_conversation_recovers_interrupted_placeholder_in_same_run -q
```

Final unfixed result: one test failed; one provider call returned `[response interrupted]` as `final_response`, with `finish_reason=stop`. Complete output, including the earlier runner-bootstrap and optional-plugin harness iterations, is in `red-focused.log`.

## Focused fixed regressions

```bash
<!-- no-tmp: ok — captured command transcript kept as review evidence, not authored text -->
env HOME=/tmp/hermes-interruption-test-home \
  HERMES_TEST_ISOLATION=1 \
<!-- no-tmp: ok — captured command transcript kept as review evidence, not authored text -->
  HERMES_HOME=/tmp/hermes-interruption-runtime-final \
  HERMES_PYTHON=/root/hermes-agent/.venv/bin/python \
  scripts/run_tests.sh \
  tests/agent/test_run_agent_codex_responses.py \
  tests/agent/test_codex_responses_adapter.py \
  -k 'interrupted_placeholder or interruption_placeholder or placeholder_preserves' -q
```

Result: five tests passed. Output: `green-final.log`.

## Adjacent interruption and streaming suites

```bash
<!-- no-tmp: ok — captured command transcript kept as review evidence, not authored text -->
env HOME=/tmp/hermes-interruption-test-home \
  HERMES_TEST_ISOLATION=1 \
<!-- no-tmp: ok — captured command transcript kept as review evidence, not authored text -->
  HERMES_HOME=/tmp/hermes-interruption-runtime-adjacent \
  HERMES_PYTHON=/root/hermes-agent/.venv/bin/python \
  scripts/run_tests.sh \
  tests/agent/test_run_agent_codex_responses.py \
  tests/agent/test_codex_responses_adapter.py \
  tests/agent/test_turn_api_call_interrupt.py \
  tests/agent/test_partial_stream_finish_reason.py \
  tests/agent/test_steer.py \
  tests/agent/test_stream_single_writer.py \
  tests/agent/test_turn_finalizer_interrupt_alternation.py \
  tests/agent/test_codex_incomplete_budget_escalation.py -q
```

Outcome: 172 tests passed before the 600-second command limit; five unrelated steer tests and twelve Codex tests failed. The optional pre-turn-end hook caused nine of the Codex failures by consuming unscripted extra responses. Subsequent isolation and diagnostics are recorded in `hook-isolation-check.log`, `remaining-codex-diagnostics.log`, and `steer-diagnostic.log`. Output: `adjacent-tests.log`.

## Final full Codex suites

```bash
<!-- no-tmp: ok — captured command transcript kept as review evidence, not authored text -->
env HOME=/tmp/hermes-interruption-test-home \
  HERMES_TEST_ISOLATION=1 \
<!-- no-tmp: ok — captured command transcript kept as review evidence, not authored text -->
  HERMES_HOME=/tmp/hermes-interruption-final-isolated \
  HERMES_PYTHON=/root/hermes-agent/.venv/bin/python \
  scripts/run_tests.sh \
  tests/agent/test_run_agent_codex_responses.py \
  tests/agent/test_codex_responses_adapter.py -q
```

Outcome: 123 tests passed and three unrelated existing tests failed; details are in `remaining-codex-diagnostics.log`. Output: `final-codex-suites-isolated.log`. The pre-isolation attempt timed out after 300 seconds and is retained in `final-codex-suites.log`.

## Partial-stream controls

The full `tests/agent/test_partial_stream_finish_reason.py` command exceeded 180 seconds without a test summary; its output is retained in `partial-stream-tests.log`. The relevant normal-stream and dropped-stream controls were then run explicitly:

```bash
<!-- no-tmp: ok — captured command transcript kept as review evidence, not authored text -->
env HOME=/tmp/hermes-interruption-test-home \
  HERMES_TEST_ISOLATION=1 \
<!-- no-tmp: ok — captured command transcript kept as review evidence, not authored text -->
  HERMES_HOME=/tmp/hermes-interruption-partial-focused \
  HERMES_PYTHON=/root/hermes-agent/.venv/bin/python \
  scripts/run_tests.sh tests/agent/test_partial_stream_finish_reason.py \
  -k 'test_text_stream_with_final_usage_chunk_completes_as_stop or test_text_stream_abrupt_drop_without_usage_still_returns_stub' -q
```

Result: two tests passed. Output: `partial-stream-focused.log`.

## Compile and import checks

```bash
/root/hermes-agent/.venv/bin/python -m py_compile \
  agent/codex_responses_adapter.py \
  tests/agent/test_codex_responses_adapter.py \
  tests/agent/test_run_agent_codex_responses.py

<!-- no-tmp: ok — captured command transcript kept as review evidence, not authored text -->
env HOME=/tmp/hermes-interruption-test-home \
<!-- no-tmp: ok — captured command transcript kept as review evidence, not authored text -->
  HERMES_HOME=/tmp/hermes-interruption-import-a \
  /root/hermes-agent/.venv/bin/python \
  -c 'import agent.agent_runtime_helpers; import agent.codex_responses_adapter'

<!-- no-tmp: ok — captured command transcript kept as review evidence, not authored text -->
env HOME=/tmp/hermes-interruption-test-home \
<!-- no-tmp: ok — captured command transcript kept as review evidence, not authored text -->
  HERMES_HOME=/tmp/hermes-interruption-import-b \
  /root/hermes-agent/.venv/bin/python \
  -c 'import agent.codex_responses_adapter; import agent.agent_runtime_helpers'

git diff --check
```

Results: all four commands exited zero. `compile.log` records the empty successful `py_compile` output.

## Post-rebase final verification

These runs used the scratch environment below. Each invocation received its own `--basetemp`: `scripts/run_tests.sh` starts one pytest process per file, so concurrent files must not share a pytest temporary root.

```bash
export HOME=/root/.hermes/cache/scratch/interruption-runtime-pr738-tests/home
export HERMES_HOME=$HOME/.hermes
export HERMES_TEST_ISOLATION=1
export PYTHONDONTWRITEBYTECODE=1
export HERMES_PYTHON=/root/hermes-agent/.venv/bin/python
export TEST_ROOT=/root/.hermes/cache/scratch/interruption-runtime-pr738-tests
```

Focused recovery, failure-precedence, adapter-boundary, and real-gateway checks:

```bash
scripts/run_tests.sh tests/agent/test_run_agent_codex_responses.py \
  -k 'test_run_conversation_recovers_interrupted_placeholder_in_same_run or test_run_conversation_interrupt_after_placeholder_skips_continuation_request or test_run_conversation_failed_placeholder_does_not_start_continuation or test_run_conversation_bounds_persistent_interrupted_placeholders' \
  --basetemp=$TEST_ROOT/basetemp-final-agent-11 -q
scripts/run_tests.sh tests/agent/test_codex_responses_adapter.py \
  -k placeholder --basetemp=$TEST_ROOT/basetemp-final-adapter-11 -q
scripts/run_tests.sh tests/gateway/test_codex_interruption_goal.py \
  --basetemp=$TEST_ROOT/basetemp-final-gateway-11 -q
```

Results: four conversation tests, three adapter tests, and one gateway integration test passed; no retries or flakes. The gateway test proves same-turn recovery, stable prompt-cache identity, judgement of only the recovered answer, persisted `done` state, and no queued continuation.

Full Codex modules were run separately:

```bash
scripts/run_tests.sh tests/agent/test_run_agent_codex_responses.py \
  --basetemp=$TEST_ROOT/basetemp-codex-agent-standalone-9 -q
scripts/run_tests.sh tests/agent/test_codex_responses_adapter.py \
  --basetemp=$TEST_ROOT/basetemp-codex-adapter-standalone-12 -q
```

Results: the conversation module had 72 passes and the three known unrelated compression/truncated-tool failures; the adapter module had 53 passes. A separate adapter-plus-conversation invocation produced the same 125 passes and three failures.

The adjacent agent suites and `tests/gateway/test_goal_verdict_send.py` passed in the combined adjacency run. The two files affected by the earlier shared-basetemp race were rerun independently:

```bash
scripts/run_tests.sh tests/gateway/test_goal_continuation_drain.py \
  --basetemp=$TEST_ROOT/basetemp-drain-standalone-10 -q
scripts/run_tests.sh tests/gateway/test_goal_auto.py \
  --basetemp=$TEST_ROOT/basetemp-goal-auto-standalone-10 -q
scripts/run_tests.sh tests/agent/test_partial_stream_finish_reason.py \
  -k 'test_text_stream_with_final_usage_chunk_completes_as_stop or test_text_stream_abrupt_drop_without_usage_still_returns_stub' \
  --basetemp=$TEST_ROOT/basetemp-stream-controls-10 -q
```

Results: two drain tests, 21 goal-automation tests, and two stream controls passed with no retries.

Final static checks:

```bash
env PYTHONDONTWRITEBYTECODE=1 /root/hermes-agent/.venv/bin/python -m py_compile \
  agent/codex_responses_adapter.py \
  tests/agent/test_codex_responses_adapter.py \
  tests/agent/test_run_agent_codex_responses.py \
  tests/gateway/test_codex_interruption_goal.py
git diff --check
```

Both commands exited zero.
