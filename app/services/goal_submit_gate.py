"""러너 제출 시 GOAL_ID·MILESTONE_ID 짝 검증 (오류 사전 후보 goal.milestone_id_missing_on_submit).

문서 규칙만으로는 지켜지지 않았다 — 목표 17ed0518 의 pipeline_job 링크 62건이
milestone_id NULL 로 쌓였다. 그래서 작업 행을 만들기 **전에** 코드로 막는다.

    인자 ↔ 지시서 값이 서로 다름   → 거부 422 (goal.id_conflict_on_submit)
    GOAL_ID 만 있음                → 거부 (MILESTONE_ID 안내)
    MILESTONE_ID 만 있음           → 그 마일스톤의 goal_id 로 보충
    MILESTONE_ID 가 다른 목표 소속 → 거부
    둘 다 유효                     → 통과
    명시 ID 인데 마일스톤 조회 실패 → 거부 503 (재시도 가능, 작업 행 생성 전)
    둘 다 없음                     → 통과. 단 제출 세션이 active 목표의 마일스톤 담당이면 경고
                                    (RUNNER_GOAL_LINK_MISSING_MODE=block 이면 거부)

ID 해석은 `goal_binding.resolve_declared_ids` 한 함수다 — 게이트·연결기·순서 검사가 같은
쌍을 읽는다. 허용된 판정의 (goal_id, milestone_id) 는 호출부가 연결(autolink)·의존 순서 검사·
파일충돌 검사·목표 게이트에 **그대로** 넘긴다(원래 인자를 다시 넘기지 않는다). 출처(인자/지시서/
마일스톤 보충)는 `goal_source`/`milestone_source` 에 따로 남는다.

연결·goal_task_links 기록 자체는 `pipeline_runner_service._link_job_to_goal_explicit` 하나가
한다(규칙을 두 벌로 두지 않는다). 여기서는 짝만 검증·보충한다.
명시 ID 의 마일스톤 조회가 실패하면 판정할 수 없으므로 fail-closed(503)다. "목표 미지정인데
담당 마일스톤이 있는가" 경고 조회는 보조 정보라 기본(warn)에서는 실패해도 제출을 막지 않는다.
"""
from __future__ import annotations

import logging
import os
from typing import Any, NamedTuple, Optional

from app.services.goal_binding import (
    BIND_SOURCE_EXPLICIT_API,
    is_uuid,
    resolve_declared_ids,
)

logger = logging.getLogger(__name__)

CODE_MILESTONE_MISSING = "goal.milestone_id_missing_on_submit"
CODE_MILESTONE_NOT_FOUND = "goal.milestone_not_found_on_submit"
CODE_MILESTONE_GOAL_MISMATCH = "goal.milestone_goal_mismatch_on_submit"
CODE_LINK_MISSING = "goal.link_missing_on_submit"
CODE_ID_CONFLICT = "goal.id_conflict_on_submit"
CODE_LOOKUP_FAILED = "goal.milestone_lookup_failed_on_submit"

MSG_MILESTONE_MISSING = (
    "MILESTONE_ID 가 없습니다. my_milestones 로 id 를 확인해 지시서에 넣으십시오"
)

MSG_LOOKUP_FAILED = (
    "마일스톤 조회에 실패해 GOAL_ID/MILESTONE_ID 짝을 확인하지 못했습니다. "
    "작업은 만들어지지 않았습니다. 잠시 후 같은 요청을 다시 제출하십시오"
)

LINK_MISSING_MODE_ENV = "RUNNER_GOAL_LINK_MISSING_MODE"

OWNED_OPEN_MILESTONES_SQL = """
SELECT ms.id::text AS milestone_id, g.id::text AS goal_id
  FROM milestones ms
  JOIN goals g ON g.id = ms.goal_id AND g.tenant_id = ms.tenant_id
 WHERE ms.tenant_id = $1::uuid
   AND ms.owner_session_id = $2::uuid
   AND ms.project = $3
   AND ms.status IN ('pending', 'in_progress')
   AND g.status = 'active'
   AND g.paused_at IS NULL
 LIMIT 5
"""


class PairVerdict(NamedTuple):
    allowed: bool
    goal_id: Optional[str] = None
    milestone_id: Optional[str] = None
    code: Optional[str] = None
    message: Optional[str] = None
    warning: Optional[str] = None
    goal_source: Optional[str] = None
    milestone_source: Optional[str] = None
    status_code: int = 422

    @property
    def bind_source(self) -> Optional[str]:
        """goal_task_links.bind_source 로 남길 출처. 목표 출처가 없으면(보충) 마일스톤 출처를 쓴다."""
        if not self.goal_id:
            return None
        return self.goal_source or self.milestone_source or BIND_SOURCE_EXPLICIT_API

    @property
    def goal_supplemented(self) -> bool:
        """목표를 제출자가 쓰지 않고 마일스톤 소속에서 보충했는가."""
        return bool(self.goal_id) and not self.goal_source

    def as_detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


def _reject(code: str, message: str, status_code: int = 422) -> PairVerdict:
    return PairVerdict(False, code=code, message=message, status_code=status_code)


def link_missing_blocks() -> bool:
    return os.getenv(LINK_MISSING_MODE_ENV, "warn").strip().lower() == "block"


async def _fetch_milestone(milestone_id: str, tenant_id: str) -> Optional[Any]:
    from app.core.db_pool import get_pool

    async with get_pool().acquire() as conn:
        return await conn.fetchrow(
            "SELECT id::text AS milestone_id, goal_id::text AS goal_id "
            "FROM milestones WHERE id = $1::uuid AND tenant_id = $2::uuid",
            milestone_id, tenant_id,
        )


async def _owned_open_milestones(tenant_id: str, session_id: str, project: str) -> list[Any]:
    from app.core.db_pool import get_pool

    async with get_pool().acquire() as conn:
        return list(await conn.fetch(OWNED_OPEN_MILESTONES_SQL, tenant_id, session_id, project))


async def _check_declared_missing(
    *, tenant_id: str, session_id: Optional[str], project: str,
) -> PairVerdict:
    if not (tenant_id and session_id and is_uuid(session_id)):
        return PairVerdict(True)
    try:
        owned = await _owned_open_milestones(tenant_id, session_id, project)
    except Exception as exc:  # noqa: BLE001 — 경고 조회 실패는 warn 모드에서 제출을 막지 않는다
        logger.warning("goal_link_missing_lookup_failed session=%s: %s", session_id, str(exc)[:200])
        if link_missing_blocks():
            return _reject(CODE_LOOKUP_FAILED, MSG_LOOKUP_FAILED, 503)
        return PairVerdict(True)
    if not owned:
        return PairVerdict(True)
    ids = ",".join(str(r["milestone_id"]) for r in owned)
    logger.warning(
        "goal_link_missing session=%s project=%s owned_milestones=%s mode=%s",
        session_id, project, ids, "block" if link_missing_blocks() else "warn",
    )
    text = (
        f"이 세션이 담당 중인 active 목표의 마일스톤이 {len(owned)}건 있는데 지시서에 "
        "GOAL_ID/MILESTONE_ID 가 없습니다. my_milestones 로 id 를 확인해 지시서에 넣으십시오"
    )
    if link_missing_blocks():
        return _reject(CODE_LINK_MISSING, text)
    return PairVerdict(True, warning=f"경고: {text}.")


async def evaluate_submit_goal_pair(
    *,
    instruction: Optional[str],
    goal_id: Optional[str],
    milestone_id: Optional[str],
    tenant_id: str,
    session_id: Optional[str],
    project: str,
) -> PairVerdict:
    """제출 한 건의 목표 짝을 판정한다. 허용이면 해석된 (goal_id, milestone_id) 를 담는다."""
    declared = resolve_declared_ids(instruction, goal_id=goal_id, milestone_id=milestone_id)
    if declared.conflict:
        arg_value, line_value = declared.conflict_values or ("", "")
        label = "goal_id" if declared.conflict == "goal" else "milestone_id"
        directive = "GOAL_ID" if declared.conflict == "goal" else "MILESTONE_ID"
        return _reject(
            CODE_ID_CONFLICT,
            f"제출 인자 {label}({arg_value}) 와 지시서 {directive}({line_value}) 가 서로 다릅니다. "
            "하나로 통일해 다시 제출하십시오",
        )
    if not declared.goal_id and not declared.milestone_id:
        return await _check_declared_missing(
            tenant_id=tenant_id, session_id=session_id, project=project,
        )
    if not declared.milestone_id:
        return _reject(CODE_MILESTONE_MISSING, MSG_MILESTONE_MISSING)

    try:
        row = await _fetch_milestone(declared.milestone_id, tenant_id)
    except Exception as exc:  # noqa: BLE001 — 판정 불가: 작업 생성 전에 재시도 가능 오류로 중단
        logger.warning("goal_submit_gate_lookup_failed milestone=%s: %s",
                       declared.milestone_id, str(exc)[:200])
        return _reject(CODE_LOOKUP_FAILED, MSG_LOOKUP_FAILED, 503)
    if not row:
        return _reject(
            CODE_MILESTONE_NOT_FOUND,
            f"MILESTONE_ID {declared.milestone_id} 마일스톤을 찾을 수 없습니다. "
            "my_milestones 로 id 를 다시 확인하십시오",
        )
    owner_goal = str(row["goal_id"]).lower()
    if declared.goal_id and declared.goal_id != owner_goal:
        return _reject(
            CODE_MILESTONE_GOAL_MISMATCH,
            f"MILESTONE_ID {declared.milestone_id} 는 GOAL_ID {declared.goal_id} 소속이 아닙니다 "
            f"(소속 목표: {owner_goal}). my_milestones 로 짝을 확인하십시오",
        )
    return PairVerdict(
        True, owner_goal, declared.milestone_id,
        goal_source=declared.goal_source,
        milestone_source=declared.milestone_source,
    )
