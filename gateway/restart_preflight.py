"""Warning-only runtime-source preflight for chat-requested gateway restarts.

Before ``gateway.restart.queue_user_restart`` persists comeback routing or
offers wind-down, this helper inspects the INSTALLED source checkout (resolved
from this module's own location, never the caller's cwd) with read-only,
lock-free Git plumbing and reports uncommitted runtime-source changes to the
requester. It is a notice, never a gate: every failure path classifies the
result as *unknown* (or *not applicable* for positively identified packaged
installs) and the restart sequence continues unchanged.

Deliberately separate from ``plugins/drift_watch`` (scheduled capture and
attribution, 120s budget) — this is the short UI preflight at the shared chat
restart boundary, and the Git subprocess/parsing detail stays out of the queue
transaction by living here.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time
import unicodedata
from pathlib import Path
from typing import Any, Optional

from hermes_cli.build_info import get_code_identity

logger = logging.getLogger(__name__)

# One total budget for ALL Git commands (spec §5: 2 seconds, shared, no retry)
# and a separate one for the notice delivery attempt. Worst case the preflight
# adds 4 seconds to the queue sequence; the restart drain stays unbounded.
_SCAN_BUDGET_S = 2.0
_DELIVERY_BUDGET_S = 2.0
# Child-cleanup allowance, reserved INSIDE the scan budget — never added on
# top of it: command work runs against ``start + _SCAN_BUDGET_S -
# _REAP_ALLOWANCE_S``, and the SIGTERM→SIGKILL escalation on expiry may spend
# the reserved tail but not one tick past ``start + _SCAN_BUDGET_S``. Worst
# case the whole inspection, reaping included, fits inside the 2 seconds the
# spec (§5) and the 4-second total above promise.
_REAP_ALLOWANCE_S = 0.5
# SIGTERM's share of the allowance; whatever remains waits for the SIGKILL
# reap. A stubborn (TERM-ignoring) child is still collected, promptly.
_TERM_WAIT_S = 0.25
# A coalesced caller rides the owning caller's pending attempt until that
# attempt's setup RESOLVES (release below) — parking the rider past the
# owner's routing write is what keeps a losing caller from overwriting the
# winner's comeback route. The owner's scan+delivery are hard-bounded above;
# the remainder of its setup is the wind-down offer round trip, so the budget
# covers the whole sequence with slack. Past it the rider stops waiting, but
# it still never overtakes a live owner (see ACTIVE_OWNER_PENDING below).
_RIDE_BUDGET_S = _SCAN_BUDGET_S + _DELIVERY_BUDGET_S + 4.0

# Captured Git stdout cap per command (spec §5: 1 MiB → unknown, never a
# truncated clean result).
_MAX_GIT_OUTPUT_BYTES = 1024 * 1024

# Presentation bounds (spec §4).
_NOTICE_CHAR_LIMIT = 1000
_MAX_SAMPLE_PATHS = 5
_MAX_SAMPLE_CHARS = 200

_GIT = "git"

# Env vars that would redirect Git away from the checkout being inspected.
# The preflight must look at the installed tree at its own root, not whatever
# context the launching process happened to carry.
_GIT_REDIRECT_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_CEILING_DIRECTORIES",
        "GIT_NAMESPACE",
    }
)

# The defined runtime-source subset (spec §3): root-level Python modules plus
# Python source under these trees, and plugin manifests under ``plugins/``.
_SCOPE_TOP_DIRS = frozenset({"agent", "cron", "gateway", "hermes_cli", "plugins", "tools"})
# Directory names that are never runtime source: tests, documentation, virtual
# environments, build/caches. (Bytecode/cache files are inherently out — only
# ``*.py`` and ``plugin.yaml`` are ever considered.)
_EXCLUDED_SEGMENTS = frozenset(
    {
        "tests",
        "test",
        "docs",
        "documentation",
        "node_modules",
        "venv",
        ".venv",
        "__pycache__",
        "build",
        "dist",
    }
)

# Porcelain v1 status letters; anything else in a record prefix is a parse
# failure (unknown), never silently skipped.
_STATUS_CHARS = frozenset("MADRCU?! ")
_RENAME_STATUS_CHARS = frozenset("RC")

# Short, FIXED descriptions for unknown causes. Never interpolate exception
# text, paths, or Git stderr into these — the notice is chat-visible.
_UNKNOWN_REASON_TEXT = {
    "deadline": "the check timed out",
    "git-unavailable": "git was unavailable",
    "git-exit": "the repository could not be read",
    "output-cap": "the status output exceeded its bound",
    "parse": "the status output could not be parsed",
    "root-mismatch": "the Git root did not match the installed checkout",
    "concurrent-change": "the checkout changed during the check",
    "internal": "an internal error interrupted the check",
}


class _PreflightUnknown(Exception):
    """Internal: this inspection attempt cannot produce a trustworthy result."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclasses.dataclass
class _SourceScan:
    """One inspection outcome plus the revision context it gathered.

    ``kind`` is one of ``clean`` / ``dirty`` / ``unknown`` / ``not_applicable``.
    Revision fields carry whatever was learned before a failure (unknown keeps
    partial context for the notice).
    """

    kind: str
    reason: Optional[str] = None
    count: int = 0
    samples: list[str] = dataclasses.field(default_factory=list)
    process_revision: Optional[str] = None
    disk_head: Optional[str] = None
    branch: Optional[str] = None
    detached: bool = False


@dataclasses.dataclass
class _Attempt:
    """Provisional preflight state for the one pending shared restart attempt.

    Lives until the owning ``queue_user_restart`` setup resolves (success,
    failure, or cancellation) so concurrent callers coalesce onto one scan and
    at most one warning, while a failed/cancelled setup clears it for a later
    attempt to retry. Not persisted anywhere — a process bounce forgets it.
    """

    finished: asyncio.Future

    def finish(self) -> None:
        if not self.finished.done():
            self.finished.set_result(None)


class _ActiveOwnerPending:
    """Sentinel: another caller's pending setup still owns the restart."""


#: Returned by :func:`warn_before_user_restart` when this caller lost the race
#: to an owner whose setup is still in flight (its wind-down offer can easily
#: outlast a rider's budget). The loser must report the shared
#: already-in-progress result WITHOUT writing comeback routing, entering
#: setup, or sending anything — losing callers never overtake an active owner.
#: If that owner's setup later fails, it releases the attempt and a future
#: call retries.
ACTIVE_OWNER_PENDING = _ActiveOwnerPending()


# One live gateway per process → one pending attempt. Riders never mutate it.
_attempt: Optional[_Attempt] = None


def release_restart_preflight(attempt: Any) -> None:
    """Clear provisional preflight state when the owning setup resolves.

    Called from ``queue_user_restart``'s ``finally``: after a successful
    hand-off (later callers hit the already-in-progress check anyway), or after
    a failed/cancelled setup so a later attempt retries the preflight. Tolerant
    of ``None``/``ACTIVE_OWNER_PENDING`` (losing callers hold no state).
    """
    global _attempt
    if not isinstance(attempt, _Attempt):
        return
    if _attempt is attempt:
        _attempt = None
    attempt.finish()


def _restart_owned(runner: Any) -> bool:
    return bool(
        getattr(runner, "_restart_requested", False)
        or getattr(runner, "_draining", False)
    )


async def warn_before_user_restart(runner: Any, source: Any) -> Any:
    """Run the warning-only source preflight for one queue caller.

    Returns the attempt token the caller must hand to
    :func:`release_restart_preflight` when its setup resolves,
    :data:`ACTIVE_OWNER_PENDING` when another caller's pending setup still
    owns the restart (this caller lost the race and must not overtake it), or
    ``None`` when a restart is already established / there was nothing to do.
    Never raises except cancellation — an inspection or delivery failure only
    downgrades the notice, never blocks the restart.
    """
    global _attempt
    while True:
        if _restart_owned(runner):
            # A restart is already established; the caller's ownership recheck
            # answers already_in_progress. No notice, no work.
            return None
        pending = _attempt
        if pending is None:
            # No pending attempt: this caller becomes the owner of a fresh
            # one, so later callers coalesce HERE rather than racing it into
            # the queue transaction below.
            attempt = _Attempt(asyncio.get_running_loop().create_future())
            _attempt = attempt
            try:
                scan = await _scan_source()
                if not _restart_owned(runner):
                    # Ownership may have moved during the scan's awaits
                    # (another entry point or a signal-initiated drain): a
                    # notice for a restart someone else already owns is noise,
                    # so skip it. The caller's recheck handles the setup side.
                    message = render_notice(scan)
                    if message:
                        await _deliver_notice(runner, source, message)
                return attempt
            except asyncio.CancelledError:
                release_restart_preflight(attempt)
                raise
            except Exception:
                # Defensive: scan/delivery classify their own failures.
                # Anything escaping anyway must not block the restart or
                # strand the state (a stranded pending attempt would silence
                # later preflights) — log it and still hand the requester ONE
                # honest unknown notice. This caller KEEPS ownership of the
                # pending attempt through that awaited fallback send and gets
                # the token back for its setup's ``finally`` to release:
                # releasing here instead would open a window in which a
                # second caller becomes a fresh owner, warns its own
                # requester, and overwrites comeback routing while this
                # caller is still headed into setup.
                logger.warning(
                    "Restart source preflight failed; continuing without a "
                    "verified source state",
                    exc_info=True,
                )
                try:
                    if not _restart_owned(runner):
                        message = render_notice(
                            _SourceScan(kind="unknown", reason="internal")
                        )
                        if message:
                            await _deliver_notice(runner, source, message)
                except asyncio.CancelledError:
                    release_restart_preflight(attempt)
                    raise
                except Exception:
                    pass
                return attempt

        # A pending attempt exists: coalesce onto it. Its owner delivers the
        # one warning and performs the setup; this caller parks (bounded) so
        # it can neither send a duplicate warning nor overwrite the owner's
        # comeback routing.
        try:
            await asyncio.wait_for(
                asyncio.shield(pending.finished), timeout=_RIDE_BUDGET_S
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Owner still unresolved past the ride budget: stop waiting —
            # but see the ownership check below before doing anything else.
            pass
        if _attempt is not None:
            # Still (or newly) owned: the pending caller keeps the restart.
            # Report the shared already-in-progress result; never replace its
            # route, enter a second setup, or warn again.
            return ACTIVE_OWNER_PENDING
        # The owner's setup resolved. Loop: either a restart is now in
        # progress (the recheck above answers None → the queue caller reports
        # already_in_progress) or that setup failed/cancelled and released
        # the attempt — this caller then retries as the owner of a fresh
        # attempt.


def _installed_source_root() -> Path:
    """The installed gateway checkout root, from THIS module's location.

    Independent of the conversation's working directory, caller-supplied paths,
    and configured watched trees — a gateway run from an unrelated cwd still
    inspects the tree it was imported from.
    """
    return Path(__file__).resolve().parent.parent


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REDIRECT_ENV}
    # Belt for Git versions without --no-optional-locks: never take the index
    # lock or refresh the index from a read-only status.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _close_transport(proc: asyncio.subprocess.Process) -> None:
    """Close the subprocess transport (and its pipes) while the loop lives.

    Reading a pipe to EOF does NOT close its transport, and ``Process.wait``
    never touches transports either — without this, the pipe stays open until
    the transport finalizes in ``__del__`` against an already-closed loop
    (the "Event loop is closed" unraisable). Idempotent; never signals
    anything once the child is reaped.
    """
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    try:
        transport.close()
    except Exception:
        pass


async def _wait_reap(proc: asyncio.subprocess.Process, until: float) -> None:
    """Bounded wait for the child to be reaped; never outlives ``until``."""
    remaining = until - time.monotonic()
    if remaining <= 0:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=remaining)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def _kill_and_reap(proc: asyncio.subprocess.Process, *, kill_by: float) -> None:
    """Terminate/kill and reap OUR child only; never signal other processes.

    ``kill_by`` is the ABSOLUTE end of the whole escalation: SIGTERM gets the
    first slice of the remaining time (``_TERM_WAIT_S``), SIGKILL the rest,
    and nothing waits past ``kill_by``. The deadline path passes the scan
    attempt's hard total (work deadline + reserved allowance =
    ``start + _SCAN_BUDGET_S``), so cleanup stays inside the total scan
    budget; the cancellation and output-cap paths bound the escalation to one
    short allowance from now instead of parking on the budget's remainder.
    The transport close in the ``finally`` is synchronous, so it runs even if
    the waits are cancelled.
    """
    try:
        if proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass
            await _wait_reap(proc, min(kill_by, time.monotonic() + _TERM_WAIT_S))
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
            await _wait_reap(proc, kill_by)
    finally:
        _close_transport(proc)


async def _read_capped(proc: asyncio.subprocess.Process) -> bytes:
    """Drain stdout up to the capture cap, reap, and validate the exit code."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await proc.stdout.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_GIT_OUTPUT_BYTES:
            await _kill_and_reap(
                proc, kill_by=time.monotonic() + _REAP_ALLOWANCE_S
            )
            raise _PreflightUnknown("output-cap")
        chunks.append(chunk)
    code = await proc.wait()
    _close_transport(proc)
    if code != 0:
        raise _PreflightUnknown("git-exit")
    return b"".join(chunks)


async def _run_git(args: tuple[str, ...], *, cwd: Path, deadline: float) -> bytes:
    """One read-only Git command under the shared scan work deadline.

    ``deadline`` is the command-work budget — the scan total minus the reap
    allowance reserved for cleanup — shared across all commands of one
    inspection. On expiry the terminate/kill/reap escalation spends exactly
    that reserved tail (``deadline + _REAP_ALLOWANCE_S`` is the attempt's
    hard total), so the whole command stays inside the total scan budget.
    Raises :class:`_PreflightUnknown` on deadline expiry, missing Git,
    nonzero exit, or an over-cap output; kills/reaps/closes its own child on
    timeout or cancellation. ``stderr`` is discarded unread — raw Git errors
    never reach the chat notice.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _PreflightUnknown("deadline")
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                _GIT,
                "--no-optional-locks",
                *args,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_git_env(),
            ),
            timeout=remaining,
        )
    except asyncio.TimeoutError:
        raise _PreflightUnknown("deadline")
    except Exception:
        raise _PreflightUnknown("git-unavailable")
    try:
        return await asyncio.wait_for(
            _read_capped(proc), timeout=deadline - time.monotonic()
        )
    except asyncio.TimeoutError:
        await _kill_and_reap(proc, kill_by=deadline + _REAP_ALLOWANCE_S)
        raise _PreflightUnknown("deadline")
    except asyncio.CancelledError:
        # Collect the child promptly — one short allowance from now — rather
        # than waiting out the remainder of the scan budget.
        await _kill_and_reap(proc, kill_by=time.monotonic() + _REAP_ALLOWANCE_S)
        raise


def _parse_sha(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) in (40, 64) and all(c in "0123456789abcdef" for c in cleaned):
        return cleaned
    raise _PreflightUnknown("parse")


def _decode_path(raw: bytes) -> str:
    # -z porcelain is untranslated bytes; surrogateescape keeps undecodable
    # names inspectable and _escape_path_text neutralizes the surrogates.
    return raw.decode("utf-8", "surrogateescape")


def _parse_porcelain(out: bytes) -> list[tuple[str, str, Optional[str]]]:
    """Parse NUL-delimited porcelain v1 into ``(status, path, orig_path)``.

    Paths are never split on whitespace. Rename/copy records carry their
    original path as the next NUL field. Malformed output — a nonempty stream
    that is not NUL-terminated (truncated), a record with an empty path, or a
    rename record with an empty/missing original path — is a parse failure
    (unknown), never a silently dropped change that could read as clean.
    """
    if out and not out.endswith(b"\0"):
        # Porcelain -z terminates EVERY record with NUL; anything else is a
        # truncated stream, not a verdict.
        raise _PreflightUnknown("parse")
    fields = out.split(b"\0")
    records: list[tuple[str, str, Optional[str]]] = []
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if not field:
            continue
        if len(field) < 3 or field[2:3] != b" ":
            raise _PreflightUnknown("parse")
        status_bytes, path_bytes = field[:2], field[3:]
        if not path_bytes:
            raise _PreflightUnknown("parse")
        try:
            status = status_bytes.decode("ascii")
        except UnicodeDecodeError:
            raise _PreflightUnknown("parse")
        if any(ch not in _STATUS_CHARS for ch in status):
            raise _PreflightUnknown("parse")
        orig: Optional[str] = None
        if any(ch in _RENAME_STATUS_CHARS for ch in status):
            if i >= len(fields) or not fields[i]:
                raise _PreflightUnknown("parse")
            orig = _decode_path(fields[i])
            i += 1
        records.append((status, _decode_path(path_bytes), orig))
    return records


def _path_in_scope(path: str) -> bool:
    """Runtime-source subset membership for one repo-relative POSIX path."""
    parts = [seg for seg in path.split("/") if seg]
    if not parts:
        return False
    if any(seg in _EXCLUDED_SEGMENTS for seg in parts):
        return False
    if len(parts) == 1:
        return parts[0].endswith(".py")
    if parts[0] not in _SCOPE_TOP_DIRS:
        return False
    name = parts[-1]
    if parts[0] == "plugins" and name == "plugin.yaml":
        return True
    return name.endswith(".py")


def _escape_path_text(path: str) -> str:
    """Make a filesystem-derived string safe inside a chat message.

    Control/format/surrogate characters become literal ``\\xNN``/``\\uNNNN``
    escapes (tabs, newlines, bidi overrides cannot reshape the notice),
    backticks are dropped so the code-span wrapping cannot be broken, and
    ``@`` becomes its fullwidth form so no filename can form a live mention.
    """
    out: list[str] = []
    for ch in path:
        code = ord(ch)
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs"):
            out.append(f"\\u{code:04x}" if code > 0xFF else f"\\x{code:02x}")
        elif ch == "`":
            out.append("'")
        elif ch == "@":
            out.append("＠")
        else:
            out.append(ch)
    return "".join(out)


def _clip_sample(raw: str) -> str:
    escaped = _escape_path_text(raw)
    if len(escaped) <= _MAX_SAMPLE_CHARS:
        return escaped
    return escaped[: _MAX_SAMPLE_CHARS - 1] + "…"


def _short(revision: str) -> str:
    return revision[:10]


def _revision_context(scan: _SourceScan) -> str:
    if not (scan.process_revision or scan.disk_head or scan.branch or scan.detached):
        return ""
    process = (
        _short(scan.process_revision) if scan.process_revision else "unavailable"
    )
    bits: list[str] = []
    if scan.disk_head:
        bits.append(f"disk {_short(scan.disk_head)}")
    if scan.branch:
        bits.append(f"branch {_escape_path_text(scan.branch)}")
    elif scan.detached:
        bits.append("detached HEAD")
    if bits:
        return f"Revision context: process {process}; {', '.join(bits)}."
    return f"Revision context: process {process}."


def _cap_notice(text: str) -> str:
    return text[:_NOTICE_CHAR_LIMIT]


def render_notice(scan: _SourceScan) -> str:
    """The requester-facing notice for a scan, or ``""`` when none is due.

    Clean matching revisions stay silent; clean with a known revision
    difference gets a short informational note (a normal update also causes
    it); dirty and unknown get one bounded warning with whatever revision
    context is available. The entry count always precedes the samples so the
    full count survives any truncation.
    """
    if scan.kind == "not_applicable":
        return ""
    context = _revision_context(scan)
    if scan.kind == "unknown":
        reason = _UNKNOWN_REASON_TEXT.get(scan.reason or "", "the check did not finish")
        return _cap_notice(
            f"⚠️ The pre-restart source check could not complete — {reason}. "
            f"Restart will continue; source consistency was not verified.{f' {context}' if context else ''}"
        )
    if scan.kind == "dirty":
        plural = "entry" if scan.count == 1 else "entries"
        for shown in range(min(_MAX_SAMPLE_PATHS, len(scan.samples)), -1, -1):
            sample_list = scan.samples[:shown]
            omitted = scan.count - len(sample_list)
            parts = [f"`{sample}`" for sample in sample_list]
            if omitted > 0:
                parts.append(f"+{omitted} more")
            listing = ", ".join(parts)
            message = (
                f"⚠️ Runtime source changes detected before restart: "
                f"{scan.count} changed {plural}"
                + (f" ({listing})" if listing else "")
                + (f" {context}" if context else "")
                + " Restart will continue using the files on disk at startup — "
                "this check does not freeze them or prove they work together."
            )
            if len(message) <= _NOTICE_CHAR_LIMIT:
                return message
        return message[:_NOTICE_CHAR_LIMIT]
    # Clean tree: only a known process-vs-disk revision difference is worth a
    # short informational note. Unavailable process identity says nothing
    # (never claim a match that was not observed).
    if scan.process_revision and scan.disk_head and scan.process_revision != scan.disk_head:
        where = (
            f"branch {_escape_path_text(scan.branch)}"
            if scan.branch
            else ("detached HEAD" if scan.detached else "unknown branch")
        )
        return _cap_notice(
            f"ℹ️ Restart note: this process started at revision "
            f"{_short(scan.process_revision)} but the checkout is now "
            f"{_short(scan.disk_head)} ({where}); a normal update also causes "
            "this. The restart uses the files currently on disk."
        )
    return ""


async def _scan_source() -> _SourceScan:
    """Inspect the installed checkout once, under the total scan budget."""
    # Cached process identity only — never refreshed here; this is the
    # revision the RUNNING process started with, not a fresh Git probe.
    identity = get_code_identity(refresh=False)
    if identity.get("source") == "build-file":
        # Positively identified packaged install (baked build sha) — the Git
        # warning does not apply.
        return _SourceScan(kind="not_applicable")
    process_revision = identity.get("sha") or None

    root = _installed_source_root()
    # Work deadline for every command: the reap allowance stays RESERVED off
    # the total, so the timeout escalation (terminate → kill → reap) can
    # spend it without the inspection ever running past
    # ``start + _SCAN_BUDGET_S``.
    deadline = time.monotonic() + _SCAN_BUDGET_S - _REAP_ALLOWANCE_S
    head: Optional[str] = None
    branch: Optional[str] = None
    detached = False
    try:
        toplevel = _decode_path(
            await _run_git(("rev-parse", "--show-toplevel"), cwd=root, deadline=deadline)
        ).strip()
        try:
            root_matches = Path(toplevel).resolve() == root
        except OSError:
            root_matches = False
        if not root_matches:
            raise _PreflightUnknown("root-mismatch")

        head = _parse_sha(
            (await _run_git(("rev-parse", "HEAD"), cwd=root, deadline=deadline))
            .decode("ascii", "replace")
            .strip()
        )
        branch_raw = _decode_path(
            await _run_git(
                ("rev-parse", "--abbrev-ref", "HEAD"), cwd=root, deadline=deadline
            )
        ).strip()
        detached = branch_raw in ("", "HEAD")
        branch = None if detached else branch_raw

        porcelain = await _run_git(
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            cwd=root,
            deadline=deadline,
        )
        # Observable identity recheck: a HEAD that moved under the status scan
        # invalidates the records just collected.
        head_after = _parse_sha(
            (await _run_git(("rev-parse", "HEAD"), cwd=root, deadline=deadline))
            .decode("ascii", "replace")
            .strip()
        )
        if head_after != head:
            raise _PreflightUnknown("concurrent-change")
        # Parsed inside the same guard: untranslatable status output is an
        # "unknown" scan (warn-honestly), never an exception past this module.
        records = _parse_porcelain(porcelain)
    except _PreflightUnknown as exc:
        return _SourceScan(
            kind="unknown",
            reason=exc.reason,
            process_revision=process_revision,
            disk_head=head,
            branch=branch,
            detached=detached,
        )

    relevant: list[tuple[str, str, Optional[str]]] = []
    for status, path, orig in records:
        # A rename counts once; either side inside the runtime subset makes the
        # record relevant.
        path_scoped = _path_in_scope(path)
        orig_scoped = orig is not None and _path_in_scope(orig)
        if path_scoped or orig_scoped:
            relevant.append((status, path, orig))

    samples: list[str] = []
    for _status, path, orig in relevant[:_MAX_SAMPLE_PATHS]:
        if orig is not None and _path_in_scope(orig):
            if _path_in_scope(path):
                samples.append(_clip_sample(f"{orig} -> {path}"))
            else:
                samples.append(_clip_sample(orig))
        else:
            samples.append(_clip_sample(path))

    return _SourceScan(
        kind="dirty" if relevant else "clean",
        count=len(relevant),
        samples=samples,
        process_revision=process_revision,
        disk_head=head,
        branch=branch,
        detached=detached,
    )


async def _deliver_notice(runner: Any, source: Any, message: str) -> None:
    """One bounded delivery attempt on the requester's own route.

    No valid route (no source/chat/adapter) → log only, never a guessed
    channel. A failed or timed-out send is logged and the restart continues;
    cancellation propagates.
    """
    adapter = None
    chat_id = ""
    try:
        chat_id = str(getattr(source, "chat_id", "") or "")
        if source is not None and chat_id:
            resolver = getattr(runner, "_adapter_for_source", None)
            adapter = resolver(source) if callable(resolver) else None
    except Exception:
        adapter = None
    if adapter is None:
        logger.warning("Restart source notice (no requester route): %s", message)
        return
    metadata: dict = {}
    thread_id = str(getattr(source, "thread_id", "") or "").strip()
    if thread_id:
        metadata["thread_id"] = thread_id
    try:
        result = await asyncio.wait_for(
            adapter.send(chat_id, message, metadata=metadata or None),
            timeout=_DELIVERY_BUDGET_S,
        )
        if getattr(result, "success", True) is False:
            logger.warning(
                "Restart source notice delivery reported failure; restart continues"
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Restart source notice delivery failed (%s); restart continues",
            type(exc).__name__,
        )
