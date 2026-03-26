"""
AADS CEO 아젠다 관리 서비스.
CEO와 각 프로젝트 CTO가 전략 논의/미결정 사항을 저장·추적·재개할 수 있도록 지원.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

VALID_PROJECTS = {"AADS", "KIS", "GO100", "SF", "NTV2", "NAS"}
VALID_STATUSES = {"논의중", "보류", "결정", "진행중", "완료"}
VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}


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
        summary: str,
        priority: str = "P2",
        tags: Optional[List[str]] = None,
        created_by: str = "CEO",
        source_session_id: Optional[str] = None,
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
        """
        if project not in VALID_PROJECTS:
            raise ValueError(f"유효하지 않은 프로젝트: {project}. 허용: {VALID_PROJECTS}")
        if priority not in VALID_PRIORITIES:
            raise ValueError(f"유효하지 않은 우선순위: {priority}. 허용: {VALID_PRIORITIES}")

        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO ceo_agenda
                    (project, title, summary, priority, tags, created_by, source_session_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """,
                project,
                title,
                summary,
                priority,
                tags or [],
                created_by,
                source_session_id,
            )
        logger.info("agenda_added", id=row["id"], project=project, title=title[:50])
        return _row_to_dict(row)

    async def list_agendas(
        self,
        project: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """아젠다 목록 조회.

        Args:
            project: None이면 전체(CEO용), 지정 시 해당 프로젝트만(CTO용)
            status: 상태 필터 (논의중, 보류, 결정, 진행중, 완료)
            priority: 우선순위 필터 (P0~P3)
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

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT * FROM ceo_agenda
            {where_clause}
            ORDER BY
                CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                created_at DESC
        """

        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [_row_to_dict(r) for r in rows]

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

    async def update_agenda(self, agenda_id: int, **kwargs) -> Optional[Dict[str, Any]]:
        """아젠다 상태/내용 업데이트.

        Args:
            agenda_id: 아젠다 ID
            **kwargs: 변경할 필드 (title, summary, status, priority, tags, source_session_id)
        """
        allowed_fields = {"title", "summary", "status", "priority", "tags", "source_session_id"}
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields and v is not None}
        if not updates:
            raise ValueError("업데이트할 필드가 없습니다.")

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

    async def decide_agenda(self, agenda_id: int, decision: str) -> Optional[Dict[str, Any]]:
        """CEO 결정 기록 — status='결정', decision 저장, decision_at=now.

        Args:
            agenda_id: 아젠다 ID
            decision: CEO 결정 내용
        """
        now = datetime.now(timezone.utc)
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE ceo_agenda
                SET status = '결정',
                    decision = $1,
                    decision_at = $2,
                    updated_at = $2
                WHERE id = $3
                RETURNING *
                """,
                decision,
                now,
                agenda_id,
            )
        if row is None:
            return None
        logger.info("agenda_decided", id=agenda_id, decision=decision[:80])
        return _row_to_dict(row)

    async def search_agendas(self, keyword: str) -> List[Dict[str, Any]]:
        """title/summary/tags 텍스트 검색.

        Args:
            keyword: 검색 키워드
        """
        pool = get_pool()
        async with pool.acquire() as conn:
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
                """,
                f"%{keyword}%",
            )
        return [_row_to_dict(r) for r in rows]


_service: Optional[AgendaService] = None


def get_agenda_service() -> AgendaService:
    """싱글톤 AgendaService 반환."""
    global _service
    if _service is None:
        _service = AgendaService()
    return _service
