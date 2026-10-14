"""자동화 초안(automation_drafts) 상태 머신 — 설계서 4·5절.

규칙
- 상태 변경은 `transition()` 한 곳에서만 한다. from 이 현재 상태와 다르면 `TransitionConflict`(API 409).
- 모든 쿼리에 tenant_id 조건이 붙는다 — 다른 테넌트의 초안은 "없음"으로 보인다.
- 모든 전이는 automation_draft_events 에 같은 트랜잭션으로 기록된다.
- 이벤트 detail 과 초안 입력은 저장 전에 비밀값 패턴을 검사하고, 발견되면 저장하지 않고 거부한다.
- 화면 문장은 이 모듈의 사전에서만 만든다. 프론트는 `build_card()` 결과를 그대로 그린다(FR-27).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any

from app.core.db_pool import get_pool

DRAFTS_FLAG = "AUTOMATION_DRAFTS_ENABLED"
AUTO_APPROVE_READ_FLAG = "AUTOMATION_AUTO_APPROVE_READ"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


def drafts_enabled() -> bool:
    """초안 기능 입구. 기본 꺼짐(롤백 = 이 플래그를 끄는 것)."""
    return _flag(DRAFTS_FLAG)


def auto_approve_read_enabled() -> bool:
    """읽기 전용 자동 승인(D1). 승인됐지만 출시 단계에서 켠다 — 기본 꺼짐."""
    return _flag(AUTO_APPROVE_READ_FLAG)


# ── 상태·전이표 (설계서 4절) ────────────────────────────────────────────────

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "intent_draft": ("intent_confirmed", "cancelled"),
    "intent_confirmed": ("credential_ready", "credential_waiting"),
    "credential_waiting": ("credential_ready", "expired"),
    "credential_ready": ("demonstrating",),
    "demonstrating": ("result_review", "paused_risk", "failed"),
    "paused_risk": ("demonstrating", "result_review", "cancelled"),
    "result_review": ("registering", "revising"),
    "revising": ("demonstrating", "manual_record"),
    "manual_record": ("result_review",),
    "registering": ("pending_approval", "active"),
    "pending_approval": ("active", "rejected", "expired"),
    "active": ("broken", "paused"),
    "failed": ("credential_ready", "demonstrating", "cancelled"),
}

# 표의 "다음 상태" 열에만 나오는 도착 상태. 이 표에는 이들에서 나가는 전이가 없다.
TERMINAL_STATES: tuple[str, ...] = ("cancelled", "expired", "rejected", "broken", "paused")
STATES: tuple[str, ...] = tuple(TRANSITIONS)
ALL_STATES: tuple[str, ...] = STATES + TERMINAL_STATES
INITIAL_STATE = "intent_draft"

FAILURE_CODES: tuple[str, ...] = (
    "auth_failed", "challenge", "blocked", "layout_changed", "empty_result", "transient",
)


class DraftError(Exception):
    """자동화 초안 서비스 공통 예외."""


class DraftsDisabled(DraftError):
    """AUTOMATION_DRAFTS_ENABLED 가 꺼져 있다."""


class DraftNotFound(DraftError):
    """초안이 없거나 다른 테넌트의 것이다(구분하지 않는다)."""


class InvalidTransition(DraftError):
    """전이표에 없는 상태 쌍이거나 알 수 없는 상태."""


class TransitionConflict(DraftError):
    """from 이 현재 상태와 다르다 — 동시 클릭·중복 탭. API 는 409."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"state_conflict: expected={expected} actual={actual}")
        self.expected = expected
        self.actual = actual


class SecretDetected(DraftError):
    """저장하려던 값에 비밀값 패턴이 있다. 값 자체는 메시지에 싣지 않는다."""

    def __init__(self, path: str) -> None:
        super().__init__(f"secret_detected_at:{path}")
        self.path = path


# ── 비밀값 검사 ────────────────────────────────────────────────────────────

_SECRET_REF = "{{secret."
_SECRET_KEY = re.compile(
    r"password|passwd|pwd|passcode|token|secret|cookie|authorization|api[_-]?key|비밀번호|쿠키",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"eyJ[\w-]{10,}"
    r"|sk-[A-Za-z0-9_-]{10,}"
    r"|bearer\s+[\w.~+/-]{8,}"
    r"|set-cookie"
    r"|(?:password|passwd|pwd|token|secret|cookie|authorization|비밀번호|쿠키)\s*[=:]\s*(?!\{\{secret\.)\S+",
    re.IGNORECASE,
)


def find_secret(value: Any, path: str = "$") -> str | None:
    """비밀값으로 보이는 첫 위치의 경로를 돌려준다. 없으면 None. 값은 돌려주지 않는다."""
    if isinstance(value, str):
        return path if _SECRET_VALUE.search(value) else None
    if isinstance(value, Mapping):
        for key, item in value.items():
            sub = f"{path}.{key}"
            if _SECRET_KEY.search(str(key)) and _filled_non_reference(item):
                return sub
            hit = find_secret(item, sub)
            if hit:
                return hit
        return None
    if isinstance(value, (list, tuple, set)):
        for idx, item in enumerate(value):
            hit = find_secret(item, f"{path}[{idx}]")
            if hit:
                return hit
    return None


def _filled_non_reference(item: Any) -> bool:
    if isinstance(item, str):
        text = item.strip()
        return bool(text) and not text.startswith(_SECRET_REF)
    if isinstance(item, (Mapping, list, tuple, set)):
        return bool(item)
    return False


def assert_no_secret(value: Any, label: str) -> None:
    hit = find_secret(value, label)
    if hit:
        raise SecretDetected(hit)


# ── 화면 문장 사전 (설계서 4절 '뜻' 열) ─────────────────────────────────────

STATE_HEADLINES: dict[str, str] = {
    "intent_draft": "이렇게 이해했어요",
    "intent_confirmed": "로그인 연결",
    "credential_waiting": "로그인 정보를 기다리는 중",
    "credential_ready": "대신 해 보는 중",
    "demonstrating": "대신 해 보는 중",
    "paused_risk": "확인이 필요해요",
    "result_review": "이 결과가 맞나요?",
    "revising": "다시 해 보는 중",
    "manual_record": "직접 보여주세요",
    "registering": "저장 중",
    "pending_approval": "확인 기다리는 중",
    "active": "사용 가능",
    "failed": "실패",
    "cancelled": "취소했어요",
    "expired": "기한이 지났어요",
    "rejected": "승인되지 않았어요",
    "broken": "더 이상 실행되지 않아요",
    "paused": "잠시 멈췄어요",
}

STATE_BODIES: dict[str, str] = {
    "intent_draft": "말씀하신 내용을 이렇게 이해했어요. 맞는지 확인해 주세요.",
    "intent_confirmed": "저장된 로그인이 있는지 확인하고 있어요.",
    "credential_waiting": "안전한 입력창에 로그인 정보를 넣어 주세요. 30분 안에 넣지 않으면 닫혀요.",
    "credential_ready": "로그인 준비가 끝나서 곧 대신 해 볼게요.",
    "demonstrating": "사이트에서 대신 해 보고 있어요. 잠시만 기다려 주세요.",
    "paused_risk": "여기서부터는 사이트에 내용을 남기거나 바꾸는 단계예요. 계속할지 골라 주세요.",
    "result_review": "대신 해 본 결과예요. 원하신 화면과 내용이 맞는지 확인해 주세요.",
    "revising": "다시 한 번 해 보고 있어요.",
    "manual_record": "화면에서 직접 한 번 보여 주세요. 그대로 따라 할게요.",
    "registering": "방법을 저장하고 있어요.",
    "pending_approval": "담당자가 확인하면 바로 쓸 수 있어요.",
    "active": "언제든 말씀하시면 바로 실행해요.",
    "cancelled": "만들기를 그만뒀어요.",
    "expired": "정해진 기간 안에 끝나지 않아서 닫았어요. 필요하면 다시 요청해 주세요.",
    "rejected": "담당자가 이번에는 승인하지 않았어요.",
    "broken": "사이트가 바뀌었거나 로그인이 막혀서 실행되지 않아요. 다시 만들어 주세요.",
    "paused": "예약 실행을 잠시 멈췄어요.",
}

FAILURE_BODIES: dict[str, str] = {
    "auth_failed": "로그인이 되지 않았어요. 아이디와 비밀번호를 다시 확인해 주세요. 같은 정보로 다시 시도하지는 않을게요.",
    "challenge": "사이트가 사람인지 확인하는 단계(인증번호나 그림 확인)를 요구했어요. 이 단계는 대신할 수 없어요.",
    "blocked": "사이트가 접속을 막았어요. 잠시 뒤에 다시 시도하거나 직접 보여 주세요.",
    "layout_changed": "화면 모양이 달라져서 찾던 버튼을 못 찾았어요. 직접 한 번 보여 주세요.",
    "empty_result": "조건에 맞는 내용이 한 줄도 없었어요. 기간이나 조건을 바꿔서 다시 해 볼까요?",
    "transient": "사이트 응답이 느리거나 끊겼어요. 잠시 뒤에 다시 해 보세요.",
}
GENERIC_FAILURE_BODY = "대신 해 보다가 멈췄어요. 다시 해 보거나 그만둘 수 있어요."

_BUTTONS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "intent_draft": (("confirm_intent", "맞아요", True), ("edit_intent", "고칠래요", False), ("cancel", "취소", False)),
    "credential_waiting": (("cancel", "취소", False),),
    "demonstrating": (("cancel", "그만두기", False),),
    "paused_risk": (
        ("proceed", "진행", True), ("stop_here", "여기까지만", False), ("cancel", "취소", False),
    ),
    "result_review": (("approve_result", "맞아요", True), ("reject_result", "아니에요", False)),
    "manual_record": (("start_manual", "직접 보여줄게요", True), ("cancel", "취소", False)),
    "active": (("run_now", "지금 실행", True), ("pause", "멈추기", False)),
}
_FAILED_BUTTONS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "auth_failed": (("relink_login", "로그인 다시 연결", True), ("cancel", "그만두기", False)),
    "challenge": (("manual_record", "직접 보여줄게요", True), ("cancel", "그만두기", False)),
    "blocked": (("retry_demo", "다시 해 보기", True), ("cancel", "그만두기", False)),
    "layout_changed": (("manual_record", "직접 보여줄게요", True), ("cancel", "그만두기", False)),
    "empty_result": (("retry_demo", "다시 해 보기", True), ("cancel", "그만두기", False)),
    "transient": (("retry_demo", "다시 해 보기", True), ("cancel", "그만두기", False)),
}
_FAILED_DEFAULT_BUTTONS = (
    ("retry_demo", "다시 해 보기", True), ("relink_login", "로그인 다시 연결", False), ("cancel", "그만두기", False),
)


def build_card(state: str, *, title: str = "", failure_code: str | None = None) -> dict[str, Any]:
    """상태 → 사람 말 카드. 프론트는 문장을 만들지 않고 이 값을 그대로 그린다."""
    if state not in ALL_STATES:
        raise InvalidTransition(f"unknown_state:{state}")
    if state == "failed":
        body = FAILURE_BODIES.get(failure_code or "", GENERIC_FAILURE_BODY)
        buttons = _FAILED_BUTTONS.get(failure_code or "", _FAILED_DEFAULT_BUTTONS)
    else:
        body = STATE_BODIES[state]
        buttons = _BUTTONS.get(state, ())
    return {
        "state": state,
        "headline": STATE_HEADLINES[state],
        "title": title,
        "body": body,
        "buttons": [{"id": bid, "label": label, "primary": primary} for bid, label, primary in buttons],
    }


def human_sentences() -> list[str]:
    """사용자에게 보이는 모든 고정 문장(사전 + 버튼 라벨). 금지 용어 검사 대상."""
    texts = [*STATE_HEADLINES.values(), *STATE_BODIES.values(), *FAILURE_BODIES.values(), GENERIC_FAILURE_BODY]
    groups = [*_BUTTONS.values(), *_FAILED_BUTTONS.values(), _FAILED_DEFAULT_BUTTONS]
    texts.extend(label for group in groups for _, label, _ in group)
    return texts


# ── 저장소 접근 ────────────────────────────────────────────────────────────

_EVENT_BY_TARGET = {
    "intent_confirmed": "intent_confirmed",
    "credential_waiting": "credential_requested",
    "credential_ready": "credential_linked",
    "demonstrating": "demo_started",
    "paused_risk": "demo_paused_risk",
    "failed": "demo_failed",
    "result_review": "demo_step",
    "revising": "result_rejected",
    "pending_approval": "registered",
    "rejected": "rejected",
    "expired": "expired",
}


def event_type_for(from_state: str, to_state: str) -> str:
    """PRD 8절 이벤트 이름. 대응이 없는 전이는 state_changed."""
    if to_state == "active":
        return "approved" if from_state == "pending_approval" else "registered"
    if from_state == "result_review" and to_state == "registering":
        return "result_confirmed"
    return _EVENT_BY_TARGET.get(to_state, "state_changed")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _row(record: Any) -> dict[str, Any] | None:
    return dict(record) if record is not None else None


async def create_draft(
    *,
    tenant_id: str,
    owner_user: str,
    title: str,
    intent_text: str,
    intent: Mapping[str, Any] | None = None,
    session_id: str | None = None,
    aliases: list[str] | None = None,
    lane: str = "server",
    actor: str | None = None,
) -> dict[str, Any]:
    """초안을 intent_draft 상태로 만들고 draft_created 이벤트를 남긴다."""
    if not drafts_enabled():
        raise DraftsDisabled(DRAFTS_FLAG)
    payload = {"title": title, "intent_text": intent_text, "intent": dict(intent or {}), "aliases": aliases or []}
    assert_no_secret(payload, "draft")
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            record = await conn.fetchrow(
                """
                INSERT INTO automation_drafts
                    (tenant_id, owner_user, session_id, title, intent_text, intent, aliases, state, lane)
                VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6::jsonb, $7::text[], $8, $9)
                RETURNING *
                """,
                str(tenant_id), owner_user, str(session_id) if session_id else None, title, intent_text,
                _dumps(payload["intent"]), list(payload["aliases"]), INITIAL_STATE, lane,
            )
            draft = dict(record)
            await conn.execute(
                """
                INSERT INTO automation_draft_events (draft_id, event_type, from_state, to_state, actor, detail)
                VALUES ($1::uuid, $2, NULL, $3, $4, $5::jsonb)
                """,
                str(draft["id"]), "draft_created", INITIAL_STATE, actor or owner_user, _dumps({}),
            )
    return draft


async def get_draft(draft_id: str, *, tenant_id: str) -> dict[str, Any] | None:
    async with get_pool().acquire() as conn:
        record = await conn.fetchrow(
            "SELECT * FROM automation_drafts WHERE id=$1::uuid AND tenant_id=$2::uuid",
            str(draft_id), str(tenant_id),
        )
    return _row(record)


async def list_active_drafts(*, tenant_id: str) -> list[dict[str, Any]]:
    """반복 실행 매칭용: active 상태 초안의 title·aliases·recipe_names 만 읽는다."""
    async with get_pool().acquire() as conn:
        records = await conn.fetch(
            "SELECT id, title, aliases, recipe_names FROM automation_drafts "
            "WHERE tenant_id=$1::uuid AND state='active'",
            str(tenant_id),
        )
    return [dict(record) for record in records]


async def transition(
    draft_id: str,
    from_state: str,
    to_state: str,
    actor: str,
    reason: str,
    *,
    tenant_id: str,
    failure_code: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """상태 변경의 단일 진입점.

    InvalidTransition(전이표 밖) → DraftNotFound(없음/타 테넌트) → TransitionConflict(409) 순으로 막고,
    통과하면 상태 갱신과 이벤트 기록을 한 트랜잭션으로 처리한다.
    """
    if from_state not in ALL_STATES or to_state not in ALL_STATES:
        raise InvalidTransition(f"unknown_state:{from_state}->{to_state}")
    if to_state not in TRANSITIONS.get(from_state, ()):
        raise InvalidTransition(f"not_allowed:{from_state}->{to_state}")
    if failure_code is not None and failure_code not in FAILURE_CODES:
        raise InvalidTransition(f"unknown_failure_code:{failure_code}")
    event_detail = {"reason": reason, **dict(detail or {})}
    if failure_code:
        event_detail["failure_code"] = failure_code
    assert_no_secret(event_detail, "detail")

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchrow(
                """
                UPDATE automation_drafts
                   SET state=$4, failure_code=$5, updated_at=NOW()
                 WHERE id=$1::uuid AND tenant_id=$2::uuid AND state=$3
                RETURNING *
                """,
                str(draft_id), str(tenant_id), from_state, to_state,
                failure_code if to_state == "failed" else None,
            )
            if updated is None:
                current = await conn.fetchrow(
                    "SELECT state FROM automation_drafts WHERE id=$1::uuid AND tenant_id=$2::uuid",
                    str(draft_id), str(tenant_id),
                )
                if current is None:
                    raise DraftNotFound(str(draft_id))
                raise TransitionConflict(from_state, current["state"])
            await conn.execute(
                """
                INSERT INTO automation_draft_events (draft_id, event_type, from_state, to_state, actor, detail)
                VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb)
                """,
                str(draft_id), event_type_for(from_state, to_state), from_state, to_state, actor,
                _dumps(event_detail),
            )
    return dict(updated)


async def list_events(draft_id: str, *, tenant_id: str) -> list[dict[str, Any]]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.id, e.event_type, e.from_state, e.to_state, e.actor, e.detail, e.created_at
              FROM automation_draft_events e
              JOIN automation_drafts d ON d.id = e.draft_id
             WHERE e.draft_id=$1::uuid AND d.tenant_id=$2::uuid
             ORDER BY e.id
            """,
            str(draft_id), str(tenant_id),
        )
    return [dict(r) for r in rows]


# ── 비상태 필드 갱신·목록 (T2) ──────────────────────────────────────────────

# 컬럼 → SQL 캐스트. 상태(state)와 failure_code 는 여기에 없다 — 그것은 transition() 만 바꾼다.
_UPDATABLE_COLUMNS: dict[str, str] = {
    "title": "text",
    "intent": "jsonb",
    "aliases": "text[]",
    "credential_id": "uuid",
    "credential_request_id": "uuid",
    "recording_ref": "jsonb",
    "evidence": "jsonb",
    "schedule_job_id": "text",
}
_JSON_COLUMNS = frozenset({"intent", "recording_ref", "evidence"})


def _bind(column: str, value: Any) -> Any:
    if value is None:
        return None
    if column in _JSON_COLUMNS:
        return _dumps(value)
    return str(value) if _UPDATABLE_COLUMNS[column] == "uuid" else value


async def update_fields(
    draft_id: str,
    *,
    tenant_id: str,
    expected_state: str | None,
    fields: Mapping[str, Any],
    actor: str,
    event_type: str,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """상태를 바꾸지 않는 필드 갱신. expected_state 가 현재와 다르면 TransitionConflict.

    허용 컬럼만 받고(`_UPDATABLE_COLUMNS`), 값은 저장 전에 비밀값 검사를 통과해야 한다.
    갱신과 이벤트 기록은 한 트랜잭션이다.
    """
    unknown = set(fields) - set(_UPDATABLE_COLUMNS)
    if unknown or not fields:
        raise DraftError(f"field_not_updatable:{sorted(unknown)}")
    if expected_state is not None and expected_state not in ALL_STATES:
        raise InvalidTransition(f"unknown_state:{expected_state}")
    assert_no_secret(dict(fields), "fields")
    assert_no_secret(dict(detail or {}), "detail")

    args: list[Any] = [str(draft_id), str(tenant_id)]
    sets: list[str] = []
    for column, value in fields.items():
        args.append(_bind(column, value))
        sets.append(f"{column}=${len(args)}::{_UPDATABLE_COLUMNS[column]}")
    state_clause = ""
    if expected_state is not None:
        args.append(expected_state)
        state_clause = f" AND state=${len(args)}"
    sql = (
        f"UPDATE automation_drafts SET {', '.join(sets)}, updated_at=NOW() "
        f"WHERE id=$1::uuid AND tenant_id=$2::uuid{state_clause} RETURNING *"
    )
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchrow(sql, *args)
            if updated is None:
                current = await conn.fetchrow(
                    "SELECT state FROM automation_drafts WHERE id=$1::uuid AND tenant_id=$2::uuid",
                    str(draft_id), str(tenant_id),
                )
                if current is None:
                    raise DraftNotFound(str(draft_id))
                raise TransitionConflict(expected_state or "", current["state"])
            await conn.execute(
                """
                INSERT INTO automation_draft_events (draft_id, event_type, from_state, to_state, actor, detail)
                VALUES ($1::uuid, $2, $3, $3, $4, $5::jsonb)
                """,
                str(draft_id), event_type, updated["state"], actor,
                _dumps({"fields": sorted(fields), **dict(detail or {})}),
            )
    return dict(updated)


async def list_drafts(
    *, tenant_id: str, owner_user: str | None = None, limit: int = 200,
) -> list[dict[str, Any]]:
    """테넌트의 초안 목록(최근 갱신순). owner_user 를 주면 그 사람의 것만."""
    args: list[Any] = [str(tenant_id)]
    owner_clause = ""
    if owner_user is not None:
        args.append(owner_user)
        owner_clause = " AND owner_user=$2"
    args.append(max(1, min(int(limit), 500)))
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM automation_drafts WHERE tenant_id=$1::uuid{owner_clause} "
            f"ORDER BY updated_at DESC LIMIT ${len(args)}",
            *args,
        )
    return [dict(r) for r in rows]
