"""모델 전환 배너 — 생성·인식의 단일 출처.

응답 앞에 붙는 "[요청모델 응답 실패(사유) → 실제모델로 응답]" 배너를 chat_service(재시도 루프)와
model_selector(직결 provider 폴백)가 같은 헬퍼로 만든다. 예전 "[X 실행 불가 → Y 전환]" 은
실패 사유도 모르면서 "실행 불가"로 단정했다. 사유를 모르면 "응답 실패"까지만 쓴다.

이 모듈은 chat_service / model_selector 양쪽에서 import 하므로 서비스 모듈을 import 하지 않는다.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

FORMAT_NEW = "new"
FORMAT_LEGACY = "legacy"

_ACTUAL_SUFFIX = "로 응답"
_PATH_PREFIX = " · 시도: "
_FAILED_MARK = "(실패)"

# 새 형식: [A 응답 실패(사유) → B로 응답] / [A 응답 실패 → B로 응답 · 시도: A → C(실패) → B]
# 사유 안에 괄호가 들어갈 수 있으므로("Codex 인증 실패(재로그인 필요)") 닫는 괄호는 " → " 앞의 것까지 잡는다.
_NEW_RE = re.compile(
    r"\[(?P<requested>[^\]\n]+?) 응답 실패(?:\((?P<reason>[^\n]*?)\))? → "
    r"(?P<actual>[^\]\n]+?)" + _ACTUAL_SUFFIX
    + r"(?:" + re.escape(_PATH_PREFIX) + r"(?P<path>[^\]\n]+?))?\]"
)
# 옛 형식: [A 실행 불가 → B 전환] / [A 오류 → Codex CLI B 전환] / [A (Antigravity) 오류 → ... 전환]
_LEGACY_RE = re.compile(
    r"\[(?P<requested>[^\]\n]+?) (?:실행 불가|오류) → (?P<actual>[^\]\n]+?) 전환\]"
)


def classify_failure_reason(error: object) -> str:
    """오류를 고정 분류로만 바꾼다. 원문은 절대 돌려주지 않는다."""
    detail = str(error or "").lower()
    if isinstance(error, BaseException):
        detail = f"{type(error).__name__.lower()} {detail}"
    status = re.search(r"\b(4\d\d|5\d\d)\b", detail)
    if "429" in detail or "quota" in detail or re.search(r"rate[\s_-]?limit|\blimit\b", detail):
        return "한도 초과"
    if "overloaded" in detail or "529" in detail:
        return "과부하"
    if "codex_auth_failed" in detail:
        return "Codex 인증 실패(재로그인 필요)"
    if re.search(r"\b40[13]\b", detail) or "unauthorized" in detail or "authentication" in detail:
        return "인증 오류"
    if "timeout" in detail or "timed out" in detail:
        return "시간 초과"
    if any(k in detail for k in ("connect", "network", "unreachable", "reset", "disconnected", "readerror")):
        return "연결 오류"
    if "empty" in detail or "missing_done" in detail or "without done" in detail:
        return "빈 응답"
    if status:
        return f"HTTP {status.group(1)}"
    return "요청 오류"


def build_switch_banner(
    requested_model: object,
    actual_model: object,
    reason: Optional[str] = None,
    *,
    failed_path: Optional[Sequence[object]] = None,
) -> str:
    """응답 앞에 붙일 전환 배너. 앞뒤 빈 줄까지 포함한다.

    reason       분류된 사유(classify_failure_reason 결과). 모르면 None/빈 문자열 — 괄호를 생략한다.
    failed_path  요청 모델부터 실제 모델 직전까지 실패한 모델 순서. 실패가 둘 이상일 때만 경로를 적는다.
    """
    requested = str(requested_model or "").strip() or "모델"
    actual = str(actual_model or "").strip() or "다른 모델"
    reason_text = str(reason or "").strip()
    head = f"{requested} 응답 실패" + (f"({reason_text})" if reason_text else "")
    banner = f"{head} → {actual}{_ACTUAL_SUFFIX}"
    failed = [str(m).strip() for m in (failed_path or []) if str(m or "").strip()]
    if len(failed) >= 2:
        hops = [failed[0]] + [f"{m}{_FAILED_MARK}" for m in failed[1:]] + [actual]
        banner += _PATH_PREFIX + " → ".join(hops)
    return f"\n\n[{banner}]\n\n"


def parse_switch_banners(text: object) -> List[Dict[str, Any]]:
    """텍스트 안의 전환 배너(새·옛 형식)를 등장 순서대로 돌려준다."""
    source = str(text or "")
    found: List[Dict[str, Any]] = []
    for match in _NEW_RE.finditer(source):
        found.append({
            "format": FORMAT_NEW,
            "requested": match.group("requested").strip(),
            "actual": match.group("actual").strip(),
            "reason": (match.group("reason") or "").strip() or None,
            "path": (match.group("path") or "").strip() or None,
            "start": match.start(),
            "end": match.end(),
        })
    for match in _LEGACY_RE.finditer(source):
        found.append({
            "format": FORMAT_LEGACY,
            "requested": match.group("requested").strip(),
            "actual": match.group("actual").strip(),
            "reason": None,
            "path": None,
            "start": match.start(),
            "end": match.end(),
        })
    found.sort(key=lambda item: item["start"])
    return found


def has_switch_banner(text: object) -> bool:
    source = str(text or "")
    return bool(_NEW_RE.search(source) or _LEGACY_RE.search(source))


def strip_switch_banners(text: object) -> str:
    """전환 배너와 그 앞뒤 빈 줄을 지운다. 배너가 없으면 원문 그대로."""
    source = str(text or "")
    for banner_re in (_NEW_RE, _LEGACY_RE):
        source = re.sub(r"\n*" + banner_re.pattern + r"\n*", "", source)
    return source
