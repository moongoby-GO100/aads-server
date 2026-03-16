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
    # Tool-result stripping: instead of deferring compaction entirely,
    # filter out tool_result blocks from messages before summarizing
    _has_tool_results = False
    if messages and messages[-1].get("role") == "user":
        last_content = messages[-1].get("content", "")
        if isinstance(last_content, list):
            if any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in last_content
            ):
                _has_tool_results = True
                logger.info("compaction_service: tool_result detected in last message, will strip before summarizing")
        elif isinstance(last_content, str) and "[시스템 도구 조회 결과" in last_content:
            _has_tool_results = True
            logger.info("compaction_service: tool result pattern in last message, will strip before summarizing")

    logger.info(f"compaction_service: session={session_id} turns={len(messages)} tool_results={_has_tool_results} → 압축 시작")

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

    # #9: 압축 대상 메시지 해시 비교 — 변경 없으면 LLM 호출 스킵
    # F18: content[:200]만 해시하면 동일 시작+다른 내용 시 skip → 처음1000+끝1000+길이 조합
    import hashlib
    def _msg_fingerprint(m: Dict[str, Any]) -> str:
        c = str(m.get("content", ""))
        return f"{c[:1000]}|{c[-1000:] if len(c) > 1000 else ''}|{len(c)}"
    _msg_hash = hashlib.md5("".join(_msg_fingerprint(m) for m in to_compress).encode()).hexdigest()
    if existing_summary and db_conn:
        try:
            _hash_row = await db_conn.fetchval(
                "SELECT content FROM session_notes WHERE session_id = $1 AND note_type = 'compaction_hash' ORDER BY created_at DESC LIMIT 1",
                str(session_id),
            )
            if _hash_row == _msg_hash:
                logger.info(f"compaction_service: skip — messages unchanged (hash={_msg_hash[:8]})")
                compaction_msg = {
                    "role": "user",
                    "content": (
                        "[SYSTEM: 이전 대화 자동 압축 요약 — 이 내용은 CEO 발언이 아닌 시스템 생성 요약입니다. "
                        f"이전 {len(to_compress)}개 메시지를 요약했습니다.]\n\n{existing_summary}"
                    ),
                }
                return [compaction_msg] + recent
        except Exception:
            pass

    # 도구 출력 압축 후 요약 생성 (tool_result 블록은 텍스트 요약으로 대체)
    filtered_to_compress = []
    for msg in to_compress:
        content = msg.get("content", "")
        if isinstance(content, list):
            # Strip tool_result blocks, keep text blocks
            text_blocks = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    # Replace with short summary
                    tool_id = block.get("tool_use_id", "unknown")
                    text_blocks.append({"type": "text", "text": f"[도구 결과 ({tool_id}) 생략]"})
                else:
                    text_blocks.append(block)
            filtered_to_compress.append({**msg, "content": text_blocks})
        else:
            filtered_to_compress.append(msg)
    compressed_messages = _compress_tool_outputs_in_messages(filtered_to_compress)
    new_summary = await _summarize(compressed_messages)

    # 기존 요약이 있으면 증분 병합
    if existing_summary:
        summary = await _merge_summaries(existing_summary, new_summary)
    else:
        summary = new_summary

    # DB에 압축 요약 저장 (AADS-CRITICAL-FIX #3: 트랜잭션 + 테이블 순서 고정)
    # 순서: session_notes → chat_messages → ai_observations (데드락 방지)
    if db_conn:
        try:
            async with db_conn.transaction():
                # 1. session_notes INSERT
                await db_conn.execute(
                    """
                    INSERT INTO session_notes (session_id, note_type, summary, content)
                    VALUES ($1, 'compaction', $2, $2)
                    """,
                    uuid.UUID(session_id) if isinstance(session_id, str) else session_id,
                    summary,
                )
                # 1.5 #9: 압축 해시 저장 (다음 호출 시 변경 감지용)
                await db_conn.execute(
                    "INSERT INTO session_notes (session_id, note_type, summary, content) VALUES ($1, 'compaction_hash', $2, $2)",
                    uuid.UUID(session_id) if isinstance(session_id, str) else session_id, _msg_hash,
                )
                # 2. chat_messages UPDATE (is_compacted 마킹)
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
                # 3. ai_observations UPSERT (양방향 메모리 동기화) — 별도 커넥션 (트랜잭션 격리)
                try:
                    from app.core.db_pool import get_pool as _get_compact_pool
                    _cp = _get_compact_pool()
                    async with _cp.acquire() as _obs_conn:
                        await _sync_to_observations(_obs_conn, session_id, summary)
                except Exception as _obs_err:
                    logger.warning(f"compaction_service _sync_to_observations isolated error: {_obs_err}")
        except Exception as e:
            logger.error(f"compaction_service db error: {e}", exc_info=True)

    # 압축 메시지를 히스토리 앞에 삽입
    compaction_msg = {
        "role": "user",
        "content": (
            "[SYSTEM: 이전 대화 자동 압축 요약 — 이 내용은 CEO 발언이 아닌 시스템 생성 요약입니다. "
            f"이전 {len(to_compress)}개 메시지를 요약했습니다.]\n\n{summary}"
        ),
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
        result = msg.content[0].text.strip()
        # #21: 요약 길이 경고
        if len(result) > 4000:
            logger.warning(f"compaction_summary_too_long: {len(result)} chars (session context)")
        return result
    except Exception as e:
        logger.warning(f"compaction_service summarize error: {e}")
        return f"[압축 오류, 원본 {len(messages)}개 메시지 — 최근 내용 우선 참고]"


async def _merge_summaries(existing_summary: str, new_summary: str) -> str:
    """기존 압축 요약과 새 요약을 증분 병합 (append 우선 전략).

    12000자 이하: 기존 요약 보존 + 새 요약 append (LLM 호출 없음, 정보 손실 방지)
    12000자 초과: LLM 병합 (강화된 보존 규칙 적용)
    """
    # #21: append되는 새 요약 최대 3000자 제한
    if len(new_summary) > 3000:
        new_summary = new_summary[:3000] + "\n[... 나머지 생략]"

    # Append strategy: preserve existing, add new
    combined = existing_summary + "\n\n---\n\n## 추가 요약 (최신)\n" + new_summary

    # #35: 점진적 전환 — 12KB 미만 append only, 12KB 초과만 LLM 호출
    if len(combined) <= 12000:
        logger.info(f"compaction_service: append merge ({len(combined)} chars, no LLM needed)")
        return combined

    # Combined too large — use LLM merge with stronger preservation rules
    logger.info(f"compaction_service: LLM merge needed ({len(combined)} chars > 12K)")
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
        return combined[:12000]


async def _sync_to_observations(db_conn, session_id: str, summary: str) -> None:
    """Stage 7: 압축 요약에서 CEO 지시사항을 추출하여 ai_observations에 저장.

    양방향 메모리 동기화: compaction summary → ai_observations 테이블.
    category='compaction_directive'로 저장하여 memory_recall에서 재주입 가능.
    """
    import re

    if not db_conn or not summary:
        return

    try:
        # #24: 유연한 정규식 — "CEO 주요 지시사항", "CEO의 지시사항", "### CEO 지시", "대표 지시" 등 매치
        directives_match = re.search(
            r"#{1,3}\s*(?:CEO|대표)[\s의]*(?:주요\s*)?지시사항?\s*\n(.*?)(?=\n#{1,3}\s|\Z)",
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
                "SELECT w.name AS workspace_name FROM chat_sessions s JOIN chat_workspaces w ON s.workspace_id = w.id WHERE s.id = $1",
                uuid.UUID(session_id) if isinstance(session_id, str) else session_id,
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
