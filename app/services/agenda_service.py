"""
AADS CEO 아젠다 관리 서비스.
CEO와 각 프로젝트 CTO가 전략 논의/미결정 사항을 저장·추적·재개할 수 있도록 지원.
"""
from __future__ import annotations

import structlog
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.db_pool import get_pool

logger = structlog.get_logger(__name__)

VALID_PROJECTS = {"AADS", "KIS", "GO100", "SF", "NTV2", "NAS"}
VALID_STATUSES = {"논의중", "보류", "결정", "진행중", "완료", "폐기"}
VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}

# CTO가 전환 가능한 상태 쌍 (현재→목표)
CTO_ALLOWED_TRANSITIONS = {
    ("논의중", "보류"),
    ("보류", "논의중"),
}


def _row_to_dict(row) -> Dict[str, Any]:
    """asyncpg Record → dict 변환."""
    if row is None:
        return {}
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


class AgendaService:
    """CEO 아젠다 관리 서비스 — asyncpg pool 사용."""

    async def add_agenda(
        self,
        project: str,
        title: str,
        summary: Optional[str] = None,
        priority: str = "P2",
        tags: Optional[List[str]] = None,
        created_by: str = "CEO",
        source_session_id: Optional[str] = None,
        related_task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """아젠다 등록.

        Args:
            project: 프로젝트 코드 (AADS, KIS, GO100, SF, NTV2, NAS)
            title: 아젠다 제목
            summary: 핵심 논점 + 옵션 + 미결정 사항 (마크다운)
            priority: P0~P3
            tags: 검색용 태그 목록
            created_by: CEO 또는 프로젝트명(CTO)
            source_session_id: 논의가 발생한 세션 ID
            related_task_id: 연결된 지시서 ID
        """
        if priority not in VALID_PRIORITIES:
            raise ValueError(f"유효하지 않은 우선순위: {priority}. 허용: {VALID_PRIORITIES}")

        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO ceo_agenda
                    (project, title, summary, priority, tags, created_by,
                     source_session_id, related_task_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING *
                """,
                project,
                title,
                summary,
                priority,
                tags or [],
                created_by,
                source_session_id,
                related_task_id,
            )
        logger.info("agenda_added", id=row["id"], project=project, title=title[:50])
        return _row_to_dict(row)

    async def create_agenda(self, **kwargs) -> Dict[str, Any]:
        """add_agenda 별칭 (API 호환성)."""
        return await self.add_agenda(**kwargs)

    async def list_agendas(
        self,
        project: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        created_by: Optional[str] = None,
        source_session_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """아젠다 목록 조회 (페이지네이션 포함).

        Args:
            project: None이면 전체(CEO용), 지정 시 해당 프로젝트만(CTO용)
            status: 상태 필터
            priority: 우선순위 필터
            created_by: 등록자 필터
            source_session_id: 세션 ID 필터
            limit: 페이지 크기 (기본 20)
            offset: 시작 위치 (기본 0)
        """
        conditions = []
        params: List[Any] = []

        if project is not None:
            params.append(project)
            conditions.append(f"project = ${len(params)}")
        if status is not None:
            params.append(status)
            conditions.append(f"status = ${len(params)}")
        if priority is not None:
            params.append(priority)
            conditions.append(f"priority = ${len(params)}")
        if created_by is not None:
            params.append(created_by)
            conditions.append(f"created_by = ${len(params)}")
        if source_session_id is not None:
            params.append(source_session_id)
            conditions.append(f"source_session_id = ${len(params)}")

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        count_query = f"SELECT COUNT(*) FROM ceo_agenda {where_clause}"
        list_query = f"""
            SELECT * FROM ceo_agenda
            {where_clause}
            ORDER BY
                CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                created_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """

        pool = get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(count_query, *params)
            rows = await conn.fetch(list_query, *params, limit, offset)

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [_row_to_dict(r) for r in rows],
        }

    async def get_agenda(self, agenda_id: int) -> Optional[Dict[str, Any]]:
        """아젠다 단건 조회.

        Args:
            agenda_id: 아젠다 ID
        """
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM ceo_agenda WHERE id = $1", agenda_id
            )
        return _row_to_dict(row) if row else None

    async def update_agenda(
        self,
        agenda_id: int,
        caller_role: str = "CEO",
        caller_project: Optional[str] = None,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        """아젠다 상태/내용 업데이트.

        Args:
            agenda_id: 아젠다 ID
            caller_role: 호출자 역할 (CEO/CTO)
            caller_project: CTO의 담당 프로젝트
            **kwargs: 변경할 필드
        """
        pool = get_pool()
        async with pool.acquire() as conn:
            current = await conn.fetchrow(
                "SELECT * FROM ceo_agenda WHERE id = $1", agenda_id
            )
        if not current:
            return None

        # CTO 권한 검증
        if caller_role != "CEO":
            if caller_project and current["project"] != caller_project:
                raise PermissionError("CTO는 자신의 프로젝트 아젠다만 수정할 수 있습니다.")
            if "status" in kwargs and kwargs["status"] is not None:
                transition = (current["status"], kwargs["status"])
                if transition not in CTO_ALLOWED_TRANSITIONS:
                    raise PermissionError(
                        f"CTO는 '논의중↔보류' 전환만 가능합니다. (현재: {current['status']})"
                    )
            for forbidden in ("decision", "decided_at", "decided_by", "priority"):
                if kwargs.get(forbidden) is not None:
                    raise PermissionError(f"CTO는 '{forbidden}' 필드를 수정할 수 없습니다.")

        allowed_fields = {
            "title", "summary", "status", "priority", "tags",
            "source_session_id", "related_task_id",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields and v is not None}
        if not updates:
            return _row_to_dict(current)

        if "status" in updates and updates["status"] not in VALID_STATUSES:
            raise ValueError(f"유효하지 않은 상태: {updates['status']}. 허용: {VALID_STATUSES}")
        if "priority" in updates and updates["priority"] not in VALID_PRIORITIES:
            raise ValueError(f"유효하지 않은 우선순위: {updates['priority']}. 허용: {VALID_PRIORITIES}")

        set_clauses = []
        params: List[Any] = []
        for field, value in updates.items():
            params.append(value)
            set_clauses.append(f"{field} = ${len(params)}")

        params.append(datetime.now(timezone.utc))
        set_clauses.append(f"updated_at = ${len(params)}")

        params.append(agenda_id)
        query = f"""
            UPDATE ceo_agenda
            SET {', '.join(set_clauses)}
            WHERE id = ${len(params)}
            RETURNING *
        """

        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)

        if row is None:
            return None
        logger.info("agenda_updated", id=agenda_id, fields=list(updates.keys()))
        return _row_to_dict(row)

    async def decide_agenda(
        self,
        agenda_id: int,
        decision: str,
        decided_by: str = "CEO",
        caller_role: str = "CEO",
    ) -> Optional[Dict[str, Any]]:
        """CEO 결정 기록 — status='결정', decision 저장, decided_at=now.

        Args:
            agenda_id: 아젠다 ID
            decision: CEO 결정 내용
            decided_by: 결정자 이름
            caller_role: 호출자 역할 (CEO만 허용)
        """
        if caller_role != "CEO":
            raise PermissionError("결정(decide)은 CEO만 수행할 수 있습니다.")

        now = datetime.now(timezone.utc)
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE ceo_agenda
                SET status = '결정',
                    decision = $1,
                    decided_at = $2,
                    decided_by = $3,
                    updated_at = $2
                WHERE id = $4
                RETURNING *
                """,
                decision,
                now,
                decided_by,
                agenda_id,
            )
        if row is None:
            return None
        logger.info("agenda_decided", id=agenda_id, decided_by=decided_by)
        return _row_to_dict(row)

    async def list_sessions(
        self,
        project: Optional[str] = None,
    ) -> List[str]:
        """아젠다에 연결된 고유 세션 ID 목록 반환."""
        pool = get_pool()
        async with pool.acquire() as conn:
            if project:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT source_session_id
                    FROM ceo_agenda
                    WHERE source_session_id IS NOT NULL AND project = $1
                    ORDER BY source_session_id
                    """,
                    project,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT source_session_id
                    FROM ceo_agenda
                    WHERE source_session_id IS NOT NULL
                    ORDER BY source_session_id
                    """
                )
        return [r["source_session_id"] for r in rows]

    async def search_agendas(
        self,
        keyword: str,
        project: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """title/summary/tags 텍스트 검색.

        Args:
            keyword: 검색 키워드
            project: 프로젝트 필터 (없으면 전체)
            limit: 최대 결과 수
        """
        pattern = f"%{keyword}%"
        pool = get_pool()
        async with pool.acquire() as conn:
            if project:
                rows = await conn.fetch(
                    """
                    SELECT * FROM ceo_agenda
                    WHERE project = $1
                      AND (title ILIKE $2 OR summary ILIKE $2
                           OR EXISTS (
                               SELECT 1 FROM unnest(tags) AS t(tag) WHERE t.tag ILIKE $2
                           ))
                    ORDER BY created_at DESC
                    LIMIT $3
                    """,
                    project,
                    pattern,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM ceo_agenda
                    WHERE
                        title ILIKE $1
                        OR summary ILIKE $1
                        OR EXISTS (
                            SELECT 1 FROM unnest(tags) AS t(tag) WHERE t.tag ILIKE $1
                        )
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    pattern,
                    limit,
                )
        return [_row_to_dict(r) for r in rows]


_service: Optional[AgendaService] = None


def get_agenda_service() -> AgendaService:
    """싱글톤 AgendaService 반환."""
    global _service
    if _service is None:
        _service = AgendaService()
    return _service
