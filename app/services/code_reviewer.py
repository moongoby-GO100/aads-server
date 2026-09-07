"""
AI-to-AI 피드백 시스템 — Feature 1: Reviewer AI
Pipeline Runner의 코드 diff를 독립 AI(Gemini)가 리뷰.
Developer(Claude Sonnet)와 다른 모델로 에코챔버 방지.
비용: ~$0.01~0.03/리뷰
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_REVIEW_MODEL = "qwen-turbo"
_REVIEW_MODEL_FALLBACK = _REVIEW_MODEL  # DB 조회 실패 시 기본값
_REVIEW_PARSE_MAX_ATTEMPTS = 3  # P0: JSON 파싱 실패 시 즉시 REVIEW_PARSER_FAILURE 대신 재시도 후 폴백

_DIFF_HEADER_RE = re.compile(r"^diff --git a\/.+ b\/.+$", re.MULTILINE)
_DIFF_HUNK_RE = re.compile(r"^@@ .+ @@$", re.MULTILINE)
_PATH_TOKEN_RE = re.compile(
    r"(?:^|[\s`'\"(])"
    r"((?:app|scripts|migrations|docs|tests|src|components|pages|lib|services|api|utils|config|public)/"
    r"[A-Za-z0-9._/\-]+)"
)
_DELETED_SYMBOL_RE = re.compile(
    r"^-\s*((?:async\s+def|def|class)\s+[A-Za-z_][A-Za-z0-9_]*|@router\.[A-Za-z_]+)",
    re.MULTILINE,
)


def _parse_review_json(raw: str) -> Optional[dict]:
    """LLM 리뷰 응답 JSON을 다단계 전략으로 파싱. 실패 시 None.

    Stage 1: ```json fence 제거 후 직접 json.loads
    Stage 2: 첫 { ~ 균형잡힌 } 까지 추출 후 json.loads
    Stage 3: 흔한 오류 자동 정정 (smart quotes / trailing comma / 제어문자)
    """
    if not raw:
        return None

    text = raw.strip()
    # ```json / ``` fence 제거
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl > 0:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    # Stage 1: 직접 시도
    try:
        return json.loads(text)
    except Exception:
        pass

    # Stage 2: 균형 잡힌 첫 JSON 객체만 추출
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    end = -1
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return None
    candidate = text[start:end]
    try:
        return json.loads(candidate)
    except Exception:
        pass

    # Stage 3: 흔한 오류 정정
    fixed = candidate
    fixed = fixed.replace("“", '"').replace("”", '"')
    fixed = fixed.replace("‘", "'").replace("’", "'")
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    fixed = re.sub(r"(?<!\\)\n", "\\\\n", fixed)
    fixed = re.sub(r"(?<!\\)\t", "\\\\t", fixed)
    try:
        return json.loads(fixed)
    except Exception:
        return None


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
    """DB AI_REVIEW 설정을 우선하고 runner/llm 라우팅 순서로 폴백."""
    try:
        from app.core.db_pool import get_pool
        import json as _j
        from app.services.model_registry import filter_executable_models

        def _routing_model(provider: str, model_id: str) -> str:
            provider_name = (provider or "").strip().lower()
            model_name = (model_id or "").strip()
            if not model_name:
                return ""
            if provider_name in {"codex", "openai"} and model_name.startswith("gpt-"):
                return f"codex:{model_name}"
            if provider_name == "anthropic":
                return model_name
            if provider_name in {"gemini", "google", "deepseek", "kimi", "minimax", "qwen", "groq", "openrouter", "litellm"}:
                return f"litellm:{model_name}"
            if ":" in model_name:
                return model_name
            return f"{provider_name}:{model_name}" if provider_name else model_name

        pool = get_pool()
        candidates: list[str] = []
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT models FROM runner_model_config WHERE size = 'AI_REVIEW'"
            )
            route_rows = await conn.fetch(
                """
                SELECT route_key, provider, model_id
                FROM model_routing_preferences
                WHERE route_key IN ('runner_llm', 'llm')
                  AND is_enabled = TRUE
                ORDER BY CASE route_key WHEN 'runner_llm' THEN 0 ELSE 1 END,
                         is_default DESC,
                         display_order ASC,
                         provider ASC,
                         model_id ASC
                """
            )
        if row:
            raw = row["models"]
            if isinstance(raw, str):
                candidates.extend(_j.loads(raw))
            elif isinstance(raw, list):
                candidates.extend(raw)
            else:
                candidates.extend(list(raw) if raw else [])
        candidates.extend(_routing_model(r["provider"], r["model_id"]) for r in route_rows)
        candidates.append(_REVIEW_MODEL_FALLBACK)
        seen: set[str] = set()
        ordered = []
        for model in candidates:
            normalized = str(model or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        filtered = await filter_executable_models(ordered)
        return filtered or [_REVIEW_MODEL_FALLBACK]
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

## 보존 하드 게이트
- 기존 함수/클래스/API 라우터 삭제가 있으면 preservation 0.2 이하, FLAG로 판정
- 삭제 라인이 추가 라인의 50%를 초과하면 preservation 0.3 이하, REQUEST_CHANGES 이상으로 차단
- 지시서에 명시된 파일 경로 밖 변경이 있으면 scope_compliance 0.3 이하로 판정
- 위 항목은 기능이 동작해 보여도 "기존 구현 조사·분류표와 삭제 사유"가 없으면 승인 금지

## 응답 형식 (JSON만, 추가 설명 금지):
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


def _extract_changed_files(diff: str) -> list[str]:
    files: list[str] = []
    for match in re.finditer(r"^diff --git a/(.+?) b/(.+)$", diff or "", re.MULTILINE):
        candidate = match.group(2).strip()
        if candidate and candidate not in files:
            files.append(candidate)
    return files


def _extract_instruction_paths(instruction: str) -> set[str]:
    paths: set[str] = set()
    for match in _PATH_TOKEN_RE.finditer(instruction or ""):
        path = match.group(1).strip().rstrip(".,:;)")
        if path:
            paths.add(path)
    return paths


def _diff_line_counts(diff: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in (diff or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def _precheck_preservation_gate(diff: str, instruction: str, files_changed: Optional[list]) -> Optional[ReviewVerdict]:
    additions, deletions = _diff_line_counts(diff)
    issues: list[str] = []
    feedback: dict[str, object] = {
        "summary": "기존 구현 보존 하드 게이트",
        "additions": additions,
        "deletions": deletions,
    }

    if additions > 0 and deletions > additions * 0.5:
        issues.append(
            f"삭제 라인({deletions})이 추가 라인({additions})의 50%를 초과했습니다. 기존 구현 조사표와 삭제 사유가 필요합니다."
        )
    elif additions == 0 and deletions > 0:
        issues.append(f"추가 없이 삭제 라인({deletions})만 존재합니다. 삭제 사유가 필요합니다.")

    symbol_matches = _DELETED_SYMBOL_RE.findall(diff or "")
    if symbol_matches:
        feedback["deleted_symbols"] = symbol_matches[:20]
        issues.append(
            "삭제된 public 함수/클래스/API 라우터가 감지되었습니다: "
            + ", ".join(symbol_matches[:10])
        )

    allowed_paths = _extract_instruction_paths(instruction)
    changed_paths = [str(path) for path in (files_changed or _extract_changed_files(diff))]
    if allowed_paths and changed_paths:
        out_of_scope = [
            path for path in changed_paths
            if not any(path == allowed or path.startswith(f"{allowed}/") for allowed in allowed_paths)
        ]
        if out_of_scope:
            feedback["allowed_paths"] = sorted(allowed_paths)
            feedback["out_of_scope_files"] = out_of_scope[:20]
            issues.append(
                "지시서에 명시되지 않은 파일 변경이 감지되었습니다: "
                + ", ".join(out_of_scope[:10])
            )

    if not issues:
        return None

    feedback["scope_compliance"] = 0.3
    feedback["preservation"] = 0.2
    feedback["issues"] = issues
    return _build_review_verdict(
        verdict="FLAG" if symbol_matches else "REQUEST_CHANGES",
        score=0.3 if symbol_matches else 0.39,
        summary="기존 구현 보존/범위 하드 게이트 차단",
        issues=issues,
        feedback=feedback,
        flag_category="PRESERVATION_HARD_GATE",
        failure_stage="pre_llm_preservation_gate",
        needs_retry=False,
        model_used="precheck",
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

    preservation_precheck = _precheck_preservation_gate(diff, instruction, files_changed)
    if preservation_precheck is not None:
        await _save_review_result(
            job_id=job_id,
            project=project,
            verdict=preservation_precheck,
            diff_size=len(diff or ""),
            model_used="precheck",
            cost=0.0,
        )
        return preservation_precheck

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

        # P0: 응답 실패(예외/빈 응답)와 JSON 파싱 실패를 하나의 재시도 루프로 묶어
        # 모델 목록을 순환하며 최대 _REVIEW_PARSE_MAX_ATTEMPTS회까지 시도한다.
        # 단일 모델만 설정된 경우(운영 기본값)에도 같은 모델을 재호출한다 —
        # LLM이 가끔 비-JSON 텍스트를 반환해도 첫 실패에 바로 review_hold로
        # 보내지 않기 위함.
        result_text = None
        details = None
        parse_fail_count = 0
        for attempt_no in range(1, _REVIEW_PARSE_MAX_ATTEMPTS + 1):
            model = review_models[(attempt_no - 1) % len(review_models)] if review_models else _REVIEW_MODEL_FALLBACK
            try:
                result_text = await call_llm_with_fallback(
                    prompt=prompt,
                    model=model,
                    system=_REVIEW_SYSTEM_PROMPT,
                    max_tokens=1024,
                )
            except Exception as model_err:
                logger.warning("review_model_failed: model=%s attempt=%s/%s error=%s",
                               model, attempt_no, _REVIEW_PARSE_MAX_ATTEMPTS, str(model_err)[:60])
                result_text = None

            if not result_text:
                continue

            used_model = model
            details = _parse_review_json(result_text)
            if details is not None:
                break

            parse_fail_count += 1
            logger.warning(
                "code_reviewer_json_parse_failed: job_id=%s model=%s attempt=%s/%s preview=%r",
                job_id, model, attempt_no, _REVIEW_PARSE_MAX_ATTEMPTS, (result_text or "")[:200]
            )
            if attempt_no < _REVIEW_PARSE_MAX_ATTEMPTS:
                await asyncio.sleep(2 * attempt_no)

        if not result_text:
            logger.warning(f"code_reviewer_no_response: job_id={job_id}")
            verdict = _build_review_verdict(
                verdict="FLAG",
                score=0.2,
                summary="리뷰 AI 응답 없음 — 승인 보류 필요",
                issues=[
                    "리뷰 AI가 응답하지 않았습니다.",
                    "코드 품질을 검증하지 못했으므로 승인 대기로 넘기면 안 됩니다.",
                ],
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

        # 재시도 루프에서 이미 파싱을 시도했으므로, 모두 실패한 경우에만 여기 도달한다.
        if details is None:
            logger.warning(
                "code_reviewer_json_parse_failed: job_id=%s model=%s attempts=%s preview=%r",
                job_id, used_model, parse_fail_count, (result_text or "")[:200]
            )
            verdict_obj = _build_review_verdict(
                verdict="FLAG",
                score=0.5,
                summary=f"리뷰 응답 파싱 실패 ({parse_fail_count}회 재시도) — 승인 보류 필요",
                issues=[
                    f"LLM 리뷰 응답이 {parse_fail_count}회 연속 유효한 JSON이 아니었습니다.",
                    "코드 품질을 검증하지 못했으므로 승인 대기로 넘기면 안 됩니다.",
                ],
                feedback={
                    "raw_preview": (result_text or "")[:500],
                    "summary": f"리뷰 응답 파싱 실패 ({parse_fail_count}회 재시도) — 승인 보류 필요",
                    "parse_attempts": parse_fail_count,
                },
                flag_category="REVIEW_PARSER_FAILURE",
                failure_stage="review_json_parse",
                needs_retry=True,
                model_used=used_model,
            )
            await _save_review_result(
                job_id=job_id,
                project=project,
                verdict=verdict_obj,
                diff_size=len(diff),
                model_used=used_model,
                cost=0.005,
            )
            return verdict_obj

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
            summary="리뷰 중 오류 발생 — 승인 보류 필요",
            issues=[
                f"리뷰 오류: {str(e)[:200]}",
                "코드 품질을 검증하지 못했으므로 승인 대기로 넘기면 안 됩니다.",
            ],
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
