#!/usr/bin/env bash
# Canonical test runner for hermes-agent. Run this instead of calling
# `pytest` directly to guarantee your local run matches CI behavior.
#
# What this script enforces:
#   * pytest-xdist with --dist loadfile — each test FILE's tests all run on
#     ONE worker, so file-internal ordering is preserved and cross-file
#     pollution is bounded to files co-scheduled on a worker. Persistent
#     workers also pay the interpreter+import wall (~0.5-1.5s on Windows)
#     once per worker instead of once per file.
#   * TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0 (deterministic)
#   * Env vars blanked (conftest.py also does this, but this
#     is belt-and-suspenders for anyone running pytest outside our
#     conftest path — e.g. on a single file)
#   * Proper venv activation (probes .venv, venv, then ~/.hermes/...)
#
# Usage:
#   scripts/run_tests.sh                            # full suite
#   scripts/run_tests.sh -j 4                       # cap worker count
#   scripts/run_tests.sh tests/agent/               # discover only here
#   scripts/run_tests.sh tests/agent/ tests/acp_adapter/    # multiple roots
#   scripts/run_tests.sh tests/foo.py               # single file
#   scripts/run_tests.sh tests/foo.py -q            # path + bare pytest flag
#   scripts/run_tests.sh tests/foo.py -v --tb=long  # bare flags "just work"
#   scripts/run_tests.sh -k 'pattern'              # value flags pass through too
#
# Bare pytest flags (anything starting with '-' that isn't -j/--jobs) are
# forwarded to pytest. Positional path arguments override the default
# discovery root (tests/).

set -uo pipefail

# ── Locate repo root ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Locate python ───────────────────────────────────────────────────────────
# Probe local venvs first; fall back to the Nix devShell's editable venv
# (HERMES_PYTHON is exported by the devShell hook and ships [dev] extras:
# pytest, pytest-asyncio, pytest-xdist).
#
# A candidate must have pytest INSTALLED, not merely exist. The release venv
# at ~/.hermes/hermes-agent/venv has bin/activate but no pytest, so an
# existence-only probe selected it in checkouts/worktrees without a local
# .venv — every file then died with "No module named pytest" and the run
# reported "0 tests passed" (which reads green at a glance even though the
# exit code is 1). Skip such a venv and keep probing instead.
VENV=""
VENV_PYTHON=""
SKIPPED_VENVS=""
for candidate in "$REPO_ROOT/.venv" "$REPO_ROOT/venv" "$HOME/.hermes/hermes-agent/venv"; do
  if [ -f "$candidate/bin/activate" ]; then
    if "$candidate/bin/python" -c 'import pytest' 2>/dev/null; then
      VENV="$candidate"
      VENV_PYTHON="$candidate/bin/python"
      break
    fi
    SKIPPED_VENVS="$SKIPPED_VENVS $candidate"
  elif [ -f "$candidate/Scripts/activate" ]; then
    if "$candidate/Scripts/python.exe" -c 'import pytest' 2>/dev/null; then
      VENV="$candidate"
      VENV_PYTHON="$candidate/Scripts/python.exe"
      break
    fi
    SKIPPED_VENVS="$SKIPPED_VENVS $candidate"
  fi
done
if [ -z "$VENV_PYTHON" ]; then
  if [ -n "${HERMES_PYTHON:-}" ] && "${HERMES_PYTHON}" -c 'import pytest' 2>/dev/null; then
    VENV_PYTHON="$HERMES_PYTHON"
  else
    echo "✗ No venv with pytest found. Install dev extras:" >&2
    echo "    uv sync --extra dev" >&2
    if [ -n "$SKIPPED_VENVS" ]; then
      echo "       (skipped for missing pytest:$SKIPPED_VENVS — install dev extras there, or create $REPO_ROOT/.venv)" >&2
    fi
    exit 1
  fi
fi
PYTHON="$VENV_PYTHON"

# ── Split args: our -j/--jobs vs pytest passthrough ─────────────────────────
N="${HERMES_TEST_WORKERS:-auto}"
PYTEST_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -j|--jobs)
      N="$2"; shift 2 ;;
    -j*)
      N="${1#-j}"; shift ;;
    --jobs=*)
      N="${1#--jobs=}"; shift ;;
    *)
      PYTEST_ARGS+=("$1"); shift ;;
  esac
done

# ── Windows location variables (computed before we drop env) ───────────────
# `env -i` forwards HOME, which is enough on POSIX. Native Windows CPython
# resolves Path.home() from USERPROFILE (or HOMEDRIVE+HOMEPATH), stdlib
# platform paths come from LOCALAPPDATA/APPDATA, ssl/sockets need SYSTEMROOT,
# and tempfile needs TEMP/TMP. Dropping them breaks collection on native
# Windows (issues #67385, #70813). These are location variables, not
# credentials, so forwarding them keeps the isolation intent intact. Each is
# only forwarded when actually set, so POSIX runs are byte-for-byte unchanged.
WIN_ENV=()
for _win_var in USERPROFILE HOMEDRIVE HOMEPATH LOCALAPPDATA APPDATA SYSTEMROOT TEMP TMP; do
  if [ -n "${!_win_var:-}" ]; then
    WIN_ENV+=("$_win_var=${!_win_var}")
  fi
done

# ── Live-gateway plugin (computed before we drop env) ───────────────────────
EXTRA_PYTHONPATH=""
EXTRA_PYTEST_PLUGINS=""
if [ -f "$HOME/.hermes/pytest_live_guard.py" ]; then
  EXTRA_PYTHONPATH="$HOME/.hermes"
  EXTRA_PYTEST_PLUGINS="pytest_live_guard"
fi

# ── Test-runner knobs (computed before we drop env) ──────────────────────────
#   * HERMES_TEST_IMAGE is read by tests/docker/conftest.py to skip its
#     session-scoped `docker build`. CI's docker.yml sets it to the image
#     the build step just loaded; stripping it made every pytest subprocess
#     rebuild the 5GB image from a cold builder cache instead (~4 min per
#     worker per run, and the rebuilt image lacked the
#     install stamp the workflow bakes in).
#
# These are test-infrastructure knobs, not credentials — same class as the
# HERMES_RUN_SLOW_PET_TESTS / HERMES_E2E_BROWSER opt-ins already forwarded.
# Keep this an explicit allowlist (no HERMES_TEST_* glob) so the "no
# credential can leak" property stays auditable at a glance.
TEST_ENV=()
for _test_var in HERMES_TEST_IMAGE; do
  if [ -n "${!_test_var:-}" ]; then
    TEST_ENV+=("$_test_var=${!_test_var}")
  fi
done

# ── Run in hermetic env ──────────────────────────────────────────────────────
# env -i: start with empty environment, opt-in only what we need.
# No credential var can leak — you'd have to explicitly add it here.
#
# __NIXOS_SET_ENVIRONMENT_DONE is a NixOS platform guard, not a credential:
# without it, every login shell (bash -l) that a test spawns re-runs
# /etc/set-environment and rebuilds PATH from the system profile — which
# evicts the dev shell's python3/rg and makes terminal, process-registry,
# and ripgrep-backed search tests fail with exit 127 on NixOS hosts.
#
# HERMES_PYTHON_SRC_ROOT is the Nix dev shell's editable-install root: the
# venv's editable finder reads it at runtime to locate first-party modules.
# Stripping it breaks "import tools" in every test subprocess whose cwd is
# not the repo root (the import-guard probe runs from a tempdir).
echo "▶ running pytest-xdist (-n $N --dist loadfile)"
echo "  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)"

cd "$REPO_ROOT"

# ── Pre-compile .pyc bytecode cache ─────────────────────────────────────────
# xdist workers import the same modules; pre-building the bytecode cache once
# here avoids every worker compiling on first import.
echo "▶ pre-compiling bytecode cache"
"$PYTHON" -m compileall -q -j 0 -- $(git ls-files '*.py') >/dev/null 2>&1 || true

echo "▶ pytest -n $N --dist loadfile"
exec env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  ${WIN_ENV[@]+"${WIN_ENV[@]}"} \
  ${TEST_ENV[@]+"${TEST_ENV[@]}"} \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  PYTHONUTF8=1 \
  ${HERMES_RUN_SLOW_PET_TESTS:+HERMES_RUN_SLOW_PET_TESTS="$HERMES_RUN_SLOW_PET_TESTS"} \
  ${HERMES_E2E_BROWSER:+HERMES_E2E_BROWSER="$HERMES_E2E_BROWSER"} \
  ${__NIXOS_SET_ENVIRONMENT_DONE:+__NIXOS_SET_ENVIRONMENT_DONE="$__NIXOS_SET_ENVIRONMENT_DONE"} \
  ${HERMES_PYTHON_SRC_ROOT:+HERMES_PYTHON_SRC_ROOT="$HERMES_PYTHON_SRC_ROOT"} \
  ${EXTRA_PYTHONPATH:+PYTHONPATH="$EXTRA_PYTHONPATH"} \
  ${EXTRA_PYTEST_PLUGINS:+PYTEST_PLUGINS="$EXTRA_PYTEST_PLUGINS"} \
  "$PYTHON" -m pytest -n "$N" --dist loadfile -p no:cacheprovider \
  -m "not integration" -q --tb=line \
  ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}
