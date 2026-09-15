"""Hermes Discord history archive and recall package."""
from __future__ import annotations

import os as _os
import shutil as _shutil
import sys as _sys
from pathlib import Path as _Path

__version__ = "0.1.0"


def _deployed_plugin_root() -> _Path | None:
    override = _os.environ.get("HERMES_DISCORD_HISTORY_DEPLOY_ROOT", "").strip()
    if override:
        return _Path(override)
    try:
        from .paths import deployed_plugin_root

        return deployed_plugin_root()
    except Exception:
        # Fail closed.  Runtime paths have one authority: paths.py, which uses
        # hermes_constants.get_hermes_home().  Falling back to cwd or parsing
        # HERMES_HOME again could scrub an unrelated tree after a path error.
        return None


def _scrub_deployed_tree_bytecode() -> None:
    """Eagerly remove any ``__pycache__/`` directories and ``.pyc`` files that
    may have accumulated inside the deployed plugin tree from prior runs.

    The deployed plugin lives under ``$HERMES_HOME/plugins/discord-history`` and
    is on the live ``sys.path``. Python routinely writes bytecode for every
    imported source file into a sibling ``__pycache__/`` directory — those
    artifacts break the deployment's private-mode guarantees (the cache
    directory is created with mode ``0o755`` and the bytecode files with
    ``0o644``). Calling this on every package import keeps the deployed tree
    clean without touching the source tree, where coverage tooling needs the
    bytecode.
    """
    root = _deployed_plugin_root()
    if root is None or not root.is_dir():
        return
    for cache in list(root.rglob("__pycache__")):
        try:
            _shutil.rmtree(cache)
        except Exception:
            pass
    for pyc in list(root.rglob("*.pyc")):
        try:
            pyc.unlink()
        except Exception:
            pass


_scrub_deployed_tree_bytecode()


def _disable_bytecode_for_deployed_tree() -> None:
    """Suppress bytecode writes when imported from the deployed plugin tree."""
    package_file = globals().get("__file__", "") or ""
    root = _deployed_plugin_root()
    if root is None:
        return
    deploy_root = str(root)
    if not deploy_root or not package_file.startswith(deploy_root.rstrip("/") + "/"):
        return
    try:
        _os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        _sys.dont_write_bytecode = True
    except Exception:
        pass


_disable_bytecode_for_deployed_tree()
