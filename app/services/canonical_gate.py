"""정본 강제 게이트 — 그림자 모드(AADS-CANONICAL-GATE-SHADOW-20261002).

세 진입점(목표 문서 API / 커밋 hook / 러너 제출)에서 "정본(project_document_heads)
없이 문서가 만들어지는가" 를 감지해 경고·기록만 한다. **어떤 경우에도 차단하지
않는다** — 거짓 양성으로 우회 습관이 생기는 것을 막으려고 1주간 실측만 한다.

불변식
  - fail-open: 판정·기록 중 예외/타임아웃은 전부 삼키고 원래 흐름을 통과시킨다.
  - mode=off 이면 DB 를 한 번도 건드리지 않는다(pool 획득 포함).
  - 호출부는 이 모듈이 돌려준 경고(dict|None)를 응답에 **선택 필드**로만 붙인다.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Awaitable, Callable, Optional

import structlog

logger = structlog.get_logger(__name__)

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
_MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)

ENTRY_GOAL_API = "goal_api"
ENTRY_COMMIT = "commit"
ENTRY_RUNNER_SUBMIT = "runner_submit"

VERDICT_OK = "ok"
VERDICT_MISSING = "missing_canonical"
VERDICT_UNKNOWN = "unknown"

_DEFAULT_TIMEOUT_MS = 300
_JUDGE_BUDGET_RATIO = 0.7
# 정본 head 의 kind CHECK 에 없는 종류는 정본이 될 수 없으므로 판정 대상이 아니다.
_NOT_CANONICAL_ELIGIBLE_KINDS = frozenset({"prototype"})

# scripts/canonical_gate_commit.py 의 CANDIDATE_RE 와 같아야 한다
# (tests/unit/test_canonical_gate_shadow.py 가 대조한다).
CANDIDATE_PATH_RE = re.compile(r"^docs/(prd|plans|design|contracts)/")


def get_mode() -> str:
    """환경변수 CANONICAL_GATE_MODE. 알 수 없는 값은 기본(shadow)으로 둔다."""
    raw = (os.getenv("CANONICAL_GATE_MODE") or MODE_SHADOW).strip().lower()
    return raw if raw in _MODES else MODE_SHADOW


def get_timeout_seconds() -> float:
    try:
        ms = float(os.getenv("CANONICAL_GATE_TIMEOUT_MS") or _DEFAULT_TIMEOUT_MS)
    except ValueError:
        ms = _DEFAULT_TIMEOUT_MS
    return max(ms, 1.0) / 1000.0


def is_candidate_path(path: str) -> bool:
    return bool(CANDIDATE_PATH_RE.match((path or "").lstrip("./")))


_GOAL_DOC_SQL = """
SELECT g.project,
       EXISTS (SELECT 1 FROM project_document_heads h
                WHERE h.tenant_id = g.tenant_id AND h.project_key = g.project
                  AND h.document_key = $2) AS key_match,
       EXISTS (SELECT 1 FROM project_document_revisions r
                WHERE r.tenant_id = g.tenant_id AND r.project_key = g.project
                  AND r.source_path = $3) AS path_match
  FROM goals g
 WHERE g.id = $1::uuid AND g.tenant_id = $4::uuid
"""

_RUNNER_GOAL_SQL = """
SELECT (SELECT count(*) FROM project_document_heads h
         WHERE h.tenant_id = $2::uuid AND h.project_key = $3
           AND h.approved_revision_id IS NOT NULL) AS project_approved,
       (SELECT count(*) FROM project_document_heads h
         WHERE h.tenant_id = $2::uuid AND h.project_key = $3
           AND h.approved_revision_id IS NOT NULL
           AND (EXISTS (SELECT 1 FROM project_document_goal_links l
                         WHERE l.head_id = h.id AND l.goal_id = $1::uuid)
                OR EXISTS (SELECT 1 FROM project_document_revisions r
                            WHERE r.head_id = h.id AND r.goal_id = $1::uuid))
       ) AS goal_approved
"""

_INSERT_EVENT_SQL = (
    "INSERT INTO canonical_gate_events "
    "(entrypoint, project, ref, verdict, detail, mode) "
    "VALUES ($1, $2, $3, $4, $5::jsonb, $6)"
)

Judge = Callable[[Any], Awaitable[tuple[str, dict[str, Any], Optional[str]]]]


def judge_goal_document_rows(row: Any, *, kind: str) -> tuple[str, dict[str, Any]]:
    """목표 문서 API 판정 규칙(순수 함수). row 는 _GOAL_DOC_SQL 결과."""
    if row is None:
        return VERDICT_UNKNOWN, {"reason": "goal_not_found"}
    if kind in _NOT_CANONICAL_ELIGIBLE_KINDS:
        return VERDICT_OK, {"reason": "kind_not_canonical_eligible", "kind": kind}
    detail = {
        "kind": kind,
        "key_match": bool(row["key_match"]),
        "path_match": bool(row["path_match"]),
    }
    if detail["key_match"] or detail["path_match"]:
        return VERDICT_OK, detail
    return VERDICT_MISSING, detail


def judge_runner_goal_rows(row: Any) -> tuple[str, dict[str, Any]]:
    """러너 제출 판정 규칙(순수 함수). 목표·프로젝트 모두 승인 정본 0건일 때만 missing."""
    if row is None:
        return VERDICT_UNKNOWN, {"reason": "no_row"}
    detail = {
        "goal_approved": int(row["goal_approved"]),
        "project_approved": int(row["project_approved"]),
    }
    if detail["goal_approved"] == 0 and detail["project_approved"] == 0:
        return VERDICT_MISSING, detail
    return VERDICT_OK, detail


def _warning(verdict: str, mode: str, entrypoint: str, ref: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "mode": mode,
        "entrypoint": entrypoint,
        "ref": ref,
        "detail": detail,
        "message": (
            "정본 미등록 — 그림자 모드라 차단하지 않고 기록만 합니다. "
            "정본 등록: POST /api/v1/projects/{project_key}/documents"
        ),
    }


async def _record(
    pool: Any, entrypoint: str, project: Optional[str], ref: str,
    verdict: str, detail: dict[str, Any], mode: str, timeout: float,
) -> None:
    """canonical_gate_events 에 한 줄 남긴다. 테이블이 없거나 느리면 로그만 남기고 끝."""
    try:
        async def _insert() -> None:
            async with pool.acquire() as conn:
                await conn.execute(
                    _INSERT_EVENT_SQL, entrypoint, project, ref, verdict,
                    json.dumps(detail, ensure_ascii=False, default=str), mode,
                )

        await asyncio.wait_for(_insert(), timeout)
    except Exception as exc:  # fail-open: 기록 실패가 요청을 막으면 안 된다
        logger.info(
            "canonical_gate.record_skipped", entrypoint=entrypoint, ref=ref, verdict=verdict,
            error=type(exc).__name__,
        )


async def _run(
    entrypoint: str, *, ref: str, project: Optional[str], judge: Judge, pool: Any = None,
) -> Optional[dict[str, Any]]:
    """판정+기록. 경고(dict) 는 missing_canonical 일 때만 돌려준다. 절대 raise 하지 않는다."""
    mode = get_mode()
    if mode == MODE_OFF:
        return None
    try:
        # 300ms 는 판정+기록 합산 상한이다. 판정에 70% 를, 기록에는 남은 예산만 준다.
        loop = asyncio.get_running_loop()
        total = get_timeout_seconds()
        deadline = loop.time() + total
        timeout = total * _JUDGE_BUDGET_RATIO
        if pool is None:
            from app.core.db_pool import get_pool

            pool = get_pool()
        detail: dict[str, Any]
        try:
            verdict, detail, judged_project = await asyncio.wait_for(judge(pool), timeout)
            project = judged_project or project
        except asyncio.TimeoutError:
            verdict, detail = VERDICT_UNKNOWN, {"reason": "timeout", "timeout_ms": int(total * 1000)}
        except Exception as exc:
            verdict, detail = VERDICT_UNKNOWN, {"reason": "error", "error": type(exc).__name__}
        if mode == MODE_ENFORCE:
            # TODO(enforce): 차단 전환은 1주 실측 후 CEO 별도 승인. 지금은 shadow 와 동일하게 통과시킨다.
            detail = {**detail, "enforce_not_implemented": True}
        await _record(
            pool, entrypoint, project, ref, verdict, detail, mode,
            max(deadline - loop.time(), 0.001),
        )
        if verdict == VERDICT_MISSING:
            return _warning(verdict, mode, entrypoint, ref, detail)
        return None
    except Exception as exc:  # 어떤 경우에도 호출부로 전파하지 않는다
        logger.warning("canonical_gate.failed_open", entrypoint=entrypoint, ref=ref, error=type(exc).__name__)
        return None


async def check_goal_document(
    *, goal_id: str, tenant_id: str, ref: str, kind: str, path: str,
    document_key: Optional[str], pool: Any = None,
) -> Optional[dict[str, Any]]:
    """목표 문서 등록(goal_api) 후 호출. 트랜잭션 밖에서, 새 커넥션으로 돈다."""

    async def judge(p: Any) -> tuple[str, dict[str, Any], Optional[str]]:
        async with p.acquire() as conn:
            row = await conn.fetchrow(_GOAL_DOC_SQL, goal_id, document_key or "", path, tenant_id)
        verdict, detail = judge_goal_document_rows(row, kind=kind)
        return verdict, detail, (row["project"] if row is not None else None)

    return await _run(ENTRY_GOAL_API, ref=ref, project=None, judge=judge, pool=pool)


async def check_runner_submit(
    *, goal_id: str, tenant_id: str, project: str, job_id: str, pool: Any = None,
) -> Optional[dict[str, Any]]:
    """러너 제출(runner_submit)에서 목표 연결이 확정된 뒤 호출."""

    async def judge(p: Any) -> tuple[str, dict[str, Any], Optional[str]]:
        async with p.acquire() as conn:
            row = await conn.fetchrow(_RUNNER_GOAL_SQL, goal_id, tenant_id, project)
        verdict, detail = judge_runner_goal_rows(row)
        return verdict, {**detail, "goal_id": goal_id}, project

    return await _run(ENTRY_RUNNER_SUBMIT, ref=job_id, project=project, judge=judge, pool=pool)
