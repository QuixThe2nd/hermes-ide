#!/usr/bin/env bash
# xdist runner: persistent workers sharing one collection pass, instead of
# the per-file subprocess model. EXPERIMENT (PR #96458): the per-file model
# pays a spawn+import wall of ~0.5-1.5s per file x 3400 files (~6 min floor
# on Windows, the dominant share of the lane's runtime). xdist pays that
# import wall once per worker.
#
# TWO PHASES:
#
#   1. xdist -n N --dist loadfile over the whole suite EXCEPT the
#      process-killer files below. loadfile keeps a file's tests on ONE
#      worker, bounding cross-file pollution to co-scheduled files.
#
#   2. The killer files run SERIALLY, one plain pytest per file. These are
#      the tests that spawn and kill REAL process trees (live_system_guard
#      bypasses, venv-holder sweeps, taskkill /F /T, stale-process reaps).
#      Under the per-file runner each ran alone in its own session
#      (start_new_session), so a sweep could only hit its own children.
#      Under shared xdist workers, those sweeps enumerate every python
#      process — 32 sibling workers and the controller match — and the
#      first CI xdist run died with "runner lost communication" (run
#      33389773498, 1h15m, no logs flushed). Serially, a sweep's only
#      reachable victims are its own children.
#
# Failures in phase 1 are stateful-test bugs to fix, not runner bugs.
set -uo pipefail
cd "$(dirname "$0")/.."

export PATH="/c/Program Files/Git/bin:$PATH"
unset PYTHONPATH PYTHONPYCACHEPREFIX HERMES_RUNTIME_DIR
export TZ=UTC LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONHASHSEED=0 PYTHONUTF8=1

N="${1:-auto}"
shift || true

# Tests that spawn/kill real process trees. Do NOT run these inside xdist
# workers — see the phase-2 comment above.
KILLERS=(
  tests/cron/test_cron_script.py
  tests/gateway/test_control_socket_windows_live.py
  tests/gateway/test_replace_child_reap.py
  tests/gateway/test_whatsapp_bridge_pidfile.py
  tests/gateway/test_whatsapp_connect.py
  tests/gateway/test_whatsapp_stale_bridge.py
  tests/hermes_cli/test_dashboard_lifecycle_flags.py
  tests/hermes_cli/test_gateway_windows.py
  tests/hermes_cli/test_update_orphan_backend_reap.py
  tests/hermes_cli/test_update_stale_dashboard.py
  tests/test_install_autostash_conflict_recovery.py
  tests/test_install_lockfile_churn.py
  tests/test_install_ps1_venv_process_tree.py
  tests/test_install_unmerged_index.py
  tests/tools/test_process_registry.py
)

IGNORES=()
for f in "${KILLERS[@]}"; do
  [ -f "$f" ] && IGNORES+=(--ignore "$f")
done

echo "▶ phase 1: xdist -n $N --dist loadfile (bulk)"
PYTEST_STATUS=0
.venv/Scripts/python.exe -m pytest -n "$N" --dist loadfile \
  -p no:cacheprovider -m "not integration" -q --tb=line \
  "${IGNORES[@]}" "$@" || PYTEST_STATUS=$?

echo "▶ phase 2: serial per-file (process-killer tests)"
for f in "${KILLERS[@]}"; do
  [ -f "$f" ] || continue
  echo "  - $f"
  .venv/Scripts/python.exe -m pytest "$f" -p no:cacheprovider \
    -m "not integration" -q --tb=line || PYTEST_STATUS=$?
done

exit "$PYTEST_STATUS"
