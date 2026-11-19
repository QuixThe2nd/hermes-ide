"""Cross-module identity sentinels.

Lives outside gateway/run.py on purpose: the gateway is launched as
``python -m gateway.run``, so run.py exists twice in sys.modules
(``__main__`` and ``gateway.run``). A sentinel defined there as
``object()`` would differ between the two copies, so ``is`` checks in
modules that ``from gateway.run import`` it would miss sentinels set by
``__main__`` (e.g. the startup auto-resume path), then crash calling
``.interrupt()`` on the bare object.
"""

AGENT_PENDING_SENTINEL = object()
