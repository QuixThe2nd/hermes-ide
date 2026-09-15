#!/usr/bin/env python3
"""Durable dev-pipeline job submission via Kanban (plugin: dev_pipeline).

Gating
------
Registers only when the Cursor Agent CLI binary is resolvable (same check
as ``delegate_cursor_agent``) and ``hermes_cli.kanban_db`` is importable.

This tool is the ONLY supported way for users to submit durable automated
development jobs that survive gateway/executor restarts.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from hermes_cli import kanban_db as kb
from plugins.dev_pipeline.pipeline import (
    OPEN_DEDUP_STATUSES,
    TERMINAL_RESUBMIT_STATUSES,
    compute_idempotency_key,
    get_dev_pipeline_config,
    is_https_repo_url,
    is_local_git_repo,
    resolve_default_branch,
    validate_repo_input,
)
from tools.cursor_agent_tool import check_cursor_agent_requirements

logger = logging.getLogger(__name__)

# Kanban accepts scratch/worktree/dir — not ``git``. The executor clones
# from body JSON; workspace_kind marks a scratch workspace the executor owns.
_DEV_PIPELINE_WORKSPACE_KIND = "scratch"


def check_dev_pipeline_requirements() -> bool:
    """Return True when Cursor CLI and kanban DB are available."""
    try:
        if not check_cursor_agent_requirements():
            return False
        # Import check — kanban_db must be loadable.
        _ = kb.create_board
        return True
    except Exception:
        return False


def _make_result(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


def _find_open_task_by_idempotency(
    conn: Any, idempotency_key: str
) -> str | None:
    placeholders = ",".join("?" * len(OPEN_DEDUP_STATUSES))
    row = conn.execute(
        f"""
        SELECT id FROM tasks
         WHERE idempotency_key = ?
           AND status IN ({placeholders})
         ORDER BY created_at DESC
         LIMIT 1
        """,
        (idempotency_key, *sorted(OPEN_DEDUP_STATUSES)),
    ).fetchone()
    return row["id"] if row else None


def _archive_terminal_tasks_for_resubmit(
    conn: Any, idempotency_key: str
) -> None:
    """Archive prior terminal tasks so create_task idempotency allows resubmit."""
    placeholders = ",".join("?" * len(TERMINAL_RESUBMIT_STATUSES))
    rows = conn.execute(
        f"""
        SELECT id FROM tasks
         WHERE idempotency_key = ?
           AND status IN ({placeholders})
        """,
        (idempotency_key, *sorted(TERMINAL_RESUBMIT_STATUSES)),
    ).fetchall()
    for row in rows:
        try:
            kb.archive_task(conn, row["id"])
        except Exception as exc:
            logger.warning(
                "failed to archive prior dev job %s for resubmit: %s",
                row["id"],
                exc,
            )


def _maybe_register_notify_sub(conn: Any, task_id: str) -> None:
    """Best-effort kanban_notify_subs registration from session context."""
    try:
        from gateway.session_context import get_session_env

        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        if not platform or not chat_id:
            session_key = (
                get_session_env("HERMES_SESSION_KEY", "")
                or os.environ.get("HERMES_SESSION_KEY", "")
            )
            if not session_key:
                return
            platform = "tui"
            chat_id = session_key

        thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "") or None
        user_id = get_session_env("HERMES_SESSION_USER_ID", "") or None
        chat_type = get_session_env("HERMES_SESSION_CHAT_TYPE", "") or None
        message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "") or ""
        notifier_profile = (
            get_session_env("HERMES_SESSION_PROFILE", "")
            or os.environ.get("HERMES_PROFILE")
        )
        if not notifier_profile:
            try:
                from hermes_cli.profiles import get_active_profile_name

                notifier_profile = get_active_profile_name() or "default"
            except Exception:
                notifier_profile = "default"

        delivery_metadata: dict[str, Any] = {}
        if thread_id:
            delivery_metadata["thread_id"] = thread_id
        if chat_type:
            delivery_metadata["chat_type"] = chat_type
        if (
            platform.lower() == "telegram"
            and thread_id
            and (chat_type or "").lower() in {"dm", "direct", "private"}
        ):
            delivery_metadata["telegram_dm_topic_reply_fallback"] = True
            if str(thread_id) not in {"", "1"}:
                delivery_metadata["direct_messages_topic_id"] = str(thread_id)
            if message_id:
                delivery_metadata["telegram_reply_to_message_id"] = str(message_id)

        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=chat_id,
            chat_type=chat_type,
            thread_id=thread_id,
            user_id=user_id,
            notifier_profile=notifier_profile,
            delivery_metadata=delivery_metadata or None,
        )
    except Exception as exc:
        logger.warning(
            "dev_pipeline notify sub registration failed for %s: %r",
            task_id,
            exc,
        )


def delegate_development(
    repo: str,
    task: str,
    branch: str | None = None,
    *,
    plan_mode: str | None = None,
    open_pr: bool | None = None,
    task_id: str | None = None,
) -> str:
    del task_id  # reserved for future correlation

    repo = (repo or "").strip()
    task = (task or "").strip()
    mode_raw = (plan_mode or "").strip().lower() or "consult"
    if mode_raw not in ("consult", "debate"):
        return _make_result(
            success=False,
            task_id=None,
            board=None,
            deduplicated=False,
            message="plan_mode must be 'consult' or 'debate'",
        )
    if open_pr is not None and not isinstance(open_pr, bool):
        return _make_result(
            success=False,
            task_id=None,
            board=None,
            deduplicated=False,
            message="open_pr must be a boolean",
        )
    if not task:
        return _make_result(
            success=False,
            task_id=None,
            board=None,
            deduplicated=False,
            message="task is required",
        )

    ok, err = validate_repo_input(repo)
    if not ok:
        return _make_result(
            success=False,
            task_id=None,
            board=None,
            deduplicated=False,
            message=err,
        )

    cfg = get_dev_pipeline_config()
    board = cfg["board"]

    is_local = not is_https_repo_url(repo)
    resolved_branch = (branch or "").strip() or resolve_default_branch(
        repo, is_local_path=is_local
    )

    idempotency_key = compute_idempotency_key(repo, resolved_branch, task)

    try:
        kb.create_board(board)
        conn = kb.connect(board=board)
        try:
            existing = _find_open_task_by_idempotency(conn, idempotency_key)
            if existing:
                return _make_result(
                    success=True,
                    task_id=existing,
                    board=board,
                    deduplicated=True,
                    message=(
                        f"Durable dev job already queued (task {existing} on board "
                        f"{board!r}). Progress arrives via the kanban notifier when "
                        "the job completes or is blocked."
                    ),
                )

            _archive_terminal_tasks_for_resubmit(conn, idempotency_key)

            body_obj: dict[str, Any] = {
                "repo": repo,
                "branch": resolved_branch,
                "task": task,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }
            if mode_raw != "consult":
                body_obj["plan_mode"] = mode_raw
            if open_pr is False:
                body_obj["open_pr"] = False
            body = json.dumps(body_obj, ensure_ascii=False)
            title = task if len(task) <= 120 else task[:117] + "..."

            new_task_id = kb.create_task(
                conn,
                title=title,
                body=body,
                workspace_kind=_DEV_PIPELINE_WORKSPACE_KIND,
                idempotency_key=idempotency_key,
                max_retries=2,
                board=board,
                created_by=os.environ.get("HERMES_PROFILE") or "agent",
            )
            _maybe_register_notify_sub(conn, new_task_id)

            return _make_result(
                success=True,
                task_id=new_task_id,
                board=board,
                deduplicated=False,
                message=(
                    f"Durable dev job {new_task_id} created on board {board!r}. "
                    "The job survives restarts; progress arrives via the kanban "
                    "notifier when it completes or is blocked."
                ),
            )
        finally:
            conn.close()
    except Exception as exc:
        logger.exception("delegate_development failed")
        return _make_result(
            success=False,
            task_id=None,
            board=board,
            deduplicated=False,
            message=str(exc),
        )


DELEGATE_DEVELOPMENT_SCHEMA = {
    "name": "delegate_development",
    "description": (
        "Submit a durable automated development job. This is the ONLY way to "
        "queue dev-pipeline work that survives gateway and executor restarts. "
        "Hermes plans the task, runs implementation via Cursor or Claude Code "
        "(plan-contract routing), verifies mechanically, reviews, and opens "
        "a draft PR on success. "
        "Returns a Kanban task id immediately; completion and blocked "
        "notifications arrive via the kanban notifier. Requires the Cursor "
        "Agent CLI and Kanban."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": (
                    "Absolute local path to a git repository, or an https URL. "
                    "For local paths the default branch is resolved from "
                    "origin/HEAD (fallback main); https URLs default to main."
                ),
            },
            "task": {
                "type": "string",
                "description": "Natural-language description of the development work.",
            },
            "branch": {
                "type": "string",
                "description": (
                    "Base git branch to implement from. Defaults to the repo's "
                    "default branch."
                ),
            },
            "plan_mode": {
                "type": "string",
                "enum": ["consult", "debate"],
                "description": (
                    "consult = single-round council plan (default, cheaper); "
                    "debate = multi-round adversarial council plan for "
                    "big/ambiguous tasks."
                ),
            },
            "open_pr": {
                "type": "boolean",
                "description": (
                    "Open a draft PR on success (default true). Set false to "
                    "stop after review: the verified branch and diff stay in "
                    "the executor workspace and the result names them."
                ),
            },
        },
        "required": ["repo", "task"],
    },
}


def _handle_delegate_development(args: dict, **kw: Any) -> str:
    return delegate_development(
        repo=args.get("repo", ""),
        task=args.get("task", ""),
        branch=args.get("branch"),
        plan_mode=args.get("plan_mode"),
        open_pr=args.get("open_pr"),
        task_id=kw.get("task_id"),
    )


def _latest_run_phase(conn: Any, task_id: str) -> tuple[int | None, str | None, dict[str, Any]]:
    """Return (run_id, phase, pipeline_state) for the task's latest run."""
    from plugins.dev_pipeline.executor import load_run_metadata, pipeline_state

    row = conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row:
        return None, None, {}
    run_id = int(row["id"])
    state = pipeline_state(load_run_metadata(conn, run_id))
    return run_id, state.get("phase"), state


def _dev_phase_history(conn: Any, task_id: str) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for ev in kb.list_events(conn, task_id):
        if ev.kind != "dev_phase":
            continue
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        history.append({
            "phase": payload.get("phase"),
            "at": ev.created_at,
        })
    return history[-20:]


def dev_pipeline_status(task_id: str | None = None) -> str:
    cfg = get_dev_pipeline_config()
    board = cfg["board"]

    try:
        kb.create_board(board)
        conn = kb.connect(board=board)
    except Exception as exc:
        return _make_result(success=False, message=str(exc))

    try:
        if task_id:
            task_id = (task_id or "").strip()
            task = kb.get_task(conn, task_id)
            if task is None:
                return _make_result(
                    success=False,
                    message=f"unknown task: {task_id}",
                )

            run_id, phase, pipeline = _latest_run_phase(conn, task_id)
            from plugins.dev_pipeline.executor import count_attempt_runs

            run_info: dict[str, Any] = {"run_id": run_id}
            if pipeline.get("run_kind") is not None:
                run_info["run_kind"] = pipeline.get("run_kind")
            if run_id is not None:
                run_info["attempt_runs"] = count_attempt_runs(conn, task_id)

            logs_dir = str(kb.worker_logs_dir(board=board) / task_id)
            return _make_result(
                success=True,
                task_id=task_id,
                board=board,
                status=task.status,
                title=task.title,
                phase=phase,
                phase_history=_dev_phase_history(conn, task_id),
                logs_dir=logs_dir,
                **run_info,
            )

        tasks_out: list[dict[str, Any]] = []
        for task in kb.list_tasks(conn):
            _run_id, phase, _pipeline = _latest_run_phase(conn, task.id)
            tasks_out.append({
                "id": task.id,
                "title": task.title,
                "status": task.status,
                "phase": phase,
                "created_at": task.created_at,
            })
        return _make_result(success=True, board=board, tasks=tasks_out)
    finally:
        conn.close()


DEV_PIPELINE_STATUS_SCHEMA = {
    "name": "dev_pipeline_status",
    "description": (
        "Read the dev-pipeline Kanban board and return current phase, recent "
        "phase history, attempt/run info, and worker log paths for one task "
        "or list all active tasks when task_id is omitted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Kanban task id to inspect. Omit to list all non-archived "
                    "tasks on the dev board."
                ),
            },
        },
    },
}


def _handle_dev_pipeline_status(args: dict, **kw: Any) -> str:
    del kw
    return dev_pipeline_status(task_id=args.get("task_id"))
