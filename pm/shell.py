"""pm.shell(): the one place Hermes resolves the shell it runs commands with.

Callers that need bash (the terminal backend, `_find_bash`) call this instead
of hunting fixed locations.

Resolution order:
  1. Provisioned PATH: shutil.which("bash") — the store dirs are on the
     process PATH after pm.activate() ran.
  2. POSIX fallback table for non-bundle / daemon-launch PATH edge cases
     (/usr/bin/bash, /bin/bash, $SHELL, /bin/sh). A systemd/cron-launched
     gateway may have a minimal PATH and macOS /bin/bash is not on PATH by
     default, so `which` alone is not enough there.
"""

from __future__ import annotations

import os
import shutil


def bash() -> str | None:
    """Resolve the bash binary to use, or None if none is available."""
    on_path = shutil.which("bash")
    if on_path:
        return on_path

    # POSIX fallbacks for minimal-PATH daemon launches / macOS /bin/bash.
    for candidate in (
        "/usr/bin/bash",
        "/bin/bash",
        os.environ.get("SHELL"),
        "/bin/sh",
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None
