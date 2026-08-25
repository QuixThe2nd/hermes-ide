#!/usr/bin/env python3
"""First-party Claude Code Remote Control lane for ``delegate_claude_agent``.

``delegate_claude_agent(remote_control=True)`` runs the delegated task on the
locally installed *bare* Claude Code CLI in interactive mode with Remote
Control enabled, so the user can watch (and steer) the run live at
``https://claude.ai/code`` or in the Claude mobile app while it executes.

Why a separate lane
-------------------
The default ``delegate_claude_agent`` path runs the ``claude-glm`` /
``claude-kimi`` wrapper scripts.  Those wrappers ``execve`` the real Claude
binary with ``ANTHROPIC_AUTH_TOKEN`` / ``ANTHROPIC_BASE_URL`` pointed at a
third-party coding plan, and Anthropic Remote Control is unavailable with a
custom base URL, API-key auth, Bedrock, Vertex or Foundry.  Remote Control
therefore *cannot* work through the wrapper lanes: this lane never invokes
them and never falls back to them.

Print mode (``-p``) is equally unusable here: it produces an
``entrypoint=sdk-cli`` transcript and publishes no Remote Control URL.  So
this lane always spawns a real interactive PTY session.

Execution model
---------------
``claude --session-id <uuid> --no-chrome --remote-control=<name>
--permission-mode <mode> --allowedTools <tools> <prompt>``

* The CLI publishes an account-bound ``https://claude.ai/code/session_…`` URL
  into the PTY at startup.  That URL is the only thing PTY text is used for —
  the PTY is *startup transport and liveness*, never the final report.
* The CLI writes the authoritative event stream to
  ``~/.claude/projects/<encoded-cwd>/<uuid>.jsonl``.  Completion is correlated
  solely through the UUID we generated and that exact file; TUI screen text is
  never parsed as a result.
* The interactive process stays alive after the turn ends, so once the final
  report is captured the runner stops it (TERM, then bounded KILL) and reaps
  the whole process group.  The web session is live for the whole tool call
  and may remain in the caller's account history afterwards.

Failure posture
---------------
Fail closed, always pre-spawn or fully cleaned up:

* POSIX-only; no PTY (or Windows) → typed failure, never a headless reroute.
* Model names requesting GLM/Kimi → rejected (they name another provider).
* Inherited custom-provider env (base URL / API key / Bedrock / Vertex /
  Foundry) → rejected, not silently stripped: deleting it would silently move
  the run onto a different account's billing.
* ``claude auth status`` must report first-party claude.ai OAuth.
* No strict Remote Control URL within the startup window → typed failure and
  full process-group cleanup.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import threading
import time
import uuid as uuid_module
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home
from tools.ansi_strip import strip_ansi

logger = logging.getLogger(__name__)

# ── Platform capability ───────────────────────────────────────────────────
#
# The lane needs a real POSIX pseudo-terminal.  Import the POSIX-only stdlib
# modules behind a guard so the module still imports (and can explain itself)
# on Windows, where the lane must fail closed rather than degrade.
try:
    import fcntl  # noqa: F401
    import pty
    import select  # noqa: F401
    import termios  # noqa: F401

    _POSIX_PTY_IMPORTS_OK = True
except ImportError:  # pragma: no cover - native Windows
    pty = None  # type: ignore[assignment]
    _POSIX_PTY_IMPORTS_OK = False

#: Process-group signalling is POSIX-only; keep a KILL that exists everywhere.
_KILLPG_SUPPORTED = hasattr(os, "killpg")
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)
_SIGTERM = getattr(signal, "SIGTERM", signal.SIGKILL)


def remote_control_platform_supported() -> bool:
    """True only on a POSIX host with a working pseudo-terminal stack."""
    return os.name == "posix" and _POSIX_PTY_IMPORTS_OK and _KILLPG_SUPPORTED


# ── Typed failures ────────────────────────────────────────────────────────


class RemoteControlError(Exception):
    """Base for every Remote Control lane failure.

    Each subclass carries a stable ``code`` so callers (and tests) can tell
    the failure classes apart without string-matching the message.
    """

    code = "remote_control_error"


class RemoteControlUnsupportedPlatform(RemoteControlError):
    """Windows / no POSIX PTY.  Never falls back to a headless run."""

    code = "unsupported_platform"


class RemoteControlLaneConflict(RemoteControlError):
    """The request names another provider's lane (GLM/Kimi model)."""

    code = "lane_conflict"


class RemoteControlProviderConflict(RemoteControlError):
    """Inherited env selects a custom provider; it is not stripped."""

    code = "provider_conflict"


class RemoteControlBinaryError(RemoteControlError):
    """No genuine bare ``claude`` executable resolved."""

    code = "binary_unavailable"


class RemoteControlAuthError(RemoteControlError):
    """``claude auth status`` did not report first-party claude.ai OAuth."""

    code = "auth_not_first_party"


class RemoteControlStartupError(RemoteControlError):
    """The CLI never published a strict Remote Control URL in the window."""

    code = "no_progress_url"


class RemoteControlRunError(RemoteControlError):
    """The run ended without a usable terminal report (timeout/stall/empty)."""

    code = "run_incomplete"


# ── Tunables ──────────────────────────────────────────────────────────────

#: Bounded wall-clock window for the CLI to publish the Remote Control URL.
STARTUP_URL_TIMEOUT_SECONDS = 120.0
#: No PTY bytes *and* no transcript growth for this long → stalled.
STALL_WATCHDOG_SECONDS = 600.0
#: How often the monitor loop re-checks everything.
MONITOR_POLL_SECONDS = 0.1

_TERM_GRACE_SECONDS = 3.0
_KILL_GRACE_SECONDS = 3.0
_REAP_POLL_SECONDS = 0.05

#: PTY byte budgets — a TUI repaints constantly, so its raw output is noise.
_PTY_BUFFER_BYTES = 256 * 1024
_PTY_LOG_BYTES = 2 * 1024 * 1024

#: Transcript budgets.
_TRANSCRIPT_MAX_BYTES = 64 * 1024 * 1024
_TRANSCRIPT_MAX_LINE_BYTES = 8 * 1024 * 1024
#: How long we tolerate the transcript not existing yet after spawn.
_TRANSCRIPT_APPEAR_GRACE_SECONDS = 60.0

#: Terminal geometry handed to the child so the TUI gets a real size.
_PTY_ROWS = 40
_PTY_COLS = 160

_AUTH_PREFLIGHT_TIMEOUT_SECONDS = 60.0

#: Session name shown in the Claude Code web/mobile session list.
SESSION_NAME_PREFIX = "Hermes:"


# ── Remote Control progress URL ───────────────────────────────────────────

#: The exact published shape: ``https://claude.ai/code/session_<safe-id>``.
#: The id is opaque to us; we only require it to be a bounded URL-safe token.
_REMOTE_CONTROL_URL_RE = re.compile(
    r"\Ahttps://claude\.ai/code/session_[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z"
)
#: Candidate URL tokens in ANSI-stripped PTY text.  Matching stops at any
#: character that cannot appear inside a published URL, so a URL embedded as
#: somebody else's query parameter stays one (rejected) token.
_URL_TOKEN_RE = re.compile(r"https?://[^\s\"'<>\]\\]+")
#: Trailing punctuation a TUI may append to a bare URL.
_URL_TRAILING_PUNCT = ".,;:!?)]}'\"`"


def extract_progress_url(text: str) -> Optional[str]:
    """Return the Remote Control URL published in ``text``, or None.

    ``text`` is ANSI-stripped PTY output.  Every ``http(s)://`` token is
    extracted and then validated *as a whole* against the strict published
    shape, so a wrong scheme, host, path, query string, malformed session id,
    or a ``claude.ai`` URL nested inside another host's URL is rejected rather
    than partially matched.
    """
    for token in _URL_TOKEN_RE.findall(text or ""):
        candidate = token.rstrip(_URL_TRAILING_PUNCT)
        if _REMOTE_CONTROL_URL_RE.match(candidate):
            return candidate
    return None


def build_session_name(workdir: str) -> str:
    """Concise deterministic Remote Control session name for ``workdir``."""
    base = str(workdir or "").rstrip("/") or "/"
    return f"{SESSION_NAME_PREFIX} {os.path.basename(base) or base}"


# ── Native (bare) Claude binary resolution ────────────────────────────────

#: Names that mean "a provider wrapper", never a bare first-party CLI.
_WRAPPER_BASENAMES = frozenset({"claude-glm", "claude-kimi"})


def _local_bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _is_wrapper_path(path: Path) -> bool:
    """True when ``path`` (or its symlink target) is a GLM/Kimi wrapper.

    Both ends are checked: a file literally named ``claude-glm`` is rejected,
    and so is a ``claude`` symlink that really points at one.
    """
    names = [path.name]
    try:
        names.append(path.resolve().name)
    except OSError:
        pass
    return any(name in _WRAPPER_BASENAMES for name in names)


def resolve_native_claude_binary() -> Optional[str]:
    """Resolve a genuine bare ``claude`` executable, or None.

    Search order (this fork's local-shim convention, then PATH):

    1. ``~/.local/bin/claude``
    2. ``claude`` on ``PATH``

    Deliberately no new environment variable: the existing wrapper overrides
    (``CLAUDE_GLM_BIN`` / ``CLAUDE_KIMI_BIN``) name *other providers* and must
    not be able to steer this lane.  Any candidate that is — or points at —
    ``claude-glm`` / ``claude-kimi`` is rejected.
    """
    candidates: List[Path] = [_local_bin_dir() / "claude"]
    try:
        found = shutil.which("claude")
    except OSError:  # pragma: no cover - broken PATH
        found = None
    if found:
        candidates.append(Path(found))

    for candidate in candidates:
        if candidate.name != "claude":
            continue
        if _is_wrapper_path(candidate):
            continue
        if _is_executable_file(candidate):
            return str(candidate)
    return None


# ── Provider-environment preflight ────────────────────────────────────────

#: Inherited env that would move the run off first-party claude.ai OAuth.
#: Remote Control is unavailable with any of these configured, and silently
#: deleting them would change whose account pays for the run — so they are
#: reported, never stripped.
_FORBIDDEN_PROVIDER_ENV: Dict[str, str] = {
    "ANTHROPIC_BASE_URL": "a custom ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN": "an ANTHROPIC_AUTH_TOKEN (wrapper/relay credential)",
    "ANTHROPIC_API_KEY": "API-key auth (ANTHROPIC_API_KEY)",
    "ANTHROPIC_CUSTOM_HEADERS": "custom Anthropic headers",
    "ANTHROPIC_BEDROCK_BASE_URL": "Amazon Bedrock",
    "CLAUDE_CODE_USE_BEDROCK": "Amazon Bedrock",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "Amazon Bedrock",
    "CLAUDE_CODE_USE_VERTEX": "Google Vertex",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH": "Google Vertex",
    "ANTHROPIC_VERTEX_PROJECT_ID": "Google Vertex",
    "CLOUD_ML_REGION": "Google Vertex",
    "CLAUDE_CODE_USE_FOUNDRY": "Microsoft Foundry",
    "ANTHROPY_FOUNDRY_BASE_URL": "Microsoft Foundry",
}


def find_conflicting_provider_env(env: Dict[str, str]) -> List[str]:
    """Return human labels for every custom-provider var set in ``env``."""
    conflicts: List[str] = []
    for name, label in _FORBIDDEN_PROVIDER_ENV.items():
        if str(env.get(name, "")).strip():
            conflicts.append(f"{label} ({name})")
    return conflicts


# ── Auth preflight ────────────────────────────────────────────────────────

#: The only auth facts this lane may retain or emit.  ``claude auth status
#: --json`` emits exactly these three fields; anything else the CLI adds
#: later (email, organization, token material) is dropped here so it can never
#: reach the result payload or the run log.
_AUTH_SUMMARY_FIELDS = ("loggedIn", "authMethod", "apiProvider")

_AUTH_REQUIRED: Dict[str, Any] = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
}


def _parse_auth_payload(stdout: str) -> Optional[Dict[str, Any]]:
    """Parse the machine-readable ``claude auth status`` payload.

    Tolerates leading non-JSON noise by taking the outermost JSON object, and
    returns None when nothing parses.
    """
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return parsed if isinstance(parsed, dict) else None


def summarize_auth_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Project an auth payload down to the three non-sensitive fields."""
    return {field: payload.get(field) for field in _AUTH_SUMMARY_FIELDS}


def auth_status_problems(summary: Dict[str, Any]) -> List[str]:
    """Return the reasons ``summary`` is not first-party claude.ai OAuth."""
    problems: List[str] = []
    for field, expected in _AUTH_REQUIRED.items():
        actual = summary.get(field)
        if actual != expected:
            problems.append(f"{field}={actual!r} (need {expected!r})")
    return problems


def run_auth_preflight(binary: str, env: Dict[str, str]) -> Dict[str, Any]:
    """Run ``claude auth status --json`` and enforce first-party OAuth.

    Returns the sanitized three-field summary.  Raises
    :class:`RemoteControlAuthError` on a non-zero exit or any mismatch.  The
    raw payload and stderr are never surfaced — they can carry account data.
    """
    try:
        proc = subprocess.run(
            [binary, "auth", "status", "--json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_AUTH_PREFLIGHT_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteControlAuthError(
            f"`claude auth status` did not answer within "
            f"{int(_AUTH_PREFLIGHT_TIMEOUT_SECONDS)}s"
        ) from exc
    except OSError as exc:
        raise RemoteControlAuthError(
            f"could not run `claude auth status`: {exc}"
        ) from exc

    payload = _parse_auth_payload(proc.stdout)
    if payload is None:
        raise RemoteControlAuthError(
            "`claude auth status` did not return machine-readable JSON "
            f"(exit {proc.returncode})"
        )

    summary = summarize_auth_status(payload)
    problems = auth_status_problems(summary)
    if proc.returncode != 0:
        problems.append(f"exit_code={proc.returncode}")
    if problems:
        raise RemoteControlAuthError(
            "Remote Control needs first-party claude.ai OAuth; "
            "`claude auth status` reports " + "; ".join(problems)
        )
    return summary


# ── Transcript path correlation ───────────────────────────────────────────


def claude_projects_root(home: Optional[Path] = None) -> Path:
    """Claude Code's per-host projects root (``~/.claude/projects``)."""
    base = Path(home) if home is not None else Path.home()
    return base / ".claude" / "projects"


def encode_claude_cwd(cwd: str) -> str:
    """Encode a cwd the way Claude Code names its project directories.

    ``/root/.hermes/x`` → ``-root--hermes-x``: every ``/`` and ``.`` becomes
    ``-``.  This is the encoding observed in real ``~/.claude/projects`` trees
    and assumed by ``hermes_cli.foreign_sessions``.
    """
    return str(cwd).replace("/", "-").replace(".", "-")


def expected_transcript_path(
    session_id: str, cwd: str, *, projects_root: Optional[Path] = None
) -> Path:
    """Exact transcript path for a generated session id in ``cwd``."""
    root = claude_projects_root() if projects_root is None else Path(projects_root)
    return root / encode_claude_cwd(cwd) / f"{session_id}.jsonl"


def _is_within(child: Path, ancestor: Path) -> bool:
    try:
        child.relative_to(ancestor)
    except ValueError:
        return False
    return True


def validate_transcript_path(path: Path, *, projects_root: Path) -> Path:
    """Check ``path`` is a regular, non-symlink file inside ``projects_root``.

    Returns the resolved path.  Raises :class:`RemoteControlRunError` for a
    symlink, a non-regular file, an oversized file, or anything that escapes
    the projects root.
    """
    try:
        resolved_root = projects_root.resolve(strict=False)
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise RemoteControlRunError(f"transcript path is not usable: {exc}") from exc

    if not _is_within(resolved, resolved_root):
        raise RemoteControlRunError(
            f"transcript path escapes the Claude projects root: {path}"
        )
    if not resolved.exists():
        raise RemoteControlRunError(f"transcript not found: {path}")
    if path.is_symlink() or resolved.is_symlink():
        raise RemoteControlRunError(f"transcript is a symlink, refusing: {path}")
    try:
        info = resolved.stat()
    except OSError as exc:
        raise RemoteControlRunError(f"transcript is not readable: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise RemoteControlRunError(f"transcript is not a regular file: {path}")
    if info.st_size > _TRANSCRIPT_MAX_BYTES:
        raise RemoteControlRunError(
            f"transcript exceeds {_TRANSCRIPT_MAX_BYTES} bytes: {path}"
        )
    return resolved


# ── Transcript completion parsing ─────────────────────────────────────────


def _text_blocks(content: Any) -> List[str]:
    """Ordered non-empty ``text`` blocks from a Claude message ``content``."""
    if isinstance(content, str):
        return [content] if content.strip() else []
    if not isinstance(content, list):
        return []
    blocks: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            blocks.append(text)
    return blocks


def _is_injected_user_turn(event: Dict[str, Any]) -> bool:
    """True for the ``user`` event that carries our injected prompt.

    Skips Claude Code's own bookkeeping rows (sidechains, meta records) and
    the rows where it echoes a tool result back into the conversation.
    """
    if event.get("isSidechain") or event.get("isMeta"):
        return False
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    return bool(_text_blocks(content))


def _is_root_assistant(event: Dict[str, Any]) -> bool:
    """True for a root (non-sidechain, non-meta) assistant record."""
    if event.get("isSidechain") or event.get("isMeta"):
        return False
    message = event.get("message")
    return isinstance(message, dict) and message.get("role") == "assistant"


def _session_identity_problem(event: Dict[str, Any], session_id: str) -> Optional[str]:
    """Return a reason ``event`` is not from our session, or None."""
    claimed = event.get("sessionId")
    if claimed is not None and claimed != session_id:
        return f"sessionId={claimed!r}"
    return None


def parse_transcript_report(
    events: List[Dict[str, Any]], session_id: str
) -> Optional[Dict[str, Any]]:
    """Extract the terminal report from ordered transcript events.

    Only root ``assistant`` records *after* the injected user turn count.  A
    record completes the run only when it carries ``stop_reason == "end_turn"``
    **and** at least one non-empty text block — Claude emits empty ``end_turn``
    rows (thinking-only, or trailing bookkeeping) that must not be mistaken
    for the answer.  Tool-use and intermediate records never complete a run.

    Returns ``None`` until such a record exists.
    """
    prompt_index: Optional[int] = None
    for index, event in enumerate(events):
        if event.get("type") != "user":
            continue
        if not _is_injected_user_turn(event):
            continue
        identity = _session_identity_problem(event, session_id)
        if identity:
            raise RemoteControlRunError(
                f"transcript identity mismatch on the injected turn: {identity}"
            )
        prompt_index = index
        break
    if prompt_index is None:
        # The CLI has not recorded our prompt yet — the turn has not started.
        return None

    models: List[str] = []
    for event in events[prompt_index + 1 :]:
        if event.get("type") != "assistant":
            continue
        identity = _session_identity_problem(event, session_id)
        if identity:
            raise RemoteControlRunError(f"transcript identity mismatch: {identity}")
        if not _is_root_assistant(event):
            continue
        message = event["message"]
        model = message.get("model")
        if isinstance(model, str) and model and model not in models:
            models.append(model)
        if message.get("stop_reason") != "end_turn":
            continue
        blocks = _text_blocks(message.get("content"))
        if not blocks:
            # An empty end_turn record can precede the real one; keep waiting.
            continue
        return {
            "final_report": "\n\n".join(blocks).strip(),
            "final_text_blocks": blocks,
            "models_used": models,
        }
    return None


# ── Incremental transcript reader ─────────────────────────────────────────


class TranscriptWatcher:
    """Incrementally tails the exact JSONL file for one generated session.

    Owns the four properties a naive "read the whole file" loop gets wrong:

    * **Delayed creation** — Claude creates the file some time after spawn.
      ``poll`` returns nothing until it appears (bounded by
      ``appear_deadline``, after which ``late`` flips so the caller can name
      the problem), and only then starts the real tail.
    * **Partial trailing line** — bytes after the last newline are held back
      until a newline completes them, so a half-written JSON record is never
      parsed.
    * **Malformed lines** — a complete-but-invalid line is skipped, not fatal:
      Claude appends non-conversational records we do not model.
    * **Budgets** — file size and per-line size are capped, and each poll only
      reads what is new.
    """

    def __init__(
        self,
        path: Path,
        session_id: str,
        *,
        projects_root: Path,
        appear_deadline: float,
    ) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self.projects_root = Path(projects_root)
        self.appear_deadline = appear_deadline
        self._offset = 0
        self._partial = ""
        self._events: List[Dict[str, Any]] = []
        self._last_growth_mono = time.monotonic()
        self._appeared = False

    @property
    def appeared(self) -> bool:
        return self._appeared

    @property
    def late(self) -> bool:
        """True past the creation grace window with no transcript in place."""
        return not self._appeared and time.monotonic() > self.appear_deadline

    @property
    def last_activity_mono(self) -> float:
        return self._last_growth_mono

    @property
    def events(self) -> List[Dict[str, Any]]:
        return self._events

    def poll(self) -> List[Dict[str, Any]]:
        """Read newly appended bytes; return the events completed this poll."""
        if not self._appeared:
            try:
                if not self.path.exists():
                    return []
            except OSError:
                return []
            self.path = validate_transcript_path(
                self.path, projects_root=self.projects_root
            )
            self._appeared = True

        try:
            info = self.path.stat()
        except OSError:
            return []
        if not stat.S_ISREG(info.st_mode):
            return []
        if info.st_size > _TRANSCRIPT_MAX_BYTES:
            raise RemoteControlRunError(
                f"transcript exceeds {_TRANSCRIPT_MAX_BYTES} bytes: {self.path}"
            )
        if info.st_size < self._offset:
            # Truncated/rotated underneath us — restart from the top rather
            # than silently reading garbage.
            self._offset = 0
            self._partial = ""
            self._events = []

        try:
            with open(self.path, "rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read(info.st_size - self._offset)
        except OSError:
            return []
        self._offset += len(chunk)
        if not chunk:
            return []

        self._last_growth_mono = time.monotonic()
        text = self._partial + chunk.decode("utf-8", errors="replace")

        # Hold back everything after the final newline: it is still arriving.
        cut = text.rfind("\n")
        if cut < 0:
            self._check_partial_budget(text)
            self._partial = text
            return []
        complete, self._partial = text[: cut + 1], text[cut + 1 :]
        self._check_partial_budget(self._partial)

        newly_read: List[Dict[str, Any]] = []
        for raw_line in complete.split("\n"):
            stripped = raw_line.strip()
            if not stripped:
                continue
            if len(stripped) > _TRANSCRIPT_MAX_LINE_BYTES:
                raise RemoteControlRunError(
                    f"transcript line exceeds {_TRANSCRIPT_MAX_LINE_BYTES} bytes"
                )
            try:
                event = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                # Complete-but-malformed records are skipped, not fatal.
                continue
            if isinstance(event, dict):
                self._events.append(event)
                newly_read.append(event)
        return newly_read

    @staticmethod
    def _check_partial_budget(partial: str) -> None:
        if len(partial) > _TRANSCRIPT_MAX_LINE_BYTES:
            raise RemoteControlRunError(
                f"transcript line exceeds {_TRANSCRIPT_MAX_LINE_BYTES} bytes"
            )

    def report(self) -> Optional[Dict[str, Any]]:
        return parse_transcript_report(self._events, self.session_id)


# ── PTY runner ────────────────────────────────────────────────────────────


class RemoteControlRun:
    """One Remote Control delegation: PTY child + transcript correlation.

    Deliberately **not** built on :func:`tools.agent_cli_runner.run_agent_cli`.
    That runner's contract is a pipe that ends when the process exits and a
    child that is dead once the work is done; this lane is the opposite — a
    PTY whose text is only startup transport, and an interactive child that is
    still alive when the work is finished.  Sharing it would mean weakening
    one of the two contracts, so this owns its own lifecycle.

    Lifecycle::

        run = RemoteControlRun(argv, ...)   # validation already done
        run.start()                         # pty.fork + execvpe
        url = run.await_progress_url()      # bounded startup window
        report = run.await_report()         # monitors PTY + transcript
        run.stop()                          # always, in a finally block
    """

    def __init__(
        self,
        argv: List[str],
        *,
        workdir: str,
        env: Dict[str, str],
        session_id: str,
        transcript_path: Path,
        projects_root: Path,
        timeout_seconds: int = 0,
        stall_watchdog_seconds: float = STALL_WATCHDOG_SECONDS,
        startup_timeout_seconds: float = STARTUP_URL_TIMEOUT_SECONDS,
        log_path: Optional[Path] = None,
        pty_log_path: Optional[Path] = None,
    ) -> None:
        self.argv = list(argv)
        self.workdir = workdir
        self.env = dict(env)
        self.session_id = session_id
        self.transcript_path = Path(transcript_path)
        self.projects_root = Path(projects_root)
        self.timeout_seconds = int(timeout_seconds)
        self.stall_watchdog_seconds = float(stall_watchdog_seconds)
        self.startup_timeout_seconds = float(startup_timeout_seconds)
        self.log_path = Path(log_path) if log_path else None
        self.pty_log_path = Path(pty_log_path) if pty_log_path else None

        self.pid: Optional[int] = None
        self.pgid: Optional[int] = None
        self._master_fd: Optional[int] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_done = threading.Event()
        self._raw_pty_tail = ""
        self._pty_bytes_logged = 0
        self._log_handle = None
        self._pty_log_handle = None
        self._last_pty_mono = time.monotonic()
        self._started_mono = time.monotonic()
        self.progress_url: Optional[str] = None
        self.exited = False
        self.returncode: Optional[int] = None

    # -- startup ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the CLI on a new PTY in its own session. No shell involved."""
        if not remote_control_platform_supported():
            raise RemoteControlUnsupportedPlatform(
                "Remote Control needs a POSIX pseudo-terminal; this host has none"
            )
        assert pty is not None  # for the type checker; guaranteed above

        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = open(self.log_path, "ab")
        if self.pty_log_path is not None:
            self.pty_log_path.parent.mkdir(parents=True, exist_ok=True)
            self._pty_log_handle = open(self.pty_log_path, "ab")

        self._started_mono = time.monotonic()
        pid, master_fd = pty.fork()

        if pid == 0:  # pragma: no cover - child process
            # Child: never let a Python exception escape into the parent.
            try:
                os.chdir(self.workdir)
                os.execvpe(self.argv[0], self.argv, self.env)
            except BaseException:
                os._exit(127)

        # Parent: pty.fork() made the child a session leader, so its process
        # group is its pid — that group is what we signal and later reap.
        self.pid = pid
        self.pgid = pid
        self._master_fd = master_fd

        try:
            import fcntl
            import termios

            fcntl.ioctl(
                master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", _PTY_ROWS, _PTY_COLS, 0, 0)
            )
        except (OSError, ImportError, AttributeError, struct.error):
            # A default-sized terminal is fine; the TUI will cope.
            pass

        self._reader_thread = threading.Thread(
            target=self._read_pty, name="claude-rc-pty", daemon=True
        )
        self._reader_thread.start()

    # -- run log ---------------------------------------------------------

    def _log_line(self, record: Dict[str, Any]) -> None:
        if self._log_handle is None:
            return
        try:
            self._log_handle.write(
                (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            )
            self._log_handle.flush()
        except (OSError, ValueError):
            pass

    def _read_pty(self) -> None:  # pragma: no cover - real I/O thread
        """Drain the PTY master: startup transport and liveness, nothing else."""
        import select

        fd = self._master_fd
        assert fd is not None
        try:
            while True:
                try:
                    ready, _, _ = select.select([fd], [], [], 0.2)
                except (OSError, ValueError):
                    break
                if not ready:
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                self._on_pty_bytes(chunk)
        finally:
            self._reader_done.set()

    def _on_pty_bytes(self, chunk: bytes) -> None:
        """Record PTY activity; the text is used for the URL and nothing else."""
        self._last_pty_mono = time.monotonic()
        if self._pty_log_handle is not None and self._pty_bytes_logged < _PTY_LOG_BYTES:
            budget = _PTY_LOG_BYTES - self._pty_bytes_logged
            self._pty_log_handle.write(chunk[:budget])
            self._pty_bytes_logged += min(len(chunk), budget)
            try:
                self._pty_log_handle.flush()
            except (OSError, ValueError):
                pass
        # Keep a bounded tail so a URL split across reads still matches and so
        # the buffer cannot grow without bound on a chatty TUI.
        combined = self._raw_pty_tail + chunk.decode("utf-8", errors="replace")
        self._raw_pty_tail = combined[-_PTY_BUFFER_BYTES:]

    def ansi_stripped_pty_text(self) -> str:
        """Bounded, ANSI-stripped view of the PTY tail (startup transport)."""
        return strip_ansi(self._raw_pty_tail)

    # -- phase 1: the published URL --------------------------------------

    def await_progress_url(self) -> str:
        """Block until the CLI publishes a strict Remote Control URL.

        Any PTY byte resets the idle clock, so this is a genuine wall-clock
        bound rather than a quiet-detector.
        """
        deadline = self._started_mono + self.startup_timeout_seconds
        while True:
            url = extract_progress_url(self.ansi_stripped_pty_text())
            if url:
                self.progress_url = url
                return url
            self._reap_if_dead()
            if self.exited:
                raise RemoteControlStartupError(
                    "Claude Code exited before publishing a Remote Control URL"
                )
            if time.monotonic() >= deadline:
                raise RemoteControlStartupError(
                    "Claude Code did not publish a Remote Control URL within "
                    f"{int(self.startup_timeout_seconds)}s — Remote Control is "
                    "unavailable with a custom provider, API-key auth, Bedrock, "
                    "Vertex or Foundry"
                )
            time.sleep(MONITOR_POLL_SECONDS)

    def _reap_if_dead(self) -> None:
        if self.pid is None or self.exited:
            return
        try:
            waited, status = os.waitpid(self.pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            self.exited = True
            self.returncode = None
            return
        if waited == self.pid:
            self.exited = True
            self.returncode = (
                os.waitstatus_to_exitcode(status)
                if hasattr(os, "waitstatus_to_exitcode")
                else status
            )

    # -- phase 2: the run itself -----------------------------------------

    def await_report(self) -> Dict[str, Any]:
        """Monitor the run until a terminal report, timeout, stall or exit.

        Completion comes only from the transcript; the PTY is liveness.
        """
        watcher = TranscriptWatcher(
            self.transcript_path,
            self.session_id,
            projects_root=self.projects_root,
            appear_deadline=time.monotonic() + _TRANSCRIPT_APPEAR_GRACE_SECONDS,
        )
        self._log_line(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "event": "progress_url",
                "url": self.progress_url,
                "session_id": self.session_id,
            }
        )

        hard_deadline = (
            self._started_mono + self.timeout_seconds if self.timeout_seconds > 0 else None
        )

        while True:
            for event in watcher.poll():
                self._log_line(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "event": "transcript_record",
                        "type": event.get("type"),
                    }
                )

            report = watcher.report()
            if report is not None:
                self._log_line(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "event": "final_report",
                        "chars": len(report["final_report"]),
                        "models": report["models_used"],
                    }
                )
                return report

            self._reap_if_dead()
            if self.exited:
                if self._drain_after_exit(watcher):
                    return watcher.report()
                raise RemoteControlRunError(
                    "Claude Code exited without a terminal assistant report "
                    f"(exit {self.returncode!r})"
                )

            if watcher.late:
                raise RemoteControlRunError(
                    "Claude Code created no session transcript at "
                    f"{self.transcript_path} within "
                    f"{int(_TRANSCRIPT_APPEAR_GRACE_SECONDS)}s"
                )

            if _check_interrupted():
                raise RemoteControlRunError("interrupted")

            now = time.monotonic()
            if hard_deadline is not None and now >= hard_deadline:
                raise RemoteControlRunError("timeout")
            last_activity = max(self._last_pty_mono, watcher.last_activity_mono)
            if now - last_activity >= self.stall_watchdog_seconds:
                raise RemoteControlRunError(
                    "stalled: no PTY or transcript activity for "
                    f"{int(self.stall_watchdog_seconds)}s"
                )
            time.sleep(MONITOR_POLL_SECONDS)

    @staticmethod
    def _drain_after_exit(watcher: TranscriptWatcher) -> bool:
        """Give a just-exited CLI a moment to flush its last transcript lines."""
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            watcher.poll()
            if watcher.report() is not None:
                return True
            time.sleep(0.05)
        return False

    # -- teardown --------------------------------------------------------

    def stop(self) -> None:
        """Stop and reap the child and its whole process group.

        The interactive CLI is *still alive* after the final report by design,
        so this runs on every path — success, failure, timeout and interrupt
        alike.  Escalates TERM → bounded KILL, then waits for the group to be
        empty so no descendant outlives the tool call even when the leader
        died first.
        """
        try:
            self._signal_group(_SIGTERM)
            if not self._wait_leader_gone(_TERM_GRACE_SECONDS):
                self._signal_group(_SIGKILL)
                self._wait_leader_gone(_KILL_GRACE_SECONDS)
            # Kill whatever the leader left behind (its own children), then
            # wait for the group to actually disappear.
            self._signal_group(_SIGKILL)
            self._wait_group_empty(_KILL_GRACE_SECONDS)
        finally:
            self._close_pty()
            self._join_reader()
            for handle in (self._log_handle, self._pty_log_handle):
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:
                        pass
            self._log_handle = None
            self._pty_log_handle = None

    def _signal_group(self, sig: int) -> None:
        if self.pgid is None or not _KILLPG_SUPPORTED:
            return
        try:
            # windows-footgun: ok — guarded by _KILLPG_SUPPORTED plus the
            # remote_control_platform_supported() gate that fail-closes the lane.
            os.killpg(self.pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _wait_leader_gone(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            self._reap_if_dead()
            if self.exited:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_REAP_POLL_SECONDS)

    def _wait_group_empty(self, timeout: float) -> None:
        if self.pgid is None or not _KILLPG_SUPPORTED:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                # windows-footgun: ok — guarded by _KILLPG_SUPPORTED plus the
                # POSIX platform gate that fail-closes the lane.
                os.killpg(self.pgid, 0)
            except (ProcessLookupError, OSError):
                return
            time.sleep(_REAP_POLL_SECONDS)

    def _close_pty(self) -> None:
        fd = self._master_fd
        self._master_fd = None
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass

    def _join_reader(self) -> None:
        thread = self._reader_thread
        self._reader_thread = None
        if thread is None:
            return
        self._reader_done.wait(timeout=_TERM_GRACE_SECONDS + 1.0)
        if thread.is_alive():  # pragma: no cover - only on a wedged reader
            thread.join(timeout=_TERM_GRACE_SECONDS)


def _check_interrupted() -> bool:
    try:
        from tools.interrupt import is_interrupted

        return is_interrupted()
    except Exception:  # pragma: no cover - defensive
        return False


# ── Child environment ─────────────────────────────────────────────────────


def build_remote_control_env(*, term: str = "xterm-256color") -> Tuple[Dict[str, str], List[str]]:
    """Build the child env for the Remote Control lane.

    Returns ``(env, conflicts)``.  ``conflicts`` is non-empty when inherited
    variables select a custom provider; the caller must reject the run rather
    than strip them, because deleting them would silently change whose
    account pays for the run.
    """
    from tools.environments.local import build_subprocess_env

    env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=False)
    if not env.get("HOME"):
        env["HOME"] = str(Path.home())
    # Same PATH guarantee as the default lane: ~/.local/bin is prepended so
    # resolution stays consistent when PATH is minimal.
    local_bin = str(Path.home() / ".local" / "bin")
    env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    # A TUI has to believe it has a real terminal.
    env["TERM"] = term

    return env, find_conflicting_provider_env(env)


# ── Lane selection ────────────────────────────────────────────────────────

#: Model markers that name the wrapper lanes' providers.  A model string
#: containing one of these is a request for GLM or Kimi, and Remote Control is
#: first-party Claude — the two cannot be combined.
_INCOMPATIBLE_MODEL_MARKERS = ("glm", "kimi", "z.ai", "zai", "moonshot")

#: Model selectors that are genuinely first-party Claude Code models.  A
#: supplied model is forwarded only when it matches one of these; anything
#: else falls back to the native CLI default, because ``remote_control=True``
#: is the explicit provider-changing opt-in and an unknown selector would make
#: the CLI silently run a different model than the caller named.
_FIRST_PARTY_MODEL_PREFIXES = ("claude", "sonnet", "opus", "haiku")


def incompatible_model_reason(model: str | None) -> Optional[str]:
    """Return why ``model`` cannot run on the Remote Control lane, or None."""
    name = str(model or "").strip().lower()
    if not name:
        return None
    for marker in _INCOMPATIBLE_MODEL_MARKERS:
        if marker not in name:
            continue
        provider = "Kimi" if marker == "kimi" else "GLM"
        return (
            f"model {model!r} requests the {provider} wrapper lane; Remote "
            "Control runs only on the locally authenticated first-party "
            "Claude subscription. Re-run without remote_control for that model."
        )
    return None


def normalize_first_party_model(model: str | None) -> Optional[str]:
    """Return the model selector to forward, or None for the native default.

    An omitted model correctly means "whatever the CLI is logged in as", since
    ``remote_control=True`` is the explicit opt-in to first-party Claude Code.
    """
    name = str(model or "").strip()
    if not name:
        return None
    lowered = name.lower()
    if any(lowered.startswith(prefix) for prefix in _FIRST_PARTY_MODEL_PREFIXES):
        return name
    return None


def build_remote_control_argv(
    binary: str,
    *,
    session_id: str,
    session_name: str,
    prompt: str,
    permission_mode: str,
    allowed_tools: str,
    model: str | None = None,
) -> List[str]:
    """Argv for the bare first-party CLI. Never ``-p``, never a wrapper.

    Order matches the verified live invocation:
    ``claude --session-id <uuid> --no-chrome --remote-control=<name>
    --permission-mode <mode> --allowedTools <tools> <prompt>``.
    """
    argv = [
        binary,
        "--session-id",
        session_id,
        "--no-chrome",
        f"--remote-control={session_name}",
        "--permission-mode",
        permission_mode,
        "--allowedTools",
        allowed_tools,
    ]
    selector = normalize_first_party_model(model)
    if selector:
        argv.extend(["--model", selector])
    argv.append(prompt)
    return argv


# ── Lane entry point ──────────────────────────────────────────────────────


def _result_fields() -> Dict[str, Any]:
    """The delegate result shape, shared with the default lane."""
    return {
        "success": False,
        "error": None,
        "final_report": "",
        "session_id": None,
        "duration_seconds": 0.0,
        "num_turns": None,
        "cost_usd": None,
        "models_used": [],
        "permission_denials": [],
        "log_path": None,
        "warnings": [],
        "progress_url": None,
        "remote_control": None,
    }


def run_remote_control_delegation(
    *,
    task: str,
    workdir: str,
    model: str | None = None,
    timeout_seconds: int = 0,
    allowed_tools: str = "Read,Write,Edit,Glob,Grep,Bash",
    permission_mode: str = "acceptEdits",
    stall_watchdog_seconds: float = STALL_WATCHDOG_SECONDS,
    log_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Execute one Remote Control delegation; return the delegate result fields.

    Never raises past the pre-spawn checks: every failure comes back as
    ``success=False`` with a message, having either spawned nothing at all or
    fully reaped what it spawned.  ``success`` is True only when a non-empty
    terminal report was captured from the transcript.
    """
    from tools.claude_agent_tool import _clamp_timeout_seconds

    fields = _result_fields()
    metadata: Dict[str, Any] = {"enabled": True}

    def _fail(exc: RemoteControlError) -> Dict[str, Any]:
        fields["success"] = False
        fields["error"] = str(exc)
        metadata["code"] = exc.code
        fields["remote_control"] = metadata
        return fields

    try:
        if not remote_control_platform_supported():
            raise RemoteControlUnsupportedPlatform(
                "remote_control=True needs a POSIX pseudo-terminal (PTY); it is "
                "unsupported on this platform and will not fall back to a "
                "headless run"
            )

        conflict = incompatible_model_reason(model)
        if conflict:
            raise RemoteControlLaneConflict(conflict)

        binary = resolve_native_claude_binary()
        if not binary:
            raise RemoteControlBinaryError(
                "bare `claude` executable not found for remote_control=True. "
                "Install Claude Code (e.g. ~/.local/bin/claude or on PATH) and "
                "run `claude auth login`. The claude-glm / claude-kimi wrappers "
                "cannot serve Remote Control."
            )

        env, env_conflicts = build_remote_control_env()
        if env_conflicts:
            raise RemoteControlProviderConflict(
                "remote_control=True cannot run while the environment selects a "
                "custom provider: "
                + "; ".join(env_conflicts)
                + ". Remote Control is available only on first-party claude.ai "
                "OAuth; unset these and run `claude auth login`."
            )

        # Claude names the project directory after its own cwd, which is the
        # resolved path — resolve before encoding so correlation is exact.
        resolved_workdir = str(Path(workdir).resolve())
        auth_summary = run_auth_preflight(binary, env)
        clamped_timeout = _clamp_timeout_seconds(timeout_seconds)

        session_id = str(uuid_module.uuid4())
        session_name = build_session_name(resolved_workdir)
        prompt = str(task).strip()
        transcript_path = expected_transcript_path(session_id, resolved_workdir)
        argv = build_remote_control_argv(
            binary,
            session_id=session_id,
            session_name=session_name,
            prompt=prompt,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            model=model,
        )

        metadata.update(
            {
                "session_name": session_name,
                "session_uuid": session_id,
                "transcript_path": str(transcript_path),
                "auth": auth_summary,
            }
        )
        fields["session_id"] = session_id
        fields["remote_control"] = metadata

        directory = Path(log_dir) if log_dir else get_hermes_home() / "claude-runs"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = directory / f"{stamp}-rc-{session_id[:8]}.jsonl"
        pty_log_path = directory / f"{stamp}-rc-{session_id[:8]}.pty.log"
        fields["log_path"] = str(log_path)
        metadata["log_path"] = str(log_path)

        run = RemoteControlRun(
            argv,
            workdir=resolved_workdir,
            env=env,
            session_id=session_id,
            transcript_path=transcript_path,
            projects_root=claude_projects_root(),
            timeout_seconds=clamped_timeout,
            stall_watchdog_seconds=stall_watchdog_seconds,
            log_path=log_path,
            pty_log_path=pty_log_path,
        )
        run._log_line(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "event": "start",
                "lane": "remote_control",
                "argv": argv,
                "workdir": resolved_workdir,
                "session_name": session_name,
                "session_id": session_id,
                "auth": auth_summary,
                "timeout_seconds": clamped_timeout,
            }
        )

        started = time.monotonic()
        try:
            run.start()
            try:
                url = run.await_progress_url()
                fields["progress_url"] = url
                metadata["progress_url"] = url
                report = run.await_report()
            finally:
                # The interactive CLI is still alive here by design; stop it on
                # every path so no orphan outlives the tool call.
                run.stop()
        finally:
            fields["duration_seconds"] = round(time.monotonic() - started, 3)

        fields["final_report"] = report["final_report"]
        fields["models_used"] = list(report["models_used"])
        if not fields["final_report"].strip():
            raise RemoteControlRunError(
                "Remote Control run produced an empty final report"
            )
        fields["success"] = True
        return fields

    except RemoteControlError as exc:
        return _fail(exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("remote-control delegation failed: %s", exc, exc_info=True)
        return _fail(_unexpected_failure(exc))


def _unexpected_failure(exc: BaseException) -> RemoteControlRunError:
    """Adapter that surfaces an unexpected exception as a lane failure."""
    failure = RemoteControlRunError(f"remote-control delegation failed: {exc}")
    failure.__cause__ = exc
    return failure
