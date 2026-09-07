"""Safe, deterministic portal-auth challenge classification.

This module intentionally does not solve CAPTCHA, read OTPs, or bypass a
portal defense. It only classifies observed portal metadata and creates a
non-secret resume reference for the existing browser session.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Protocol

ALLOWED_STATES = frozenset(
    {
        "captcha_required",
        "otp_required",
        "login_required",
        "portal_error",
        "session_expired",
        "collectable_page",
        "transaction_table",
        "no_records",
        "parse_failed",
        "certificate_password_required",
        "identity_check_required",
        "unknown",
    }
)
ALLOWED_ACTIONS = frozenset(
    {
        "no_action",
        "focus_password_manager",
        "focus_operator_input",
        "wait_for_push_approval",
        "select_certificate_profile",
        "parse_table",
        "retry_with_same_session",
        "refresh_and_retry",
        "operator_approval_required",
        "blocked_by_policy",
    }
)
MIN_PROVIDER_CONFIDENCE = 0.55


class OptionalStateProvider(Protocol):
    def classify(self, *, url: str, text: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ChallengeDecision:
    state: str
    reason_code: str
    evidence: tuple[str, ...] = ()
    confidence: float = 1.0
    suggested_action: str = "no_action"
    requires_operator: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason_code": self.reason_code,
            "evidence": list(self.evidence),
            "confidence": round(float(self.confidence), 3),
            "suggested_action": self.suggested_action,
            "requires_operator": bool(self.requires_operator),
        }


_CAPTCHA = ("captcha", "캡차", "보안문자", "자동입력방지", "숫자를 입력")
_OTP = ("otp", "인증번호", "일회용 비밀번호", "2차 인증", "추가 인증", "휴대폰 인증", "기기 인증")
_LOGIN = ("로그인해주세요", "로그인이 필요", "사장님 로그인", "login")
_ERROR = ("access denied", "forbidden", "올바르지 않은 요청", "보안 위배 접근 제한", "오류가 발생")
_SESSION_EXPIRED = (
    "일정시간 이상 서비스 이용 정보가 없습니다",
    "새로고침 후 이용",
    "session expired",
    "세션이 만료",
)
_CERTIFICATE_PASSWORD = ("인증서 비밀번호", "공동인증서 암호", "금융인증서 비밀번호", "cert password")
_IDENTITY = ("본인인증", "휴대폰 본인확인", "실명확인", "추가 본인확인")
_NO_RECORDS = ("조회된 거래내역이 없습니다", "거래내역이 없습니다", "no data", "no records")
_TRANSACTION_TABLE = ("거래일자", "입금금액", "출금금액", "거래후잔액", "transaction date")
_PARSE_FAILED = ("parse_failed", "날짜 컬럼 없음", "테이블을 인식하지 못했습니다")


def _decision_for(state: str, reason_code: str, evidence: tuple[str, ...]) -> ChallengeDecision:
    action_by_state = {
        "captcha_required": "focus_operator_input",
        "otp_required": "focus_operator_input",
        "login_required": "focus_password_manager",
        "certificate_password_required": "focus_password_manager",
        "identity_check_required": "wait_for_push_approval",
        "session_expired": "refresh_and_retry",
        "transaction_table": "parse_table",
        "no_records": "no_action",
        "portal_error": "operator_approval_required",
    }
    operator_states = {"captcha_required", "otp_required", "identity_check_required", "portal_error"}
    return ChallengeDecision(
        state,
        reason_code,
        evidence,
        suggested_action=action_by_state.get(state, "no_action"),
        requires_operator=state in operator_states,
    )


def _hits(value: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    lowered = str(value or "").lower()
    return tuple(term for term in terms if term.lower() in lowered)


def classify_portal_state(
    url: str = "",
    text: str = "",
    *,
    provider: OptionalStateProvider | None = None,
) -> ChallengeDecision:
    """Classify portal state; provider output is schema- and allowlist-checked."""
    if provider is not None:
        try:
            output = provider.classify(url=url, text=text)
            if isinstance(output, dict) and output.get("state") in ALLOWED_STATES:
                state = str(output["state"])
                return validate_provider_output(output)
        except Exception:
            pass
    combined = f"{url}\n{text}"
    for state, terms, code in (
        ("portal_error", _ERROR, "PORTAL_ERROR"),
        ("session_expired", _SESSION_EXPIRED, "SESSION_EXPIRED"),
        ("captcha_required", _CAPTCHA, "CAPTCHA_REQUIRED"),
        ("otp_required", _OTP, "OTP_REQUIRED"),
        ("certificate_password_required", _CERTIFICATE_PASSWORD, "CERTIFICATE_PASSWORD_REQUIRED"),
        ("identity_check_required", _IDENTITY, "IDENTITY_CHECK_REQUIRED"),
        ("login_required", _LOGIN, "LOGIN_REQUIRED"),
        ("no_records", _NO_RECORDS, "NO_RECORDS"),
        ("transaction_table", _TRANSACTION_TABLE, "TRANSACTION_TABLE_VISIBLE"),
        ("parse_failed", _PARSE_FAILED, "PARSE_FAILED"),
    ):
        evidence = _hits(combined, terms)
        if evidence:
            return _decision_for(state, code, evidence)
    if str(url or "").lower().startswith(("http://", "https://")) and str(text or "").strip():
        return ChallengeDecision("collectable_page", "PORTAL_CONTENT_PRESENT")
    return ChallengeDecision("unknown", "INSUFFICIENT_PORTAL_SIGNAL")


def validate_provider_output(value: Any) -> ChallengeDecision:
    """Validate untrusted model/provider output without accepting free-form states."""
    if not isinstance(value, dict) or value.get("state") not in ALLOWED_STATES:
        return ChallengeDecision("unknown", "INVALID_PROVIDER_OUTPUT")
    try:
        confidence = float(value.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 0.0
    suggested_action = str(value.get("suggested_action") or "no_action")
    if suggested_action not in ALLOWED_ACTIONS:
        return ChallengeDecision("unknown", "INVALID_PROVIDER_ACTION", confidence=confidence)
    if confidence < MIN_PROVIDER_CONFIDENCE:
        return ChallengeDecision("unknown", "LOW_PROVIDER_CONFIDENCE", confidence=confidence)
    evidence = value.get("evidence") if isinstance(value.get("evidence"), list) else []
    return ChallengeDecision(
        str(value["state"]),
        "provider",
        tuple(str(item)[:80] for item in evidence[:3]),
        confidence=confidence,
        suggested_action=suggested_action,
        requires_operator=bool(value.get("requires_operator")),
    )


def make_resume_token(work_key: str, session_id: str, run_id: str) -> str:
    """Return a non-secret opaque reference; no credentials or challenge values are included."""
    material = "|".join(str(value or "") for value in (work_key, session_id, run_id))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def approved_operator_input(payload: dict[str, Any]) -> str:
    """Return an input only when the caller explicitly records operator approval.

    The value is transient and must never be written to a ledger or logs.
    """
    if not payload.get("operator_approved"):
        return ""
    value = str(payload.get("approved_input") or payload.get("captcha_value") or "")
    return re.sub(r"[^0-9A-Za-z-]", "", value)[:32]
