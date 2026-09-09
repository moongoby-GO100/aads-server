"""OHVIS 내부 LangSmith-compatible LLMOps API.

GET  /api/v1/ohvis/llmops/status               — foundation 상태 + export 게이트
GET  /api/v1/ohvis/llmops/traces               — trace 검색 (v1 폴백 포함)
GET  /api/v1/ohvis/llmops/traces/{trace_id}    — trace 상세 (span/tool call/feedback)
GET  /api/v1/ohvis/llmops/candidates           — 실패/저품질 승격 후보
POST /api/v1/ohvis/llmops/datasets/from-trace  — trace를 eval example로 승격
POST /api/v1/ohvis/llmops/evals/run            — rule evaluator 오프라인 평가 실행
GET  /api/v1/ohvis/llmops/evals/{experiment_id}— 평가 결과 조회
POST /api/v1/ohvis/llmops/feedback             — trace 피드백 기록

인증은 기존 OHVIS API(`ohvis_harness`, `ohvis_tasks`)와 동일하게 라우터 수준
의존성 없이 앱 미들웨어에 맡긴다.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.auth import require_internal_admin
from app.services import llmops_evaluator, llmops_store

router = APIRouter()
logger = logging.getLogger(__name__)

# 외부 발신자(GO100)가 보내는 값에만 적용하는 엄격 규칙.
# - ID: 공백 없는 출력 가능 ASCII만 (원문 ID는 변형 없이 그대로 저장된다)
# - 텍스트: 제어문자 금지 (NUL은 TEXT/JSONB 저장 자체가 실패한다)
# - 라벨: 소문자 슬러그만 (원장 집계·대시보드 필터가 깨지지 않게 한다)
_INGEST_ID_RE = re.compile(r"^[\x21-\x7e]+$")
_INGEST_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_INGEST_LABEL_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_INGEST_CLIENT_ID_RE = r"^[a-z0-9][a-z0-9._-]+$"


def _reject_control_characters(value: Optional[str], field_name: str) -> Optional[str]:
    if value is not None and _INGEST_CONTROL_RE.search(value):
        raise ValueError(f"{field_name} must not contain control characters")
    return value


class DatasetFromTraceRequest(BaseModel):
    trace_id: str = Field(..., min_length=8, max_length=64)
    dataset_slug: str = Field(llmops_store.DEFAULT_FAILURE_DATASET, min_length=1, max_length=120)
    project: Optional[str] = Field(None, max_length=40)
    force: bool = Field(False, description="성공/고품질 trace도 강제로 승격한다")


class EvalRunRequest(BaseModel):
    dataset_slug: Optional[str] = Field(None, max_length=120)
    dataset_id: Optional[str] = Field(None, max_length=64)
    name: str = Field("", max_length=200)
    candidate_sha: Optional[str] = Field(None, max_length=64)
    model_id: Optional[str] = Field(None, max_length=120)
    limit: int = Field(200, ge=1, le=1000)
    created_by: Optional[str] = Field(None, max_length=120)


class FeedbackRequest(BaseModel):
    trace_id: Optional[str] = Field(None, max_length=64)
    source_ref: Optional[str] = Field(None, max_length=200)
    rating: Optional[int] = Field(None, ge=-1, le=5)
    label: Optional[str] = Field(None, max_length=60)
    comment: str = Field("", max_length=2000)
    created_by: Optional[str] = Field(None, max_length=120)


class TraceIngestToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(..., min_length=1, max_length=120)
    risk_tier: str = Field("read", min_length=1, max_length=30, pattern=_INGEST_LABEL_RE.pattern)
    approval_state: str = Field(
        "not_required", min_length=1, max_length=30, pattern=_INGEST_LABEL_RE.pattern
    )
    status: str = Field("success", min_length=1, max_length=30, pattern=_INGEST_LABEL_RE.pattern)
    sequence: int = Field(0, ge=0, le=10_000)
    input_summary: str = Field("", max_length=2000)
    output_summary: str = Field("", max_length=2000)
    latency_ms: Optional[int] = Field(None, ge=0, le=86_400_000)
    error: Optional[str] = Field(None, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tool_name", "input_summary", "output_summary", "error")
    @classmethod
    def validate_tool_call_text(cls, value: Optional[str], info) -> Optional[str]:
        return _reject_control_characters(value, str(info.field_name))


class TraceIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    project: Literal["GO100"]
    external_trace_id: str = Field(..., min_length=1, max_length=200)
    graph_run_id: Optional[str] = Field(None, min_length=1, max_length=200)
    session_id: Optional[str] = Field(None, max_length=64)
    run_type: str = Field("chain", min_length=1, max_length=60, pattern=_INGEST_LABEL_RE.pattern)
    status: Literal["success", "error", "cancelled", "running"] = "success"
    model: Optional[str] = Field(None, max_length=120)
    input_summary: str = Field("", max_length=2000)
    output_summary: str = Field("", max_length=2000)
    latency_ms: Optional[int] = Field(None, ge=0, le=86_400_000)
    cost_usd: Optional[float] = Field(None, ge=0, le=1_000_000)
    quality_score: Optional[float] = Field(None, ge=0, le=1)
    error: Optional[str] = Field(None, max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    metadata: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[TraceIngestToolCall] = Field(default_factory=list, max_length=llmops_store.MAX_INGEST_TOOL_CALLS)

    @field_validator("external_trace_id", "graph_run_id")
    @classmethod
    def validate_external_trace_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("external_trace_id must be an exact printable identifier")
        # 원문 ID는 변형 없이 저장되므로 저장 가능한 문자만 받는다. 공백·제어문자·
        # 비ASCII를 허용하면 멱등 키와 원장 조회 키가 서로 어긋난다.
        if not _INGEST_ID_RE.match(value):
            raise ValueError("identifier must be printable ASCII without whitespace")
        return value

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        # 저장 단계에서 조용히 버려지지 않도록 입력 시점에 거른다.
        try:
            uuid.UUID(value)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("session_id must be a UUID") from exc
        return value

    @field_validator("model", "input_summary", "output_summary", "error")
    @classmethod
    def validate_text_fields(cls, value: Optional[str], info) -> Optional[str]:
        return _reject_control_characters(value, str(info.field_name))

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        if any(not item or len(item) > 60 for item in values):
            raise ValueError("tags must be 1..60 characters")
        for item in values:
            _reject_control_characters(item, "tags")
        return values

    @model_validator(mode="after")
    def validate_total_size(self):
        try:
            llmops_store.validate_ingest_payload_size(self.model_dump(mode="json"))
        except ValueError as exc:
            raise ValueError("payload exceeds 65536 bytes") from exc
        return self


class IngestClientRequest(BaseModel):
    client_id: str = Field(..., min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    project: Literal["GO100"]


@router.get("/ohvis/llmops/status", tags=["ohvis-llmops"])
async def llmops_status(project: Optional[str] = Query(None, max_length=40)):
    return await llmops_store.get_status(project=project)


def _bearer_token(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="trace ingest credential required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="trace ingest credential required")
    return token


async def require_trace_ingest_client(
    authorization: Optional[str] = Header(None),
) -> dict[str, str]:
    """Authenticate the sender before FastAPI validates the request body."""
    token = _bearer_token(authorization)
    try:
        client = await llmops_store.authenticate_ingest_client(token)
    except Exception as exc:
        logger.warning("trace ingest authentication unavailable: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="trace ingest authentication temporarily unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid trace ingest credential",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return client


def _body_validation_error(errors: list[dict[str, Any]]) -> RequestValidationError:
    """Reuse FastAPI's own 422 shape for the manually parsed ingest body."""
    return RequestValidationError(errors)


async def parse_trace_ingest_body(request: Request) -> TraceIngestRequest:
    """Read and validate the body only after the credential check has passed.

    FastAPI decodes a declared body parameter *before* solving dependencies, so
    a malformed JSON body would answer 422 to an unauthenticated caller and let
    it probe the schema. Parsing here keeps 401 strictly first.
    """
    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit():
        if int(declared_length) > llmops_store.MAX_INGEST_PAYLOAD_BYTES:
            raise _body_validation_error(
                [
                    {
                        "type": "value_error",
                        "loc": ("body",),
                        "msg": f"payload exceeds {llmops_store.MAX_INGEST_PAYLOAD_BYTES} bytes",
                        "input": {},
                    }
                ]
            )
    raw = await request.body()
    if len(raw) > llmops_store.MAX_INGEST_PAYLOAD_BYTES:
        raise _body_validation_error(
            [
                {
                    "type": "value_error",
                    "loc": ("body",),
                    "msg": f"payload exceeds {llmops_store.MAX_INGEST_PAYLOAD_BYTES} bytes",
                    "input": {},
                }
            ]
        )
    try:
        parsed = json.loads(raw) if raw.strip() else None
    except ValueError as exc:
        raise _body_validation_error(
            [
                {
                    "type": "json_invalid",
                    "loc": ("body",),
                    "msg": "JSON decode error",
                    "input": {},
                    "ctx": {"error": str(exc)},
                }
            ]
        ) from exc
    try:
        return TraceIngestRequest.model_validate(parsed)
    except ValidationError as exc:
        raise _body_validation_error(
            [{**error, "loc": ("body", *error.get("loc", ()))} for error in exc.errors()]
        ) from exc


@router.post(
    "/ohvis/llmops/trace-ingest",
    tags=["ohvis-llmops-ingest"],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": TraceIngestRequest.model_json_schema()}},
        }
    },
)
async def llmops_trace_ingest_endpoint(
    request: Request,
    client: dict[str, str] = Depends(require_trace_ingest_client),
):
    return await llmops_trace_ingest(await parse_trace_ingest_body(request), client=client)


async def llmops_trace_ingest(
    req: TraceIngestRequest,
    client: dict[str, str],
):
    """Scope-check and store one authenticated external trace."""
    if client["project"] != req.project:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="credential project scope mismatch")
    try:
        result = await llmops_store.ingest_external_trace(
            req.model_dump(mode="json"), client_id=client["client_id"]
        )
    except Exception as exc:
        logger.warning("trace ingest store unavailable: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="trace ingest temporarily unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    try:
        await llmops_store.mark_ingest_client_used(client["client_id"])
    except Exception as exc:
        logger.warning("trace ingest client usage stamp failed: %s", type(exc).__name__)
    return result


@router.post(
    "/ohvis/llmops/ingest-clients",
    tags=["ohvis-llmops-ingest"],
    dependencies=[Depends(require_internal_admin)],
)
async def llmops_provision_ingest_client(req: IngestClientRequest):
    return await llmops_store.provision_ingest_client(
        client_id=req.client_id,
        project=req.project,
        created_by="internal-admin",
    )


@router.post(
    "/ohvis/llmops/ingest-clients/{client_id}/rotate",
    tags=["ohvis-llmops-ingest"],
    dependencies=[Depends(require_internal_admin)],
)
async def llmops_rotate_ingest_client(
    client_id: str = Path(..., min_length=3, max_length=80, pattern=_INGEST_CLIENT_ID_RE),
):
    # 경로 파라미터를 먼저 검증하지 않으면 잘못된 client_id가 모델 생성 단계에서
    # 처리되지 않은 ValidationError(500)로 새어나간다.
    req = IngestClientRequest(client_id=client_id, project="GO100")
    return await llmops_store.provision_ingest_client(
        client_id=req.client_id,
        project=req.project,
        created_by="internal-admin",
    )


@router.delete(
    "/ohvis/llmops/ingest-clients/{client_id}",
    tags=["ohvis-llmops-ingest"],
    dependencies=[Depends(require_internal_admin)],
)
async def llmops_revoke_ingest_client(
    client_id: str = Path(..., min_length=3, max_length=80, pattern=_INGEST_CLIENT_ID_RE),
):
    revoked = await llmops_store.revoke_ingest_client(client_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="active ingest client not found")
    return {"revoked": True, "client_id": client_id}


@router.get("/ohvis/llmops/traces", tags=["ohvis-llmops"])
async def llmops_list_traces(
    project: Optional[str] = Query(None, max_length=40),
    status: Optional[str] = Query(None, max_length=30),
    graph_run_id: Optional[str] = Query(None, max_length=200),
    session_id: Optional[str] = Query(None, max_length=64),
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(50, ge=1, le=llmops_store.MAX_LIMIT),
    include_legacy: bool = Query(True),
):
    return await llmops_store.list_traces(
        project=project,
        status=status,
        graph_run_id=graph_run_id,
        session_id=session_id,
        hours=hours,
        limit=limit,
        include_legacy=include_legacy,
    )


@router.get("/ohvis/llmops/candidates", tags=["ohvis-llmops"])
async def llmops_promotion_candidates(
    project: Optional[str] = Query(None, max_length=40),
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(20, ge=1, le=100),
):
    candidates = await llmops_store.find_promotion_candidates(
        project=project, hours=hours, limit=limit
    )
    return {"candidates": candidates, "count": len(candidates), "quality_floor": llmops_store.QUALITY_FLOOR}


@router.get("/ohvis/llmops/traces/{trace_id}", tags=["ohvis-llmops"])
async def llmops_get_trace(trace_id: str):
    trace = await llmops_store.get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return trace


@router.post("/ohvis/llmops/datasets/from-trace", tags=["ohvis-llmops"])
async def llmops_promote_trace(req: DatasetFromTraceRequest):
    result = await llmops_store.promote_trace_to_dataset(
        req.trace_id,
        dataset_slug=req.dataset_slug,
        project=req.project,
        force=req.force,
    )
    if not result.get("promoted") and result.get("reason") == "trace_not_found":
        raise HTTPException(status_code=404, detail="trace not found")
    return result


@router.post("/ohvis/llmops/evals/run", tags=["ohvis-llmops"])
async def llmops_run_eval(req: EvalRunRequest):
    if not req.dataset_slug and not req.dataset_id:
        raise HTTPException(status_code=400, detail="dataset_slug 또는 dataset_id가 필요합니다")
    try:
        result = await llmops_evaluator.run_experiment(
            dataset_slug=req.dataset_slug,
            dataset_id=req.dataset_id,
            name=req.name,
            candidate_sha=req.candidate_sha,
            model_id=req.model_id,
            limit=req.limit,
            created_by=req.created_by,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"eval failed: {str(exc)[:200]}") from exc
    if not result.get("ok") and result.get("reason") == "dataset_not_found":
        raise HTTPException(status_code=404, detail="dataset not found")
    return result


@router.get("/ohvis/llmops/evals/{experiment_id}", tags=["ohvis-llmops"])
async def llmops_get_eval(experiment_id: str):
    experiment = await llmops_evaluator.get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return experiment


@router.post("/ohvis/llmops/feedback", tags=["ohvis-llmops"])
async def llmops_record_feedback(req: FeedbackRequest):
    ok = await llmops_store.record_feedback(
        trace_id=req.trace_id,
        source_ref=req.source_ref,
        rating=req.rating,
        label=req.label,
        comment=req.comment,
        created_by=req.created_by,
    )
    return {"recorded": ok}
