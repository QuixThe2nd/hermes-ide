"""Live reload of the Discord reaction gate's ``reaction_gate`` config block.

Editing the block in the running profile's ``config.yaml`` takes effect on the
connected adapter — no gateway restart, no adapter reconnect, no touch of the
speaking (response) gate. The watcher is the mechanics only (stat, sleep, read,
extract); validating the block, rebuilding the runtime and swapping
``adapter._reaction_gate`` stay in the adapter's ``_reaction_gate_reload`` so the
runtime contract keeps a single owner.

Design constraints (see the reaction-gate section of the Discord config reference):

* **Startup parity.** The block is read through the same machinery the gateway
  loader uses — the managed overlay (`hermes_cli.managed_scope.apply_managed_overlay`)
  and the platform-section merge/bridge (`gateway.config_loader.merge_platform_sections`
  + `bridge_platform_shared_keys`, then the typed-then-``extra`` lookup
  ``PlatformConfig.from_dict`` performs) — so a live edit produces exactly the
  effective config a restart would, for every supported spelling (top-level
  ``discord:``, ``gateway.platforms.discord``, ``platforms.discord``,
  ``gateway.discord``) and with administrator-pinned values unbypassable.
* **Captured scope.** The user config file, the managed-scope directory AND the
  values of every ``${VAR}`` ref in the managed config are resolved ONCE by the
  caller, inside ``connect()``'s profile scope (the same capture discipline as the
  judge credential and the gate-env snapshot). This loop runs outside any profile
  scope, so it never re-resolves ``HERMES_HOME``, the managed dir, a ref's value
  or any secret: a multiplexed sibling profile flipping the process env mid-flight
  cannot redirect or disable another adapter's watch, and a managed
  ``reaction_gate.channels``/``criteria``/``decisions_url`` ref keeps the value the
  owning profile captured. Both captured files are stat()ed, so a managed-file
  edit reloads on the same cadence as a user edit.
* **Managed edits fail closed too.** A watched managed file that is malformed,
  truncated, non-mapping or unreadable mid-edit (strict read — see
  ``managed_scope.load_managed_config(strict=True)``) is a failed reload attempt
  like any other: the administrator's previous pins and the active runtime keep
  running, with one fixed, content-free warning per attempt. Only a VALID managed
  mapping (including one that removes gate leaves) changes policy. The same holds
  for a user file whose nested shapes the merge machinery cannot read (e.g. a
  non-mapping ``platforms.discord.extra``): the attempt is skipped, the runtime is
  retained, and the next valid edit applies.
* **Fail closed, never fail loud.** A detected change that cannot be applied —
  unreadable or deleted file, invalid YAML, a non-mapping document root
  (``false``/``[]``/scalar/null/empty — an editor's truncate-write passes through
  exactly this shape, so it is never read as a deliberate removal), or a block
  that fails validation — keeps the previously active runtime running unchanged
  and logs ONE warning per failed reload attempt. Attempts only happen on a stat
  change, so a broken file left alone is warned about once, not once per poll
  tick. Only a valid MAPPING whose block is gone (or ``enabled: false``) disables.
* **Boot reconcile, not boot seed.** The adapter built its gate from the
  platform-config snapshot loaded at gateway start; the file may have moved on
  since, and a replacement adapter can carry a stale cached snapshot. Seeding
  the current file as "already applied" would pin that stale state until the
  next edit, so the watcher's first act hands the CURRENT block to ``apply``
  (which no-ops when the running gate already matches it).
* **Counts only.** Logs carry counts and outcomes — never the emoji whitelist,
  criteria text or file contents.
* **Bounded.** One task per adapter, a fixed poll cadence slept in small slices
  (so a cancel is honored promptly mid-wait), cancelled on ``disconnect()``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

#: How often the captured config files are stat()ed for an edit (mtime_ns + size).
REACTION_GATE_RELOAD_POLL_SECONDS = 5.0

#: The poll wait is slept in slices of this size so a cancel lands promptly even
#: mid-wait (and a test can shrink the cadence without long dead time).
REACTION_GATE_RELOAD_SLEEP_SLICE = 0.1

#: ``applied_block`` before any block was successfully applied (boot load failed
#: or the boot block was rejected): unlike ``None`` — a real block value meaning
#: "gate removed" — the sentinel never compares equal, so the next edit is
#: always attempted.
_BOOT_UNKNOWN = object()


def extract_reaction_gate_block(yaml_cfg: Any) -> Any:
    """The effective raw ``reaction_gate`` block of a parsed ``config.yaml``, or ``None``.

    Resolved through the real startup-loader machinery — ``merge_platform_sections``
    + ``bridge_platform_shared_keys``, then the typed-slot-then-``extra`` lookup
    ``PlatformConfig.from_dict`` performs — so every spelling the gateway honors
    (top-level ``discord:``, ``gateway.platforms.discord``, ``platforms.discord``,
    ``gateway.discord``) lands here with the same precedence as at startup, for
    live edits and for the boot reconcile alike. ``None`` covers "no block
    anywhere" (a valid-mapping edit that removes the block disables the gate).
    """
    from gateway.config import Platform
    from gateway.config_loader import bridge_platform_shared_keys, merge_platform_sections

    if not isinstance(yaml_cfg, dict):
        return None
    gateway_section = yaml_cfg.get("gateway")
    gateway_platforms = (
        gateway_section.get("platforms") if isinstance(gateway_section, dict) else None
    )
    platforms_data = merge_platform_sections(yaml_cfg, gateway_section, {})
    bridge_platform_shared_keys(yaml_cfg, gateway_platforms, {}, platforms_data, [Platform.DISCORD])
    section = platforms_data.get(Platform.DISCORD.value)
    if not isinstance(section, dict):
        return None
    block = section.get("reaction_gate")
    if block is None:
        extra = section.get("extra")
        block = extra.get("reaction_gate") if isinstance(extra, dict) else None
    return block


class ReactionGateReloadWatcher:
    """Poll the captured config files and hand each NEW ``reaction_gate`` block to ``apply``.

    ``apply(raw_block_or_None) -> bool`` validates, rebuilds and swaps the runtime
    (``True`` = a new runtime or a deliberate off was installed; ``False`` = the
    block was rejected and the previous gate keeps running). After the one boot
    reconcile (see ``_reconcile_boot``), the watcher calls it only when a stat
    signature changed AND the extracted block differs from the last applied one,
    so an unrelated config edit — or a pure mtime touch — rebuilds nothing and
    logs nothing, and a rejected block is re-attempted on the next edit (the
    operator's fix), not on every poll tick.
    """

    def __init__(
        self,
        *,
        config_path: Path,
        apply: Callable[[Any], bool],
        logger: logging.Logger,
        name: str = "",
        managed_dir: Optional[Path] = None,
        managed_env: Optional[Dict[str, str]] = None,
        poll_seconds: Optional[float] = None,
        sleep_slice: Optional[float] = None,
    ) -> None:
        self.config_path = Path(config_path)
        # The managed scope captured at connect (None = no scope present THEN;
        # a scope appearing later belongs to the next connect, not to a poll-time
        # env re-resolve). Its config.yaml is watched alongside the user file:
        # admin-pinned values are part of the effective config, so a managed edit
        # is a config change like any other.
        self._managed_dir = Path(managed_dir) if managed_dir is not None else None
        self._managed_file = (
            self._managed_dir / "config.yaml" if self._managed_dir is not None else None
        )
        # Values of the managed config's ${VAR} refs, captured at connect inside the
        # owning profile's scope. The overlay expands against THIS mapping only —
        # a poll never reads os.environ or a secret, so a mid-flight env flip (or a
        # sibling profile) cannot rewrite a pinned ref.
        self._managed_env = dict(managed_env) if managed_env is not None else None
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
        applied_block = self._reconcile_boot()
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
                continue  # the files changed outside the effective reaction_gate block
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

    def _reconcile_boot(self) -> Any:
        """Hand the CURRENT block to ``apply`` once; return the block now in force.

        The adapter's gate was built from the platform-config snapshot loaded at
        gateway start, which the file may have moved ahead of (or a replacement
        adapter may carry a stale cached snapshot): seeding the file as "already
        applied" would pin that stale state until the next edit. ``apply``
        no-ops when the running gate already matches, so an unchanged file costs
        one validation, not a rebuild. A file that fails to load at boot is not
        a reload attempt — startup already reported it — so stay silent and
        report the sentinel: the first real edit is always attempted.
        """
        block, error = self._load()
        if error is not None:
            return _BOOT_UNKNOWN
        try:
            applied = bool(self._apply(block))
        except Exception:  # pragma: no cover - defensive: the watch outlives a bad block
            self._log.warning(
                "%sreaction_gate reload attempt failed; the active gate keeps running",
                self._prefix,
                exc_info=True,
            )
            return _BOOT_UNKNOWN
        return block if applied else _BOOT_UNKNOWN

    @staticmethod
    def _stat_one(path: Optional[Path]) -> Optional[Tuple[int, int]]:
        """``(st_mtime_ns, st_size)`` of one captured file; ``None`` while it is absent."""
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _stat(self) -> Tuple[Optional[Tuple[int, int]], Optional[Tuple[int, int]]]:
        """``(user, managed)`` stat signatures of the captured files."""
        return (self._stat_one(self.config_path), self._stat_one(self._managed_file))

    def _load(self) -> Tuple[Any, Optional[str]]:
        """``(block, error)``: the extracted effective block, or ``(None, reason)``.

        Reasons are fixed strings, never the parser's message: ``yaml`` errors embed a
        snippet of the file, and file contents never reach the log. A non-mapping
        document root (``false``/``[]``/scalar/null/empty) is an ERROR, not a
        removal — the startup loader reads that shape as "no user config", and a
        live reload must never dismantle a running gate over it. The managed
        overlay is part of the effective config at startup, so it is part of it
        here — from the scope captured at connect, never re-resolved at poll time,
        read STRICT: a managed file that exists but is malformed/truncated/
        non-mapping/unreadable mid-edit fails this attempt (the administrator's
        pins and the active runtime keep running) instead of silently dropping
        the pins and letting user values through. A user file whose nested shapes
        break the extraction machinery (e.g. a non-mapping ``platforms.discord.extra``)
        is likewise a failed attempt, not a watcher death: the next valid edit
        applies normally.
        """
        import yaml

        try:
            with open(self.config_path, "r", encoding="utf-8") as handle:
                yaml_cfg = yaml.safe_load(handle)
        except (OSError, UnicodeError):
            return None, "config file unreadable"
        except yaml.YAMLError:
            return None, "config file is not valid YAML"
        if not isinstance(yaml_cfg, dict):
            return None, "config file root is not a mapping"
        if self._managed_dir is not None:
            from hermes_cli import managed_scope
            try:
                yaml_cfg = managed_scope.apply_managed_overlay(
                    yaml_cfg,
                    managed_dir=self._managed_dir,
                    env=self._managed_env,
                    strict=True,
                )
            except managed_scope.ManagedConfigError:
                return None, "managed config unreadable or not a valid mapping"
        try:
            return extract_reaction_gate_block(yaml_cfg), None
        except Exception:  # noqa: BLE001 — a broken shape fails the attempt, never the watch
            return None, "config structure could not be merged"

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
    managed_dir: Optional[Path] = None,
    managed_env: Optional[Dict[str, str]] = None,
    poll_seconds: Optional[float] = None,
    sleep_slice: Optional[float] = None,
) -> asyncio.Task:
    """Start one bounded watcher task for an adapter's ``reaction_gate`` block.

    The caller resolves ``config_path``, ``managed_dir`` and ``managed_env`` inside
    ``connect()``'s profile scope; from here on the watcher uses only the captured
    paths and values. ``name`` is the adapter's display name, used only to prefix
    log lines like every other adapter line.
    """
    watcher = ReactionGateReloadWatcher(
        config_path=config_path, apply=apply, logger=logger, name=name,
        managed_dir=managed_dir, managed_env=managed_env,
        poll_seconds=poll_seconds, sleep_slice=sleep_slice,
    )
    return asyncio.create_task(watcher.run(), name="discord-reaction-gate-reload")
