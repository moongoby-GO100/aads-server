"""
Visual QA API 엔드포인트.

POST /api/v1/visual-qa/capture      — 스크린샷 촬영
POST /api/v1/visual-qa/compare      — baseline 대비 비교
POST /api/v1/visual-qa/set-baseline — 현재 스크린샷을 baseline으로 설정
GET  /api/v1/visual-qa/baselines/{project_id} — baseline 목록 조회
"""
from typing import List, Optional

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, HttpUrl, field_validator

from app.services.visual_qa import visual_qa_service

logger = structlog.get_logger()
router = APIRouter(prefix="/visual-qa", tags=["visual-qa"])


# ---------------------------------------------------------------------------
# Request / Response 모델
# ---------------------------------------------------------------------------

class CaptureRequest(BaseModel):
    url: str
    pages: List[str] = ["/"]
    project_id: str

    @field_validator("pages")
    @classmethod
    def pages_must_start_with_slash(cls, v: List[str]) -> List[str]:
        result = []
        for p in v:
            if not p.startswith("/"):
                p = "/" + p
            result.append(p)
        return result


class CaptureResultItem(BaseModel):
    page: str
    page_name: str
    path: Optional[str]
    success: bool
    error: Optional[str] = None


class CaptureResponse(BaseModel):
    project_id: str
    screenshots: List[CaptureResultItem]
    total: int
    success_count: int


class CompareRequest(BaseModel):
    project_id: str
    page_name: str


class CompareResponse(BaseModel):
    project_id: str
    page_name: str
    match: bool
    diff_percent: float
    diff_image_path: Optional[str]
    current_path: Optional[str]
    baseline_path: Optional[str]
    error: Optional[str] = None


class SetBaselineRequest(BaseModel):
    project_id: str
    page_name: str
    screenshot_path: Optional[str] = None  # None이면 최신 스크린샷 자동 선택


class SetBaselineResponse(BaseModel):
    project_id: str
    page_name: str
    baseline_path: str


class BaselineItem(BaseModel):
    page_name: str
    path: str
    size_bytes: int
    created_at: str


class BaselinesResponse(BaseModel):
    project_id: str
    baselines: List[BaselineItem]
    total: int


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------

@router.post("/capture", response_model=CaptureResponse)
async def capture_screenshots(req: CaptureRequest):
    """headless Playwright으로 지정 페이지 스크린샷 촬영."""
    logger.info("api_capture", url=req.url, pages=req.pages, project_id=req.project_id)
    try:
        results = await visual_qa_service.capture_screenshots(
            base_url=req.url,
            pages=req.pages,
            project_id=req.project_id,
        )
    except Exception as e:
        logger.error("api_capture_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"스크린샷 촬영 실패: {e}")

    items = [
        CaptureResultItem(
            page=r.page,
            page_name=r.page_name,
            path=r.path,
            success=r.success,
            error=r.error,
        )
        for r in results
    ]
    success_count = sum(1 for i in items if i.success)
    return CaptureResponse(
        project_id=req.project_id,
        screenshots=items,
        total=len(items),
        success_count=success_count,
    )


@router.post("/compare", response_model=CompareResponse)
async def compare_with_baseline(req: CompareRequest):
    """최신 스크린샷과 baseline을 Pillow pixelmatch로 비교."""
    logger.info("api_compare", project_id=req.project_id, page_name=req.page_name)

    current_path = visual_qa_service.get_latest_screenshot(req.project_id, req.page_name)
    if not current_path:
        raise HTTPException(
            status_code=404,
            detail=f"스크린샷 없음: project={req.project_id}, page={req.page_name}",
        )

    from app.services.visual_qa import BASELINES_DIR
    baseline_path = str(BASELINES_DIR / req.project_id / f"{req.page_name}_baseline.png")

    try:
        result = await visual_qa_service.compare_with_baseline(current_path, baseline_path)
    except Exception as e:
        logger.error("api_compare_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"비교 실패: {e}")

    return CompareResponse(
        project_id=req.project_id,
        page_name=req.page_name,
        match=result.match,
        diff_percent=result.diff_percent,
        diff_image_path=result.diff_image_path,
        current_path=current_path,
        baseline_path=baseline_path,
        error=result.error,
    )


@router.post("/set-baseline", response_model=SetBaselineResponse)
async def set_baseline(req: SetBaselineRequest):
    """현재 스크린샷(또는 지정 경로)을 baseline으로 설정."""
    logger.info("api_set_baseline", project_id=req.project_id, page_name=req.page_name)

    screenshot_path = req.screenshot_path
    if not screenshot_path:
        screenshot_path = visual_qa_service.get_latest_screenshot(req.project_id, req.page_name)
    if not screenshot_path:
        raise HTTPException(
            status_code=404,
            detail=f"스크린샷 없음: project={req.project_id}, page={req.page_name}",
        )

    try:
        baseline_path = await visual_qa_service.save_as_baseline(
            screenshot_path=screenshot_path,
            project_id=req.project_id,
            page_name=req.page_name,
        )
    except Exception as e:
        logger.error("api_set_baseline_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"baseline 저장 실패: {e}")

    return SetBaselineResponse(
        project_id=req.project_id,
        page_name=req.page_name,
        baseline_path=baseline_path,
    )


@router.get("/baselines/{project_id}", response_model=BaselinesResponse)
async def list_baselines(project_id: str):
    """등록된 baseline 목록 조회."""
    logger.info("api_list_baselines", project_id=project_id)
    try:
        items = await visual_qa_service.list_baselines(project_id)
    except Exception as e:
        logger.error("api_list_baselines_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"baseline 목록 조회 실패: {e}")

    return BaselinesResponse(
        project_id=project_id,
        baselines=[BaselineItem(**i) for i in items],
        total=len(items),
    )
