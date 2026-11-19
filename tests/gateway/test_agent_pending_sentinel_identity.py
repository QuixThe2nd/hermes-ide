"""The pending-agent sentinel must be one object across both copies of run.py.

The gateway is launched as ``python -m gateway.run``, so gateway/run.py is
executed twice and lives in sys.modules under two names (``__main__`` and
``gateway.run``). The sentinel is stored into the shared running-agent maps
by whichever copy runs the startup auto-resume path and then ``is``-checked
by modules that late-import it — a per-copy ``object()`` makes those checks
miss, and callers crash calling ``.interrupt()`` on the bare object.

Regression for the live-hotfix: identity must come from gateway.sentinels.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import gateway.run
import gateway.sentinels


def test_pending_sentinel_identity_survives_a_second_load_of_run_py():
    """A second exec of gateway/run.py (the ``__main__`` copy) shares the sentinel."""
    source = Path(gateway.run.__file__).resolve()
    spec = importlib.util.spec_from_file_location("gateway.run.duplicate_load", source)
    assert spec is not None and spec.loader is not None
    duplicate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(duplicate)

    assert (
        duplicate._AGENT_PENDING_SENTINEL is gateway.run._AGENT_PENDING_SENTINEL
    ), "the two sys.modules copies of run.py must share one pending-agent sentinel"
    assert (
        gateway.run._AGENT_PENDING_SENTINEL is gateway.sentinels.AGENT_PENDING_SENTINEL
    ), "the sentinel must come from gateway.sentinels, not a per-copy object()"
