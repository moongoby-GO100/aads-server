"""
Visual QA API 엔드포인트.

POST /api/v1/visual-qa/capture           — 스크린샷 촬영
POST /api/v1/visual-qa/compare           — baseline 대비 비교
POST /api/v1/visual-qa/set-baseline      — 현재 스크린샷을 baseline으로 설정
GET  /api/v1/visual-qa/baselines/{project_id} — baseline 목록 조회
POST /api/v1/visual-qa/audit             — LLM 디자인 감리 (T-025)
GET  /api/v1/visual-qa/audit/{project_id}/latest — 최근 감리 결과 조회 (T-025)
"""
from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, HttpUrl, field_validator

from app.services.visual_qa import visual_qa_service
from app.services.design_auditor import design_auditor

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


# ---------------------------------------------------------------------------
# T-025: LLM 디자인 감리 (DesignAuditor)
# ---------------------------------------------------------------------------

class AuditRequest(BaseModel):
    """POST /visual-qa/audit 요청 모델."""
    project_id: str
    url: str
    pages: List[str] = ["/"]
    project_context: str = ""

    @field_validator("pages")
    @classmethod
    def pages_must_start_with_slash(cls, v: List[str]) -> List[str]:
        result = []
        for p in v:
            if not p.startswith("/"):
                p = "/" + p
            result.append(p)
        return result


class CategoryScoreItem(BaseModel):
    score: int
    issues: List[str]
    fixes: List[str]


class AuditResultItem(BaseModel):
    screenshot_path: Optional[str]
    scores: Dict[str, CategoryScoreItem]
    total_score: int
    verdict: str
    summary: str
    critical_issues: List[str]
    error: Optional[str] = None
    audited_at: str


class AuditResponse(BaseModel):
    project_id: str
    results: List[AuditResultItem]
    report_markdown: str
    total_pages: int
    pass_count: int
    conditional_count: int
    fail_count: int
    error_count: int


class LatestAuditResponse(BaseModel):
    project_id: str
    found: bool
    audit: Optional[AuditResultItem] = None
    message: str = ""


@router.post("/audit", response_model=AuditResponse)
async def run_design_audit(req: AuditRequest):
    """
    T-025: 지정 URL/페이지 스크린샷 촬영 후 LLM 디자인 감리 실행.

    1. Playwright로 스크린샷 촬영
    2. Gemini 2.5 Flash Vision → (fallback) Claude Sonnet Vision으로 검수
    3. 5개 항목 스코어카드 + 판정 반환
    4. experience_memory에 결과 저장
    """
    logger.info("api_audit_start", project_id=req.project_id, url=req.url, pages=req.pages)

    # 1. 스크린샷 촬영
    try:
        screenshot_results = await visual_qa_service.capture_screenshots(
            base_url=req.url,
            pages=req.pages,
            project_id=req.project_id,
        )
    except Exception as e:
        logger.error("api_audit_capture_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"스크린샷 촬영 실패: {e}")

    # 성공한 스크린샷만 감리
    valid_paths = [r.path for r in screenshot_results if r.success and r.path]
    if not valid_paths:
        raise HTTPException(status_code=422, detail="촬영 성공한 스크린샷 없음")

    # 2. LLM 감리
    try:
        audit_results = await design_auditor.audit_multiple(
            screenshot_paths=valid_paths,
            project_context=req.project_context or f"project_id={req.project_id}",
        )
    except Exception as e:
        logger.error("api_audit_llm_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"LLM 감리 실패: {e}")

    # 3. 보고서 생성
    try:
        report_md = await design_auditor.generate_report(audit_results)
    except Exception as e:
        logger.warning("api_audit_report_error", error=str(e))
        report_md = "보고서 생성 실패"

    # 4. 응답 조립
    result_items = []
    for ar in audit_results:
        result_items.append(AuditResultItem(
            screenshot_path=ar.screenshot_path,
            scores={
                k: CategoryScoreItem(score=v.score, issues=v.issues, fixes=v.fixes)
                for k, v in ar.scores.items()
            },
            total_score=ar.total_score,
            verdict=ar.verdict,
            summary=ar.summary,
            critical_issues=ar.critical_issues,
            error=ar.error,
            audited_at=ar.audited_at,
        ))

    pass_count = sum(1 for r in audit_results if r.verdict == "PASS")
    conditional_count = sum(1 for r in audit_results if r.verdict == "CONDITIONAL")
    fail_count = sum(1 for r in audit_results if r.verdict == "FAIL")
    error_count = sum(1 for r in audit_results if r.verdict == "ERROR")

    logger.info(
        "api_audit_done",
        project_id=req.project_id,
        total=len(audit_results),
        pass_count=pass_count,
        fail_count=fail_count,
    )

    return AuditResponse(
        project_id=req.project_id,
        results=result_items,
        report_markdown=report_md,
        total_pages=len(audit_results),
        pass_count=pass_count,
        conditional_count=conditional_count,
        fail_count=fail_count,
        error_count=error_count,
    )


@router.get("/audit/{project_id}/latest", response_model=LatestAuditResponse)
async def get_latest_audit(project_id: str):
    """
    T-025: 프로젝트의 최근 감리 결과를 experience_memory에서 조회.
    """
    logger.info("api_audit_latest", project_id=project_id)

    try:
        from app.memory.store import memory_store

        # experience_memory에서 design_audit 타입으로 조회
        # project_id 필터는 content JSON 내부 검색
        async with memory_store.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT content, created_at
                FROM experience_memory
                WHERE experience_type = 'design_audit'
                  AND content::text LIKE $1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                f"%{project_id}%",
            )
    except Exception as e:
        logger.warning("api_audit_latest_db_error", error=str(e))
        return LatestAuditResponse(
            project_id=project_id,
            found=False,
            message=f"DB 조회 실패: {e}",
        )

    if not rows:
        return LatestAuditResponse(
            project_id=project_id,
            found=False,
            message="감리 이력 없음",
        )

    import json as _json
    row = rows[0]
    content = _json.loads(row["content"]) if isinstance(row["content"], str) else dict(row["content"])

    scores_raw = content.get("scores", {})
    scores_items = {
        k: CategoryScoreItem(
            score=v.get("score", 0),
            issues=v.get("issues", []),
            fixes=v.get("fixes", []),
        )
        for k, v in scores_raw.items()
    }

    audit_item = AuditResultItem(
        screenshot_path=content.get("screenshot_path"),
        scores=scores_items,
        total_score=content.get("total_score", 0),
        verdict=content.get("verdict", "UNKNOWN"),
        summary=content.get("summary", ""),
        critical_issues=content.get("critical_issues", []),
        error=content.get("error"),
        audited_at=content.get("audited_at", row["created_at"].isoformat()),
    )

    return LatestAuditResponse(
        project_id=project_id,
        found=True,
        audit=audit_item,
    )
