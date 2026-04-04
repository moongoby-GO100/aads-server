"""
F11: Self-Evaluation — AI 응답 후 Haiku로 정확도/완성도/관련성 평가.
chat_messages.quality_score + quality_details 저장.
비용: ~$0.0003/턴 (200자 이상 응답만).

auto_reflexion_loop: 완전 자동 Reflexion 루프 (LLM 호출 없이 키워드/패턴 기반).
- score < 0.5 → 실패 유형 분류(정보_부족/도구_오류/형식_부적합/지시_위반) → correction_directive 저장
- 연속 3회 실패 (DB 카운터) → 사전 정의 strategy_update 저장 + escalation_needed=true
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

_ENABLED = os.getenv("SELF_EVAL_ENABLED", "true").lower() == "true"
_MIN_RESPONSE_LEN = int(os.getenv("SELF_EVAL_MIN_LEN", "1"))
_HAIKU_MODEL = os.getenv("SELF_EVAL_MODEL", "qwen-turbo")

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

이전 대화 맥락: {prev_context}
사용자 질문: {user_msg}
AI 응답 (앞 1000자): {ai_msg}

6가지 기준으로 0.0~1.0 점수를 JSON으로 반환:
- context_awareness: 이전 대화 맥락을 정확히 이해하고 답변했는가? 이전에 논의된 내용을 무시하거나 엉뚱한 답을 하면 0점. 맥락 없는 첫 질문이면 0.5.
- accuracy: 사실적 정확성
- completeness: 응답의 완성도
- relevance: 질문과의 관련성
- tool_grounding: 도구를 사용하여 주장/데이터를 검증했는가? 대화/인사/의견/설명 등 도구 불필요 시 0.5.
- actionability: 구체적인 다음 단계를 제시하는가?
- tool_needed: 이 질문에 도구 사용이 필요했는가? (true/false)

{{"context_awareness": 0.0~1.0, "accuracy": 0.0~1.0, "completeness": 0.0~1.0, "relevance": 0.0~1.0, "tool_grounding": 0.0~1.0, "actionability": 0.0~1.0, "tool_needed": true/false, "overall": 0.0~1.0, "note": "한줄 평가"}}

overall = context_awareness×0.25 + accuracy×0.25 + completeness×0.15 + tool_grounding×0.15 + relevance×0.10 + actionability×0.10 으로 계산.
단, tool_needed=false이면 tool_grounding=0.5로 재설정 후 계산. 이전 맥락이 없으면 context_awareness=0.5.
JSON만 반환. 마크다운 코드블록 없이."""


async def evaluate_response(
    user_message: str,
    ai_response: str,
    message_id: str,
    session_id: Optional[str] = None,
    project: Optional[str] = None,
    prev_messages: Optional[list] = None,
) -> Optional[float]:
    """AI 응답 품질을 평가하고 DB에 저장. B1: 낮은 품질 시 reflexion 생성."""
    if not _ENABLED or len(ai_response) < _MIN_RESPONSE_LEN:
        return None

    try:
        from app.core.anthropic_client import call_llm_with_fallback

        # 이전 대화 맥락 구성 (최근 6개 메시지)
        prev_context = "없음 (첫 대화)"
        if prev_messages:
            ctx_parts = []
            for m in prev_messages[-6:]:
                role = m.get("role", "")
                content = m.get("content", "")
                if isinstance(content, list):
                    content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
                if not isinstance(content, str):
                    content = str(content)
                if content.strip() and role in ("user", "assistant"):
                    label = "CEO" if role == "user" else "AI"
                    ctx_parts.append(f"[{label}] {content[:150]}")
            prev_context = "\n".join(ctx_parts[-6:]) if ctx_parts else "없음 (첫 대화)"

        prompt = _EVAL_PROMPT.format(
            prev_context=prev_context[:800],
            user_msg=user_message[:300],
            ai_msg=ai_response[:1000],
        )

        raw_text = await call_llm_with_fallback(prompt, model=_HAIKU_MODEL, max_tokens=256)
        if raw_text is None:
            logger.warning("self_eval_llm_all_failed", message=message_id[:8])
            return None

        text = raw_text.strip()
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
        # Weighted overall: context×0.25 + accuracy×0.25 + completeness×0.15 + tool_grounding×0.15 + relevance×0.10 + actionability×0.10
        ctx_score = float(details.get("context_awareness", 0.5))
        overall = (
            ctx_score * 0.25
            + float(details.get("accuracy", 0.5)) * 0.25
            + float(details.get("completeness", 0.5)) * 0.15
            + tg_score * 0.15
            + float(details.get("relevance", 0.5)) * 0.10
            + act_score * 0.10
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
                from app.core.anthropic_client import call_llm_with_fallback as _refl_llm
                _refl_text = await _refl_llm(reflection_prompt, model=_HAIKU_MODEL, max_tokens=256)
                reflection = _refl_text.strip() if _refl_text else ""
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

        # D: 세션 품질 알림 — 최근 10건 평균 0.5 이하 시 텔레그램 경고 (1시간 1회 제한)
        if overall < 0.5 and session_id:
            try:
                async with pool.acquire() as conn_alert:
                    avg_row = await conn_alert.fetchrow(
                        """SELECT AVG(quality_score) as avg_q, COUNT(*) as cnt
                           FROM (SELECT quality_score FROM chat_messages
                                 WHERE session_id = $1::uuid AND quality_score IS NOT NULL
                                 ORDER BY created_at DESC LIMIT 10) sub""",
                        uuid.UUID(session_id),
                    )
                    if avg_row and avg_row["cnt"] >= 5 and avg_row["avg_q"] is not None and float(avg_row["avg_q"]) < 0.5:
                        # 1시간 중복 방지
                        _alert_key = f"quality_alert:{session_id[:8]}"
                        _recent = await conn_alert.fetchval(
                            """SELECT COUNT(*) FROM ai_meta_memory
                               WHERE key = $1 AND created_at > NOW() - INTERVAL '1 hour'""",
                            _alert_key,
                        )
                        if (_recent or 0) == 0:
                            await conn_alert.execute(
                                """INSERT INTO ai_meta_memory (category, key, value, confidence)
                                   VALUES ('quality_alert', $1, $2::jsonb, 0.9)""",
                                _alert_key,
                                json.dumps({"session_id": session_id, "avg_quality": float(avg_row["avg_q"]), "count": avg_row["cnt"]}, ensure_ascii=False),
                            )
                            # 텔레그램 알림
                            try:
                                from app.services.telegram_bot import init_telegram_bot
                                _bot = init_telegram_bot()
                                if _bot:
                                    _title_row = await conn_alert.fetchval(
                                        "SELECT title FROM chat_sessions WHERE id = $1::uuid",
                                        uuid.UUID(session_id),
                                    )
                                    await _bot.send_message(
                                        f"⚠️ [품질 경고] 세션 '{_title_row or session_id[:8]}'\n"
                                        f"최근 10건 평균: {float(avg_row['avg_q']):.2f} (임계값 0.5)\n"
                                        f"현재 점수: {overall:.2f}"
                                    )
                            except Exception:
                                pass
                            logger.warning("session_quality_alert", session=session_id[:8], avg=float(avg_row["avg_q"]))
            except Exception as e_alert:
                logger.debug("quality_alert_error", error=str(e_alert))

        return overall

    except Exception as e:
        logger.warning("self_eval_error", error=str(e))
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
                from app.core.anthropic_client import call_llm_with_fallback as _corr_llm
                _corr_prompt = (
                    f"다음 AI 오류가 {count}회 반복되고 있습니다.\n"
                    f"프로젝트: {project}\n\n"
                    f"반복 오류 패턴:\n"
                    + "\n".join(f"- {s}" for s in similar_subjects)
                    + "\n\n반성문 요약:\n"
                    + "\n".join(f"- {d}" for d in similar_details)
                    + "\n\n이 오류를 영구적으로 방지하기 위한 구체적 지시를 1-2문장으로 작성하세요.\n"
                    f"'항상 ~하라', '절대 ~하지 마라' 형태로 작성.\n"
                    f"지시문만 반환하세요."
                )
                _corr_text = await _corr_llm(_corr_prompt, model=_HAIKU_MODEL, max_tokens=256)
                directive = _corr_text.strip() if _corr_text else ""
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


# ── 키워드/패턴 기반 품질 평가 헬퍼 상수 ─────────────────────────────────────────

# 응답 품질 저하 신호 패턴
_NEGATIVE_PATTERNS = [
    "죄송합니다",
    "알 수 없습니다",
    "확인이 필요합니다",
    "오류가 발생",
    "에러가 발생",
    "실패했습니다",
    "처리할 수 없",
    "이해하지 못",
    "잘 모르겠",
    "정보가 없",
    "제공할 수 없",
]

# 실패 유형별 교정 지시 (LLM 없이 사전 정의)
_FAILURE_DIRECTIVES: dict[str, str] = {
    "정보_부족": "응답 전 반드시 관련 도구/DB를 조회하여 구체적 정보를 확보하라.",
    "도구_오류": "도구 호출 실패 시 즉시 대안 도구를 시도하고, 실패 원인을 명시하라.",
    "형식_부적합": "CEO 요청 형식(표/코드/목록 등)을 정확히 파악하고 그 형식으로만 응답하라.",
    "지시_위반": "CEO의 명시적 지시(절대/반드시/금지 등)를 최우선으로 준수하라.",
}

# 실패 유형 분류 키워드 (우선순위 순: 지시_위반 > 도구_오류 > 형식_부적합 > 정보_부족)
_FAILURE_KEYWORDS: dict[str, list[str]] = {
    "지시_위반": ["절대", "반드시", "금지", "하지 마", "하지마", "안돼", "안 돼", "말했잖", "지시했"],
    "도구_오류": ["오류", "에러", "error", "실패", "timeout", "타임아웃", "연결 실패", "접근 불가", "tool"],
    "형식_부적합": ["표로", "코드로", "목록으로", "형식으로", "json으로", "markdown", "마크다운", "번호로"],
    "정보_부족": ["구체적", "자세히", "더 알려", "정확히", "수치", "통계", "데이터", "근거", "출처"],
}


def _calc_keyword_score(query: str, response: str) -> float:
    """
    키워드/패턴 기반 품질 점수 계산 (LLM 호출 없음).

    평가 기준:
    - 부정 패턴 수: 패턴당 -0.15 감점 (최대 -0.60)
    - 응답 길이: 50자 미만이면 -0.20, 200자 이상이면 +0.05 보너스
    - 질문 키워드 포함 여부: 주요 명사 미포함 시 -0.10

    Returns: 0.0 ~ 1.0 점수
    """
    base = 0.70

    # 부정 패턴 감점
    neg_count = sum(1 for pat in _NEGATIVE_PATTERNS if pat in response)
    base -= min(neg_count * 0.15, 0.60)

    # 응답 길이 평가
    resp_len = len(response.strip())
    if resp_len < 50:
        base -= 0.20
    elif resp_len >= 200:
        base += 0.05

    # 질문 핵심 키워드 포함 여부 (간단 명사 추출: 2자 이상 단어)
    query_words = [w for w in query.split() if len(w) >= 2]
    if query_words:
        matched = sum(1 for w in query_words[:5] if w in response)
        if matched == 0:
            base -= 0.10

    return round(min(1.0, max(0.0, base)), 3)


def _classify_failure_type(query: str, response: str) -> str:
    """
    쿼리와 응답 텍스트에서 실패 유형을 분류한다.
    매칭 우선순위: 지시_위반 > 도구_오류 > 형식_부적합 > 정보_부족

    Returns: "정보_부족" | "도구_오류" | "형식_부적합" | "지시_위반"
    """
    combined = query + " " + response
    for failure_type in ["지시_위반", "도구_오류", "형식_부적합", "정보_부족"]:
        keywords = _FAILURE_KEYWORDS[failure_type]
        if any(kw in combined for kw in keywords):
            return failure_type
    return "정보_부족"  # 기본값


async def auto_reflexion_loop(
    query: str,
    response: str,
    project: str,
    pool=None,
    session_id: Optional[str] = None,
) -> Optional[dict]:
    """
    자동 반성 루프 — 응답 품질 자동 평가 + 전략 수정.
    LLM 호출 없이 키워드/패턴 기반으로 실패 원인을 분석하여 비용 효율을 극대화한다.

    흐름:
    1. 키워드/패턴 기반 품질 점수 계산 (기존 evaluate_response 로직과 독립)
    2. score < 0.5 시:
       - 실패 유형 분류: "정보_부족" | "도구_오류" | "형식_부적합" | "지시_위반"
       - correction_directive 생성 → ai_meta_memory에 저장
    3. 연속 실패 카운터 관리 (DB 조회 기반):
       - 같은 프로젝트 최근 7일 내 correction_directive 3회 이상 시
         ai_meta_memory(category='strategy_update') 저장 + escalation_needed=true

    Args:
        query: 사용자 질문
        response: AI 응답 텍스트
        project: 프로젝트 코드 (예: AADS, KIS)
        pool: asyncpg 커넥션 풀 (None이면 내부에서 get_pool() 사용)
        session_id: 세션 ID (선택)

    Returns:
        {"score": float, "failure_type": str, "saved": bool} 또는 None
    """
    if not _ENABLED or not project:
        return None

    try:
        import json as _json
        import time
        from app.core.db_pool import get_pool as _get_pool

        if pool is None:
            pool = _get_pool()

        normalized_project = _normalize_project(project)
        if not normalized_project:
            return None

        # ── Step 1: 키워드/패턴 기반 품질 점수 계산 (LLM 호출 없음) ─────────
        score = _calc_keyword_score(query, response)

        logger.info(
            "auto_reflexion_loop_score",
            score=round(score, 3),
            project=normalized_project,
        )

        # score >= 0.65 이면 이후 단계 불필요
        if score >= 0.65:
            return {"score": score, "failure_type": None, "saved": False}

        # ── Step 2: 실패 유형 분류 + correction_directive 저장 ───────────────
        failure_type = _classify_failure_type(query, response)
        directive_text = _FAILURE_DIRECTIVES.get(failure_type, "응답 품질을 전반적으로 개선하라.")
        directive_key = f"reflexion:{normalized_project}:{int(time.time())}"
        directive_value = _json.dumps({
            "directive": directive_text,
            "failure_type": failure_type,
            "score": round(score, 3),
            "project": normalized_project,
        })
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ai_meta_memory (project, category, key, value, updated_at)
                   VALUES ($1, 'correction_directive', $2, $3::jsonb, NOW())
                   ON CONFLICT (project, category, key)
                   DO UPDATE SET value = $3::jsonb, updated_at = NOW()""",
                normalized_project,
                directive_key,
                directive_value,
            )
        logger.info(
            "auto_reflexion_correction_saved",
            project=normalized_project,
            failure_type=failure_type,
            score=round(score, 3),
            key=directive_key,
        )

        # ── Step 3: 연속 실패 카운터 확인 → strategy_update 저장 ─────────────
        await _check_strategy_update(
            pool=pool,
            project=normalized_project,
            failure_type=failure_type,
            score=score,
        )

        return {"score": score, "failure_type": failure_type, "saved": True}

    except Exception as e:
        logger.debug("auto_reflexion_loop_error", error=str(e))
        return None


async def _check_strategy_update(
    pool,
    project: str,
    failure_type: str,
    score: float,
) -> None:
    """
    연속 실패 카운터 관리 — DB 조회 기반, LLM 호출 없음.

    최근 7일 내 같은 프로젝트 correction_directive 3회 이상이면
    ai_meta_memory(category='strategy_update')에 전략 갱신 지시 저장.
    - escalation_needed=true 메타데이터 포함
    - 24시간 내 중복 생성 방지
    - 실패 유형별 사전 정의 전략 텍스트 사용 (LLM 호출 없음)
    """
    # 실패 유형별 전략 수정 지시 (사전 정의, LLM 호출 불필요)
    _STRATEGY_TEMPLATES: dict[str, str] = {
        "정보_부족": (
            "응답 전 반드시 관련 도구/DB를 먼저 조회하여 실측 데이터를 확보하라. "
            "추측이나 일반론 대신 구체적 수치와 근거를 제시하는 전략으로 전환하라."
        ),
        "도구_오류": (
            "도구 호출 시 예외 처리를 강화하고, 1차 도구 실패 즉시 대안 도구를 시도하라. "
            "오류 원인을 CEO에게 명확히 보고하고 우회 방법을 제시하는 전략으로 전환하라."
        ),
        "형식_부적합": (
            "응답 생성 전 CEO의 요청 형식(표/코드/목록/JSON 등)을 먼저 파악하고 해당 형식으로만 응답하라. "
            "형식 불명확 시 즉시 확인 질문을 먼저 하는 전략으로 전환하라."
        ),
        "지시_위반": (
            "CEO의 절대 지시(절대/반드시/금지/하지마 포함)를 최우선으로 확인한 후 응답하라. "
            "지시 목록을 매 응답 전 내부 검토하는 전략으로 전환하라."
        ),
    }

    try:
        import json as _json
        import time

        async with pool.acquire() as conn:
            # 최근 7일 내 같은 프로젝트 correction_directive 수 확인
            recent_count = await conn.fetchval(
                """SELECT COUNT(*) FROM ai_meta_memory
                   WHERE project = $1
                     AND category = 'correction_directive'
                     AND updated_at > NOW() - interval '7 days'""",
                project,
            )
            count = int(recent_count or 0)

            if count < 3:
                logger.debug(
                    "strategy_update_below_threshold",
                    project=project,
                    count=count,
                )
                return

            # 최근 24시간 내 strategy_update 중복 생성 방지
            existing_update = await conn.fetchval(
                """SELECT COUNT(*) FROM ai_meta_memory
                   WHERE project = $1
                     AND category = 'strategy_update'
                     AND updated_at > NOW() - interval '6 hours'""",
                project,
            )
            if int(existing_update or 0) > 0:
                logger.debug(
                    "strategy_update_skip_recent_exists",
                    project=project,
                    count=count,
                )
                return

            # 실패 유형별 사전 정의 전략 텍스트 선택 (LLM 호출 없음)
            strategy_text = _STRATEGY_TEMPLATES.get(
                failure_type,
                "응답 품질 전반을 개선하라. 매 응답 전 6-criteria 체크리스트를 내부 검토하는 전략으로 전환하라.",
            )

            strategy_key = f"strategy:{project}:{int(time.time())}"
            strategy_value = _json.dumps({
                "strategy": strategy_text,
                "failure_type": failure_type,
                "trigger_count": count,
                "project": project,
                "escalation_needed": True,
            })
            await conn.execute(
                """INSERT INTO ai_meta_memory (project, category, key, value, updated_at)
                   VALUES ($1, 'strategy_update', $2, $3::jsonb, NOW())
                   ON CONFLICT (project, category, key)
                   DO UPDATE SET value = $3::jsonb, updated_at = NOW()""",
                project,
                strategy_key,
                strategy_value,
            )
        logger.warning(
            "strategy_update_created",
            project=project,
            failure_type=failure_type,
            trigger_count=count,
            escalation_needed=True,
            key=strategy_key,
        )
    except Exception as e:
        logger.debug("check_strategy_update_error", error=str(e))


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
