"""WorkRecipe 기록 API — 브라우저 조작을 녹화하고 승인 뒤 레시피로 등록한다.

`app/services/work_recipe/recorder.py` 는 이미 main 에 있고 검증됐다 —
완료 시에는 FR-17 dry-run과 B-scope 등록 승인 요청을 저장한다. 승인 전 draft는
`work_recipes`에 들어가지 않으므로 플레이어가 미승인 레시피를 실행할 수 없다.

인증은 `ohvis_console.require_console_admin` 과 동일하다 — 레시피 기록도
CEO 운영 화면에서만 트리거되는 내부 관리 동작이라 내부 관리자로 막는다.

기록 상태는 DB에 보존하고 테넌트별 행 잠금으로 슬롯 간 요청을 직렬화한다.
"""
from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.ohvis_console import TenantContext, require_console_admin
from app.services.work_recipe import recorder as recorder_module
from app.services.work_recipe import recording_store
from app.services.work_recipe.registration import (
    RegistrationError,
    decide_registration_as_human,
    get_registration,
    list_registrations,
    registration_error_status,
    registration_summary,
)

router = APIRouter(prefix="/ohvis/recipes", tags=["ohvis-recipes"])


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])


def _decided_by(context: TenantContext) -> str:
    user = context.get("user") or {}
    return str(user.get("email") or user.get("id") or "unknown")


class RecordingStartIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=1, max_length=300)
    lane: Literal["server", "pc"] = "server"
    browser_work_key: str = Field(default="", max_length=200)
    credential_scope: dict[str, Any] = Field(default_factory=dict)


class RecordingStepIn(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    succeeded: bool = True
    credential_scope: dict[str, Any] | None = None


class RecordingFinishIn(BaseModel):
    created_by: str = Field(default="", max_length=200)


class RegistrationDecisionIn(BaseModel):
    # `status` is the dashboard/API contract; `decision` remains accepted for
    # recorder clients that shipped before the approval screen.
    status: str = Field(default="", max_length=20, description="approved | rejected")
    decision: str = Field(default="", max_length=20, description="approve | reject (legacy)")
    reason: str = Field(default="", max_length=1000)
    # 승인자는 인증된 사용자로만 정해진다 — 본문에 decided_by 를 실어도 읽지 않는다.
    confirm_high_risk: bool = Field(
        default=False, description="max_risk 가 WRITE_EXTERNAL 이상인 approve 에 필수"
    )


@router.post("/recording")
async def start_recording(
    body: RecordingStartIn,
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    tenant_id = _tenant_id(context)
    try:
        recording = recorder_module.start_recording(
            body.name, body.domain, tenant_id,
            credential_bindings_required=True,
            flow_metadata={"lane": body.lane, "browser_work_key": body.browser_work_key,
                           **({"credential_scope": body.credential_scope} if body.credential_scope else {})},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    recording_id = uuid4().hex
    await recording_store.create(recording_id, recording)
    return {
        "recording_id": recording_id,
        "name": recording.name,
        "domain": recording.domain,
        "step_count": 0,
    }


@router.get("/recording/{recording_id}")
async def get_recording_status(
    recording_id: str,
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    async with recording_store.owned(recording_id, _tenant_id(context)) as recording:
        return {
            "recording_id": recording_id,
            "name": recording.name,
            "domain": recording.domain,
            "step_count": len(recording.steps),
            "input_count": len(recording.inputs),
            "metadata": recording.flow_metadata,
            "steps": [step.to_dict() for step in recording.steps],
        }


@router.post("/recording/{recording_id}/steps")
async def record_recording_step(
    recording_id: str,
    body: RecordingStepIn,
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    async with recording_store.owned(recording_id, _tenant_id(context)) as recording:
        try:
            if body.credential_scope is not None:
                recording.set_credential_scope(body.credential_scope)
            step = recording.record_step(body.payload, succeeded=body.succeeded)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "recording_id": recording_id,
            "step": step.to_dict() if step else None,
            "step_count": len(recording.steps),
        }


@router.post("/recording/{recording_id}/finish")
async def finish_recording(
    recording_id: str,
    body: RecordingFinishIn,
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    async with recording_store.owned(recording_id, _tenant_id(context)) as recording:
        try:
            result = await recorder_module.finish_recording(
                recording, created_by=body.created_by or _decided_by(context)
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "approval_required", "registration": result}


@router.get("/registrations")
async def list_registration_requests(
    status: str = "pending",
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    """Return registration requests for the current tenant only."""
    normalized_status = status.strip().lower()
    if normalized_status not in {"pending", "approved", "rejected"}:
        raise HTTPException(status_code=422, detail="invalid_registration_status")
    rows = await list_registrations(
        tenant_id=_tenant_id(context), status=normalized_status
    )
    # 대시보드용 필드는 추가만 한다 — 기존 행 필드가 항상 우선한다.
    return {"registrations": [{**registration_summary(row), **row} for row in rows]}


@router.get("/registrations/{registration_id}")
async def get_registration_status(
    registration_id: str,
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    row = await get_registration(registration_id, tenant_id=_tenant_id(context))
    if row is None:
        raise HTTPException(status_code=404, detail="registration_not_found")
    return {"registration": row}


@router.post("/registrations/{registration_id}/decision")
async def decide_registration_request(
    registration_id: str,
    body: RegistrationDecisionIn,
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    decision = (body.status or body.decision).strip().lower()
    decision = {"approved": "approve", "rejected": "reject"}.get(decision, decision)
    try:
        result = await decide_registration_as_human(
            registration_id,
            tenant_id=_tenant_id(context),
            decision=decision,
            principal=context,
            reason=body.reason,
            confirm_high_risk=body.confirm_high_risk,
        )
    except RegistrationError as exc:
        raise HTTPException(
            status_code=registration_error_status(exc), detail=str(exc)
        ) from exc
    return {"status": result["status"], "registration": result}
