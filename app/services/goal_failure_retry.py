"""Bounded approval candidates and explicit failed-link rework lineage."""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from app.services.goal_binding import DONE_JOB_STATUSES, normalize_job_state_for_project
from app.services.release_evidence import is_certified_deploy_row

logger = logging.getLogger(__name__)
_MAX_RETRY = min(2, max(0, int(os.getenv("GOAL_FAILED_LINK_RETRY_MAX", "2"))))
_RETRY_MARKER = re.compile(r"^RETRY_OF_LINK: (\S+)$", re.MULTILINE)
# 운영 지시서의 승계 헤더. 줄 시작에서만 인식해 본문 중간의 언급은 잡지 않는다.
_HEADER_MARKER = re.compile(r"^[ \t]*(AUTO_REWORK_OF|SUPERSEDES)[ \t]*:[ \t]*(.*)$", re.MULTILINE)
_HEADER_PAREN = re.compile(r"\([^()]*\)")
_RUNNER_JOB_ID = re.compile(r"(?<![\w-])runner-[0-9a-f]{8}(?![\w-])")


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
                    _audited_goal_status_update,
                    active_link_predicate,
                    link_optional_columns,
                    superseded_link_predicate,
                )
                columns = await link_optional_columns(conn)
                unblocked = await _audited_goal_status_update(
                    conn, goal_id, "active",
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
                    source="failure_retry_unblock", actor="system:goal_failure_retry",
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


async def _has_release_evidence_column(conn) -> bool:
    """goal_task_links.release_deploy_run_id 존재 여부 (migration 170 이전 스키마 대비).

    캐시하지 않는다: 프로세스 기동 뒤 migration 이 적용돼도 재시작 없이 증거 경로가 켜져야 한다.
    goal_manager.link_optional_columns 에 넣으면 그쪽 preserve_certified 분기가 바뀌므로 따로 본다.
    """
    try:
        rows = await conn.fetch(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'goal_task_links' AND column_name = 'release_deploy_run_id'"
        )
    except Exception as exc:  # noqa: BLE001 — 확인 실패 시 증거 컬럼 없는 스키마로 간주
        logger.debug("goal_release_column_probe_failed: %s", str(exc)[:200])
        return False
    return bool(rows)


def _completion_accept_reason(replacement) -> str | None:
    """후속 job 을 완료로 인정하는 근거(job_phase | release_evidence). 인정하지 않으면 None.

    release_evidence 는 링크가 완료 계열이고, 링크에 기록된 배포 run 이 인증(success/completed,
    이미지 digest 일치)을 통과했을 때만 인정한다. run id 가 남아 있어도 실패·롤백 run 이면 거절.
    """
    if normalize_job_state_for_project(
        replacement["status"], replacement["phase"], replacement["project"],
    ) == "completed":
        return "job_phase"
    link_status = str(replacement.get("link_status") or "").strip().lower()
    if link_status not in DONE_JOB_STATUSES or replacement.get("release_deploy_run_id") is None:
        return None
    deploy = {
        "status": replacement.get("deploy_status"),
        "phase": replacement.get("deploy_phase"),
        "image_digest": replacement.get("image_digest"),
        "standby_digest": replacement.get("standby_digest"),
    }
    return "release_evidence" if is_certified_deploy_row(deploy) else None


def parse_supersede_markers(instruction: str) -> list[tuple[str, str]]:
    """지시서 헤더에서 (실패 job id, marker 종류) 목록을 뽑는다.

    RETRY_OF_LINK 가 가리키는 id 는 카드 검증 경로가 우선하므로 헤더에서 중복 제외한다.
    SUPERSEDES 는 복수, AUTO_REWORK_OF 는 첫 토큰만. 괄호 주석 안의 id 는 버린다.
    """
    text = instruction or ""
    found: list[tuple[str, str]] = []
    retry = _RETRY_MARKER.search(text)
    if retry:
        found.append((retry.group(1), "retry_of_link"))
    for key, value in _HEADER_MARKER.findall(text):
        while True:
            stripped = _HEADER_PAREN.sub(" ", value)
            if stripped == value:
                break
            value = stripped
        ids = _RUNNER_JOB_ID.findall(value)
        if key == "AUTO_REWORK_OF":
            ids = ids[:1]
        marker = key.lower()
        for job_id in ids:
            if all(job_id != seen for seen, _ in found):
                found.append((job_id, marker))
    return found


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
    evidence_select = evidence_join = ""
    if await _has_release_evidence_column(conn):
        evidence_select = (", l.release_deploy_run_id, d.status AS deploy_status, "
                           "d.phase AS deploy_phase, d.image_digest, d.standby_digest")
        evidence_join = "LEFT JOIN deploy_runs d ON d.id = l.release_deploy_run_id"
    replacements = await conn.fetch(
        f"""SELECT l.task_id, l.goal_id, l.milestone_id, l.status AS link_status,
                   p.tenant_id::text AS tenant_id, p.created_at,
                   p.instruction, p.status, p.phase, p.project{evidence_select}
            FROM goal_task_links l JOIN pipeline_jobs p ON p.job_id = l.task_id
            {evidence_join}
            WHERE l.milestone_id = $1::uuid AND l.task_type = 'pipeline_job'
              {active_link_predicate(columns, 'l')}""",
        milestone_id,
    )
    count = 0
    for replacement in replacements:
        markers = parse_supersede_markers(replacement["instruction"])
        if not markers:
            continue
        accept_reason = _completion_accept_reason(replacement)
        if accept_reason is None:
            # 진행 중인 후속은 완료 판정마다 다시 오므로 debug 로만 남긴다.
            # 링크는 완료인데 배포 run 이 인증되지 않은 경우만 이상 징후라 info 로 올린다.
            uncertified = (replacement.get("release_deploy_run_id") is not None
                           and str(replacement.get("link_status") or "").strip().lower()
                           in DONE_JOB_STATUSES)
            for failed_id, marker in markers:
                (logger.info if uncertified else logger.debug)(
                    "goal_retry_supersede_rejected milestone=%s failed=%s replacement=%s "
                    "reason=%s marker=%s", milestone_id, failed_id, replacement["task_id"],
                    "release_deploy_not_certified" if uncertified else "replacement_not_completed",
                    marker)
            continue
        for failed_id, marker in markers:
            if await _supersede_one(conn, milestone_id, row, columns, replacement,
                                    failed_id, marker, accept_reason):
                count += 1
    return count


def _reject(milestone_id: str, failed_id: str, replacement_id, reason: str, marker: str) -> bool:
    logger.warning("goal_retry_supersede_rejected milestone=%s failed=%s replacement=%s reason=%s marker=%s",
                   milestone_id, failed_id, replacement_id, reason, marker)
    return False


async def _supersede_one(conn, milestone_id: str, row, columns: set, replacement,
                         failed_id: str, marker: str, accept_reason: str = "job_phase") -> bool:
    """완료된 후속 job 하나가 marker 로 가리킨 실패 링크 하나를 승계한다.

    retry_of_link 는 살아 있는 retry_candidate 카드와 카드 이후 생성을 요구한다.
    auto_rework_of/supersedes 헤더 경로는 카드를 요구하지 않는 대신 후속 job 이
    실패 job 보다 나중에 생성됐는지 확인한다. 범위·tenant·자기 자신 검증은 공통.
    """
    from app.services.goal_manager import active_link_predicate

    header = marker != "retry_of_link"
    reason = None
    if (str(replacement["goal_id"]) != str(row["goal_id"])
            or str(replacement["milestone_id"]) != milestone_id):
        reason = "link_scope_mismatch"
    elif str(replacement["tenant_id"]) != row["tenant_id"]:
        reason = "tenant_mismatch"
    elif failed_id == replacement["task_id"]:
        reason = "self_replacement"
    elif header:
        failed_job = await conn.fetchrow(
            "SELECT p.created_at FROM pipeline_jobs p "
            "WHERE p.job_id = $1 AND p.tenant_id = $2::uuid",
            failed_id, row["tenant_id"],
        )
        try:
            failed_at = failed_job["created_at"] if failed_job else None
            created_at = replacement["created_at"]
            if failed_at is None:
                reason = "failed_job_missing"
            else:
                if isinstance(failed_at, str):
                    failed_at = datetime.fromisoformat(failed_at)
                if isinstance(created_at, str):
                    created_at = datetime.fromisoformat(created_at)
                if _aware(created_at) <= _aware(failed_at):
                    reason = "created_before_failed"
        except (KeyError, TypeError, ValueError, AttributeError):
            reason = "invalid_job_time"
    if reason:
        return _reject(milestone_id, failed_id, replacement["task_id"], reason, marker)
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
        candidate = max(candidates, key=lambda item: item.get("at") or "") if candidates else None
        if not header:
            if candidate is None:
                reason = "candidate_missing"
            else:
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
            return _reject(milestone_id, failed_id, replacement["task_id"], reason, marker)
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
        if result != "UPDATE 1":
            return False
        if candidate is not None:
            candidate["superseded_by_task_id"] = replacement["task_id"]
            candidate["superseded_accept_reason"] = accept_reason
            await conn.execute(
                "UPDATE milestones SET evidence = $2::jsonb, updated_at = NOW() "
                "WHERE id = $1::uuid AND tenant_id = $3::uuid",
                milestone_id, json.dumps(evidence), row["tenant_id"],
            )
        else:
            logger.info("goal_header_supersede_without_candidate milestone=%s failed=%s replacement=%s",
                        milestone_id, failed_id, replacement["task_id"])
        logger.info("goal_retry_link_superseded milestone=%s failed=%s replacement=%s marker=%s "
                    "accept_reason=%s", milestone_id, failed_id, replacement["task_id"], marker,
                    accept_reason)
    return True
