"""Goal binding + pipeline job status normalization (pure, DB 없음).

목적 (AADS Goal Control P0):
1. pipeline_jobs 의 종료 상태/phase 별칭을 **한 곳에서만** 정규화한다.
   기존에는 goal_manager._normalize_task_status 와 pipeline_runner_service
   _TERMINAL_JOB_STATUSES 가 따로 판단해서 terminated / review_failed /
   blocked_dependency 같은 별칭이 goal_task_links 로 전파되지 않았다.
2. "프로젝트만 보고 첫 활성 목표에 붙이는" 암묵적 자동연결을 대체할
   **명시적 목표 컨텍스트** 파싱을 제공한다.

이 모듈은 순수 함수만 담는다(커넥션/풀 의존 없음) — 단위 테스트가 DB 없이 돈다.
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional

# ─── pipeline_jobs 상태/phase 정규화 ────────────────────────────────────────
# 링크 상태 어휘: completed | failed | action_required | running | pending
DONE_JOB_STATUSES = frozenset({"completed", "done", "approved", "deployed"})

# 종료이면서 목표 진행을 막아야 하는 상태들.
# rejected_done/cancelled 는 기존 집합에 이미 있었고, terminated/review_failed/
# blocked_dependency/rejected/timeout 은 phase 별칭이라 누락돼 있었다.
FAILED_JOB_STATUSES = frozenset({
    "failed",
    "error",
    "cancelled",
    "canceled",
    "rejected",
    "rejected_done",
    "terminated",
    "review_failed",
    "blocked_dependency",
    "timeout",
    "timed_out",
})

# 종료가 아니라 "사람/재검수 대기"인 상태 — 마일스톤을 완료시키지도, 막지도 않는다.
# review_hold 는 /pipeline/jobs/{id}/retry-review 로 되살아나므로 failed 로 굳히면
# 재검수 가능한 작업이 마일스톤을 영구히 blocked 로 만든다.
ACTION_REQUIRED_JOB_STATUSES = frozenset({"review_hold", "awaiting_approval", "approved_pending"})

# pipeline_jobs 가 더 이상 진행되지 않는(=durable write 이후 재조정이 필요한) 상태.
TERMINAL_JOB_STATUSES = frozenset(DONE_JOB_STATUSES | FAILED_JOB_STATUSES | {"review_hold"})

# 링크 상태 어휘
LINK_STATUS_COMPLETED = "completed"
LINK_STATUS_FAILED = "failed"
LINK_STATUS_ACTION_REQUIRED = "action_required"

# goal_task_links.link_state (migration 165)
LINK_STATE_ACTIVE = "active"
LINK_STATE_DETACHED = "detached"
LINK_STATE_ORPHAN = "orphan"

# 마일스톤 완료/차단 판정에 실제로 참여하는 링크 상태.
EFFECTIVE_LINK_STATES = frozenset({LINK_STATE_ACTIVE})

# goal_task_links.bind_source
BIND_SOURCE_EXPLICIT_API = "explicit_api"
BIND_SOURCE_EXPLICIT_DIRECTIVE = "explicit_directive"
BIND_SOURCE_RECONCILER = "reconciler"
BIND_SOURCE_LEGACY_AUTO = "legacy_auto_project"
BIND_SOURCE_UNKNOWN = "unknown"

# 명시적 근거 없이(프로젝트만 보고) 붙었을 수 있는 계보 — 재조정에서 별도 보고 대상.
UNVERIFIED_BIND_SOURCES = frozenset({BIND_SOURCE_LEGACY_AUTO, BIND_SOURCE_UNKNOWN, ""})


def normalize_job_state(status: Optional[str], phase: Optional[str] = None) -> str:
    """pipeline_jobs (status, phase) → goal_task_links.status 어휘로 정규화.

    phase 는 status 를 **악화시키는 방향으로만** 반영한다. 예를 들어
    status='cancelled', phase='blocked_dependency' 는 failed 이고,
    status='error', phase='review_failed' 도 failed 다. 반대로
    status='running', phase='done' 같은 과도기 조합이 완료로 승격되지는 않는다.
    """
    normalized = (status or "").strip().lower()
    phase_normalized = (phase or "").strip().lower()

    if normalized in DONE_JOB_STATUSES:
        return LINK_STATUS_COMPLETED
    if normalized in FAILED_JOB_STATUSES or phase_normalized in FAILED_JOB_STATUSES:
        return LINK_STATUS_FAILED
    if normalized in ACTION_REQUIRED_JOB_STATUSES or phase_normalized in ACTION_REQUIRED_JOB_STATUSES:
        return LINK_STATUS_ACTION_REQUIRED
    return normalized or "pending"


def is_terminal_job_state(status: Optional[str], phase: Optional[str] = None) -> bool:
    """durable write 이후 목표 재조정을 트리거해야 하는 상태인지."""
    normalized = (status or "").strip().lower()
    phase_normalized = (phase or "").strip().lower()
    return normalized in TERMINAL_JOB_STATUSES or phase_normalized in FAILED_JOB_STATUSES


# ─── 명시적 목표 컨텍스트 ───────────────────────────────────────────────────
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE,
)

# 지시서 메타데이터 계약: `GOAL_ID: <uuid>` / `MILESTONE_ID: <uuid>`
# (기존 TASK_ID/TITLE/PRIORITY 헤더와 같은 형식이라 하위호환이다 — 없으면 그냥 미연결)
_GOAL_ID_PATTERN = re.compile(r"^\s*GOAL[_-]?ID\s*[:=]\s*([0-9a-fA-F-]{32,40})\s*$", re.MULTILINE)
_MILESTONE_ID_PATTERN = re.compile(
    r"^\s*MILESTONE[_-]?ID\s*[:=]\s*([0-9a-fA-F-]{32,40})\s*$", re.MULTILINE,
)


def is_uuid(value: Optional[str]) -> bool:
    return bool(value) and bool(_UUID_PATTERN.match(str(value).strip()))


class GoalBinding(NamedTuple):
    """작업이 어느 목표/마일스톤에 붙어야 하는지에 대한 **명시적** 근거."""

    goal_id: Optional[str]
    milestone_id: Optional[str]
    source: Optional[str]

    @property
    def is_explicit(self) -> bool:
        return bool(self.goal_id)


EMPTY_BINDING = GoalBinding(None, None, None)


def parse_goal_binding(
    instruction: Optional[str] = None,
    *,
    goal_id: Optional[str] = None,
    milestone_id: Optional[str] = None,
) -> GoalBinding:
    """명시적 목표 컨텍스트를 해석한다.

    우선순위: 호출자가 넘긴 goal_id/milestone_id(=제출 API 필드) → 지시서
    메타데이터(GOAL_ID/MILESTONE_ID). 둘 다 없으면 EMPTY_BINDING 을 돌려주고,
    호출부는 **아무 목표에도 연결하지 않는다**.
    """
    explicit_goal = (goal_id or "").strip()
    explicit_milestone = (milestone_id or "").strip()
    if explicit_goal:
        if not is_uuid(explicit_goal):
            return EMPTY_BINDING
        return GoalBinding(
            explicit_goal.lower(),
            explicit_milestone.lower() if is_uuid(explicit_milestone) else None,
            BIND_SOURCE_EXPLICIT_API,
        )

    text = instruction or ""
    if not text:
        return EMPTY_BINDING
    goal_match = _GOAL_ID_PATTERN.search(text)
    if not goal_match or not is_uuid(goal_match.group(1)):
        return EMPTY_BINDING
    milestone_match = _MILESTONE_ID_PATTERN.search(text)
    milestone_value = milestone_match.group(1) if milestone_match else None
    return GoalBinding(
        goal_match.group(1).strip().lower(),
        milestone_value.strip().lower() if is_uuid(milestone_value) else None,
        BIND_SOURCE_EXPLICIT_DIRECTIVE,
    )


# ─── 재시도 승계(supersession) 판정 ─────────────────────────────────────────
class SupersessionDecision(NamedTuple):
    """실패한 링크가 후속 재시도로 대체되었는지에 대한 결정적 판정."""

    superseded: bool
    superseded_by: Optional[str]
    reason: Optional[str]


NOT_SUPERSEDED = SupersessionDecision(False, None, None)

# 승계 근거: 같은 project + instruction_hash 로 다시 제출돼 성공한 후속 작업.
# instruction_hash 는 /pipeline/jobs 제출 경로가 이미 중복/재시도 판정에 쓰는 키라
# 새 추론을 만들지 않고 기존 계약을 그대로 재사용한다.
SUPERSEDE_REASON_RETRY = "instruction_hash_retry_succeeded"


def decide_supersession(
    *,
    link_status: Optional[str],
    job_id: str,
    instruction_hash: Optional[str],
    job_created_at,
    candidates,
) -> SupersessionDecision:
    """실패 링크가 재시도 성공으로 대체됐는지 판정한다 (순수 함수).

    candidates 는 같은 project 의 `{job_id, instruction_hash, status, phase,
    created_at}` 매핑 목록이다. 판정 조건은 전부 결정적이다:
      - 대상 링크가 failed 여야 한다 (성공/진행 중 링크는 건드리지 않는다)
      - 후속 작업이 같은 instruction_hash 이고 job_id 가 다르며
      - 대상보다 나중에 생성됐고, 종료 상태가 completed 여야 한다
    조건을 하나라도 못 채우면 **현재 실패로 남겨** 마일스톤이 계속 blocked 로 보인다.
    """
    if link_status != LINK_STATUS_FAILED:
        return NOT_SUPERSEDED
    normalized_hash = (instruction_hash or "").strip()
    if not normalized_hash or job_created_at is None:
        return NOT_SUPERSEDED

    winner = None
    for candidate in candidates or ():
        candidate_id = str(candidate.get("job_id") or "")
        if not candidate_id or candidate_id == job_id:
            continue
        if (candidate.get("instruction_hash") or "").strip() != normalized_hash:
            continue
        created_at = candidate.get("created_at")
        if created_at is None or created_at <= job_created_at:
            continue
        if normalize_job_state(candidate.get("status"), candidate.get("phase")) != LINK_STATUS_COMPLETED:
            continue
        if winner is None or created_at < winner[1]:
            winner = (candidate_id, created_at)

    if winner is None:
        return NOT_SUPERSEDED
    return SupersessionDecision(True, winner[0], SUPERSEDE_REASON_RETRY)
