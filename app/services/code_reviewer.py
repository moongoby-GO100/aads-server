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
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_REVIEW_MODEL = "qwen-turbo"
_REVIEW_MODEL_FALLBACK = _REVIEW_MODEL  # DB 조회 실패 시 기본값
_REVIEW_OAUTH_FALLBACK_MODEL = "claude-haiku-4-5-20251001"  # gitleaks:allow
_REVIEW_LITELLM_FALLBACK_MODEL = "litellm:gemini-2.5-flash-lite"
_REVIEW_PARSE_MAX_ATTEMPTS = 3  # P0: JSON 파싱 실패 시 즉시 REVIEW_PARSER_FAILURE 대신 재시도 후 폴백
# DB에 여러 독립 리뷰 모델이 등록되어 있으면 앞쪽 모델 장애만으로 뒤쪽의 정상
# 모델을 영구히 건너뛰지 않는다. 다만 잘못된 설정이 요청 시간을 무한히 늘리지
# 않도록 전체 모델 시도 수도 별도 상한으로 제한한다.
_REVIEW_MODEL_MAX_ATTEMPTS = int(os.environ.get("REVIEW_MODEL_MAX_ATTEMPTS", "6"))
# P0: 리뷰 LLM 시도 1회 상한(초). 초과하면 무응답으로 간주하고 다음 시도로 넘긴다.
#
# 2026-09-15 실측 — runner-2a202a8e 의 실제 리뷰 프롬프트(10,602자)로 재현했더니
# claude-opus-5 20.0초, claude-haiku-4-5 25.5초였다. 상한 25초는 유휴 상태에서도
# 이미 경계에 걸려 있었고, 릴레이에 claude 세션이 8~10개 쌓인 구간에서는 등록된
# 네 모델이 전부 25초에 잘려 REVIEW_MODEL_NO_RESPONSE 가 됐다. GO100 5건이 그렇게
# 최대 200분 review_hold 에 묶였다.
#
# 상한을 줄일 때는 반드시 실제 리뷰 프롬프트로 지연을 먼저 재라. 프록시 한도에서
# 거꾸로 계산해 내려잡으면 "무응답" 이 아닌 것을 무응답으로 만든다.
_REVIEW_LLM_TIMEOUT_SEC = int(os.environ.get("REVIEW_LLM_TIMEOUT_SEC", "45"))
# 마감까지 남은 시간이 이보다 짧으면 새 시도를 걸지 않는다. 남은 시간이 상한보다
# 짧아도 이 값보다 길면 남은 만큼이라도 써서 시도한다 — 예산을 버리지 않는다.
_REVIEW_MIN_ATTEMPT_SEC = int(os.environ.get("REVIEW_MIN_ATTEMPT_SEC", "12"))
# 검수 전체에 마감을 둔다.
#
# 러너는 공개 URL(Cloudflare)로 이 API 를 부르고, Cloudflare 는 약 100초에
# 원본 응답을 포기하고 524 를 돌려준다. 그런데 서버는 최대
# 6회 x 45초 = 270초를 쓸 수 있었다. 앞 두 모델이 응답하지 않으면 뒤에
# 멀쩡한 모델이 있어도 러너는 언제나 524 를 받는다.
# 2026-09-13 실측 — GO100 반려 13건 중 8건이 이 경로였다.
#   verdict=FLAG score=0.0 http=524 attempts=3/3 category=REVIEW_API_UNAVAILABLE
# 당시 Codex 계정은 주간 98% + 크레딧 0 이라 응답이 올 수 없었다.
#
# 마감 안에 답이 없으면 "응답 없음"으로 정직하게 닫는다. 프록시에
# 잘려 사유를 잃는 것보다 낫다.
_REVIEW_TOTAL_DEADLINE_SEC = int(os.environ.get("REVIEW_TOTAL_DEADLINE_SEC", "85"))
# 비동기 요청 경로(POST /api/v1/review/code-diff/requests)는 202 로 즉시 반환하고
# 클라이언트가 request_id 를 폴링한다. 프록시 마감에 묶이지 않으므로 같은 85초를
# 쓸 이유가 없다. 재검수 스위퍼가 이 경로를 쓴다 — 동기 경로에서 상한에 걸린
# 작업이 재검수에서도 똑같이 걸리면 복구 경로가 아무 의미가 없다.
_REVIEW_ASYNC_DEADLINE_SEC = int(os.environ.get("REVIEW_ASYNC_DEADLINE_SEC", "240"))
# 리뷰 프롬프트에 넣는 diff 상한(문자수). 리뷰 모델은 200K 컨텍스트인데 종전
# 10KB 하드코딩은 그 0.5%도 쓰지 않았다. 실측(2026-09-17): runner 리뷰 287건 중
# 196건(68%)이 절단된 채 심사됐고 승인율이 41.8% → 22.4% 로 떨어졌다. 반려 사유는
# 코드 결함이 아니라 "diff 가 잘려 확인 불가"였다 (AADS-REVIEWER-DIFF-TRUNCATION-P0).
REVIEW_DIFF_MAX_CHARS = int(os.environ.get("REVIEW_DIFF_MAX_CHARS", "200000"))

_DIFF_HEADER_RE = re.compile(r"^diff --git a\/.+ b\/.+$", re.MULTILINE)
_DIFF_HUNK_RE = re.compile(r"^@@ .+ @@$", re.MULTILINE)
_PATH_TOKEN_RE = re.compile(
    r"(?:^|[\s`'\"(])"
    r"((?:app|scripts|migrations|docs|tests|src|components|pages|lib|services|api|utils|config|public)/"
    r"[A-Za-z0-9._/\-]+)"
)
_SCOPE_DECLARATION_RE = re.compile(
    r"^\s*(?:[-*]\s*)?"
    r"(?:exact\s+authorized\s+(?:files?|paths?)|authorized\s+(?:files?|paths?)|"
    r"allowed(?:\s+(?:files?|paths?|scope))?|file\s+scope|path\s+scope|"
    r"허용\s*(?:파일|경로|범위)|수정\s*허용\s*(?:파일|경로))"
    r"\s*:\s*(.*)$",
    re.IGNORECASE,
)
_SCOPE_PATH_RE = re.compile(
    r"(?:^|[\s,`'\"(])"
    r"((?:/?[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|"
    r"[A-Za-z0-9_.-]+\.(?:py|pyi|js|jsx|ts|tsx|sql|md|json|ya?ml|toml|sh|html|css))"
)
_EXCLUDED_REVIEW_MODELS_RE = re.compile(
    r"^\s*\[REVIEW_EXCLUDE_MODELS:\s*([^\]]+)\]\s*$", re.MULTILINE
)
# 삭제/추가 비율 게이트가 허용하는 순삭제(삭제-추가) 줄 수.
# 2026-09-16: 1추가/1삭제짜리 외과적 핫픽스가 `1 > 0.5` 로 매번 차단돼
# GO100 P0 러너 3건(runner-ae30c8d2 / runner-8c04cd98 / 그 외)이 연속 실패했다.
# 제자리 수정(추가 == 삭제)은 기존 구현을 지운 것이 아니므로 비율만으로 막으면
# 안 된다. 실제로 줄이 사라지는 diff(순삭제 > 0)만 게이트에 걸린다.
# public 심볼 삭제는 아래 `_removed_preservation_symbols` 가 별도로 계속 차단하고,
# 추가 0 / 삭제 N 인 순수 삭제 diff 도 아래 elif 가 그대로 차단한다.
_PRESERVATION_NET_REMOVAL_TOLERANCE = 0
# 순삭제 기준만으로는 1추가/2삭제처럼 줄을 합치는 외과적 수정이 아직 걸린다
# (2026-09-16 실측: chat-direct-7fc58510 은 18추가/19삭제, 순삭제 1줄로 차단됐다).
# 아래 두 조건을 모두 만족하는 아주 작은 diff 만 비율 게이트에서 면제한다.
# 면제돼도 public 심볼 삭제 게이트·범위 게이트·본 LLM 리뷰는 그대로 돈다.
_PRESERVATION_SMALL_DIFF_MAX_LINES = 10
_PRESERVATION_SMALL_DIFF_NET_REMOVAL = 2

_DELETED_SYMBOL_RE = re.compile(
    r"^-[ \t]*((?:async[ \t]+def|def|class)[ \t]+[A-Za-z_][A-Za-z0-9_]*|@router\.[A-Za-z_]+)",
    re.MULTILINE,
)
# 테스트 함수 **리네임**을 삭제로 오판하던 것을 막는다(runner-27087189, FLAG 0.30 —
# def test_card310_is_unchanged_by_card119_global_buy_evaluator 하나로 326추가/20삭제
# diff 전체가 차단됐다). 테스트 파일의 test_* 함수는 외부에서 이름으로 호출하는
# 계약이 아니므로, 같은 파일 diff 안에서 새 테스트 함수가 생겼다면 삭제가 아니라 리네임이다.
# 운영 코드의 public 심볼 리네임은 호출부를 깨는 실제 계약 변경이라 계속 게이트에 남긴다.
_TEST_FILE_PATH_RE = re.compile(r"(?:^|/)(?:tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")
_TEST_FUNCTION_RE = re.compile(r"(?:async[ \t]+def|def)[ \t]+test_[A-Za-z0-9_]*")
_DIFF_HEADER_PATH_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)")


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
                WHERE route_key = 'runner_llm'
                  AND is_enabled = TRUE
                ORDER BY is_default DESC,
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


def _review_attempt_models(models: list[str], instruction: str) -> list[str]:
    """Build a distinct failover chain and honor job-scoped sweeper exclusions."""
    excluded: set[str] = set()
    for match in _EXCLUDED_REVIEW_MODELS_RE.finditer(instruction or ""):
        excluded.update(part.strip() for part in match.group(1).split(",") if part.strip())

    candidates = [
        *models,
        _REVIEW_OAUTH_FALLBACK_MODEL,
        _REVIEW_LITELLM_FALLBACK_MODEL,
    ]
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if not normalized or normalized in seen or normalized in excluded:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


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


async def _call_review_model(
    *,
    model: str,
    prompt: str,
    system: str,
    max_tokens: int,
) -> Optional[str]:
    """Route CLI model IDs to their real relay and API IDs to the central client.

    ``call_llm_with_fallback`` treats every non-Claude name as a LiteLLM model.
    Sending ``codex:*`` there therefore produces HTTP 400 instead of reaching
    the Codex CLI relay. Claude CLI IDs also need the relay so its DB-priority
    OAuth slot selection and refresh-capable credentials are honored.
    """
    normalized = str(model or "").strip()
    provider, separator, bare_model = normalized.partition(":")
    provider = provider.lower() if separator else ""
    if provider in {"codex", "claude"} or (
        not provider
        and normalized != _REVIEW_OAUTH_FALLBACK_MODEL
        and (normalized.startswith("gpt-") or normalized.startswith("claude-"))
    ):
        from app.services.directive_draft_service import _call_configured_model

        return await _call_configured_model(
            model_candidate=normalized,
            prompt=prompt,
            max_tokens=max_tokens,
            system=system,
            tenant_id=None,
            user_id=None,
        )

    from app.core.anthropic_client import call_llm_with_fallback

    return await call_llm_with_fallback(
        prompt=prompt,
        model=bare_model if provider == "litellm" else normalized,
        system=system,
        max_tokens=max_tokens,
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


def _normalize_scope_path(path: str) -> str:
    """Convert an explicitly authorized path to the repo-relative form used by git."""
    normalized = path.strip().rstrip(".,:;)").lstrip("./")
    for repo_marker in (
        "aads-server/",
        "aads-dashboard/",
        "kis-autotrade-v4/",
        "shortflow/",
        "newtalk-v2/",
        "webapp/",
    ):
        if repo_marker in normalized:
            normalized = normalized.split(repo_marker, 1)[1]
            break
    return normalized


def _extract_explicit_scope_paths(instruction: str) -> set[str]:
    """Return paths only from a labelled file/path allowlist declaration."""
    paths: set[str] = set()
    in_scope_block = False

    for line in (instruction or "").splitlines():
        declaration = _SCOPE_DECLARATION_RE.match(line)
        if declaration:
            in_scope_block = True
            candidate_text = declaration.group(1)
        elif in_scope_block:
            candidate_text = line.strip()
            if not candidate_text:
                in_scope_block = False
                continue
        else:
            continue

        matches = list(_SCOPE_PATH_RE.finditer(candidate_text))
        if in_scope_block and not declaration and not matches:
            in_scope_block = False
            continue
        for match in matches:
            normalized = _normalize_scope_path(match.group(1))
            if normalized:
                paths.add(normalized)

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


def _diff_stat_summary(diff: str) -> str:
    """diff --git 헤더 기준 파일별 +/- 라인 수 요약을 만든다.

    호출부가 `git diff --stat` 을 넘겨주지 않으므로, 절단이 필요한 큰 diff 에서도
    리뷰어가 전체 변경 규모를 파악할 수 있게 diff 텍스트에서 직접 집계한다.
    """
    diff = diff or ""
    headers = list(_DIFF_HEADER_RE.finditer(diff))
    if not headers:
        additions, deletions = _diff_line_counts(diff)
        return f"전체 변경: +{additions} -{deletions}"

    lines = ["파일별 변경 요약 (diff --stat 대체):"]
    for idx, match in enumerate(headers):
        section_start = match.end()
        section_end = headers[idx + 1].start() if idx + 1 < len(headers) else len(diff)
        additions, deletions = _diff_line_counts(diff[section_start:section_end])
        header_match = re.match(r"^diff --git a/(.+?) b/(.+)$", match.group(0))
        path = header_match.group(2).strip() if header_match else "?"
        lines.append(f"  {path} | +{additions} -{deletions}")
    return "\n".join(lines)


def _truncate_diff_for_review(diff: str) -> tuple[str, bool]:
    """diff 를 리뷰 프롬프트 상한(REVIEW_DIFF_MAX_CHARS) 안으로 다듬는다.

    앞부분만 남기면 뒤쪽 변경이 통째로 보이지 않아 리뷰어가 "잘려서 확인 불가"를
    근거로 반려한다. 파일별 stat 요약을 앞에 붙이고, 본문은 앞 60% + 뒤 40% 를
    남겨 중간만 생략한다.
    """
    diff = diff or ""
    if len(diff) <= REVIEW_DIFF_MAX_CHARS:
        return diff, False

    stat_summary = _diff_stat_summary(diff)
    head_len = int(REVIEW_DIFF_MAX_CHARS * 0.6)
    tail_len = REVIEW_DIFF_MAX_CHARS - head_len
    head = diff[:head_len]
    tail = diff[-tail_len:] if tail_len > 0 else ""
    omitted = len(diff) - head_len - tail_len
    marker = (
        f"\n... [diff 중간 {omitted}자 생략 — 전체 {len(diff)}자 중 "
        f"{head_len + tail_len}자 표시. 생략 구간은 '확인 불가'이지 '결함'이 아니다] ...\n"
    )
    return f"{stat_summary}\n\n{head}{marker}{tail}", True


def _removed_preservation_symbols(diff: str) -> list[str]:
    """Keep hard gates for removals, without calling private edits deletions.

    Match a declaration only when it occurs exactly once on each side of the
    same file diff. This covers signature edits and indentation moves, for
    public and private declarations alike; it does not certify behavior, which
    still goes through the normal review. Renames, routes whose path is not in
    the symbol, and ambiguous duplicate names remain gated.
    """
    removed: list[str] = []
    for file_diff in re.split(r"(?=^diff --git )", diff or "", flags=re.MULTILINE):
        deletions = _DELETED_SYMBOL_RE.findall(file_diff)
        # Only additions may cancel a removed declaration.
        additions = _DELETED_SYMBOL_RE.findall(
            "\n".join(
                "-" + line[1:] for line in file_diff.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            )
        )
        deleted_counts, added_counts = Counter(deletions), Counter(additions)
        header = _DIFF_HEADER_PATH_RE.match(file_diff)
        in_test_file = bool(header and _TEST_FILE_PATH_RE.search(header.group(2)))
        # 같은 파일 diff 에서 새로 생긴 테스트 함수 = 리네임 짝 후보.
        rename_pool: Counter = Counter()
        if in_test_file:
            for symbol, count in added_counts.items():
                surplus = count - deleted_counts[symbol]
                if surplus > 0 and _TEST_FUNCTION_RE.fullmatch(symbol):
                    rename_pool[symbol] = surplus
        for symbol in deletions:
            # 같은 파일 diff 에서 같은 선언이 정확히 한 번 사라지고 한 번 다시
            # 생겼으면 그것은 삭제가 아니라 시그니처 재작성·들여쓰기 이동이다.
            # 종전에는 이 면제를 private 함수(_foo)로만 한정했고, 그래서 공개
            # 함수의 여러 줄 시그니처 전환이 통째로 "삭제"로 잡혔다.
            # 2026-09-18 실측 — runner-3eeda0e6 / runner-2872d735 두 건이
            #   -async def create_task(req: CreateTaskRequest):
            #   +async def create_task(
            # 이 한 쌍 때문에 302추가/6삭제 diff 전체를 FLAG 0.3 으로 잃었다.
            # 진짜 삭제(+ 쪽 없음)와 리네임(+ 쪽 이름 다름)은 개수가 맞지 않아
            # 그대로 게이트에 남는다. 경로가 심볼 문자열에 담기지 않는
            # @router.* 는 이름만으로 동일성을 판정할 수 없으므로 면제하지 않는다.
            declaration = re.fullmatch(
                r"(?:async[ \t]+def|def|class)[ \t]+[A-Za-z_][A-Za-z0-9_]*", symbol
            )
            preserved = (
                file_diff.startswith("diff --git ")
                and declaration
                and deleted_counts[symbol] == added_counts[symbol] == 1
            )
            if not preserved and in_test_file and _TEST_FUNCTION_RE.fullmatch(symbol):
                # 사라진 테스트 함수 이름을 같은 파일에서 다른 이름으로 다시 추가했으면
                # 삭제가 아니라 리네임이다. 짝이 없으면(순수 삭제) 그대로 게이트에 남는다.
                pair = next((name for name, left in rename_pool.items() if left > 0), None)
                if pair is not None:
                    rename_pool[pair] -= 1
                    preserved = True
            if not preserved:
                removed.append(symbol)
    return removed


def _precheck_preservation_gate(diff: str, instruction: str, files_changed: Optional[list]) -> Optional[ReviewVerdict]:
    additions, deletions = _diff_line_counts(diff)
    issues: list[str] = []
    feedback: dict[str, object] = {
        "summary": "기존 구현 보존 하드 게이트",
        "additions": additions,
        "deletions": deletions,
    }

    net_removal = deletions - additions
    # 아주 작은 diff 는 비율만으로 판정할 근거가 못 된다(하한 임계치).
    small_surgical_diff = (
        additions + deletions <= _PRESERVATION_SMALL_DIFF_MAX_LINES
        and net_removal <= _PRESERVATION_SMALL_DIFF_NET_REMOVAL
    )

    if (
        additions > 0
        and deletions > additions * 0.5
        and net_removal > _PRESERVATION_NET_REMOVAL_TOLERANCE
        and not small_surgical_diff
    ):
        issues.append(
            f"삭제 라인({deletions})이 추가 라인({additions})의 50%를 초과하고 순삭제가 "
            f"{deletions - additions}줄입니다. 기존 구현 조사표와 삭제 사유가 필요합니다."
        )
    elif additions == 0 and deletions > 0:
        issues.append(f"추가 없이 삭제 라인({deletions})만 존재합니다. 삭제 사유가 필요합니다.")

    symbol_matches = _removed_preservation_symbols(diff)
    if symbol_matches:
        feedback["deleted_symbols"] = symbol_matches[:20]
        issues.append(
            "삭제된 public 함수/클래스/API 라우터가 감지되었습니다: "
            + ", ".join(symbol_matches[:10])
        )

    allowed_paths = _extract_explicit_scope_paths(instruction)
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
    deadline_sec: Optional[int] = None,
) -> ReviewVerdict:
    """코드 diff를 독립 AI로 리뷰. Claude Haiku 사용.

    deadline_sec 을 주면 전체 검수 마감을 그 값으로 바꾼다. 프록시에 묶이지 않는
    비동기 요청 경로가 더 긴 마감을 쓰기 위한 것이다(_REVIEW_ASYNC_DEADLINE_SEC).
    """
    start = time.time()
    total_deadline = int(deadline_sec or _REVIEW_TOTAL_DEADLINE_SEC)

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

    # diff 크기 제한 — _truncate_diff_for_review() 가 REVIEW_DIFF_MAX_CHARS 기준으로
    # stat 요약 + 앞 60% + 뒤 40% 패턴으로 다듬는다.
    truncated_diff, was_truncated = _truncate_diff_for_review(diff)
    truncation_notice = (
        "\n[절단 고지] 위 diff 는 길이 제한으로 일부가 생략됐다.\n"
        "- 생략 구간을 근거로 REQUEST_CHANGES 나 PRESERVATION_HARD_GATE 를 내지 마라.\n"
        "- 보이는 코드의 실제 결함만 판정하라.\n"
        "- 생략 구간 확인이 꼭 필요하면 verdict=FLAG, needs_retry=true 로 내고"
        " issues 에 확인이 필요한 파일/함수명을 구체적으로 적어라.\n"
    ) if was_truncated else ""

    prompt = f"""다음 코드 변경사항을 리뷰하세요.

프로젝트: {project}
작업 지시: {instruction[:500]}
변경 파일: {', '.join(files_changed or [])}
{truncation_notice}
```diff
{truncated_diff}
```

위 기준에 따라 JSON으로 판정하세요."""

    try:
        configured_models = await _get_review_models()
        review_models = _review_attempt_models(configured_models, instruction)
        used_model = review_models[0] if review_models else _REVIEW_MODEL_FALLBACK

        # 응답 실패와 JSON 파싱 실패는 같은 모델을 다시 부르지 않고 다음 모델로
        # 넘긴다. 끝의 두 후보는 중앙 Anthropic OAuth 1→2 체인과 Gemini LiteLLM
        # 순서를 고정해 단일 DB 모델 설정에서도 실제 폴백이 일어나게 한다.
        result_text = None
        details = None
        parse_fail_count = 0
        attempt_limit = min(len(review_models), _REVIEW_MODEL_MAX_ATTEMPTS)
        _review_started_at = time.monotonic()
        for attempt_no in range(1, attempt_limit + 1):
            _elapsed = time.monotonic() - _review_started_at
            _remaining = total_deadline - _elapsed
            # 남은 예산이 의미 있는 시도조차 못 할 만큼 짧을 때만 멈춘다.
            #
            # 예전에는 "남은 시간 < 시도당 상한" 이면 바로 break 했다. 상한이 25초,
            # 마감이 85초일 때 첫 시도가 실패하면 60초가 남아도 두 번째 모델을
            # 부르지 않고 끝나는 구간이 생겼다. 남은 만큼이라도 쓰는 편이 낫다.
            if _remaining < _REVIEW_MIN_ATTEMPT_SEC:
                logger.warning(
                    "review_deadline_reached: job_id=%s elapsed=%.0fs attempts=%s/%s deadline=%ss",
                    job_id, _elapsed, attempt_no - 1, attempt_limit, total_deadline,
                )
                break
            _attempt_timeout = min(float(_REVIEW_LLM_TIMEOUT_SEC), _remaining)
            model = review_models[attempt_no - 1]
            # 빈 응답이어도 마지막으로 실제 호출한 모델을 기록해야 스위퍼가
            # 다음 재검수에서 정확한 실패 모델을 제외할 수 있다.
            used_model = model
            try:
                # P0: 리뷰 모델이 실패하면 call_llm_with_fallback 이 Claude 429 재시도(최대 60회)와
                # LiteLLM 폴백 체인을 순회하며 수 분간 반환되지 않는 경우가 있다. 그동안 러너의
                # ai_review 요청과 재검수 스위퍼가 함께 묶여 review_hold 가 누적됐다.
                # 시도당 상한을 두고 다음 모델/재시도로 넘긴다.
                result_text = await asyncio.wait_for(
                    _call_review_model(
                        prompt=prompt,
                        model=model,
                        system=_REVIEW_SYSTEM_PROMPT,
                        max_tokens=1024,
                    ),
                    timeout=_attempt_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("review_model_timeout: model=%s attempt=%s/%s limit=%.0fs",
                               model, attempt_no, attempt_limit, _attempt_timeout)
                result_text = None
            except Exception as model_err:
                logger.warning("review_model_failed: model=%s attempt=%s/%s error=%s",
                               model, attempt_no, attempt_limit, str(model_err)[:60])
                result_text = None

            if not result_text:
                continue

            details = _parse_review_json(result_text)
            if details is not None:
                break

            parse_fail_count += 1
            logger.warning(
                "code_reviewer_json_parse_failed: job_id=%s model=%s attempt=%s/%s preview=%r",
                job_id, model, attempt_no, attempt_limit, (result_text or "")[:200]
            )
            if attempt_no < attempt_limit:
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
