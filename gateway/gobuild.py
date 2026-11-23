"""Shared resolution for the repo-shipped Go helper binaries under ``internal/``.

Several bridges execute a small Go module as a stdin-JSON → stdout-JSON
helper binary (``gateway/bot_allowlist.py``,
``plugins/platforms/discord/mention_render.py``, …). Each needs the same
dance: prefer a cached binary under ``$HERMES_HOME/cache/<name>/`` whose
filename is content-hashed from the Go sources (so a tree update never
serves a stale helper), else build it from the repo checkout with the local
Go toolchain. :class:`HelperBinary` is that dance, parameterized per helper
— a fourth copy of it does not get written.

Resolution never raises: source hashing, cache-path resolution and the build
can all fail (unreadable checkout, unwritable home, no toolchain), and every
caller is a message-path feature that must decide its own failure posture
(fail-closed for the authorization gate, fail-open for rendering) rather
than inherit an exception.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

_BUILD_TIMEOUT_SECONDS = 300
# A SUCCESSFUL resolution is pinned for the process. A failed one is retried
# after this cooldown: a later toolchain install revives the helper without
# a gateway restart, while a permanently broken environment does not
# re-attempt a 300 s build per message.
RETRY_COOLDOWN_SECONDS = 300.0


class HelperBinary:
    """Resolves one ``internal/<module>`` cmd binary: cached, else built."""

    def __init__(
        self,
        *,
        module: str,
        sources: Sequence[str],
        build_target: str,
        cache_dir: str,
        binary_stem: str,
        label: str,
        failure_hint: str,
    ):
        self._module = module
        self._sources = tuple(sources)
        self._build_target = build_target
        self._cache_dir = cache_dir
        self._binary_stem = binary_stem
        self._label = label
        self._failure_hint = failure_hint
        self._lock = threading.Lock()
        self._binary: Optional[str] = None
        self._failed_at: Optional[float] = None

    def repo_module_dir(self) -> Optional[Path]:
        """The internal/<module> checkout this tree ships, when present."""
        module_dir = Path(__file__).resolve().parents[1] / "internal" / self._module
        if all((module_dir / name).is_file() for name in self._sources):
            return module_dir
        return None

    def cached_binary_path(self) -> Path:
        """Cache location, keyed by a hash of the Go sources (staleness-proof)."""
        module_dir = self.repo_module_dir()
        digest = "built"
        if module_dir is not None:
            hasher = hashlib.sha256()
            for name in self._sources:
                hasher.update((module_dir / name).read_bytes())
            digest = hasher.hexdigest()[:12]
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "cache" / self._cache_dir / f"{self._binary_stem}-{digest}"

    def resolve(self) -> Optional[str]:
        """One resolution attempt: cached binary, else a fresh build. Never raises."""
        try:
            binary = self.cached_binary_path()
            if binary.is_file() and os.access(binary, os.X_OK):
                return str(binary)
        except Exception:
            return None
        module_dir = self.repo_module_dir()
        if module_dir is None or shutil.which("go") is None:
            return None
        try:
            binary.parent.mkdir(parents=True, exist_ok=True)
            staging = binary.with_suffix(".tmp")
            build = subprocess.run(
                ["go", "build", "-o", str(staging), self._build_target],
                cwd=module_dir, capture_output=True, text=True,
                timeout=_BUILD_TIMEOUT_SECONDS,
            )
            if build.returncode == 0 and staging.is_file():
                os.replace(staging, binary)  # atomic: concurrent processes converge
                return str(binary)
            logger.warning(
                "%s build failed (go build exit %s); %s",
                self._label, build.returncode, self._failure_hint,
            )
        except Exception as exc:
            logger.warning("%s build failed: %s; %s", self._label, exc, self._failure_hint)
        return None

    def get(self) -> Optional[str]:
        """Pinned resolution: success cached for the process, failure retried
        after :data:`RETRY_COOLDOWN_SECONDS`."""
        with self._lock:
            if self._binary is not None:
                return self._binary
            if (
                self._failed_at is not None
                and (time.monotonic() - self._failed_at) < RETRY_COOLDOWN_SECONDS
            ):
                return None
            binary = self.resolve()
            if binary is not None:
                self._binary = binary
            else:
                self._failed_at = time.monotonic()
            return binary
