"""goal_task_links ↔ pipeline_jobs 정합화 (Goal Control P0 — C/D/E).

관측된 프로덕션 결함:
  * 작업이 종결(cancelled/error/rejected_done)됐는데 링크는 queued/running 으로 남음
  * 링크가 가리키는 pipeline_jobs 행이 아예 없음(orphan)
  * 구 project-only 자동 연결로 무관한 작업이 활성 목표에 매달림
  * 재시도로 대체된 과거 실패가 마일스톤을 영구히 blocked 로 고정

설계 원칙
  1. **dry-run 이 기본.** 무엇을 바꿀지 먼저 보고한다.
  2. **행을 지우지 않는다.** detach 는 milestone_id 를 떼고 근거를 남기는 것이지 DELETE 가 아니다.
  3. **결정적 근거만.** 근거를 못 대는 링크는 보고만 하고 건드리지 않는다.
  4. **멱등.** 같은 인자로 두 번 돌리면 두 번째는 변경 0건이다.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.services.goal_manager import (
    goal_state_machine,
    is_terminal_task_status,
    normalize_task_status,
)

logger = logging.getLogger(__name__)

# 한 번의 repair 가 건드릴 수 있는 링크 수 상한 (폭주 방지)
DEFAULT_REPAIR_LIMIT = 200

# 분류 라벨
STALE = "stale_status"
ORPHAN = "orphan_no_job"
MISBOUND = "misbound_project"
SUPERSEDABLE = "supersedable_retry"
UNVERIFIED = "unverified_legacy_binding"


_BASE_COLUMNS = """
        l.id           AS link_id,
        l.goal_id      AS goal_id,
        l.milestone_id AS milestone_id,
        l.task_type    AS task_type,
        l.task_id      AS task_id,
        l.status       AS link_status,
        g.project      AS goal_project,
        g.status       AS goal_status,
        j.job_id       AS job_id,
        j.status       AS job_status,
        j.phase        AS job_phase,
        j.project      AS job_project,
        j.instruction_hash AS instruction_hash,
        md5(j.instruction) AS instruction_md5,
        j.created_at   AS job_created_at
"""


async def _fetch_links(conn: Any, project: Optional[str]) -> list[dict[str, Any]]:
    """링크 + 대응 작업 + 목표를 한 번에 읽는다 (읽기 전용)."""
    from app.services.goal_binding import has_supersession_columns

    if await has_supersession_columns(conn):
        extra = ", l.bind_source AS bind_source, l.superseded_by AS superseded_by"
    else:
        extra = ", NULL::text AS bind_source, NULL::text AS superseded_by"

    rows = await conn.fetch(
        f"""
        SELECT {_BASE_COLUMNS}{extra}
        FROM goal_task_links l
        LEFT JOIN goals g ON g.id = l.goal_id
        LEFT JOIN pipeline_jobs j
               ON j.job_id = l.task_id AND l.task_type = 'pipeline_job'
        WHERE ($1::text IS NULL OR g.project = $1::text)
        ORDER BY l.created_at
        """,
        project,
    )
    return [dict(r) for r in rows]


def _retry_key(row: dict[str, Any]) -> Optional[str]:
    """재시도 동일성 키 — instruction_hash 우선, 없으면 instruction 해시."""
    return row.get("instruction_hash") or row.get("instruction_md5") or None


def classify_links(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """링크를 정합화 범주로 분류한다 (순수 함수 — DB 없이 테스트 가능).

    한 링크는 **가장 심각한 한 범주**에만 들어간다. misbound > orphan >
    supersedable > stale > unverified 순으로 판정한다.
    """
    findings: dict[str, list[dict[str, Any]]] = {
        MISBOUND: [], ORPHAN: [], SUPERSEDABLE: [], STALE: [], UNVERIFIED: [],
    }

    # 마일스톤별 "완료된 재시도 후보" 색인 — supersede 판정용
    replacements: dict[tuple[Any, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = _retry_key(row)
        if not key or not row.get("milestone_id") or not row.get("job_id"):
            continue
        if normalize_task_status(row.get("job_status")) != "completed":
            continue
        replacements.setdefault((row["milestone_id"], key), []).append(row)

    for row in rows:
        if row.get("superseded_by"):
            continue  # 이미 정리됨 — 멱등성

        job_status = row.get("job_status")
        link_status = row.get("link_status")
        normalized = normalize_task_status(job_status) if row.get("job_id") else None

        # 1) 프로젝트 불일치 — 목표와 작업의 프로젝트가 다르면 결정적 오연결
        if row.get("job_id") and row.get("goal_project") and row.get("job_project") \
                and row["goal_project"] != row["job_project"]:
            findings[MISBOUND].append({
                **_summary(row),
                "reason": "goal_project != job_project",
                "goal_project": row["goal_project"],
                "job_project": row["job_project"],
            })
            continue

        # 2) 고아 — 링크는 있는데 pipeline_jobs 행이 없다 (영구 미해결 링크)
        if row.get("task_type") == "pipeline_job" and not row.get("job_id"):
            findings[ORPHAN].append({
                **_summary(row),
                "reason": "no pipeline_jobs row for task_id",
            })
            continue

        # 3) 재시도 대체 — 실패했지만 같은 마일스톤에 더 나중의 성공 재시도가 있다
        if normalized == "failed":
            key = _retry_key(row)
            candidates = replacements.get((row.get("milestone_id"), key), []) if key else []
            replacement = _earliest_later(candidates, row)
            if replacement:
                findings[SUPERSEDABLE].append({
                    **_summary(row),
                    "reason": "later completed retry with identical instruction",
                    "superseded_by": replacement["task_id"],
                    "match_key": key,
                })
                continue

        # 4) 상태 표류 — 작업은 종결됐는데 링크가 따라오지 않았다
        if normalized and is_terminal_task_status(job_status) and link_status != normalized:
            findings[STALE].append({
                **_summary(row),
                "reason": f"job terminal({job_status}) but link={link_status}",
                "target_status": normalized,
            })
            continue

        # 5) 근거 없는 과거 바인딩 — 보고만 한다 (자동 분리 금지)
        if (row.get("bind_source") or "legacy_auto") == "legacy_auto":
            findings[UNVERIFIED].append({
                **_summary(row),
                "reason": "created by project-only auto-link; binding intent unknown",
            })

    return findings


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "link_id": str(row.get("link_id")),
        "goal_id": str(row["goal_id"]) if row.get("goal_id") else None,
        "milestone_id": str(row["milestone_id"]) if row.get("milestone_id") else None,
        "task_id": row.get("task_id"),
        "link_status": row.get("link_status"),
        "job_status": row.get("job_status"),
        "job_phase": row.get("job_phase"),
        "bind_source": row.get("bind_source"),
    }


def _earliest_later(candidates: list[dict[str, Any]], row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """실패 시도보다 **나중에 생성된** 성공 재시도 중 가장 이른 것."""
    failed_at = row.get("job_created_at")
    later = [
        c for c in candidates
        if c["task_id"] != row["task_id"]
        and c.get("job_created_at") is not None
        and failed_at is not None
        and c["job_created_at"] > failed_at
    ]
    if not later:
        return None
    return sorted(later, key=lambda c: c["job_created_at"])[0]


async def scan(project: Optional[str] = None) -> dict[str, Any]:
    """읽기 전용 감사 — 범주별 건수와 표본을 돌려준다."""
    pool = await goal_state_machine._pool()
    async with pool.acquire() as conn:
        rows = await _fetch_links(conn, project)
    findings = classify_links(rows)
    return {
        "project": project,
        "links_examined": len(rows),
        "counts": {label: len(items) for label, items in findings.items()},
        "samples": {label: items[:10] for label, items in findings.items() if items},
    }


async def reconcile(
    project: Optional[str] = None,
    *,
    dry_run: bool = True,
    limit: int = DEFAULT_REPAIR_LIMIT,
    detach_misbound: bool = False,
    mark_orphans: bool = False,
) -> dict[str, Any]:
    """정합화 실행. 기본은 dry-run (아무것도 쓰지 않는다).

    수리 범위
      * stale        → 링크 상태를 작업의 정규화 상태로 맞춘다 (항상)
      * supersedable → superseded_by/superseded_at + 근거 기록 (항상)
      * misbound     → `detach_misbound=True` 일 때만 milestone_id 분리 (행 보존)
      * orphan       → `mark_orphans=True` 일 때만 status='orphaned' 표시 (행 보존)
      * unverified   → **절대 자동 수리하지 않는다** (보고 전용)

    수리 후 영향받은 마일스톤/목표의 진행률을 재계산한다.
    """
    from app.services.goal_binding import has_supersession_columns

    pool = await goal_state_machine._pool()
    applied: dict[str, int] = {STALE: 0, SUPERSEDABLE: 0, MISBOUND: 0, ORPHAN: 0}
    touched_milestones: set[str] = set()
    touched_goals: set[str] = set()

    async with pool.acquire() as conn:
        rows = await _fetch_links(conn, project)
        findings = classify_links(rows)
        supersession_ready = await has_supersession_columns(conn)

        planned = (
            [(STALE, f) for f in findings[STALE]]
            + [(SUPERSEDABLE, f) for f in findings[SUPERSEDABLE]]
            + ([(MISBOUND, f) for f in findings[MISBOUND]] if detach_misbound else [])
            + ([(ORPHAN, f) for f in findings[ORPHAN]] if mark_orphans else [])
        )
        truncated = len(planned) > limit
        planned = planned[:limit]

        if not dry_run:
            for label, finding in planned:
                link_id = finding["link_id"]
                if label == STALE:
                    await conn.execute(
                        "UPDATE goal_task_links SET status = $2 WHERE id = $1::uuid",
                        link_id, finding["target_status"],
                    )
                    await _stamp(conn, link_id, supersession_ready, finding)
                elif label == SUPERSEDABLE:
                    if not supersession_ready:
                        continue  # 마이그레이션 165 미적용 — 조용히 건너뛴다
                    await conn.execute(
                        """
                        UPDATE goal_task_links
                        SET superseded_by = $2, superseded_at = NOW()
                        WHERE id = $1::uuid AND superseded_by IS NULL
                        """,
                        link_id, finding["superseded_by"],
                    )
                    await _stamp(conn, link_id, supersession_ready, finding)
                elif label == MISBOUND:
                    # 삭제가 아니라 분리 — 마일스톤 판정에서만 빼고 감사용으로 남긴다
                    await conn.execute(
                        "UPDATE goal_task_links SET milestone_id = NULL WHERE id = $1::uuid "
                        "AND goal_id IS NOT NULL",
                        link_id,
                    )
                    await _stamp(conn, link_id, supersession_ready, finding)
                elif label == ORPHAN:
                    await conn.execute(
                        "UPDATE goal_task_links SET status = 'orphaned' WHERE id = $1::uuid",
                        link_id,
                    )
                    await _stamp(conn, link_id, supersession_ready, finding)

                applied[label] += 1
                if finding.get("milestone_id"):
                    touched_milestones.add(finding["milestone_id"])
                if finding.get("goal_id"):
                    touched_goals.add(finding["goal_id"])

    # E: 정합화 후 마일스톤 완료 재판정 → 목표 진행률 재계산.
    # 완료 조건이 실제로 충족될 때만 다음 단계가 열린다 (check_milestone_completion 경유).
    recomputed: list[dict[str, Any]] = []
    if not dry_run:
        for milestone_id in sorted(touched_milestones):
            try:
                recomputed.append(await goal_state_machine.check_milestone_completion(milestone_id))
            except Exception as exc:  # noqa: BLE001 — 재계산 실패가 수리를 되돌리지 않는다
                logger.warning("goal_reconcile_milestone_recheck_failed ms=%s: %s", milestone_id, exc)
        for goal_id in sorted(touched_goals):
            try:
                await goal_state_machine._update_goal_progress(goal_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("goal_reconcile_progress_failed goal=%s: %s", goal_id, exc)

    return {
        "project": project,
        "dry_run": dry_run,
        "links_examined": len(rows),
        "counts": {label: len(items) for label, items in findings.items()},
        "planned": [{"action": label, **finding} for label, finding in planned],
        "applied": applied,
        "truncated": truncated,
        "limit": limit,
        "supersession_schema_ready": supersession_ready,
        "milestones_recomputed": recomputed,
        "goals_recomputed": sorted(touched_goals),
        "manual_review_required": len(findings[UNVERIFIED]),
    }


async def _stamp(conn: Any, link_id: str, supersession_ready: bool, evidence: dict[str, Any]) -> None:
    """수리 근거를 감사 컬럼에 남긴다 (컬럼 없으면 생략)."""
    if not supersession_ready:
        return
    try:
        await conn.execute(
            """
            UPDATE goal_task_links
            SET bind_evidence = $2::jsonb, reconciled_at = NOW(), updated_at = NOW()
            WHERE id = $1::uuid
            """,
            link_id, json.dumps(evidence, ensure_ascii=False, default=str),
        )
    except Exception as exc:  # noqa: BLE001 — 감사 기록 실패가 수리를 막지 않는다
        logger.debug("goal_link_evidence_skipped link=%s: %s", link_id, str(exc)[:200])
