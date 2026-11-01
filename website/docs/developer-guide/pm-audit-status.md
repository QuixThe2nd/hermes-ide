# PM audit remediation status

This change integrates repairs from the aggregate branch audit. It is not a
release certificate. The audit compared `49945b14029e09fef608db9ede899377cdb54e11`
with merge base `433f7196760e5d76f77ea6d5464ee7f1b602a4ee`.

## Implemented repairs

| Findings | Implementation |
| --- | --- |
| C01, C11, C12, C24 | Remove the deleted TLS import; fix ACP file decoding, proxy path ownership, and the monitoring SDK call. |
| C02 | Restore code-execution schema, registration, and child lifecycle handling. |
| C03, C04, C17 | Keep update metadata commands read-only; synchronize plugin updates; compare feed identities correctly. |
| C05, C27, C28 | Include managed Python in the install closure; repair installer stage dispatch and runtime selection. |
| C06, C07, C09 | Remove the unsupported SCM surface. Retain Scheduled Task supervision and protect unrelated runtimes on desktop quit. |
| C08, C19 | Correct the App Installer checker and restore meaningful update states. |
| C10 | Repair uninstall dispatch and owned-link removal. |
| C13, C14 | Prepare one complete dependency environment before publication. Preserve declared constraints and explicit pins; permit compatible transitive upgrades. Infrastructure failures do not disable plugins. |
| C15, C18 | Retain recoverable tool-store entries on failed publication. Use atomic facts/config writes and context-local receipts. |
| C16, C29 | Migrate removed dependency helpers and restore missing-dependency hints. Run the embedded Hindsight service from its separate environment. |
| C20, C21 | Correct platform-test selection and make the removed-import guard inspect actual source. |
| C22, C23 | Publish immutable release artifacts before feeds and resolve FFmpeg artifacts by target. |
| C25 | Preserve paused downloads, resume handles, and single-worker ownership. |
| C26 | Wire the boot and plugin-cadence paths to their existing owners. |

Implementation does not mean that every platform acceptance test is complete.
The updater, backup, setup, voice-text helpers, and several plugins now use
one implementation per reconciled concern. Runtime paths have a shared owner
in `hermes_cli/runtime_paths.py`.

## Verification limits

The first broad native-Windows run completed its file list with 43,358 passed,
257 failed, and 1,390 skipped tests. Eight files had no completed result.
Repairs overlapped that run, so it does not certify the final tree.

A later run selected all changed Python test files plus the broad run's failed
files. Its 206 files reported 4,630 passed, one failed, and 330 skipped tests.
There were no files without completed results. The remaining failure was a
Hindsight setup fixture that crossed into dependency installation. The fixture
now isolates that boundary. The final six-file rerun reported 234 passed and
two skipped tests, including Hindsight setup, PM state, admission, environment
sanitization, and terminal timeout output. A separate four-file store review
rerun reported 83 passed and one skipped test.

Desktop renderer tests reported 7,225 passed. Electron tests reported 2,295
passed and 23 skipped. Both desktop TypeScript checks also passed after the
locked development packages were restored. These are unit/type checks, not an
installer or signed-release lifecycle test.

A real Hindsight side-environment daemon started in a disposable home, answered
its health endpoint with HTTP 200, and stopped successfully. This verifies
startup, health, and shutdown, not an LLM retain/recall operation.

## Remaining acceptance work

- Run the full Python suite on one fixed final tree and the relevant native
  platform CI lanes. Targeted reruns do not substitute for this gate.
- Complete packaged install, update, relaunch, and uninstall checks. Verify
  that the messaging gateway survives ordinary desktop quit.
- Complete the remaining updater-strategy and warning-surface contracts from
  the audit. Manual update checks alone do not prove those surfaces.
- Define and verify safe collection of obsolete dependency generations while
  older processes still use them. The current conservative retention avoids
  deleting a live generation but does not bound storage use.
- Strengthen publication recovery for process death between plugin-config and
  runtime-facts writes. Exception rollback is tested; those two files are not
  one crash-atomic transaction.
- Complete receipt correlation and failure-reporting checks across nested PM
  and updater operations.
- Reconcile the remaining documentation and complete the final aggregate diff
  review. The bounded pre-commit review does not cover every moved updater line.

The external audit directory contains the original reports, per-batch logs,
review adjudication, and the exact test-file selection. No remote release,
service installation, or push is implied by this commit.
