"""
Auto-Fix Dispatcher — 오류 감지 → Pipeline Runner 자동 수정 작업 제출.

감시 대상:
1. 소스 오류: uvicorn 시작 실패, ImportError, SyntaxError
2. 기능 오류: API 500 에러 반복 (3회+), NoneType, DB 오류
3. 접속 오류: 서비스 다운, health check 실패, 502/504

동작:
- 5분마다 error_log 스캔
- 자동 수정 가능한 오류 패턴 매칭
- Pipeline Runner에 수정 작업 제출 (pipeline_jobs INSERT)
- CEO 채팅방에 보고
- 동일 오류 중복 제출 방지 (24시간 쿨다운)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

# 오류 패턴 → 수정 지시 매핑
_ERROR_FIX_RULES: List[Dict] = [
    # ── 소스 오류 ──
    {
        "pattern": ["ImportError", "ModuleNotFoundError", "No module named"],
        "project_detect": True,  # error_log.source에서 프로젝트 추론
        "instruction": (
            "서버 로그에서 다음 import 오류가 발생했습니다:\n\n{error_message}\n\n"
            "1. 해당 모듈의 import 경로를 확인하고 수정하세요.\n"
            "2. 누락된 의존성이 있으면 requirements.txt에 추가하세요.\n"
            "3. 수정 후 python3 -c 'import 모듈명'으로 검증하세요.\n"
            "4. 서비스를 재시작하고 health check를 확인하세요."
        ),
        "severity": "critical",
        "auto_approve": False,
    },
    {
        "pattern": ["SyntaxError", "IndentationError", "TabError"],
        "project_detect": True,
        "instruction": (
            "서버 로그에서 다음 문법 오류가 발생했습니다:\n\n{error_message}\n\n"
            "1. 해당 파일:라인을 찾아 문법 오류를 수정하세요.\n"
            "2. py_compile로 검증하세요.\n"
            "3. ruff check로 추가 오류가 없는지 확인하세요."
        ),
        "severity": "critical",
        "auto_approve": False,
    },
    # ── 기능 오류 ──
    {
        "pattern": ["NoneType", "AttributeError: 'NoneType'"],
        "project_detect": True,
        "min_occurrences": 3,
        "instruction": (
            "다음 NoneType 오류가 {occurrence_count}회 반복 발생했습니다:\n\n{error_message}\n\n"
            "스택트레이스:\n{stack_trace}\n\n"
            "1. 해당 코드에서 None 체크를 추가하세요.\n"
            "2. 방어적 코딩으로 None 전파를 차단하세요.\n"
            "3. 단위 테스트를 추가하여 재발을 방지하세요."
        ),
        "severity": "high",
        "auto_approve": False,
    },
    {
        "pattern": ["500 Internal Server Error", "Traceback", "Unhandled exception"],
        "project_detect": True,
        "min_occurrences": 3,
        "instruction": (
            "API 500 에러가 {occurrence_count}회 반복 발생했습니다:\n\n{error_message}\n\n"
            "스택트레이스:\n{stack_trace}\n\n"
            "1. 에러의 근본 원인을 분석하세요.\n"
            "2. 적절한 에러 핸들링을 추가하세요.\n"
            "3. 수정 후 해당 API 엔드포인트를 테스트하세요."
        ),
        "severity": "high",
        "auto_approve": False,
    },
    {
        "pattern": ["NotNullViolation", "IntegrityError", "UniqueViolation"],
        "project_detect": True,
        "min_occurrences": 2,
        "instruction": (
            "DB 무결성 오류가 {occurrence_count}회 발생했습니다:\n\n{error_message}\n\n"
            "1. INSERT/UPDATE 전 필수 필드 검증을 추가하세요.\n"
            "2. 시퀀스 정합성을 확인하세요 (setval).\n"
            "3. ON CONFLICT 처리가 적절한지 확인하세요."
        ),
        "severity": "high",
        "auto_approve": False,
    },
    # ── 접속 오류 ──
    {
        "pattern": ["Connection refused", "Connection reset", "ECONNREFUSED"],
        "project_detect": True,
        "min_occurrences": 5,
        "instruction": (
            "서비스 접속 오류가 {occurrence_count}회 발생했습니다:\n\n{error_message}\n\n"
            "1. 해당 서비스의 프로세스 상태를 확인하세요.\n"
            "2. 포트 바인딩 상태를 확인하세요.\n"
            "3. 필요시 서비스를 재시작하고 health check를 확인하세요."
        ),
        "severity": "medium",
        "auto_approve": False,
    },
]

# 프로젝트 추론 매핑
_SOURCE_TO_PROJECT = {
    "aads": "AADS",
    "kis": "KIS",
    "go100": "GO100",
    "shortflow": "SF",
    "sf": "SF",
    "ntv2": "NTV2",
    "newtalk": "NTV2",
}


def _detect_project(error_row: dict) -> str:
    """error_log row에서 프로젝트 추론."""
    source = (error_row.get("source") or "").lower()
    server = (error_row.get("server") or "").lower()
    message = (error_row.get("message") or "").lower()

    for key, project in _SOURCE_TO_PROJECT.items():
        if key in source or key in server or key in message:
            return project

    # 기본값
    return "AADS"


async def scan_and_dispatch() -> Dict:
    """error_log 스캔 → 자동 수정 작업 제출. 5분마다 호출."""
    from app.core.db_pool import get_pool
    pool = get_pool()
    dispatched = []
    skipped = 0

    try:
        async with pool.acquire() as conn:
            # 최근 30분 이내, 미해결 오류 조회
            errors = await conn.fetch("""
                SELECT id, error_type, source, server, message,
                       stack_trace, occurrence_count, created_at, auto_recoverable
                FROM error_log
                WHERE resolved_at IS NULL
                  AND created_at > NOW() - INTERVAL '30 minutes'
                ORDER BY occurrence_count DESC, created_at DESC
                LIMIT 20
            """)

            for error in errors:
                msg = error["message"] or ""
                stack = error["stack_trace"] or ""
                full_text = msg + " " + stack

                for rule in _ERROR_FIX_RULES:
                    # 패턴 매칭
                    if not any(pat.lower() in full_text.lower() for pat in rule["pattern"]):
                        continue

                    # 최소 발생 횟수 체크
                    min_occ = rule.get("min_occurrences", 1)
                    if (error["occurrence_count"] or 1) < min_occ:
                        continue

                    # 프로젝트 감지
                    project = _detect_project(error) if rule.get("project_detect") else "AADS"

                    # 중복 제출 방지: 같은 error_hash로 24시간 내 이미 작업 제출했는지
                    error_hash = error.get("error_hash") or str(error["id"])
                    existing = await conn.fetchval("""
                        SELECT COUNT(*) FROM pipeline_jobs
                        WHERE instruction LIKE $1
                          AND created_at > NOW() - INTERVAL '24 hours'
                          AND status NOT IN ('error', 'rejected')
                    """, f"%{error_hash[:20]}%")

                    if existing and existing > 0:
                        skipped += 1
                        continue

                    # 지시 생성
                    instruction = rule["instruction"].format(
                        error_message=msg[:500],
                        stack_trace=stack[:1000],
                        occurrence_count=error["occurrence_count"] or 1,
                    )
                    instruction = f"[자동 수정 요청] error_hash={error_hash[:20]}\n\n{instruction}"

                    # 세션 ID 조회
                    session_id = ""
                    try:
                        row = await conn.fetchrow("""
                            SELECT s.id::text FROM chat_sessions s
                            JOIN chat_workspaces w ON s.workspace_id = w.id
                            WHERE w.name ILIKE $1
                            ORDER BY s.updated_at DESC LIMIT 1
                        """, f"[{project}]%")
                        if row:
                            session_id = row["id"]
                    except Exception:
                        pass

                    # Pipeline Runner 작업 제출
                    job_id = f"autofix-{uuid.uuid4().hex[:8]}"
                    await conn.execute("""
                        INSERT INTO pipeline_jobs
                          (job_id, project, instruction, chat_session_id, status, phase, max_cycles, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, 'queued', 'queued', 3, NOW(), NOW())
                    """, job_id, project, instruction, session_id)

                    # error_log에 수정 시도 기록
                    await conn.execute("""
                        UPDATE error_log SET recovery_command = $1, auto_recoverable = true
                        WHERE id = $2
                    """, f"pipeline_runner:{job_id}", error["id"])

                    dispatched.append({
                        "job_id": job_id,
                        "project": project,
                        "error_type": error["error_type"],
                        "severity": rule["severity"],
                    })
                    logger.warning(
                        "auto_fix_dispatched",
                        job_id=job_id,
                        project=project,
                        error_type=error["error_type"],
                        severity=rule["severity"],
                        occurrence_count=error["occurrence_count"],
                    )
                    break  # 하나의 오류에 하나의 규칙만 적용

    except Exception as e:
        logger.error("auto_fix_scan_error", error=str(e))

    result = {"dispatched": len(dispatched), "skipped": skipped, "jobs": dispatched}
    if dispatched:
        logger.info("auto_fix_scan_complete", **result)
    return result
