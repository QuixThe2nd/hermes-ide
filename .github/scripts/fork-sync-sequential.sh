#!/usr/bin/env bash
# Sequential fork-sync: import missing upstream commits ONE original commit
# at a time, pushing each merge separately.
#
# Why a script instead of inline YAML: the merge loop has to be runnable
# outside GitHub Actions so the acceptance harness can drive this exact file
# with real git against local bare remotes. Requires: git, jq, curl (only
# when alerting).
#
# Configuration is environment-only — workflow_dispatch inputs reach us as
# env values (never interpolated into a shell string) and every value is
# validated against a strict pattern before git ever sees it.
#
#   SYNC_REMOTE          remote name of the fork push target        (origin)
#   SYNC_TARGET_BRANCH   branch on the fork to update              (main)
#   SYNC_UPSTREAM        remote name of upstream                    (upstream)
#   SYNC_UPSTREAM_REF    upstream ref to import                     (main)
#   SYNC_UPSTREAM_URL    if set and the remote is unknown, add it with this URL
#   SYNC_MAX_COMMITS     stop after N merge+push units; 0 = unlimited (0)
#   SYNC_DRY_RUN         true = merge locally, but never push, never alert (false)
#   SYNC_ALERT_WEBHOOK   optional webhook notified (compact JSON) on failure
#   SYNC_RUN_URL         run URL included in failure output and alerts
#   SYNC_DETAIL_FILE     where the machine-readable failure JSON is written
#
# Algorithm: freeze the fetched upstream tip; enumerate missing commits with
# `git rev-list --reverse --topo-order <tip> --not HEAD` (parents strictly
# before children); for each candidate that is not already an ancestor of
# HEAD, require `git rev-list --count HEAD..<candidate>` == 1 (all of its
# parents, including for merge commits, were already imported), merge it with
# --no-ff so the original commit survives verbatim as the second parent, push
# HEAD to the target, then verify the remote holds that exact SHA (an
# ancestor is also accepted — the branch advanced concurrently). The first
# conflict or rejected push stops the run with a nonzero exit; commits
# already pushed stay pushed, and the next run resumes purely from Git
# ancestry — there is no separate cursor. No force, no rebase, no batching.

set -euo pipefail

SYNC_REMOTE=${SYNC_REMOTE:-origin}
SYNC_TARGET_BRANCH=${SYNC_TARGET_BRANCH:-main}
SYNC_UPSTREAM=${SYNC_UPSTREAM:-upstream}
SYNC_UPSTREAM_REF=${SYNC_UPSTREAM_REF:-main}
SYNC_UPSTREAM_URL=${SYNC_UPSTREAM_URL:-}
SYNC_MAX_COMMITS=${SYNC_MAX_COMMITS:-0}
SYNC_DRY_RUN=${SYNC_DRY_RUN:-false}
SYNC_ALERT_WEBHOOK=${SYNC_ALERT_WEBHOOK:-}
SYNC_RUN_URL=${SYNC_RUN_URL:-}
SYNC_DETAIL_FILE=${SYNC_DETAIL_FILE:-}

# Failure context, filled in by fail sites; consumed by the EXIT funnel.
FAIL_REASON=""
FAIL_CANDIDATE=""
FAIL_CONFLICTS=""   # newline-separated paths
FAIL_OUTPUT=""      # tail of the failing command's stderr
FROZEN_TIP=""

log() { printf '%s\n' "$*"; }

fail() {
  FAIL_REASON=$1
  # Terminal: exit runs the EXIT funnel (emit_failure + alert). A plain
  # `return 1` would NOT stop merge_one/push_and_verify — they run with
  # errexit suspended (`||`-guarded call chain), so a conflict or rejected
  # push would fall through and the loop would continue.
  exit 1
}

valid_remote_name() {
  local r=$1
  [[ "$r" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]
}

valid_refname() {
  local r=$1
  case "$r" in
    *..*|*.lock|*//*) return 1 ;;
  esac
  [[ "$r" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]
}

validate_env() {
  valid_remote_name "$SYNC_REMOTE"    || fail "invalid SYNC_REMOTE value"
  valid_remote_name "$SYNC_UPSTREAM"  || fail "invalid SYNC_UPSTREAM value"
  valid_refname "$SYNC_TARGET_BRANCH" || fail "invalid SYNC_TARGET_BRANCH value"
  valid_refname "$SYNC_UPSTREAM_REF"  || fail "invalid SYNC_UPSTREAM_REF value"
  [[ "$SYNC_MAX_COMMITS" =~ ^[0-9]+$ ]] || fail "SYNC_MAX_COMMITS must be a non-negative integer"
  case "$SYNC_DRY_RUN" in
    true|false) ;;
    *) fail "SYNC_DRY_RUN must be true or false" ;;
  esac
}

# --- alerting -------------------------------------------------------------
# One place builds and ships the webhook payload. `allowed_mentions` is empty
# so a notification can never ping anyone; the webhook URL itself is never
# echoed, and a delivery failure is downgraded to a warning so it cannot mask
# the sync failure that triggered it.
alert_content() { # $1 reason -> compact single-line content on stdout
  local content="fork-sync FAILED: $1"
  if [ -n "$FAIL_CANDIDATE" ]; then
    content="$content | candidate: $FAIL_CANDIDATE"
  fi
  if [ -n "$FAIL_CONFLICTS" ]; then
    local paths
    paths=$(printf '%s\n' "$FAIL_CONFLICTS" | head -n 5 | paste -sd, -)
    if [ "$(printf '%s\n' "$FAIL_CONFLICTS" | grep -c .)" -gt 5 ]; then
      paths="$paths,+more"
    fi
    content="$content | conflicts: $paths"
  fi
  content="$content | branch: $SYNC_TARGET_BRANCH"
  if [ -n "$SYNC_RUN_URL" ]; then
    content="$content | run: $SYNC_RUN_URL"
  fi
  printf '%s' "${content:0:900}"
}

send_alert() { # $1 reason
  [ -n "$SYNC_ALERT_WEBHOOK" ] || return 0
  local payload
  payload=$(jq -cn --arg content "$(alert_content "$1")" \
    '{content: $content, allowed_mentions: {parse: []}}') || return 0
  if ! curl --fail --silent --show-error --max-time 20 \
       -H 'Content-Type: application/json' \
       -d "$payload" \
       "$SYNC_ALERT_WEBHOOK" >/dev/null 2>&1; then
    printf '::warning::alert delivery failed; the sync failure above is the real problem\n' >&2
  fi
  return 0
}

# --- failure funnel -------------------------------------------------------
# Writes the detail JSON, prints the exact failing candidate / conflict paths
# / run URL, appends to the step summary, and alerts — all best-effort so the
# original exit code survives.
emit_failure() {
  local code=$1
  local reason=${FAIL_REASON:-}
  if [ -z "$reason" ]; then
    reason="unexpected failure (exit $code)"
  fi
  local conflicts_json
  conflicts_json=$(printf '%s' "$FAIL_CONFLICTS" | jq -R . | jq -s . 2>/dev/null) || conflicts_json='[]'
  local detail
  detail=$(jq -n \
    --arg reason "$reason" \
    --arg target "$SYNC_TARGET_BRANCH" \
    --arg remote "$SYNC_REMOTE" \
    --arg upstream_tip "$FROZEN_TIP" \
    --arg candidate "$FAIL_CANDIDATE" \
    --argjson conflicts "${conflicts_json:-[]}" \
    --arg output "$FAIL_OUTPUT" \
    --arg run_url "$SYNC_RUN_URL" \
    --argjson exit_code "$code" \
    '{reason: $reason, target: $target, remote: $remote, upstream_tip: $upstream_tip,
      candidate: $candidate, conflicts: $conflicts, failing_output: $output,
      run_url: $run_url, exit_code: $exit_code}' 2>/dev/null) || detail=""

  if [ -n "$SYNC_DETAIL_FILE" ] && [ -n "$detail" ]; then
    printf '%s\n' "$detail" >"$SYNC_DETAIL_FILE" || true
  fi

  printf '::error title=fork-sync::%s\n' "$reason" >&2
  if [ -n "$FAIL_CANDIDATE" ]; then
    printf 'candidate commit: %s\n' "$FAIL_CANDIDATE" >&2
  fi
  if [ -n "$FAIL_CONFLICTS" ]; then
    printf 'conflict paths:\n' >&2
    printf '%s\n' "$FAIL_CONFLICTS" | while IFS= read -r p; do printf '  %s\n' "$p" >&2; done
  fi
  if [ -n "$FAIL_OUTPUT" ]; then
    printf 'failing command output:\n%s\n' "$FAIL_OUTPUT" >&2
  fi
  printf 'target: %s/%s\n' "$SYNC_REMOTE" "$SYNC_TARGET_BRANCH" >&2
  if [ -n "$SYNC_RUN_URL" ]; then
    printf 'run: %s\n' "$SYNC_RUN_URL" >&2
  fi
  if [ -n "$SYNC_DETAIL_FILE" ] && [ -n "$detail" ]; then
    printf 'failure detail: %s\n' "$SYNC_DETAIL_FILE" >&2
  fi

  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
      printf '## Fork sync FAILED\n\n```\n%s\n' "$reason"
      [ -n "$FAIL_CANDIDATE" ] && printf 'candidate: %s\n' "$FAIL_CANDIDATE"
      if [ -n "$FAIL_CONFLICTS" ]; then
        printf 'conflicts:\n'
        printf '%s\n' "$FAIL_CONFLICTS" | while IFS= read -r p; do printf '  %s\n' "$p"; done
      fi
      [ -n "$SYNC_RUN_URL" ] && printf 'run: %s\n' "$SYNC_RUN_URL"
      printf '```\n'
    } >>"$GITHUB_STEP_SUMMARY" 2>/dev/null || true
  fi

  if [ "$SYNC_DRY_RUN" != true ]; then
    send_alert "$reason"
  fi
}

on_exit() {
  local code=$?
  if [ "$code" -eq 0 ]; then
    return 0
  fi
  set +e
  emit_failure "$code"
  exit "$code"
}
trap on_exit EXIT

# --- summary --------------------------------------------------------------
summary() { # $1.. appended to stdout AND the Actions step summary
  log "$@"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    printf '%s\n' "$*" >>"$GITHUB_STEP_SUMMARY" 2>/dev/null || true
  fi
}

# --- the loop -------------------------------------------------------------
push_and_verify() { # $1 candidate sha ("" when completing an interrupted run)
  local errf
  errf=$(mktemp) || fail "mktemp failed"
  if ! git push "$SYNC_REMOTE" "HEAD:refs/heads/$SYNC_TARGET_BRANCH" 2>"$errf"; then
    [ -n "$1" ] && FAIL_CANDIDATE=$1
    FAIL_OUTPUT=$(tail -n 8 "$errf" 2>/dev/null || true)
    rm -f "$errf"
    fail "push to $SYNC_REMOTE/$SYNC_TARGET_BRANCH rejected while importing ${1:-HEAD}"
  fi
  rm -f "$errf"

  # Verify the remote holds what we pushed. An ancestor is fine — someone
  # advanced the branch concurrently; the *next* push will then be rejected
  # and stop the run, which is the intended behavior.
  local pushed rhead
  pushed=$(git rev-parse --verify HEAD) || { [ -n "$1" ] && FAIL_CANDIDATE=$1; fail "cannot resolve HEAD after pushing"; }
  if ! git fetch --quiet "$SYNC_REMOTE" "$SYNC_TARGET_BRANCH" 2>"$errf"; then
    [ -n "$1" ] && FAIL_CANDIDATE=$1
    FAIL_OUTPUT=$(tail -n 8 "$errf" 2>/dev/null || true)
    rm -f "$errf"
    fail "verification fetch of $SYNC_REMOTE/$SYNC_TARGET_BRANCH failed after pushing ${1:-HEAD}"
  fi
  rm -f "$errf"
  rhead=$(git rev-parse --verify 'FETCH_HEAD^{commit}') || { [ -n "$1" ] && FAIL_CANDIDATE=$1; fail "cannot resolve remote tip after pushing ${1:-HEAD}"; }
  if [ "$rhead" != "$pushed" ]; then
    if git merge-base --is-ancestor "$pushed" "$rhead"; then
      log "remote advanced concurrently; our merge is contained — continuing"
    else
      [ -n "$1" ] && FAIL_CANDIDATE=$1
      fail "remote $SYNC_TARGET_BRANCH is at $rhead after push, expected $pushed"
    fi
  fi
}

merge_one() { # $1 candidate sha — merge it, push it, verify it
  local cand=$1
  local short subject
  short=$(git log -1 --format=%h "$cand") || fail "cannot read $cand"
  subject=$(git log -1 --format=%s "$cand") || fail "cannot read $cand"

  local errf
  errf=$(mktemp) || fail "mktemp failed"
  if ! git merge --no-ff --no-edit -m "Merge upstream $short: $subject" "$cand" 2>"$errf"; then
    FAIL_CANDIDATE=$cand
    FAIL_CONFLICTS=$(git diff --name-only --diff-filter=U 2>/dev/null || true)
    FAIL_OUTPUT=$(tail -n 8 "$errf" 2>/dev/null || true)
    # Leave a clean tree; remote progress is untouched either way.
    git merge --abort >/dev/null 2>&1 || git reset --hard HEAD >/dev/null 2>&1 || true
    rm -f "$errf"
    if [ -n "$FAIL_CONFLICTS" ]; then
      fail "merge conflict importing $cand"
    else
      fail "merge failed importing $cand"
    fi
  fi
  rm -f "$errf"

  if [ "$SYNC_DRY_RUN" = true ]; then
    log "dry-run: merged $short locally, not pushing"
    return 0
  fi

  push_and_verify "$cand"
}

main() {
  validate_env

  # Wire the upstream remote when the workflow supplies its (fixed) URL.
  if [ -n "$SYNC_UPSTREAM_URL" ]; then
    if git remote get-url "$SYNC_UPSTREAM" >/dev/null 2>&1; then
      git remote set-url "$SYNC_UPSTREAM" "$SYNC_UPSTREAM_URL" || fail "cannot set URL of remote $SYNC_UPSTREAM"
    else
      git remote add "$SYNC_UPSTREAM" "$SYNC_UPSTREAM_URL" || fail "cannot add remote $SYNC_UPSTREAM"
    fi
  fi

  git config user.email >/dev/null 2>&1 || fail "git identity not configured (user.email)"
  git config user.name  >/dev/null 2>&1 || fail "git identity not configured (user.name)"

  [ -z "$(git status --porcelain)" ] || fail "working tree not clean at start"

  # Align HEAD with the live remote target — the checkout ref may be stale.
  git fetch --quiet "$SYNC_REMOTE" "$SYNC_TARGET_BRANCH" || fail "fetch of $SYNC_REMOTE/$SYNC_TARGET_BRANCH failed"
  local remote_head local_head
  remote_head=$(git rev-parse --verify 'FETCH_HEAD^{commit}') || fail "cannot resolve $SYNC_REMOTE/$SYNC_TARGET_BRANCH"
  local_head=$(git rev-parse --verify HEAD) || fail "cannot resolve HEAD"
  if [ "$remote_head" != "$local_head" ]; then
    if git merge-base --is-ancestor "$local_head" "$remote_head"; then
      git merge --ff-only --quiet "$remote_head" || fail "cannot fast-forward to remote target tip $remote_head"
    elif ! git merge-base --is-ancestor "$remote_head" "$local_head"; then
      fail "local HEAD and $SYNC_REMOTE/$SYNC_TARGET_BRANCH have diverged; manual intervention required"
    fi
    # else: local is ahead (dispatch raced a just-finished run); our push
    # will fast-forward the remote, so proceed.
  fi

  # Freeze the upstream tip for this whole run.
  git fetch --quiet "$SYNC_UPSTREAM" "$SYNC_UPSTREAM_REF" || fail "fetch of $SYNC_UPSTREAM/$SYNC_UPSTREAM_REF failed"
  FROZEN_TIP=$(git rev-parse --verify 'FETCH_HEAD^{commit}') || fail "cannot resolve $SYNC_UPSTREAM/$SYNC_UPSTREAM_REF"

  if git merge-base --is-ancestor "$FROZEN_TIP" HEAD; then
    # Local tree already contains the tip. "Done" means the REMOTE contains
    # it too — if a previous run merged but its push was interrupted, finish
    # that push now (fast-forward only; alignment ruled out divergence).
    if git merge-base --is-ancestor "$FROZEN_TIP" "$remote_head"; then
      summary "### Fork sync — up to date"
      summary "Upstream $SYNC_UPSTREAM_REF ($(git log -1 --format=%h "$FROZEN_TIP")) is already contained in $SYNC_REMOTE/$SYNC_TARGET_BRANCH; nothing to do."
      return 0
    fi
    if [ "$SYNC_DRY_RUN" = true ]; then
      summary "### Fork sync — dry-run"
      summary "Upstream tip already merged locally; remote target lacks it (nothing pushed in dry-run)."
      return 0
    fi
    # Complete only a single interrupted merge+push; more than one unpushed
    # local commit would batch-push and violate one-commit-per-push.
    # First-parent count: the merge makes the imported original reachable,
    # but only the merge itself is a commit we created.
    local gap
    gap=$(git rev-list --count --first-parent "$remote_head..HEAD") || fail "cannot count unpushed commits"
    if [ "$gap" -ne 1 ]; then
      fail "local HEAD is $gap commits ahead of $SYNC_REMOTE/$SYNC_TARGET_BRANCH; refusing to batch-push (start from a fresh checkout)"
    fi
    push_and_verify ""
    summary "### Fork sync — completed an interrupted run"
    summary "Upstream $SYNC_UPSTREAM_REF ($(git log -1 --format=%h "$FROZEN_TIP")) was already merged locally; fast-forwarded $SYNC_REMOTE/$SYNC_TARGET_BRANCH to $(git rev-parse HEAD)."
    return 0
  fi

  # Missing upstream commits, parents strictly before children.
  local candidates total=0
  candidates=$(git rev-list --reverse --topo-order "$FROZEN_TIP" --not HEAD) || fail "rev-list failed"
  if [ -n "$candidates" ]; then
    total=$(printf '%s\n' "$candidates" | grep -c .) || true
  fi
  [ "$total" -gt 0 ] || fail "upstream tip is not an ancestor, yet no missing commits were enumerated"

  local merged=0 skipped=0 cand n
  while IFS= read -r cand; do
    [ -n "$cand" ] || continue
    # Resume is pure Git ancestry: anything already reachable from HEAD
    # (e.g. a manual single-commit merge done by a human) is skipped.
    if git merge-base --is-ancestor "$cand" HEAD 2>/dev/null; then
      skipped=$((skipped + 1))
      continue
    fi
    if [ "$SYNC_MAX_COMMITS" -gt 0 ] && [ "$merged" -ge "$SYNC_MAX_COMMITS" ]; then
      break
    fi
    n=$(git rev-list --count "HEAD..$cand") || fail "rev-list --count failed for $cand"
    if [ "$n" -ne 1 ]; then
      FAIL_CANDIDATE=$cand
      fail "candidate $cand still has $n unimported commit(s); DAG invariant violated (expected exactly 1)"
    fi
    merge_one "$cand"
    merged=$((merged + 1))
  done <<<"$candidates"

  local remaining=$((total - skipped - merged))
  summary "### Fork sync"
  summary "- upstream tip: \`$FROZEN_TIP\` ($(git log -1 --format=%s "$FROZEN_TIP"))"
  summary "- merged and pushed individually: $merged"
  if [ "$skipped" -gt 0 ]; then
    summary "- already present (skipped via ancestry): $skipped"
  fi
  if [ "$remaining" -gt 0 ]; then
    summary "- remaining for the next run: $remaining (stopped at max_commits=$SYNC_MAX_COMMITS)"
  fi
  summary "- mode: $([ "$SYNC_DRY_RUN" = true ] && echo 'dry-run (nothing pushed)' || echo 'live')"
  summary "- target: $SYNC_REMOTE/$SYNC_TARGET_BRANCH @ $(git rev-parse HEAD)"
  return 0
}

# `alert <reason>`: standalone notification used by the workflow when a step
# fails before this script's sync logic ran (e.g. actions/checkout). Never
# fails the caller — the original failure must stay visible.
if [ "${1:-}" = "alert" ]; then
  reason=${2:-fork-sync failed before the sync step ran}
  FAIL_REASON=$reason
  send_alert "$reason" || true
  log "alert attempted: $reason"
  exit 0
fi

main
