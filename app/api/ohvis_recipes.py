"""WorkRecipe 기록 API — 브라우저 조작을 녹화해 재생 가능한 레시피로 저장한다.

`app/services/work_recipe/recorder.py` 는 이미 main 에 있고 검증됐다 —
여기서는 그 공개 함수(`start_recording`/`finish_recording`)만 부르는
얇은 HTTP 표면을 얹는다. recorder 자체는 수정하지 않는다.

인증은 `ohvis_console.require_console_admin` 과 동일하다 — 레시피 기록도
CEO 운영 화면에서만 트리거되는 내부 관리 동작이라 내부 관리자로 막는다.

기록 상태(진행 중인 `WorkRecipeRecorder`)는 프로세스 메모리에만 있다.
녹화는 짧게(한 세션) 끝나므로 재시작/재배포로 유실돼도 다시 시작하면 된다.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.ohvis_console import TenantContext, require_console_admin
from app.services.work_recipe import recorder as recorder_module

router = APIRouter(prefix="/ohvis/recipes", tags=["ohvis-recipes"])

# recording_id -> (WorkRecipeRecorder, tenant_id). 프로세스 로컬 상태.
_ACTIVE_RECORDINGS: dict[str, tuple[recorder_module.WorkRecipeRecorder, str]] = {}


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])


def _decided_by(context: TenantContext) -> str:
    user = context.get("user") or {}
    return str(user.get("email") or user.get("id") or "unknown")


def _get_owned_recording(
    recording_id: str, tenant_id: str
) -> recorder_module.WorkRecipeRecorder:
    """존재하지 않거나 다른 테넌트 소유면 404 — 타 테넌트에 존재 여부를 흘리지 않는다."""
    entry = _ACTIVE_RECORDINGS.get(recording_id)
    if entry is None or entry[1] != tenant_id:
        raise HTTPException(status_code=404, detail="recording_not_found")
    return entry[0]


class RecordingStartIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=1, max_length=300)


class RecordingStepIn(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    succeeded: bool = True


class RecordingFinishIn(BaseModel):
    created_by: str = Field(default="", max_length=200)


@router.post("/recording")
async def start_recording(
    body: RecordingStartIn,
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    tenant_id = _tenant_id(context)
    try:
        recording = recorder_module.start_recording(body.name, body.domain, tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    recording_id = uuid4().hex
    _ACTIVE_RECORDINGS[recording_id] = (recording, tenant_id)
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
    recording = _get_owned_recording(recording_id, _tenant_id(context))
    return {
        "recording_id": recording_id,
        "name": recording.name,
        "domain": recording.domain,
        "step_count": len(recording.steps),
        "input_count": len(recording.inputs),
        "steps": [step.to_dict() for step in recording.steps],
    }


@router.post("/recording/{recording_id}/steps")
async def record_recording_step(
    recording_id: str,
    body: RecordingStepIn,
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    recording = _get_owned_recording(recording_id, _tenant_id(context))
    try:
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
    recording = _get_owned_recording(recording_id, _tenant_id(context))
    try:
        result = await recorder_module.finish_recording(
            recording, created_by=body.created_by or _decided_by(context)
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        _ACTIVE_RECORDINGS.pop(recording_id, None)
    return {"status": "saved", "recipe": result}
