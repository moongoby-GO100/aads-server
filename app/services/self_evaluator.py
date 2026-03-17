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
- tool_grounding: 도구를 사용하여 주장/데이터를 검증했는가? 단, 대화/인사/의견/설명 등 도구가 불필요한 질문이면 0.5로 평가.
- actionability: 구체적인 다음 단계를 제시하는가? (모호한 서술만 있으면 낮음)
- tool_needed: 이 질문에 도구 사용이 필요했는가? (true/false)

{{"accuracy": 0.0~1.0, "completeness": 0.0~1.0, "relevance": 0.0~1.0, "tool_grounding": 0.0~1.0, "actionability": 0.0~1.0, "tool_needed": true/false, "overall": 0.0~1.0, "note": "한줄 평가"}}

overall = accuracy×0.3 + completeness×0.25 + tool_grounding×0.15 + relevance×0.15 + actionability×0.15 으로 계산.
단, tool_needed=false이면 tool_grounding=0.5로 재설정 후 계산.
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
        from app.core.anthropic_client import get_client
        client = get_client()

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
        # tool_needed=false이면 tool_grounding을 중립값 0.5로 재설정 (대화형 응답 패널티 방지)
        tool_needed = details.get("tool_needed", True)
        if isinstance(tool_needed, str):
            tool_needed = tool_needed.lower() not in ("false", "no", "0")
        tg_score = float(details.get("tool_grounding", 0.5))
        if not tool_needed:
            tg_score = max(tg_score, 0.5)
            details["tool_grounding"] = tg_score
            # 대화형 응답은 actionability 패널티 제거
            act_score = max(float(details.get("actionability", 0.5)), 0.5)
            details["actionability"] = act_score
        else:
            act_score = float(details.get("actionability", 0.5))
        # Weighted overall: accuracy×0.3 + completeness×0.25 + tool_grounding×0.15 + relevance×0.15 + actionability×0.15
        overall = (
            float(details.get("accuracy", 0.5)) * 0.30
            + float(details.get("completeness", 0.5)) * 0.25
            + tg_score * 0.15
            + float(details.get("relevance", 0.5)) * 0.15
            + act_score * 0.15
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
        if overall < 0.5 and session_id:
            try:
                reflection_prompt = (
                    f"이 AI 응답의 품질이 낮습니다 (점수: {overall:.1f}).\n"
                    f"무엇이 잘못되었고, 다음에 어떻게 개선해야 하는지 1-2문장으로 반성하세요.\n\n"
                    f"사용자: {user_message[:300]}\n"
                    f"AI: {ai_response[:500]}\n"
                    f"품질 세부: {json.dumps(details, ensure_ascii=False)}\n\n"
                    f"반성문만 작성하세요."
                )
                from app.core.anthropic_client import get_client as _get_refl_client
                _refl_client = _get_refl_client()
                _refl_resp = await _refl_client.messages.create(
                    model=_HAIKU_MODEL,
                    max_tokens=256,
                    messages=[{"role": "user", "content": reflection_prompt}],
                )
                reflection = _refl_resp.content[0].text.strip() if _refl_resp.content else ""
                if reflection:
                    # MEDIUM-6: 프로젝트명 정규화 (workspace_name → project code)
                    normalized_project = _normalize_project(project)
                    # H-10 fix: fetch the fact ID inside the async with block
                    fact_id_for_embed = None
                    async with pool.acquire() as conn_refl:
                        await conn_refl.execute(
                            """INSERT INTO memory_facts (session_id, project, category, subject, detail, confidence, tags)
                               VALUES ($1::uuid, $2, 'error_pattern', $3, $4, 0.85, ARRAY['reflexion', 'self-improvement'])""",
                            uuid.UUID(session_id),
                            normalized_project,
                            f"품질 부족: {details.get('note', '')[:100]}",
                            reflection[:500],
                        )
                        # Fetch the ID while connection is still active
                        fact_id_for_embed = await conn_refl.fetchval(
                            "SELECT id FROM memory_facts WHERE session_id = $1::uuid AND category = 'error_pattern' ORDER BY created_at DESC LIMIT 1",
                            uuid.UUID(session_id),
                        )
                    logger.info("b1_reflexion_saved", score=overall, session=session_id[:8])
                    # 임베딩 생성 (fact_extractor 공용 함수 활용)
                    try:
                        from app.services.fact_extractor import _embed_facts
                        import asyncio
                        if fact_id_for_embed:
                            asyncio.create_task(_embed_facts([{
                                "id": str(fact_id_for_embed),
                                "category": "error_pattern",
                                "subject": f"품질 부족: {details.get('note', '')[:100]}",
                            }]))
                    except Exception:
                        pass  # 임베딩 실패해도 reflexion 저장은 유지
                    # Check for repeated errors → generate permanent correction directive
                    await _check_repeated_errors(
                        pool, session_id, normalized_project,
                        f"품질 부족: {details.get('note', '')[:100]}",
                    )
            except Exception as e_refl:
                logger.debug("b1_reflexion_error", error=str(e_refl))

        logger.info("self_eval_complete", score=overall, message=message_id[:8])

        # P3: Reflexion 효과 검증 — 반성 후 실제 품질 개선 여부 추적
        if session_id and pool:
            try:
                async with pool.acquire() as conn_p3:
                    recent_scores = await conn_p3.fetch(
                        """SELECT quality_score FROM chat_messages
                           WHERE session_id = $1::uuid
                             AND quality_score IS NOT NULL
                             AND role = 'assistant'
                           ORDER BY created_at DESC LIMIT 3""",
                        uuid.UUID(session_id),
                    )
                    if len(recent_scores) >= 2:
                        scores = [r["quality_score"] for r in recent_scores]
                        prev_avg = sum(scores[1:]) / len(scores[1:])
                        curr_score = scores[0]
                        if curr_score > prev_avg + 0.1:
                            # 반성이 효과적 → 최근 반성문 confidence 강화
                            await conn_p3.execute(
                                """UPDATE memory_facts
                                   SET confidence = LEAST(0.95, confidence + 0.05),
                                       updated_at = NOW()
                                   WHERE session_id = $1::uuid
                                     AND category = 'error_pattern'
                                     AND tags @> ARRAY['reflexion']
                                     AND created_at > NOW() - interval '1 hour'""",
                                uuid.UUID(session_id),
                            )
                            logger.info("p3_reflexion_effective", improvement=round(curr_score - prev_avg, 3))
                        elif curr_score < prev_avg - 0.05:
                            # 반성이 비효과적 → 반성문 confidence 감쇠
                            await conn_p3.execute(
                                """UPDATE memory_facts
                                   SET confidence = GREATEST(0.3, confidence * 0.85),
                                       updated_at = NOW()
                                   WHERE session_id = $1::uuid
                                     AND category = 'error_pattern'
                                     AND tags @> ARRAY['reflexion']
                                     AND created_at > NOW() - interval '1 hour'""",
                                uuid.UUID(session_id),
                            )
                            logger.info("p3_reflexion_ineffective", delta=round(curr_score - prev_avg, 3))
            except Exception as e_p3:
                logger.debug("p3_reflexion_verify_error", error=str(e_p3))

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
    embedding cosine similarity >= 0.8 로 유사 error_pattern을 찾아
    최근 7일 내 2회 이상 반복되면 ai_meta_memory에 correction directive 생성.
    (기존 subject substring 매칭은 LLM이 매번 다른 텍스트를 생성하여 작동 불가 → 임베딩 기반으로 교체)
    """
    if not subject or not project:
        return
    try:
        # 현재 에러의 임베딩 생성
        from app.services.chat_embedding_service import embed_texts
        embeddings = await embed_texts([subject])
        if not embeddings or not embeddings[0]:
            logger.debug("repeated_error_no_embedding", subject=subject[:50])
            return
        current_embedding = embeddings[0]
        embedding_str = "[" + ",".join(str(x) for x in current_embedding) + "]"

        async with pool.acquire() as conn:
            # cosine similarity >= 0.8 인 최근 7일 error_pattern 검색
            similar_facts = await conn.fetch(
                """SELECT id, subject, detail,
                          1 - (embedding <=> $1::vector) AS similarity
                   FROM memory_facts
                   WHERE project = $2
                     AND category = 'error_pattern'
                     AND embedding IS NOT NULL
                     AND created_at > NOW() - interval '7 days'
                     AND 1 - (embedding <=> $1::vector) >= 0.8
                   ORDER BY similarity DESC
                   LIMIT 20""",
                embedding_str,
                project,
            )
            count = len(similar_facts)
            logger.info(
                "repeated_error_similarity_check",
                project=project,
                subject=subject[:50],
                similar_count=count,
            )

            # 2회 이상 유사 에러가 있으면 교정 지시 생성
            if count >= 2:
                # 이미 유사한 교정 지시가 있는지 확인 (임베딩으로)
                existing_directive = await conn.fetchval(
                    """SELECT COUNT(*) FROM ai_meta_memory
                       WHERE project = $1
                         AND category = 'correction_directive'
                         AND updated_at > NOW() - interval '7 days'""",
                    project,
                )
                # 프로젝트당 최근 7일 내 교정 지시가 5개 이상이면 스킵 (과다 생성 방지)
                if existing_directive and int(existing_directive) >= 5:
                    logger.debug("repeated_error_directive_limit_reached", project=project)
                    return

                # 유사 에러들의 subject를 모아서 패턴 요약에 활용
                similar_subjects = [r["subject"][:100] for r in similar_facts[:5]]
                similar_details = [r["detail"][:200] for r in similar_facts[:3] if r["detail"]]

                # 영구 교정 지시 생성
                from app.core.anthropic_client import get_client as _get_corr_client
                _corr_client = _get_corr_client()
                _corr_resp = await _corr_client.messages.create(
                    model=_HAIKU_MODEL,
                    max_tokens=256,
                    messages=[{"role": "user", "content": (
                        f"다음 AI 오류가 {count}회 반복되고 있습니다.\n"
                        f"프로젝트: {project}\n\n"
                        f"반복 오류 패턴:\n"
                        + "\n".join(f"- {s}" for s in similar_subjects)
                        + "\n\n반성문 요약:\n"
                        + "\n".join(f"- {d}" for d in similar_details)
                        + "\n\n이 오류를 영구적으로 방지하기 위한 구체적 지시를 1-2문장으로 작성하세요.\n"
                        f"'항상 ~하라', '절대 ~하지 마라' 형태로 작성.\n"
                        f"지시문만 반환하세요."
                    )}],
                )
                directive = _corr_resp.content[0].text.strip() if _corr_resp.content else ""
                if directive:
                    # 키에 타임스탬프를 포함하여 고유성 보장
                    import time
                    directive_key = f"반복오류교정:{project}:{int(time.time())}"
                    # value는 jsonb 컬럼이므로 JSON 객체로 감싸서 저장
                    import json as _json
                    directive_json = _json.dumps({"directive": directive[:500], "similar_count": count, "project": project})
                    await conn.execute(
                        """INSERT INTO ai_meta_memory (project, category, key, value, updated_at)
                           VALUES ($1, 'correction_directive', $2, $3::jsonb, NOW())
                           ON CONFLICT (project, category, key) DO UPDATE SET value = $3::jsonb, updated_at = NOW()""",
                        project,
                        directive_key,
                        directive_json,
                    )
                    logger.warning(
                        "repeated_error_correction_created",
                        project=project,
                        similar_count=count,
                        top_similarity=round(float(similar_facts[0]["similarity"]), 3) if similar_facts else 0,
                        directive_key=directive_key,
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
