"""Drift inventory, capture, auditd attribution, and the scheduled watch pass.

Everything here is read-only toward the watched tree's git state: inventories,
diffs, and copies only. Drift is never reverted, staged, or deleted.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence

from plugins.drift_watch.config import DEFAULT_MAX_CAPTURES, DEFAULT_RETAIN_DAYS

logger = logging.getLogger(__name__)

ERROR_PREFIX = "drift_watch error:"
ATTRIBUTION_UNAVAILABLE = "attribution unavailable"
AUSEARCH_MISSING = "attribution unavailable: ausearch not found"
AUDIT_KEY = "live-tree-write"
AUDIT_TAIL_LINES = 12
GIT_TIMEOUT_SEC = 120
TOO_BIG_BYTES = 1024 * 1024  # 1 MiB


class DriftWatchError(RuntimeError):
    """One-line operational failure — surfaced as an error string, never raised out."""


# ── git plumbing ────────────────────────────────────────────────────────────


def _git(tree: Path, *args: str) -> str:
    argv = ["git", "--no-optional-locks", *args]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(tree),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DriftWatchError(f"git {' '.join(args)} failed in {tree}: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        first = detail[0] if detail else f"exit code {proc.returncode}"
        raise DriftWatchError(f"git {' '.join(args)} failed in {tree}: {first}")
    return proc.stdout or ""


def _resolve_head(tree: Path) -> str:
    return _git(tree, "rev-parse", "HEAD").strip()


def _porcelain_lines(tree: Path) -> list[str]:
    return sorted(line for line in _git(tree, "status", "--porcelain").splitlines() if line.strip())


def _file_sha256(path: Path) -> str:
    """Hex digest of a regular file, or ``-`` when it is gone/not a regular file."""
    try:
        if not path.is_file():
            return "-"
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return "-"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


# ── inventory ───────────────────────────────────────────────────────────────


def build_inventory(tree: str | Path) -> str:
    """One ``head <sha>`` line plus one ``<xy> <path> <sha256|->`` line per drift entry.

    Raises :class:`DriftWatchError` when the tree is missing or not a git repo.
    """
    root = Path(tree)
    if not root.is_dir():
        raise DriftWatchError(f"tree not found: {root}")
    head = _resolve_head(root)
    lines = [f"head {head}"]
    for porcelain in _porcelain_lines(root):
        path_text = porcelain[3:] if len(porcelain) > 3 else ""
        lines.append(f"{porcelain} {_file_sha256(root / path_text)}")
    return "\n".join(lines) + "\n"


def inventory_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def drift_lines(inventory_text: str) -> list[str]:
    """Porcelain lines of an inventory — everything after the ``head`` line."""
    return [line for line in (inventory_text or "").splitlines()[1:] if line.strip()]


def last_drift_count(state_dir: str | Path) -> int | None:
    """Drift path count in the stored inventory, or ``None`` when nothing is stored."""
    text = _read_text(Path(state_dir) / "last-inventory.txt")
    if text is None:
        return None
    return len(drift_lines(text))


def last_capture_dir(state_dir: str | Path) -> Path | None:
    """Newest capture directory (timestamp names sort chronologically), if any."""
    captures = Path(state_dir) / "captures"
    if not captures.is_dir():
        return None
    dirs = sorted((d for d in captures.iterdir() if d.is_dir()), key=lambda d: d.name)
    return dirs[-1] if dirs else None


# ── auditd attribution ──────────────────────────────────────────────────────

_AUDIT_TIME_RE = re.compile(r"^time->(.+)$", re.MULTILINE)
_AUDIT_MSG_RE = re.compile(r"msg=audit\(([^)]*)\)")


def _audit_values(block: str, key: str) -> list[str]:
    """Values for ``key=`` tokens — quoted or bare (``ausearch -i`` uses bare)."""
    pattern = rf"\b{key}=(?:\"([^\"]*)\"|(\S+))"
    return [quoted or bare for quoted, bare in re.findall(pattern, block)]


def _audit_timestamp(block: str) -> str:
    """``time->`` line when present, else ``msg=audit(...)`` cut at the millis.

    ``ausearch -i`` renders ``msg=audit(29/08/26 13:31:00.067:17810)``; the
    reference keeps the ``29/08/26 13:31:00`` part.
    """
    ts_match = _AUDIT_TIME_RE.search(block)
    if ts_match:
        return ts_match.group(1).strip()
    msg_match = _AUDIT_MSG_RE.search(block)
    if msg_match:
        return msg_match.group(1).strip().split(".", 1)[0]
    return "?"


def parse_ausearch_blocks(text: str, *, keep: int = AUDIT_TAIL_LINES) -> list[str]:
    """One ``timestamp | comm | exe | paths`` line per audit event block.

    ``CONFIG_CHANGE`` blocks are skipped; file paths come from ``name=``
    tokens that do not end in ``/`` (directories), kept raw.
    """
    lines: list[str] = []
    for block in text.split("\n----\n"):
        if "CONFIG_CHANGE" in block or not block.strip():
            continue
        comm = _audit_values(block, "comm")
        exe = _audit_values(block, "exe")
        paths = [
            name
            for name in _audit_values(block, "name")
            if name and not name.endswith("/")
        ]
        if not comm and not exe and not paths:
            continue
        lines.append(
            " | ".join(
                (
                    _audit_timestamp(block),
                    comm[0] if comm else "?",
                    exe[0] if exe else "?",
                    ",".join(paths),
                )
            )
        )
    return lines[-keep:] if keep > 0 else []


def _run_ausearch(argv: Sequence[str]) -> tuple[int, str]:
    proc = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc.returncode, proc.stdout or ""


def auditd_tail(
    *,
    run_cmd: Callable[[Sequence[str]], tuple[int, str]] | None = None,
    which: Callable[[str], str | None] | None = None,
    keep: int = AUDIT_TAIL_LINES,
) -> str:
    """Best-effort ``ausearch -k live-tree-write -i`` tail — never raises."""
    finder = which or shutil.which
    if finder("ausearch") is None:
        return AUSEARCH_MISSING
    runner = run_cmd or _run_ausearch
    try:
        code, out = runner(["ausearch", "-k", AUDIT_KEY, "-i"])
    except Exception as exc:
        logger.debug("drift-watch attribution query failed (non-fatal): %s", exc)
        return ATTRIBUTION_UNAVAILABLE
    if code != 0:
        return ATTRIBUTION_UNAVAILABLE
    return "\n".join(parse_ausearch_blocks(out, keep=keep))


# ── capture + prune ─────────────────────────────────────────────────────────


def _timestamp(now: datetime) -> str:
    return now.strftime("%Y%m%d-%H%M%S")


def capture_drift(
    tree: str | Path,
    state_dir: str | Path,
    inventory: str,
    *,
    now: datetime | None = None,
) -> Path:
    """Write one capture snapshot and roll the stored inventory forward.

    Never mutates the tree's git state — diffs and copies only.
    """
    root = Path(tree)
    state = Path(state_dir)
    moment = now or datetime.now()
    ts = _timestamp(moment)
    head = drift_head(inventory)
    capture = state / "captures" / ts

    _write_text(capture / "tracked.patch", _git(root, "diff"))
    _write_text(capture / "stat.txt", _git(root, "diff", "--stat"))
    _write_text(capture / "inventory.txt", inventory)
    _write_text(capture / "meta.txt", f"head {head}\ncaptured {ts}\n")

    too_big: list[str] = []
    for porcelain in _porcelain_lines(root):
        if not porcelain.startswith("??"):
            continue
        relative = porcelain[3:]
        source = root / relative
        if source.is_dir():
            # Untracked directories stay listed in the inventory; not copied.
            continue
        try:
            size = source.stat().st_size
        except OSError:
            continue
        if size >= TOO_BIG_BYTES:
            too_big.append(f"{relative} ({size} bytes, too big to copy)")
            continue
        target = capture / "untracked" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if too_big:
        _write_text(capture / "untracked-too-big.txt", "\n".join(too_big) + "\n")

    _write_text(capture / "attribution.txt", auditd_tail() + "\n")

    _write_text(state / "last-inventory.txt", inventory)
    _write_text(state / "history" / f"inventory-{ts}.txt", inventory)
    return capture


def drift_head(inventory_text: str) -> str:
    """The ``head`` sha line of an inventory (empty string when malformed)."""
    lines = (inventory_text or "").splitlines()
    if not lines or not lines[0].startswith("head "):
        return ""
    return lines[0][len("head "):].strip()


def prune_state(
    state_dir: str | Path,
    *,
    retain_days: int = DEFAULT_RETAIN_DAYS,
    max_captures: int = DEFAULT_MAX_CAPTURES,
    now: datetime | None = None,
) -> None:
    """Drop captures past ``retain_days`` or beyond the newest ``max_captures``.

    History inventories are pruned by age only (no count cap).
    """
    state = Path(state_dir)
    cutoff = (now or datetime.now()).timestamp() - retain_days * 86400

    captures = state / "captures"
    if captures.is_dir():
        dirs = sorted(d for d in captures.iterdir() if d.is_dir())
        stale = {d for d in dirs if d.stat().st_mtime < cutoff}
        remaining = [d for d in dirs if d not in stale]
        surplus = remaining[: max(0, len(remaining) - max(0, max_captures))]
        for entry in [*stale, *surplus]:
            shutil.rmtree(entry, ignore_errors=True)

    history = state / "history"
    if history.is_dir():
        for entry in history.iterdir():
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)


# ── the watch pass ──────────────────────────────────────────────────────────


def _format_alert(root: Path, inventory: str, capture: Path) -> str:
    drift = drift_lines(inventory)
    if not drift:
        return f"drift gone: live tree matches HEAD again (capture: {capture})"
    body = "\n".join(drift)
    return (
        f"drift detected in {root}: {len(drift)} path(s) differ from the last "
        f"inventory\n"
        f"```\n{body}\n```"
    )


def _run_pass(
    tree: str | Path,
    state_dir: str | Path,
    retain_days: int,
    max_captures: int,
    now: datetime | None,
) -> str:
    tree_text = str(tree or "").strip()
    if not tree_text:
        raise DriftWatchError(
            "no tree configured (set drift_watch.tree or HERMES_PROJECT)"
        )
    root = Path(tree_text)
    state = Path(state_dir)

    inventory = build_inventory(root)
    current = inventory_hash(inventory)
    stored = _read_text(state / "last-inventory.txt")
    if stored is not None and inventory_hash(stored) == current:
        return ""

    capture = capture_drift(root, state, inventory, now=now)
    prune_state(state, retain_days=retain_days, max_captures=max_captures, now=now)
    return _format_alert(root, inventory, capture)


def run_drift_watch(
    tree: str | Path,
    state_dir: str | Path,
    retain_days: int = DEFAULT_RETAIN_DAYS,
    max_captures: int = DEFAULT_MAX_CAPTURES,
    *,
    now: datetime | None = None,
) -> str:
    """One watch pass: alert text on drift change, ``""`` when unchanged.

    Never raises — operational failures return a one-line ``drift_watch error:``
    string so the scheduled run surfaces them instead of dying.
    """
    try:
        return _run_pass(tree, state_dir, retain_days, max_captures, now)
    except DriftWatchError as exc:
        return f"{ERROR_PREFIX} {exc}"
    except Exception as exc:  # best-effort by contract
        logger.warning("drift-watch pass failed: %s", exc, exc_info=True)
        detail = str(exc).strip().splitlines() or [exc.__class__.__name__]
        return f"{ERROR_PREFIX} {detail[0]}"


if __name__ == "__main__":  # pragma: no cover
    from plugins.drift_watch.config import load_drift_watch_config

    _cfg = load_drift_watch_config()
    _text = run_drift_watch(
        _cfg["tree"],
        _cfg["state_dir"],
        retain_days=_cfg["retain_days"],
        max_captures=_cfg["max_captures"],
    )
    if _text:
        print(_text)
