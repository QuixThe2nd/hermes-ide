"""Bridge for pre-reexec ``hermes update``: stale root ``utils`` must not kill restart.

The daily Install & Update E2E (and every field upgrade from ≤v2026.9.14 onto a
tree that added ``utils.file_signature``) failed with::

    Update incomplete — gateway auto-restart failed: cannot import name
    'file_signature' from 'utils' (.../hermes-agent/utils.py)

Mechanism (matches the scheduled E2E hermes-update legs):

1. The updater process is still the pre-pull interpreter (no post-swap hand-off
   yet — that landed after v2026.9.14).
2. Its narrow ``_purge_stale_hermes_modules`` only evicts package prefixes, so
   root ``utils`` stays cached without ``file_signature``.
3. The restart phase then does ``from hermes_cli.gateway import ...``, which
   loads fresh ``hermes_cli.config``, whose ``from utils import file_signature``
   hits the stale cache.

``hermes_cli.stale_modules.drop_stale_root_modules`` + the call at config import
time is the bridge: freshly imported hermes_cli code drops the incomplete
cache before importing utils. Post-swap hand-off makes the class dead for
updaters that already include it; this keeps the one upgrade from a
pre-handoff release green.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


def _import_fresh_consumer(name: str, source: str) -> types.ModuleType:
    """Run ``source`` as a brand-new module body (first import on a stale process)."""
    mod = types.ModuleType(name)
    mod.__file__ = f"{name}.py"
    sys.modules.pop(name, None)
    exec(compile(source, mod.__file__, "exec"), mod.__dict__)
    sys.modules[name] = mod
    return mod


# Mirrors the post-pull purge of a pre-handoff updater (v2026.9.14
# hermes_cli/update_cmd_maint.py): package prefixes only, root-level modules survive.
_PRE_HANDOFF_PURGE_PREFIXES = ("hermes_cli", "gateway", "tools", "tui_gateway", "agent")
_PRE_HANDOFF_PURGE_PROTECTED = {"hermes_cli", "hermes_cli.main", "hermes_cli.hermes_logging"}


@pytest.fixture
def pre_handoff_purge():
    """Evict what a pre-handoff updater evicts after the pull, restoring it afterwards."""
    saved: dict = {}

    def _purge() -> None:
        for name in list(sys.modules):
            if name in _PRE_HANDOFF_PURGE_PROTECTED or name.startswith("hermes_cli.update_"):
                continue
            if name.split(".", 1)[0] in _PRE_HANDOFF_PURGE_PREFIXES:
                module = sys.modules.pop(name, None)
                if module is not None:
                    saved[name] = module

    yield _purge
    for name, module in saved.items():
        sys.modules.setdefault(name, module)


def test_drop_stale_root_modules_evicts_utils_missing_file_signature(monkeypatch):
    import utils
    from hermes_cli.stale_modules import drop_stale_root_modules

    assert hasattr(utils, "file_signature")
    monkeypatch.delattr(utils, "file_signature")
    assert "utils" in sys.modules

    dropped = drop_stale_root_modules()
    assert dropped == ["utils"]
    assert "utils" not in sys.modules


def test_drop_stale_root_modules_leaves_complete_utils_alone():
    import utils
    from hermes_cli.stale_modules import drop_stale_root_modules

    assert hasattr(utils, "file_signature")
    before = sys.modules["utils"]
    assert drop_stale_root_modules() == []
    assert sys.modules["utils"] is before


def test_naive_consumer_still_dies_on_stale_utils(monkeypatch):
    """Control: without the heal, the ImportError the E2E saw still fires."""
    import utils

    monkeypatch.delattr(utils, "file_signature")
    with pytest.raises(ImportError, match=r"cannot import name 'file_signature' from 'utils'"):
        _import_fresh_consumer(
            "stale_utils_bridge_control",
            "from utils import file_signature\n",
        )


def test_fresh_config_import_heals_stale_utils_missing_file_signature(monkeypatch, pre_handoff_purge):
    """Restart-phase shape: the whole hermes_cli.* graph purged, root utils stale, config
    re-imported — the exact shape of the post-pull gateway-restart import."""
    import utils

    monkeypatch.delattr(utils, "file_signature")
    pre_handoff_purge()

    config = importlib.import_module("hermes_cli.config")
    assert hasattr(config, "file_signature")
    assert callable(config.file_signature)
    assert hasattr(sys.modules["utils"], "file_signature")


def test_fresh_managed_scope_import_heals_stale_utils(monkeypatch, pre_handoff_purge):
    """managed_scope binds ``utils.file_signature`` at import time; a fresh import into a
    purged graph must heal the root cache itself rather than depend on config running first."""
    import utils

    monkeypatch.delattr(utils, "file_signature")
    pre_handoff_purge()
    assert "hermes_cli.managed_scope" not in sys.modules

    managed_scope = importlib.import_module("hermes_cli.managed_scope")
    assert callable(managed_scope.file_signature)
