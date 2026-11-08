"""Resume-pending reason markers shared across the restart/resume paths.

Kept in a dependency-free module so ``gateway.run`` and
``gateway.forced_resume_replay`` can share the constant without an import
cycle.
"""

from __future__ import annotations

# The idle ``/restart`` requester's synthesized comeback turn: the session
# interrupted nothing, so it resumes with the restore note instead of the
# transparent forced-victim replay.
RESTART_REQUESTER_RESUME_REASON = "restart_requester"
