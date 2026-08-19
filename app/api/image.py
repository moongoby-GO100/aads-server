"""AADS media generation API."""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response, RedirectResponse
from pydantic import BaseModel, Field

from app.auth import require_internal_admin
from app.services.media_generation_service import _app_static_dir, media_generation_service

logger = logging.getLogger(__name__)

router = APIRouter()


class ImageRequest(BaseModel):
    prompt: str
    size: str = "1024x1024"
    model_id: str | None = None
    provider: str | None = None
    requested_by: str | None = None
    session_id: str | None = None
    aspect_ratio: str | None = None
    image_size: str | None = None
    reference_images: list[str] | None = None
    browser_work_key: str | None = None
    target_url: str | None = None


class EditImageRequest(BaseModel):
    prompt: str
    image_path: str | None = None
    image_url: str | None = None
    image_data: str | None = None
    mask_path: str | None = None
    size: str = "1024x1024"
    model_id: str | None = None
    provider: str | None = None
    requested_by: str | None = None
    session_id: str | None = None


class VideoRequest(BaseModel):
    prompt: str
    input_refs: dict = Field(default_factory=dict)
    model_id: str | None = None
    provider: str | None = None
    requested_by: str | None = None
    session_id: str | None = None


class VideoDownloadRequest(BaseModel):
    output_dir: str | None = None


class GensparkProcessRequest(BaseModel):
    job_id: str | None = None
    browser_session_id: str | None = None
    browser_work_key: str | None = None
    target_url: str | None = None
    timeout_seconds: int = Field(default=240, ge=30, le=900)


class GalleryApprovalRequest(BaseModel):
    reference_ids: list[int] = Field(default_factory=list)
    approve: bool = True
    approve_recommended: bool = False
    approved_by: str = "ceo_gallery"


class GalleryDeleteRequest(BaseModel):
    job_ids: list[str] = Field(default_factory=list)
    deleted_by: str = "ceo_gallery"


@router.post("/generate", dependencies=[Depends(require_internal_admin)])
async def generate_image(req: ImageRequest):
    """채팅창에서 이미지 생성"""
    logger.info("image_generate_request reference_images=%s model_id=%s aspect_ratio=%s image_size=%s", req.reference_images, req.model_id, req.aspect_ratio, req.image_size)
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="프롬프트를 입력하세요")
    try:
        result = await media_generation_service.generate_image(
            req.prompt,
            req.size,
            model_id=req.model_id,
            provider=req.provider,
            requested_by=req.requested_by,
            session_id=req.session_id,
            aspect_ratio=req.aspect_ratio,
            image_size=req.image_size,
            reference_images=req.reference_images,
            browser_work_key=req.browser_work_key,
            target_url=req.target_url,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"이미지 생성 실패: {e}")


@router.post("/edit", dependencies=[Depends(require_internal_admin)])
async def edit_image(req: EditImageRequest):
    """이미지 편집 job 생성/실행."""
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="프롬프트를 입력하세요")
    input_refs = {
        key: value
        for key, value in {
            "image_path": req.image_path,
            "image_url": req.image_url,
            "image_data": req.image_data,
            "mask_path": req.mask_path,
        }.items()
        if value
    }
    try:
        return await media_generation_service.edit_image(
            req.prompt,
            input_refs=input_refs,
            size=req.size,
            model_id=req.model_id,
            provider=req.provider,
            requested_by=req.requested_by,
            session_id=req.session_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"이미지 편집 실패: {e}")


@router.post("/video/generate", dependencies=[Depends(require_internal_admin)])
async def generate_video(req: VideoRequest):
    """비동기 동영상 생성 job 생성."""
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="프롬프트를 입력하세요")
    try:
        return await media_generation_service.generate_video(
            req.prompt,
            input_refs=req.input_refs,
            model_id=req.model_id,
            provider=req.provider,
            requested_by=req.requested_by,
            session_id=req.session_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"동영상 생성 실패: {e}")


@router.get("/video/{job_id}/status", dependencies=[Depends(require_internal_admin)])
async def video_status(job_id: str):
    """동영상 생성 job 상태 조회."""
    return await media_generation_service.video_status(job_id)


@router.post("/video/{job_id}/download", dependencies=[Depends(require_internal_admin)])
async def video_download(job_id: str, req: VideoDownloadRequest | None = None):
    """동영상 생성 결과를 안전 경로에 저장하고 메타데이터 반환."""
    return await media_generation_service.video_download(
        job_id,
        output_dir=req.output_dir if req else None,
    )


@router.post("/genspark-ui/process-next", dependencies=[Depends(require_internal_admin)])
async def process_genspark_ui_job(req: GensparkProcessRequest | None = None):
    """대기 중인 Genspark UI media job 1건을 Browser Bridge로 실행한다."""
    payload = req or GensparkProcessRequest()
    return await media_generation_service.process_genspark_ui_job(
        job_id=payload.job_id,
        browser_session_id=payload.browser_session_id,
        browser_work_key=payload.browser_work_key,
        target_url=payload.target_url,
        timeout_seconds=payload.timeout_seconds,
    )


# ── Gallery endpoints ──


@router.get("/gallery")
async def image_gallery(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = None,
):
    """생성된 이미지 갤러리 메타데이터 (base64 미포함)"""
    from app.core.db_pool import get_pool

    pool = get_pool()
    if pool is None:
        raise HTTPException(503, "DB 미연결")
    async with pool.acquire() as conn:
        base_where = "WHERE j.kind = 'image'"
        params: list = [limit, offset]
        count_where = "WHERE kind = 'image'"
        count_params: list = []
        if status:
            base_where += " AND j.status = $3"
            count_where += " AND status = $1"
            params.append(status)
            count_params.append(status)
        rows = await conn.fetch(
            f"""SELECT j.id, j.job_id, j.provider, j.model_id, j.status,
                       j.prompt, j.completed_at, j.created_at,
                       CASE WHEN j.result_uri IS NOT NULL THEN TRUE ELSE FALSE END AS has_image,
                       r.id AS reference_id,
                       r.ref_type AS reference_type,
                       r.angle_degree AS reference_angle_degree,
                       r.is_approved AS reference_is_approved,
                       COALESCE((r.metadata->>'approval_recommended')::boolean, false) AS approval_recommended,
                       (r.metadata->>'approval_recommendation_rank')::integer AS approval_recommendation_rank,
                       r.metadata->>'approval_recommendation_reason' AS approval_recommendation_reason,
                       r.metadata->>'reference_set' AS reference_set,
                       r.metadata->>'outfit' AS reference_outfit,
                       r.metadata->>'view' AS reference_view,
                       r.metadata->>'rear_ref_type' AS reference_rear_type,
                       r.metadata->>'style_preset_name' AS style_preset_name,
                       r.metadata->>'style_preset_slug' AS style_preset_slug,
                       r.metadata->>'trial_index' AS style_preset_trial_index
                FROM media_generation_jobs j
                LEFT JOIN LATERAL (
                    SELECT ref.id, ref.ref_type, ref.angle_degree, ref.is_approved, ref.metadata
                    FROM ai_persona_references ref
                    WHERE ref.media_job_id = j.job_id
                    ORDER BY ref.id DESC
                    LIMIT 1
                ) r ON true
                {base_where}
                ORDER BY j.id DESC
                LIMIT $1 OFFSET $2""",
            *params,
        )
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM media_generation_jobs {count_where}",
            *count_params,
        )
    return {
        "total": total,
        "items": [
            {
                "id": r["id"],
                "job_id": r["job_id"],
                "provider": r["provider"],
                "model_id": r["model_id"],
                "status": r["status"],
                "prompt": r["prompt"],
                "has_image": bool(r["has_image"]),
                "image_url": f"/api/v1/image/gallery/{r['job_id']}/image" if r["has_image"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "reference_id": r["reference_id"],
                "reference_type": r["reference_type"],
                "reference_angle_degree": r["reference_angle_degree"],
                "reference_is_approved": r["reference_is_approved"],
                "approval_recommended": bool(r["approval_recommended"]),
                "approval_recommendation_rank": r["approval_recommendation_rank"],
                "approval_recommendation_reason": r["approval_recommendation_reason"],
                "reference_set": r["reference_set"],
                "reference_outfit": r["reference_outfit"],
                "reference_view": r["reference_view"],
                "reference_rear_type": r["reference_rear_type"],
                "style_preset_name": r["style_preset_name"],
                "style_preset_slug": r["style_preset_slug"],
                "style_preset_trial_index": r["style_preset_trial_index"],
            }
            for r in rows
        ],
    }


@router.post("/gallery/approve", dependencies=[Depends(require_internal_admin)])
async def approve_gallery_references(req: GalleryApprovalRequest):
    """CEO 갤러리에서 AI 모델 reference 이미지를 승인/취소 처리."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    if pool is None:
        raise HTTPException(503, "DB 미연결")

    reference_ids = sorted({int(ref_id) for ref_id in req.reference_ids if int(ref_id) > 0})
    if not reference_ids and not req.approve_recommended:
        raise HTTPException(status_code=400, detail="승인할 reference_id가 없습니다")

    approval_metadata = {
        "approval_source": "gallery",
        "approval_action_at": datetime.now(timezone.utc).isoformat(),
        "approval_action_by": req.approved_by[:64] or "ceo_gallery",
    }
    if not req.approve:
        approval_metadata["approval_cancelled"] = True

    async with pool.acquire() as conn:
        if req.approve_recommended:
            rows = await conn.fetch(
                """
                UPDATE ai_persona_references
                   SET is_approved = $1,
                       approved_at = CASE WHEN $1 THEN NOW() ELSE NULL END,
                       metadata = COALESCE(metadata, '{}'::jsonb) || $2::jsonb
                 WHERE COALESCE((metadata->>'approval_recommended')::boolean, false) = true
                 RETURNING id, media_job_id, ref_type, angle_degree, is_approved
                """,
                req.approve,
                json.dumps(approval_metadata),
            )
        else:
            rows = await conn.fetch(
                """
                UPDATE ai_persona_references
                   SET is_approved = $1,
                       approved_at = CASE WHEN $1 THEN NOW() ELSE NULL END,
                       metadata = COALESCE(metadata, '{}'::jsonb) || $3::jsonb
                 WHERE id = ANY($2::bigint[])
                 RETURNING id, media_job_id, ref_type, angle_degree, is_approved
                """,
                req.approve,
                reference_ids,
                json.dumps(approval_metadata),
            )

    return {
        "approved": req.approve,
        "requested_count": len(reference_ids) if not req.approve_recommended else "recommended",
        "updated_count": len(rows),
        "items": [
            {
                "reference_id": r["id"],
                "job_id": r["media_job_id"],
                "reference_type": r["ref_type"],
                "angle_degree": r["angle_degree"],
                "is_approved": r["is_approved"],
            }
            for r in rows
        ],
    }


@router.post("/gallery/delete", dependencies=[Depends(require_internal_admin)])
async def delete_gallery_items(req: GalleryDeleteRequest):
    """CEO 갤러리에서 선택한 이미지 job과 연결 reference를 삭제."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    if pool is None:
        raise HTTPException(503, "DB 미연결")

    job_ids = sorted({job_id.strip() for job_id in req.job_ids if job_id and job_id.strip()})
    if not job_ids:
        raise HTTPException(status_code=400, detail="삭제할 job_id가 없습니다")

    async with pool.acquire() as conn:
        async with conn.transaction():
            ref_rows = await conn.fetch(
                """
                DELETE FROM ai_persona_references
                 WHERE media_job_id = ANY($1::text[])
                 RETURNING id, media_job_id, ref_type, angle_degree, is_approved
                """,
                job_ids,
            )
            job_rows = await conn.fetch(
                """
                DELETE FROM media_generation_jobs
                 WHERE kind = 'image'
                   AND job_id = ANY($1::text[])
                 RETURNING id, job_id, status, model_id
                """,
                job_ids,
            )

    return {
        "requested_count": len(job_ids),
        "deleted_jobs": len(job_rows),
        "deleted_references": len(ref_rows),
        "deleted_by": req.deleted_by[:64] or "ceo_gallery",
        "items": [
            {
                "id": r["id"],
                "job_id": r["job_id"],
                "status": r["status"],
                "model_id": r["model_id"],
            }
            for r in job_rows
        ],
    }


@router.get("/gallery/{job_id}/image")
async def get_gallery_image(job_id: str):
    """개별 이미지 바이너리 반환 — <img src=...>에서 직접 사용"""
    from app.core.db_pool import get_pool

    pool = get_pool()
    if pool is None:
        raise HTTPException(503, "DB 미연결")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT result_uri, result_path FROM media_generation_jobs WHERE job_id = $1",
            job_id,
        )
    if not row or not row["result_uri"]:
        raise HTTPException(404, "이미지 없음 또는 생성 중")
    uri: str = row["result_uri"]
    generated_root = (_app_static_dir() / "media" / "generated").resolve()

    def _safe_generated_file(candidate: str | Path | None) -> Path | None:
        if not candidate:
            return None
        try:
            path = Path(candidate).expanduser().resolve()
            path.relative_to(generated_root)
        except (OSError, RuntimeError, ValueError):
            return None
        return path if path.is_file() else None

    local_path = _safe_generated_file(row["result_path"])
    if local_path is None and uri.startswith("/static/media/generated/"):
        relative_uri = uri.removeprefix("/static/")
        local_path = _safe_generated_file(_app_static_dir() / relative_uri)
    if local_path is not None:
        media_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        return FileResponse(
            local_path,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )
    if uri.startswith("data:"):
        header, b64data = uri.split(",", 1)
        media_type = header.split(";")[0].replace("data:", "")
        return Response(
            content=base64.b64decode(b64data),
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )
    if uri.startswith(f"/api/v1/image/gallery/{job_id}/image"):
        raise HTTPException(404, "생성 이미지 파일을 찾을 수 없습니다")
    return RedirectResponse(uri)
