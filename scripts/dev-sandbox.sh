#!/usr/bin/env bash
# Run a Hermes instance in an isolated sandbox with its own HERMES_HOME.
#
# By default the sandbox is throwaway: a temp dir is created and removed on
# exit. Use --persistent to keep the sandbox across restarts (stored under
# .hermes-sandbox/ in the worktree git root).
#
# Usage:
#   scripts/dev-sandbox.sh python -m hermes_cli.main
#   scripts/dev-sandbox.sh hermes --tui
#   scripts/dev-sandbox.sh hermes dashboard
#   scripts/dev-sandbox.sh --persistent hermes --tui
#   scripts/dev-sandbox.sh -- npm run dev   # from ui-tui/ or web/
#
# Seed the sandbox HERMES_HOME from an existing directory (e.g. your main
# ~/.hermes) so config, sessions, skills, etc. are pre-populated:
#   scripts/dev-sandbox.sh --from ~/.hermes hermes --tui
#
# Override the persistent sandbox dir name (default: .hermes-sandbox):
#   HERMES_DEV_SANDBOX_DIR=.staging-sandbox scripts/dev-sandbox.sh --persistent hermes --tui

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

print_help() {
  cat <<'EOF'
Usage: dev-sandbox.sh [--persistent] [--from DIR] [--] <command...>

Run a Hermes instance in an isolated sandbox.

Options:
  --persistent    Keep the sandbox dir across restarts (under the worktree
                  git root, in .hermes-sandbox/). Without this flag the
                  sandbox is a temp dir that is removed on exit.
  --from DIR      Copy DIR into the sandbox HERMES_HOME as the starting
                  point (config, sessions, skills, etc.).
                  Ignored if the sandbox HERMES_HOME already has content
                  (e.g. reusing a --persistent sandbox) to avoid clobbering.
  --delete        Delete the existing persistent sandbox in .hermes-sandbox.
  -h, --help      Show this help message.

Environment:
  HERMES_DEV_SANDBOX_DIR   Override the persistent dir name (default: .hermes-sandbox)

Examples:
  dev-sandbox.sh hermes --tui
  dev-sandbox.sh --persistent hermes dashboard
  dev-sandbox.sh --from ~/.hermes hermes --tui
  dev-sandbox.sh -- npm run dev
EOF
}

PERSISTENT=false
DELETE=false
SEED_DIR=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --persistent)
      PERSISTENT=true
      shift
      ;;
    --from)
      if [ "$#" -lt 2 ] || [[ "$2" == -* ]]; then
        echo "error: --from requires a directory argument" >&2
        exit 1
      fi
      SEED_DIR="$2"
      shift 2
      ;;
    --from=*)
      SEED_DIR="${1#--from=}"
      if [ -z "$SEED_DIR" ]; then
        echo "error: --from requires a directory argument" >&2
        exit 1
      fi
      shift
      ;;
    --delete)
      DELETE=true
      shift
      ;;
    -h|--help)
      print_help
      exit 0
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

if [ -n "$SEED_DIR" ]; then
  if [ ! -d "$SEED_DIR" ]; then
    echo "error: --from dir '$SEED_DIR' does not exist" >&2
    exit 1
  fi
  # Resolve to absolute path so it's valid after we cd later.
  SEED_DIR="$(cd "$SEED_DIR" && pwd)"
fi

if [ "$#" -eq 0 ]; then
  print_help >&2
  exit 1
fi


SANDBOX_DIR_NAME="${HERMES_DEV_SANDBOX_DIR:-.hermes-sandbox}"
GIT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$SCRIPT_DIR/..")"
GIT_ROOT="$(cd "$GIT_ROOT" && pwd)"
PERSISTENT_SANDBOX_ROOT="$GIT_ROOT/$SANDBOX_DIR_NAME"

if [ "$DELETE" = true ]; then
  if [ -d "$PERSISTENT_SANDBOX_ROOT" ]; then
    read -r -p "[sandbox] delete $PERSISTENT_SANDBOX_ROOT? [y/N] " REPLY
    case "$REPLY" in
      [yY]|[yY][eE][sS])
        echo "[sandbox] deleting $PERSISTENT_SANDBOX_ROOT" >&2
        rm -rf -- "$PERSISTENT_SANDBOX_ROOT"
        ;;
      *)
        echo "[sandbox] aborted" >&2
        exit 1
        ;;
    esac
  else
    echo "[sandbox] nothing to delete at $PERSISTENT_SANDBOX_ROOT" >&2
  fi
  exit 0
fi

if [ "$PERSISTENT" = true ]; then
  SANDBOX_ROOT="$PERSISTENT_SANDBOX_ROOT"
else
  SANDBOX_ROOT="$(mktemp -d -t hermes-sandbox.XXXXXX)"
fi

export HERMES_HOME="$SANDBOX_ROOT/hermes-home"

mkdir -p "$HERMES_HOME"

if [ -n "$SEED_DIR" ]; then
  # Only seed when the sandbox HERMES_HOME is empty — avoids clobbering an
  # existing persistent sandbox on re-run.
  if [ -z "$(ls -A "$HERMES_HOME" 2>/dev/null)" ]; then
    echo "[sandbox] seeding HERMES_HOME from $SEED_DIR" >&2
    cp -a "$SEED_DIR/." "$HERMES_HOME/"
  else
    echo "[sandbox] --from ignored: $HERMES_HOME already has content" >&2
  fi
fi

echo "[sandbox] HERMES_HOME=$HERMES_HOME" >&2
if [ "$PERSISTENT" = true ]; then
  echo "[sandbox] persistent: $SANDBOX_ROOT" >&2
else
  echo "[sandbox] ephemeral (will be cleaned up on exit)" >&2
fi

if [ "$PERSISTENT" = false ]; then
  # shellcheck disable=SC2329  # invoked via trap
  cleanup() {
    chmod -R u+w "$SANDBOX_ROOT"
    rm -rf -- "$SANDBOX_ROOT"
  }
  trap cleanup EXIT
  trap 'cleanup; exit 130' INT TERM
fi

"$@"
rc=$?
exit $rc