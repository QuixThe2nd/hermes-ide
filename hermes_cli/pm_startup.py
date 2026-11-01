"""Non-blocking pm store startup and background repair."""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


def run_pm_startup(*, warn=None) -> list[str] | None:
    """Adopt and activate a healthy pm store, or start its background heal."""
    try:
        import pm

        pm.adopt()
        problems = pm.check()
        if not problems:
            pm.activate()
            return []

        message = "install out of sync (%s) — auto-healing in background" % "; ".join(
            problems
        )
        (warn or logger.warning)(message)
        # Concurrent boots may race here; pm.store.install_lock() already
        # serializes their installs machine-wide, so no second lock is needed.
        threading.Thread(
            target=_heal_and_activate,
            name="hermes-pm-autoheal",
            daemon=True,
        ).start()
        return problems
    except Exception:
        logger.debug("pm startup check failed", exc_info=True)
        return None


def _heal_and_activate() -> None:
    """Reuse post-update provisioning, then refresh PATH without raising."""
    try:
        from hermes_cli.post_update import step_provision_runtimes

        result = step_provision_runtimes()
        logger.info("pm background heal result: %s", result)
    except Exception:
        logger.debug("pm background heal failed", exc_info=True)
    finally:
        try:
            import pm

            pm.activate()
        except Exception:
            logger.debug("pm activation after background heal failed", exc_info=True)
