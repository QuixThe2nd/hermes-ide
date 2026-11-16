"""Shell-identity guard at delegation boundaries — PIN: deliberate no-op.

Delegate children share the parent's terminal environment on purpose (one
shell, one cwd, one set of installed packages): ``_resolve_container_task_id``
collapses the child onto the parent key and ``seed_workspace`` registers the
container alias. The shared bash snapshot
(``tools/environments/base_session_env.py`` re-dumps ``export -p`` after every
command) then persists a child's ``export HOME=... HERMES_HOME=...`` so the
PARENT's next command sources the scratch home — GitHub CLI reads it and asks
for login although the real account is fine. The process ``os.environ`` is not
the leak; the snapshot is.

This guard will snapshot the identity names (GH_CONFIG_DIR, HERMES_HOME,
HOME, PYTHONPATH, XDG_CONFIG_HOME) at the first delegation boundary and
restore them when the LAST guard exits, refcounted per shared-environment key
so inner or parallel-sibling exits never restore early. Until the restore
lands, every method is a no-op so the pinning tests fail on the observable
leak, not on an import error.
"""

from __future__ import annotations

from typing import Optional


class ShellIdentityGuard:
    """No-op placeholder: ``__enter__``/``release`` do nothing yet."""

    def __init__(self, task_id: Optional[str] = None) -> None:
        self.task_id = task_id

    def __enter__(self) -> "ShellIdentityGuard":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def release(self) -> None:
        """Inert, therefore idempotent."""
