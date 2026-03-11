"""
AADS-185/190: 자동 압축 서비스 — 토큰 기반 트리거 (context_builder 80K 초과 시 호출)
Claude Haiku로 구조화 요약 + 증분 병합. 턴 수 트리거 제거됨.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic
from app.config import Settings

logger = logging.getLogger(__name__)

settings = Settings()
_anthropic = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY.get_secret_value())

COMPACTION_TRIGGER_TOKENS = int(os.getenv("COMPACTION_TRIGGER_TOKENS", "80000"))
COMPACTION_KEEP_RECENT = int(os.getenv("COMPACTION_KEEP_RECENT", "20"))
_DB_URL_ENV = "DATABASE_URL"

STRUCTURED_TEMPLATE = """## 현재 목표
(CEO가 달성하려는 것)

## CEO 주요 지시사항
(CEO가 내린 구체적 지시, 번호 매기기)

## 에이전트 위임 내역
(다른 에이전트/서버에 위임된 작업과 상태)

## 수정된 파일
(수정/생성된 파일 경로와 변경 내용 요약)

## 내려진 결정
(아키텍처/기술적 결정사항)

## 실패한 접근 & 에러
(시도했으나 실패한 접근법, 발생한 에러)

## 보류 작업 & 핵심 수치
(미완료 작업, 다음 단계, 중요한 숫자/경로/데이터)"""


def _db_url() -> str:
    url = os.getenv(_DB_URL_ENV, "")
    return url.replace("postgresql://", "postgres://") if url else url


def _compress_tool_outputs_in_messages(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """도구 raw 출력을 압축하여 요약기에 전달할 토큰 수를 줄인다."""
    compressed = []
    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, str):
            compressed.append(msg)
            continue

        if "[시스템 도구 조회 결과" not in content:
            compressed.append(msg)
            continue

        lines = content.split("\n")
        result_lines: List[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if "[시스템 도구 조회 결과" in line:
                # Extract tool name from the header line
                tool_name = line.strip()
                # Collect the block content (until next blank line or end)
                block_chars: List[str] = []
                i += 1
                while i < len(lines):
                    if lines[i].strip() == "" and len("".join(block_chars)) > 200:
                        break
                    block_chars.append(lines[i])
                    i += 1
                preview = "".join(block_chars)[:200]
                result_lines.append(f"{tool_name} → {preview}...")
            else:
                result_lines.append(line)
                i += 1

        new_msg = dict(msg)
        new_msg["content"] = "\n".join(result_lines)
        compressed.append(new_msg)
    return compressed


async def check_and_compact(
    session_id: str,
    messages: List[Dict[str, Any]],
    db_conn=None,
) -> List[Dict[str, Any]]:
    """
    토큰 기반 압축 — context_builder가 80K 토큰 초과 시 호출.
    턴 수 체크 없이 호출되면 항상 압축 수행.
    기존 압축 요약이 있으면 증분 병합(append), 없으면 신규 생성.
    압축 결과를 session_notes에 저장, 이전 메시지를 is_compacted=true로 마킹.

    # Layer 0/1/2 (system prompt) is NEVER modified by compaction — only Layer 3 messages

    Returns:
        압축 후 메시지 리스트 (최근 COMPACTION_KEEP_RECENT턴 + 압축 요약 메시지)
    """
    # Tool-loop deferral: if last message is a tool_result, skip compaction this turn
    if messages and messages[-1].get("role") == "user":
        last_content = messages[-1].get("content", "")
        if isinstance(last_content, list):
            if any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in last_content
            ):
                logger.info("compaction_service: tool_result detected in last message, deferring compaction")
                return messages
        elif isinstance(last_content, str) and "[시스템 도구 조회 결과" in last_content:
            logger.info("compaction_service: tool result pattern in last message, deferring compaction")
            return messages

    logger.info(f"compaction_service: session={session_id} turns={len(messages)} → 압축 시작")

    # 압축 대상: 최근 COMPACTION_KEEP_RECENT턴 이전 메시지들
    keep = min(COMPACTION_KEEP_RECENT, len(messages))
    to_compress = messages[:-keep] if keep < len(messages) else []
    recent = messages[-keep:]

    if not to_compress:
        return messages

    # 기존 압축 요약 조회
    existing_summary: Optional[str] = None
    if db_conn:
        try:
            row = await db_conn.fetchrow(
                """
                SELECT summary, content FROM session_notes
                WHERE session_id = $1 AND note_type = 'compaction'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                str(session_id),
            )
            if row:
                # summary 컬럼 우선, 없으면 content 폴백
                existing_summary = row["summary"] if row["summary"] else row["content"]
        except Exception as e:
            logger.warning(f"compaction_service: 기존 요약 조회 실패: {e}")

    # 도구 출력 압축 후 요약 생성
    compressed_messages = _compress_tool_outputs_in_messages(to_compress)
    new_summary = await _summarize(compressed_messages)

    # 기존 요약이 있으면 증분 병합
    if existing_summary:
        summary = await _merge_summaries(existing_summary, new_summary)
    else:
        summary = new_summary

    # DB에 압축 요약 저장
    if db_conn:
        try:
            await db_conn.execute(
                """
                INSERT INTO session_notes (session_id, note_type, summary, content)
                VALUES ($1, 'compaction', $2, $2)
                """,
                str(session_id),
                summary,
            )
            # 이전 메시지 is_compacted 마킹 (최근 COMPACTION_KEEP_RECENT*2 보존)
            await db_conn.execute(
                """
                UPDATE chat_messages
                SET is_compacted = true
                WHERE session_id = $1
                  AND id NOT IN (
                      SELECT id FROM chat_messages
                      WHERE session_id = $1
                      ORDER BY created_at DESC
                      LIMIT $2
                  )
                """,
                uuid.UUID(session_id) if len(session_id) == 36 else session_id,
                COMPACTION_KEEP_RECENT * 2,
            )
            # Stage 7: 양방향 메모리 동기화 — ai_observations에 핵심 지시사항 저장
            await _sync_to_observations(db_conn, session_id, summary)
        except Exception as e:
            logger.error(f"compaction_service db error: {e}", exc_info=True)

    # 압축 메시지를 히스토리 앞에 삽입
    compaction_msg = {
        "role": "user",
        "content": f"[대화 압축 요약 — 이전 {len(to_compress)}개 메시지]\n\n{summary}",
    }
    result = [compaction_msg] + recent
    logger.info(f"compaction_service: {len(messages)} → {len(result)} 메시지로 압축 완료")
    return result


async def _summarize(messages: List[Dict[str, Any]]) -> str:
    """Claude Haiku로 구조화 템플릿 기반 대화 요약 (비용 최소화)."""
    if not messages:
        return "(압축할 내용 없음)"

    # 메시지를 텍스트로 변환
    conv_text = ""
    for msg in messages:
        role = "사용자" if msg["role"] == "user" else "AI"
        content = msg.get("content", "")
        if isinstance(content, str):
            conv_text += f"\n{role}: {content[:2000]}\n"
        else:
            conv_text += f"\n{role}: {str(content)[:2000]}\n"

    prompt = f"""다음 대화를 아래 구조화 템플릿에 맞춰 요약하세요.
반드시 7개 섹션 모두 작성. CEO 지시사항은 원문 보존.
각 섹션에 해당 내용이 없으면 "(없음)"으로 기재합니다.

템플릿:
{STRUCTURED_TEMPLATE}

보존 항목:
- CEO 주요 지시 내용 (원문 그대로)
- 에이전트 위임 내역과 상태
- 아키텍처 결정 및 기술적 합의
- 미해결 이슈 및 다음 단계
- 중요한 수치/데이터/경로
- 수정/생성된 파일 경로와 변경 내용
- 시도했으나 실패한 접근법과 에러

삭제 항목:
- 중복 인사, 확인 대화
- 이전 도구 raw 출력
- 반복되는 상태 설명

대화:
{conv_text[:15000]}

위 대화를 템플릿 형식에 맞춰 요약하세요 (7개 섹션 빠짐없이):"""

    try:
        msg = await _anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.warning(f"compaction_service summarize error: {e}")
        # 폴백: 간단 트리밍
        return f"[압축 오류, 원본 {len(messages)}개 메시지 — 최근 내용 우선 참고]"


async def _merge_summaries(existing_summary: str, new_summary: str) -> str:
    """기존 압축 요약과 새 요약을 증분 병합 (append 우선 전략).

    8000자 이하: 기존 요약 보존 + 새 요약 append (LLM 호출 없음, 정보 손실 방지)
    8000자 초과: LLM 병합 (강화된 보존 규칙 적용)
    """
    # Append strategy: preserve existing, add new
    combined = existing_summary + "\n\n---\n\n## 추가 요약 (최신)\n" + new_summary

    if len(combined) <= 8000:
        logger.info(f"compaction_service: append merge ({len(combined)} chars, no LLM needed)")
        return combined

    # Combined too large — use LLM merge with stronger preservation rules
    logger.info(f"compaction_service: LLM merge needed ({len(combined)} chars > 8000)")
    prompt = f"""아래 두 개의 구조화 요약을 하나로 병합하세요.

규칙:
- 동일한 7개 섹션(현재 목표, CEO 주요 지시사항, 에이전트 위임 내역, 수정된 파일, 내려진 결정, 실패한 접근 & 에러, 보류 작업 & 핵심 수치)을 유지
- CEO 지시사항은 절대 삭제하지 말 것 — 원문 보존
- 기존 요약의 내용을 기반으로 새 요약의 정보를 업데이트/추가
- 완료된 보류 작업은 제거하고, 새로운 보류 작업을 추가
- 목표가 변경되었으면 최신 목표로 교체
- 수정된 파일은 누적 (중복 제거)
- 실패한 접근도 누적
- 에이전트 위임 내역은 최신 상태 반영
- 구체적인 파일 경로, 숫자, 데이터는 절대 생략하지 말 것

[기존 요약]
{existing_summary}

[새 요약]
{new_summary}

병합된 구조화 요약 (7개 섹션 모두 포함):"""

    try:
        msg = await _anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.warning(f"compaction_service merge error: {e}")
        # 병합 실패 시 append 결과라도 반환 (새 요약만 반환하는 것보다 나음)
        return combined[:8000]


async def _sync_to_observations(db_conn, session_id: str, summary: str) -> None:
    """Stage 7: 압축 요약에서 CEO 지시사항을 추출하여 ai_observations에 저장.

    양방향 메모리 동기화: compaction summary → ai_observations 테이블.
    category='compaction_directive'로 저장하여 memory_recall에서 재주입 가능.
    """
    import re

    if not db_conn or not summary:
        return

    try:
        # "## CEO 주요 지시사항" 섹션 추출
        directives_match = re.search(
            r"## CEO 주요 지시사항\s*\n(.*?)(?=\n## |\Z)",
            summary,
            re.DOTALL,
        )
        if not directives_match:
            return

        directives_text = directives_match.group(1).strip()
        if not directives_text or directives_text == "(없음)":
            return

        # workspace/project 추출 시도 (세션에서)
        project = None
        try:
            ws_row = await db_conn.fetchrow(
                "SELECT workspace_name FROM chat_sessions WHERE id = $1",
                uuid.UUID(session_id),
            )
            if ws_row and ws_row["workspace_name"]:
                project = ws_row["workspace_name"].upper().strip()
                if project not in ("AADS", "CEO", "KIS", "GO100", "SF", "NTV2", "NAS"):
                    project = None
        except Exception:
            pass

        # ai_observations에 upsert
        await db_conn.execute(
            """
            INSERT INTO ai_observations (category, key, value, confidence, project, updated_at)
            VALUES ('compaction_directive', $1, $2, 0.8, $3, NOW())
            ON CONFLICT (category, key, COALESCE(project, ''))
            DO UPDATE SET
                value = EXCLUDED.value,
                confidence = LEAST(ai_observations.confidence + 0.1, 1.0),
                updated_at = NOW()
            """,
            f"session_{session_id[:8]}",
            directives_text[:2000],
            project,
        )
        logger.info(f"compaction_service: synced directives to ai_observations (session={session_id[:8]}, project={project})")
    except Exception as e:
        logger.warning(f"compaction_service _sync_to_observations error: {e}")
