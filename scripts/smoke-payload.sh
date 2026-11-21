#!/bin/bash
# Smoke-test a staged pm payload (`pm bundle --out`): the raw store python
# boots hermes_cli/pm out of the staged repo snapshot (cwd at the staged
# repo, no network, lazy installs off). The desktop's self-relative CLI
# trampoline is minted only by the desktop release workflow, not by
# `pm bundle`, so it is not exercised here. Usage: smoke-payload.sh <payload-dir>
set -e
PAYLOAD="${1:?usage: smoke-payload.sh <payload-dir>}"
cd "$PAYLOAD"

PYTHON_ENTRY=$(node -e "
  const f = require(process.argv[1] + '/tools/facts.json');
  console.log(f.packages.python.entry);
" "$PWD")
PY="$PWD/tools/$PYTHON_ENTRY/bin/python3"; SP="$PWD/venv/lib/python3.11/site-packages"

[ -f "$PY" ] || { echo "FAIL: no store interpreter at $PY"; exit 1; }
[ -d "$SP" ] || { echo "FAIL: no venv site-packages at $SP"; exit 1; }
[ -f manifest.json ] || { echo "FAIL: no manifest.json"; exit 1; }
[ -f tools/facts.json ] || { echo "FAIL: no tools/facts.json"; exit 1; }

TOOLS="$PWD/tools"
cd hermes-agent

echo "— store python boots + hermes_cli imports out of the payload —"
env -u PYTHONPATH -u PYTHONHOME \
  HERMES_RUNTIME_DIR="$TOOLS" HERMES_DISABLE_LAZY_INSTALLS=1 \
  PYTHONPATH="$SP" \
  "$PY" -c "import hermes_cli.config; import pm; print('imports ok')"

echo "— hermes --version through the real entrypoint —"
env -u PYTHONPATH -u PYTHONHOME \
  HERMES_RUNTIME_DIR="$TOOLS" HERMES_DISABLE_LAZY_INSTALLS=1 \
  PYTHONPATH="$SP" \
  "$PY" -m hermes_cli.main --version

echo "— pm sees the staged tools —"
env -u PYTHONPATH -u PYTHONHOME \
  HERMES_RUNTIME_DIR="$TOOLS" HERMES_DISABLE_LAZY_INSTALLS=1 \
  PYTHONPATH="$SP" \
  "$PY" -m pm.cli doctor

echo "SMOKE OK"
