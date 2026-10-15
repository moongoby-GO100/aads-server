"""goals.approval_policy 표준 스키마 — 쓰기 검증과 분류 (읽기 전용 순수 함수).

실제로 자동승인에 쓰이는 키는 `live_trading_guard.goal_policy_allows` 와
`next_step_proposals` 가 읽는 4개뿐이다.

    auto_approve_high / auto_approve_critical / max_executions / used

ACCT 목표처럼 `db_ddl`·`deploy`·`db_destructive` 역할 키만 들어 있으면 화면에는
설정된 것처럼 보여도 가드는 아무것도 열지 않는다. 이 모듈은 그 차이를 드러내기만
하고 데이터를 바꾸지 않는다(레거시 → 표준 변환 금지).
"""
from __future__ import annotations

from typing import Any

EFFECTIVE_KEYS: tuple[str, ...] = (
    "auto_approve_high", "auto_approve_critical", "max_executions", "used",
)
BOOKKEEPING_KEYS: tuple[str, ...] = ("set_by", "set_at")
STANDARD_KEYS: tuple[str, ...] = EFFECTIVE_KEYS + BOOKKEEPING_KEYS
META_KEYS_ALLOWLIST: frozenset[str] = frozenset({"note", "reason", "reset_at", "reset_by"})
LEGACY_ROLE_KEYS: tuple[str, ...] = ("db_ddl", "deploy", "db_destructive")

CLASS_STANDARD = "standard"
CLASS_LEGACY = "legacy_role_keys"
CLASS_MIXED = "mixed"
CLASS_EMPTY = "empty"

ERR_USED_EXCEEDS_MAX = "used_exceeds_max_executions"

_TRUE_TEXT = {"true", "t", "yes", "y", "on", "1"}


def _as_dict(policy: Any) -> dict[str, Any]:
    return policy if isinstance(policy, dict) else {}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_policy(policy: Any) -> tuple[bool, list[str]]:
    """표준 키의 타입·범위만 검사한다. 미지 키는 오류가 아니다(기존 화면 호환).

    오류 문자열은 `<코드>: <설명>` 형식이다.
    """
    if not isinstance(policy, dict):
        return False, ["policy_not_object: approval_policy 는 객체여야 합니다"]

    errors: list[str] = []
    for key in ("auto_approve_high", "auto_approve_critical"):
        if key in policy and not isinstance(policy[key], bool):
            errors.append(f"{key}_type: {key} 는 bool 이어야 합니다")
    for key in ("max_executions", "used"):
        if key not in policy:
            continue
        value = policy[key]
        if not _is_int(value):
            errors.append(f"{key}_type: {key} 는 정수여야 합니다")
        elif value < 0:
            errors.append(f"{key}_negative: {key} 는 0 이상이어야 합니다")
    for key in BOOKKEEPING_KEYS:
        if key in policy and not isinstance(policy[key], str):
            errors.append(f"{key}_type: {key} 는 문자열이어야 합니다")

    max_exec, used = policy.get("max_executions"), policy.get("used")
    if _is_int(max_exec) and _is_int(used) and used > max_exec:
        errors.append(
            f"{ERR_USED_EXCEEDS_MAX}: used({used}) 가 max_executions({max_exec}) 를 넘습니다"
        )
    return (not errors), errors


def ignored_keys(policy: Any) -> list[str]:
    """가드가 읽지 않고 표준에도 없는 키 (정렬)."""
    known = set(STANDARD_KEYS) | META_KEYS_ALLOWLIST
    return sorted(str(k) for k in _as_dict(policy) if k not in known)


def classify_policy(policy: Any) -> str:
    """standard | legacy_role_keys | mixed | empty.

    표준 키(set_by/set_at 포함)가 하나도 없고 역할 키가 있으면 legacy_role_keys.
    표준 쪽이 없는데 역할 키도 아닌 미지 키만 있는 경우는 mixed 로 둔다.
    """
    data = _as_dict(policy)
    if not data:
        return CLASS_EMPTY
    if not ignored_keys(data):
        return CLASS_STANDARD
    has_standard = any(k in data for k in STANDARD_KEYS)
    if not has_standard and any(k in data for k in LEGACY_ROLE_KEYS):
        return CLASS_LEGACY
    return CLASS_MIXED


def _pg_bool(value: Any) -> bool:
    """`(approval_policy->>key)::boolean` 과 같은 해석. 해석 불가면 False."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_TEXT
    return False


def _pg_int(value: Any) -> int:
    """`COALESCE((approval_policy->>key)::int, 0)` 과 같은 해석. 해석 불가면 0."""
    if _is_int(value):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def effective_summary(policy: Any) -> dict[str, Any]:
    """goal_policy_allows 가 실제로 적용하는 값과 무시되는 키."""
    data = _as_dict(policy)
    high = _pg_bool(data.get("auto_approve_high"))
    critical = _pg_bool(data.get("auto_approve_critical"))
    max_exec = _pg_int(data.get("max_executions"))
    used = _pg_int(data.get("used"))
    remaining = max(max_exec - used, 0)
    return {
        "auto_approve_high": high,
        "auto_approve_critical": critical,
        "max_executions": max_exec,
        "used": used,
        "remaining": remaining,
        "exhausted": used >= max_exec,
        "high_effective": high and remaining > 0,
        "critical_effective": critical and remaining > 0,
        "ignored_keys": ignored_keys(data),
    }


def needs_attention(policy: Any, *, include_empty: bool = False) -> bool:
    """lint 대상: 표준이 아니거나 횟수를 소진한 목표.

    빈 설정(미설정)은 문제가 아니라 기본값이므로 include_empty 일 때만 포함한다.
    """
    data = _as_dict(policy)
    classification = classify_policy(data)
    if classification == CLASS_EMPTY:
        return include_empty
    if classification != CLASS_STANDARD:
        return True
    summary = effective_summary(data)
    return summary["max_executions"] > 0 and summary["used"] >= summary["max_executions"]
