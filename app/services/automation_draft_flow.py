"""자동화 초안 흐름 — API 와 에이전트 도구가 함께 부르는 단일 서비스 (설계서 6·7절).

- 상태 변경은 `automation_draft_service.transition()`, 그 밖의 필드는 `update_fields()` 로만 한다.
- 모든 응답은 `{draft, card, ...}` — 화면 문장은 `build_card()` 사전에서만 나온다.
- 권한: 일반 사용자는 자기 초안만, 관리자(owner/admin)는 테넌트의 모든 초안을 다룬다. 다른 테넌트 초안은 '없음'이다.
- `owner_user` 는 사람(이메일/사용자 id)이다. 채팅 세션 id 를 넣지 않는다.
- 시연 실행은 `automation_demo_runner`, 조각 분할·등록은 `automation_fragment_splitter` 가 한다(T3).
  여기서는 상태를 옮기고 실행기를 띄우며, 결과 확인 뒤 등록을 부른다.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core.db_pool import get_pool
from app.services import automation_credential_linker as linker
from app.services import automation_demo_runner as demo_runner
from app.services import automation_draft_service as svc
from app.services import automation_intent_parser as parser
from app.services import automation_fragment_splitter as splitter
from app.services import automation_schedule as schedule
from app.services import vault_secure_input
from app.services.work_recipe.store import list_recipes

MAX_INTENT_TEXT = 2000
MAX_NOTE = 500
CREDENTIAL_WAIT_MINUTES = vault_secure_input.REQUEST_TTL_MINUTES
PROGRESS_EVENT = demo_runner.PROGRESS_EVENT
# 로그인 정보를 바꾸지 않고는 다시 시연하지 않는 실패(로그인 반복 제출 금지, FR-25).
LOGIN_RETRY_BLOCKED = frozenset({"auth_failed", "challenge"})
# [아니에요] 이유 → 다시 해 보기 방식(FR-14, AC-04).
REDEMO_REASONS = frozenset({"wrong_date", "wrong_menu", "other"})
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_JSON_FIELDS = ("intent", "recording_ref", "evidence")
_SCHEDULABLE = frozenset({
    "intent_draft", "intent_confirmed", "credential_waiting", "credential_ready", "demonstrating",
    "paused_risk", "result_review", "revising", "manual_record", "registering", "pending_approval", "active",
})
_OUTPUT_ALIASES = {"표": "table", "파일": "file", "화면": "screen"}


class FlowError(Exception):
    """HTTP 상태로 옮길 수 있는 흐름 오류. code 는 기계용, 사용자 문장은 카드가 맡는다."""

    def __init__(self, status_code: int, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.extra = extra


@dataclass(frozen=True)
class Actor:
    tenant_id: str
    user: str
    is_admin: bool = False


def actor_from_context(context: dict[str, Any]) -> Actor:
    """인증 컨텍스트 → Actor. 사람(이메일 또는 사용자 id)이 없으면 만들지 않는다."""
    user = context.get("user") or {}
    membership = context.get("membership") or {}
    person = str(user.get("email") or user.get("id") or membership.get("user_id") or "").strip()
    if not person:
        raise FlowError(403, "user_required")
    role = str(membership.get("role") or "").lower()
    return Actor(str(context["tenant"]["id"]), person, role in ("owner", "admin"))


# ── 표현 ───────────────────────────────────────────────────────────────────

def _obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _norm(draft: dict[str, Any]) -> dict[str, Any]:
    row = dict(draft)
    for field in _JSON_FIELDS:
        row[field] = _obj(row.get(field))
    row["aliases"] = list(row.get("aliases") or [])
    return row


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def public_draft(draft: dict[str, Any]) -> dict[str, Any]:
    """화면에 내보내는 초안. 내부 식별(레인·등록 id·예약 id·세션)은 뺀다."""
    row = _norm(draft)
    intent = row["intent"]
    return {
        "id": str(row["id"]),
        "state": row["state"],
        "title": row.get("title") or "",
        "intent_text": row.get("intent_text") or "",
        "intent": {
            "site_name": intent.get("site_name") or "",
            "url": intent.get("url") or "",
            "domain": intent.get("domain") or "",
            "task": intent.get("task") or "",
            "output_kind": intent.get("output_kind") or "screen",
            "schedule": intent.get("schedule"),
            "needs_domain_confirm": bool(intent.get("needs_domain_confirm")),
            "login_required": intent.get("login_required") if isinstance(intent.get("login_required"), bool) else None,
        },
        "aliases": row["aliases"],
        "credential_id": str(row["credential_id"]) if row.get("credential_id") else None,
        "failure_code": row.get("failure_code"),
        "evidence": row["evidence"],
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _intent_fields(intent: dict[str, Any]) -> list[dict[str, str]]:
    """카드 1 에 보이는 '이렇게 이해했어요' 항목."""
    site = intent.get("site_name") or intent.get("domain") or "아직 모르겠어요"
    return [
        {"id": "site", "label": "사이트", "value": str(site)},
        {"id": "task", "label": "할 일", "value": str(intent.get("task") or "")},
        {"id": "output", "label": "결과", "value": parser.OUTPUT_LABELS.get(str(intent.get("output_kind")), "화면")},
        {"id": "when", "label": "언제", "value": parser.describe_schedule(intent.get("schedule"))},
    ]


def _card(draft: dict[str, Any]) -> dict[str, Any]:
    row = _norm(draft)
    card = svc.build_card(
        row["state"], title=row.get("title") or "", failure_code=row.get("failure_code"),
        no_login=row["intent"].get("login_required") is False and not row.get("credential_id"),
    )
    if row["state"] == "intent_draft":
        card["fields"] = _intent_fields(row["intent"])
        card["needs_domain_confirm"] = bool(row["intent"].get("needs_domain_confirm"))
    elif row["state"] == "paused_risk" and row["recording_ref"].get("paused_label"):
        card["paused_step"] = str(row["recording_ref"]["paused_label"])
    return card


def _launch_demo(draft_id: str, *, tenant_id: str) -> Any:
    """시연을 요청과 분리해 띄운다. 테스트는 이 함수를 바꿔 끼운다."""
    return demo_runner.launch(draft_id, tenant_id=tenant_id)


def view(draft: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"draft": public_draft(draft), "card": _card(draft), **extra}


# ── 로딩·권한 ──────────────────────────────────────────────────────────────

def _require_enabled() -> None:
    if not svc.drafts_enabled():
        raise FlowError(404, "feature_disabled")


def _uuid_or_404(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise FlowError(404, "draft_not_found") from None


async def _load(actor: Actor, draft_id: str) -> dict[str, Any]:
    draft = await svc.get_draft(_uuid_or_404(draft_id), tenant_id=actor.tenant_id)
    if draft is None:
        raise FlowError(404, "draft_not_found")
    if not actor.is_admin and str(draft.get("owner_user")) != actor.user:
        raise FlowError(403, "not_owner")
    return _norm(draft)


def _conflict(exc: svc.TransitionConflict) -> FlowError:
    return FlowError(409, "state_conflict", expected=exc.expected, actual=exc.actual)


def _need_state(draft: dict[str, Any], *allowed: str, expected: str | None = None) -> None:
    """클라이언트가 본 상태(expected)와 허용 상태를 모두 현재 상태와 맞춘다. 어긋나면 409."""
    state = draft["state"]
    if expected is not None and expected != state:
        raise FlowError(409, "state_conflict", expected=expected, actual=state)
    if state not in allowed:
        raise FlowError(409, "state_conflict", expected="|".join(allowed), actual=state)


async def _apply(coro: Any) -> dict[str, Any]:
    """서비스 호출 한 번을 흐름 오류로 옮긴다."""
    try:
        return _norm(await coro)
    except svc.TransitionConflict as exc:
        raise _conflict(exc) from None
    except svc.DraftNotFound:
        raise FlowError(404, "draft_not_found") from None
    except svc.InvalidTransition as exc:
        raise FlowError(409, "transition_not_allowed", detail=str(exc).split(":", 1)[0]) from None
    except svc.SecretDetected:
        raise FlowError(422, "secret_detected") from None


def _move(actor: Actor, draft_id: str, from_state: str, to_state: str, reason: str, **kw: Any) -> Any:
    return svc.transition(draft_id, from_state, to_state, actor.user, reason, tenant_id=actor.tenant_id, **kw)


def _set(actor: Actor, draft_id: str, expected: str | None, fields: dict[str, Any], event: str, **detail: Any) -> Any:
    return svc.update_fields(
        draft_id, tenant_id=actor.tenant_id, expected_state=expected, fields=fields,
        actor=actor.user, event_type=event, detail=detail or None,
    )


# ── 1. 시작·고치기·확정 ────────────────────────────────────────────────────

async def _check_session(actor: Actor, session_id: str) -> str:
    try:
        sid = str(uuid.UUID(str(session_id)))
    except ValueError:
        raise FlowError(422, "session_invalid") from None
    async with get_pool().acquire() as conn:
        found = await conn.fetchval(
            "SELECT 1 FROM chat_sessions WHERE id=$1::uuid AND tenant_id=$2::uuid", sid, actor.tenant_id,
        )
    if not found:
        raise FlowError(404, "session_not_found")
    return sid


async def start(actor: Actor, intent_text: str, session_id: str | None = None) -> dict[str, Any]:
    _require_enabled()
    text = " ".join(str(intent_text or "").split())
    if not 2 <= len(text) <= MAX_INTENT_TEXT:
        raise FlowError(422, "intent_text_invalid")
    sid = await _check_session(actor, session_id) if session_id else None
    try:
        recipes = await list_recipes(tenant_id=actor.tenant_id)
        intent = await parser.parse_intent(text, tenant_id=actor.tenant_id, recipe_rows=recipes)
    except svc.SecretDetected:
        raise FlowError(422, "secret_detected") from None
    title = parser.make_title(intent["site_name"], intent["task"], text)
    aliases = parser.make_aliases(intent["site_name"], intent["task"])
    draft = await _apply(svc.create_draft(
        tenant_id=actor.tenant_id, owner_user=actor.user, title=title, intent_text=text,
        intent=intent, session_id=sid, aliases=aliases, actor=actor.user,
    ))
    return view(draft)


def _clean_task(value: Any) -> str:
    task = " ".join(str(value or "").split())
    if not 1 <= len(task) <= parser.MAX_TASK:
        raise FlowError(422, "task_invalid")
    return task


def _clean_output(value: Any) -> str:
    code = _OUTPUT_ALIASES.get(str(value or "").strip(), str(value or "").strip().lower())
    if code not in parser.OUTPUT_KINDS:
        raise FlowError(422, "output_invalid")
    return code


async def edit_intent(actor: Actor, draft_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """changes 에는 클라이언트가 보낸 키만 있다(site/task/output/schedule). schedule=None 은 '한 번만'."""
    _require_enabled()
    draft = await _load(actor, draft_id)
    _need_state(draft, "intent_draft")
    intent = dict(draft["intent"])
    try:
        if "site" in changes:
            recipes = await list_recipes(tenant_id=actor.tenant_id)
            intent.update(parser.resolve_site_input(str(changes["site"] or ""), recipes))
        if "task" in changes:
            intent["task"] = _clean_task(changes["task"])
        if "output" in changes:
            intent["output_kind"] = _clean_output(changes["output"])
        if "schedule" in changes:
            intent["schedule"] = parser.normalize_schedule(changes["schedule"])
    except ValueError as exc:
        raise FlowError(422, str(exc)) from None
    fields = {
        "intent": intent,
        "title": parser.make_title(intent.get("site_name", ""), intent.get("task", ""), draft["intent_text"]),
        "aliases": parser.make_aliases(intent.get("site_name", ""), intent.get("task", "")),
    }
    updated = await _apply(_set(actor, draft_id, "intent_draft", fields, "intent_edited", changed=sorted(changes)))
    return view(updated)


async def confirm_intent(
    actor: Actor, draft_id: str, expected_state: str, domain_confirmed: bool = False,
) -> dict[str, Any]:
    _require_enabled()
    draft = await _load(actor, draft_id)
    _need_state(draft, "intent_draft", expected=expected_state)
    intent = draft["intent"]
    if not intent.get("domain"):
        raise FlowError(422, "domain_required")
    if intent.get("needs_domain_confirm") and not domain_confirmed:
        raise FlowError(422, "domain_confirmation_required")
    confirmed = await _apply(_move(actor, draft_id, "intent_draft", "intent_confirmed", "intent_confirmed"))
    return await _link_after_confirm(actor, confirmed)


async def _link_after_confirm(actor: Actor, draft: dict[str, Any]) -> dict[str, Any]:
    link = await linker.link_status(tenant_id=actor.tenant_id, domain=draft["intent"]["domain"])
    credential: dict[str, Any] = {"status": link["status"], "credentials": link["credentials"]}
    if link["status"] == "none":
        try:
            draft, requested = await _issue_request(actor, draft)
            credential = {"status": "requested", **requested}
        except FlowError as exc:
            credential["request_error"] = exc.code
    return view(draft, credential=credential)


# ── 2. 로그인 연결 ─────────────────────────────────────────────────────────

async def _issue_request(actor: Actor, draft: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """안전 입력 요청을 만들고 초안을 credential_waiting 으로 옮긴다. intent_confirmed 에서만."""
    _need_state(draft, "intent_confirmed")
    draft_id = str(draft["id"])
    session = str(draft.get("session_id") or "") or f"automation-draft:{draft_id}"
    url = draft["intent"].get("url") or f"https://{draft['intent']['domain']}"
    try:
        requested = await linker.request_new_input(
            tenant_id=actor.tenant_id, session_id=session, url=url, draft_id=draft_id,
        )
    except vault_secure_input.SecureInputError as exc:
        raise FlowError(exc.status_code if exc.status_code in (400, 404, 429) else 502, exc.code) from None
    await _apply(_set(
        actor, draft_id, "intent_confirmed", {"credential_request_id": requested["request_id"]},
        "credential_request_linked",
    ))
    waiting = await _apply(_move(actor, draft_id, "intent_confirmed", "credential_waiting", "credential_requested"))
    return waiting, requested


async def choose_credential(
    actor: Actor, draft_id: str, credential_id: str | None = None, request_new: bool = False,
    no_login: bool = False,
) -> dict[str, Any]:
    """credential_id · request_new · no_login 중 정확히 하나. no_login 은 사용자가 고른 경우에만 쓴다."""
    _require_enabled()
    if sum((bool(credential_id), bool(request_new), bool(no_login))) != 1:
        raise FlowError(422, "credential_choice_required")
    draft = await _load(actor, draft_id)
    if no_login:
        return await _proceed_without_login(actor, draft)
    if request_new:
        _need_state(draft, "intent_confirmed")
        waiting, requested = await _issue_request(actor, draft)
        return view(waiting, credential={"status": "requested", **requested})
    # 로그인 실패(auth_failed) 뒤에는 다시 연결만 허용한다 — 비밀번호를 고친 같은 계정 또는 다른 계정.
    relink = draft["state"] == "failed" and draft.get("failure_code") == "auth_failed"
    if not relink:
        _need_state(draft, "intent_confirmed", "credential_waiting")
    chosen = await linker.get_credential_for_domain(
        tenant_id=actor.tenant_id, credential_id=str(credential_id), domain=draft["intent"].get("domain") or "",
    )
    if chosen is None:
        raise FlowError(422, "credential_mismatch")
    state = draft["state"]
    fields: dict[str, Any] = {"credential_id": chosen["id"]}
    if draft["intent"].get("login_required") is False:
        # 로그인 없이 시작했다가 로그인 화면을 만난 초안: 로그인 단계가 없는 옛 계획·진행 기록은 버린다.
        fields["intent"] = {**draft["intent"], "login_required": True}
        fields["recording_ref"] = {
            k: v for k, v in draft["recording_ref"].items() if k not in ("plan", "completed", "recorded")
        }
    await _apply(_set(actor, draft_id, state, fields, "credential_linked_ref"))
    ready = await _apply(_move(actor, draft_id, state, "credential_ready", "credential_selected"))
    return view(ready, credential={"status": "linked", "display_name": chosen["display_name"]})


async def _proceed_without_login(actor: Actor, draft: dict[str, Any]) -> dict[str, Any]:
    """[로그인 없이 진행] — 사용자가 직접 고른 경우에만 로그인 연결을 건너뛴다. credential_id 는 비워 둔다."""
    _need_state(draft, "intent_confirmed", "credential_waiting")
    draft_id, state = str(draft["id"]), draft["state"]
    intent = {**draft["intent"], "login_required": False}
    await _apply(_set(actor, draft_id, state, {"intent": intent}, "login_not_required"))
    ready = await _apply(_move(actor, draft_id, state, "credential_ready", "credential_not_required"))
    return view(ready, credential={"status": "not_required"})


async def refresh_credential(actor: Actor, draft: dict[str, Any]) -> dict[str, Any]:
    """credential_waiting 초안을 보안 입력 요청의 현재 상태에 맞춘다(제출 → 준비됨, 취소·30분 경과 → 만료)."""
    if draft["state"] != "credential_waiting":
        return draft
    draft_id = str(draft["id"])
    request_id = draft.get("credential_request_id")
    progress = (
        await linker.request_progress(tenant_id=actor.tenant_id, request_id=str(request_id))
        if request_id else {"status": "unknown", "credential_id": None}
    )
    try:
        if progress["status"] in ("submitted", "verified") and progress["credential_id"]:
            await _apply(_set(
                actor, draft_id, "credential_waiting", {"credential_id": progress["credential_id"]},
                "credential_linked_ref",
            ))
            return await _apply(_move(actor, draft_id, "credential_waiting", "credential_ready", "credential_submitted"))
        updated = draft.get("updated_at")
        age_min = (datetime.now(timezone.utc) - updated).total_seconds() / 60 if isinstance(updated, datetime) else 0
        if progress["status"] in ("cancelled", "expired") or age_min > CREDENTIAL_WAIT_MINUTES:
            return await _apply(_move(actor, draft_id, "credential_waiting", "expired", "credential_not_submitted"))
    except FlowError as exc:
        if exc.code != "state_conflict":
            raise
        return await _load(actor, draft_id)
    return draft


async def get(actor: Actor, draft_id: str) -> dict[str, Any]:
    """현재 상태 + 카드 (앱 재실행·탭 전환 복구)."""
    _require_enabled()
    draft = await refresh_credential(actor, await _load(actor, draft_id))
    extra: dict[str, Any] = {}
    if draft["state"] == "intent_confirmed":
        link = await linker.link_status(tenant_id=actor.tenant_id, domain=draft["intent"].get("domain") or "")
        extra["credential"] = {"status": link["status"], "credentials": link["credentials"]}
    elif draft["state"] == "credential_waiting":
        extra["credential"] = {
            "status": "requested", "requestId": str(draft.get("credential_request_id") or ""),
            "request_id": str(draft.get("credential_request_id") or ""),
            "expires_in_minutes": CREDENTIAL_WAIT_MINUTES,
        }
    return view(draft, **extra)


# ── 3. 시연·위험 판단·결과 확인 ────────────────────────────────────────────

async def _begin_demo(actor: Actor, draft: dict[str, Any], reason: str, **ref_changes: Any) -> dict[str, Any]:
    """시연 1회를 센 뒤 demonstrating 으로 옮기고 실행기를 띄운다. 초안당 3회까지.

    [진행] 승인은 그 시연의 그 단계에만 유효하다 — 새 시연은 이전 승인을 하나도 가져가지 않는다.
    """
    draft_id, state = str(draft["id"]), draft["state"]
    attempts = int(draft.get("demo_attempts") or 0)
    if attempts >= demo_runner.MAX_DEMOS:
        raise FlowError(409, "demo_limit_reached", limit=demo_runner.MAX_DEMOS)
    ref = {k: v for k, v in draft["recording_ref"].items() if k != "approved_risk_seqs"}
    fields: dict[str, Any] = {
        "demo_attempts": attempts + 1,
        "recording_ref": {**ref, **ref_changes, "approved_risk": {}},
    }
    await _apply(_set(actor, draft_id, state, fields, "demo_attempt_counted", attempt=attempts + 1))
    moved = await _apply(_move(actor, draft_id, state, "demonstrating", reason))
    _launch_demo(draft_id, tenant_id=actor.tenant_id)
    return view(moved, progress_event=PROGRESS_EVENT)


async def start_demo(actor: Actor, draft_id: str, expected_state: str) -> dict[str, Any]:
    """credential_ready(처음) 또는 failed(다시) → demonstrating 후 시연 실행기를 띄운다.

    로그인 실패·사람 확인 요구로 멈춘 초안은 로그인을 다시 연결하기 전에는 다시 하지 않는다.
    """
    _require_enabled()
    draft = await _load(actor, draft_id)
    _need_state(draft, "credential_ready", "failed", expected=expected_state)
    if draft["state"] == "failed" and draft.get("failure_code") in LOGIN_RETRY_BLOCKED:
        raise FlowError(409, "login_retry_blocked", failure_code=draft["failure_code"])
    return await _begin_demo(actor, draft, "demo_requested")


_RISK_TARGET = {"proceed": "demonstrating", "stop_here": "result_review", "cancel": "cancelled"}


async def risk_decision(actor: Actor, draft_id: str, decision: str) -> dict[str, Any]:
    _require_enabled()
    target = _RISK_TARGET.get(decision)
    if target is None:
        raise FlowError(422, "decision_invalid")
    draft = await _load(actor, draft_id)
    _need_state(draft, "paused_risk")
    ref = draft["recording_ref"]
    paused = ref.get("paused_seq")
    step = next((s for s in ref.get("plan") or [] if paused is not None and int(s.get("seq") or 0) == int(paused)), None)
    if decision == "proceed" and step is not None:
        # [진행]은 멈춘 그 단계 하나에만, 이 시연 안에서만, 지금 내용 그대로일 때만 유효하다.
        approvals = ref.get("approved_risk") if isinstance(ref.get("approved_risk"), dict) else {}
        approved = {**approvals, str(int(paused)): demo_runner.step_fingerprint(step)}
        await _apply(_set(
            actor, draft_id, "paused_risk", {"recording_ref": {**ref, "approved_risk": approved}},
            "risk_step_approved", seq=int(paused),
        ))
    moved = await _apply(_move(actor, draft_id, "paused_risk", target, f"risk_{decision}"))
    if target != "demonstrating":
        return view(moved)
    _launch_demo(draft_id, tenant_id=actor.tenant_id)
    return view(moved, progress_event=PROGRESS_EVENT)


async def review(
    actor: Actor, draft_id: str, ok: bool, reason_code: str | None = None, note: str | None = None,
) -> dict[str, Any]:
    _require_enabled()
    if reason_code is not None and not _REASON_CODE.match(reason_code):
        raise FlowError(422, "reason_code_invalid")
    clean_note = " ".join(str(note or "").split())[:MAX_NOTE]
    draft = await _load(actor, draft_id)
    if ok and draft["state"] == "registering" and draft["evidence"].get("screen_verified"):
        return await _register(actor, draft)  # 등록이 중간에 실패했을 때 다시 부르는 입구
    _need_state(draft, "result_review")
    detail = {k: v for k, v in (("reason_code", reason_code), ("note", clean_note)) if v}
    if ok:
        evidence = {**draft["evidence"], "screen_verified": True}
        await _apply(_set(actor, draft_id, "result_review", {"evidence": evidence}, "result_verified"))
        moved = await _apply(_move(actor, draft_id, "result_review", "registering", "result_confirmed", detail=detail))
        return await _register(actor, moved)
    moved = await _apply(_move(actor, draft_id, "result_review", "revising", "result_rejected", detail=detail))
    return view(moved)


async def _register(actor: Actor, draft: dict[str, Any]) -> dict[str, Any]:
    """[맞아요] 뒤에만 부른다(FR-15). 조각을 나눠 등록하고 승인 대기 또는 사용 가능으로 옮긴다.

    시연 기록이 없는 초안(직접 보여주기 경로 등)은 registering 에 둔다 — 등록은 그 경로가 맡는다.
    """
    recorded_map = draft["recording_ref"].get("recorded") or {}
    if not recorded_map or not draft["evidence"].get("screen_verified"):
        return view(draft)
    recorded = [recorded_map[key] for key in sorted(recorded_map, key=int)]
    draft_id = str(draft["id"])
    try:
        result = await splitter.register_fragments(
            tenant_id=actor.tenant_id, owner_user=str(draft["owner_user"]), draft_id=draft_id,
            domain=draft["intent"].get("domain") or "", title=str(draft.get("title") or ""),
            credential_id=str(draft["credential_id"]) if draft.get("credential_id") else None,
            recorded=recorded, evidence=draft["evidence"], session_id=draft.get("session_id"),
        )
    except Exception as exc:  # noqa: BLE001 — 등록 실패는 registering 에 남기고 다시 부를 수 있게 한다
        raise FlowError(502, "registration_failed", detail=type(exc).__name__) from None
    await _apply(_set(
        actor, draft_id, "registering",
        {"recipe_names": result["recipe_names"], "registration_ids": result["registration_ids"],
         "max_risk": result["max_risk"]},
        "fragments_registered", reused=len(result["reused"]),
    ))
    moved = await _apply(_move(actor, draft_id, "registering", "pending_approval", "registered"))
    if result["all_approved"]:
        return await activate_on_approval(actor, draft_id)
    return view(moved)


def revise_plan(
    plan: list[dict[str, Any]], reason_code: str, hint: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[int]]:
    """이유에 맞는 단계만 고친다. 돌려주는 값: (새 계획, 바뀐 단계 번호)."""
    revised = [dict(step) for step in plan]
    if reason_code == "wrong_date":
        date = str(hint.get("date") or "")
        if not _DATE.match(date):
            raise FlowError(422, "date_required")
        targets = [step for step in revised if step.get("role") == "date"]
        if not targets:
            raise FlowError(409, "no_date_step")
        for step in targets:
            step["value"] = date
    elif reason_code == "wrong_menu":
        menu = " ".join(str(hint.get("menu") or "").split())[:40]
        if not menu or svc.find_secret(menu):
            raise FlowError(422, "menu_required")
        targets = [step for step in revised if step.get("role") == "menu"]
        if not targets:
            raise FlowError(409, "no_menu_step")
        for step in targets:
            step.update(selector=f"text={json.dumps(menu, ensure_ascii=False)}", label=menu)
    else:
        targets = []
    return revised, [int(step["seq"]) for step in targets]


async def redemo(actor: Actor, draft_id: str, reason_code: str, hint: dict[str, Any] | None = None) -> dict[str, Any]:
    """[아니에요] 뒤 이유에 맞춰 단계를 고치고 한 번 더 해 본다(revising → demonstrating)."""
    _require_enabled()
    if reason_code not in REDEMO_REASONS:
        raise FlowError(422, "reason_code_invalid")
    draft = await _load(actor, draft_id)
    _need_state(draft, "revising")
    ref = draft["recording_ref"]
    plan = list(ref.get("plan") or [])
    if not plan:
        raise FlowError(409, "nothing_to_redo")
    revised, changed = revise_plan(plan, reason_code, dict(hint or {}))
    first = min(changed) if changed else 1
    completed = [int(s) for s in ref.get("completed") or [] if int(s) < first]
    recorded = {k: v for k, v in (ref.get("recorded") or {}).items() if int(k) < first}
    return await _begin_demo(
        actor, draft, f"redemo_{reason_code}", plan=revised, completed=completed, recorded=recorded,
        revised_seqs=changed,
    )


async def manual_record(actor: Actor, context: dict[str, Any], draft_id: str) -> dict[str, Any]:
    """revising → manual_record 로 옮기고, manual_record 에서는 기존 녹화를 시작한다(관리자 전용)."""
    _require_enabled()
    draft = await _load(actor, draft_id)
    _need_state(draft, "revising", "manual_record")
    if draft["state"] == "revising":
        moved = await _apply(_move(actor, draft_id, "revising", "manual_record", "manual_record_requested"))
        return view(moved)
    if not actor.is_admin:
        raise FlowError(403, "admin_required")
    from app.api.ohvis_recipes import RecordingStartIn, start_recording

    domain = draft["intent"].get("domain") or ""
    slug = re.sub(r"[^a-z0-9]+", "_", domain.lower()).strip("_") or "site"
    try:
        started = await start_recording(
            RecordingStartIn(name=f"{slug}_manual_{str(draft['id'])[:8]}", domain=domain), context,
        )
    except Exception as exc:  # noqa: BLE001 — HTTPException(400) 등을 흐름 오류로 통일
        raise FlowError(502, "recording_start_failed", detail=type(exc).__name__) from None
    updated = await _apply(_set(
        actor, draft_id, "manual_record", {"recording_ref": {"recording_id": started["recording_id"]}},
        "recording_started",
    ))
    return view(updated, recording_id=started["recording_id"])


# ── 4. 예약 ────────────────────────────────────────────────────────────────

def _directive_of(draft: dict[str, Any]) -> str:
    names = draft.get("recipe_names") or []
    return str(names[-1]) if names else str(draft.get("title") or "")


async def set_schedule(actor: Actor, draft_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    """예약을 정한다. 승인 전에는 등록하되 멈춰 두고, 승인된(active) 초안은 바로 켠다."""
    _require_enabled()
    try:
        normalized = parser.normalize_schedule(spec)
    except ValueError as exc:
        raise FlowError(422, str(exc)) from None
    if normalized is None:
        raise FlowError(422, "schedule_invalid")
    draft = await _load(actor, draft_id)
    _need_state(draft, *sorted(_SCHEDULABLE))
    old_job = draft.get("schedule_job_id")
    try:
        if old_job:
            schedule.remove(old_job)
        registered = await schedule.register(
            draft_id=str(draft["id"]), tenant_id=actor.tenant_id, owner_user=draft["owner_user"],
            directive=_directive_of(draft), schedule=normalized, active=draft["state"] == "active",
        )
    except schedule.ScheduleUnavailable as exc:
        raise FlowError(503, str(exc)) from None
    intent = {**draft["intent"], "schedule": normalized}
    updated = await _apply(_set(
        actor, draft_id, draft["state"], {"intent": intent, "schedule_job_id": registered["job_id"]},
        "schedule_set",
    ))
    return view(
        updated,
        schedule={
            "text": parser.describe_schedule(normalized), "schedule_id": registered["job_id"],
            "active": draft["state"] == "active", "next_run": schedule.next_run_at(registered["job_id"]),
        },
    )


async def activate_on_approval(actor: Actor, draft_id: str) -> dict[str, Any]:
    """승인되면 pending_approval → active, 멈춰 둔 예약을 켠다. 승인 쪽(등록 단계)이 부르는 입구다."""
    draft = await _load(actor, draft_id)
    _need_state(draft, "pending_approval")
    job_id = draft.get("schedule_job_id")
    sched = draft["intent"].get("schedule")
    try:
        if job_id:
            schedule.resume(job_id)
        elif sched:
            registered = await schedule.register(
                draft_id=str(draft["id"]), tenant_id=actor.tenant_id, owner_user=draft["owner_user"],
                directive=_directive_of(draft), schedule=sched, active=True,
            )
            await _apply(_set(actor, draft_id, "pending_approval", {"schedule_job_id": registered["job_id"]}, "schedule_set"))
    except schedule.ScheduleUnavailable as exc:
        raise FlowError(503, str(exc)) from None
    moved = await _apply(_move(actor, draft_id, "pending_approval", "active", "approved"))
    return view(moved)


# ── 5. 사용 중인 자동화 (통합 목록의 동작) ─────────────────────────────────

def parse_key(key: str) -> tuple[str, str]:
    kind, _, ident = str(key or "").partition(":")
    if kind not in ("draft", "work", "browser", "ovis") or not ident:
        raise FlowError(404, "automation_not_found")
    return kind, _uuid_or_404(ident)


async def _active_draft(actor: Actor, key: str) -> dict[str, Any]:
    kind, ident = parse_key(key)
    if kind != "draft":
        raise FlowError(409, "not_supported_for_source")
    draft = await _load(actor, ident)
    _need_state(draft, "active")
    return draft


async def run_now(actor: Actor, context: dict[str, Any], key: str) -> dict[str, Any]:
    """저장된 자동화를 지금 실행한다(서버 레인). 사이트가 PC 레인을 요구하면 막힌 이유를 돌려준다."""
    _require_enabled()
    draft = await _active_draft(actor, key)
    from app.services.channel_router import ChannelRouter, directive_from_authenticated_context
    from app.services.work_recipe.approval import ApprovalRequired
    from app.services.work_recipe.orchestrator import run_directive

    directive = _directive_of(draft)
    intent = ChannelRouter().route_directive(
        directive_from_authenticated_context(
            context, session_id=f"automation-draft:{draft['id']}", correlation_id=str(uuid.uuid4()),
            payload={"directive": directive}, capabilities=frozenset({"recipe.execute"}),
            source="approved_recipe",
        ),
        capability="recipe.execute",
    )
    try:
        result = await run_directive(directive, actor.tenant_id, triggered_by=actor.user, action_intent=intent)
    except ApprovalRequired:
        raise FlowError(409, "approval_required") from None
    except ValueError as exc:
        raise FlowError(422, "run_not_allowed", detail=str(exc)[:80]) from None
    if result is None:
        raise FlowError(404, "recipe_not_found")
    run_id = getattr(result, "run_id", None)
    return view(draft, run={"status": str(getattr(result, "status", "")), "run_id": str(run_id) if run_id else None})


async def run_scheduled(action_config: dict[str, Any]) -> str:
    """스케줄러가 부르는 실행. 승인된 레시피 출처(approved_recipe)로 서버가 직접 의도를 만든다."""
    from app.services.channel_router import ChannelRouter, directive_from_authenticated_context
    from app.services.work_recipe.orchestrator import run_directive

    tenant_id, owner = str(action_config["tenant_id"]), str(action_config["owner_user"])
    directive = str(action_config["directive"])
    context = {"tenant": {"id": tenant_id}, "membership": {"user_id": owner}}
    intent = ChannelRouter().route_directive(
        directive_from_authenticated_context(
            context, session_id=f"automation-draft:{action_config['draft_id']}", correlation_id=str(uuid.uuid4()),
            payload={"directive": directive}, capabilities=frozenset({"recipe.execute"}),
            source="approved_recipe",
        ),
        capability="recipe.execute",
    )
    result = await run_directive(directive, tenant_id, triggered_by=f"schedule:{owner}", action_intent=intent)
    if result is None:
        return "recipe_not_found"
    return f"status={getattr(result, 'status', '')}"


async def pause_schedule(actor: Actor, key: str) -> dict[str, Any]:
    """예약만 멈춘다(상태표에 active 에서 되돌아오는 전이가 없어 상태는 그대로 active)."""
    _require_enabled()
    draft = await _active_draft(actor, key)
    if not draft.get("schedule_job_id"):
        raise FlowError(409, "no_schedule")
    try:
        schedule.pause(draft["schedule_job_id"])
    except schedule.ScheduleUnavailable as exc:
        raise FlowError(503, str(exc)) from None
    updated = await _apply(_set(actor, str(draft["id"]), "active", {"schedule_job_id": draft["schedule_job_id"]}, "schedule_paused"))
    return view(updated, schedule={"paused": True})


async def resume_schedule(actor: Actor, key: str) -> dict[str, Any]:
    _require_enabled()
    draft = await _active_draft(actor, key)
    if not draft.get("schedule_job_id"):
        raise FlowError(409, "no_schedule")
    try:
        schedule.resume(draft["schedule_job_id"])
    except schedule.ScheduleUnavailable as exc:
        raise FlowError(503, str(exc)) from None
    updated = await _apply(_set(actor, str(draft["id"]), "active", {"schedule_job_id": draft["schedule_job_id"]}, "schedule_resumed"))
    return view(updated, schedule={"paused": False, "next_run": schedule.next_run_at(draft["schedule_job_id"])})
