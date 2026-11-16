"""Hash-verified artifact egress for FINISHED Cursor cloud runs.

A cloud status of FINISHED only means the agent's writeup exists; the
writeup routinely names commits that were never produced on this host.
Success therefore requires an independently verified artifact manifest:
either the My Machines worker checkout on this host (hash-verified
against its own git objects) or a complete run-stream snapshot whose
file bytes we hash ourselves. Without one, the run fails closed with
"missing artifact egress".

This module is the egress contract only; ``tools/cursor_agent_tool.py``
calls into it and owns cloud dispatch. The Go mirror of these rules
lives in ``internal/cursoregress``.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from tools.cursor_run_receipts import read_receipt

EGRESS_ERROR_MARKER = "missing artifact egress"
STREAM_FETCH_TIMEOUT_SECONDS = 300
GIT_PROBE_TIMEOUT_SECONDS = 15
_WORKSPACE_PREFIX = "/workspace/"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_FetchStream = Callable[..., bytes]


def _failure(detail: str) -> Dict[str, Any]:
    return {
        "success": False,
        "error": f"{EGRESS_ERROR_MARKER}: {detail}",
        "manifest": None,
    }


def hashes_match(
    files: Sequence[Dict[str, str]],
    snapshot_texts: Dict[str, str],
) -> bool:
    """Recompute sha256 of every snapshot text and compare to ``files``.

    Pure function so tests can force it False and prove the success path
    actually consults it. Any sha256 field carried inside a stream event
    is never trusted — this recomputation from the bytes is the check.
    """
    if not files:
        return False
    seen = set()
    for entry in files:
        path = entry.get("path")
        claimed = entry.get("sha256")
        text = snapshot_texts.get(path) if isinstance(path, str) else None
        if not isinstance(path, str) or not isinstance(claimed, str):
            return False
        if not isinstance(text, str):
            return False
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != claimed:
            return False
        seen.add(path)
    return seen == set(snapshot_texts)


def canonical_tree_sha(files: Sequence[Dict[str, str]]) -> str:
    """sha256 of the canonical manifest file list: sorted ``path\\nsha256\\n``.

    A stream-only artifact has no git tree object, so the manifest hashes
    the file list itself instead of inventing a tree SHA.
    """
    canonical = "".join(
        f"{entry['path']}\n{entry['sha256']}\n"
        for entry in sorted(files, key=lambda item: item["path"])
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Worker-checkout locator (disk egress)
# ---------------------------------------------------------------------------


def _argv_from_cmdline(raw: bytes) -> List[str]:
    return [part for part in raw.decode("utf-8", errors="replace").split("\0") if part]


def _flag_values(argv: Sequence[str], flag: str) -> List[str]:
    """Collect values for ``--flag value`` and ``--flag=value`` spellings."""
    values: List[str] = []
    prefix = flag + "="
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            values.append(argv[index + 1])
        elif token.startswith(prefix):
            values.append(token[len(prefix):])
    return values


def _is_worker_argv(argv: Sequence[str], machine_name: str) -> bool:
    """argv contains ``worker``, ``--name``, and that machine's name."""
    tokens = set(argv)
    if "worker" not in tokens:
        return False
    if "--name" not in tokens and not any(t.startswith("--name=") for t in argv):
        return False
    return machine_name in _flag_values(argv, "--name")


def discover_checkout_roots(machine_name: str = "hermes-server") -> List[str]:
    """Scan /proc cmdlines for the machine's worker; return its --worker-dir roots.

    The caller's workdir is never considered unless that same path is one
    of the worker's own roots — roots come only from the worker's argv.
    """
    roots: List[str] = []
    try:
        entries = sorted(Path("/proc").iterdir())
    except OSError:
        return roots
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            argv = _argv_from_cmdline((entry / "cmdline").read_bytes())
        except OSError:
            continue
        if not _is_worker_argv(argv, machine_name):
            continue
        for value in _flag_values(argv, "--worker-dir"):
            if value and value not in roots:
                roots.append(value)
    return roots


def _git(repo: str, *args: str) -> Optional[str]:
    """One read-only git probe; any failure means the root does not match."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=GIT_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _file_sha256(path: Path) -> Optional[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def match_disk_root(root: str, source_base: str) -> Optional[Dict[str, Any]]:
    """Verify one candidate worker checkout against its own git objects.

    A root matches only when it is a git repo, ``source_base`` is an
    ancestor of HEAD, HEAD has moved past ``source_base``, at least one
    path changed, and every changed path's on-disk bytes equal the bytes
    of ``git show HEAD:path`` — a dirty worktree fails closed.
    """
    if not source_base:
        return None
    if _git(root, "rev-parse", "--git-dir") is None:
        return None
    is_ancestor = subprocess.run(
        ["git", "-C", root, "merge-base", "--is-ancestor", source_base, "HEAD"],
        capture_output=True,
        timeout=GIT_PROBE_TIMEOUT_SECONDS,
        check=False,
    )
    if is_ancestor.returncode != 0:
        return None
    head = (_git(root, "rev-parse", "HEAD") or "").strip()
    if not head or head == source_base:
        return None
    changed = [
        line.strip()
        for line in (_git(root, "diff", "--name-only", source_base, "HEAD") or "").splitlines()
        if line.strip()
    ]
    if not changed:
        return None
    repo_path = Path(root)
    files: List[Dict[str, str]] = []
    for rel in changed:
        blob = _git(root, "show", f"HEAD:{rel}")
        if blob is None:
            return None  # deleted at HEAD or unreadable: fail closed
        blob_sha = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        disk_sha = _file_sha256(repo_path / rel)
        if disk_sha is None or disk_sha != blob_sha:
            return None  # dirty worktree vs HEAD
        files.append({"path": rel, "sha256": blob_sha})
    tree = (_git(root, "rev-parse", "HEAD^{tree}") or "").strip()
    if not tree:
        return None
    files.sort(key=lambda item: item["path"])
    return {
        "checkout_path": str(repo_path.resolve()),
        "source_base": source_base,
        "head_sha": head,
        "tree_sha": tree,
        "files": files,
        "source": "disk",
    }


# ---------------------------------------------------------------------------
# Run-stream fallback (only when no disk root matched)
# ---------------------------------------------------------------------------


def _http_get_stream(url: str, *, api_key: str, timeout: float) -> bytes:
    """Single read-only GET of the run stream; HTTP/1.1, SSE Accept.

    Deliberately NOT tools.cursor_agent_tool._http_request: that client
    sends Accept: application/json with a 30 s timeout and cannot read a
    run stream. A timeout or short read returns what arrived — the
    completeness check decides.
    """
    import httpx

    with httpx.Client(http2=False, timeout=timeout) as client:
        response = client.get(
            url,
            auth=(api_key, ""),
            headers={
                "Accept": "text/event-stream",
                "Accept-Encoding": "gzip",
            },
        )
        response.raise_for_status()
        return response.content


def stream_body_is_complete(body: bytes) -> Tuple[bool, str]:
    """A complete body is fully received, newline-terminated, and turn-ended.

    Truncation signals — any one means failure even if some file bytes
    are present: no trailing newline (cut mid-line), a data line whose
    JSON is invalid, or no terminal ``{"type":"turn-ended"}`` event.
    """
    if not body:
        return False, "empty stream body"
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return False, "stream body is not valid UTF-8 (cut multi-byte sequence)"
    if not text.endswith("\n"):
        return False, "stream body does not end with a newline"
    saw_turn_ended = False
    for line in text.split("\n")[:-1]:
        if not line or line.startswith(":") or line.startswith("event:"):
            continue
        if not line.startswith("data:"):
            return False, f"stream line is not a data event: {line[:60]!r}"
        payload = line[len("data:"):].strip()
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return False, "invalid trailing data line"
        if isinstance(event, dict) and event.get("type") == "turn-ended":
            saw_turn_ended = True
    if not saw_turn_ended:
        return False, "stream body has no turn-ended event"
    return True, ""


def _strip_workspace(path: str) -> str:
    if path.startswith(_WORKSPACE_PREFIX):
        return path[len(_WORKSPACE_PREFIX):]
    return path


def _snapshot_from_holder(holder: Any) -> Optional[Tuple[str, str]]:
    """(repo-relative path, full text) from an object with afterFullFileContent."""
    if not isinstance(holder, dict):
        return None
    content = holder.get("afterFullFileContent")
    if not isinstance(content, str):
        return None
    for key in ("path", "targetPath", "filePath"):
        value = holder.get(key)
        if isinstance(value, str) and value.strip():
            return _strip_workspace(value.strip()), content
    return None


def extract_stream_snapshots(body: bytes) -> Dict[str, str]:
    """Path -> latest full-file text from the stream's edit snapshots.

    The bytes ride in the untyped ``result.success.afterFullFileContent``
    shape (the typed tool-call-completed edit event often carries only a
    diff); ``toolCall.result.value`` and ``toolCall.editToolCall`` are
    accepted too. Later events win per path. No sha256 inside an event
    is ever trusted — callers hash these texts themselves.
    """
    texts: Dict[str, str] = {}
    text = body.decode("utf-8", errors="replace")
    for line in text.split("\n"):
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload.startswith("{"):
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        found: Optional[Tuple[str, str]] = None
        result = event.get("result")
        if isinstance(result, dict):
            found = _snapshot_from_holder(result.get("success"))
        if found is None:
            tool_call = event.get("toolCall")
            if isinstance(tool_call, dict):
                inner = tool_call.get("result")
                if isinstance(inner, dict):
                    found = _snapshot_from_holder(inner.get("value"))
                if found is None:
                    found = _snapshot_from_holder(tool_call.get("editToolCall"))
        if found is not None:
            texts[found[0]] = found[1]
    return texts


def _stream_egress(
    receipt: Dict[str, Any],
    *,
    api_key: str,
    fetch_stream: Optional[_FetchStream],
    api_base: str,
) -> Dict[str, Any]:
    agent_id = str(receipt.get("cloud_agent_id") or "").strip()
    run_id = str(receipt.get("cloud_run_id") or "").strip()
    if not agent_id or not run_id:
        return _failure("receipt has no cloud agent/run ids for the stream fetch")
    fetch = fetch_stream or _http_get_stream
    url = f"{api_base}/v1/agents/{agent_id}/runs/{run_id}/stream"
    try:
        body = fetch(url, api_key=api_key, timeout=STREAM_FETCH_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - one GET, any failure is egress failure
        return _failure(f"stream fetch failed: {type(exc).__name__}")
    complete, reason = stream_body_is_complete(body)
    if not complete:
        return _failure(f"run stream truncated ({reason})")
    texts = extract_stream_snapshots(body)
    if not texts:
        return _failure("complete run stream carries no full-file snapshots")
    files = [
        {
            "path": path,
            "sha256": hashlib.sha256(texts[path].encode("utf-8")).hexdigest(),
        }
        for path in sorted(texts)
    ]
    if not hashes_match(files, texts):
        return _failure("stream snapshot hashes do not match their bytes")
    return {
        "success": True,
        "error": None,
        "manifest": {
            "checkout_path": None,
            "source_base": receipt.get("source_base_sha"),
            "head_sha": None,
            "tree_sha": canonical_tree_sha(files),
            "files": files,
            "source": "stream",
        },
    }


def verify_finished_egress(
    receipt: Dict[str, Any],
    *,
    api_key: str,
    checkout_roots: Optional[Sequence[str]] = None,
    fetch_stream: Optional[_FetchStream] = None,
    machine_name: str = "hermes-server",
    api_base: str = "https://api.cursor.com",
) -> Dict[str, Any]:
    """Verify a FINISHED run's artifact independently of its writeup.

    Returns ``{"success", "error", "manifest"}``. Disk egress (preferred):
    the machine worker's own checkout, hash-verified against its git
    objects. Stream egress (only when no disk root matches): one
    read-only GET whose complete body must carry turn-ended plus
    full-file snapshots we hash ourselves.
    """
    receipt = receipt if isinstance(receipt, dict) else {}
    source_base = receipt.get("source_base_sha")
    source_base = source_base if isinstance(source_base, str) else ""
    roots = list(checkout_roots) if checkout_roots is not None else discover_checkout_roots(
        machine_name
    )
    matches: List[Dict[str, Any]] = []
    for root in roots:
        manifest = match_disk_root(root, source_base)
        if manifest is not None:
            matches.append(manifest)
    if len(matches) > 1:
        return _failure(
            f"{len(matches)} worker checkouts match the dispatched base; refusing to guess"
        )
    if len(matches) == 1:
        return {"success": True, "error": None, "manifest": matches[0]}
    return _stream_egress(
        receipt, api_key=api_key, fetch_stream=fetch_stream, api_base=api_base
    )


def recover_artifact_for_receipt(receipt_path: Path, *, api_key: str) -> Dict[str, Any]:
    """Verify the artifact for an existing receipt without creating anything.

    Loads that exact receipt, uses only its cloud ids and source base,
    and issues no agent create and no POST. A receipt without cloud ids
    performs zero HTTP.
    """
    receipt = read_receipt(receipt_path)
    if not isinstance(receipt, dict):
        return _failure("receipt unreadable")
    return verify_finished_egress(receipt, api_key=api_key)


__all__ = [
    "EGRESS_ERROR_MARKER",
    "STREAM_FETCH_TIMEOUT_SECONDS",
    "canonical_tree_sha",
    "discover_checkout_roots",
    "extract_stream_snapshots",
    "hashes_match",
    "match_disk_root",
    "recover_artifact_for_receipt",
    "stream_body_is_complete",
    "verify_finished_egress",
]
