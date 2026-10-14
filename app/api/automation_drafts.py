"""대화형 업무 자동화 등록 API (설계서 6절).

- 모든 응답에 `card` 가 있다. 화면 문장은 서버의 카드 사전에서만 나온다.
- `AUTOMATION_DRAFTS_ENABLED` 가 꺼져 있으면 모든 경로가 404 — 인증보다 먼저 확인한다.
- 일반 사용자는 자기 초안만, 관리자(owner/admin)는 테넌트 전체를 다룬다. 다른 테넌트는 '없음'(404).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import TenantRole, require_tenant_role
from app.services import automation_draft_flow as flow
from app.services import automation_draft_service as svc

TenantContext = dict[str, Any]
require_member = require_tenant_role(TenantRole.MEMBER)


def _flag_on() -> None:
    if not svc.drafts_enabled():
        raise HTTPException(status_code=404, detail={"error": "automation_drafts_disabled"})


router = APIRouter(
    prefix="/automation-drafts", tags=["automation-drafts"], dependencies=[Depends(_flag_on)],
)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftStartIn(_Body):
    intent_text: str = Field(min_length=2, max_length=flow.MAX_INTENT_TEXT)
    session_id: str | None = Field(default=None, max_length=64)


class DraftPatchIn(_Body):
    site: str | None = Field(default=None, max_length=200)
    task: str | None = Field(default=None, max_length=200)
    output: str | None = Field(default=None, max_length=20)
    schedule: dict[str, Any] | None = None


class ConfirmIn(_Body):
    expected_state: str = Field(max_length=40)
    domain_confirmed: bool = False


class CredentialIn(_Body):
    credential_id: str | None = Field(default=None, max_length=64)
    request_new: bool = False


class StateIn(_Body):
    expected_state: str = Field(max_length=40)


class RiskIn(_Body):
    decision: Literal["proceed", "stop_here", "cancel"]


class ReviewIn(_Body):
    ok: bool
    reason_code: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=flow.MAX_NOTE)


class ScheduleIn(_Body):
    kind: Literal["once", "daily", "weekdays", "weekly"]
    hour: int = Field(ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    day_of_week: str | None = Field(default=None, max_length=3)


def _actor(context: TenantContext) -> flow.Actor:
    try:
        return flow.actor_from_context(context)
    except flow.FlowError as exc:
        raise _http(exc) from None


def _http(exc: flow.FlowError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"error": exc.code, **exc.extra})


async def _run(coro: Any) -> Any:
    try:
        return await coro
    except flow.FlowError as exc:
        raise _http(exc) from None
    except svc.DraftsDisabled:
        raise HTTPException(status_code=404, detail={"error": "automation_drafts_disabled"}) from None


@router.post("", status_code=201)
async def create_draft(body: DraftStartIn, context: TenantContext = Depends(require_member)) -> dict[str, Any]:
    """문장 하나로 초안을 만든다(카드 1)."""
    return await _run(flow.start(_actor(context), body.intent_text, body.session_id))


@router.get("/{draft_id}")
async def get_draft(draft_id: str, context: TenantContext = Depends(require_member)) -> dict[str, Any]:
    """현재 상태와 카드. 앱을 다시 열거나 탭을 바꿔도 여기서 이어진다."""
    return await _run(flow.get(_actor(context), draft_id))


@router.patch("/{draft_id}")
async def patch_draft(
    draft_id: str, body: DraftPatchIn, context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    """카드 1 의 사이트·할 일·결과·언제를 고친다. 보낸 항목만 바뀐다."""
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail={"error": "no_changes"})
    return await _run(flow.edit_intent(_actor(context), draft_id, changes))


@router.post("/{draft_id}/confirm-intent")
async def confirm_intent(
    draft_id: str, body: ConfirmIn, context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    return await _run(flow.confirm_intent(_actor(context), draft_id, body.expected_state, body.domain_confirmed))


@router.post("/{draft_id}/credential")
async def choose_credential(
    draft_id: str, body: CredentialIn, context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    """기존 계정을 고르거나(credential_id) 새 계정 입력을 요청한다(request_new)."""
    return await _run(flow.choose_credential(_actor(context), draft_id, body.credential_id, body.request_new))


@router.post("/{draft_id}/demo", status_code=202)
async def start_demo(
    draft_id: str, body: StateIn, context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    """시연을 시작한다. 진행은 `automation_draft.progress` 이벤트로 흐른다."""
    return await _run(flow.start_demo(_actor(context), draft_id, body.expected_state))


@router.post("/{draft_id}/risk-decision")
async def risk_decision(
    draft_id: str, body: RiskIn, context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    return await _run(flow.risk_decision(_actor(context), draft_id, body.decision))


@router.post("/{draft_id}/review")
async def review(
    draft_id: str, body: ReviewIn, context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    """결과 확인(카드 5) — 맞아요(ok=true) 또는 아니에요(ok=false)."""
    return await _run(flow.review(_actor(context), draft_id, body.ok, body.reason_code, body.note))


@router.post("/{draft_id}/manual-record")
async def manual_record(draft_id: str, context: TenantContext = Depends(require_member)) -> dict[str, Any]:
    return await _run(flow.manual_record(_actor(context), context, draft_id))


@router.post("/{draft_id}/schedule")
async def set_schedule(
    draft_id: str, body: ScheduleIn, context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    return await _run(flow.set_schedule(_actor(context), draft_id, body.model_dump(exclude_none=True)))
