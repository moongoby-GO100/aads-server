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
  REPORT_STRUCTURE_WEAK  — 보고/분석 응답이 문제점·원인·권장안·검증기준 없이 빈약한 경우 (재작성)
  PROGRESS_ONLY_RESPONSE — 최종 결과 없이 진행 로그/예고로 끝나는 응답 (차단)
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

_PROGRESS_ONLY_PATTERNS: List[str] = [
    "확인하겠습니다",
    "확인해보겠습니다",
    "실측하겠습니다",
    "실측합니다",
    "조회하겠습니다",
    "점검하겠습니다",
    "분석하겠습니다",
    "파악하겠습니다",
    "조사하겠습니다",
    "살펴보겠습니다",
    "진행하겠습니다",
    "로드하고",
    "병렬 확인",
    "병렬로",
    "먼저 ",
    "이제 ",
]

_PROGRESS_TAIL_RE = re.compile(
    r"(?:"
    r"(?:이제|먼저|다음으로|추가로|바로|곧)?\s*"
    r".{0,80}?"
    r"(?:확인|조회|점검|분석|파악|조사|검토|진행|실행|처리|수정|패치|적용|반영|준비)"
    r"(?:하겠습니다|하겠습니?다|합니다|하겠습니다\.|합니다\.)"
    r")\s*$",
    re.IGNORECASE | re.DOTALL,
)

_COMPLETION_EVIDENCE_PATTERNS: List[str] = [
    "결론",
    "원인:",
    "**원인",
    "근본 원인",
    "확인 결과",
    "실측 결과",
    "조치 완료",
    "반영 완료",
    "수정 완료",
    "검증 완료",
    "커밋 완료",
    "커밋했습니다",
    "푸시 완료",
    "푸시했습니다",
    "배포 완료",
    "배포했습니다",
    "정상화",
    "현재 상태",
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

_SOURCE_TAG_PATTERN = re.compile(
    r'\[(?:DB\s*조회|코드\s*확인|로그|명령|도구|검증|실측|출처|공식문서|미측정)[^\]]*\]',
    re.IGNORECASE,
)

_QUANTIFIED_CLAIM_PATTERN = re.compile(
    r'(?:\d[\d,]*(?:\.\d+)?\s*(?:건|개|행|초|분|시간|일|%|원|달러|GB|MB|줄|회)|'
    r'\b\d{4}-\d{2}-\d{2}\b|KST|커밋|commit|hash)',
    re.IGNORECASE,
)

# ─── 보고서 품질 구조 검사 ───────────────────────────────────────────────────

_REPORT_QUALITY_INTENTS = frozenset({
    "report",
    "audit",
    "deep_research",
    "cto_strategy",
    "url_analyze",
    "knowledge_query",
    "fact_check",
    "research",
    "complex_analysis",
    "analysis",
    "strategy",
    "planning",
    "decision",
    "cto_code_analysis",
    "cto_verify",
    "cto_impact",
    "cto_tech_debt",
    "cost_report",
    "runner_response",
    "status_check",
    "task_query",
    "health_check",
    "diagnosis",
    "debug",
    "error_analysis",
    "code_modify",
    "deploy",
    "pipeline_runner",
    "pipeline",
    "git_ops",
    "execute",
})

_REPORT_REQUIRED_GROUPS: dict[str, tuple[str, ...]] = {
    "summary_or_conclusion": (
        "요약", "결론", "핵심", "현황", "판정",
    ),
    "problem_or_risk": (
        "문제", "문제점", "이슈", "리스크", "위험", "한계", "차단", "주의", "누락",
    ),
    "cause_or_evidence": (
        "원인", "근거", "증거", "확인", "실측", "출처", "코드", "DB", "로그", "검증",
    ),
    "recommendation": (
        "권장", "권장안", "개선", "개선안", "조치", "대안", "추천", "다음 단계",
    ),
    "success_or_validation": (
        "완료기준", "성공 기준", "검증", "테스트", "측정", "확인 방법", "판정",
    ),
}

_REPORT_MIN_STRUCTURE_CHARS = 280
_STATUS_REPORT_MIN_STRUCTURE_CHARS = 180

# ─── 확인형 질문 예외 (짧은 yes/no 응답 허용) ────────────────────────────────

_CONFIRMATION_QUESTION_PATTERNS: list[re.Pattern] = [
    re.compile(r'(?:되는|된|반영되는|적용되는|나오는|들어가는|포함되는|돌아가는)\s*(?:거지|건지|거야|거냐|건가|거잖아)', re.IGNORECASE),
    re.compile(r'(?:맞지|맞나|맞아)\s*\??$'),
    re.compile(r'(?:된거야|된거지|된건가|됐나|됐지|됐어|했어|했나|했지)\s*\??$'),
    re.compile(r'(?:있어|없어|있나|없나|있지|없지)\s*\??$'),
]


def _is_confirmation_question(user_message: str) -> bool:
    """CEO의 단순 확인 질문(~거지?, ~맞지?, ~된거야?)을 감지한다."""
    if not user_message:
        return False
    msg = user_message.strip()
    if len(msg) > 120:
        return False
    for pat in _CONFIRMATION_QUESTION_PATTERNS:
        if pat.search(msg):
            return True
    return False


def _is_structured_next_action_tail(text: str) -> bool:
    """Do not treat a proper report's next-action footer as a progress-only answer."""
    tail = (text or "")[-500:].strip()
    if not (("→ 다음" in tail) or ("→ 권장" in tail) or ("다음 단계" in tail)):
        return False

    lowered = (text or "").lower()
    group_hits = sum(
        1
        for keywords in _REPORT_REQUIRED_GROUPS.values()
        if any(keyword.lower() in lowered for keyword in keywords)
    )
    has_evidence = bool(_MARKDOWN_TABLE.search(text) or _SOURCE_TAG_PATTERN.search(text))
    return group_hits >= 4 and has_evidence


def _looks_progress_only_response(response_text: str, intent: str, *, tools_called: bool = False) -> bool:
    """완료 보고가 아니라 '지금 확인하겠다'는 진행 안내만 있는 응답을 차단한다."""
    normalized_intent = (intent or "").strip()
    if normalized_intent not in _REPORT_QUALITY_INTENTS:
        return False
    if tools_called and normalized_intent in {"status_check", "task_query", "health_check", "execution_verify"}:
        # 짧은 상태 조회는 실제 도구 결과가 있으면 진행형 꼬리말만으로 차단하지 않는다.
        # 보고/러너/실행형 응답은 완료 아닌 버블이 completed로 저장되지 않게 기존 검사를 유지한다.
        tail_is_progress = bool(_PROGRESS_TAIL_RE.search((response_text or "").strip()[-500:].strip()))
        if len((response_text or "").strip()) >= _STATUS_REPORT_MIN_STRUCTURE_CHARS or not tail_is_progress:
            return False
    text = response_text.strip()
    if not text:
        return False
    tail = text[-500:].strip()
    if _PROGRESS_TAIL_RE.search(tail):
        if _is_structured_next_action_tail(text):
            return False
        return True
    if len(text) > 700:
        return False
    progress_hits = sum(1 for pattern in _PROGRESS_ONLY_PATTERNS if pattern in text)
    if progress_hits < 1:
        return False
    if any(pattern in text for pattern in _COMPLETION_EVIDENCE_PATTERNS):
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) <= 4:
        return True
    return progress_hits >= 2 and not _MARKDOWN_TABLE.search(text)

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
    user_message: str = "",
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

    _skip_report_quality = _is_confirmation_question(user_message)

    if _looks_progress_only_response(stripped, intent, tools_called=tools_called):
        return ValidationResult(
            is_valid=False,
            violation_type="PROGRESS_ONLY_RESPONSE",
            message="진행 안내만 있고 질의/조치 결과가 없어 완료 응답으로 저장할 수 없습니다.",
            retry_prompt=(
                "방금 응답은 진행 안내뿐입니다. 지금까지 실제로 확인한 도구/DB/로그/코드 근거를 바탕으로 "
                "원인, 조치 내용, 검증 결과, 남은 리스크를 포함한 최종 완료 보고를 작성하세요. "
                "'확인하겠습니다' 같은 예고 문장으로 끝내지 마세요."
            ),
        )

    # 도구가 호출된 응답 — XML 날조는 위에서 이미 검사, 데이터 불일치만 추가 검사
    if tools_called:
        if tool_results_text:
            _incon = check_inconsistent_data(stripped, tool_results_text)
            if _incon:
                logger.error(f"[OutputValidator] INCONSISTENT_DATA detected: {_incon.message}")
                return _incon
        # Do not turn a real tool-backed status/verification answer into a failed
        # completed bubble just because its report shape is imperfect. Structural
        # quality can be improved by the completion contract/critic path, but the
        # actual tool-backed answer must remain deliverable.
        if intent in {"status_check", "task_query", "health_check", "execution_verify"}:
            return _OK
        if not _skip_report_quality:
            _report_quality = check_report_quality_structure(stripped, intent)
            if _report_quality:
                logger.warning(f"[OutputValidator] REPORT_STRUCTURE_WEAK detected: {_report_quality.message}")
                return _report_quality
        return _OK

    # ── FABRICATED_DATA_TABLE: 도구 미호출인데 DB 조회 결과처럼 보이는 테이블 ──
    _fdt = check_fabricated_data_table(stripped)
    if _fdt:
        logger.error(f"[OutputValidator] FABRICATED_DATA_TABLE detected: {_fdt.message}")
        return _fdt

    # ── REPORT_STRUCTURE_WEAK: 분석/보고 응답에 핵심 섹션이 빠진 경우 ──
    if not _skip_report_quality:
        _report_quality = check_report_quality_structure(stripped, intent)
        if _report_quality:
            logger.warning(f"[OutputValidator] REPORT_STRUCTURE_WEAK detected: {_report_quality.message}")
            return _report_quality

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
    # 설명형 인텐트는 예시 숫자 사용이 자연스러우므로 예외 처리
    _EXPLAIN_INTENTS = {
        "deep_research", "cto_strategy", "architect", "general_knowledge",
        "explain", "education", "concept", "strategy", "analysis",
    }
    if intent not in _EXPLAIN_INTENTS:
        _warn = check_unverified_counts(stripped, tools_called)
        if _warn:
            logger.warning(f"[OutputValidator] {_warn.message}")
            return _warn  # 이제 차단 (is_valid=False)

    return _OK


def check_report_quality_structure(
    response_text: str,
    intent: str = "",
) -> Optional[ValidationResult]:
    """
    보고/분석 응답이 CEO 판단에 필요한 구조를 갖췄는지 검사한다.

    UI 렌더러가 좋아져도 본문 자체가 빈약하면 사용자는 개선을 체감하지 못한다.
    이 검사는 보고형 인텐트에서 문제점, 근거/원인, 권장안, 검증/완료 기준 중
    2개 이상이 누락되면 재작성하도록 막는다.
    """
    normalized_intent = (intent or "").strip()
    if normalized_intent not in _REPORT_QUALITY_INTENTS:
        return None

    text = (response_text or "").strip()
    min_chars = (
        _STATUS_REPORT_MIN_STRUCTURE_CHARS
        if normalized_intent in {"status_check", "task_query", "health_check", "runner_response"}
        else _REPORT_MIN_STRUCTURE_CHARS
    )
    if len(text) < min_chars:
        return ValidationResult(
            is_valid=False,
            violation_type="REPORT_STRUCTURE_WEAK",
            message=(
                f"보고형 인텐트 응답이 너무 짧음: {len(text)}자 — "
                "문제점/원인/권장안/검증기준을 담기 어려움"
            ),
            retry_prompt=_build_report_quality_retry_prompt(
                ["minimum_depth"],
                "응답 분량이 부족합니다.",
            ),
        )

    lowered = text.lower()
    missing = [
        group
        for group, keywords in _REPORT_REQUIRED_GROUPS.items()
        if not any(keyword.lower() in lowered for keyword in keywords)
    ]
    has_table = bool(_MARKDOWN_TABLE.search(text))
    has_next_action = ("→ 다음" in text) or ("→ 권장" in text) or ("다음 단계" in text)
    has_source_tags = bool(_SOURCE_TAG_PATTERN.search(text))
    has_quantified_claim = bool(_QUANTIFIED_CLAIM_PATTERN.search(text))

    structural_gaps = list(missing)
    if not has_table and len(text) >= 500:
        structural_gaps.append("table_or_matrix")
    if not has_next_action:
        structural_gaps.append("next_action")
    if has_quantified_claim and not has_source_tags and len(text) >= 500:
        structural_gaps.append("source_tags")

    if len(structural_gaps) < 2:
        return None

    return ValidationResult(
        is_valid=False,
        violation_type="REPORT_STRUCTURE_WEAK",
        message=(
            "보고서 핵심 구조 누락: "
            + ", ".join(structural_gaps[:6])
        ),
        retry_prompt=_build_report_quality_retry_prompt(
            structural_gaps,
            "문제점·원인·권장안·검증/완료기준 중 필수 항목이 부족합니다.",
        ),
    )


def _build_report_quality_retry_prompt(missing: list[str], reason: str) -> str:
    missing_text = ", ".join(missing[:8]) if missing else "unknown"
    return (
        "[시스템 재시도 지시 — 보고 품질 부족] "
        f"{reason} 누락 항목: {missing_text}. "
        "이전 응답을 CEO 보고서 기준으로 다시 작성하세요. "
        "첫 1~2줄에 결론을 두고, 본문에는 반드시 다음 섹션을 포함하세요: "
        "1) 문제점/리스크, 2) 원인/근거(도구·DB·코드 출처), "
        "3) 개선 권장안(우선순위 포함), 4) 검증 방법/완료기준, "
        "5) → 다음 단계. "
        "수치·날짜·커밋·상태값에는 [DB 조회], [코드 확인], [명령], [로그], [미측정] 같은 출처 태그를 붙이세요. "
        "비교 항목이 3개 이상이면 마크다운 표를 사용하고, 확인하지 못한 값은 미검증으로 표시하세요."
    )


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

    # 불일치 수치가 1개 이상이면 모순으로 판단
    if len(mismatched) >= 1:
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
