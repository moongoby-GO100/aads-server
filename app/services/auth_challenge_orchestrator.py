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
    {"captcha_required", "otp_required", "login_required", "portal_error", "collectable_page", "unknown"}
)


class OptionalStateProvider(Protocol):
    def classify(self, *, url: str, text: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ChallengeDecision:
    state: str
    reason_code: str
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "reason_code": self.reason_code, "evidence": list(self.evidence)}


_CAPTCHA = ("captcha", "캡차", "보안문자", "자동입력방지", "숫자를 입력")
_OTP = ("otp", "인증번호", "일회용 비밀번호", "2차 인증", "추가 인증", "휴대폰 인증", "기기 인증")
_LOGIN = ("로그인해주세요", "로그인이 필요", "사장님 로그인", "login")
_ERROR = ("access denied", "forbidden", "올바르지 않은 요청", "보안 위배 접근 제한", "오류가 발생")


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
                return ChallengeDecision(state, "provider", tuple(str(x)[:80] for x in output.get("evidence", [])[:3]))
        except Exception:
            pass
    combined = f"{url}\n{text}"
    for state, terms, code in (
        ("portal_error", _ERROR, "PORTAL_ERROR"),
        ("captcha_required", _CAPTCHA, "CAPTCHA_REQUIRED"),
        ("otp_required", _OTP, "OTP_REQUIRED"),
        ("login_required", _LOGIN, "LOGIN_REQUIRED"),
    ):
        evidence = _hits(combined, terms)
        if evidence:
            return ChallengeDecision(state, code, evidence)
    if str(url or "").lower().startswith(("http://", "https://")) and str(text or "").strip():
        return ChallengeDecision("collectable_page", "PORTAL_CONTENT_PRESENT")
    return ChallengeDecision("unknown", "INSUFFICIENT_PORTAL_SIGNAL")


def validate_provider_output(value: Any) -> ChallengeDecision:
    """Validate untrusted model/provider output without accepting free-form states."""
    if not isinstance(value, dict) or value.get("state") not in ALLOWED_STATES:
        return ChallengeDecision("unknown", "INVALID_PROVIDER_OUTPUT")
    evidence = value.get("evidence") if isinstance(value.get("evidence"), list) else []
    return ChallengeDecision(str(value["state"]), "provider", tuple(str(item)[:80] for item in evidence[:3]))


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
