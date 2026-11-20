"""Per-turn phase timing accumulators: ``mem`` / ``setup`` / ``compress`` / ``save``.

The conversation loop resets these next to ``_turn_api_time``; phase owners add
perf_counter deltas and the finalizer copies them onto the turn result as
``<phase>_time`` so the gateway footer can render measured categories instead
of one unexplained ``other`` remainder.

Non-double-counting contract: a phase bucket never overlaps
``api_time``/``tool_time``. In-retry-loop overflow compression stays inside
``api_time`` (retries are api time), and the prologue's ``setup`` bucket
subtracts the ``mem``/``compress`` deltas its sub-phases claimed while running
inside the same block (``timed_setup_exclusive``)."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator

PHASE_KEYS: tuple[str, ...] = ("mem", "setup", "compress", "save")


def reset_turn_phase_times(agent: Any) -> None:
    """Zero every phase accumulator (called next to the api/tool resets)."""
    for key in PHASE_KEYS:
        setattr(agent, f"_turn_{key}_time", 0.0)


def add_phase_time(agent: Any, key: str, seconds: float) -> None:
    """Add wall-clock seconds to a phase accumulator, seeding missing attrs at 0.

    Negative deltas (a monkeypatched clock running backwards) clamp to zero.
    Never raises: read-only test doubles share these call sites with real agents.
    """
    attr = f"_turn_{key}_time"
    current = getattr(agent, attr, 0.0)
    try:
        setattr(agent, attr, float(current or 0.0) + max(0.0, float(seconds)))
    except Exception:
        pass


@contextmanager
def timed_phase(agent: Any, key: str) -> Iterator[None]:
    """Attribute the with-block's wall time to one phase bucket."""
    start = time.perf_counter()
    try:
        yield
    finally:
        add_phase_time(agent, key, time.perf_counter() - start)


@contextmanager
def timed_setup_exclusive(agent: Any) -> Iterator[None]:
    """Time the turn prologue (``build_turn_context``) as ``setup``.

    Memory prefetch and turn-start compression run INSIDE the prologue and claim
    their own buckets first; subtracting their deltas keeps the same wall time
    from being attributed to both ``setup`` and ``mem``/``compress``.
    """
    start = time.perf_counter()
    mem0 = getattr(agent, "_turn_mem_time", 0.0) or 0.0
    comp0 = getattr(agent, "_turn_compress_time", 0.0) or 0.0
    try:
        yield
    finally:
        claimed = max(0.0, float(getattr(agent, "_turn_mem_time", 0.0) or 0.0) - mem0) \
            + max(0.0, float(getattr(agent, "_turn_compress_time", 0.0) or 0.0) - comp0)
        add_phase_time(agent, "setup", (time.perf_counter() - start) - claimed)
