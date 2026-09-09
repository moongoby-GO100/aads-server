"""릴리스 증거 다리 — 인증된 배포 + DB 내구 계보로 goal_task_links 를 재조정한다.

설계 제약 (2026-09-09 거부 사유에서 직접 유도)
---------------------------------------------
`.dockerignore` 가 `.git` 을 제외하므로 **실행 중인 컨테이너에는 `/app/.git` 이
없다**. 따라서 이 모듈은 런타임에 `git` 을 절대 호출하지 않는다. 커밋 계보는
Git 히스토리가 살아 있는 시점(호스트의 깨끗한 릴리스 워크트리에서 deploy.sh 가
인증을 마치는 순간)에 `scripts/record-release-provenance.sh` 가 계산해
`deploy_release_provenance` 에 40자 full SHA 로 못박아 둔 것을 읽기만 한다.

증거 규칙
---------
1. **인증 배포 게이트**: deploy_runs.status='success' AND phase='completed' AND
   image_digest/standby_digest 가 둘 다 NULL 이 아니고 서로 같아야 한다.
   (blue/green 두 슬롯이 같은 이미지를 들고 있다 = 릴리스가 실제로 굳었다)
2. **정확 일치(exact)** 또는 **조상(ancestor)** 계보만 인정한다. 둘 다 기록
   시점에 Git 으로 해석된 40자 full SHA 다.
3. **접두사 금지**: 40자 소문자 hex 가 아니면 증거로 쓰지 않는다(fail closed).
   12자 축약 SHA(deploy_runs.release_sha 의 대다수)는 여기서 절대 확장하지 않는다.
4. **완료 판정은 GoalStateMachine 만 한다**. 이 모듈은 링크(goal_task_links)까지만
   만지고, 마일스톤/목표는 check_milestone_completion / _update_goal_progress 를
   통해서만 움직인다. goals/milestones 를 직접 completed 로 쓰지 않는다.
5. **멱등**: 증거로 완료된 링크는 release_deploy_run_id 가 채워진다. 두 번째
   실행은 같은 입력에서 completed=0 이다.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Optional

from app.services.goal_binding import (
    DONE_JOB_STATUSES,
    LINK_STATUS_FAILED,
    normalize_job_state,
)

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 200
MAX_LIMIT = 2000

# 계보 종류 — deploy_release_provenance.relationship 어휘와 1:1.
RELATIONSHIP_EXACT = "exact"
RELATIONSHIP_ANCESTOR = "ancestor"
VALID_RELATIONSHIPS = frozenset({RELATIONSHIP_EXACT, RELATIONSHIP_ANCESTOR})

# 40자 소문자 hex 만 통과. 12자 축약/대문자 혼합/공백 포함은 전부 거부한다.
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# migration 170 이 적용됐는지 1회만 확인한다(블루/그린 과도기 안전).
_RELEASE_LINK_COLUMNS = (
    "release_deploy_run_id", "release_sha", "release_relationship", "release_verified_at",
)
_release_schema_cache: Optional[bool] = None


def reset_schema_cache() -> None:
    """테스트/마이그레이션 직후에 스키마 탐지 캐시를 비운다."""
    global _release_schema_cache
    _release_schema_cache = None


# ─── 순수 함수 (DB 없음) ────────────────────────────────────────────────────
def normalize_full_sha(value: Any) -> Optional[str]:
    """40자 full SHA 로 정규화한다. 아니면 None (접두사도 None).

    대문자는 소문자로 내리고 양끝 공백만 제거한다. 그 외 어떤 확장/추측도 하지
    않는다 — 축약 SHA 를 확장하려면 Git 이 필요한데 런타임에는 Git 이 없다.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _FULL_SHA_RE.match(candidate) else None


def is_certified_deploy_row(row: Any) -> bool:
    """deploy_runs 한 행이 '인증된 릴리스'인지 판정한다 (요구사항 1).

    status='success' + phase='completed' + image_digest/standby_digest 가 모두
    존재하고 서로 같을 때만 True. 하나라도 어긋나면 증거로 쓰지 않는다.
    """
    if row is None:
        return False
    get = row.get if isinstance(row, dict) else (lambda k, d=None: row[k] if k in row else d)
    if (get("status") or "") != "success":
        return False
    if (get("phase") or "") != "completed":
        return False
    image = get("image_digest")
    standby = get("standby_digest")
    if not image or not standby:
        return False
    return str(image) == str(standby)


def select_best_provenance(rows: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """한 task_sha 에 대한 인증 계보 후보들 중 가장 강한 증거 하나를 고른다.

    exact 가 ancestor 보다 강하고, 같은 등급이면 최신 배포(deploy_run_id 큰 쪽).
    """
    usable = [r for r in rows if r.get("relationship") in VALID_RELATIONSHIPS]
    if not usable:
        return None
    return max(
        usable,
        key=lambda r: (
            1 if r.get("relationship") == RELATIONSHIP_EXACT else 0,
            int(r.get("deploy_run_id") or 0),
        ),
    )


def build_evidence_index(
    provenance_rows: list[dict[str, Any]],
    deploy_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """task_sha → 사용 가능한 증거 1건. 인증되지 않은 배포는 여기서 탈락한다.

    계보 행 자체는 인증 전에 기록될 수 있으므로(요구사항 3의 "후보 관계"),
    **배포가 인증되기 전에는 절대 쓰이지 않도록** 이 단계에서 걸러낸다.
    """
    certified = {
        int(d["id"]): d for d in deploy_rows if is_certified_deploy_row(d)
    }
    by_sha: dict[str, list[dict[str, Any]]] = {}
    for row in provenance_rows:
        task_sha = normalize_full_sha(row.get("task_sha"))
        release_sha = normalize_full_sha(row.get("release_sha"))
        if not task_sha or not release_sha:
            # 스키마 CHECK 를 우회해 들어온 값이 있어도 여기서 fail closed.
            continue
        run_id = int(row.get("deploy_run_id") or 0)
        if run_id not in certified:
            continue
        relationship = row.get("relationship")
        if relationship == RELATIONSHIP_EXACT and task_sha != release_sha:
            continue  # 위조된 exact — 무시한다
        by_sha.setdefault(task_sha, []).append({
            "task_sha": task_sha,
            "release_sha": release_sha,
            "relationship": relationship,
            "deploy_run_id": run_id,
        })
    return {
        sha: best
        for sha, rows in by_sha.items()
        if (best := select_best_provenance(rows)) is not None
    }


def plan_release_completions(
    links: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """어떤 링크를 릴리스 증거로 완료 처리할지 계산한다 (순수 함수).

    한 링크당 결과는 하나다. 건너뛴 이유도 함께 돌려주어 운영자가 dry-run 으로
    "왜 안 붙었는지"를 볼 수 있게 한다.
    """
    planned: list[dict[str, Any]] = []
    for link in links:
        base = {
            "link_id": link.get("link_id"),
            "task_id": link.get("task_id"),
            "goal_id": link.get("goal_id"),
            "milestone_id": link.get("milestone_id"),
        }

        # 1) 이미 릴리스 증거로 완료된 링크 → 두 번째 실행은 아무것도 쓰지 않는다.
        if link.get("link_status") in DONE_JOB_STATUSES and link.get("release_deploy_run_id"):
            planned.append({**base, "action": "skip", "reason": "already_release_certified"})
            continue

        # 2) 실패한 작업은 릴리스에 들어갔더라도 완료로 승격하지 않는다.
        job_state = normalize_job_state(link.get("job_status"), link.get("job_phase"))
        if job_state == LINK_STATUS_FAILED:
            planned.append({**base, "action": "skip", "reason": "job_failed"})
            continue

        # 3) 40자 full SHA 가 아니면 증거를 만들 수 없다 (모호한 접두사 거부).
        task_sha = normalize_full_sha(link.get("commit_hash"))
        if not task_sha:
            planned.append({
                **base,
                "action": "skip",
                "reason": "unresolvable_task_sha",
                "raw_commit_hash": (str(link.get("commit_hash"))[:64] if link.get("commit_hash") else None),
            })
            continue

        # 4) 인증된 배포에 속한 계보가 있어야 한다.
        found = evidence.get(task_sha)
        if not found:
            planned.append({**base, "action": "skip", "reason": "no_certified_release", "task_sha": task_sha})
            continue

        planned.append({
            **base,
            "action": "complete",
            "task_sha": task_sha,
            "release_sha": found["release_sha"],
            "relationship": found["relationship"],
            "deploy_run_id": found["deploy_run_id"],
        })
    return planned


def summarize_plan(planned: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in planned:
        key = item["action"] if item["action"] == "complete" else f"skip:{item['reason']}"
        counts[key] = counts.get(key, 0) + 1
    return counts


# ─── DB 계층 ────────────────────────────────────────────────────────────────
async def has_release_schema(conn) -> bool:
    """goal_task_links 에 migration 170 컬럼이 있는지 (프로세스 수명 동안 캐시)."""
    global _release_schema_cache
    if _release_schema_cache is None:
        try:
            rows = await conn.fetch(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'goal_task_links'
                """
            )
            present = {r["column_name"] for r in rows}
            _release_schema_cache = set(_RELEASE_LINK_COLUMNS).issubset(present)
        except Exception as exc:  # noqa: BLE001 — 탐지 실패는 레거시 스키마로 간주
            logger.debug("release_schema_probe_failed: %s", str(exc)[:200])
            return False
    return _release_schema_cache


async def _load_links(conn, project: Optional[str], limit: int) -> list[dict[str, Any]]:
    """완료로 승격될 수 있는 활성 링크 + 작업 커밋을 읽는다 (읽기 전용)."""
    rows = await conn.fetch(
        """
        SELECT l.id::text            AS link_id,
               l.goal_id::text       AS goal_id,
               l.milestone_id::text  AS milestone_id,
               l.task_id,
               l.status              AS link_status,
               l.release_deploy_run_id,
               j.commit_hash,
               j.status              AS job_status,
               j.phase               AS job_phase,
               COALESCE(g.project, j.project) AS project
        FROM goal_task_links l
        JOIN pipeline_jobs j
          ON l.task_type = 'pipeline_job' AND j.job_id = l.task_id
        LEFT JOIN goals g ON g.id = l.goal_id
        WHERE COALESCE(l.link_state, 'active') = 'active'
          AND l.superseded_by IS NULL
          AND j.commit_hash IS NOT NULL
          AND ($1::text IS NULL OR g.project = $1::text OR j.project = $1::text)
        ORDER BY l.created_at DESC
        LIMIT $2
        """,
        project,
        limit,
    )
    return [dict(r) for r in rows]


async def _load_evidence(conn, project: Optional[str], shas: list[str]) -> dict[str, dict[str, Any]]:
    """계보 + 그 배포의 인증 컬럼을 읽어 사용 가능한 증거만 남긴다."""
    if not shas:
        return {}
    rows = await conn.fetch(
        """
        SELECT p.task_sha, p.release_sha, p.relationship, p.deploy_run_id,
               d.status, d.phase, d.image_digest, d.standby_digest
        FROM deploy_release_provenance p
        JOIN deploy_runs d ON d.id = p.deploy_run_id
        WHERE p.task_sha = ANY($1::text[])
          AND ($2::text IS NULL OR p.project = $2::text)
        ORDER BY p.deploy_run_id DESC
        LIMIT 5000
        """,
        shas,
        project,
    )
    records = [dict(r) for r in rows]
    # 인증 판정은 Python 순수 함수로 한 번 더 건다 — SQL 조건이 바뀌어도
    # "인증되지 않은 배포는 증거가 아니다"는 규칙이 테스트로 고정된다.
    deploy_rows = [
        {
            "id": r["deploy_run_id"],
            "status": r.get("status"),
            "phase": r.get("phase"),
            "image_digest": r.get("image_digest"),
            "standby_digest": r.get("standby_digest"),
        }
        for r in records
    ]
    return build_evidence_index(records, deploy_rows)


async def reconcile_release_links(
    project: Optional[str] = None,
    *,
    dry_run: bool = True,
    limit: int = DEFAULT_LIMIT,
    actor: str = "release_evidence",
) -> dict[str, Any]:
    """인증된 릴리스에 포함된 작업의 링크를 완료로 승격하고 목표를 전진시킨다.

    dry_run=True (기본) 이면 계획만 돌려주고 DB 를 쓰지 않는다.
    같은 입력으로 두 번 돌리면 두 번째는 completed=0 이다(멱등).
    """
    from app.core.db_pool import get_pool
    from app.services.goal_manager import goal_state_machine

    limit = min(max(int(limit), 1), MAX_LIMIT)
    correlation_id = uuid.uuid4().hex[:16]
    result: dict[str, Any] = {
        "project": project or "ALL",
        "dry_run": dry_run,
        "correlation_id": correlation_id,
        "scanned": 0,
        "planned": 0,
        "completed": 0,
        "counts": {},
        "actions": [],
    }

    pool = get_pool()
    touched_milestones: set[str] = set()
    touched_goals: set[str] = set()

    async with pool.acquire() as conn:
        schema_ready = await has_release_schema(conn)
        result["schema_ready"] = schema_ready
        if not schema_ready:
            # migration 170 이전 이미지에서도 죽지 않는다 — 읽기 전용 보고로 격하.
            result["error"] = "migration_170_required"
            return result

        links = await _load_links(conn, project, limit)
        shas = sorted({
            sha for link in links
            if (sha := normalize_full_sha(link.get("commit_hash")))
        })
        evidence = await _load_evidence(conn, project, shas)
        planned = plan_release_completions(links, evidence)
        completions = [p for p in planned if p["action"] == "complete"]

        result["scanned"] = len(links)
        result["resolvable_shas"] = len(shas)
        result["evidence_shas"] = len(evidence)
        result["planned"] = len(completions)
        result["counts"] = summarize_plan(planned)
        result["actions"] = planned[:50]

        if dry_run:
            await goal_state_machine._trace(
                "goal_release_evidence",
                project=project,
                input_summary=f"release evidence dry-run project={project or 'ALL'}",
                output_summary=f"planned={len(completions)} scanned={len(links)}",
                metadata={
                    "correlation_id": correlation_id,
                    "dry_run": True,
                    "counts": result["counts"],
                },
                conn=conn,
            )
            return result

        completed = 0
        for action in completions:
            try:
                # 조건부 UPDATE — 이미 증거가 붙은 행은 건드리지 않는다(멱등).
                status = await conn.execute(
                    """
                    UPDATE goal_task_links
                    SET status = 'completed',
                        last_job_status = 'completed',
                        release_deploy_run_id = $2::bigint,
                        release_sha = $3,
                        release_relationship = $4,
                        release_verified_at = NOW(),
                        reconciled_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $1::uuid
                      AND release_deploy_run_id IS DISTINCT FROM $2::bigint
                    """,
                    action["link_id"],
                    action["deploy_run_id"],
                    action["release_sha"],
                    action["relationship"],
                )
                if _rows_affected(status) == 0:
                    action["applied"] = False
                    continue
                action["applied"] = True
                completed += 1
                if action.get("milestone_id"):
                    touched_milestones.add(action["milestone_id"])
                if action.get("goal_id"):
                    touched_goals.add(action["goal_id"])
            except Exception as exc:  # noqa: BLE001 — 한 행 실패가 전체를 막지 않는다
                logger.warning(
                    "release_evidence_row_failed link=%s: %s",
                    action.get("link_id"), str(exc)[:200],
                )
        result["completed"] = completed

    # 완료 판정과 다음 단계 개시는 **오직 GoalStateMachine** 을 통해서만 한다.
    # 여기서 goals/milestones 를 직접 쓰지 않으므로 기존 완료 기준이 그대로 적용된다.
    milestones_result: dict[str, Any] = {}
    for milestone_id in sorted(touched_milestones):
        try:
            milestones_result[milestone_id] = await goal_state_machine.check_milestone_completion(milestone_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("release_evidence_milestone_failed %s: %s", milestone_id, str(exc)[:200])
    for goal_id in sorted(touched_goals):
        try:
            await goal_state_machine._update_goal_progress(goal_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("release_evidence_goal_failed %s: %s", goal_id, str(exc)[:200])

    result["milestones_recomputed"] = len(touched_milestones)
    result["goals_recomputed"] = len(touched_goals)
    result["milestones"] = {
        mid: {"completed": bool(res.get("completed")), "status": res.get("status")}
        for mid, res in milestones_result.items()
    }

    await goal_state_machine._trace(
        "goal_release_evidence",
        project=project,
        input_summary=f"release evidence reconcile project={project or 'ALL'} actor={actor}",
        output_summary=(
            f"completed={result['completed']} milestones={len(touched_milestones)} "
            f"goals={len(touched_goals)}"
        ),
        metadata={
            "correlation_id": correlation_id,
            "dry_run": False,
            "counts": result["counts"],
            "deploy_run_ids": sorted({a["deploy_run_id"] for a in completions if a.get("applied")}),
            "release_shas": sorted({a["release_sha"] for a in completions if a.get("applied")}),
            "milestones": result["milestones"],
        },
    )
    logger.info(
        "release_evidence_done project=%s correlation=%s completed=%s counts=%s",
        project or "ALL", correlation_id, result["completed"], result["counts"],
    )
    return result


def _rows_affected(status: Any) -> int:
    """asyncpg execute() 의 'UPDATE n' 태그에서 갱신 행 수를 뽑는다."""
    if isinstance(status, int):
        return status
    if isinstance(status, str):
        parts = status.strip().split()
        if parts and parts[-1].isdigit():
            return int(parts[-1])
    # 태그를 못 읽으면 보수적으로 '갱신됨'으로 본다 — 조건부 UPDATE 라 안전하다.
    return 1
