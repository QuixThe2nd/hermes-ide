"""Scheduled update runner — invokes the stock ``hermes`` CLI only.

One timer tick is two phases:

**Phase A — prepare.** Always runs ``hermes update --check`` and, when an
update is available, ``hermes update --yes --defer-restart``. Preparation is
*not* idle-gated: pulling code, syncing dependencies and running migrations
never interrupts a conversation, so a busy Hermes still ends the tick with
the update staged and ``fleet_restart_pending`` left behind. A check or
prepare that fails — or hangs past its timeout — is a **nonzero** tick
outcome and dispatches no activation: not knowing whether an update exists,
or whether a preparation finished, is never treated as "nothing to do".
A nonzero ``--check`` exit is a check failure regardless of output text —
the stock check reports availability with exit 0, so no nonzero rc ever
means "update available".
A timed-out prepare child is killed AND reaped by ``subprocess.run`` before
``TimeoutExpired`` reaches this code, so a child cannot publish readiness
after the parent reported the timeout; the attempt/generation boundary does
the rest — readiness is a fresh generation minted only at the end of a
*completed* attempt, published into ``fleet_restart_prepared`` (never the
generic pull-time marker), so a timed-out re-prepare leaves an older valid
prepared generation byte-identical.

**Phase B — activate.** Always runs, including on ticks that prepared
nothing, and dispatches to the ``hermes auto_update activate`` subcommand in
a *fresh process*. The freshly spawned interpreter imports the code Phase A
just pulled; the parent process must not, because it is still running the
pre-pull modules. The activation command owns the final idle check and the
pending-restart catch-up, so a prepared update waits however many ticks it
takes for Hermes to go idle — and it also refuses any pending update whose
preparation never finished (see ``_PREPARE_INCOMPLETE_REASONS``).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

from hermes_cli.relaunch import resolve_hermes_bin
from hermes_cli.update_lock import read_live_update

from plugins.auto_update.config import (
    _coerce_bool,
    load_auto_update_config,
    plugin_explicitly_disabled,
)
from plugins.auto_update.lock import nonblocking_run_lock
from plugins.auto_update.notify import emit_notification

logger = logging.getLogger(__name__)

UPDATE_CHECK_ARGV = ("update", "--check")
UPDATE_APPLY_ARGV = ("update", "--yes")
# Preparation without activation: the stock updater stages the update and
# leaves ``fleet_restart_pending`` instead of restarting the fleet.
UPDATE_PREPARE_ARGV = ("update", "--yes", "--defer-restart")
ACTIVATE_ARGV = ("auto_update", "activate")

UP_TO_DATE_MARKERS = ("already up to date", "✓ already up to date")
UPDATE_AVAILABLE_MARKER = "⚕ update available"

# Prepare outcomes after which Phase B must NOT run: whatever is on disk is
# not a completed update, so restarting the fleet onto it is exactly the
# "presented as ready" failure the deferred mode must never produce. The same
# guard holds across ticks — the marker is written before the preparation
# finishes, so ``auto_update activate`` refuses any pending marker that lacks
# the prepared generation only a completed ``--defer-restart`` run publishes.
# A check failure or timeout is here for the same reason: not knowing whether
# an update is available is not "no update", and the tick must not paper over
# either with exit 0. An older prepared update waits for a tick whose check
# actually succeeded — never for one that could not see the remote at all.
_PREPARE_INCOMPLETE_REASONS = frozenset(
    {"prepare_failed", "prepare_timeout", "check_failed", "check_timeout"}
)

# mode → public CLI argv tail. Every dispatch goes through the public
# subcommand surface — no internal updater modules are imported here, so a
# freshly pulled checkout is only ever loaded by a fresh process.
_UPDATER_ARGV_BY_MODE = {
    "check": UPDATE_CHECK_ARGV,
    "apply": UPDATE_APPLY_ARGV,
    "prepare": UPDATE_PREPARE_ARGV,
    "activate": ACTIVATE_ARGV,
}


@dataclass(frozen=True)
class RunOutcome:
    code: int
    reason: str


def build_stock_updater_argv(mode: str) -> list[str]:
    """Return the exact public CLI argv surface — no internal modules."""
    try:
        tail = _UPDATER_ARGV_BY_MODE[mode]
    except KeyError:
        raise ValueError(f"unknown updater mode: {mode!r}") from None
    hermes_bin = resolve_hermes_bin()
    if hermes_bin:
        return [hermes_bin, *tail]
    import sys

    return [sys.executable, "-m", "hermes_cli.main", *tail]


def _check_output_indicates_update_available(text: str) -> bool:
    lowered = (text or "").lower()
    if any(marker in lowered for marker in UP_TO_DATE_MARKERS):
        return False
    return UPDATE_AVAILABLE_MARKER in lowered


def run_subprocess(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )


def _prepare_phase(
    settings: dict,
    run_cmd: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> RunOutcome:
    """Check for an update and stage it. Never restarts anything."""
    try:
        check = run_cmd(build_stock_updater_argv("check"))
    except subprocess.TimeoutExpired:
        emit_notification(settings.get("notify_on_failure", ""))
        logger.warning("auto-update check timed out")
        return RunOutcome(1, "check_timeout")

    combined = "\n".join(filter(None, (check.stdout, check.stderr)))
    # The stock check reports availability with exit 0, so a nonzero rc is a
    # check failure no matter what the output says — never "update available".
    if check.returncode != 0:
        emit_notification(settings.get("notify_on_failure", ""))
        logger.warning(
            "auto-update check failed: rc=%s stderr=%s",
            check.returncode,
            (check.stderr or "")[:500],
        )
        return RunOutcome(check.returncode, "check_failed")

    if not _check_output_indicates_update_available(combined):
        return RunOutcome(0, "no_update")

    try:
        prepare = run_cmd(build_stock_updater_argv("prepare"))
    except subprocess.TimeoutExpired:
        emit_notification(settings.get("notify_on_failure", ""))
        logger.warning("auto-update prepare timed out")
        return RunOutcome(1, "prepare_timeout")

    if prepare.returncode == 0:
        emit_notification(settings.get("notify_on_success", ""))
        return RunOutcome(0, "prepared")
    emit_notification(settings.get("notify_on_failure", ""))
    logger.warning(
        "auto-update prepare failed: rc=%s stderr=%s",
        prepare.returncode,
        (prepare.stderr or "")[:500],
    )
    return RunOutcome(prepare.returncode, "prepare_failed")


def _activation_phase(
    settings: dict,
    run_activation: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> RunOutcome:
    """Dispatch the fresh-process activation attempt."""
    try:
        activation = run_activation(build_stock_updater_argv("activate"))
    except subprocess.TimeoutExpired:
        logger.warning("auto-update activation timed out")
        return RunOutcome(1, "activation_timeout")
    if activation.returncode == 0:
        return RunOutcome(0, "activation_attempted")
    emit_notification(settings.get("notify_on_failure", ""))
    logger.warning(
        "auto-update activation failed: rc=%s stderr=%s",
        activation.returncode,
        (activation.stderr or "")[:500],
    )
    return RunOutcome(activation.returncode, "activation_failed")


def run_scheduled_update(
    *,
    cfg: dict | None = None,
    run_cmd: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
    read_live_update_fn: Callable | None = None,
    run_activation: Callable[
        [Sequence[str]], subprocess.CompletedProcess[str]
    ] | None = None,
) -> RunOutcome:
    """Run one timer tick: prepare whatever is available, then try to activate.

    Idleness is deliberately NOT consulted here — preparation is safe while
    Hermes is busy, and the activation subcommand re-checks idleness in its
    own fresh process immediately before restarting anything.

    The subprocess defaults resolve at call time (not at import) so tests can
    substitute them by patching this module's attributes.
    """
    run_cmd = run_cmd or run_subprocess
    read_live_update_fn = read_live_update_fn or read_live_update
    run_activation = run_activation or run_subprocess

    if plugin_explicitly_disabled():
        return RunOutcome(0, "disabled")

    settings = cfg or load_auto_update_config()
    if not _coerce_bool(settings.get("enabled"), True):
        return RunOutcome(0, "disabled")

    if read_live_update_fn() is not None:
        return RunOutcome(0, "update_in_progress")

    with nonblocking_run_lock() as locked:
        if not locked:
            return RunOutcome(0, "lock_contention")

        prepare = _prepare_phase(settings, run_cmd)
        if prepare.reason in _PREPARE_INCOMPLETE_REASONS:
            # A preparation that failed or never finished must never be
            # activated: whatever is on disk is not a completed update.
            return prepare
        return _activation_phase(settings, run_activation)
