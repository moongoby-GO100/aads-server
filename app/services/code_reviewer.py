"""
AI-to-AI 피드백 시스템 — Feature 1: Reviewer AI
Pipeline Runner의 코드 diff를 독립 AI(Gemini)가 리뷰.
Developer(Claude Sonnet)와 다른 모델로 에코챔버 방지.
비용: ~$0.01~0.03/리뷰
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_REVIEW_MODEL = "qwen-turbo"
_REVIEW_MODEL_FALLBACK = _REVIEW_MODEL  # DB 조회 실패 시 기본값

_DIFF_HEADER_RE = re.compile(r"^diff --git a\/.+ b\/.+$", re.MULTILINE)
_DIFF_HUNK_RE = re.compile(r"^@@ .+ @@$", re.MULTILINE)
_SUSPICIOUS_INPUT_PATTERNS: list[tuple[re.Pattern[str], str, str, bool, str]] = [
    (
        re.compile(
            r"(oauth authentication is currently not supported|failed to authenticate|authentication_error)",
            re.IGNORECASE,
        ),
        "RUNNER_AUTH_FAILURE",
        "runner_execution",
        True,
        "러너 인증 실패 텍스트가 diff 대신 전달되었습니다.",
    ),
    (
        re.compile(
            r"(traceback \(most recent call last\)|importerror:|modulenotfounderror:|syntaxerror:|nameerror:)",
            re.IGNORECASE,
        ),
        "RUNNER_EXECUTION_FAILURE",
        "runner_execution",
        True,
        "러너 실행 오류 텍스트가 diff 대신 전달되었습니다.",
    ),
    (
        re.compile(
            r"(fatal:|not a git repository|ambiguous argument|pathspec .* did not match|bad revision)",
            re.IGNORECASE,
        ),
        "GIT_DIFF_FAILURE",
        "git_diff_capture",
        True,
        "git diff 수집 실패 텍스트가 리뷰 입력으로 들어왔습니다.",
    ),
]


async def _get_review_models() -> list[str]:
    """DB runner_model_config에서 AI_REVIEW 모델 목록 조회."""
    try:
        from app.core.db_pool import get_pool
        import json as _j
        from app.services.model_registry import filter_executable_models
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT models FROM runner_model_config WHERE size = 'AI_REVIEW'"
            )
        if row:
            raw = row["models"]
            if isinstance(raw, str):
                return await filter_executable_models(_j.loads(raw))
            elif isinstance(raw, list):
                return await filter_executable_models(raw)
            return await filter_executable_models(list(raw) if raw else [_REVIEW_MODEL_FALLBACK])
        return [_REVIEW_MODEL_FALLBACK]
    except Exception as e:
        logger.warning("review_model_db_lookup_failed: %s", str(e)[:80])
        return [_REVIEW_MODEL_FALLBACK]


@dataclass
class ReviewVerdict:
    """코드 리뷰 판정 결과."""
    verdict: str  # APPROVE / REQUEST_CHANGES / FLAG
    score: float  # 0.0 ~ 1.0
    feedback: dict  # 상세 피드백
    issues: list  # 발견된 이슈 목록
    flag_category: Optional[str] = None
    failure_stage: Optional[str] = None
    needs_retry: bool = False
    model_used: Optional[str] = None


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


def _looks_like_git_diff(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _DIFF_HEADER_RE.search(stripped):
        return True
    return bool(stripped.startswith("--- ") and "\n+++ " in stripped and _DIFF_HUNK_RE.search(stripped))


def _build_review_verdict(
    *,
    verdict: str,
    score: float,
    summary: str,
    issues: list[str],
    feedback: Optional[dict] = None,
    flag_category: Optional[str] = None,
    failure_stage: Optional[str] = None,
    needs_retry: bool = False,
    model_used: Optional[str] = None,
) -> ReviewVerdict:
    details = dict(feedback or {})
    details.setdefault("summary", summary)
    if issues:
        details.setdefault("issues", issues)
    if flag_category:
        details.setdefault("flag_category", flag_category)
    if failure_stage:
        details.setdefault("failure_stage", failure_stage)
    if needs_retry:
        details.setdefault("needs_retry", True)
    return ReviewVerdict(
        verdict=verdict,
        score=score,
        feedback=details,
        issues=issues,
        flag_category=flag_category,
        failure_stage=failure_stage,
        needs_retry=needs_retry,
        model_used=model_used,
    )


def _precheck_review_input(diff: str) -> Optional[ReviewVerdict]:
    stripped = (diff or "").strip()
    if not stripped:
        return _build_review_verdict(
            verdict="SKIP",
            score=0.0,
            summary="변경사항 없음 — 검수 생략",
            issues=[],
            failure_stage="input_validation",
        )

    if _looks_like_git_diff(stripped):
        return None

    for pattern, category, stage, needs_retry, summary in _SUSPICIOUS_INPUT_PATTERNS:
        if pattern.search(stripped):
            return _build_review_verdict(
                verdict="FLAG",
                score=0.0,
                summary=summary,
                issues=[summary, "실제 코드 diff가 없어 LLM 코드 리뷰를 수행하지 않았습니다."],
                flag_category=category,
                failure_stage=stage,
                needs_retry=needs_retry,
            )

    return _build_review_verdict(
        verdict="FLAG",
        score=0.1,
        summary="실제 git diff 형식이 아닌 입력이 리뷰에 전달되었습니다.",
        issues=[
            "리뷰 입력이 `diff --git` 형식이 아니어서 코드 품질 판정을 신뢰할 수 없습니다.",
            "러너 출력과 git diff 수집 단계를 우선 점검해야 합니다.",
        ],
        flag_category="INVALID_REVIEW_INPUT",
        failure_stage="input_validation",
        needs_retry=False,
    )


async def _save_review_result(
    *,
    job_id: str,
    project: str,
    verdict: ReviewVerdict,
    diff_size: int,
    model_used: Optional[str],
    cost: float,
) -> None:
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    """INSERT INTO code_reviews
                       (job_id, project, verdict, score, feedback, diff_size, model_used, cost,
                        flag_category, failure_stage, needs_retry)
                       VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11)""",
                    job_id,
                    project,
                    verdict.verdict,
                    verdict.score,
                    json.dumps(verdict.feedback, ensure_ascii=False),
                    diff_size,
                    model_used,
                    cost,
                    verdict.flag_category,
                    verdict.failure_stage,
                    verdict.needs_retry,
                )
            except Exception as schema_err:
                logger.warning("code_reviewer_db_save_new_schema_failed: %s", schema_err)
                await conn.execute(
                    """INSERT INTO code_reviews
                       (job_id, project, verdict, score, feedback, diff_size, model_used, cost)
                       VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)""",
                    job_id,
                    project,
                    verdict.verdict,
                    verdict.score,
                    json.dumps(verdict.feedback, ensure_ascii=False),
                    diff_size,
                    model_used,
                    cost,
                )
    except Exception as db_err:
        logger.warning("code_reviewer_db_save_error: error=%s", db_err)


async def review_code_diff(
    project: str,
    job_id: str,
    diff: str,
    instruction: str,
    files_changed: Optional[list] = None,
) -> ReviewVerdict:
    """코드 diff를 독립 AI로 리뷰. Claude Haiku 사용."""
    start = time.time()

    precheck = _precheck_review_input(diff)
    if precheck is not None:
        if precheck.verdict != "SKIP":
            await _save_review_result(
                job_id=job_id,
                project=project,
                verdict=precheck,
                diff_size=len(diff or ""),
                model_used="precheck",
                cost=0.0,
            )
        return precheck

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
        from app.core.anthropic_client import call_llm_with_fallback
        review_models = await _get_review_models()
        used_model = review_models[0] if review_models else _REVIEW_MODEL_FALLBACK

        # 모델 목록 순서대로 시도 (CEO 설정 우선순위)
        result_text = None
        for model in review_models:
            try:
                result_text = await call_llm_with_fallback(
                    prompt=prompt,
                    model=model,
                    system=_REVIEW_SYSTEM_PROMPT,
                    max_tokens=1024,
                )
                if result_text:
                    used_model = model
                    break
            except Exception as model_err:
                logger.warning("review_model_failed: model=%s error=%s", model, str(model_err)[:60])
                continue

        if not result_text:
            logger.warning(f"code_reviewer_no_response: job_id={job_id}")
            verdict = _build_review_verdict(
                verdict="FLAG",
                score=0.2,
                summary="리뷰 AI 응답 없음",
                issues=["리뷰 AI가 응답하지 않았습니다."],
                flag_category="REVIEW_MODEL_NO_RESPONSE",
                failure_stage="review_llm",
                needs_retry=True,
                model_used=used_model,
            )
            await _save_review_result(
                job_id=job_id,
                project=project,
                verdict=verdict,
                diff_size=len(diff),
                model_used=used_model,
                cost=0.0,
            )
            return verdict

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

        flag_category = details.get("flag_category")
        failure_stage = details.get("failure_stage")
        needs_retry = bool(details.get("needs_retry", False))
        if verdict == "FLAG" and not flag_category:
            flag_category = "CODE_QUALITY"
        if verdict == "FLAG" and not failure_stage:
            failure_stage = "review_analysis"

        verdict_obj = _build_review_verdict(
            verdict=verdict,
            score=score,
            summary=details.get("summary", "리뷰 완료"),
            issues=details.get("issues", []),
            feedback=details,
            flag_category=flag_category,
            failure_stage=failure_stage,
            needs_retry=needs_retry,
            model_used=used_model,
        )
        await _save_review_result(
            job_id=job_id,
            project=project,
            verdict=verdict_obj,
            diff_size=len(diff),
            model_used=used_model,
            cost=0.01,
        )

        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            f"code_review_complete: job_id={job_id} verdict={verdict} "
            f"score={round(score, 3)} duration_ms={duration_ms}"
        )

        return verdict_obj

    except Exception as e:
        logger.error(f"code_reviewer_error: job_id={job_id} error={e}")
        verdict = _build_review_verdict(
            verdict="FLAG",
            score=0.2,
            summary="리뷰 중 오류 발생",
            issues=[f"리뷰 오류: {str(e)[:200]}"],
            feedback={"error": str(e)},
            flag_category="REVIEW_SYSTEM_FAILURE",
            failure_stage="review_runtime",
            needs_retry=True,
        )
        await _save_review_result(
            job_id=job_id,
            project=project,
            verdict=verdict,
            diff_size=len(diff or ""),
            model_used="review_runtime",
            cost=0.0,
        )
        return verdict
