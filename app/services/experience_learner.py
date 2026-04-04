"""
AI 자율 연구개선 — Phase 3: 경험 기반 학습 강화
Pipeline Runner 완료 작업 → 성공/실패 패턴 추출 → pgvector 저장.
새 작업 시 유사 경험 검색 → 프롬프트 주입으로 품질 향상.
비용: ~$0.002/작업 (Haiku 1회 패턴 추출)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_HAIKU_MODEL = "qwen-turbo"


@dataclass
class ExperiencePattern:
    """작업 경험 패턴."""
    project: str
    task_type: str  # code_fix, feature, refactor, config, deploy
    outcome: str  # success, failure
    pattern: str  # 핵심 패턴 요약
    lessons: list = field(default_factory=list)
    cost: float = 0.0


async def extract_experience_from_job(pool, job_id: str) -> Optional[ExperiencePattern]:
    """완료된 Pipeline 작업에서 경험 패턴 추출.

    done 상태 작업 → instruction + result + diff 분석 → 패턴 추출 → DB 저장.
    """
    try:
        async with pool.acquire() as conn:
            job = await conn.fetchrow(
                """SELECT job_id, project, instruction, status,
                          result_output, git_diff, review_feedback,
                          created_at, updated_at
                   FROM pipeline_jobs
                   WHERE job_id = $1""",
                job_id,
            )

            if not job:
                return None

            outcome = "success" if job["status"] == "done" else "failure"
            instruction = str(job["instruction"] or "")[:500]
            result_output = str(job["result_output"] or "")[:500]
            review_feedback = str(job["review_feedback"] or "")[:300]
            diff = str(job["git_diff"] or "")[:1000]

            # Haiku로 패턴 추출
            from app.core.anthropic_client import call_llm_with_fallback
            prompt = f"""다음 AI 코딩 작업의 경험 패턴을 추출하세요.

프로젝트: {job['project']}
결과: {outcome}
지시: {instruction}
실행 결과: {result_output[:300]}
리뷰 피드백: {review_feedback[:200]}
코드 변경: {diff[:500]}

JSON으로 반환:
{{"task_type": "code_fix feature refactor config deploy",
  "pattern": "이 유형 작업의 핵심 패턴 (1문장)",
  "lessons": ["교훈 1", "교훈 2"],
  "keywords": ["관련 키워드"]}}"""

            result_text = await call_llm_with_fallback(
                prompt=prompt,
                model=_HAIKU_MODEL,
                max_tokens=256,
            )

            if not result_text:
                return None

            text = result_text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = {"task_type": "unknown", "pattern": text[:200], "lessons": [], "keywords": []}

            experience = ExperiencePattern(
                project=job["project"],
                task_type=data.get("task_type", "unknown"),
                outcome=outcome,
                pattern=data.get("pattern", "")[:500],
                lessons=data.get("lessons", [])[:5],
                cost=0.002,
            )

            # DB 저장 (memory_facts 활용)
            content = json.dumps({
                "job_id": job_id,
                "task_type": experience.task_type,
                "outcome": outcome,
                "pattern": experience.pattern,
                "lessons": experience.lessons,
                "keywords": data.get("keywords", []),
                "instruction_preview": instruction[:200],
            }, ensure_ascii=False)

            category = "experience_success" if outcome == "success" else "experience_failure"
            await conn.execute(
                """INSERT INTO memory_facts
                   (project, category, subject, detail, confidence, tags)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                job["project"],
                category,
                f"{experience.task_type}: {experience.pattern[:100]}",
                content,
                0.8 if outcome == "success" else 0.7,
                ["experience", experience.task_type, outcome],
            )

            # 임베딩 생성 (유사 경험 검색용)
            try:
                from app.services.fact_extractor import _embed_facts
                fact_id = await conn.fetchval(
                    """SELECT id FROM memory_facts
                       WHERE project = $1 AND category = $2
                       ORDER BY created_at DESC LIMIT 1""",
                    job["project"], category,
                )
                if fact_id:
                    import asyncio
                    asyncio.create_task(_embed_facts([{
                        "id": str(fact_id),
                        "category": category,
                        "subject": f"{experience.task_type}: {experience.pattern[:100]}",
                    }]))
            except Exception:
                pass  # 임베딩 실패해도 경험 저장은 유지

            logger.info(
                "experience_extracted",
                extra={"job_id": job_id, "outcome": outcome, "task_type": experience.task_type},
            )
            return experience

    except Exception as e:
        logger.warning("experience_extraction_error", extra={"error": str(e), "job_id": job_id})
        return None


async def find_similar_experiences(
    pool,
    instruction: str,
    project: str,
    limit: int = 3,
) -> list:
    """유사 과거 경험을 검색하여 프롬프트 주입용 텍스트 생성.

    pgvector 임베딩 유사도로 검색. 임베딩 없으면 키워드 매칭 폴백.
    """
    experiences = []

    try:
        # 1차: 임베딩 유사도 검색
        try:
            from app.services.chat_embedding_service import embed_texts
            embeddings = await embed_texts([instruction[:500]])
            if embeddings and embeddings[0]:
                embedding_str = "[" + ",".join(str(x) for x in embeddings[0]) + "]"
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        """SELECT subject, detail, category,
                                  1 - (embedding <=> $1::vector) AS similarity
                           FROM memory_facts
                           WHERE project = $2
                             AND category IN ('experience_success', 'experience_failure')
                             AND embedding IS NOT NULL
                             AND confidence > 0.3
                           ORDER BY embedding <=> $1::vector
                           LIMIT $3""",
                        embedding_str, project, limit,
                    )

                    for r in rows:
                        detail = r["detail"]
                        if isinstance(detail, str):
                            try:
                                detail = json.loads(detail)
                            except (json.JSONDecodeError, TypeError):
                                detail = {"pattern": detail[:200]}
                        experiences.append({
                            "outcome": "성공" if r["category"] == "experience_success" else "실패",
                            "pattern": detail.get("pattern", r["subject"]),
                            "lessons": detail.get("lessons", []),
                            "similarity": round(float(r["similarity"]), 3),
                        })
        except Exception as embed_err:
            logger.debug("experience_embedding_search_failed", extra={"error": str(embed_err)})

        # 2차: 임베딩 실패 시 키워드 매칭 폴백
        if not experiences:
            async with pool.acquire() as conn:
                # instruction에서 키워드 추출 (단순 분할)
                keywords = [w for w in instruction.split() if len(w) > 3][:5]
                if keywords:
                    keyword_pattern = "%".join(keywords[:3])
                    rows = await conn.fetch(
                        """SELECT subject, detail, category
                           FROM memory_facts
                           WHERE project = $1
                             AND category IN ('experience_success', 'experience_failure')
                             AND (subject ILIKE $2 OR detail::text ILIKE $2)
                             AND confidence > 0.3
                           ORDER BY created_at DESC
                           LIMIT $3""",
                        project, f"%{keyword_pattern}%", limit,
                    )
                    for r in rows:
                        detail = r["detail"]
                        if isinstance(detail, str):
                            try:
                                detail = json.loads(detail)
                            except (json.JSONDecodeError, TypeError):
                                detail = {"pattern": detail[:200]}
                        experiences.append({
                            "outcome": "성공" if r["category"] == "experience_success" else "실패",
                            "pattern": detail.get("pattern", r["subject"]),
                            "lessons": detail.get("lessons", []),
                            "similarity": 0.5,
                        })

    except Exception as e:
        logger.debug("find_experiences_error", extra={"error": str(e)})

    return experiences


def format_experiences_for_prompt(experiences: list) -> str:
    """경험 목록을 프롬프트 주입용 텍스트로 포맷."""
    if not experiences:
        return ""

    lines = ["## 유사 과거 경험 (참고)"]
    for i, exp in enumerate(experiences, 1):
        outcome_emoji = "V" if exp["outcome"] == "성공" else "X"
        lines.append(f"{i}. [{outcome_emoji}] [{exp['outcome']}] {exp['pattern']}")
        for lesson in exp.get("lessons", [])[:2]:
            lines.append(f"   - {lesson}")

    return "\n".join(lines)


async def process_completed_jobs(pool) -> dict:
    """최근 완료 작업 중 경험 미추출 건을 일괄 처리.

    매일 07:30 UTC에 research_agent 이후 실행.
    done 상태 작업 → 템플릿 기반 요약 → ai_observations 저장 (LLM 호출 없음).
    """
    result = {"processed": 0}

    try:
        async with pool.acquire() as conn:
            # 최근 3일 done 작업 중 경험 미기록 건
            jobs = await conn.fetch("""
                SELECT j.job_id, j.project, j.instruction, j.status,
                       j.result_output, j.review_feedback, j.created_at
                FROM pipeline_jobs j
                WHERE j.status = 'done'
                  AND j.created_at >= NOW() - interval '3 days'
                  AND NOT EXISTS (
                      SELECT 1 FROM ai_observations ao
                      WHERE ao.key = j.job_id
                        AND ao.category = 'experience'
                  )
                ORDER BY j.created_at DESC
                LIMIT 10
            """)

            for job in jobs:
                try:
                    # 템플릿 기반 요약 생성 (LLM 호출 없음)
                    instruction_preview = (job['instruction'] or '')[:100]
                    result_preview = (job['result_output'] or '')[:100]
                    review = (job['review_feedback'] or '')[:80]

                    summary = (
                        f"[{job['project']}] 완료 작업: {instruction_preview}... "
                        f"결과: {result_preview}... "
                        + ("리뷰: " + review if review else "")
                    )

                    # ai_observations 테이블에 INSERT (중복 시 UPDATE)
                    await conn.execute(
                        """INSERT INTO ai_observations
                           (category, key, value, confidence, project)
                           VALUES ($1, $2, $3, $4, $5)
                           ON CONFLICT (category, key, COALESCE(project, ''))
                           DO UPDATE SET value = $3, updated_at = NOW(),
                                         usage_count = usage_count + 1""",
                        'experience',                    # category
                        job['job_id'],                   # key
                        summary,                         # value
                        0.8,                             # confidence
                        job['project'],                  # project
                    )

                    result["processed"] += 1

                except Exception as e:
                    logger.warning(
                        "experience_store_error",
                        extra={"job_id": job['job_id'], "error": str(e)}
                    )
                    continue

            if result["processed"] > 0:
                logger.info("experience_batch_complete", extra=result)

    except Exception as e:
        logger.warning("experience_batch_error", extra={"error": str(e)})

    return result
