# Papercut stage work orders

One template per stage below, delimited by `<!-- template: <stage> -->` markers.
`render_stage_prompt()` in `lifecycle.py` picks the block and substitutes the
record fields. Keep prose free of literal curly braces — the renderer is
`str.format`.

<!-- template: shared-header -->
You are working ONE papercut end-to-end: id `{id}`, summary "{summary}".

Record: component `{component}`, severity {severity}, occurrences {occurrences},
category {category}, first seen in session `{session_id}`.

Observed:
{observed}

Suggested fix:
{suggested_fix}

Workaround used at the time:
{workaround}

Human steer notes since the last stage change:
{steer_notes}

When your stage's work is done you MUST finish by calling the `papercuts` tool
with action `advance` (id `{id}`, to_stage, note, pr_url when a PR was opened).
That single call records the outcome and posts it to the papercut's Discord
thread. The `advance` note is what the owner actually reads in Discord: lead
with the plain-language verdict and why it matters in at most ~3 short
sentences. Put technical evidence (commands, SHAs, file:line) after that under
`Evidence:`, clearly secondary; never open with machinery or an audit dump.
Never edit the events journal by hand and never post to Discord directly —
`advance` owns both.

<!-- template: triage -->
Triage the papercut {id}.

1. Staleness: check the current state of the component on github/main. The
   papercut may already be fixed — look at recent commits touching the
   component before reproducing anything.
2. Reproduce the friction if reproduction is feasible from the repo; cite the
   exact command, file, and observed output.
3. Decide and `advance`:
   - already fixed or not reproducible → to_stage `closed` with evidence
     (commit SHA, command output) in the note.
   - real and fixable → to_stage `fixing` with a concrete fix brief in the
     note: files to touch, approach, test plan.
   - needs a human decision or credentials you lack → to_stage `needs_human`
     with the precise question in the note.

Be decisive: this papercut has been seen {occurrences} time(s); the point of
triage is to move it, not to describe it again.

<!-- template: fixing -->
Implement the fix brief for papercut {id}.

- Work through the coding lane, never by editing source directly in this
  session: use delegate_backend_dev / client_dev / ui_dev where one fits, or
  delegate_agent with backend=claude otherwise. The fixing lane writes the
  code; you supervise and verify.
- Tests FIRST: write or locate the failing test that pins the friction before
  touching the fix.
- Open a pull request against the base repo with the fix and its tests.
- Then `advance`: to_stage `verifying`, note = what changed and why, pr_url =
  the pull request URL.
- If the brief turns out wrong or the fix is not viable, `advance` to
  `needs_human` with what blocked you.

<!-- template: verifying -->
Verify the fix for papercut {id}.

- PR: {pr_url}
- The fix must be MERGED and the fix commit present in the live tree before
  verification counts. If the PR is still open, wait/re-check once, then
  `advance` back to `fixing` with the reason if it was rejected or stalled.
- Re-run the ORIGINAL symptom from the papercut's Observed section above where
  possible; cite real command output.
- On success `advance`: to_stage `closed`, note = the evidence (merged commit
  SHA + the re-run output that shows the friction gone).
