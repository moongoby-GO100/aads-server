"""
AADS-188C Phase 3 + R-CRITICAL-002: Output Validator — 거짓 보고 방지.

탐지 유형:
  EMPTY_PROMISE          — 행동 없이 "하겠습니다"로 끝나는 응답
  NO_TOOL_FOR_ACTION     — 도구 호출 없이 행동을 약속하는 응답
  TOO_SHORT              — 도구 결과 없이 극단적으로 짧은 응답
  UNVERIFIED_COUNT       — 도구 호출 없이 DB 수치/건수를 보고하는 응답 (차단)
  FABRICATED_RESULTS     — 가짜 도구 결과 XML 태그를 텍스트로 생성한 응답 (차단)
  FABRICATED_DATA_TABLE  — 도구 미호출 상태에서 DB 조회/결과처럼 보이는 마크다운 테이블 생성 (차단)
  INCONSISTENT_DATA      — 응답 내 수치가 동일 턴의 도구 결과와 모순되는 경우 (차단)
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

# ─── 날조 도구 결과 패턴 (XML) ────────────────────────────────────────────────

_FABRICATED_XML_PATTERNS: List[re.Pattern] = [
    re.compile(r'<function_results>', re.IGNORECASE),
    re.compile(r'<invoke\s+name=', re.IGNORECASE),
    re.compile(r'<function_calls>', re.IGNORECASE),
    re.compile(r'<function_response>', re.IGNORECASE),
    re.compile(r'<tool_results>', re.IGNORECASE),
    re.compile(r'<tool_call>', re.IGNORECASE),
    re.compile(r'<tool_response>', re.IGNORECASE),
]

# ─── 날조 데이터 테이블 패턴 (마크다운) ──────────────────────────────────────

# "DB 조회 결과", "실측 확인", "쿼리 결과" 등 키워드 뒤에 마크다운 테이블이 오는 패턴
_DATA_CLAIM_KEYWORDS = re.compile(
    r'(?:DB\s*조회|쿼리\s*결과|실측\s*확인|실측\s*결과|database\s*(?:query|result)|'
    r'query\s*result|SELECT\s+.*?FROM|조회\s*결과|테이블\s*조회|데이터\s*확인)',
    re.IGNORECASE,
)

# 마크다운 테이블 패턴 (헤더행 + 구분행)
_MARKDOWN_TABLE = re.compile(
    r'\|[^\n]+\|\s*\n\s*\|[\s\-:]+\|',
)

# ─── 검증 결과 ─────────────────────────────────────────────────────────────────


@dataclass
class ValidationResult:
    is_valid: bool
    violation_type: str
    message: str
    retry_prompt: str


def validate_response(
    response_text: str,
    tools_called: bool,
    intent: str = "",
    tool_results_text: str = "",
) -> ValidationResult:
    """
    모델 응답을 검증하여 거짓 보고 여부를 판단한다.
    """
    _OK = ValidationResult(is_valid=True, violation_type="", message="", retry_prompt="")

    stripped = response_text.strip()

    # ── FABRICATED_RESULTS: 가짜 XML 태그 (모든 인텐트, 모든 경로에서 항상 검사) ──
    _fab = check_fabricated_results(stripped)
    if _fab:
        logger.error(f"[OutputValidator] FABRICATED_RESULTS detected: {_fab.message}")
        return _fab

    if intent in ("greeting", "casual", ""):
        return _OK

    # 도구가 호출된 응답 — XML 날조는 위에서 이미 검사, 데이터 불일치만 추가 검사
    if tools_called:
        if tool_results_text:
            _incon = check_inconsistent_data(stripped, tool_results_text)
            if _incon:
                logger.error(f"[OutputValidator] INCONSISTENT_DATA detected: {_incon.message}")
                return _incon
        return _OK

    # ── FABRICATED_DATA_TABLE: 도구 미호출인데 DB 조회 결과처럼 보이는 테이블 ──
    _fdt = check_fabricated_data_table(stripped)
    if _fdt:
        logger.error(f"[OutputValidator] FABRICATED_DATA_TABLE detected: {_fdt.message}")
        return _fdt

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

    # ── UNVERIFIED_COUNT: 도구 호출 없이 수치/건수 보고 (차단) ──────────────
    _warn = check_unverified_counts(stripped, tools_called)
    if _warn:
        logger.warning(f"[OutputValidator] {_warn.message}")
        return _warn  # 이제 차단 (is_valid=False)

    return _OK


# ─── 수치 환각 감지 (차단) ────────────────────────────────────────────────────

# DB 건수/수량을 나타내는 패턴: "50건", "120개", "총 30종목", "약 200건" 등
_COUNT_PATTERN = re.compile(
    r'(?:총\s*|약\s*)?(\d{1,6})\s*(?:건|개|행|종목|row|rows|개의|건의|종목의|건이|개가|종목이|명|대|곳|장|EA)',
    re.IGNORECASE,
)


def check_unverified_counts(
    response_text: str,
    tools_called: bool,
) -> Optional[ValidationResult]:
    """
    도구 호출 없이 DB 수치/건수를 보고하는 응답을 탐지한다.
    차단 + 재시도.
    """
    if tools_called:
        return None

    matches = _COUNT_PATTERN.findall(response_text)
    if not matches:
        return None

    # 숫자 1~9 같은 소규모 수치는 일반 대화일 가능성이 높으므로 무시
    significant = [m for m in matches if int(m) >= 10]
    if not significant:
        return None

    return ValidationResult(
        is_valid=False,
        violation_type="UNVERIFIED_COUNT",
        message=(
            f"도구 미호출 상태에서 수치 보고 감지: "
            f"{', '.join(significant)} — 환각 가능성"
        ),
        retry_prompt=(
            "[시스템 재시도 지시 — 미검증 수치 감지] "
            "방금 응답에서 도구를 호출하지 않고 수치(건수/개수)를 보고했습니다. "
            "DB 수치는 반드시 query_database 도구로 실제 조회한 결과만 사용하세요. "
            "추정이나 이전 대화의 수치를 재활용하지 마세요. "
            "지금 즉시 query_database 또는 관련 도구를 호출하여 실측 데이터로 보고하세요."
        ),
    )


# ─── 날조 도구 결과 XML 감지 (차단) ──────────────────────────────────────────


def check_fabricated_results(
    response_text: str,
) -> Optional[ValidationResult]:
    """
    AI가 도구를 호출하지 않고 가짜 <function_results>, <invoke name=...> 등
    XML 태그를 텍스트로 직접 생성한 경우를 탐지한다.
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


# ─── 날조 데이터 테이블 감지 (차단) ──────────────────────────────────────────


def check_fabricated_data_table(
    response_text: str,
) -> Optional[ValidationResult]:
    """
    도구를 호출하지 않고 'DB 조회 결과', '실측 확인' 등의 키워드 뒤에
    마크다운 테이블을 배치하여 마치 실제 데이터인 것처럼 보이게 하는 패턴을 탐지한다.
    """
    # "DB 조회 결과" 류 키워드가 있는지 확인
    has_data_claim = _DATA_CLAIM_KEYWORDS.search(response_text)
    if not has_data_claim:
        return None

    # 마크다운 테이블이 있는지 확인
    has_table = _MARKDOWN_TABLE.search(response_text)
    if not has_table:
        return None

    return ValidationResult(
        is_valid=False,
        violation_type="FABRICATED_DATA_TABLE",
        message=(
            f"날조 데이터 테이블 감지: '{has_data_claim.group(0)}' 키워드 + 마크다운 테이블 — "
            f"도구 호출 없이 DB 결과처럼 보이는 데이터를 생성함"
        ),
        retry_prompt=(
            "[시스템 재시도 지시 — 날조 데이터 테이블 감지] "
            "방금 응답에서 도구를 호출하지 않고 'DB 조회 결과'나 '실측 확인' 등의 표현과 함께 "
            "마크다운 테이블을 작성했습니다. 이는 실제 데이터가 아닌 날조된 내용입니다. "
            "데이터를 보고하려면 반드시 query_database, read_remote_file, task_history 등 "
            "도구를 실제로 호출하고 그 결과를 사용하세요. "
            "도구로 확인할 수 없다면 '현재 실시간 데이터를 확인할 수 없습니다'라고 솔직히 답하세요."
        ),
    )


# ─── 데이터 불일치 감지 (차단) ────────────────────────────────────────────────

# 응답에서 유의미한 숫자를 추출하는 패턴 (소수점 포함, 3자리 이상 또는 단위 동반)
_SIGNIFICANT_NUMBER = re.compile(
    r'(?<!\d)(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?:건|개|행|종목|row|rows|원|%|명|대|곳|장|EA)',
    re.IGNORECASE,
)


def check_inconsistent_data(
    response_text: str,
    tool_results_text: str,
) -> Optional[ValidationResult]:
    """
    응답에 포함된 수치가 동일 턴의 도구 결과와 모순되는지 감지한다.
    도구 결과에 나타나지 않는 유의미한 수치가 응답에 있으면 불일치로 판단.
    """
    if not tool_results_text or not response_text:
        return None

    # 응답에서 수치+단위 추출
    response_numbers = _SIGNIFICANT_NUMBER.findall(response_text)
    if not response_numbers:
        return None

    # 도구 결과 텍스트에서 모든 숫자 추출 (정규화: 콤마 제거)
    tool_numbers_raw = re.findall(r'\d{1,3}(?:,\d{3})*(?:\.\d+)?', tool_results_text)
    tool_numbers_normalized = {n.replace(",", "") for n in tool_numbers_raw}

    # 응답 수치 중 도구 결과에 없는 것 찾기
    mismatched = []
    for num in response_numbers:
        normalized = num.replace(",", "")
        # 작은 숫자(0~9)는 일반 표현일 수 있으므로 무시
        try:
            if float(normalized) < 10:
                continue
        except ValueError:
            continue
        if normalized not in tool_numbers_normalized:
            mismatched.append(num)

    if not mismatched:
        return None

    # 불일치 수치가 3개 이상이면 확실한 모순
    if len(mismatched) >= 3:
        return ValidationResult(
            is_valid=False,
            violation_type="INCONSISTENT_DATA",
            message=(
                f"도구 결과와 불일치하는 수치 감지: {', '.join(mismatched[:5])} — "
                f"도구 결과에 없는 데이터를 응답에 포함"
            ),
            retry_prompt=(
                "[시스템 재시도 지시 — 데이터 불일치 감지] "
                "방금 응답에서 도구 결과와 다른 수치를 보고했습니다. "
                "응답에 포함하는 모든 수치는 반드시 도구 호출 결과에서 직접 인용해야 합니다. "
                "도구 결과를 다시 확인하고, 실제 데이터만 사용하여 정확하게 보고하세요."
            ),
        )

    return None
