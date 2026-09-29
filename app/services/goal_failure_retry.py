"""Bounded approval candidates and explicit failed-link rework lineage."""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from app.services.goal_binding import normalize_job_state_for_project

logger = logging.getLogger(__name__)
_MAX_RETRY = min(2, max(0, int(os.getenv("GOAL_FAILED_LINK_RETRY_MAX", "2"))))
_RETRY_MARKER = re.compile(r"^RETRY_OF_LINK: (\S+)$", re.MULTILINE)


def _evidence(value) -> dict:
    return deepcopy(json.loads(value) if isinstance(value, str) else value or {})


def _aware(value: datetime) -> datetime:
    """DB timestamp columns may be timestamp or timestamptz."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _live_candidate_sql() -> str:
    return """(
        (candidate->>'card_kind' = 'auto_queue' AND EXISTS (
          SELECT 1 FROM chat_deferred_reactions q
          JOIN chat_sessions s ON s.id = q.session_id
          WHERE q.id::text = candidate->>'card_id'
            AND s.tenant_id = g.tenant_id
            AND q.status IN ('pending', 'claimed', 'completed')
        )) OR (COALESCE(candidate->>'card_kind', 'approval') = 'approval' AND EXISTS (
          SELECT 1 FROM agent_permission_requests card
          WHERE card.id::text = candidate->>'card_id'
            AND card.tenant_id = g.tenant_id
            AND card.decision IN ('pending', 'approved')
            AND (card.expires_at IS NULL OR card.expires_at > NOW())
        ))
    )"""


async def _refresh_candidates(conn, candidates: list, tenant_id: str, now: datetime) -> bool:
    """Retire stale reservations and cards that cannot be used for a retry."""
    changed = False
    for item in candidates:
        if item.get("failed"):
            continue
        card_id = item.get("card_id")
        if card_id:
            if item.get("card_kind") == "auto_queue":
                queue = await conn.fetchrow(
                    "SELECT q.status FROM chat_deferred_reactions q "
                    "JOIN chat_sessions s ON s.id = q.session_id "
                    "WHERE q.id = $1::uuid AND s.tenant_id = $2::uuid",
                    card_id, tenant_id,
                )
                if queue and queue["status"] in {"pending", "claimed", "completed"}:
                    continue
            else:
                card = await conn.fetchrow(
                    "SELECT decision, expires_at FROM agent_permission_requests "
                    "WHERE id = $1::uuid AND tenant_id = $2::uuid",
                    card_id, tenant_id,
                )
                if card and card["decision"] in {"pending", "approved"} and (
                    card["expires_at"] is None or _aware(card["expires_at"]) > now
                ):
                    continue
        else:
            try:
                reserved_at = datetime.fromisoformat(item["at"])
            except (KeyError, TypeError, ValueError):
                reserved_at = None
            if reserved_at and now - _aware(reserved_at) < timedelta(minutes=10):
                continue
        item["failed"] = True
        item["failed_at"] = item.get("at") if not card_id else now.isoformat()
        changed = True
    return changed


async def ensure_retry_candidate(
    conn, *, milestone_id: str, goal_id: str, failed_task_id: str, reason: str,
) -> dict:
    """Reserve in a top-level transaction before calling the external proposer."""
    if not goal_id:
        return {"created": False, "why": "missing_goal_id"}
    if getattr(conn, "is_in_transaction", lambda: False)():
        logger.warning("goal_retry_candidate_deferred milestone=%s reason=outer_transaction", milestone_id)
        return {"created": False, "why": "outer_transaction"}
    reservation_id = str(uuid.uuid4())
    reserved = False
    tenant_id = None
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                """SELECT m.title, m.evidence, m.owner_session_id::text AS milestone_owner,
                          g.owner_session_id::text AS goal_owner, m.owner_role_key,
                          g.tenant_id::text AS tenant_id
                   FROM milestones m JOIN goals g ON g.id = m.goal_id
                   WHERE m.id = $1::uuid AND g.id = $2::uuid
                     AND m.tenant_id = g.tenant_id FOR UPDATE OF m""",
                milestone_id, goal_id,
            )
            if not row:
                return {"created": False, "why": "milestone_not_found"}
            tenant_id = row["tenant_id"]
            evidence = _evidence(row["evidence"])
            candidates = evidence.get("retry_candidates", [])
            now = datetime.now(UTC)
            if await _refresh_candidates(conn, candidates, tenant_id, now):
                await conn.execute(
                    "UPDATE milestones SET evidence = $2::jsonb, updated_at = NOW() "
                    "WHERE id = $1::uuid AND tenant_id = $3::uuid",
                    milestone_id, json.dumps(evidence), tenant_id,
                )
            matching = [item for item in candidates
                        if str(item.get("failed_task_id")) == failed_task_id]
            if any(item.get("card_id") and not item.get("failed") for item in matching):
                return {"created": False, "why": "already_proposed",
                        "candidate_alive": True}
            if any(not item.get("failed") for item in matching):
                return {"created": False, "why": "already_proposed",
                        "candidate_alive": False}
            failed = [item for item in matching if item.get("failed")]
            last_failed = max((datetime.fromisoformat(item["failed_at"])
                               for item in failed if item.get("failed_at")),
                              default=None)
            if len(failed) >= 3 or (last_failed and now - _aware(last_failed) < timedelta(minutes=10)):
                return {"created": False, "why": "propose_backoff"}
            # A created card consumes the bounded retry allowance permanently.
            # Failed proposal attempts without a card do not consume it.
            if sum(bool(item.get("card_id")) or not item.get("failed")
                   for item in candidates) >= _MAX_RETRY:
                await conn.execute(
                    "UPDATE milestones SET dispatch_note = $2, updated_at = NOW() "
                    "WHERE id = $1::uuid AND tenant_id = $3::uuid "
                    "AND dispatch_note IS DISTINCT FROM $2",
                    milestone_id, f"재작업 후보 {_MAX_RETRY}회 소진 — 사람이 확인해야 한다",
                    tenant_id,
                )
                return {"created": False, "why": "retry_exhausted"}

            session_id = row["milestone_owner"] or row["goal_owner"]
            if not session_id and row["owner_role_key"]:
                session_id = await conn.fetchval(
                    "SELECT id::text FROM chat_sessions WHERE role_key = $1 "
                    "AND tenant_id = $2::uuid ORDER BY created_at DESC LIMIT 1",
                    row["owner_role_key"], tenant_id,
                )
            if not session_id:
                return {"created": False, "why": "no_owner_session"}
            session_id = await conn.fetchval(
                "SELECT id::text FROM chat_sessions WHERE id = $1::uuid AND tenant_id = $2::uuid",
                str(session_id), tenant_id,
            )
            if not session_id:
                return {"created": False, "why": "no_owner_session"}
            candidates.append({
                "reservation_id": reservation_id, "at": datetime.now(UTC).isoformat(),
                "failed_task_id": failed_task_id, "retry_of_link": failed_task_id,
                "reason": reason, "card_id": None,
            })
            evidence["retry_candidates"] = candidates
            await conn.execute(
                "UPDATE milestones SET evidence = $2::jsonb, updated_at = NOW() "
                "WHERE id = $1::uuid AND tenant_id = $3::uuid",
                milestone_id, json.dumps(evidence), tenant_id,
            )
            reserved = True

        from app.services.next_step_proposals import propose
        step = {
            "title": f"[재작업 {failed_task_id}] {row['title']} — {reason}"[:200],
            "tool": "pipeline_runner_submit", "risk": "medium", "rollback": "커밋 revert",
            "detail": (
                f"실패한 job id: {failed_task_id}\n사유: {reason}\n"
                f"GOAL_ID: {goal_id}\nMILESTONE_ID: {milestone_id}\n"
                f"RETRY_OF_LINK: {failed_task_id}\n"
                "REMEDIATION_PURPOSE: failed_link_retry\n"
                "담당자는 위 메타데이터 줄을 러너 지시서에 그대로 포함하세요."
            ),
        }
        proposal = await propose(
            session_id=str(session_id), steps=[step],
            context="실패 링크 재작업 후보", tenant_id=tenant_id,
        )
        cards = proposal.get("cards") or []
        auto = [item for item in proposal.get("auto") or [] if item.get("queued")]
        card_id = cards[0]["id"] if cards else auto[0].get("queue_id") if auto else None
        card_kind = "approval" if cards else "auto_queue" if auto else None
    except Exception as exc:  # noqa: BLE001 — blocked transition must continue
        logger.warning("goal_retry_candidate_failed milestone=%s error=%s",
                       milestone_id, str(exc)[:200])
        if not reserved:
            return {"created": False, "why": "proposal_failed"}
        card_id = None
        card_kind = None

    try:
        async with conn.transaction():
            locked = await conn.fetchrow(
                "SELECT evidence FROM milestones WHERE id = $1::uuid "
                "AND tenant_id = $2::uuid FOR UPDATE",
                milestone_id, tenant_id,
            )
            if not locked:
                return {"created": False, "why": "milestone_not_found"}
            evidence = _evidence(locked["evidence"])
            for item in evidence.get("retry_candidates", []):
                if item.get("reservation_id") == reservation_id:
                    item["card_id"] = card_id
                    if card_id:
                        item["card_kind"] = card_kind
                    if not card_id:
                        item["failed"] = True
                        item["failed_at"] = datetime.now(UTC).isoformat()
                    break
            else:
                return {"created": False, "why": "reservation_missing"}
            await conn.execute(
                "UPDATE milestones SET evidence = $2::jsonb, updated_at = NOW() "
                "WHERE id = $1::uuid AND tenant_id = $3::uuid",
                milestone_id, json.dumps(evidence), tenant_id,
            )
            if card_id:
                from app.services.goal_manager import (
                    _FAILED_TASK_STATUSES,
                    active_link_predicate,
                    link_optional_columns,
                    superseded_link_predicate,
                )
                columns = await link_optional_columns(conn)
                unblocked = await conn.execute(
                    f"""UPDATE goals g SET status = 'active', updated_at = NOW()
                       WHERE g.id = $1::uuid AND g.tenant_id = $2::uuid
                         AND g.status = 'blocked'
                         AND NOT EXISTS (
                           SELECT 1 FROM milestones m
                           WHERE m.goal_id = g.id AND m.tenant_id = g.tenant_id
                             AND m.status = 'blocked'
                             AND NOT EXISTS (
                               SELECT 1 FROM jsonb_array_elements(
                                 COALESCE(m.evidence->'retry_candidates', '[]'::jsonb)
                               ) AS candidate
                               WHERE candidate->>'card_id' IS NOT NULL
                                 AND COALESCE(candidate->>'failed', 'false') <> 'true'
                                 AND {_live_candidate_sql()}
                             )
                         )
                         AND NOT EXISTS (
                           SELECT 1 FROM goal_task_links l
                           JOIN milestones m ON m.id = l.milestone_id
                             AND m.goal_id = g.id AND m.tenant_id = g.tenant_id
                           WHERE l.goal_id = g.id AND l.tenant_id = g.tenant_id
                             AND l.task_type = 'pipeline_job'
                             AND l.status = ANY($3::text[])
                             {active_link_predicate(columns, 'l')}
                             {superseded_link_predicate(columns, 'l')}
                             AND NOT EXISTS (
                               SELECT 1 FROM jsonb_array_elements(
                                 COALESCE(m.evidence->'retry_candidates', '[]'::jsonb)
                               ) AS candidate
                               WHERE candidate->>'failed_task_id' = l.task_id
                                 AND candidate->>'card_id' IS NOT NULL
                                 AND COALESCE(candidate->>'failed', 'false') <> 'true'
                                 AND {_live_candidate_sql()}
                             )
                         )""",
                    goal_id, tenant_id, list(_FAILED_TASK_STATUSES),
                )
                if unblocked == "UPDATE 1":
                    logger.info("goal_retry_goal_unblocked goal=%s milestone=%s",
                                goal_id, milestone_id)
        return {"created": bool(card_id), **(
            {"card_id": card_id} if card_id else {"why": "proposal_failed"}
        )}
    except Exception as exc:  # noqa: BLE001
        logger.warning("goal_retry_candidate_finalize_failed milestone=%s error=%s",
                       milestone_id, str(exc)[:200])
        try:
            async with conn.transaction():
                locked = await conn.fetchrow(
                    "SELECT evidence FROM milestones WHERE id = $1::uuid "
                    "AND tenant_id = $2::uuid FOR UPDATE",
                    milestone_id, tenant_id,
                )
                if locked:
                    evidence = _evidence(locked["evidence"])
                    for item in evidence.get("retry_candidates", []):
                        if item.get("reservation_id") == reservation_id and not item.get("card_id"):
                            item["failed"] = True
                            item["failed_at"] = datetime.now(UTC).isoformat()
                            await conn.execute(
                                "UPDATE milestones SET evidence = $2::jsonb, updated_at = NOW() "
                                "WHERE id = $1::uuid AND tenant_id = $3::uuid",
                                milestone_id, json.dumps(evidence), tenant_id,
                            )
                            break
        except Exception:  # stale reservation is recovered on next visit
            logger.exception("goal_retry_candidate_cleanup_failed milestone=%s", milestone_id)
        return {"created": False, "why": "proposal_failed"}


async def mark_explicit_retry_supersession(conn, milestone_id: str) -> int:
    """Supersede a recorded failed link when its marked replacement succeeds."""
    from app.services.goal_manager import active_link_predicate, link_optional_columns

    columns = await link_optional_columns(conn)
    if "superseded_by" not in columns:
        return 0
    row = await conn.fetchrow(
        """SELECT m.evidence, m.goal_id, m.tenant_id::text AS tenant_id
           FROM milestones m JOIN goals g ON g.id = m.goal_id
             AND g.tenant_id = m.tenant_id WHERE m.id = $1::uuid""",
        milestone_id,
    )
    if not row:
        return 0
    replacements = await conn.fetch(
        f"""SELECT l.task_id, l.goal_id, l.milestone_id,
                   p.tenant_id::text AS tenant_id, p.created_at,
                   p.instruction, p.status, p.phase, p.project
            FROM goal_task_links l JOIN pipeline_jobs p ON p.job_id = l.task_id
            WHERE l.milestone_id = $1::uuid AND l.task_type = 'pipeline_job'
              {active_link_predicate(columns, 'l')}""",
        milestone_id,
    )
    count = 0
    for replacement in replacements:
        if normalize_job_state_for_project(
            replacement["status"], replacement["phase"], replacement["project"],
        ) != "completed":
            continue
        marker = _RETRY_MARKER.search(replacement["instruction"] or "")
        failed_id = marker.group(1) if marker else None
        if not failed_id:
            continue
        reason = None
        if (str(replacement["goal_id"]) != str(row["goal_id"])
                or str(replacement["milestone_id"]) != milestone_id):
            reason = "link_scope_mismatch"
        elif str(replacement["tenant_id"]) != row["tenant_id"]:
            reason = "tenant_mismatch"
        elif failed_id == replacement["task_id"]:
            reason = "self_replacement"
        if reason:
            logger.warning("goal_retry_supersede_rejected milestone=%s failed=%s replacement=%s reason=%s",
                           milestone_id, failed_id, replacement["task_id"], reason)
            continue
        async with conn.transaction():
            locked = await conn.fetchrow(
                "SELECT evidence FROM milestones WHERE id = $1::uuid "
                "AND tenant_id = $2::uuid FOR UPDATE",
                milestone_id, row["tenant_id"],
            )
            evidence = _evidence(locked["evidence"]) if locked else {}
            if await _refresh_candidates(
                conn, evidence.get("retry_candidates", []), row["tenant_id"], datetime.now(UTC),
            ):
                await conn.execute(
                    "UPDATE milestones SET evidence = $2::jsonb, updated_at = NOW() "
                    "WHERE id = $1::uuid AND tenant_id = $3::uuid",
                    milestone_id, json.dumps(evidence), row["tenant_id"],
                )
            candidates = [item for item in evidence.get("retry_candidates", [])
                          if str(item.get("failed_task_id")) == failed_id
                          and item.get("card_id") and not item.get("failed")]
            if not candidates:
                reason = "candidate_missing"
            else:
                candidate = max(candidates, key=lambda item: item.get("at") or "")
                try:
                    proposed_at = datetime.fromisoformat(candidate["at"])
                    created_at = replacement["created_at"]
                    if isinstance(created_at, str):
                        created_at = datetime.fromisoformat(created_at)
                    if _aware(created_at) <= _aware(proposed_at):
                        reason = "created_before_candidate"
                except (KeyError, TypeError, ValueError):
                    reason = "invalid_candidate_time"
            if reason:
                logger.warning("goal_retry_supersede_rejected milestone=%s failed=%s replacement=%s reason=%s",
                               milestone_id, failed_id, replacement["task_id"], reason)
                continue
            superseded_at = ", superseded_at = NOW()" if "superseded_at" in columns else ""
            result = await conn.execute(
                f"""UPDATE goal_task_links l SET superseded_by = $4{superseded_at}
                    WHERE l.milestone_id = $1::uuid AND l.goal_id = $2::uuid
                      AND l.tenant_id = $3::uuid AND l.task_type = 'pipeline_job'
                      AND l.task_id = $5 AND l.status = 'failed'
                      AND l.superseded_by IS NULL
                      {active_link_predicate(columns, 'l')}""",
                milestone_id, row["goal_id"], row["tenant_id"],
                replacement["task_id"], failed_id,
            )
            if result == "UPDATE 1":
                candidate["superseded_by_task_id"] = replacement["task_id"]
                await conn.execute(
                    "UPDATE milestones SET evidence = $2::jsonb, updated_at = NOW() "
                    "WHERE id = $1::uuid AND tenant_id = $3::uuid",
                    milestone_id, json.dumps(evidence), row["tenant_id"],
                )
                count += 1
                logger.info("goal_retry_link_superseded milestone=%s failed=%s replacement=%s",
                            milestone_id, failed_id, replacement["task_id"])
    return count
