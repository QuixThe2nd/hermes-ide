"""Late-life restart paths must survive an on-disk update of their modules.

Regression for the 2026-11-19 13:13 incident: a gateway generation booted
on build c83f98b6 kept running after the checkout fast-forwarded to #844
(which deleted ``gateway/restart_chain.py``); hours later its overlap claim
executed ``from gateway.restart_chain import ...`` from the old in-memory
code and the generation died (ModuleNotFoundError). With the generation
array, old-code generations live for hours beside an updated tree, so the
gateway now preloads every module the restart / overlap-claim / handover /
drain / successor-spawn / shutdown paths import lazily
(``gateway/restart_eager.py``), during startup and before the overlap wait.

The test boots a child exactly like the gateway does (``import
gateway.run``, then the startup preload), lets an "update" delete the
late-path modules from disk, and then drives the real claim seam
(``claim_authority_when_lock_frees`` through the post-claim step). The
claim must still succeed without touching disk.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Keep the copied tree to the importable source: everything else (tests,
# docs, skills, JS apps, caches) only slows the copy down.
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".pytest_cache", "__pycache__", ".hermes-lane", ".go-cache",
    ".go-home", ".task-evidence", ".venv", "venv", "node_modules",
    "tests", "docs", "evals", "scripts", "skills", "optional-skills",
    "ui-tui", "apps", "dist", "build", "*.md",
)

# Modules the claim loop and the post-claim startup step import lazily;
# the "update" deletes them from disk after boot.
_UPDATED_AWAY = (
    "gateway/generations.py",
    "gateway/overlap_registry.py",
    "gateway/restart_hub.py",
)

# Child driver: boots like the gateway, applies the preload the same way
# ``start_gateway`` does, survives an on-disk update, then drives the
# overlap claim through its post-claim step.
_CHILD_DRIVER = """
import asyncio
import os
import sys

# A deployed gateway has no second hermes tree to fall back on; an
# editable install (meta-path finders + path-hook placeholder entries)
# would silently rescue imports of modules deleted from this tree. Strip
# both so this disk is the only disk. NB: meta-path finders are usually
# classes, so the check must read the finder itself, not type(finder).
def _editable_finder(f) -> bool:
    mod = getattr(f, "__module__", None) or ""
    name = getattr(f, "__name__", None) or ""
    return "editable" in mod.lower() or "editable" in name.lower()


sys.meta_path = [f for f in sys.meta_path if not _editable_finder(f)]
sys.path = [p for p in sys.path if "editable" not in p.lower()]

# Boot exactly like the gateway process: the entry module first...
import gateway.run  # noqa: F401

# ...then the startup preload start_gateway runs before the overlap wait.
try:
    from gateway.restart_eager import preload_late_life_imports
except ModuleNotFoundError:
    sys.stderr.write("preload module absent (pre-fix tree)\\n")
else:
    preload_late_life_imports()

# The updater fast-forwards the checkout under this live generation.
for rel in %r:
    os.unlink(rel)

class _Runner:
    \"\"\"Pending successor whose hub is unreachable (empty registry).\"\"\"

    adapters = {}
    _shutdown_event = None
    _overlap_predecessor_pid = None


async def _post_claim_step():
    # The lazy imports run.py's _on_overlap_authoritative performs after the
    # claim, in order.
    from gateway.status import clear_runtime_status_released
    from gateway.restart_hub import complete_restart_hub_report
    from gateway.lifecycle_ledger import record_startup
    from gateway.shutdown_flush import recover_pending_to_db
    from gateway.overlap_restart import start_deferred_adapters
    clear_runtime_status_released()


async def _main():
    import gateway.overlap_restart as overlap

    won = await overlap.claim_authority_when_lock_frees(
        _Runner(), 0, _post_claim_step, poll_seconds=0.05)
    assert won is True, f"overlap claim failed: {won!r}"


asyncio.run(_main())
print("CLAIM-OK")
"""


def _copy_source_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    shutil.copytree(_PROJECT_ROOT, tree, ignore=_COPY_IGNORE, dirs_exist_ok=True)
    return tree


def _child_env(tree: Path, tmp_path: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tree)
    env["HERMES_HOME"] = str(tmp_path / "home")
    # The child is an ordinary claiming generation, not a launched successor.
    env.pop("HERMES_OVERLAP_SUCCESSOR", None)
    env.pop("HERMES_OVERLAP_PREDECESSOR_PID", None)
    return env


def test_overlap_claim_survives_update_deleting_late_path_modules(tmp_path):
    tree = _copy_source_tree(tmp_path)
    (tmp_path / "home").mkdir()

    result = subprocess.run(
        [sys.executable, "-c", _CHILD_DRIVER % (_UPDATED_AWAY,)],
        cwd=tree, env=_child_env(tree, tmp_path),
        capture_output=True, text=True, timeout=90,
    )

    assert result.returncode == 0, (
        f"late-path overlap claim died after on-disk update "
        f"(rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "CLAIM-OK" in result.stdout


def test_preload_pins_every_late_life_module():
    """The preload list must actually resolve: a stale entry would silently
    leave that module lazy again (the exact bug class this guards)."""
    from gateway import restart_eager

    restart_eager.preload_late_life_imports()
    missing = [m for m in restart_eager._LATE_LIFE_MODULES if m not in sys.modules]
    assert not missing, f"late-life modules not pinned by preload: {missing}"
