"""Native OS desktop notification when an agent turn finishes.

Optional display feature, sibling of ``display.bell_on_complete``: when
``display.notify_on_complete`` is enabled, the end of each completed turn
pops a native OS notification titled with the chat/session title —
subtitle ``Task finished``, body ``Session finished · <platform>``.

The primary use case is a Hermes gateway running on a headless Linux box
while the user sits at a Mac: ``display.notify_on_complete_ssh: "host"``
runs the same AppleScript on the Mac over SSH instead of dispatching
locally, so the toast appears on the user's actual desktop.

Everything here is best-effort by contract: no call may raise, print, or
block longer than ``_SUBPROCESS_TIMEOUT_SECONDS``. A notification that
cannot be delivered is dropped with a debug log — it must never delay or
fail the turn it is announcing.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys

from hermes_constants import get_hermes_home
from hermes_cli._subprocess_compat import windows_hide_flags

logger = logging.getLogger(__name__)

# Hard ceiling on any single dispatcher subprocess (command override, SSH,
# or local). The ssh connection gets its own faster ConnectTimeout; this
# bounds the whole attempt regardless of what the remote side does.
_SUBPROCESS_TIMEOUT_SECONDS = 10

# Read-only title lookup must never stall the turn on a locked database.
_DB_TIMEOUT_SECONDS = 1.0

# Everything outside this set is stripped before any text reaches a shell
# or AppleScript string: quotes, backslashes, $, backticks, semicolons,
# parentheses, percent signs, and all non-ASCII. What survives cannot
# break double quotes in an osascript -e argument nor single quotes in the
# remote ssh command string.
_ALLOWED_TEXT_RE = re.compile(r"[^A-Za-z0-9._ #/@:+-]")

_TITLE_LIMIT = 120
_PLATFORM_LIMIT = 40

_FALLBACK_TITLE = "Hermes"


def sanitize_notify_text(value: object, limit: int = _TITLE_LIMIT) -> str:
    """Return *value* reduced to the notification-safe character set.

    Keeps only ``[A-Za-z0-9._ #/@:+-]``, strips surrounding whitespace, and
    truncates to *limit*. Used for every variable piece of a notification
    (title, platform) so no quoting context downstream — AppleScript string
    literals, the remote ssh command line, a user command override — can
    ever see a metacharacter.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    cleaned = _ALLOWED_TEXT_RE.sub("", value).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip()
    return cleaned


def resolve_session_title(session_id: object) -> str:
    """Best-effort chat/session title for *session_id* from ``state.db``.

    Prefers ``sessions.display_name``, then ``sessions.title``, then
    ``"Hermes"``. Opens the database strictly read-only (``mode=ro``) with
    a 1-second timeout so it is safe against a live writer, and never
    raises: any failure (missing db, missing row, lock timeout) resolves
    to the fallback title.
    """
    if not session_id:
        return _FALLBACK_TITLE
    try:
        from hermes_cli.sqlite_safe_read import connect_tracked

        db_path = get_hermes_home() / "state.db"
        conn = connect_tracked(
            f"file:{db_path}?mode=ro",
            tracking_path=db_path,
            uri=True,
            timeout=_DB_TIMEOUT_SECONDS,
        )
        try:
            row = conn.execute(
                "SELECT display_name, title FROM sessions WHERE id = ?",
                (str(session_id),),
            ).fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return _FALLBACK_TITLE
    if not row:
        return _FALLBACK_TITLE
    for candidate in row:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return _FALLBACK_TITLE


def _applescript(title: str, subtitle: str, body: str) -> str:
    """Build the ``display notification`` AppleScript source.

    Only safe to interpolate because every variable fragment passed in has
    been through :func:`sanitize_notify_text` — the fixed literals carry no
    quotes either — so the double-quoted AppleScript strings cannot be
    escaped out of.
    """
    return (
        f'display notification "{body}" '
        f'with title "{title}" subtitle "{subtitle}"'
    )


def _run_quiet(argv: list[str], *, timeout: float = _SUBPROCESS_TIMEOUT_SECONDS) -> None:
    """Run *argv* to completion, swallowing every possible failure.

    Output goes to DEVNULL (a notification helper has nothing to say to
    the agent's stdout), the timeout is hard, and no exception — spawn
    failure, timeout, kill failure — ever escapes.
    """
    try:
        subprocess.run(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            creationflags=windows_hide_flags(),
        )
    except Exception as exc:
        logger.debug("os_notify: command %r failed: %s", argv[:1], exc)


def _run_command_override(command: str, *, title: str, subtitle: str,
                          body: str, platform: str,
                          session_id: str) -> None:
    """Run the user's ``notify_on_complete_command`` as a shell command.

    The notification fields are exported as ``HERMES_NOTIFY_*`` environment
    variables so the command can compose its own toast (or route it
    anywhere else — ntfy, a phone push gateway, Slack) without Hermes
    guessing at a quoting convention.
    """
    env = dict(os.environ)
    env.update({
        "HERMES_NOTIFY_TITLE": title,
        "HERMES_NOTIFY_SUBTITLE": subtitle,
        "HERMES_NOTIFY_BODY": body,
        "HERMES_NOTIFY_PLATFORM": platform,
        "HERMES_NOTIFY_SESSION_ID": session_id,
    })
    try:
        subprocess.run(
            command,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
            env=env,
            creationflags=windows_hide_flags(),
        )
    except Exception as exc:
        logger.debug("os_notify: command override failed: %s", exc)


def _notify_local(title: str, subtitle: str, body: str) -> None:
    """Dispatch on the machine Hermes itself runs on."""
    if sys.platform == "darwin":
        _run_quiet(["osascript", "-e", _applescript(title, subtitle, body)])
        return
    if sys.platform == "win32":
        # A reliable Windows toast needs either the BurntToast PowerShell
        # module or a Win32 NotifyIcon with its own message pump — too much
        # machinery for a fire-and-forget finalizer. Windows users can set
        # ``display.notify_on_complete_command`` instead.
        logger.debug(
            "os_notify: no built-in Windows dispatcher; set "
            "display.notify_on_complete_command"
        )
        return
    # Linux / BSD: notify-send when a notification daemon is present.
    if shutil.which("notify-send"):
        _run_quiet(["notify-send", title, body])
        return
    logger.debug("os_notify: notify-send not found; notification skipped")


def notify_session_complete(
    *,
    session_id: str = "",
    platform: str = "",
    interrupted: bool = False,
    completed: bool = False,
    ssh_target: str = "",
    command: str = "",
    title: str = "",
) -> None:
    """Pop the turn-finished OS notification. Never raises.

    Called from ``agent/turn_finalizer.py`` at the end of every turn when
    ``display.notify_on_complete`` is enabled.

    - ``interrupted`` turns stay silent (the user is already looking).
    - ``command`` (``display.notify_on_complete_command``) wins over the
      built-in dispatchers and runs as a shell command with the
      ``HERMES_NOTIFY_*`` fields in its environment.
    - ``ssh_target`` (``display.notify_on_complete_ssh``) runs the
      AppleScript on that host — how a Linux gateway notifies the user's
      Mac. The remote command is passed to ssh as ONE argument (ssh joins
      its arguments with spaces and hands the result to the remote shell,
      so ``osascript -e`` must not be split into local args).
    - otherwise the local platform dispatcher runs (osascript on Darwin,
      notify-send on Linux when available).
    - ``title`` may be supplied by a caller that already resolved it; it
      is sanitized here either way.
    """
    if interrupted:
        return

    raw_title = title if title else resolve_session_title(session_id)
    safe_title = sanitize_notify_text(raw_title) or _FALLBACK_TITLE
    safe_platform = sanitize_notify_text(platform, _PLATFORM_LIMIT)
    body = f"Session finished · {safe_platform}" if safe_platform else "Session finished"
    subtitle = "Task finished"
    session_id = str(session_id or "")

    if command:
        _run_command_override(
            command,
            title=safe_title,
            subtitle=subtitle,
            body=body,
            platform=safe_platform,
            session_id=session_id,
        )
        return

    if ssh_target:
        # Single quoted argument: the sanitized text cannot contain a
        # single quote, so the remote shell sees exactly one -e string.
        remote = f"osascript -e '{_applescript(safe_title, subtitle, body)}'"
        _run_quiet(
            [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=6",
                ssh_target,
                remote,
            ]
        )
        return

    _notify_local(safe_title, subtitle, body)
