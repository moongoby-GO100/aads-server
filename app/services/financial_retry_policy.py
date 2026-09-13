"""금융 자동화 실패의 재시도 허용 여부를 판정한다.

일반 사이트는 재시도가 싸다. 은행은 다르다 — **로그인 실패가 누적되면 계정이
잠긴다.** 잠기면 자동화가 멈추는 정도가 아니라 CEO 가 직접 영업점이나 ARS 로
풀어야 한다. 같은 재시도 정책을 쓰면 안 된다.

2026-09-13 GenSpark 로그인을 네 번 시도했다. 일반 사이트라 대가가 시간뿐이었지만,
은행이었다면 그 네 번이 계정 잠금이었다.

원칙은 하나다. **인증 관련 실패는 재시도하지 않는다.** 네트워크·타임아웃처럼
계정에 흔적을 남기지 않는 실패만 재시도한다. 애매하면 재시도하지 않는다 —
잘못 재시도해서 잠그는 쪽이, 한 번 더 시도하지 않아 수집을 거르는 쪽보다
훨씬 비싸다.
"""
from __future__ import annotations

import re
from typing import Any

# 계정에 흔적을 남기는 실패. 절대 재시도하지 않는다.
_NEVER_RETRY = {
    "auth_failed": (
        "비밀번호가 틀렸", "비밀번호 오류", "아이디 또는 비밀번호",
        "인증에 실패", "인증 실패", "로그인 정보가 올바르지",
        "invalid credential", "authentication failed", "login failed",
        "incorrect password", "unauthorized",
    ),
    "account_locked": (
        "계정이 잠", "사용이 정지", "이용이 제한", "거래가 중지",
        "오류 횟수", "횟수를 초과", "5회", "잠금 상태",
        "account locked", "account is locked", "too many attempts",
    ),
    "cert_required": (
        "공동인증서", "공인인증서", "인증서를 선택", "인증서 오류", "npki",
    ),
    "extra_auth_required": (
        "otp", "ars", "추가 인증", "2차 인증", "본인 확인", "휴대폰 인증",
        "보안카드", "안심클릭",
    ),
}

# 계정에 흔적을 남기지 않는 실패. 재시도해도 안전하다.
_SAFE_RETRY = {
    "network": ("timeout", "timed out", "연결", "network", "econnreset",
                "temporarily unavailable", "503", "502", "504"),
    "pc_agent_unavailable": ("pc agent", "pc_agent", "헤드리스", "headless"),
    "page_not_ready": ("요소를 찾을 수 없", "element not found", "not visible",
                       "navigation", "로딩"),
}


def classify_financial_failure(text: Any) -> str:
    """실패 문구를 분류한다. 판단 불가는 ``unknown``.

    인증 계열을 먼저 본다. 한 메시지에 여러 신호가 섞이면 더 위험한 쪽을
    택해야 하기 때문이다 — "타임아웃 후 인증 실패" 는 재시도 대상이 아니다.
    """
    body = str(text or "").strip().lower()
    if not body:
        return "unknown"
    for category, markers in _NEVER_RETRY.items():
        for marker in markers:
            if marker.lower() in body:
                return category
    for category, markers in _SAFE_RETRY.items():
        for marker in markers:
            if marker.lower() in body:
                return category
    return "unknown"


def is_retry_allowed(category: str) -> bool:
    """재시도해도 되는 분류인지.

    ``unknown`` 은 금지한다. 모르는 실패를 은행에 다시 던지는 것은
    계정 잠금을 감수하는 일이다. 사람이 보고 판단해야 한다.
    """
    return str(category or "").strip() in _SAFE_RETRY


def evaluate_financial_failure(text: Any, *, attempt: int = 1, max_attempts: int = 2) -> dict:
    """분류 + 재시도 판정을 한 번에. 호출부가 그대로 기록할 수 있는 형태.

    ``reason`` 은 사람이 읽고 판단하는 문장이다. 자동화가 멈춘 이유가 로그에
    남지 않으면, 다음 사람이 같은 실패를 다시 재시도하게 된다.
    """
    category = classify_financial_failure(text)
    allowed = is_retry_allowed(category)
    if allowed and attempt >= max_attempts:
        allowed = False
        reason = f"재시도 가능한 실패({category})이나 시도 한도 {max_attempts}회 도달"
    elif allowed:
        reason = f"계정에 흔적을 남기지 않는 실패({category}) — 재시도 허용"
    elif category == "unknown":
        reason = "실패 사유를 분류할 수 없음 — 계정 잠금 위험이 있어 재시도하지 않는다"
    else:
        reason = f"인증 계열 실패({category}) — 재시도하면 계정이 잠길 수 있다"
    return {
        "category": category,
        "retry_allowed": allowed,
        "reason": reason,
        "attempt": attempt,
        "max_attempts": max_attempts,
    }
