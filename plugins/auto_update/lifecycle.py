"""Non-oneshot install/reconcile hook for the auto-update scheduler."""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence

from hermes_constants import get_hermes_home

from plugins.auto_update.config import load_auto_update_config, plugin_explicitly_disabled
from plugins.auto_update.platform import detect_install_scope, platform_supported
from plugins.auto_update.systemd import (
    ReconcileResult,
    _canonical_home,
    _takeover_warning,
    reconcile_units,
)

logger = logging.getLogger(__name__)


def _path_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _is_disposable_home(home: Path) -> bool:
    raw_roots = ["/tmp", "/var/tmp", "/var/folders", "/run/user", "/dev/shm"]
    try:
        raw_roots.append(tempfile.gettempdir())
    except (OSError, RuntimeError):
        pass
    raw_roots.extend(os.environ.get(name, "") for name in ("TMPDIR", "TMP", "TEMP"))
    for raw_root in raw_roots:
        if not raw_root:
            continue
        try:
            root = _canonical_home(Path(raw_root))
        except (OSError, RuntimeError, ValueError):
            continue
        if root.parent != root and _path_within(home, root):
            return True

    parts = home.parts
    return any(
        re.fullmatch(r"pytest-of-[^/]+", parts[index])
        and re.fullmatch(r"pytest-[0-9]+", parts[index + 1])
        for index in range(len(parts) - 1)
    )


def _disposable_home_refusal(scope) -> ReconcileResult | None:
    try:
        current_home = _canonical_home(get_hermes_home())
    except (OSError, RuntimeError, ValueError):
        # The low-level reconciler returns the canonicalization failure.
        return None
    if not _is_disposable_home(current_home):
        return None
    selected = scope or detect_install_scope()
    if selected is None:
        return None
    return ReconcileResult(
        supported=True,
        scope=selected,
        changed=False,
        enabled=False,
        timer_active=False,
        legacy=(),
        warnings=(
            _takeover_warning(
                f"HERMES_HOME is in a disposable location: {current_home}"
            ),
        ),
        enabled_known=False,
        timer_active_known=False,
    )


def is_oneshot_run_invocation(argv: Sequence[str] | None = None) -> bool:
    """True when argv targets ``hermes auto_update run`` (systemd oneshot entry)."""
    tokens = [str(tok) for tok in (argv or sys.argv)]
    for idx, tok in enumerate(tokens):
        if tok != "auto_update":
            continue
        nxt = tokens[idx + 1] if idx + 1 < len(tokens) else ""
        if nxt == "run":
            return True
    return False


def reconcile_scheduler_on_load(
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
    scope=None,
    takeover: bool = False,
) -> ReconcileResult | None:
    """Install/reconcile hook — never call from ``register()`` or oneshot ``run``.

    Explicit disablement (``auto_update.enabled: false`` or ``plugins.disabled``)
    stops any installed timer. Enabled installs reconcile idempotently.
    Disposable homes are refused here, before the explicit low-level unit
    reconciler; persistent ownership is enforced there for every caller.
    """
    if is_oneshot_run_invocation():
        return None
    if not platform_supported():
        return None

    cfg = load_auto_update_config()
    enabled = not plugin_explicitly_disabled() and bool(cfg.get("enabled", True))
    kwargs = {}
    if run_systemctl is not None:
        kwargs["run_systemctl"] = run_systemctl
    if scope is not None:
        kwargs["scope"] = scope
    if takeover:
        kwargs["takeover"] = True
    try:
        if enabled and not takeover:
            refusal = _disposable_home_refusal(scope)
            if refusal is not None:
                return refusal
        return reconcile_units(cfg, enabled=enabled, **kwargs)
    except Exception as exc:
        logger.warning(
            "auto_update scheduler reconcile skipped: %s", exc, exc_info=True
        )
        return None
