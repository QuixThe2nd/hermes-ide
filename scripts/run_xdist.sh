#!/usr/bin/env bash
# xdist runner: persistent workers sharing one collection pass, instead of
# the per-file subprocess model. EXPERIMENT (PR #96458): the per-file model
# pays a spawn+import wall of ~0.5-1.5s per file x 3400 files (~6 min floor
# on Windows, the dominant share of the lane's runtime). xdist pays that
# import wall once per worker.
#
# --dist loadfile keeps every test of a file on ONE worker, so cross-file
# state pollution is bounded to "files sharing a worker", the same class of
# hazard the per-file model eliminated. Failures here are stateful-test bugs
# to fix, not runner bugs.
set -uo pipefail
cd "$(dirname "$0")/.."

export PATH="/c/Program Files/Git/bin:$PATH"
unset PYTHONPATH PYTHONPYCACHEPREFIX HERMES_RUNTIME_DIR
export TZ=UTC LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONHASHSEED=0 PYTHONUTF8=1

N="${1:-auto}"
shift || true

echo "▶ xdist run: -n $N --dist loadfile $*"
exec .venv/Scripts/python.exe -m pytest -n "$N" --dist loadfile \
  -p no:cacheprovider -m "not integration" -q --tb=line "$@"
