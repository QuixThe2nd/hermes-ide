"""Objective archive and end-to-end verification contracts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence


def _linked_inventory_ok(conn: Any, channel_id: str) -> bool:
    """Require the latest successful reconcile to link to a prior complete inventory."""
    return bool(conn.execute("""
        WITH target AS (
          SELECT c.channel_id,coalesce(c.parent_channel_id,c.channel_id) AS parent_channel_id
          FROM discord_archive.channels c WHERE c.channel_id=%s
        ), reconciled AS (
          SELECT r.run_id,r.inventory_run_id,r.started_at,r.finished_at
          FROM discord_archive.ingest_runs r,target t
          WHERE r.channel_id=t.channel_id AND r.mode='reconcile' AND r.status='ok'
            AND r.inventory_run_id IS NOT NULL
          ORDER BY r.finished_at DESC LIMIT 1
        ), linked AS (
          SELECT r.*,i.finished_at AS inventory_finished_at,t.channel_id,t.parent_channel_id
          FROM reconciled r JOIN discord_archive.ingest_runs i ON i.run_id=r.inventory_run_id
          CROSS JOIN target t
          WHERE i.mode='inventory' AND i.status='ok' AND i.finished_at<=r.started_at
        )
        SELECT 1 FROM linked l
        WHERE EXISTS (
          SELECT 1 FROM discord_archive.inventory_parent_unions u
          WHERE u.run_id=l.inventory_run_id AND u.parent_channel_id=l.parent_channel_id
            AND u.state='complete'
            AND (l.channel_id=u.parent_channel_id OR l.channel_id=ANY(u.all_thread_ids))
        )
        AND NOT EXISTS (
          SELECT 1 FROM discord_archive.inventory_endpoint_manifests m
          WHERE m.run_id=l.inventory_run_id AND (
            m.state<>'complete' OR m.termination_reason='' OR
            m.page_count<>(SELECT count(*) FROM discord_archive.inventory_pages p
              WHERE p.run_id=m.run_id AND p.parent_channel_id=m.parent_channel_id
                AND p.endpoint=m.endpoint)
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM discord_archive.inventory_parent_unions u
          WHERE u.run_id=l.inventory_run_id AND (
            u.state<>'complete' OR u.all_thread_ids<>(
              SELECT ARRAY(SELECT x FROM (
                SELECT DISTINCT x FROM discord_archive.inventory_endpoint_manifests m,
                unnest(m.endpoint_thread_ids) AS t(x)
                WHERE m.run_id=u.run_id AND m.parent_channel_id=u.parent_channel_id
              ) d ORDER BY x::numeric)
            )
          )
        )
        AND (SELECT count(*) FROM discord_archive.inventory_endpoint_manifests m
             WHERE m.run_id=l.inventory_run_id)
            = 4*(SELECT count(*) FROM discord_archive.inventory_parent_unions u
                 WHERE u.run_id=l.inventory_run_id)
        AND EXISTS (
          SELECT 1 FROM discord_archive.ingest_run_scope s
          WHERE s.run_id=l.run_id AND s.channel_id=l.channel_id
            AND s.inventory_state='complete' AND s.export_state IN ('ok','empty')
        )
        AND NOT EXISTS (
          SELECT 1 FROM discord_archive.inventory_endpoint_manifests m
          WHERE m.run_id=l.inventory_run_id AND (
            m.endpoint NOT IN ('active','public','private','joined_private') OR
            m.page_count<1
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM discord_archive.inventory_pages p
          LEFT JOIN discord_archive.inventory_pages previous
            ON previous.run_id=p.run_id AND previous.parent_channel_id=p.parent_channel_id
           AND previous.endpoint=p.endpoint AND previous.page_no=p.page_no-1
          WHERE p.run_id=l.inventory_run_id AND (
            p.page_no<1 OR p.page_fingerprint='' OR
            (p.page_no=1 AND p.request_cursor IS NOT NULL) OR
            (p.page_no>1 AND p.request_cursor IS DISTINCT FROM previous.response_cursor) OR
            (p.has_more AND p.response_cursor IS NULL)
          )
        )
    """, (channel_id,)).fetchone())


def verify_channel(conn: Any, channel_id: str, *, cutoff: datetime,
                   dce_messages: Mapping[str, tuple[str, datetime]]) -> dict[str, Any]:
    """Verify persisted manifests and fixed-cutoff DCE/canonical equality."""
    exists = bool(conn.execute(
        "SELECT 1 FROM discord_archive.channels WHERE channel_id=%s", (channel_id,)
    ).fetchone())
    counts = conn.execute(
        "SELECT count(*) FILTER (WHERE deleted_at IS NULL), "
        "count(*) FILTER (WHERE deleted_at IS NOT NULL), count(*) "
        "FROM discord_archive.messages WHERE channel_id=%s", (channel_id,)
    ).fetchone()
    live, tombstones, total = map(int, counts or (0, 0, 0))
    cursor_complete = bool(conn.execute(
        "SELECT 1 FROM discord_archive.ingest_cursors WHERE channel_id=%s "
        "AND coverage_state='complete' AND last_reconciled_at IS NOT NULL",
        (channel_id,),
    ).fetchone())
    db_rows = conn.execute(
        "SELECT message_id,content_hash,created_at FROM discord_archive.messages "
        "WHERE channel_id=%s AND deleted_at IS NULL AND created_at<%s ORDER BY message_id",
        (channel_id, cutoff),
    ).fetchall()
    db_messages = {str(row[0]): (str(row[1]), row[2]) for row in db_rows}
    tombstone_ids = {str(row[0]) for row in conn.execute(
        "SELECT message_id FROM discord_archive.messages "
        "WHERE channel_id=%s AND deleted_at IS NOT NULL ORDER BY message_id",
        (channel_id,),
    ).fetchall()}
    dce_ids, db_ids = set(dce_messages), set(db_messages)
    shared = sorted(dce_ids & db_ids)
    sampled = shared[:5]
    dce_times = [value[1] for value in dce_messages.values()]
    db_times = [value[1] for value in db_messages.values()]
    checks = {
        "channel_exists": exists,
        "canonical_accounting": exists and live + tombstones == total,
        "coverage_complete": cursor_complete,
        "linked_inventory_reconciliation": exists and _linked_inventory_ok(conn, channel_id),
        "dce_live_set_equality": dce_ids == db_ids,
        "tombstones_disjoint": not (dce_ids & tombstone_ids),
        "timestamp_bounds_equal": ((not dce_times and not db_times) or
                                   (bool(dce_times) and bool(db_times) and
                                    min(dce_times) == min(db_times) and
                                    max(dce_times) == max(db_times))),
        "sampled_content_hashes_equal": all(
            dce_messages[message_id][0] == db_messages[message_id][0]
            for message_id in sampled
        ) and dce_ids == db_ids,
    }
    return {"ok": all(checks.values()), "channel_id": channel_id, "checks": checks,
            "cutoff": cutoff.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "live_count": live, "tombstone_count": tombstones, "total_count": total,
            "window_live_count": len(db_ids), "exported_count": len(dce_ids),
            "missing_from_database": sorted(dce_ids-db_ids)[:50],
            "extra_in_database": sorted(db_ids-dce_ids)[:50],
            "sampled_message_ids": sampled}


def verify_e2e(conn: Any, *, guild_id: str, owner_audit_id: str,
               expected_message_id: str, expected_phrase: str | None,
               owner_principal_hmacs: Sequence[str],
               probes: Mapping[str, bool] | Callable[[], dict[str, bool]] | None = None) -> dict[str, Any]:
    """Orchestrate persisted and independent live checks into the sole PASS/FAIL verdict."""
    checks: dict[str, bool] = {}
    versions = [int(row[0]) for row in conn.execute(
        "SELECT version FROM discord_archive.schema_migrations ORDER BY version"
    ).fetchall()]
    checks["schema_idempotence"] = bool(versions) and versions == list(range(1, max(versions) + 1)) and max(versions) >= 4
    if expected_phrase is None:
        expected = conn.execute(
            "SELECT channel_id FROM discord_archive.messages WHERE message_id=%s "
            "AND guild_id=%s AND deleted_at IS NULL",
            (expected_message_id, guild_id),
        ).fetchone()
    else:
        expected = conn.execute(
            "SELECT channel_id FROM discord_archive.messages WHERE message_id=%s AND guild_id=%s "
            "AND deleted_at IS NULL AND position(%s in content)>0",
            (expected_message_id, guild_id, expected_phrase),
        ).fetchone()
    checks["expected_live_message"] = bool(expected)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    checks["recent_owner_audit_proof"] = bool(conn.execute(
        "SELECT 1 FROM discord_archive.search_audit WHERE audit_id=%s AND platform='discord' "
        "AND action='search' AND principal_user_hmac=ANY(%s) "
        "AND outcome='ok' AND requested_at >= %s AND %s = ANY(result_message_ids)",
        (owner_audit_id, list(owner_principal_hmacs), cutoff, expected_message_id),
    ).fetchone())
    channel_id = str(expected[0]) if expected else ""
    checks["linked_inventory_reconciliation"] = bool(channel_id) and _linked_inventory_ok(conn, channel_id)
    checks["freshness"] = bool(channel_id) and bool(conn.execute(
        "SELECT 1 FROM discord_archive.ingest_cursors WHERE channel_id=%s "
        "AND coverage_state='complete' AND last_reconciled_at IS NOT NULL "
        "AND coverage_end>=now()-interval '48 hours'",
        (channel_id,),
    ).fetchone())
    if probes is None:
        checks.update({"no_db_denial_probes": False, "denial_log_checks": False,
                       "retrieval_checks": False, "dce_set_equality": False,
                       "dce_hash_equality": False,
                       "retrieval_bounds_and_citation": False})
    else:
        probe_results = probes() if callable(probes) else probes
        checks.update({str(key): bool(value) for key, value in probe_results.items()})
    failed = sorted(name for name, passed in checks.items() if not passed)
    return {"verdict": "PASS" if not failed else "FAIL", "checks": checks,
            "failed_checks": failed, "guild_id": guild_id,
            "expected_message_id": expected_message_id}
