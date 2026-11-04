# Confirmed historical upgrade limitations

These failures cannot be repaired by changing the update target: the failing code is already loaded from the starting release. The workflow still executes each original update path. Only a match on the exact starting commit, method pair, failed assertion, and fresh log signatures produces a non-red known-failure receipt. Other errors still fail. The results table shows matched cases as `known [n]`, with the explanation and evidence in a footnote at the bottom. These cases are counted separately from passed upgrades.

The machine-readable rules in `e2e-assets/known-failures.json` own the matcher and report footnote text. This document explains their historical evidence. Logs are rotated before each attempt so an earlier failure cannot classify a later one.

## Windows launcher self-lock

Classification: **unfixable in the update target for the exact released `hermes.exe update` path**.

| Starting release | Released commit | Install → update | Verified failing job |
|---|---|---|---|
| `v2026.3.12` | `a370ab8391ca5f8de7ebbc449f05cb0df36ade7c` | `installer-script` → `hermes-update` | [101514756800](https://github.com/ethernet8023/hermes-agent/actions/runs/34043635705/job/101514756800) |
| `v2026.4.8` | `86960cdbb0148145890e2ee90b4e157fa899f6e1` | `installer-script` → `hermes-update` | [101514755527](https://github.com/ethernet8023/hermes-agent/actions/runs/34043635705/job/101514755527) |

The running console launcher holds `venv/Scripts/hermes.exe` open. The old updater pulls the new checkout, then asks uv to replace that same executable during an editable install. Windows rejects the replacement with `Access is denied. (os error 5)`. The old updater's ZIP fallback repeats the dependency install and encounters the same lock.

Evidence required: the CLI update phase failed, the traceback identifies the running `hermes.exe/__main__.py`, and uv reports failure to remove that install's `Scripts/hermes.exe` with OS error 5. A generic access-denied error on another file does not match.

The March call is in the released `hermes_cli/main.py:1678-1683`, with the ZIP fallback at `1571-1576`. April calls `_install_python_dependencies_with_optional_fallback`, whose released body at `3295-3321` runs the installs without launcher quarantine. Those function objects were loaded before the checkout changed. May's sampled CLI update passed; do not classify it from this record.

Re-running the installer is a separate tested upgrade route. Invoking the old CLI through its venv Python is a possible recovery route, but is not silently substituted for the console-launcher leg.

Autostash conflicts, network failures, and generic timeouts remain actionable or unclassified until diagnosed. They must not inherit a historical label because they occurred on an old release.
