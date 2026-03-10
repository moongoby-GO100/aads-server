"""
AADS-185: 자동 압축 서비스 — 20턴 초과 시 Claude Haiku로 구조화 요약 + 증분 병합
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

COMPACTION_TRIGGER_TURNS = int(os.getenv("COMPACTION_TRIGGER_TURNS", "20"))
_DB_URL_ENV = "DATABASE_URL"

STRUCTURED_TEMPLATE = """## 현재 목표
(what the CEO is trying to achieve)

## 수정된 파일
(files modified/created with brief description)

## 내려진 결정
(architectural/technical decisions made)

## 실패한 접근
(approaches that didn't work)

## 보류 작업
(pending items, next steps)

## 활성 Directive
(any active directives/instructions from CEO)"""


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
    20턴 초과 시 자동 압축 실행.
    기존 압축 요약이 있으면 증분 병합, 없으면 신규 생성.
    압축 결과를 session_notes에 저장, 이전 메시지를 is_compacted=true로 마킹.

    Returns:
        압축 후 메시지 리스트 (최근 5턴 + 압축 요약 메시지)
    """
    if len(messages) <= COMPACTION_TRIGGER_TURNS:
        return messages

    logger.info(f"compaction_service: session={session_id} turns={len(messages)} → 압축 시작")

    # 압축 대상: 최근 5턴 이전 메시지들
    to_compress = messages[:-5]
    recent = messages[-5:]

    # 기존 압축 요약 조회
    existing_summary: Optional[str] = None
    if db_conn:
        try:
            row = await db_conn.fetchrow(
                """
                SELECT content FROM session_notes
                WHERE session_id = $1 AND note_type = 'compaction'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                uuid.UUID(session_id),
            )
            if row:
                existing_summary = row["content"]
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
                INSERT INTO session_notes (session_id, note_type, content)
                VALUES ($1, 'compaction', $2)
                """,
                uuid.UUID(session_id),
                summary,
            )
            # 이전 메시지 is_compacted 마킹
            await db_conn.execute(
                """
                UPDATE chat_messages
                SET is_compacted = true
                WHERE session_id = $1
                  AND id NOT IN (
                      SELECT id FROM chat_messages
                      WHERE session_id = $1
                      ORDER BY created_at DESC
                      LIMIT 10
                  )
                """,
                uuid.UUID(session_id),
            )
        except Exception as e:
            logger.warning(f"compaction_service db error: {e}")

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
            conv_text += f"\n{role}: {content[:500]}\n"
        else:
            conv_text += f"\n{role}: {str(content)[:500]}\n"

    prompt = f"""다음 대화를 아래 구조화 템플릿에 맞춰 요약하세요.
각 섹션에 해당 내용이 없으면 "(없음)"으로 기재합니다.

템플릿:
{STRUCTURED_TEMPLATE}

보존 항목:
- 아키텍처 결정 및 기술적 합의
- 미해결 이슈 및 다음 단계
- 중요한 수치/데이터/경로
- CEO 주요 지시 내용
- 수정/생성된 파일 경로와 변경 내용
- 시도했으나 실패한 접근법

삭제 항목:
- 중복 인사, 확인 대화
- 이전 도구 raw 출력
- 반복되는 상태 설명

대화:
{conv_text[:6000]}

위 대화를 템플릿 형식에 맞춰 요약하세요 (각 섹션 빠짐없이):"""

    try:
        msg = await _anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.warning(f"compaction_service summarize error: {e}")
        # 폴백: 간단 트리밍
        return f"[압축 오류, 원본 {len(messages)}개 메시지 — 최근 내용 우선 참고]"


async def _merge_summaries(existing_summary: str, new_summary: str) -> str:
    """기존 압축 요약과 새 요약을 증분 병합한다. Haiku 사용 (저비용)."""
    prompt = f"""아래 두 개의 구조화 요약을 하나로 병합하세요.

규칙:
- 동일한 6개 섹션(현재 목표, 수정된 파일, 내려진 결정, 실패한 접근, 보류 작업, 활성 Directive)을 유지
- 기존 요약의 내용을 기반으로 새 요약의 정보를 업데이트/추가
- 완료된 보류 작업은 제거하고, 새로운 보류 작업을 추가
- 목표가 변경되었으면 최신 목표로 교체
- 수정된 파일은 누적 (중복 제거)
- 실패한 접근도 누적
- 활성 Directive는 최신 상태로 유지 (취소된 것은 제거)

[기존 요약]
{existing_summary}

[새 요약]
{new_summary}

병합된 구조화 요약:"""

    try:
        msg = await _anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.warning(f"compaction_service merge error: {e}")
        # 병합 실패 시 새 요약으로 대체
        return new_summary
