"""
AADS-186E-2: 4계층 영속 메모리 관리자
Layer 1: Session Buffer (기존 chat_service 히스토리)
Layer 2: Working Memory — session_notes (세션 종료 시 요약 저장)
Layer 3: CKP — 코드베이스 지식 (기존 ckp_manager)
Layer 4: Meta Memory — ai_meta_memory (CEO 선호도, 패턴 학습)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import asyncpg

logger = logging.getLogger(__name__)


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql://", "postgres://") if url else url


async def _get_conn() -> asyncpg.Connection:
    return await asyncpg.connect(_db_url(), timeout=10)


# ─── 데이터 모델 ─────────────────────────────────────────────────────────────

@dataclass
class SessionNote:
    """Layer 2: 세션 요약 노트."""
    id: int = 0
    session_id: str = ""
    summary: str = ""
    key_decisions: List[str] = field(default_factory=list)
    action_items: List[str] = field(default_factory=list)
    unresolved_issues: List[str] = field(default_factory=list)
    projects_discussed: List[str] = field(default_factory=list)
    created_at: Optional[datetime] = None


@dataclass
class Memory:
    """Layer 4: 메타 메모리 항목."""
    id: int = 0
    category: str = ""
    key: str = ""
    value: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    last_used_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ─── MemoryManager ────────────────────────────────────────────────────────────

class MemoryManager:
    """
    AADS 4계층 영속 메모리 관리자.
    Layer 2(Working Memory) + Layer 4(Meta Memory) 담당.
    Layer 1(Session Buffer), Layer 3(CKP)는 기존 시스템 유지.
    """

    # ── Layer 2: Working Memory ──────────────────────────────────────────────

    async def save_session_note(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        summary: Optional[str] = None,
        key_decisions: Optional[List[str]] = None,
        action_items: Optional[List[str]] = None,
        unresolved_issues: Optional[List[str]] = None,
    ) -> SessionNote:
        """
        세션 종료 시 핵심 내용 요약 저장.
        summary가 없으면 Claude Haiku로 자동 요약 (~$0.001).
        """
        if not summary:
            summary, key_decisions, action_items, unresolved_issues = await self._auto_summarize(
                messages, key_decisions, action_items, unresolved_issues
            )

        # 언급된 프로젝트 추출
        projects = _extract_projects(messages)

        conn = await _get_conn()
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO session_notes
                    (session_id, summary, key_decisions, action_items, unresolved_issues, projects_discussed)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING *
                """,
                session_id,
                summary,
                key_decisions or [],
                action_items or [],
                unresolved_issues or [],
                projects,
            )
            return _row_to_session_note(row)
        except Exception as e:
            logger.error(f"memory_manager save_session_note error: {e}")
            return SessionNote(session_id=session_id, summary=summary)
        finally:
            await conn.close()

    async def get_recent_notes(self, count: int = 5) -> List[SessionNote]:
        """Context Builder Layer 2에 주입할 최근 노트 반환. 총합 1,500 토큰 이내."""
        conn = await _get_conn()
        try:
            rows = await conn.fetch(
                "SELECT * FROM session_notes ORDER BY created_at DESC LIMIT $1",
                min(count, 20),
            )
            notes = [_row_to_session_note(r) for r in rows]
            # 토큰 제한: 한국어 기준 1토큰 ≈ 1.5자 → 1500 토큰 ≈ 2250자
            _TOKEN_LIMIT_CHARS = 2250
            total = 0
            result = []
            for note in notes:
                text_len = len(note.summary) + sum(len(d) for d in note.key_decisions)
                if total + text_len > _TOKEN_LIMIT_CHARS:
                    break
                total += text_len
                result.append(note)
            return result
        except Exception as e:
            logger.debug(f"memory_manager get_recent_notes error: {e}")
            return []
        finally:
            await conn.close()

    # ── Layer 4: Meta Memory ─────────────────────────────────────────────────

    async def learn(self, category: str, key: str, value: Dict[str, Any]) -> None:
        """
        AI가 CEO 선호도, 프로젝트 패턴 등을 학습.
        UPSERT: 기존 키 있으면 업데이트 + confidence 증가 (최대 1.0).
        value는 완전 교체 (merge 아님).
        """
        valid_categories = {"ceo_preference", "project_pattern", "known_issue", "decision_history"}
        if category not in valid_categories:
            logger.warning(f"memory_manager learn: 유효하지 않은 category={category}")
            return

        conn = await _get_conn()
        try:
            await conn.execute(
                """
                INSERT INTO ai_meta_memory (category, key, value, confidence, updated_at)
                VALUES ($1, $2, $3::jsonb, 1.0, NOW())
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    confidence = LEAST(ai_meta_memory.confidence + 0.1, 1.0),
                    updated_at = NOW()
                """,
                category,
                key,
                json.dumps(value),
            )
            logger.debug(f"memory_manager learn: category={category} key={key}")
        except Exception as e:
            logger.error(f"memory_manager learn error: {e}")
        finally:
            await conn.close()

    async def recall(
        self,
        category: Optional[str] = None,
        query: Optional[str] = None,
    ) -> List[Memory]:
        """
        AI가 기억 검색.
        category 필터 + value JSONB 텍스트 검색.
        last_used_at 업데이트.
        """
        conn = await _get_conn()
        try:
            if category and query:
                rows = await conn.fetch(
                    """
                    SELECT * FROM ai_meta_memory
                    WHERE category = $1 AND value::text ILIKE $2
                    ORDER BY confidence DESC, updated_at DESC
                    LIMIT 20
                    """,
                    category,
                    f"%{query}%",
                )
            elif category:
                rows = await conn.fetch(
                    "SELECT * FROM ai_meta_memory WHERE category = $1 ORDER BY confidence DESC LIMIT 20",
                    category,
                )
            elif query:
                rows = await conn.fetch(
                    """
                    SELECT * FROM ai_meta_memory
                    WHERE value::text ILIKE $1 OR key ILIKE $1
                    ORDER BY confidence DESC, updated_at DESC
                    LIMIT 20
                    """,
                    f"%{query}%",
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM ai_meta_memory ORDER BY confidence DESC, updated_at DESC LIMIT 20"
                )

            memories = [_row_to_memory(r) for r in rows]

            # last_used_at 업데이트 (비동기 — 응답 지연 없음)
            if memories:
                ids = [m.id for m in memories]
                await conn.execute(
                    "UPDATE ai_meta_memory SET last_used_at = NOW() WHERE id = ANY($1)",
                    ids,
                )

            return memories
        except Exception as e:
            logger.debug(f"memory_manager recall error: {e}")
            return []
        finally:
            await conn.close()

    async def get_meta_context(self, max_tokens: int = 500) -> str:
        """
        Context Builder Layer 2에 주입할 메타 기억 요약.
        CEO 선호도 + 알려진 이슈 + 최근 결정 → 압축 텍스트 (500 토큰 이내).
        """
        conn = await _get_conn()
        try:
            rows = await conn.fetch(
                """
                SELECT category, key, value FROM ai_meta_memory
                WHERE category IN ('ceo_preference', 'known_issue', 'decision_history')
                ORDER BY confidence DESC, updated_at DESC
                LIMIT 30
                """,
            )
            if not rows:
                return ""

            lines: List[str] = []
            char_limit = max_tokens * 3  # 한국어 기준

            for r in rows:
                cat = r["category"]
                key = r["key"]
                val = r["value"]
                if isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except Exception:
                        pass
                val_str = json.dumps(val, ensure_ascii=False) if not isinstance(val, str) else val
                line = f"[{cat}] {key}: {val_str}"
                lines.append(line)
                if sum(len(l) for l in lines) > char_limit:
                    break

            return "\n".join(lines)
        except Exception as e:
            logger.debug(f"memory_manager get_meta_context error: {e}")
            return ""
        finally:
            await conn.close()

    # ── 자동 요약 (Claude Haiku) ─────────────────────────────────────────────

    async def _auto_summarize(
        self,
        messages: List[Dict[str, Any]],
        key_decisions: Optional[List[str]],
        action_items: Optional[List[str]],
        unresolved_issues: Optional[List[str]],
    ) -> tuple[str, List[str], List[str], List[str]]:
        """Claude Haiku로 대화 요약 (~$0.001)."""
        if not messages:
            return "빈 세션", key_decisions or [], action_items or [], unresolved_issues or []

        # 최근 15개 메시지로 제한
        recent = messages[-15:]
        dialog = "\n".join(
            f"{m.get('role', 'user')}: {str(m.get('content', ''))[:300]}"
            for m in recent
        )

        try:
            from anthropic import AsyncAnthropic
            from app.config import Settings
            s = Settings()
            client = AsyncAnthropic(api_key=s.ANTHROPIC_API_KEY.get_secret_value())

            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                system=(
                    "대화를 분석하여 JSON으로 요약하라. "
                    "{'summary': '한 문장 요약', "
                    "'key_decisions': ['결정1', ...], "
                    "'action_items': ['액션1', ...], "
                    "'unresolved_issues': ['이슈1', ...]} 형식. "
                    "각 목록은 최대 3개. 없으면 빈 배열."
                ),
                messages=[{"role": "user", "content": f"대화:\n{dialog}"}],
            )

            raw = resp.content[0].text.strip()
            if raw.startswith("{"):
                parsed = json.loads(raw)
                return (
                    parsed.get("summary", "세션 요약"),
                    parsed.get("key_decisions", []),
                    parsed.get("action_items", []),
                    parsed.get("unresolved_issues", []),
                )
        except Exception as e:
            logger.debug(f"memory_manager auto_summarize haiku error: {e}")

        # 폴백: 마지막 사용자 메시지 첫 줄
        last_user = next(
            (m for m in reversed(messages) if m.get("role") == "user"), None
        )
        summary = str(last_user.get("content", ""))[:100] if last_user else "세션 요약"
        return summary, [], [], []


# ─── 헬퍼 ─────────────────────────────────────────────────────────────────────

_PROJECTS = ("AADS", "KIS", "GO100", "SF", "NTV2", "NAS")


def _extract_projects(messages: List[Dict[str, Any]]) -> List[str]:
    """메시지에서 언급된 프로젝트명 추출."""
    text = " ".join(str(m.get("content", "")) for m in messages).upper()
    return [p for p in _PROJECTS if p in text]


def _row_to_session_note(row: asyncpg.Record) -> SessionNote:
    if not row:
        return SessionNote()
    return SessionNote(
        id=row["id"],
        session_id=row.get("session_id") or "",
        summary=row.get("summary") or "",
        key_decisions=list(row.get("key_decisions") or []),
        action_items=list(row.get("action_items") or []),
        unresolved_issues=list(row.get("unresolved_issues") or []),
        projects_discussed=list(row.get("projects_discussed") or []),
        created_at=row.get("created_at"),
    )


def _row_to_memory(row: asyncpg.Record) -> Memory:
    if not row:
        return Memory()
    val = row.get("value")
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            val = {"raw": val}
    return Memory(
        id=row["id"],
        category=row.get("category") or "",
        key=row.get("key") or "",
        value=val or {},
        confidence=float(row.get("confidence") or 1.0),
        last_used_at=row.get("last_used_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


# ─── 싱글턴 ─────────────────────────────────────────────────────────────────

_memory_manager: Optional[MemoryManager] = None


def get_memory_manager() -> MemoryManager:
    """MemoryManager 싱글턴 반환."""
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager()
    return _memory_manager
