"""
AADS-188C Phase 3: Output Validator — 빈 약속 응답 탐지 및 재시도.

"확인하겠습니다" 류의 행동 없는 빈 약속 응답을 탐지하고,
도구 호출을 강제하는 재시도 프롬프트를 생성한다.

탐지 유형:
  EMPTY_PROMISE       — 행동 없이 "하겠습니다"로 끝나는 응답
  NO_TOOL_FOR_ACTION  — 도구 호출 없이 행동을 약속하는 응답
  TOO_SHORT           — 도구 결과 없이 극단적으로 짧은 응답
  UNVERIFIED_COUNT    — 도구 호출 없이 DB 수치/건수를 보고하는 응답 (경고)
  FABRICATED_RESULTS  — 도구를 호출하지 않고 가짜 도구 결과 XML을 텍스트로 생성한 응답
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# ─── 빈 약속 패턴 ─────────────────────────────────────────────────────────────

_EMPTY_PROMISE_PATTERNS: List[str] = [
    "확인하겠습니다",
    "알겠습니다",
    "처리하겠습니다",
    "잠시만요",
    "확인해보겠습니다",
    "살펴보겠습니다",
    "조치하겠습니다",
    "진행하겠습니다",
    "검토하겠습니다",
    "바로 확인",
    "지금 확인",
    "확인해 드리겠습니다",
    "알아보겠습니다",
]

# 행동 약속 동사 — 도구 호출 없이 사용되면 빈 약속
_ACTION_VERBS: List[str] = [
    "확인해", "조회해", "점검해", "분석해", "검색해",
    "살펴보", "파악해", "조사해", "체크해",
]

# ─── 검증 결과 ─────────────────────────────────────────────────────────────────

# ─── 날조 도구 결과 패턴 ──────────────────────────────────────────────────────

_FABRICATED_XML_PATTERNS: List[re.Pattern] = [
    re.compile(r'<function_results>', re.IGNORECASE),
    re.compile(r'<invoke\s+name=', re.IGNORECASE),
    re.compile(r'<function_calls>', re.IGNORECASE),
    re.compile(r'<function_response>', re.IGNORECASE),
    re.compile(r'<tool_results>', re.IGNORECASE),
]


@dataclass
class ValidationResult:
    is_valid: bool
    violation_type: str  # "" | "EMPTY_PROMISE" | "NO_TOOL_FOR_ACTION" | "TOO_SHORT" | "FABRICATED_RESULTS"
    message: str
    retry_prompt: str  # 재시도 시 추가할 사용자 메시지


def validate_response(
    response_text: str,
    tools_called: bool,
    intent: str = "",
) -> ValidationResult:
    """
    모델 응답을 검증하여 빈 약속 여부를 판단한다.

    Args:
        response_text: 모델이 생성한 텍스트 응답
        tools_called: 이 턴에서 도구가 호출되었는지 여부
        intent: 분류된 인텐트 (greeting/casual이면 검증 스킵)

    Returns:
        ValidationResult — is_valid=False면 재시도 필요
    """
    _OK = ValidationResult(is_valid=True, violation_type="", message="", retry_prompt="")

    # greeting/casual은 도구 호출 불필요 (단, 날조 검사는 항상 수행)
    stripped = response_text.strip()

    # ── FABRICATED_RESULTS: 가짜 도구 결과 XML 태그 생성 탐지 ──────────────
    # 도구 호출 여부와 무관하게 항상 검사 — AI가 텍스트로 XML 태그를 생성하면 차단
    _fab = check_fabricated_results(stripped)
    if _fab:
        logger.error(f"[OutputValidator] FABRICATED_RESULTS detected: {_fab.message}")
        return _fab

    if intent in ("greeting", "casual", ""):
        return _OK

    # 도구가 호출된 응답은 유효
    if tools_called:
        return _OK

    # ── EMPTY_PROMISE: 짧은 텍스트 + 빈 약속 패턴 ─────────────────────────
    if len(stripped) < 100:
        for pat in _EMPTY_PROMISE_PATTERNS:
            if pat in stripped:
                return ValidationResult(
                    is_valid=False,
                    violation_type="EMPTY_PROMISE",
                    message=f"빈 약속 탐지: '{pat}' — 도구 호출 없이 약속만 함",
                    retry_prompt=(
                        "[시스템 재시도 지시] 방금 응답은 빈 약속입니다. "
                        "반드시 관련 도구를 호출하여 실제 데이터를 확인한 후 보고하세요. "
                        "도구 호출 없이 '하겠습니다'로 응답하는 것은 금지입니다."
                    ),
                )

    # ── NO_TOOL_FOR_ACTION: 행동 약속 동사가 있지만 도구 미호출 ─────────────
    if len(stripped) < 200:
        for verb in _ACTION_VERBS:
            if verb in stripped and "겠" in stripped:
                return ValidationResult(
                    is_valid=False,
                    violation_type="NO_TOOL_FOR_ACTION",
                    message=f"행동 약속 탐지: '{verb}...겠' — 도구 호출 없음",
                    retry_prompt=(
                        "[시스템 재시도 지시] 행동을 약속했지만 도구를 호출하지 않았습니다. "
                        "즉시 관련 도구(health_check, task_history, check_directive_status, "
                        "query_database, read_remote_file 등)를 호출하세요."
                    ),
                )

    # ── TOO_SHORT: 도구 호출 없이 극단적으로 짧은 응답 ──────────────────────
    if len(stripped) < 30 and intent not in ("greeting", "casual"):
        return ValidationResult(
            is_valid=False,
            violation_type="TOO_SHORT",
            message=f"응답 너무 짧음: {len(stripped)}자 — 도구 호출 없음",
            retry_prompt=(
                "[시스템 재시도 지시] 응답이 너무 짧습니다. "
                "요청에 맞는 도구를 호출하여 충분한 정보를 제공하세요."
            ),
        )

    # ── UNVERIFIED_COUNT: 도구 호출 없이 수치/건수 보고 (경고만, 차단 안 함) ──
    _warn = check_unverified_counts(stripped, tools_called)
    if _warn:
        logger.warning(f"[OutputValidator] {_warn.message}")

    return _OK


# ─── 수치 환각 감지 (경고 전용) ──────────────────────────────────────────────

# DB 건수/수량을 나타내는 패턴: "50건", "120개", "총 30", "약 200건" 등
_COUNT_PATTERN = re.compile(
    r'(?:총\s*|약\s*)?(\d{1,6})\s*(?:건|개|행|row|rows|개의|건의)',
    re.IGNORECASE,
)


def check_unverified_counts(
    response_text: str,
    tools_called: bool,
) -> Optional[ValidationResult]:
    """
    도구 호출 없이 DB 수치/건수를 보고하는 응답을 탐지한다.
    차단하지 않고 경고 로그만 남긴다 (is_valid=True).

    Returns:
        ValidationResult with violation_type="UNVERIFIED_COUNT" if detected, else None
    """
    if tools_called:
        return None

    matches = _COUNT_PATTERN.findall(response_text)
    if not matches:
        return None

    # 숫자 1~2 같은 소규모 수치는 일반 대화일 가능성이 높으므로 무시
    significant = [m for m in matches if int(m) >= 3]
    if not significant:
        return None

    return ValidationResult(
        is_valid=True,  # 경고만, 차단하지 않음
        violation_type="UNVERIFIED_COUNT",
        message=(
            f"도구 미호출 상태에서 수치 보고 감지: "
            f"{', '.join(significant)} — 환각 가능성 경고"
        ),
        retry_prompt="",  # 차단하지 않으므로 재시도 프롬프트 없음
    )


# ─── 날조 도구 결과 감지 (차단) ───────────────────────────────────────────────


def check_fabricated_results(
    response_text: str,
) -> Optional[ValidationResult]:
    """
    AI가 도구를 호출하지 않고 가짜 <function_results>, <invoke name=...> 등
    XML 태그를 텍스트로 직접 생성한 경우를 탐지한다.
    이는 CEO에 대한 거짓 보고이므로 차단 + 재시도한다.

    Returns:
        ValidationResult with violation_type="FABRICATED_RESULTS" if detected, else None
    """
    for pattern in _FABRICATED_XML_PATTERNS:
        match = pattern.search(response_text)
        if match:
            tag = match.group(0)
            return ValidationResult(
                is_valid=False,
                violation_type="FABRICATED_RESULTS",
                message=(
                    f"날조된 도구 결과 태그 감지: '{tag}' — "
                    f"AI가 도구를 호출하지 않고 가짜 결과를 텍스트로 생성함"
                ),
                retry_prompt=(
                    "[시스템 재시도 지시 — 거짓 보고 감지] "
                    "방금 응답에서 <function_results>, <invoke> 등의 XML 태그를 텍스트로 직접 작성했습니다. "
                    "이것은 도구를 실제로 호출한 것이 아니라 가짜 결과를 날조한 것입니다. "
                    "절대로 도구 결과 XML 태그를 텍스트로 생성하지 마세요. "
                    "작업 상태를 확인하려면 check_directive_status, task_history, query_database 등 "
                    "실제 도구를 호출하세요. 도구 없이 확인할 수 없다면 솔직히 '현재 확인할 수 없습니다'라고 답하세요."
                ),
            )

    return None
