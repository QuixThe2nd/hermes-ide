"""Live reload of the Discord reaction gate's ``reaction_gate`` config block.

Editing the block in the running profile's ``config.yaml`` takes effect on the
connected adapter — no gateway restart, no adapter reconnect, no touch of the
speaking (response) gate. The watcher is the mechanics only (stat, sleep, read,
extract); validating the block, rebuilding the runtime and swapping
``adapter._reaction_gate`` stay in the adapter's ``_reaction_gate_reload`` so the
runtime contract keeps a single owner.

Design constraints (see the reaction-gate section of the Discord config reference):

* **Captured path.** The config file is resolved ONCE by the caller, inside
  ``connect()``'s profile scope (the same capture discipline as the judge
  credential and the gate-env snapshot). This loop runs outside any profile
  scope, so it never re-resolves ``HERMES_HOME`` and never reads ``os.environ``:
  a multiplexed sibling profile flipping the process env mid-flight cannot
  redirect or disable another adapter's watch.
* **Fail closed, never fail loud.** A detected change that cannot be applied —
  unreadable file, invalid YAML, a block that fails validation — keeps the
  previously active runtime running unchanged and logs ONE warning per failed
  reload attempt. Attempts only happen on a stat change, so a broken file left
  alone is warned about once, not once per poll tick.
* **Counts only.** Logs carry counts and outcomes — never the emoji whitelist,
  criteria text or file contents.
* **Bounded.** One task per adapter, a fixed poll cadence slept in small slices
  (so a cancel is honored promptly mid-wait), cancelled on ``disconnect()``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

#: How often the captured config file is stat()ed for an edit (mtime_ns + size).
REACTION_GATE_RELOAD_POLL_SECONDS = 5.0

#: The poll wait is slept in slices of this size so a cancel lands promptly even
#: mid-wait (and a test can shrink the cadence without long dead time).
REACTION_GATE_RELOAD_SLEEP_SLICE = 0.1


def extract_reaction_gate_block(yaml_cfg: Any) -> Any:
    """The raw ``reaction_gate`` sub-block of a parsed ``config.yaml``, or ``None``.

    Same precedence the gateway loader applies (``gateway.config_loader.platform_section``):
    a top-level ``discord:`` block wins; otherwise the block under
    ``gateway.platforms.discord`` / ``platforms.discord`` carries it. ``None`` covers
    "no block anywhere" (an edit that removes the block disables the gate).
    """
    from gateway.config_loader import platform_section

    if not isinstance(yaml_cfg, dict):
        return None
    gateway_section = yaml_cfg.get("gateway")
    gateway_platforms = (
        gateway_section.get("platforms") if isinstance(gateway_section, dict) else None
    )
    section, _toplevel = platform_section(yaml_cfg, "discord", gateway_platforms)
    if not isinstance(section, dict):
        return None
    return section.get("reaction_gate")


class ReactionGateReloadWatcher:
    """Poll the captured config file and hand each NEW ``reaction_gate`` block to ``apply``.

    ``apply(raw_block_or_None) -> bool`` validates, rebuilds and swaps the runtime
    (``True`` = a new runtime or a deliberate off was installed; ``False`` = the
    block was rejected and the previous gate keeps running). The watcher calls it
    only when the file's stat signature changed AND the extracted block differs
    from the last applied one, so an unrelated config edit — or a pure mtime touch
    — rebuilds nothing and logs nothing, and a rejected block is re-attempted on
    the next edit (the operator's fix), not on every poll tick.
    """

    def __init__(
        self,
        *,
        config_path: Path,
        apply: Callable[[Any], bool],
        logger: logging.Logger,
        name: str = "",
        poll_seconds: Optional[float] = None,
        sleep_slice: Optional[float] = None,
    ) -> None:
        self.config_path = Path(config_path)
        self._apply = apply
        self._log = logger
        self._prefix = f"[{name}] " if name else ""
        self.poll_seconds = (
            REACTION_GATE_RELOAD_POLL_SECONDS if poll_seconds is None else float(poll_seconds)
        )
        self.sleep_slice = (
            REACTION_GATE_RELOAD_SLEEP_SLICE if sleep_slice is None else float(sleep_slice)
        )

    async def run(self) -> None:
        """Watch until cancelled. Never raises: a wedged reload cannot kill the watch."""
        signature = self._stat()
        applied_block = self._load()[0]  # seed silently: boot state, not a reload attempt
        while True:
            await self._sleep(self.poll_seconds)
            current = self._stat()
            if current == signature:
                continue
            signature = current
            block, error = self._load()
            if error is not None:
                # One warning per FAILED RELOAD ATTEMPT (this is one); the advanced
                # signature means the same broken file is not re-attempted next tick.
                self._log.warning(
                    "%sreaction_gate reload skipped (%s): the active gate keeps running",
                    self._prefix, error,
                )
                continue
            if block == applied_block:
                continue  # the file changed outside the reaction_gate block
            try:
                applied = bool(self._apply(block))
            except Exception:  # pragma: no cover - defensive: the watch outlives a bad block
                self._log.warning(
                    "%sreaction_gate reload attempt failed; the active gate keeps running",
                    self._prefix,
                    exc_info=True,
                )
                continue
            if applied:
                applied_block = block

    # --- mechanics ---------------------------------------------------------

    def _stat(self) -> Optional[Tuple[int, int]]:
        """``(st_mtime_ns, st_size)`` of the captured file; ``None`` while it is absent."""
        try:
            stat = self.config_path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _load(self) -> Tuple[Any, Optional[str]]:
        """``(block, error)``: the extracted raw block, or ``(None, reason)`` when unreadable.

        Reasons are fixed strings, never the parser's message: ``yaml`` errors embed a
        snippet of the file, and file contents never reach the log.
        """
        import yaml

        try:
            with open(self.config_path, "r", encoding="utf-8") as handle:
                yaml_cfg = yaml.safe_load(handle) or {}
        except (OSError, UnicodeError):
            return None, "config file unreadable"
        except yaml.YAMLError:
            return None, "config file is not valid YAML"
        return extract_reaction_gate_block(yaml_cfg), None

    async def _sleep(self, seconds: float) -> None:
        """Sleep in small slices so a cancel is honored promptly mid-wait."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, seconds)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, self.sleep_slice))


def start_reaction_gate_reload_watcher(
    *,
    config_path: Path,
    apply: Callable[[Any], bool],
    logger: logging.Logger,
    name: str = "",
    poll_seconds: Optional[float] = None,
    sleep_slice: Optional[float] = None,
) -> asyncio.Task:
    """Start one bounded watcher task for an adapter's ``reaction_gate`` block.

    The caller resolves ``config_path`` inside ``connect()``'s profile scope; from
    here on the watcher uses only the captured path. ``name`` is the adapter's
    display name, used only to prefix log lines like every other adapter line.
    """
    watcher = ReactionGateReloadWatcher(
        config_path=config_path, apply=apply, logger=logger, name=name,
        poll_seconds=poll_seconds, sleep_slice=sleep_slice,
    )
    return asyncio.create_task(watcher.run(), name="discord-reaction-gate-reload")
