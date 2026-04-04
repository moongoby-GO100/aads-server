"""
AI-to-AI 피드백 시스템 — Feature 1: Reviewer AI
Pipeline Runner의 코드 diff를 독립 AI(Gemini)가 리뷰.
Developer(Claude Sonnet)와 다른 모델로 에코챔버 방지.
비용: ~$0.01~0.03/리뷰
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_REVIEW_MODEL = "qwen-turbo"


@dataclass
class ReviewVerdict:
    """코드 리뷰 판정 결과."""
    verdict: str  # APPROVE / REQUEST_CHANGES / FLAG
    score: float  # 0.0 ~ 1.0
    feedback: dict  # 상세 피드백
    issues: list  # 발견된 이슈 목록


_REVIEW_SYSTEM_PROMPT = """당신은 AADS의 독립 Code Reviewer AI입니다.
Developer AI(Claude Sonnet)가 작성한 코드를 검증합니다.
Developer와 완전히 독립된 컨텍스트에서 평가합니다.

## 평가 기준 (각 0.0~1.0)
1. correctness (30%): 코드 정확성, 논리 오류, 버그
2. security (25%): API 키 노출, SQL 인젝션, XSS 등 OWASP 취약점
3. scope_compliance (20%): instruction 범위 내 변경만 했는지
4. preservation (15%): 기존 코드 불필요하게 삭제/변경하지 않았는지
5. quality (10%): 가독성, 네이밍, 코딩 관례

## 판정
- APPROVE (가중 평균 0.7+): 코드 품질 양호
- REQUEST_CHANGES (0.4~0.69): 수정 필요, 구체적 피드백 제공
- FLAG (0.4 미만): 심각한 문제, CEO 경고 필요

## 응답 형식 (JSON만):
{
  "verdict": "APPROVE" | "REQUEST_CHANGES" | "FLAG",
  "score": 0.0~1.0,
  "correctness": 0.0~1.0,
  "security": 0.0~1.0,
  "scope_compliance": 0.0~1.0,
  "preservation": 0.0~1.0,
  "quality": 0.0~1.0,
  "issues": ["구체적 문제점"],
  "suggestions": ["개선 제안"],
  "summary": "한줄 요약"
}"""


async def review_code_diff(
    project: str,
    job_id: str,
    diff: str,
    instruction: str,
    files_changed: Optional[list] = None,
) -> ReviewVerdict:
    """코드 diff를 독립 AI로 리뷰. Claude Haiku 사용."""
    start = time.time()

    if not diff or not diff.strip():
        return ReviewVerdict(
            verdict="APPROVE",
            score=1.0,
            feedback={"summary": "변경사항 없음"},
            issues=[],
        )

    # diff 크기 제한 (10KB)
    truncated_diff = diff[:10000]
    if len(diff) > 10000:
        truncated_diff += "\n... [diff 일부 생략]"

    prompt = f"""다음 코드 변경사항을 리뷰하세요.

프로젝트: {project}
작업 지시: {instruction[:500]}
변경 파일: {', '.join(files_changed or [])}

```diff
{truncated_diff}
```

위 기준에 따라 JSON으로 판정하세요."""

    try:
        from app.core.anthropic_client import call_background_llm
        result_text = await call_background_llm(
            prompt=prompt,
            system=_REVIEW_SYSTEM_PROMPT,
            max_tokens=1024,
        )

        if not result_text:
            logger.warning(f"code_reviewer_no_response: job_id={job_id}")
            return ReviewVerdict(
                verdict="FLAG",
                score=0.5,
                feedback={"summary": "리뷰 AI 응답 없음"},
                issues=["리뷰 AI가 응답하지 않음"],
            )

        # JSON 파싱
        text = result_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        import re
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            details = json.loads(json_match.group())
        else:
            details = json.loads(text)

        # 가중 평균 계산
        score = (
            float(details.get("correctness", 0.5)) * 0.30
            + float(details.get("security", 0.5)) * 0.25
            + float(details.get("scope_compliance", 0.5)) * 0.20
            + float(details.get("preservation", 0.5)) * 0.15
            + float(details.get("quality", 0.5)) * 0.10
        )
        score = min(1.0, max(0.0, score))

        # 판정
        if score >= 0.7:
            verdict = "APPROVE"
        elif score >= 0.4:
            verdict = "REQUEST_CHANGES"
        else:
            verdict = "FLAG"

        # DB 저장
        try:
            from app.core.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO code_reviews
                       (job_id, project, verdict, score, feedback, diff_size, model_used, cost)
                       VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)""",
                    job_id, project, verdict, score,
                    json.dumps(details, ensure_ascii=False),
                    len(diff),
                    _REVIEW_MODEL,
                    0.01,  # 예상 비용
                )
        except Exception as db_err:
            logger.warning(f"code_reviewer_db_save_error: error={db_err}")

        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            f"code_review_complete: job_id={job_id} verdict={verdict} "
            f"score={round(score, 3)} duration_ms={duration_ms}"
        )

        return ReviewVerdict(
            verdict=verdict,
            score=score,
            feedback=details,
            issues=details.get("issues", []),
        )

    except Exception as e:
        logger.error(f"code_reviewer_error: job_id={job_id} error={e}")
        return ReviewVerdict(
            verdict="FLAG",
            score=0.5,
            feedback={"error": str(e), "summary": "리뷰 중 오류 발생"},
            issues=[f"리뷰 오류: {str(e)[:200]}"],
        )
