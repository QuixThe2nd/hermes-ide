"""Goal-bound assistant missions for messaging contacts and group chats.

The ``missions`` plugin lets the agent dispatch a "mission": a stated goal
bound to a specific WhatsApp chat. A DM mission binds the contact's JID and
every inbound message from that contact lands in that contact's own gateway
session; a group mission binds the group JID (``...@g.us``) and every member
message lands in ONE shared group session. In both cases the session-context
renderer injects an **Active Mission** section into that session's system
prompt, and — once the goal condition is met — the agent calls
``end_session`` to record the outcome and wake the dispatching thread.

Design notes:
- No extra process: inbound WhatsApp messages are already routed to isolated
  per-chat sessions by the gateway; the mission is just prompt state.
- Prompt-cache safe: mission state rides the pinned session-context change
  key (``_ephemeral_change_key`` in gateway/run.py) via
  ``active_mission_digest``, so adding/completing a mission busts the pinned
  context block exactly once for exactly the affected sessions.
- Identity: DM missions are keyed on ``canonical_whatsapp_identifier()`` so
  the bookkeeping survives phone-JID/LID alias flips, matching Hermes' own
  session-key identity. Group missions are keyed on the EXACT group chat id —
  ``canonical_whatsapp_identifier()`` strips the ``@g.us`` domain, so any
  alias-style matching would risk cross-chat confusion; a group JID is stable
  so exact matching is both sufficient and safer.
- Group missions never touch the DM pairing store, the global allowlist, or
  member DM history: authorization is by the group chat id alone (admitted
  while the mission is active by WhatsApp intake and gateway authz), and the
  shared group session starts fresh rather than copying any member's DM.

Canonical store: one JSON file per active mission under
``$HERMES_HOME/missions/`` plus an append-only JSONL outcome journal.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_process_hermes_home

logger = logging.getLogger(__name__)

_ALLOWED_ACTIONS = {"start", "status", "complete", "cancel"}
_MAX_GOAL = 2_000
_MAX_PERSONA = 2_000
_MAX_OUTCOME = 4_000

# Outcome journal cap: keep the last N completed/cancelled outcomes so the
# store cannot grow unbounded.
_OUTCOME_JOURNAL_MAX = 500


def _missions_dir() -> Path:
    # NOTE(future-me): missions must be visible across profiles — a mission is
    # created from the default profile but answered by the routed assistant
    # profile under _profile_runtime_scope (which redirects get_hermes_home()).
    # Anchor on the PROCESS home so the store stays shared.
    d = get_process_hermes_home() / "missions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _mission_path(mission_id: str) -> Path:
    return _missions_dir() / f"mission-{mission_id}.json"


def _outcome_journal_path() -> Path:
    return _missions_dir() / "outcomes.jsonl"


def _load_mission(mission_id: str) -> Optional[Dict[str, Any]]:
    path = _mission_path(mission_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("missions: failed to read %s: %s", path.name, exc)
        return None


def _save_mission(mission: Dict[str, Any]) -> None:
    from utils import atomic_replace

    path = _mission_path(str(mission["mission_id"]))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(mission, indent=2, sort_keys=True), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    atomic_replace(tmp, path)


def list_active_missions() -> List[Dict[str, Any]]:
    """Return all active missions (oldest first)."""
    out = []
    try:
        paths = sorted(_missions_dir().glob("mission-*.json"))
    except OSError:
        return []
    for p in paths:
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(m, dict) and m.get("status") == "active":
                out.append(m)
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda m: str(m.get("created_at") or ""))
    return out


def _is_group_chat_id(chat_id: str) -> bool:
    """True for a WhatsApp group chat id (JID ending ``@g.us``)."""
    return str(chat_id or "").strip().lower().endswith("@g.us")


def _mission_chat_type(mission: Dict[str, Any]) -> str:
    """Classify a mission as ``group`` or ``dm``.

    Missions created before group support have no ``chat_type``; one whose
    chat id is a group JID is treated as a group mission so those records
    keep working.
    """
    declared = str(mission.get("chat_type") or "").strip().lower()
    if declared == "group":
        return "group"
    if not declared and _is_group_chat_id(mission.get("chat_id")):
        return "group"
    return "dm"


def find_active_group_mission(chat_id: str) -> Optional[Dict[str, Any]]:
    """Return the active GROUP mission bound to exactly *chat_id*, if any.

    Group missions authorize and route by the exact group chat id — never by
    a participant's ``user_id`` and never through canonical/alias matching
    (which strips the ``@g.us`` domain). Used by WhatsApp intake, gateway
    authorization, and session-key construction.
    """
    if not chat_id:
        return None
    needle = str(chat_id).strip()
    for m in list_active_missions():
        if _mission_chat_type(m) != "group":
            continue
        if str(m.get("chat_id") or "").strip() == needle:
            return m
    return None


def find_active_mission_for_chat(chat_id: str) -> Optional[Dict[str, Any]]:
    """Return the active mission bound to *chat_id* (any platform), if any.

    DM matching is done on the canonical identity so JID/LID flips do not
    orphan a mission; non-WhatsApp identifiers match verbatim. Group chats
    (``@g.us``) match only group missions, and only on the exact chat id.
    """
    if not chat_id:
        return None
    if _is_group_chat_id(chat_id):
        return find_active_group_mission(chat_id)
    canon = _canonical(chat_id)
    for m in list_active_missions():
        if _mission_chat_type(m) == "group":
            # Group missions bind the group chat only — a canonical DM id
            # must never match one (the canonicalizer strips ``@g.us``).
            continue
        targets = {_canonical(str(t)) for t in (m.get("chat_id"), *(m.get("aliases") or []))}
        if canon and canon in {t for t in targets if t}:
            return m
        # Degraded mode (no canonical mapping available): exact match.
        if not canon and chat_id in {m.get("chat_id"), *(m.get("aliases") or [])}:
            return m
    return None


def _canonical(identifier: str) -> str:
    try:
        from gateway.whatsapp_identity import canonical_whatsapp_identifier

        return canonical_whatsapp_identifier(identifier)
    except Exception:
        return identifier.strip().lower()


def _grant_mission_pairing(mission: Dict[str, Any]) -> None:
    """Pre-approve the contact on the serving profile's pairing store.

    The WhatsApp bridge only forwards unknown DMs when dm_policy=pairing;
    the gateway then checks the pairing store. Granting here lets a
    dispatched contact talk to the assistant without a pairing-code dance,
    scoped to the mission's profile so the default profile is untouched.
    """
    try:
        from gateway.pairing import PairingStore
    except Exception as exc:
        logger.warning("missions: pairing grant skipped (import): %s", exc)
        return
    platform = str(mission.get("platform") or "whatsapp")
    chat_id = str(mission.get("chat_id") or "")
    name = str(mission.get("chat_name") or "")
    profile = str(mission.get("profile") or "assistant")
    if not chat_id:
        return
    try:
        store = PairingStore(profile=profile)
        with store._lock:
            store._approve_user(platform, chat_id, name)
            # LID/JID aliases so inbound user_id matches even if the
            # stored chat_id was the other form.
            try:
                from gateway.whatsapp_identity import expand_whatsapp_aliases

                for alias in expand_whatsapp_aliases(chat_id) or []:
                    if alias and alias != chat_id:
                        store._approve_user(platform, alias, name)
            except Exception:
                logger.debug("missions: alias grant skipped", exc_info=True)
        logger.info(
            "missions: granted pairing for %s on profile %s", chat_id, profile
        )
    except Exception as exc:
        logger.warning("missions: pairing grant failed for %s: %s", chat_id, exc)


def _revoke_mission_pairing(mission: Dict[str, Any]) -> None:
    """Drop the contact from the serving profile's pairing store."""
    try:
        from gateway.pairing import PairingStore
    except Exception as exc:
        logger.warning("missions: pairing revoke skipped (import): %s", exc)
        return
    platform = str(mission.get("platform") or "whatsapp")
    chat_id = str(mission.get("chat_id") or "")
    profile = str(mission.get("profile") or "assistant")
    if not chat_id:
        return
    try:
        store = PairingStore(profile=profile)
        store.revoke(platform, chat_id)
        try:
            from gateway.whatsapp_identity import expand_whatsapp_aliases

            for alias in expand_whatsapp_aliases(chat_id) or []:
                if alias and alias != chat_id:
                    store.revoke(platform, alias)
        except Exception:
            logger.debug("missions: alias revoke skipped", exc_info=True)
        for extra in mission.get("aliases") or []:
            extra_s = str(extra or "").strip()
            if extra_s and extra_s != chat_id:
                try:
                    store.revoke(platform, extra_s)
                except Exception:
                    logger.debug("missions: extra alias revoke skipped", exc_info=True)
        logger.info(
            "missions: revoked pairing for %s on profile %s", chat_id, profile
        )
    except Exception as exc:
        logger.warning("missions: pairing revoke failed for %s: %s", chat_id, exc)


def _append_outcome(entry: Dict[str, Any]) -> None:
    path = _outcome_journal_path()
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        # Bound the journal.
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > _OUTCOME_JOURNAL_MAX:
            path.write_text("\n".join(lines[-_OUTCOME_JOURNAL_MAX:]) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("missions: failed to append outcome journal: %s", exc)



def _notify_origin_session(mission: Dict[str, Any]) -> bool:
    """Wake the dispatching session the same way delegate_task does.

    Pushes an async_delegation-shaped event onto process_registry.completion_queue
    so the origin Discord/CLI turn is resumed with the outcome. No Discord tool
    is required on the assistant profile.
    """
    session_key = str(mission.get("created_by_session") or "").strip()
    parent_session_id = str(
        mission.get("origin_parent_session_id") or mission.get("origin_session_id") or ""
    ).strip()
    if not session_key and not parent_session_id:
        logger.warning(
            "missions: no origin session on %s; outcome not queued",
            mission.get("mission_id"),
        )
        return False
    chat = mission.get("chat_name") or mission.get("chat_id") or "contact"
    summary = (
        f"WhatsApp assistant mission {mission.get('mission_id')} "
        f"{mission.get('status')} with {chat}.\n"
        f"Goal: {mission.get('goal')}\n"
        f"Outcome: {mission.get('outcome') or '(none)'}"
    )
    evt = {
        "type": "async_delegation",
        "delegation_id": f"mission-{mission.get('mission_id')}",
        "session_key": session_key,
        "origin_session_id": str(mission.get("origin_session_id") or ""),
        "parent_session_id": parent_session_id,
        "goal": mission.get("goal") or "",
        "status": "completed" if mission.get("status") == "completed" else "error",
        "summary": summary,
        "error": None if mission.get("status") == "completed" else (mission.get("outcome") or mission.get("status")),
        "completed_at": time.time(),
        "dispatched_at": time.time(),
        "mission_id": mission.get("mission_id"),
        "reply_target": mission.get("reply_target") or "",
    }
    try:
        from tools.process_registry import process_registry

        process_registry.completion_queue.put(evt)
        logger.info(
            "missions: queued outcome for %s to session %s",
            mission.get("mission_id"),
            session_key or parent_session_id,
        )
        return True
    except Exception as exc:
        logger.warning("missions: failed to queue outcome: %s", exc)
        return False


def _error(code: str, msg: str) -> str:
    return json.dumps({"ok": False, "error": code, "message": msg})


# Cap seeded transcript so a huge default-profile DM cannot blow the
# assistant session on first dispatch. Oldest turns drop first.
_MAX_SEEDED_MESSAGES = 1000


def _contact_match_tokens(chat_id: str) -> set:
    """Phone/LID/JID forms that all identify the same WhatsApp contact."""
    tokens = {str(chat_id or "").strip()}
    canon = _canonical(chat_id)
    if canon:
        tokens.add(canon)
    try:
        from gateway.whatsapp_identity import (
            expand_whatsapp_aliases,
            normalize_whatsapp_identifier,
        )

        tokens |= set(expand_whatsapp_aliases(chat_id) or [])
        normalized = normalize_whatsapp_identifier(chat_id)
        if normalized:
            tokens.add(normalized)
    except Exception:
        logger.debug("missions: alias expand skipped", exc_info=True)
    return {t for t in tokens if t}


def _assistant_session_key(canon: str) -> str:
    return f"agent:assistant:whatsapp:dm:{canon}"


def _find_prior_whatsapp_dm(default_db, tokens):
    """Return the latest default-profile WhatsApp DM for this contact.

    ``find_latest_gateway_session_for_peer`` misses compression-ended rows
    and unkeyed continuation tips. Scan matching DMs, skip assistant keys
    and groups, then walk ``get_compression_tip`` so the seeded chat is the
    live long thread, not a stale compressed ancestor.
    """
    token_list = [t for t in tokens if t]
    if not token_list:
        return None
    clauses = []
    params: list = []
    for token in token_list:
        like = f"%{token}%"
        clauses.append(
            "(COALESCE(session_key,'') LIKE ? OR COALESCE(chat_id,'') LIKE ? "
            "OR COALESCE(user_id,'') LIKE ?)"
        )
        params.extend([like, like, like])
    sql = (
        "SELECT * FROM sessions WHERE source = 'whatsapp' "
        "AND COALESCE(chat_type, 'dm') = 'dm' "
        f"AND ({' OR '.join(clauses)}) "
        "AND COALESCE(session_key, '') NOT LIKE '%:group:%' "
        "AND COALESCE(chat_id, '') NOT LIKE '%@g.us' "
        "AND COALESCE(session_key, '') NOT LIKE 'agent:assistant:%' "
        "ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 20"
    )
    try:
        with default_db._lock:
            rows = default_db._conn.execute(sql, params).fetchall()
            decoded = [default_db._session_row_dict(r) for r in rows]
    except Exception:
        logger.debug("missions: prior-dm scan failed", exc_info=True)
        decoded = []
    if not decoded:
        for token in sorted(token_list, key=lambda t: (len(t), t)):
            try:
                row = default_db.find_latest_gateway_session_for_peer(
                    source="whatsapp",
                    session_key=f"agent:main:whatsapp:dm:{token}",
                )
            except Exception:
                row = None
            if row:
                decoded.append(row)
    if not decoded:
        return None
    best = decoded[0]
    try:
        tip_id = default_db.get_compression_tip(str(best.get("id") or ""))
    except Exception:
        tip_id = None
    if tip_id and tip_id != best.get("id"):
        try:
            with default_db._lock:
                tip_row = default_db._conn.execute(
                    "SELECT * FROM sessions WHERE id = ?", (tip_id,)
                ).fetchone()
            if tip_row:
                return default_db._session_row_dict(tip_row)
        except Exception:
            logger.debug("missions: compression tip lookup failed", exc_info=True)
    return best


def _identity_from_prior(db, prior: Dict[str, Any]) -> Dict[str, str]:
    """Prefer identity on *prior*, then walk compression ancestors for a keyed row."""
    chat_jid = str(prior.get("chat_id") or "").strip()
    user_id = str(prior.get("user_id") or "").strip()
    display_name = str(prior.get("display_name") or "").strip()
    if chat_jid and user_id:
        return {"chat_id": chat_jid, "user_id": user_id, "display_name": display_name}
    try:
        lineage = db._session_lineage_root_to_tip(str(prior.get("id") or ""))
    except Exception:
        lineage = [str(prior.get("id") or "")]
    try:
        with db._lock:
            for sid in reversed(lineage):
                if not sid:
                    continue
                row = db._conn.execute(
                    "SELECT chat_id, user_id, display_name FROM sessions WHERE id = ?",
                    (sid,),
                ).fetchone()
                if not row:
                    continue
                c = str(row["chat_id"] or "").strip()
                u = str(row["user_id"] or "").strip()
                n = str(row["display_name"] or "").strip()
                if c and not chat_jid:
                    chat_jid = c
                if u and not user_id:
                    user_id = u
                if n and not display_name:
                    display_name = n
                if chat_jid and user_id:
                    break
    except Exception:
        logger.debug("missions: lineage identity lookup failed", exc_info=True)
    return {"chat_id": chat_jid, "user_id": user_id, "display_name": display_name}


def _seedable_turns(conversation):
    """Keep user/assistant text so the seeded chat reads as one thread.

    Tool-only rows (empty content) are dropped. Assistant turns that also
    made tool calls still keep their visible text.
    """
    out = []
    for msg in conversation or []:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        out.append(
            {
                "role": role,
                "content": content,
                "timestamp": msg.get("timestamp"),
            }
        )
    if len(out) > _MAX_SEEDED_MESSAGES:
        return out[-_MAX_SEEDED_MESSAGES:]
    return out


def _seed_contact_history(mission: Dict[str, Any]) -> None:
    """Copy the contact's existing WhatsApp DM into the assistant session.

    Missions are tickets at the prompt layer (goal + persona). The human
    chat should still read as one long thread, so the assistant profile
    resumes the default-profile DM instead of starting empty.
    """
    chat_id = str(mission.get("chat_id") or "").strip()
    profile = str(mission.get("profile") or "assistant").strip() or "assistant"
    if not chat_id:
        return
    # DM-only by contract: a group mission shares one fresh session and must
    # not copy any member's private DM history into it.
    if _mission_chat_type(mission) == "group":
        return
    tokens = _contact_match_tokens(chat_id)
    if not tokens:
        return
    canon = _canonical(chat_id) or next(iter(tokens))
    try:
        from hermes_state import SessionDB
    except Exception as exc:
        logger.warning("missions: history seed skipped (import): %s", exc)
        return

    # Gateway inbound loads history from the process home under
    # agent:assistant:whatsapp:dm:<canon> (default handler scope). Seeding
    # the assistant-profile store would never be replayed.
    db_path = get_process_hermes_home() / "state.db"
    if not db_path.exists():
        return
    db = SessionDB(db_path=db_path)
    session_key = _assistant_session_key(canon)
    display_name = str(mission.get("chat_name") or "").strip()
    chat_jid = chat_id
    user_id = chat_id
    try:
        prior = _find_prior_whatsapp_dm(db, tokens)
        prior_turns = []
        if prior:
            prior_turns = _seedable_turns(
                db.get_messages_as_conversation(
                    str(prior["id"]),
                    include_ancestors=True,
                )
            )
            ident = _identity_from_prior(db, prior)
            if ident.get("chat_id"):
                chat_jid = ident["chat_id"]
            if ident.get("user_id"):
                user_id = ident["user_id"]
            display_name = display_name or ident.get("display_name") or ""
        if not prior_turns:
            return

        existing = db.find_latest_gateway_session_for_peer(
            source="whatsapp",
            session_key=session_key,
        )
        if existing and str(existing.get("id")) == str(prior.get("id") if prior else ""):
            # Do not copy a session onto itself (live rewrite of the current DM).
            return
        if existing:
            sid = str(existing["id"])
            db.record_gateway_session_peer(
                sid,
                source="whatsapp",
                user_id=user_id,
                session_key=session_key,
                chat_id=chat_jid,
                chat_type="dm",
                display_name=display_name,
            )
            existing_convo = db.get_messages_as_conversation(sid)
        else:
            sid = f"mission-{mission['mission_id']}"
            db.create_session(
                sid,
                "whatsapp",
                session_key=session_key,
                chat_id=chat_jid,
                chat_type="dm",
                user_id=user_id,
                display_name=display_name,
                profile_name=profile,
            )
            existing_convo = []

        seen = {
            (m.get("role"), m.get("content"))
            for m in existing_convo
            if m.get("role") in ("user", "assistant")
        }
        missing = [
            t for t in prior_turns if (t["role"], t["content"]) not in seen
        ]
        if not missing:
            return
        if existing_convo:
            db.replace_messages(sid, missing + existing_convo, active_only=True)
        else:
            for msg in missing:
                db.append_message(
                    sid,
                    msg["role"],
                    msg["content"],
                    timestamp=msg.get("timestamp"),
                )
        logger.info(
            "missions: seeded %s prior turns into %s for %s",
            len(missing),
            session_key,
            chat_id,
        )
    except Exception as exc:
        logger.warning("missions: history seed failed: %s", exc)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _handle_start(args: Dict[str, Any], **kwargs: Any) -> str:
    chat_id = str(args.get("chat_id") or "").strip()
    goal = str(args.get("goal") or "").strip()
    persona = str(args.get("persona_instructions") or "").strip()
    reply_to = str(
        args.get("reply_to") or kwargs.get("reply_to") or ""
    ).strip()
    profile = str(
        args.get("profile")
        or os.environ.get("HERMES_MISSION_PROFILE")
        or "assistant"
    ).strip()

    if not chat_id:
        return _error("missing_chat_id", "chat_id (contact's WhatsApp JID/phone, or a group JID ending @g.us) is required.")
    if not goal:
        return _error("missing_goal", "goal is required.")

    existing = find_active_mission_for_chat(chat_id)
    if existing:
        return _error(
            "mission_exists",
            (
                f"An active mission {existing['mission_id']} already exists for this "
                f"chat. Complete or cancel it first, or use action='status'."
            ),
        )

    # A WhatsApp group JID (``...@g.us``) makes this a GROUP mission: every
    # member message shares one session, admission is by the group chat id,
    # and no pairing grant or DM-history seed happens (a group is not a DM
    # contact — approving it on a DM pairing store would be nonsense, and no
    # single member's DM history speaks for the group).
    chat_type = "group" if _is_group_chat_id(chat_id) else "dm"

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mission_id = uuid.uuid4().hex[:12]
    mission: Dict[str, Any] = {
        "mission_id": mission_id,
        "status": "active",
        "platform": str(args.get("platform") or "whatsapp"),
        "chat_id": chat_id,
        "chat_type": chat_type,
        "chat_name": str(args.get("chat_name") or "").strip(),
        "goal": goal[:_MAX_GOAL],
        "persona_instructions": persona[:_MAX_PERSONA],
        "reply_target": reply_to,  # where the outcome report should be posted
        # Serving profile: inbound chats with an active mission are routed to
        # this profile (scoped tools/credentials/persona) by run.py.
        "profile": profile,
        "created_at": now,
        "created_by_session": str(kwargs.get("session_key") or ""),
        "origin_session_id": str(kwargs.get("session_id") or ""),
        "origin_parent_session_id": str(
            kwargs.get("parent_session_id") or kwargs.get("session_id") or ""
        ),
        "completed_at": None,
        "outcome": None,
    }
    _save_mission(mission)
    if chat_type == "dm":
        _grant_mission_pairing(mission)
        try:
            _seed_contact_history(mission)
        except Exception as exc:
            logger.warning("missions: history seed failed: %s", exc)
    who = (
        f"the group {mission['chat_name'] or chat_id}"
        if chat_type == "group"
        else mission["chat_name"] or chat_id
    )
    return json.dumps(
        {
            "ok": True,
            "mission_id": mission_id,
            "status": "active",
            "chat_type": chat_type,
            "message": (
                "Mission dispatched. Inbound messages from "
                f"{who} will now be answered while working "
                "toward the goal; call action='complete' with the outcome once the "
                "goal is hit."
            ),
        },
        indent=2,
    )


def _handle_status(args: Dict[str, Any], **kwargs: Any) -> str:
    mission_id = str(args.get("mission_id") or "").strip()
    if mission_id:
        m = _load_mission(mission_id)
        if not m:
            return _error("not_found", f"No mission {mission_id}.")
        return json.dumps({"ok": True, "mission": m}, indent=2)
    active = list_active_missions()
    recent: List[Dict[str, Any]] = []
    journal = _outcome_journal_path()
    try:
        for line in journal.read_text(encoding="utf-8").splitlines()[-10:]:
            try:
                recent.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return json.dumps({"ok": True, "active_missions": active, "recent_outcomes": recent}, indent=2)


def _close_mission(
    args: Dict[str, Any], new_status: str, require_outcome: bool, **kwargs: Any
) -> str:
    mission_id = str(args.get("mission_id") or "").strip()
    outcome = str(args.get("outcome") or "").strip()

    mission = None
    if mission_id:
        mission = _load_mission(mission_id)
    else:
        # Close by chat when only one mission exists for it.
        chat_id = str(args.get("chat_id") or "").strip()
        if chat_id:
            mission = find_active_mission_for_chat(chat_id)
        elif len(list_active_missions()) == 1:
            mission = list_active_missions()[0]

    if not mission:
        return _error(
            "not_found",
            "Provide mission_id (or chat_id) to identify the mission. Use action='status' to list.",
        )
    if mission.get("status") != "active":
        return _error("already_closed", f"Mission {mission['mission_id']} is {mission.get('status')}.")
    if require_outcome and not outcome:
        return _error("missing_outcome", "outcome summary is required when completing a mission.")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mission["status"] = new_status
    mission["completed_at"] = now
    mission["outcome"] = outcome[:_MAX_OUTCOME] or None
    _save_mission(mission)

    _append_outcome(
        {
            "mission_id": mission["mission_id"],
            "platform": mission.get("platform"),
            "chat_id": mission.get("chat_id"),
            "chat_name": mission.get("chat_name"),
            "status": new_status,
            "goal": mission.get("goal"),
            "outcome": mission.get("outcome"),
            "reply_target": mission.get("reply_target"),
            "completed_at": now,
        }
    )
    _revoke_mission_pairing(mission)
    delivered = _notify_origin_session(mission)
    target = mission.get("reply_target") or mission.get("created_by_session") or "the dispatching thread"
    return json.dumps(
        {
            "ok": True,
            "mission_id": mission["mission_id"],
            "status": new_status,
            "notified": delivered,
            "message": (
                f"Mission closed as {new_status}. "
                + (
                    f"Outcome queued to {target}."
                    if delivered
                    else f"Outcome recorded; notify of {target} failed."
                )
            ),
        },
        indent=2,
    )


def handle_dispatch_agent(args: Dict[str, Any], **kwargs: Any) -> str:
    action = str(args.get("action") or "").strip().lower()
    if action == "start":
        return _handle_start(args, **kwargs)
    if action == "status":
        return _handle_status(args, **kwargs)
    if action == "complete":
        return _close_mission(args, "completed", require_outcome=True, **kwargs)
    if action == "cancel":
        return _close_mission(args, "cancelled", require_outcome=False, **kwargs)
    return _error("invalid_input", f"action must be one of {sorted(_ALLOWED_ACTIONS)}")


MISSIONS_SCHEMA = {
    "name": "dispatch_agent",
    "description": (
        "Dispatch and manage background assistant-missions bound to a messaging "
        "chat. action='start' binds a goal to a contact's DM or a group chat "
        "(chat_id ending @g.us): from then on you answer that chat's messages as "
        "the assistant working toward the goal until it is hit, then call "
        "action='complete' with an outcome summary (which should also be delivered "
        "to the reply target). Group missions are answered in one shared session "
        "for all members. Use action='status' to list active missions and recent "
        "outcomes, action='cancel' to abort without an outcome. One active mission "
        "per chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ALLOWED_ACTIONS),
                "description": "Operation to perform.",
            },
            "chat_id": {
                "type": "string",
                "description": (
                    "Chat identifier for start (WhatsApp JID like "
                    "61491234567@s.whatsapp.net, bare phone number, LID, or a "
                    "group JID ending @g.us for a group mission); alternative "
                    "mission locator for complete/cancel."
                ),
            },
            "chat_name": {
                "type": "string",
                "description": "Human-readable name of the contact or group (start only).",
            },
            "goal": {
                "type": "string",
                "description": "The concrete goal to achieve in the conversation, including any constraints (start only).",
            },
            "persona_instructions": {
                "type": "string",
                "description": "How to present: e.g. 'you are Parsa's casual assistant arranging his weekend' (start only).",
            },
            "reply_to": {
                "type": "string",
                "description": (
                    "Where the outcome report should be delivered, e.g. "
                    "'discord:guild:1541444732641878148' or a channel id. Defaults to "
                    "the current conversation when started from a messaging session "
                    "(start only)."
                ),
            },
            "profile": {
                "type": "string",
                "description": (
                    "Serving profile for the mission's chat (start only). The "
                    "contact's messages are answered by this profile with its "
                    "scoped tool whitelist and persona. Default: 'assistant'."
                ),
            },
            "mission_id": {
                "type": "string",
                "description": "Identifier returned by start; identifies the mission for status/complete/cancel.",
            },
            "outcome": {
                "type": "string",
                "description": "Final result summary: what was agreed/achieved, remaining caveats (required for complete).",
            },
        },
        "required": ["action"],
    },
}


def check_requirements() -> bool:
    try:
        _missions_dir()
        return True
    except OSError:
        return False


DISPATCH_ASSISTANT_SCHEMA = {
    "name": "dispatch_assistant",
    "description": (
        "Start a sandboxed WhatsApp assistant conversation with a contact or "
        "group. You (the main thread) keep this tool. The chat is answered by "
        "the locked-down assistant profile until the goal is hit; complete/"
        "cancel then wakes THIS session with the outcome. Do not put the "
        "contact on WHATSAPP_ALLOWED_USERS. One active mission per chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": (
                    "Contact WhatsApp JID (61491234567@s.whatsapp.net), bare "
                    "phone, LID, or group JID ending @g.us."
                ),
            },
            "chat_name": {
                "type": "string",
                "description": "Human-readable name of the contact or group.",
            },
            "goal": {
                "type": "string",
                "description": "What the assistant must achieve before closing.",
            },
            "persona_instructions": {
                "type": "string",
                "description": "How the assistant should present in that chat.",
            },
            "profile": {
                "type": "string",
                "description": "Serving profile. Default: assistant.",
            },
        },
        "required": ["chat_id", "goal"],
    },
}


END_SESSION_SCHEMA = {
    "name": "end_session",
    "description": (
        "Close this WhatsApp assistant conversation. Call when the goal is "
        "met (or impossible). The origin Discord thread is woken with the "
        "outcome. You do not have dispatch_agent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "description": (
                    "What was agreed or done, plus any remaining caveats."
                ),
            },
        },
        "required": ["outcome"],
    },
}


def _chat_id_from_session_key(session_key: str) -> str:
    """Best-effort chat id from a gateway session key.

    WhatsApp keys look like ``agent:<profile>:whatsapp:dm:<phone-or-jid>``
    for DMs and ``agent:<profile>:whatsapp:group:<group-jid>`` for the shared
    group-mission session — both carry the chat id in slot 4.
    """
    parts = [p for p in str(session_key or "").split(":") if p]
    if len(parts) >= 5 and parts[2] == "whatsapp":
        return parts[4]
    return parts[-1] if len(parts) >= 5 else ""


def _current_chat_id(args: Dict[str, Any], **kwargs: Any) -> str:
    chat_id = str(args.get("chat_id") or "").strip()
    if chat_id:
        return chat_id
    session_key = str(kwargs.get("session_key") or "").strip()
    if not session_key:
        try:
            from tools.approval import get_current_session_key

            session_key = str(get_current_session_key(default="") or "").strip()
        except Exception:
            session_key = ""
    if not session_key:
        session_key = str(os.environ.get("HERMES_SESSION_KEY") or "").strip()
    return _chat_id_from_session_key(session_key)


def handle_end_session(args: Dict[str, Any], **kwargs: Any) -> str:
    """Assistant-side close. Completes the active mission for this chat."""
    close_args = dict(args or {})
    chat_id = _current_chat_id(close_args, **kwargs)
    if chat_id and not str(close_args.get("chat_id") or "").strip():
        close_args["chat_id"] = chat_id
    return _close_mission(close_args, "completed", require_outcome=True, **kwargs)


def handle_dispatch_assistant(args: Dict[str, Any], **kwargs: Any) -> str:
    """Origin-side start. Forces action=start and captures this session as reply target."""
    start_args = dict(args or {})
    start_args["action"] = "start"
    if not str(start_args.get("reply_to") or "").strip():
        # Prefer the live gateway session so completion_queue can wake us.
        start_args["reply_to"] = str(kwargs.get("session_key") or "")
    return _handle_start(start_args, **kwargs)


def register(ctx) -> None:
    ctx.register_tool(
        name="dispatch_agent",
        toolset="missions",
        schema=MISSIONS_SCHEMA,
        handler=handle_dispatch_agent,
        check_fn=check_requirements,
        emoji="\U0001F3AF",
    )
    ctx.register_tool(
        name="dispatch_assistant",
        toolset="missions",
        schema=DISPATCH_ASSISTANT_SCHEMA,
        handler=handle_dispatch_assistant,
        check_fn=check_requirements,
        emoji="\U0001F4AC",
    )
    ctx.register_tool(
        name="end_session",
        toolset="end_session",
        schema=END_SESSION_SCHEMA,
        handler=handle_end_session,
        check_fn=check_requirements,
        emoji="\U0001F6D1",
    )
