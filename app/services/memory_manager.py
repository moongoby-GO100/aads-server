"""
AADS-186E-2/186E-3: 4계층 영속 메모리 관리자
Layer 1: Session Buffer (기존 chat_service 히스토리)
Layer 2: Working Memory — session_notes (세션 종료 시 요약 저장)
Layer 3: CKP — 코드베이스 지식 (기존 ckp_manager)
Layer 4: Meta Memory — ai_meta_memory(수동 학습) + ai_observations(자동 관찰)
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
    content: str = ""
    key_decisions: List[str] = field(default_factory=list)
    action_items: List[str] = field(default_factory=list)
    unresolved_issues: List[str] = field(default_factory=list)
    projects_discussed: List[str] = field(default_factory=list)
    created_at: Optional[datetime] = None


@dataclass
class Memory:
    """Layer 4: 메타 메모리 항목 (ai_meta_memory)."""
    id: int = 0
    category: str = ""
    key: str = ""
    value: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    last_used_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class Observation:
    """Layer 4: AI 자동 관찰 항목 (ai_observations, AADS-186E-3)."""
    id: int = 0
    category: str = ""  # 'ceo_preference' | 'project_pattern' | 'recurring_issue' | 'decision' | 'learning'
    key: str = ""
    value: str = ""
    confidence: float = 0.5
    source_session_id: Optional[int] = None
    last_confirmed_at: Optional[datetime] = None
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
            logger.warning(f"memory_manager get_recent_notes error: {e}")
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
            logger.info(f"memory_manager learn: category={category} key={key}")
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
            logger.warning(f"memory_manager recall error: {e}")
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
            logger.warning(f"memory_manager get_meta_context error: {e}")
            return ""
        finally:
            await conn.close()

    # ── Layer 4: AI 자동 관찰 (ai_observations, AADS-186E-3) ────────────────

    async def observe(
        self,
        category: str,
        key: str,
        value: str,
        confidence: float = 0.5,
        source_session_id: Optional[int] = None,
    ) -> None:
        """
        AI가 관찰한 패턴/선호도를 ai_observations에 UPSERT.
        기존 키 존재 시: confidence 증가 (min(1.0, existing + 0.1)).
        value 변경 시: value 업데이트 + confidence 리셋.
        """
        valid_categories = {"ceo_preference", "project_pattern", "recurring_issue", "decision", "learning"}
        if category not in valid_categories:
            logger.warning(f"memory_manager observe: 유효하지 않은 category={category}")
            return

        conn = await _get_conn()
        try:
            await conn.execute(
                """
                INSERT INTO ai_observations
                    (category, key, value, confidence, source_session_id, last_confirmed_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                ON CONFLICT (category, key) DO UPDATE SET
                    confidence = CASE
                        WHEN ai_observations.value = EXCLUDED.value
                        THEN LEAST(ai_observations.confidence + 0.1, 1.0)
                        ELSE EXCLUDED.confidence
                    END,
                    value = EXCLUDED.value,
                    last_confirmed_at = NOW(),
                    updated_at = NOW()
                """,
                category, key, value, confidence, source_session_id,
            )
            logger.info(f"memory_manager observe: category={category} key={key}")
        except Exception as e:
            logger.error(f"memory_manager observe error: {e}")
        finally:
            await conn.close()

    async def recall_observations(
        self,
        category: Optional[str] = None,
        min_confidence: float = 0.3,
    ) -> List[Observation]:
        """
        ai_observations 검색.
        category 지정 시 해당 카테고리만, min_confidence 기준 필터링.
        최근 confirmed 순 정렬.
        """
        conn = await _get_conn()
        try:
            if category:
                rows = await conn.fetch(
                    """
                    SELECT * FROM ai_observations
                    WHERE category = $1 AND confidence >= $2
                    ORDER BY last_confirmed_at DESC NULLS LAST, confidence DESC
                    LIMIT 30
                    """,
                    category, min_confidence,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM ai_observations
                    WHERE confidence >= $1
                    ORDER BY last_confirmed_at DESC NULLS LAST, confidence DESC
                    LIMIT 30
                    """,
                    min_confidence,
                )
            return [_row_to_observation(r) for r in rows]
        except Exception as e:
            logger.warning(f"memory_manager recall_observations error: {e}")
            return []
        finally:
            await conn.close()

    async def build_meta_context(self, max_tokens: int = 500) -> str:
        """
        Context Builder Layer 4에 주입할 메타 메모리 자연어 요약.
        ai_observations + ai_meta_memory 통합.
        가장 confident한 관찰 10~15개를 자연어로 정리.
        """
        lines: List[str] = []
        char_limit = max_tokens * 3  # 한국어 기준

        # ai_observations에서 high-confidence 항목
        try:
            obs = await self.recall_observations(min_confidence=0.4)
            if obs:
                by_cat: Dict[str, List[str]] = {}
                for o in obs[:15]:
                    cat = o.category
                    if cat not in by_cat:
                        by_cat[cat] = []
                    by_cat[cat].append(f"{o.key}: {o.value}")

                for cat, items in by_cat.items():
                    cat_label = {
                        "ceo_preference": "CEO 선호",
                        "project_pattern": "프로젝트 패턴",
                        "recurring_issue": "반복 이슈",
                        "decision": "결정 사항",
                        "learning": "학습",
                    }.get(cat, cat)
                    for item in items[:3]:
                        line = f"[{cat_label}] {item}"
                        lines.append(line)
                        if sum(len(l) for l in lines) > char_limit:
                            break
                    if sum(len(l) for l in lines) > char_limit:
                        break
        except Exception as e:
            logger.warning(f"memory_manager build_meta_context obs error: {e}")

        # ai_meta_memory에서 CEO 선호도 + 결정 이력
        if sum(len(l) for l in lines) < char_limit:
            try:
                meta_text = await self.get_meta_context(max_tokens=max_tokens // 2)
                if meta_text:
                    lines.append(meta_text)
            except Exception:
                pass

        return "\n".join(lines) if lines else ""

    async def auto_observe_from_session(self, messages: List[Dict[str, Any]]) -> None:
        """
        세션 종료 시 자동으로 패턴 추출·기록 (백그라운드 실행).
        Haiku로 메시지 분석 → observe() 호출.
        비용: ~$0.001/호출.
        """
        if not messages:
            return

        recent = messages[-20:]
        dialog = "\n".join(
            f"{m.get('role', 'user')}: {str(m.get('content', ''))[:200]}"
            for m in recent
        )

        try:
            from anthropic import AsyncAnthropic
            from app.config import Settings
            s = Settings()
            client = AsyncAnthropic(api_key=s.ANTHROPIC_API_KEY.get_secret_value())

            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=500,
                system=(
                    "대화를 분석하여 CEO의 선호도, 반복 패턴, 새로운 결정을 추출하라. "
                    "JSON 배열로 반환: "
                    "[{'category': 'ceo_preference'|'project_pattern'|'recurring_issue'|'decision'|'learning', "
                    "'key': '영문_snake_case', 'value': '한국어 설명', 'confidence': 0.3~0.9}] "
                    "최대 5개. 없으면 빈 배열 []."
                ),
                messages=[{"role": "user", "content": f"대화:\n{dialog}"}],
            )

            raw = resp.content[0].text.strip()
            # JSON 배열 파싱
            if raw.startswith("["):
                observations = json.loads(raw)
                for obs in observations[:5]:
                    if all(k in obs for k in ("category", "key", "value")):
                        await self.observe(
                            category=obs["category"],
                            key=obs["key"],
                            value=str(obs["value"]),
                            confidence=float(obs.get("confidence", 0.5)),
                        )
                logger.info(f"auto_observe_from_session: {len(observations)}개 관찰 저장")
        except Exception as e:
            logger.warning(f"memory_manager auto_observe_from_session error: {e}")

    # ── Layer 2 도구 인터페이스 (AADS-186E-3) ───────────────────────────────

    async def save_note(
        self, title: str, content: str, category: str = "general"
    ) -> str:
        """
        AI가 명시적으로 노트 저장 (도구 인터페이스).
        session_notes 테이블에 저장. content 컬럼에 전문 보존.
        반환: "노트 저장 완료: {title}"
        """
        if not title or not content:
            return "오류: title과 content 필수"

        summary = f"[{category}] {title}"
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        session_id = f"note_{category}_{ts}"
        conn = await _get_conn()
        try:
            await conn.execute(
                """
                INSERT INTO session_notes
                    (session_id, summary, content, key_decisions, action_items,
                     unresolved_issues, projects_discussed, note_type)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                session_id,
                summary,
                content,
                [title],
                [],
                [],
                _extract_projects([{"content": content}]),
                "tool_note",
            )
            return f"노트 저장 완료: {title}"
        except Exception as e:
            logger.error(f"memory_manager save_note error: {e}")
            return f"노트 저장 실패: {e}"
        finally:
            await conn.close()

    async def delete_note(self, note_id: int = 0, keyword: str = "") -> str:
        """
        노트 삭제 (도구 인터페이스).
        note_id 지정 시 해당 ID 삭제, keyword 지정 시 summary/content ILIKE 매칭 삭제.
        반환: 삭제 결과 메시지.
        """
        if not note_id and not keyword:
            return "오류: note_id 또는 keyword 중 하나 필수"

        conn = await _get_conn()
        try:
            if note_id:
                result = await conn.execute(
                    "DELETE FROM session_notes WHERE id = $1", note_id
                )
                count = int(result.split()[-1])  # "DELETE N"
                if count:
                    return f"노트 #{note_id} 삭제 완료"
                return f"노트 #{note_id}를 찾을 수 없습니다"
            else:
                # keyword 기반: 먼저 매칭 건수 확인
                rows = await conn.fetch(
                    """
                    SELECT id, summary FROM session_notes
                    WHERE summary ILIKE $1 OR content ILIKE $1
                    ORDER BY created_at DESC LIMIT 10
                    """,
                    f"%{keyword}%",
                )
                if not rows:
                    return f"'{keyword}' 키워드와 일치하는 노트가 없습니다"
                ids = [r["id"] for r in rows]
                result = await conn.execute(
                    "DELETE FROM session_notes WHERE id = ANY($1)", ids
                )
                count = int(result.split()[-1])
                titles = ", ".join(r["summary"][:30] for r in rows[:3])
                return f"'{keyword}' 매칭 노트 {count}건 삭제 완료 ({titles}{'...' if len(rows) > 3 else ''})"
        except Exception as e:
            logger.error(f"memory_manager delete_note error: {e}")
            return f"노트 삭제 실패: {e}"
        finally:
            await conn.close()

    async def recall_notes(self, query: str, limit: int = 5) -> List[SessionNote]:
        """
        키워드로 노트 검색 (도구 인터페이스).
        session_notes에서 summary/content/session_id ILIKE 검색, 최근순 정렬.
        """
        conn = await _get_conn()
        try:
            rows = await conn.fetch(
                """
                SELECT * FROM session_notes
                WHERE summary ILIKE $1
                   OR content ILIKE $1
                   OR $1 ILIKE '%' || session_id || '%'
                ORDER BY created_at DESC
                LIMIT $2
                """,
                f"%{query}%",
                min(limit, 20),
            )
            return [_row_to_session_note(r) for r in rows]
        except Exception as e:
            logger.warning(f"memory_manager recall_notes error: {e}")
            return []
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
            logger.warning(f"memory_manager auto_summarize haiku error: {e}")

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
        content=row.get("content") or "",
        key_decisions=list(row.get("key_decisions") or []),
        action_items=list(row.get("action_items") or []),
        unresolved_issues=list(row.get("unresolved_issues") or []),
        projects_discussed=list(row.get("projects_discussed") or []),
        created_at=row.get("created_at"),
    )


def _row_to_observation(row: asyncpg.Record) -> Observation:
    if not row:
        return Observation()
    return Observation(
        id=row["id"],
        category=row.get("category") or "",
        key=row.get("key") or "",
        value=row.get("value") or "",
        confidence=float(row.get("confidence") or 0.5),
        source_session_id=row.get("source_session_id"),
        last_confirmed_at=row.get("last_confirmed_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
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
