"""Runtime integration for model retrieval and administrative sync.

This module is the only bridge between Hermes registration, trusted config,
Discord/DCE transport, and the lower-level archive modules. Model calls remain
read-only; mutation is reachable only from the local CLI.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from .config import PluginConfig, load_secrets
from .db import connect
from .paths import dce_binary, state_root

STATE_ROOT = state_root()
DCE_BINARY = dce_binary()
EXPORT_SETTLE_SECONDS = 5


def _config_mapping() -> Mapping[str, Any]:
    from hermes_cli.config import load_config

    cfg = load_config() or {}
    return (
        (((cfg.get("plugins") or {}).get("entries") or {}).get("discord-history") or {})
        .get("config")
        or {}
    )


def load_plugin_config() -> PluginConfig:
    return PluginConfig.from_mapping(_config_mapping())


def handle_discord_history(arguments: Mapping[str, Any], **_dispatch_context: Any) -> str:
    """Model-tool entry point. Inputs cannot replace config, secrets, or connector."""
    from .tool import handle_discord_history as handle

    config = load_plugin_config()
    secrets = load_secrets()
    return handle(arguments, config=config, secrets=secrets, connector=connect)


def _discord_helpers():
    from hermes_cli.env_loader import load_hermes_dotenv
    from hermes_constants import get_hermes_home
    from tools.discord_tool import _discord_request, _get_bot_token

    load_hermes_dotenv(hermes_home=get_hermes_home())
    return _discord_request, _get_bot_token


def _channel_metadata(channel_id: str, token: str) -> dict[str, Any]:
    request, _ = _discord_helpers()
    payload = request("GET", f"/channels/{channel_id}", token)
    if not isinstance(payload, Mapping) or str(payload.get("id", "")) != channel_id:
        raise RuntimeError("discord_channel_metadata_invalid")
    return dict(payload)


def _validate_channel_scope(
    metadata: Mapping[str, Any], *, guild_id: str, allowed_roots: frozenset[str]
) -> None:
    if str(metadata.get("guild_id", "")) != guild_id:
        raise RuntimeError("channel_guild_mismatch")
    channel_id = str(metadata.get("id", ""))
    parent_id = str(metadata.get("parent_id") or "")
    if channel_id not in allowed_roots and parent_id not in allowed_roots:
        raise RuntimeError("channel_not_allowlisted")


def _collect_inventory(guild_id: str, config: PluginConfig) -> dict[str, Any]:
    from .discord_api import inventory_guild

    _discord_helpers()
    return inventory_guild(
        guild_id, parent_channel_ids=config.channels_for_guild(guild_id)
    )


def _persist_inventory(guild_id: str, inventory: Mapping[str, Any]) -> dict[str, Any]:
    from .ingest import record_inventory_manifests

    if str((inventory.get("guild") or {}).get("id", "")) != guild_id:
        raise RuntimeError("inventory_guild_mismatch")
    run_id = uuid4()
    observed = datetime.now(timezone.utc)
    state = str(inventory.get("state") or "error")
    status = "ok" if state == "complete" else "partial" if state == "inaccessible" else "error"
    parents = inventory.get("parents") or {}
    guild_metadata = inventory.get("guild") or {}
    conn = connect(load_secrets().database_url)
    scope_count = 0
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO discord_archive.guilds(guild_id,name,icon_url,last_observed_at) "
                "VALUES(%s,%s,%s,%s) ON CONFLICT(guild_id) DO UPDATE SET "
                "name=EXCLUDED.name,icon_url=EXCLUDED.icon_url,last_observed_at=EXCLUDED.last_observed_at",
                (guild_id, str(guild_metadata.get("name") or "Unknown guild"),
                 guild_metadata.get("icon") or guild_metadata.get("icon_url"), observed),
            )
            conn.execute(
                "INSERT INTO discord_archive.ingest_runs"
                "(run_id,channel_id,mode,dce_version,started_at,status) "
                "VALUES(%s,NULL,'inventory','2.47.3',%s,'running')",
                (run_id, observed),
            )
            for parent_id, parent in parents.items():
                parent_id = str(parent_id)
                parent_metadata = parent.get("parent_channel") or {}
                if str(parent_metadata.get("id", "")) != parent_id:
                    raise RuntimeError("inventory_parent_metadata_mismatch")
                try:
                    parent_type = int(parent_metadata.get("type", -1))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("inventory_parent_metadata_invalid") from exc
                if parent_type >= 0:
                    conn.execute(
                        "INSERT INTO discord_archive.channels"
                        "(channel_id,guild_id,parent_channel_id,channel_type,name,topic,is_thread,archived,locked,last_observed_at) "
                        "VALUES(%s,%s,NULL,%s,%s,%s,false,false,false,%s) "
                        "ON CONFLICT(channel_id) DO UPDATE SET guild_id=EXCLUDED.guild_id,"
                        "parent_channel_id=NULL,channel_type=EXCLUDED.channel_type,name=EXCLUDED.name,"
                        "topic=EXCLUDED.topic,is_thread=false,archived=false,locked=false,"
                        "last_observed_at=EXCLUDED.last_observed_at",
                        (parent_id, guild_id, parent_type,
                         str(parent_metadata.get("name") or "Unknown channel"),
                         parent_metadata.get("topic"), observed),
                    )
                active_ids = set(map(str, parent.get("active_thread_ids", ())))
                all_ids = set(map(str, parent.get("all_thread_ids", ())))
                parent_state = str(parent.get("state") or "error")
                scopes = {parent_id: "channel"}
                scopes.update({tid: "active_thread" if tid in active_ids else "archived_thread" for tid in all_ids})
                for channel_id, kind in scopes.items():
                    conn.execute(
                        "INSERT INTO discord_archive.ingest_run_scope"
                        "(run_id,channel_id,channel_kind,inventory_observed_at,inventory_state,export_state) "
                        "VALUES(%s,%s,%s,%s,%s,'pending')",
                        (run_id, channel_id, kind, observed, parent_state),
                    )
                    scope_count += 1
                record_inventory_manifests(
                    conn,
                    run_id,
                    parent_id,
                    list((parent.get("endpoints") or {}).values()),
                    parent,
                )
            conn.execute(
                "UPDATE discord_archive.ingest_runs SET finished_at=%s,status=%s,error_code=%s "
                "WHERE run_id=%s",
                (observed, status, None if state == "complete" else inventory.get("termination_reason"), run_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"run_id": str(run_id), "state": state, "status": status,
            "parent_count": len(parents), "scope_count": scope_count}


def _inventory_with_retries(guild_id: str, config: PluginConfig,
                            *, max_attempts: int = 3) -> tuple[dict[str, Any], dict[str, Any], int]:
    inventory: dict[str, Any] = {}
    persisted: dict[str, Any] = {}
    for attempt in range(1, max_attempts + 1):
        inventory = _collect_inventory(guild_id, config)
        persisted = _persist_inventory(guild_id, inventory)
        if inventory.get("state") == "complete" or inventory.get("state") == "inaccessible":
            return inventory, persisted, attempt
        failures = [endpoint for parent in inventory.get("parents", {}).values()
                    for endpoint in (parent.get("endpoints") or {}).values()
                    if endpoint.get("state") != "complete"]
        retryable = bool(failures)
        for endpoint in failures:
            reason = str(endpoint.get("termination_reason") or "")
            if reason == "transport_error":
                continue
            if reason.startswith("http_"):
                try:
                    status = int(reason.removeprefix("http_"))
                except ValueError:
                    retryable = False
                    break
                if status == 429 or status >= 500:
                    continue
            retryable = False
            break
        if not retryable:
            return inventory, persisted, attempt
        if attempt < max_attempts:
            time.sleep(attempt)
    return inventory, persisted, max_attempts


def run_inventory(*, guild_id: str) -> dict[str, Any]:
    config = load_plugin_config()
    if guild_id not in config.allowed_guild_ids:
        raise RuntimeError("guild_not_allowlisted")
    inventory, persisted, attempts = _inventory_with_retries(guild_id, config)
    thread_ids = {str(tid) for parent in inventory.get("parents", {}).values()
                  for tid in parent.get("all_thread_ids", ())}
    return {"ok": inventory.get("state") == "complete", "command": "inventory",
            "guild_id": guild_id, "thread_count": len(thread_ids),
            "attempts": attempts, **persisted}


def _all_inventory_scopes(guild_id: str, config: PluginConfig) -> tuple[list[str], dict[str, Any]]:
    inventory, persisted, attempts = _inventory_with_retries(guild_id, config)
    inventory["persistence"] = {**persisted, "attempts": attempts}
    if inventory.get("state") != "complete":
        raise RuntimeError("inventory_incomplete")
    scopes = set(config.channels_for_guild(guild_id))
    for parent in inventory.get("parents", {}).values():
        scopes.update(map(str, parent.get("all_thread_ids", ())))
    return sorted(scopes, key=int), inventory


def _cursor(conn: Any, channel_id: str) -> datetime | None:
    row = conn.execute(
        "SELECT newest_created_at FROM discord_archive.ingest_cursors WHERE channel_id=%s",
        (channel_id,),
    ).fetchone()
    return row[0] if row else None


def _try_channel_lock(conn: Any, channel_id: str) -> bool:
    row = conn.execute(
        "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (channel_id,)
    ).fetchone()
    conn.commit()
    return bool(row and row[0])


def _release_channel_lock(conn: Any, channel_id: str) -> None:
    row = conn.execute(
        "SELECT pg_advisory_unlock(hashtextextended(%s,0))", (channel_id,)
    ).fetchone()
    conn.commit()
    if not row or not row[0]:
        raise RuntimeError("advisory_unlock_failed")


def _sync_one_channel(*, conn: Any, exporter: Any, token: str, channel_id: str,
                      mode: str, fixed_before: datetime, batch_dir: Path,
                      channel_metadata: Mapping[str, Any], keep_export: bool,
                      inventory_run_id: str | None = None) -> dict[str, Any]:
    from .dce import ExportRequest
    from .ingest import import_export
    from .sync import plan_sync

    if not _try_channel_lock(conn, channel_id):
        return {"channel_id": channel_id, "ok": False, "state": "already_locked"}
    try:
        newest = _cursor(conn, channel_id)
        # psycopg starts a transaction for the cursor SELECT. End that read
        # transaction before the long DCE subprocess.
        conn.commit()
        plan = plan_sync(
            channel_id,
            mode,
            newest_created_at=newest,
            overlap_hours=48,
            export_before=fixed_before,
        )
        output = batch_dir / f"{channel_id}.json"
        manifest = exporter.export(
            ExportRequest(channel_id, output, after=plan.after, before=plan.before),
            token,
        )
        committed = False
        try:
            imported = import_export(
                conn,
                output,
                mode=mode,
                dce_version="2.47.3",
                complete=plan.full_export,
                source_after=(
                    datetime.fromisoformat(plan.after.replace("Z", "+00:00"))
                    if plan.after
                    else None
                ),
                source_before=fixed_before,
                channel_metadata=channel_metadata,
                inventory_run_id=inventory_run_id,
            )
            conn.commit()
            committed = True
            return {
                "channel_id": channel_id,
                "ok": True,
                "state": "ok",
                "manifest": {k: v for k, v in manifest.items() if k != "output"},
                **imported,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            if committed and output.exists() and not keep_export:
                output.unlink()
    finally:
        _release_channel_lock(conn, channel_id)


def run_sync(
    *, guild_id: str, channel_ids: Iterable[str] | None, mode: str,
    keep_export: bool = False,
) -> dict[str, Any]:
    """Export and atomically import selected allowlisted channels or all inventory scopes."""
    from .dce import DCEExporter

    if mode not in {"backfill", "incremental", "reconcile"}:
        raise RuntimeError("invalid_sync_mode")
    config = load_plugin_config()
    if guild_id not in config.allowed_guild_ids:
        raise RuntimeError("guild_not_allowlisted")
    allowed_roots = config.channels_for_guild(guild_id)
    request, get_token = _discord_helpers()
    token = get_token()
    if not token:
        raise RuntimeError("discord_token_missing")

    selected = list(dict.fromkeys(map(str, channel_ids or ())))
    inventory: dict[str, Any] | None = None
    if not selected:
        selected, inventory = _all_inventory_scopes(guild_id, config)
    elif mode == "reconcile":
        inventory, persisted, attempts = _inventory_with_retries(guild_id, config)
        inventory["persistence"] = {**persisted, "attempts": attempts}
        if inventory.get("state") != "complete":
            raise RuntimeError("inventory_incomplete")
        discovered = set(config.channels_for_guild(guild_id))
        for parent in inventory.get("parents", {}).values():
            discovered.update(map(str, parent.get("all_thread_ids", ())))
        if not set(selected).issubset(discovered):
            raise RuntimeError("reconcile_scope_missing_from_inventory")
    if not selected:
        raise RuntimeError("no_sync_scopes")

    metadata: dict[str, dict[str, Any]] = {}
    for channel_id in selected:
        if not channel_id.isascii() or not channel_id.isdecimal() or not 17 <= len(channel_id) <= 20:
            raise RuntimeError("invalid_channel_id")
        item = request("GET", f"/channels/{channel_id}", token)
        if not isinstance(item, Mapping) or str(item.get("id", "")) != channel_id:
            raise RuntimeError("discord_channel_metadata_invalid")
        metadata[channel_id] = dict(item)
        _validate_channel_scope(metadata[channel_id], guild_id=guild_id, allowed_roots=allowed_roots)

    secrets = load_secrets()
    conn = connect(secrets.database_url)
    exporter = DCEExporter(DCE_BINARY)
    # Discord can expose a just-created message after its snowflake timestamp.
    # Leave the moving boundary for the next overlapping sync.
    fixed_before = datetime.now(timezone.utc) - timedelta(seconds=EXPORT_SETTLE_SECONDS)
    batch_dir = STATE_ROOT / "tmp" / fixed_before.strftime("%Y%m%dT%H%M%S.%fZ")
    batch_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(batch_dir, 0o700)
    results: list[dict[str, Any]] = []
    inventory_run_id = ((inventory or {}).get("persistence") or {}).get("run_id")
    try:
        for channel_id in selected:
            results.append(_sync_one_channel(
                conn=conn, exporter=exporter, token=token, channel_id=channel_id,
                mode=mode, fixed_before=fixed_before, batch_dir=batch_dir,
                channel_metadata=metadata[channel_id], keep_export=keep_export,
                inventory_run_id=inventory_run_id if mode == "reconcile" else None,
            ))
    finally:
        conn.close()
        if not keep_export:
            try:
                batch_dir.rmdir()
            except OSError:
                pass

    ok = len(results) == len(selected) and all(item["ok"] for item in results)
    return {
        "ok": ok,
        "command": "sync",
        "mode": mode,
        "guild_id": guild_id,
        "selected_scope_count": len(selected),
        "inventory_state": inventory.get("state") if inventory else "selected",
        "results": results,
    }


def archive_status(*, guild_id: str | None = None, channel_id: str | None = None) -> list[dict[str, Any]]:
    config = load_plugin_config()
    guilds = [guild_id] if guild_id else sorted(config.allowed_guild_ids)
    if any(g not in config.allowed_guild_ids for g in guilds):
        raise RuntimeError("guild_not_allowlisted")
    conn = connect(load_secrets().database_url)
    try:
        rows: list[dict[str, Any]] = []
        for gid in guilds:
            roots = config.channels_for_guild(gid)
            params: list[Any] = [gid, list(roots), list(roots)]
            clause = "c.guild_id=%s AND (c.channel_id=ANY(%s) OR c.parent_channel_id=ANY(%s))"
            if channel_id:
                clause += " AND c.channel_id=%s"
                params.append(channel_id)
            result = conn.execute(
                "SELECT c.channel_id,c.parent_channel_id,"
                "(SELECT pc.name FROM discord_archive.channels pc WHERE pc.channel_id=c.parent_channel_id) AS parent_channel_name,"
                "c.name,c.is_thread,"
                "i.coverage_state,i.coverage_start,i.coverage_end,i.last_incremental_at,"
                "i.last_reconciled_at,count(m.message_id) FILTER (WHERE m.deleted_at IS NULL) AS live_count,"
                "max(m.created_at) FILTER (WHERE m.deleted_at IS NULL) AS newest_message_at,"
                "ok_run.finished_at AS last_successful_run,err_run.error_code AS last_error_code,"
                "greatest(0,extract(epoch FROM (now()-coalesce(i.coverage_end,max(m.created_at) FILTER (WHERE m.deleted_at IS NULL),now())))::bigint) AS lag_seconds,"
                "(i.coverage_end IS NULL OR i.coverage_end < now()-interval '48 hours') AS stale "
                "FROM discord_archive.channels c "
                "LEFT JOIN discord_archive.ingest_cursors i ON i.channel_id=c.channel_id "
                "LEFT JOIN discord_archive.messages m ON m.channel_id=c.channel_id "
                "LEFT JOIN LATERAL (SELECT finished_at FROM discord_archive.ingest_runs r "
                "WHERE r.channel_id=c.channel_id AND r.status='ok' ORDER BY r.finished_at DESC NULLS LAST LIMIT 1) ok_run ON true "
                "LEFT JOIN LATERAL (SELECT error_code FROM discord_archive.ingest_runs r "
                "WHERE r.channel_id=c.channel_id AND r.status='error' ORDER BY r.finished_at DESC NULLS LAST LIMIT 1) err_run ON true "
                f"WHERE {clause} GROUP BY c.channel_id,c.parent_channel_id,c.name,c.is_thread,"
                "i.coverage_state,i.coverage_start,i.coverage_end,i.last_incremental_at,i.last_reconciled_at,"
                "ok_run.finished_at,err_run.error_code "
                "ORDER BY c.channel_id",
                params,
            )
            columns = [col.name for col in result.description]
            for row in result.fetchall():
                item = dict(zip(columns, row))
                for key, value in list(item.items()):
                    if isinstance(value, datetime):
                        item[key] = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                rows.append(item)
        return rows
    finally:
        conn.close()


def run_channel_verification(channel_id: str) -> dict[str, Any]:
    """Export and verify one channel at its latest persisted reconciliation cutoff."""
    from .dce import DCEExporter, ExportRequest
    from .ingest import load_export
    from .verify import verify_channel

    if not channel_id.isascii() or not channel_id.isdecimal() or not 17 <= len(channel_id) <= 20:
        raise RuntimeError("invalid_channel_id")
    config = load_plugin_config()
    secrets = load_secrets()
    conn = connect(secrets.database_url)
    try:
        scope = conn.execute("""
            SELECT c.guild_id,coalesce(c.parent_channel_id,c.channel_id),r.source_before
            FROM discord_archive.channels c
            JOIN LATERAL (
              SELECT source_before FROM discord_archive.ingest_runs r
              WHERE r.channel_id=c.channel_id AND r.mode='reconcile' AND r.status='ok'
                AND r.inventory_run_id IS NOT NULL AND r.source_before IS NOT NULL
              ORDER BY r.finished_at DESC LIMIT 1
            ) r ON true WHERE c.channel_id=%s
        """, (channel_id,)).fetchone()
    finally:
        conn.close()
    if not scope:
        raise RuntimeError("verified_reconciliation_missing")
    guild_id, root_id, cutoff = str(scope[0]), str(scope[1]), scope[2]
    if guild_id not in config.allowed_guild_ids or root_id not in config.channels_for_guild(guild_id):
        raise RuntimeError("channel_not_allowlisted")
    _request, get_token = _discord_helpers()
    token = get_token()
    with tempfile.TemporaryDirectory(prefix="verify-channel-", dir=STATE_ROOT / "tmp") as temp_dir:
        output = Path(temp_dir) / f"{channel_id}.json"
        manifest = DCEExporter(DCE_BINARY, timeout=600).export(
            ExportRequest(channel_id=channel_id, output=output,
                          before=cutoff.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")),
            token,
        )
        _guild, _channel, messages = load_export(output)
        dce_messages = {
            message.message_id: (
                hashlib.sha256(message.content.encode("utf-8")).hexdigest(),
                message.created_at,
            ) for message in messages
        }
    conn = connect(secrets.database_url)
    try:
        result = verify_channel(conn, channel_id, cutoff=cutoff,
                                dce_messages=dce_messages)
    finally:
        conn.close()
    result["dce_manifest_state"] = manifest.get("state")
    result["checks"]["dce_exit_zero"] = manifest.get("state") == "ok" \
        and manifest.get("exit_code") == 0
    result["ok"] = all(result["checks"].values())
    return result


def run_denial_and_retrieval_probes() -> dict[str, bool]:
    """Exercise fail-closed authorization and a real authorized status call."""
    from gateway import session_context
    from . import tool as tool_module
    handle = tool_module.handle_discord_history
    from .retrieval import MAX_JSON_BYTES, RetrievalService

    config = load_plugin_config()
    secrets = load_secrets()
    guild_id = sorted(config.allowed_guild_ids)[0]
    owner_id = sorted(config.owner_user_ids)[0]
    root_id = sorted(config.channels_for_guild(guild_id))[0]
    arguments = {"action": "status", "guild_id": guild_id}
    connector_calls = 0
    denial_log = STATE_ROOT / "logs" / "access-denied.jsonl"
    before_denials = (len(denial_log.read_text(encoding="utf-8").splitlines())
                      if denial_log.is_file() else 0)

    def forbidden_connector(_dsn: str):
        nonlocal connector_calls
        connector_calls += 1
        raise AssertionError("database opened for denied request")

    variables = (
        session_context._SESSION_PLATFORM,
        session_context._SESSION_USER_ID,
        session_context._SESSION_CHAT_ID,
        session_context._SESSION_THREAD_ID,
    )

    def invoke(values: tuple[Any, Any, Any, Any], connector: Any,
               call_arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        tokens = [var.set(value) for var, value in zip(variables, values)]
        try:
            return json.loads(
                handle(call_arguments or arguments, config=config, secrets=secrets,
                       connector=connector)
            )
        finally:
            for var, token in reversed(list(zip(variables, tokens))):
                var.reset(token)

    denied = invoke(("discord", "00000000000000000", root_id, ""), forbidden_connector)
    wrong_platform = invoke(("local", owner_id, root_id, ""), forbidden_connector)
    empty_chat = invoke(("discord", owner_id, "", ""), forbidden_connector)
    wrong_chat = invoke(("discord", owner_id, "99999999999999999", ""), forbidden_connector)
    empty_user = invoke(("discord", "", root_id, ""), forbidden_connector)
    malformed_thread = invoke(("discord", owner_id, root_id, "١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦٧"),
                              forbidden_connector)
    disallowed_guild = invoke(("discord", owner_id, root_id, ""), forbidden_connector,
                              {"action": "status", "guild_id": "99999999999999999"})
    disallowed_channel = invoke(("discord", owner_id, root_id, ""), forbidden_connector,
                                {"action": "status", "guild_id": guild_id,
                                 "channel_ids": ["99999999999999999"]})
    original_resolver = tool_module._resolve_thread_metadata
    tool_module._resolve_thread_metadata = lambda thread_id: {
        "id": thread_id, "guild_id": guild_id,
        "parent_id": "99999999999999999", "type": 11,
    }
    try:
        wrong_thread = invoke(("discord", owner_id, root_id,
                               "88888888888888888"), forbidden_connector)
    finally:
        tool_module._resolve_thread_metadata = original_resolver
    old_platform = os.environ.get("HERMES_SESSION_PLATFORM")
    old_user = os.environ.get("HERMES_SESSION_USER_ID")
    os.environ["HERMES_SESSION_PLATFORM"] = "discord"
    os.environ["HERMES_SESSION_USER_ID"] = owner_id
    try:
        unset_context = invoke((session_context._UNSET, session_context._UNSET,
                                session_context._UNSET, session_context._UNSET),
                               forbidden_connector)
    finally:
        if old_platform is None: os.environ.pop("HERMES_SESSION_PLATFORM", None)
        else: os.environ["HERMES_SESSION_PLATFORM"] = old_platform
        if old_user is None: os.environ.pop("HERMES_SESSION_USER_ID", None)
        else: os.environ["HERMES_SESSION_USER_ID"] = old_user
    authorized = invoke(("discord", owner_id, root_id, ""), connect)
    primary = [{"message_id": f"p{index}", "match_type": "fts"}
               for index in range(50)]
    contexts = [{"message_id": f"c{index}", "match_type": "context",
                 "snippet": "x" * 320} for index in range(550)]
    bounded = RetrievalService(lambda _dsn: None, secrets.audit_hmac_key)._bound({
        "action": "search", "results": [*primary, *contexts], "coverage": {},
        "truncated": False, "omitted_context_count": 0, "omitted_result_count": 0,
    })
    bounded_ids = {str(row["message_id"]) for row in bounded["results"]}
    bounds_ok = (all(row["message_id"] in bounded_ids for row in primary)
                 and bounded["omitted_context_count"] == 550 -
                     sum(row.get("match_type") == "context" for row in bounded["results"])
                 and bounded["omitted_result_count"] == 0
                 and len(json.dumps(bounded, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")) <= MAX_JSON_BYTES)
    denial_events = ([json.loads(line) for line in
                      denial_log.read_text(encoding="utf-8").splitlines()[before_denials:]]
                     if denial_log.is_file() else [])
    denial_stat = denial_log.stat() if denial_log.is_file() else None
    denial_ok = bool(denial_stat) and (denial_stat.st_mode & 0o777) == 0o600 \
        and denial_stat.st_uid == 0 and len(denial_events) == 10 \
        and all(set(event) <= {"timestamp", "reason", "platform_present", "user_id_hmac"}
                and isinstance(event.get("timestamp"), str)
                and isinstance(event.get("reason"), str)
                and isinstance(event.get("platform_present"), bool)
                and ("user_id_hmac" not in event or
                     (isinstance(event["user_id_hmac"], str) and len(event["user_id_hmac"]) == 64))
                and owner_id not in json.dumps(event)
                for event in denial_events)
    return {
        "no_db_denial_probes": connector_calls == 0
        and denied.get("error") == "authorization_failed"
        and wrong_platform.get("error") == "authorization_failed"
        and empty_chat.get("error") == "authorization_failed"
        and wrong_chat.get("error") == "authorization_failed"
        and empty_user.get("error") == "authorization_failed"
        and malformed_thread.get("error") == "authorization_failed"
        and disallowed_guild.get("error") == "authorization_failed"
        and disallowed_channel.get("error") == "authorization_failed"
        and wrong_thread.get("error") == "authorization_failed"
        and unset_context.get("error") == "authorization_failed",
        "denial_log_checks": denial_ok,
        "retrieval_checks": authorized.get("action") == "status" and "channels" in authorized,
        "retrieval_bounds_suite": bounds_ok,
    }


def run_live_acceptance_probes(*, guild_id: str, expected_message_id: str,
                               expected_phrase: str | None = None) -> dict[str, bool]:
    """Re-export one reconciled scope and exercise owner-bound recall end to end."""
    from gateway import session_context
    from .dce import DCEExporter, ExportRequest
    from .ingest import load_export
    from .tool import handle_discord_history as handle

    config = load_plugin_config()
    if guild_id not in config.allowed_guild_ids:
        raise RuntimeError("guild_not_allowlisted")
    secrets = load_secrets()
    conn = connect(secrets.database_url)
    try:
        expected = conn.execute(
            "SELECT m.channel_id,c.parent_channel_id,left(m.content,500) "
            "FROM discord_archive.messages m "
            "JOIN discord_archive.channels c ON c.channel_id=m.channel_id "
            "WHERE m.guild_id=%s AND m.message_id=%s AND m.deleted_at IS NULL",
            (guild_id, expected_message_id),
        ).fetchone()
        if not expected:
            return {"dce_set_equality": False, "dce_hash_equality": False,
                    "reconciliation_inventory_link": False,
                    "retrieval_bounds_and_citation": False}
        channel_id, parent_id = str(expected[0]), str(expected[1] or expected[0])
        canonical_content = str(expected[2] or "")
        linked = conn.execute("""
            SELECT r.source_before,r.inventory_run_id
            FROM discord_archive.ingest_runs r
            JOIN discord_archive.ingest_runs i ON i.run_id=r.inventory_run_id
            JOIN discord_archive.inventory_parent_unions u
              ON u.run_id=i.run_id AND u.parent_channel_id=%s
            WHERE r.channel_id=%s AND r.mode='reconcile' AND r.status='ok'
              AND i.mode='inventory' AND i.status='ok' AND u.state='complete'
              AND (%s=u.parent_channel_id OR %s=ANY(u.all_thread_ids))
            ORDER BY r.finished_at DESC LIMIT 1
        """, (parent_id, channel_id, channel_id, channel_id)).fetchone()
        if not linked:
            return {"dce_set_equality": False, "dce_hash_equality": False,
                    "reconciliation_inventory_link": False,
                    "retrieval_bounds_and_citation": False}
        cutoff, inventory_run_id = linked
        db_rows = conn.execute(
            "SELECT message_id,content_hash FROM discord_archive.messages "
            "WHERE guild_id=%s AND channel_id=%s AND deleted_at IS NULL AND created_at<%s ORDER BY message_id",
            (guild_id, channel_id, cutoff),
        ).fetchall()
        db_map = {str(row[0]): str(row[1]) for row in db_rows}
    finally:
        conn.close()

    request, get_token = _discord_helpers()
    del request
    token = get_token()
    with tempfile.TemporaryDirectory(prefix="verify-e2e-", dir=STATE_ROOT / "tmp") as temp_dir:
        output = Path(temp_dir) / f"{channel_id}.json"
        manifest = DCEExporter(DCE_BINARY, timeout=600).export(
            ExportRequest(channel_id=channel_id, output=output,
                          before=cutoff.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")),
            token,
        )
        _guild, _channel, messages = load_export(output)
        dce_map = {message.message_id: hashlib.sha256(message.content.encode("utf-8")).hexdigest()
                   for message in messages}

    owner_id = sorted(config.owner_user_ids)[0]
    variables = (session_context._SESSION_PLATFORM, session_context._SESSION_USER_ID,
                 session_context._SESSION_CHAT_ID, session_context._SESSION_THREAD_ID)
    values = ("discord", owner_id, parent_id, channel_id if channel_id != parent_id else "")
    tokens = [variable.set(value) for variable, value in zip(variables, values)]
    try:
        probe_arguments = ({"action": "search", "guild_id": guild_id,
                            "query": expected_phrase or canonical_content,
                            "channel_ids": [channel_id], "limit": 10,
                            "context_before": 0, "context_after": 0}
                           if expected_phrase or canonical_content else
                           {"action": "get", "guild_id": guild_id,
                            "message_id": expected_message_id,
                            "channel_ids": [channel_id]})
        encoded = handle(probe_arguments,
                         config=config, secrets=secrets, connector=connect)
    finally:
        for variable, context_token in reversed(list(zip(variables, tokens))):
            variable.reset(context_token)
    payload = json.loads(encoded)
    expected_permalink = f"https://discord.com/channels/{guild_id}/{channel_id}/{expected_message_id}"
    hit = next((row for row in payload.get("results", [])
                if str(row.get("message_id")) == expected_message_id), None)
    coverage = payload.get("coverage") or {}
    return {
        "dce_set_equality": manifest.get("state") == "ok" and set(dce_map) == set(db_map),
        "dce_hash_equality": dce_map == db_map,
        "reconciliation_inventory_link": bool(inventory_run_id),
        "retrieval_bounds_and_citation": bool(hit)
        and hit.get("permalink") == expected_permalink
        and len(encoded.encode("utf-8")) <= 100_000
        and int(coverage.get("channel_count", 0)) >= 1,
    }
