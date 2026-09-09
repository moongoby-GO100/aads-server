"""명시적 Goal 바인딩 계약 (Goal Control P0).

기존 `_auto_link_job_to_goal` 은 **프로젝트만** 보고 "그 프로젝트의 첫 active 목표"에
작업을 붙였다. 그래서 목표와 아무 상관없는 AADS 작업(OHVIS trace receiver 등)까지
활성 목표 "채팅 시스템 안정화 및 응답 가독성 개선" 에 매달렸다.

이 모듈은 **명시적 goal 컨텍스트만** 바인딩 근거로 인정한다.

우선순위
    1. 호출자가 넘긴 goal_id/milestone_id (submit 요청 필드)
    2. 지시서 메타데이터 계약 — `GOAL_ID: <uuid>` / `MILESTONE_ID: <uuid>`
    3. 없으면 **연결하지 않는다** (활성 목표로의 암묵적 흡착 금지)

어떤 경로든 project 일치와 목표 open 상태를 검증한 뒤에만 바인딩한다.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─── 바인딩 출처(provenance) ──────────────────────────────────────────────────
BIND_SOURCE_REQUEST = "request"      # submit 요청의 goal_id/milestone_id 필드
BIND_SOURCE_DIRECTIVE = "directive"  # 지시서 GOAL_ID:/MILESTONE_ID: 메타데이터
BIND_SOURCE_EXPLICIT = "explicit"    # Goals API link-task 직접 호출
BIND_SOURCE_LEGACY_AUTO = "legacy_auto"  # 구 project-only 자동 연결(마이그레이션 백필)

_OPEN_GOAL_STATUSES = ("active", "draft")

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# 지시서 메타데이터 계약: 줄 시작의 `GOAL_ID: <uuid>` (선행 목록기호/공백 허용)
_GOAL_ID_RE = re.compile(rf"^[\s\-*#>]*GOAL_ID\s*[:=]\s*({_UUID})\s*$", re.MULTILINE)
_MILESTONE_ID_RE = re.compile(rf"^[\s\-*#>]*MILESTONE_ID\s*[:=]\s*({_UUID})\s*$", re.MULTILINE)


def parse_goal_directives(instruction: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """지시서에서 `GOAL_ID:` / `MILESTONE_ID:` 메타데이터를 읽는다.

    본문 어디에도 없으면 (None, None). 여러 번 나오면 **첫 선언**만 쓴다.
    """
    text = instruction or ""
    if not text:
        return None, None
    goal_match = _GOAL_ID_RE.search(text)
    ms_match = _MILESTONE_ID_RE.search(text)
    return (
        goal_match.group(1).lower() if goal_match else None,
        ms_match.group(1).lower() if ms_match else None,
    )


def _normalize_uuid(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    return text if re.fullmatch(_UUID, text) else None


def _unbound(reason: str, **extra: Any) -> dict[str, Any]:
    return {"bound": False, "goal_id": None, "milestone_id": None,
            "bind_source": None, "reason": reason, **extra}


async def resolve_goal_binding(
    conn: Any,
    project: str,
    *,
    goal_id: Optional[str] = None,
    milestone_id: Optional[str] = None,
    instruction: Optional[str] = None,
) -> dict[str, Any]:
    """명시적 goal 컨텍스트를 검증해 바인딩 결정을 돌려준다.

    반환: `{"bound", "goal_id", "milestone_id", "bind_source", "reason"}`.
    `bound=False` 면 **연결하지 않는다** — reason 이 거절 사유다.
    부작용(쓰기)은 없다. 판정만 한다.
    """
    requested_goal = _normalize_uuid(goal_id)
    requested_ms = _normalize_uuid(milestone_id)
    bind_source = BIND_SOURCE_REQUEST

    if not requested_goal:
        directive_goal, directive_ms = parse_goal_directives(instruction)
        if directive_goal:
            requested_goal = directive_goal
            requested_ms = requested_ms or directive_ms
            bind_source = BIND_SOURCE_DIRECTIVE

    if not requested_goal:
        # 명시적 근거 없음 → 활성 목표에 조용히 붙이지 않는다 (P0 결함 #1)
        return _unbound("no_explicit_goal_context")

    goal = await conn.fetchrow(
        "SELECT id, project, status FROM goals WHERE id = $1::uuid",
        requested_goal,
    )
    if not goal:
        return _unbound("goal_not_found", requested_goal_id=requested_goal)
    if (goal["project"] or "") != (project or ""):
        return _unbound(
            "goal_project_mismatch",
            requested_goal_id=requested_goal,
            goal_project=goal["project"],
            job_project=project,
        )
    if goal["status"] not in _OPEN_GOAL_STATUSES:
        return _unbound(
            "goal_not_open",
            requested_goal_id=requested_goal,
            goal_status=goal["status"],
        )

    if requested_ms:
        ms = await conn.fetchrow(
            "SELECT id, goal_id FROM milestones WHERE id = $1::uuid",
            requested_ms,
        )
        if not ms:
            return _unbound("milestone_not_found", requested_milestone_id=requested_ms)
        if str(ms["goal_id"]) != requested_goal:
            return _unbound(
                "milestone_goal_mismatch",
                requested_goal_id=requested_goal,
                requested_milestone_id=requested_ms,
            )

    return {
        "bound": True,
        "goal_id": requested_goal,
        "milestone_id": requested_ms,
        "bind_source": bind_source,
        "reason": "explicit_binding_validated",
    }


# ─── 스키마 기능 탐지 ─────────────────────────────────────────────────────────
# 마이그레이션 165 적용 전/후 어디서도 동작해야 하므로(무중단 배포), provenance
# 컬럼 존재 여부를 한 번만 확인하고 캐시한다.
_supersession_supported: Optional[bool] = None


async def has_supersession_columns(conn: Any) -> bool:
    """goal_task_links 에 supersession/provenance 컬럼이 있는지(캐시됨)."""
    global _supersession_supported
    if _supersession_supported is not None:
        return _supersession_supported
    try:
        found = await conn.fetchval(
            """
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'goal_task_links'
              AND column_name IN ('bind_source', 'superseded_by')
            """
        )
        _supersession_supported = int(found or 0) >= 2
    except Exception as exc:  # noqa: BLE001 — 탐지 실패는 "미지원"으로 취급
        logger.debug("goal_link_schema_probe_failed: %s", str(exc)[:200])
        _supersession_supported = False
    return _supersession_supported


def reset_schema_probe_cache() -> None:
    """마이그레이션 직후/테스트에서 기능 탐지 캐시를 비운다."""
    global _supersession_supported
    _supersession_supported = None
