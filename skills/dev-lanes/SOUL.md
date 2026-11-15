# Fork coding conventions (lane edition)

Applies when working in a hermes-agent / hermes-ide tree. The publishing agent
owns merging and PR mechanics; you own the code and its tests.

## Ownership

This codebase is yours: what it's for and how it's built are your call, and you'll keep building on it.

In the area this task touches, act as an owner, not a guest: merge duplicate code into one implementation instead of adding a parallel one, and fix related bugs you find rather than only mentioning them. Put each cleanup or adjacent fix in its own commit with its own test, and list it in your final report, so the publishing agent can see what went beyond the brief. Unrelated areas stay out of the diff; report observations instead. For static analysis, ownership applies to the working repository, never to analyzed target artifacts.

## Commits
- Conventional commits: `feat(lanes): ...`, `fix(gateway): ...`. Scope = the area touched.
- One logical change per commit. Never mix a refactor with a behavior change.
- Never amend or force-push a branch another agent may have fetched.

## Tests
- Tests BEFORE (reproduce/fail on current code) + AFTER (pass, including adjacent tests in the same file).
- Run with the canonical runner: `scripts/run_tests.sh tests/<area>/ -q`.
- No change-detector tests (no `assert len(models) == 8`); assert relationships, not snapshots.
- Never read source code inside tests to assert on its text.
- Never modify an existing test to make your change pass without stating why in the PR body — tests are contracts. If the contract changed, say so explicitly.

## Diff discipline
- No noise in the diff: no drive-by reformatting, no re-export shims, no comment churn. Merging duplicates and fixing related bugs (see Ownership) is not noise.
- Respect facade + siblings layout: new behavior goes in a topical sibling, not appended to a facade.
- Table-driven over `if/elif` ladders ≥ 4 branches.
- Never import from compat-pointer paths; import from the defining module.
- Config in config.yaml, secrets in .env only. No new `HERMES_*` env vars for behavior.

## Reporting
- Report what you verified (exact commands + counts), not what you believe.
- State file:line when citing code. Never claim CI status you cannot see.
- If the task itself turns out bigger than briefed (new subsystem, different design, another area), stop and report scope. Related cleanup under Ownership does not need that stop; it needs its own commit and a line in the report.
