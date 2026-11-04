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
LINK_STATUS_PENDING = "pending"

# AADS 작업 승인은 배포 인증이 아니다. 기존 normalize_job_state 공개 계약은
# 그대로 두고, 프로젝트를 아는 호출부만 이 추가 게이트를 사용한다.
RELEASE_GATED_PROJECTS = frozenset({"AADS"})
RELEASE_CERTIFIED_PHASE = "release_certified"

# goal_task_links.link_state (migration 165)
LINK_STATE_ACTIVE = "active"
LINK_STATE_DETACHED = "detached"
LINK_STATE_ORPHAN = "orphan"

# 마일스톤 완료/차단 판정에 실제로 참여하는 링크 상태.
EFFECTIVE_LINK_STATES = frozenset({LINK_STATE_ACTIVE})

# goal_task_links.bind_source
BIND_SOURCE_EXPLICIT_API = "explicit_api"
BIND_SOURCE_EXPLICIT_DIRECTIVE = "explicit_directive"
# I-3(c): 제출 세션이 맡은 목표에 in_progress 마일스톤이 정확히 1개일 때만 붙는다.
BIND_SOURCE_SESSION_SINGLE = "session_single"
BIND_SOURCE_RECONCILER = "reconciler"
BIND_SOURCE_LEGACY_AUTO = "legacy_auto_project"
BIND_SOURCE_UNKNOWN = "unknown"

# 명시적 근거 없이(프로젝트만 보고) 붙었을 수 있는 계보 — 재조정에서 별도 보고 대상.
UNVERIFIED_BIND_SOURCES = frozenset({BIND_SOURCE_LEGACY_AUTO, BIND_SOURCE_UNKNOWN, ""})

def remediation_purpose(instruction: str) -> Optional[str]:
    """Return a non-empty, explicitly declared remediation purpose."""
    for line in (instruction or "").splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower().replace("-", "_") == "remediation_purpose":
            return value.strip() or None
    return None


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


def normalize_job_state_for_project(
    status: Optional[str],
    phase: Optional[str] = None,
    project: Optional[str] = None,
) -> str:
    """프로젝트별 릴리스 게이트를 적용한 링크 상태를 반환한다.

    기존 :func:`normalize_job_state` 시그니처와 비프로덕션 프로젝트 동작은
    보존한다. AADS의 완료 계열 상태는 인증 phase 전까지 ``pending``이며,
    실패 phase는 완료보다 우선한다.
    """
    normalized = normalize_job_state(status, phase)
    phase_normalized = (phase or "").strip().lower()
    project_normalized = (project or "").strip().upper()
    if project_normalized not in RELEASE_GATED_PROJECTS:
        return normalized
    if phase_normalized in FAILED_JOB_STATUSES:
        return LINK_STATUS_FAILED
    if normalized == LINK_STATUS_COMPLETED and phase_normalized != RELEASE_CERTIFIED_PHASE:
        return LINK_STATUS_PENDING
    return normalized


def release_gate_withheld(
    status: Optional[str],
    phase: Optional[str] = None,
    project: Optional[str] = None,
) -> bool:
    """릴리스 게이트 때문에 완료 상태가 pending으로 보류됐는지 판정한다."""
    return (
        normalize_job_state(status, phase) == LINK_STATUS_COMPLETED
        and normalize_job_state_for_project(status, phase, project) == LINK_STATUS_PENDING
    )


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


class DeclaredIds(NamedTuple):
    """제출자가 선언한 id 쌍과 각 값의 출처. 한쪽만 있어도 값을 돌려준다(검증은 호출부가 DB 로 한다).

    ``conflict`` 가 있으면 인자와 지시서가 서로 다른 값을 가리킨 것이다 — 어느 쪽도 고르지 않는다.
    """

    goal_id: Optional[str]
    milestone_id: Optional[str]
    goal_source: Optional[str] = None
    milestone_source: Optional[str] = None
    conflict: Optional[str] = None
    conflict_values: Optional[tuple[str, str]] = None


def _pick_declared(
    arg: Optional[str], pattern: "re.Pattern[str]", text: str,
) -> tuple[Optional[str], Optional[str], Optional[tuple[str, str]]]:
    """(값, 출처, 충돌 시 (인자값, 지시서값)). 인자·지시서 줄이 모두 유효한 UUID 이고 서로 다르면 충돌이다."""
    arg_value = (arg or "").strip().lower()
    arg_value = arg_value if is_uuid(arg_value) else None
    match = pattern.search(text)
    line_value = match.group(1).strip().lower() if match and is_uuid(match.group(1)) else None
    if arg_value and line_value and arg_value != line_value:
        return None, None, (arg_value, line_value)
    if arg_value:
        return arg_value, BIND_SOURCE_EXPLICIT_API, None
    if line_value:
        return line_value, BIND_SOURCE_EXPLICIT_DIRECTIVE, None
    return None, None, None


def resolve_declared_ids(
    instruction: Optional[str] = None,
    *,
    goal_id: Optional[str] = None,
    milestone_id: Optional[str] = None,
) -> DeclaredIds:
    """제출 인자와 지시서 `GOAL_ID:`/`MILESTONE_ID:` 줄을 읽는 **유일한** 해석 함수.

    제출 게이트(goal_submit_gate)·연결기(parse_goal_binding)·마일스톤 순서 검사가 모두 이
    함수를 거치므로 서로 다른 쌍을 읽을 수 없다. 목표와 마일스톤은 각각 독립해서 읽는다
    (인자 > 지시서 줄). UUID 형식이 아닌 값은 없는 것으로 본다. 인자와 지시서 줄이 서로 다른
    값을 가리키면 어느 쪽도 고르지 않고 ``conflict`` ("goal"/"milestone") 를 표시한다.
    """
    text = instruction or ""
    goal_value, goal_source, goal_conflict = _pick_declared(goal_id, _GOAL_ID_PATTERN, text)
    ms_value, ms_source, ms_conflict = _pick_declared(milestone_id, _MILESTONE_ID_PATTERN, text)
    if goal_conflict:
        return DeclaredIds(None, None, None, None, "goal", goal_conflict)
    if ms_conflict:
        return DeclaredIds(goal_value, None, goal_source, None, "milestone", ms_conflict)
    return DeclaredIds(goal_value, ms_value, goal_source, ms_source)


def parse_goal_binding(
    instruction: Optional[str] = None,
    *,
    goal_id: Optional[str] = None,
    milestone_id: Optional[str] = None,
) -> GoalBinding:
    """명시적 목표 컨텍스트를 해석한다 (`resolve_declared_ids` 위의 얇은 래퍼).

    목표가 없으면 EMPTY_BINDING 을 돌려주고 호출부는 **아무 목표에도 연결하지 않는다**.
    인자와 지시서가 충돌해도 EMPTY_BINDING 이다 — 제출 단계는 게이트가 먼저 422 로 거부하고,
    그 밖의 경로는 어느 쪽인지 모르는 채 붙이지 않는다.
    """
    declared = resolve_declared_ids(instruction, goal_id=goal_id, milestone_id=milestone_id)
    if declared.conflict or not declared.goal_id:
        return EMPTY_BINDING
    return GoalBinding(declared.goal_id, declared.milestone_id, declared.goal_source)


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
