"""
F11: Self-Evaluation — AI 응답 후 Haiku로 정확도/완성도/관련성 평가.
chat_messages.quality_score + quality_details 저장.
비용: ~$0.0003/턴 (200자 이상 응답만).
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

_ENABLED = os.getenv("SELF_EVAL_ENABLED", "true").lower() == "true"
_MIN_RESPONSE_LEN = int(os.getenv("SELF_EVAL_MIN_LEN", "200"))
_HAIKU_MODEL = os.getenv("SELF_EVAL_MODEL", "claude-haiku-4-5-20251001")

_PROJECT_KEYS = ("KIS", "AADS", "GO100", "SF", "NTV2", "NAS", "CEO")


def _normalize_project(raw: str | None) -> str | None:
    """'[KIS] 자동매매' → 'KIS', '[AADS] 프로젝트 매니저' → 'AADS' 등."""
    if not raw:
        return None
    upper = raw.upper()
    for key in _PROJECT_KEYS:
        if key in upper:
            return key
    return upper[:20] if raw else None


_EVAL_PROMPT = """다음 AI 응답의 품질을 평가하세요.

사용자 질문: {user_msg}
AI 응답 (앞 1000자): {ai_msg}

5가지 기준으로 0.0~1.0 점수와 간단한 이유를 JSON으로 반환:
- accuracy: 사실적 정확성
- completeness: 응답의 완성도
- relevance: 질문과의 관련성
- tool_grounding: 도구를 사용하여 주장/데이터를 검증했는가? (도구 미사용 시 0.0~0.3)
- actionability: 구체적인 다음 단계를 제시하는가? (모호한 서술만 있으면 낮음)

{{"accuracy": 0.0~1.0, "completeness": 0.0~1.0, "relevance": 0.0~1.0, "tool_grounding": 0.0~1.0, "actionability": 0.0~1.0, "overall": 0.0~1.0, "note": "한줄 평가"}}

overall = tool_grounding×0.3 + accuracy×0.25 + completeness×0.2 + relevance×0.15 + actionability×0.1 으로 계산.
JSON만 반환. 마크다운 코드블록 없이."""


async def evaluate_response(
    user_message: str,
    ai_response: str,
    message_id: str,
    session_id: Optional[str] = None,
    project: Optional[str] = None,
) -> Optional[float]:
    """AI 응답 품질을 평가하고 DB에 저장. B1: 낮은 품질 시 reflexion 생성."""
    if not _ENABLED or len(ai_response) < _MIN_RESPONSE_LEN:
        return None

    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()

        prompt = _EVAL_PROMPT.format(
            user_msg=user_message[:300],
            ai_msg=ai_response[:1000],
        )

        response = await client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        details = json.loads(text)
        # Weighted overall: tool_grounding×0.3 + accuracy×0.25 + completeness×0.2 + relevance×0.15 + actionability×0.1
        overall = (
            float(details.get("tool_grounding", 0.5)) * 0.3
            + float(details.get("accuracy", 0.5)) * 0.25
            + float(details.get("completeness", 0.5)) * 0.2
            + float(details.get("relevance", 0.5)) * 0.15
            + float(details.get("actionability", 0.5)) * 0.1
        )
        overall = min(1.0, max(0.0, overall))
        details["overall"] = round(overall, 3)

        # DB 저장
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE chat_messages
                SET quality_score = $1, quality_details = $2::jsonb
                WHERE id = $3
                """,
                overall,
                json.dumps(details),
                uuid.UUID(message_id),
            )

        # A1: Low quality → reduce confidence of recently extracted facts
        if overall < 0.5:
            try:
                async with pool.acquire() as conn2:
                    session_row = await conn2.fetchrow(
                        "SELECT session_id, project FROM chat_messages WHERE id = $1",
                        uuid.UUID(message_id),
                    )
                    if session_row and session_row["session_id"]:
                        # 1차: session_id 기준 60분 이내 facts
                        updated = await conn2.execute(
                            """UPDATE memory_facts
                               SET confidence = GREATEST(0.1, confidence * $1)
                               WHERE session_id = $2::uuid
                                 AND created_at > NOW() - interval '60 minutes'
                                 AND confidence > 0.1""",
                            max(0.3, overall),
                            session_row["session_id"],
                        )
                        affected = int(updated.split()[-1]) if updated else 0
                        # 2차: session hit 없으면 project + 1시간 이내 (보호 카테고리 제외)
                        if affected == 0 and session_row.get("project"):
                            await conn2.execute(
                                """UPDATE memory_facts
                                   SET confidence = GREATEST(0.1, confidence * $1)
                                   WHERE project = $2
                                     AND created_at > NOW() - interval '1 hour'
                                     AND confidence > 0.3
                                     AND category NOT IN ('ceo_instruction','ceo_preference','decision')""",
                                max(0.5, overall + 0.2),
                                session_row["project"],
                            )
                            affected = -1  # fallback used
                        logger.info(
                            "a1_fact_confidence_reduced",
                            score=overall,
                            session=str(session_row["session_id"])[:8],
                            affected=affected,
                        )
            except Exception as e_a1:
                logger.debug("a1_fact_confidence_error", error=str(e_a1))

        # B1: Reflexion — auto-reflect on poor quality responses
        if overall < 0.4 and session_id:
            try:
                reflection_prompt = (
                    f"이 AI 응답의 품질이 낮습니다 (점수: {overall:.1f}).\n"
                    f"무엇이 잘못되었고, 다음에 어떻게 개선해야 하는지 1-2문장으로 반성하세요.\n\n"
                    f"사용자: {user_message[:300]}\n"
                    f"AI: {ai_response[:500]}\n"
                    f"품질 세부: {json.dumps(details, ensure_ascii=False)}\n\n"
                    f"반성문만 작성하세요."
                )
                from anthropic import AsyncAnthropic as _ReflexionClient
                _refl_client = _ReflexionClient()
                _refl_resp = await _refl_client.messages.create(
                    model=_HAIKU_MODEL,
                    max_tokens=256,
                    messages=[{"role": "user", "content": reflection_prompt}],
                )
                reflection = _refl_resp.content[0].text.strip() if _refl_resp.content else ""
                if reflection:
                    # MEDIUM-6: 프로젝트명 정규화 (workspace_name → project code)
                    normalized_project = _normalize_project(project)
                    async with pool.acquire() as conn_refl:
                        await conn_refl.execute(
                            """INSERT INTO memory_facts (session_id, project, category, subject, detail, confidence, tags)
                               VALUES ($1::uuid, $2, 'error_pattern', $3, $4, 0.85, ARRAY['reflexion', 'self-improvement'])""",
                            uuid.UUID(session_id),
                            normalized_project,
                            f"품질 부족: {details.get('note', '')[:100]}",
                            reflection[:500],
                        )
                    logger.info("b1_reflexion_saved", score=overall, session=session_id[:8])
                    # Check for repeated errors → generate permanent correction directive
                    await _check_repeated_errors(
                        pool, session_id, normalized_project,
                        f"품질 부족: {details.get('note', '')[:100]}",
                    )
            except Exception as e_refl:
                logger.debug("b1_reflexion_error", error=str(e_refl))

        logger.info("self_eval_complete", score=overall, message=message_id[:8])
        return overall

    except Exception as e:
        logger.debug("self_eval_error", error=str(e))
        return None


async def _check_repeated_errors(
    pool,
    session_id: str,
    project: Optional[str],
    subject: str,
) -> None:
    """
    동일 error_pattern subject가 최근 7일 내 3회 이상 반복되면
    ai_meta_memory에 permanent correction directive를 생성한다.
    """
    if not subject or not project:
        return
    try:
        # subject 핵심 키워드 추출 (앞 50자)
        subject_key = subject[:50]
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM memory_facts
                   WHERE project = $1
                     AND category = 'error_pattern'
                     AND subject LIKE $2
                     AND created_at > NOW() - interval '7 days'""",
                project,
                f"%{subject_key}%",
            )
            if count is not None and int(count) >= 3:
                # 이미 동일 교정 지시가 있는지 확인
                existing = await conn.fetchval(
                    """SELECT COUNT(*) FROM ai_meta_memory
                       WHERE project = $1
                         AND category = 'correction_directive'
                         AND key LIKE $2""",
                    project,
                    f"%{subject_key[:30]}%",
                )
                if existing and int(existing) > 0:
                    logger.debug("repeated_error_directive_exists", subject=subject_key[:30])
                    return

                # 영구 교정 지시 생성
                from anthropic import AsyncAnthropic as _CorrClient
                _corr_client = _CorrClient()
                _corr_resp = await _corr_client.messages.create(
                    model=_HAIKU_MODEL,
                    max_tokens=256,
                    messages=[{"role": "user", "content": (
                        f"다음 AI 오류가 {count}회 반복되고 있습니다.\n"
                        f"오류 유형: {subject}\n"
                        f"프로젝트: {project}\n\n"
                        f"이 오류를 영구적으로 방지하기 위한 구체적 지시를 1-2문장으로 작성하세요.\n"
                        f"'항상 ~하라', '절대 ~하지 마라' 형태로 작성.\n"
                        f"지시문만 반환하세요."
                    )}],
                )
                directive = _corr_resp.content[0].text.strip() if _corr_resp.content else ""
                if directive:
                    await conn.execute(
                        """INSERT INTO ai_meta_memory (project, category, key, value)
                           VALUES ($1, 'correction_directive', $2, $3)
                           ON CONFLICT (project, category, key) DO UPDATE SET value = $3""",
                        project,
                        f"반복오류교정: {subject_key[:30]}",
                        directive[:500],
                    )
                    logger.warning(
                        "repeated_error_correction_created",
                        project=project,
                        subject=subject_key[:30],
                        count=count,
                    )
    except Exception as e:
        logger.debug("check_repeated_errors_error", error=str(e))


def should_stop_generation(quality_scores: list) -> tuple[bool, str]:
    """
    자율 실행기에서 품질 점수 추이를 보고 중단 여부를 판단한다.
    Returns: (should_stop, reason)
    """
    if not quality_scores:
        return (False, "")

    # 조건 1: 최근 3개 모두 0.3 미만
    if len(quality_scores) >= 3:
        last3 = quality_scores[-3:]
        if all(s < 0.3 for s in last3):
            return (True, "품질 연속 하락 — 접근 방식 재검토 필요")

    # 조건 2: 최근 5개가 지속적으로 하락 (각각 이전보다 작음)
    if len(quality_scores) >= 5:
        last5 = quality_scores[-5:]
        declining = all(last5[i] < last5[i - 1] for i in range(1, 5))
        if declining:
            return (True, "품질 추세 하락 — 전략 변경 필요")

    return (False, "")
