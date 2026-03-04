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


# ---------------------------------------------------------------------------
# T-026: Full QA 파이프라인 (코드 테스트 + Visual Regression + 디자인 감리)
# ---------------------------------------------------------------------------

class FullQARequest(BaseModel):
    """POST /visual-qa/full-qa 요청 모델."""
    project_id: str
    deploy_url: str
    pages: List[str] = ["/"]
    notify_ceo: bool = True
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


class VisualDetailItem(BaseModel):
    page: str
    page_name: str
    status: str
    diff_percent: Optional[float] = None
    diff_image_path: Optional[str] = None
    error: Optional[str] = None


class FullQAResponse(BaseModel):
    project_id: str
    deploy_url: str
    pages: List[str]
    test_status: str
    visual_status: str
    visual_details: List[VisualDetailItem]
    design_score: int
    design_verdict: str
    scorecard: Optional[Dict[str, Any]] = None
    screenshots: List[str]
    diff_images: List[str]
    verdict: str
    report_markdown: str
    executed_at: str
    ceo_notify_result: Optional[Dict[str, Any]] = None


@router.post("/full-qa", response_model=FullQAResponse)
async def run_full_qa(req: FullQARequest):
    """
    T-026: 종합 QA 파이프라인 실행.

    코드 테스트 결과(state 외부이므로 SKIP) + 스크린샷 + Visual Regression +
    LLM 디자인 감리를 순차 실행하고 AUTO PASS / CEO 확인 요청 / AUTO FAIL 판정 반환.

    판정 기준:
      - 테스트 PASS + Visual PASS + 디자인 35+ → AUTO PASS
      - 테스트 PASS + (Visual diff있음 OR 디자인 25-34) → CEO 확인 요청
      - 테스트 FAIL OR 디자인 24 이하 → AUTO FAIL
    """
    logger.info(
        "api_full_qa_start",
        project_id=req.project_id,
        deploy_url=req.deploy_url,
        pages=req.pages,
    )

    try:
        from app.services.qa_pipeline import run_full_qa as _run_full_qa
        qa_result = await _run_full_qa(
            project_id=req.project_id,
            deploy_url=req.deploy_url,
            pages=req.pages,
            existing_test_results=None,  # API 호출 시 기존 테스트 없음 → SKIP
            project_context=req.project_context,
        )
    except Exception as e:
        logger.error("api_full_qa_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"QA 파이프라인 실패: {e}")

    # CEO 알림
    ceo_notify_result: Optional[Dict[str, Any]] = None
    if req.notify_ceo:
        try:
            from app.services.ceo_notify import notify_ceo
            ceo_notify_result = await notify_ceo(
                project_id=req.project_id,
                qa_result=qa_result,
                screenshots=qa_result.get("screenshots", []),
                scorecard=qa_result.get("scorecard"),
            )
        except Exception as e:
            logger.warning("api_full_qa_ceo_notify_error", error=str(e))
            ceo_notify_result = {"error": str(e)}

    logger.info(
        "api_full_qa_done",
        project_id=req.project_id,
        verdict=qa_result.get("verdict"),
        design_score=qa_result.get("design_score"),
    )

    visual_detail_items = [
        VisualDetailItem(**d) for d in qa_result.get("visual_details", [])
    ]

    return FullQAResponse(
        project_id=qa_result["project_id"],
        deploy_url=qa_result["deploy_url"],
        pages=qa_result["pages"],
        test_status=qa_result["test_status"],
        visual_status=qa_result["visual_status"],
        visual_details=visual_detail_items,
        design_score=qa_result["design_score"],
        design_verdict=qa_result["design_verdict"],
        scorecard=qa_result.get("scorecard"),
        screenshots=qa_result.get("screenshots", []),
        diff_images=qa_result.get("diff_images", []),
        verdict=qa_result["verdict"],
        report_markdown=qa_result.get("report_markdown", ""),
        executed_at=qa_result["executed_at"],
        ceo_notify_result=ceo_notify_result,
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


# ---------------------------------------------------------------------------
# T-027: ShortFlow 영상 품질 게이트 + 자동 보정 루프
# ---------------------------------------------------------------------------

import asyncio as _asyncio
import base64 as _base64
import json as _json_mod
import os as _os
import re as _re
import subprocess as _subprocess
import tempfile as _tempfile
from pathlib import Path as _Path

VIDEO_QA_PROMPT = """
당신은 숏폼 영상 품질 심사관입니다.
첨부된 영상 프레임들을 아래 6개 기준으로 검수하세요.

[평가 항목] (각 10점 만점, 총 60점)
1. subtitle_readability (자막 가독성): 폰트 크기, 배경박스, 색상 대비, 가독성 (10점)
2. background_quality (배경 품질): 해상도, 선명도, 스타일 적합성 (10점)
3. composition (구도/레이아웃): 세이프존, 텍스트 위치, 여백 균형 (10점)
4. brand_consistency (브랜드 일관성): 색상 팔레트, 톤앤매너, 스타일 통일 (10점)
5. visual_consistency (시각 일관성): 전체 프레임 간 스타일 통일성 (10점)
6. polish (완성도): 전체적인 완성도, 깨진 요소 없음, 세련됨 (10점)

[출력 형식 - 반드시 JSON]
{
  "scores": {
    "subtitle_readability": {"score": 8, "issues": ["..."], "fixes": ["..."]},
    "background_quality": {"score": 7, "issues": ["..."], "fixes": ["..."]},
    "composition": {"score": 9, "issues": [], "fixes": []},
    "brand_consistency": {"score": 8, "issues": ["..."], "fixes": ["..."]},
    "visual_consistency": {"score": 7, "issues": ["..."], "fixes": ["..."]},
    "polish": {"score": 8, "issues": ["..."], "fixes": ["..."]}
  },
  "total_score": 47,
  "match_percent": 78.3,
  "summary": "한 줄 요약",
  "critical_issues": ["즉시 수정 필요 항목"]
}

판정 기준: AUTO_PUBLISH(85%+, 51점+) / CONDITIONAL(70-84%, 42-50점) / AUTO_REJECT(70% 미만, 41점 이하)
"""


async def _extract_video_frames(video_path: str, num_frames: int = 5) -> List[str]:
    """FFmpeg으로 영상에서 프레임 추출 → base64 목록 반환."""
    with _tempfile.TemporaryDirectory() as tmpdir:
        frame_pattern = _os.path.join(tmpdir, "frame_%03d.jpg")

        # 영상 길이 확인
        probe_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", video_path,
        ]
        try:
            probe_proc = await _asyncio.create_subprocess_exec(
                *probe_cmd,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
            )
            stdout, _ = await _asyncio.wait_for(probe_proc.communicate(), timeout=30)
            probe_data = _json_mod.loads(stdout.decode())
            duration = float(probe_data.get("format", {}).get("duration", 10))
        except Exception:
            duration = 10.0

        # 균등 간격 프레임 추출
        interval = max(duration / (num_frames + 1), 0.5)
        cmd = [
            "ffmpeg", "-i", video_path,
            "-vf", f"fps=1/{interval:.2f}",
            "-vframes", str(num_frames),
            "-q:v", "2",
            frame_pattern,
            "-y",
        ]
        try:
            proc = await _asyncio.create_subprocess_exec(
                *cmd,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
            )
            await _asyncio.wait_for(proc.communicate(), timeout=60)
        except Exception as e:
            raise RuntimeError(f"FFmpeg 프레임 추출 실패: {e}")

        # 추출된 프레임 → base64
        frames_b64 = []
        for fname in sorted(_os.listdir(tmpdir)):
            if fname.endswith(".jpg"):
                fpath = _os.path.join(tmpdir, fname)
                with open(fpath, "rb") as f:
                    frames_b64.append(_base64.b64encode(f.read()).decode("utf-8"))

        if not frames_b64:
            raise RuntimeError(f"프레임 추출 결과 없음: {video_path}")

        return frames_b64


async def _run_video_qa_llm(frames_b64: List[str], prompt: str) -> str:
    """Gemini Vision (primary) → Claude Vision (fallback)으로 영상 QA."""
    import os as _os2
    import base64 as _b64

    # Primary: Gemini
    try:
        import google.generativeai as genai
        import PIL.Image
        import io

        api_key = _os2.getenv("GOOGLE_API_KEY", "")
        if not api_key:
            raise ValueError("GOOGLE_API_KEY not set")
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")

        contents = [prompt]
        for b64 in frames_b64[:5]:
            image_bytes = _b64.b64decode(b64)
            pil_image = PIL.Image.open(io.BytesIO(image_bytes))
            contents.append(pil_image)

        loop = _asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: model.generate_content(contents))
        return response.text
    except Exception as e:
        logger.warning("video_qa_gemini_failed", error=str(e))

    # Fallback: Claude
    try:
        import anthropic
        api_key = _os2.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        client = anthropic.AsyncAnthropic(api_key=api_key)

        content_blocks: List[dict] = []
        for b64 in frames_b64[:3]:
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
            })
        content_blocks.append({"type": "text", "text": prompt})

        message = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2048,
            messages=[{"role": "user", "content": content_blocks}],
        )
        return message.content[0].text
    except Exception as e:
        raise RuntimeError(f"모든 Vision LLM 실패: {e}")


def _extract_video_qa_json(text: str) -> dict:
    m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, _re.DOTALL)
    if m:
        return _json_mod.loads(m.group(1))
    m = _re.search(r"\{.*\}", text, _re.DOTALL)
    if m:
        return _json_mod.loads(m.group(0))
    raise ValueError(f"JSON 없음: {text[:200]}")


def _calc_video_verdict(match_percent: float) -> str:
    if match_percent >= 85.0:
        return "AUTO_PUBLISH"
    if match_percent >= 70.0:
        return "CONDITIONAL"
    return "AUTO_REJECT"


# ---------------------------------------------------------------------------
# Quality Gate Request / Response 모델
# ---------------------------------------------------------------------------

class QualityGateRequest(BaseModel):
    project_id: str
    video_path: str
    video_id: str
    channel_name: str
    auto_correct: bool = True


class QualityGateResponse(BaseModel):
    action: str          # publish | ceo_review | re-render | reject | error
    project_id: str
    video_id: str
    channel_name: str
    verdict: str         # AUTO_PUBLISH | CONDITIONAL | AUTO_REJECT
    match_percent: float
    total_score: int
    scores: Dict[str, Any]
    summary: str
    critical_issues: List[str]
    corrections: Optional[Dict[str, Any]] = None
    screenshots: Optional[List[str]] = None
    error: Optional[str] = None


class ExtractSpecRequest(BaseModel):
    project_id: str
    channel_name: str
    benchmark_frames: List[str]  # base64 이미지 목록


class ExtractSpecResponse(BaseModel):
    project_id: str
    channel_name: str
    spec: Dict[str, Any]
    ffmpeg_params: Dict[str, Any]
    saved: bool
    error: Optional[str] = None


class BenchmarkSpecResponse(BaseModel):
    project_id: str
    channel_name: str
    found: bool
    spec: Optional[Dict[str, Any]] = None
    message: str = ""


# ---------------------------------------------------------------------------
# Quality Gate 엔드포인트
# ---------------------------------------------------------------------------

@router.post("/quality-gate", response_model=QualityGateResponse)
async def video_quality_gate(req: QualityGateRequest):
    """
    T-027: ShortFlow 영상 품질 게이트.

    1. FFmpeg으로 프레임 추출
    2. 벤치마크 사양 조회
    3. LLM Vision으로 6항목 심사
    4. match_percent 계산 → 판정
    5. AUTO_REJECT + auto_correct → 보정 파라미터 생성 + 재렌더링 지시서 반환
    """
    logger.info(
        "api_quality_gate_start",
        project_id=req.project_id,
        video_path=req.video_path,
        video_id=req.video_id,
        channel_name=req.channel_name,
    )

    from app.services.benchmark_spec import benchmark_spec_extractor
    from app.services.auto_correction import auto_corrector

    # 1. 영상 파일 존재 확인
    if not _Path(req.video_path).exists():
        logger.error("quality_gate_video_not_found", path=req.video_path)
        return QualityGateResponse(
            action="error",
            project_id=req.project_id,
            video_id=req.video_id,
            channel_name=req.channel_name,
            verdict="ERROR",
            match_percent=0.0,
            total_score=0,
            scores={},
            summary=f"영상 파일 없음: {req.video_path}",
            critical_issues=[f"파일 없음: {req.video_path}"],
            error=f"영상 파일 없음: {req.video_path}",
        )

    # 2. FFmpeg 프레임 추출
    try:
        frames_b64 = await _extract_video_frames(req.video_path, num_frames=5)
        logger.info("quality_gate_frames_extracted", count=len(frames_b64))
    except Exception as e:
        logger.error("quality_gate_frame_extraction_failed", error=str(e))
        return QualityGateResponse(
            action="error",
            project_id=req.project_id,
            video_id=req.video_id,
            channel_name=req.channel_name,
            verdict="ERROR",
            match_percent=0.0,
            total_score=0,
            scores={},
            summary=f"프레임 추출 실패: {e}",
            critical_issues=[str(e)],
            error=str(e),
        )

    # 3. 벤치마크 사양 조회
    benchmark_spec = await benchmark_spec_extractor.get_spec(req.project_id, req.channel_name)
    if not benchmark_spec:
        logger.warning("quality_gate_no_benchmark_spec", project_id=req.project_id, channel=req.channel_name)
        benchmark_spec = {}

    # 4. LLM Vision 심사
    try:
        raw_text = await _run_video_qa_llm(frames_b64, VIDEO_QA_PROMPT)
        qa_data = _extract_video_qa_json(raw_text)
    except Exception as e:
        logger.error("quality_gate_llm_failed", error=str(e))
        return QualityGateResponse(
            action="error",
            project_id=req.project_id,
            video_id=req.video_id,
            channel_name=req.channel_name,
            verdict="ERROR",
            match_percent=0.0,
            total_score=0,
            scores={},
            summary=f"LLM 심사 실패: {e}",
            critical_issues=[str(e)],
            error=str(e),
        )

    scores = qa_data.get("scores", {})
    total_score = int(qa_data.get("total_score", sum(
        v.get("score", 0) if isinstance(v, dict) else v for v in scores.values()
    )))
    # match_percent: total_score / 60 * 100
    match_percent = round(qa_data.get("match_percent", (total_score / 60.0) * 100), 2)
    summary = qa_data.get("summary", "")
    critical_issues = qa_data.get("critical_issues", [])

    verdict = _calc_video_verdict(match_percent)

    logger.info(
        "quality_gate_llm_done",
        video_id=req.video_id,
        total_score=total_score,
        match_percent=match_percent,
        verdict=verdict,
    )

    # 5. 판정별 액션 결정
    if verdict == "AUTO_PUBLISH":
        return QualityGateResponse(
            action="publish",
            project_id=req.project_id,
            video_id=req.video_id,
            channel_name=req.channel_name,
            verdict=verdict,
            match_percent=match_percent,
            total_score=total_score,
            scores=scores,
            summary=summary,
            critical_issues=critical_issues,
        )

    elif verdict == "CONDITIONAL":
        return QualityGateResponse(
            action="ceo_review",
            project_id=req.project_id,
            video_id=req.video_id,
            channel_name=req.channel_name,
            verdict=verdict,
            match_percent=match_percent,
            total_score=total_score,
            scores=scores,
            summary=summary,
            critical_issues=critical_issues,
            screenshots=[],  # CEO 리뷰용 스크린샷 (추후 확장)
        )

    else:  # AUTO_REJECT
        if req.auto_correct:
            # 자동 보정
            failures = await auto_corrector.analyze_failures({"scores": scores})
            benchmark_ffmpeg = await benchmark_spec_extractor.spec_to_ffmpeg_params(benchmark_spec) if benchmark_spec else {}
            correction_params = await auto_corrector.generate_correction_params(
                failures=failures,
                current_params={},
                benchmark_spec=benchmark_ffmpeg,
            )
            directive = await auto_corrector.create_correction_directive(
                project_id=req.project_id,
                video_id=req.video_id,
                corrections=correction_params,
            )
            return QualityGateResponse(
                action="re-render",
                project_id=req.project_id,
                video_id=req.video_id,
                channel_name=req.channel_name,
                verdict=verdict,
                match_percent=match_percent,
                total_score=total_score,
                scores=scores,
                summary=summary,
                critical_issues=critical_issues,
                corrections=directive,
            )
        else:
            return QualityGateResponse(
                action="reject",
                project_id=req.project_id,
                video_id=req.video_id,
                channel_name=req.channel_name,
                verdict=verdict,
                match_percent=match_percent,
                total_score=total_score,
                scores=scores,
                summary=summary,
                critical_issues=critical_issues,
                error=f"품질 미달 (match_percent={match_percent}%): {summary}",
            )


@router.get("/benchmark-specs/{project_id}/{channel_name}", response_model=BenchmarkSpecResponse)
async def get_benchmark_spec(project_id: str, channel_name: str):
    """
    T-027: 등록된 벤치마크 사양 조회.
    """
    logger.info("api_benchmark_spec_get", project_id=project_id, channel_name=channel_name)

    from app.services.benchmark_spec import benchmark_spec_extractor

    try:
        spec = await benchmark_spec_extractor.get_spec(project_id, channel_name)
    except Exception as e:
        logger.error("api_benchmark_spec_get_error", error=str(e))
        return BenchmarkSpecResponse(
            project_id=project_id,
            channel_name=channel_name,
            found=False,
            message=f"조회 실패: {e}",
        )

    if spec is None:
        return BenchmarkSpecResponse(
            project_id=project_id,
            channel_name=channel_name,
            found=False,
            message="벤치마크 사양 없음. /extract-spec으로 먼저 등록하세요.",
        )

    return BenchmarkSpecResponse(
        project_id=project_id,
        channel_name=channel_name,
        found=True,
        spec=spec,
    )


@router.post("/extract-spec", response_model=ExtractSpecResponse)
async def extract_benchmark_spec(req: ExtractSpecRequest):
    """
    T-027: 벤치마크 프레임에서 사양 추출 + system_memory 저장.
    """
    logger.info(
        "api_extract_spec_start",
        project_id=req.project_id,
        channel_name=req.channel_name,
        frame_count=len(req.benchmark_frames),
    )

    from app.services.benchmark_spec import benchmark_spec_extractor

    if not req.benchmark_frames:
        return ExtractSpecResponse(
            project_id=req.project_id,
            channel_name=req.channel_name,
            spec={},
            ffmpeg_params={},
            saved=False,
            error="benchmark_frames가 비어 있습니다. 최소 1개 이상의 base64 프레임이 필요합니다.",
        )

    try:
        spec = await benchmark_spec_extractor.extract_spec(req.benchmark_frames)
    except Exception as e:
        logger.error("api_extract_spec_error", error=str(e))
        return ExtractSpecResponse(
            project_id=req.project_id,
            channel_name=req.channel_name,
            spec={},
            ffmpeg_params={},
            saved=False,
            error=str(e),
        )

    # FFmpeg 파라미터 변환
    try:
        ffmpeg_params = await benchmark_spec_extractor.spec_to_ffmpeg_params(spec)
    except Exception as e:
        logger.warning("api_extract_spec_ffmpeg_params_error", error=str(e))
        ffmpeg_params = {}

    # system_memory 저장
    saved = True
    try:
        await benchmark_spec_extractor.save_spec(req.project_id, req.channel_name, spec)
    except Exception as e:
        logger.warning("api_extract_spec_save_error", error=str(e))
        saved = False

    logger.info("api_extract_spec_done", project_id=req.project_id, channel_name=req.channel_name, saved=saved)

    return ExtractSpecResponse(
        project_id=req.project_id,
        channel_name=req.channel_name,
        spec=spec,
        ffmpeg_params=ffmpeg_params,
        saved=saved,
    )


# ---------------------------------------------------------------------------
# T-028: 이커머스 이미지 검수 (뉴톡 V2)
# ---------------------------------------------------------------------------


class ImageItem(BaseModel):
    image_base64: str
    image_id: str
    category: str = "상품"


class ImageQARequest(BaseModel):
    """POST /visual-qa/image-qa 요청 모델."""
    project_id: str
    images: List[ImageItem]


class ImageScoreItem(BaseModel):
    score: int
    issues: List[str] = []
    fixes: List[str] = []


class ImageAuditResultItem(BaseModel):
    image_id: str
    category: str
    scores: Dict[str, ImageScoreItem]
    total_score: int
    verdict: str
    summary: str
    critical_issues: List[str]
    error: Optional[str] = None


class ImageQAResponse(BaseModel):
    project_id: str
    results: List[ImageAuditResultItem]
    total: int
    pass_count: int
    conditional_count: int
    fail_count: int
    error_count: int
    overall_verdict: str


class ImageQualityGateRequest(BaseModel):
    """POST /visual-qa/image-quality-gate 요청 모델."""
    project_id: str
    image_base64: str
    image_id: str
    min_score: int = 48


class ImageQualityGateResponse(BaseModel):
    action: str       # approve | reject | error
    project_id: str
    image_id: str
    verdict: str
    total_score: int
    min_score: int
    issues: List[str] = []
    summary: str = ""
    error: Optional[str] = None


@router.post("/image-qa", response_model=ImageQAResponse)
async def image_qa(req: ImageQARequest):
    """
    T-028: 이커머스 상품 이미지 일괄 검수.

    - 6개 기준 스코어카드 (resolution_clarity, background_quality, product_visibility,
      color_accuracy, text_overlay, commercial_readiness)
    - PASS 48+(80%), CONDITIONAL 36-47, FAIL 35이하
    """
    logger.info("api_image_qa_start", project_id=req.project_id, count=len(req.images))

    if not req.images:
        return ImageQAResponse(
            project_id=req.project_id,
            results=[],
            total=0,
            pass_count=0,
            conditional_count=0,
            fail_count=0,
            error_count=0,
            overall_verdict="SKIP",
        )

    tasks = [
        design_auditor.audit_product_image(item.image_base64, is_base64=True)
        for item in req.images
    ]
    raw_results = await _asyncio.gather(*tasks, return_exceptions=False)

    result_items = []
    for item, raw in zip(req.images, raw_results):
        scores_dict = raw.get("scores", {})
        scores_items = {
            k: ImageScoreItem(
                score=v.get("score", 0) if isinstance(v, dict) else int(v),
                issues=v.get("issues", []) if isinstance(v, dict) else [],
                fixes=v.get("fixes", []) if isinstance(v, dict) else [],
            )
            for k, v in scores_dict.items()
        }
        result_items.append(ImageAuditResultItem(
            image_id=item.image_id,
            category=item.category,
            scores=scores_items,
            total_score=raw.get("total_score", 0),
            verdict=raw.get("verdict", "ERROR"),
            summary=raw.get("summary", ""),
            critical_issues=raw.get("critical_issues", []),
            error=raw.get("error"),
        ))

    pass_count = sum(1 for r in result_items if r.verdict == "PASS")
    conditional_count = sum(1 for r in result_items if r.verdict == "CONDITIONAL")
    fail_count = sum(1 for r in result_items if r.verdict == "FAIL")
    error_count = sum(1 for r in result_items if r.verdict == "ERROR")

    if fail_count > 0 or error_count > 0:
        overall_verdict = "FAIL"
    elif conditional_count > 0:
        overall_verdict = "CONDITIONAL"
    else:
        overall_verdict = "PASS"

    logger.info(
        "api_image_qa_done",
        project_id=req.project_id,
        total=len(result_items),
        pass_count=pass_count,
        fail_count=fail_count,
        overall_verdict=overall_verdict,
    )

    return ImageQAResponse(
        project_id=req.project_id,
        results=result_items,
        total=len(result_items),
        pass_count=pass_count,
        conditional_count=conditional_count,
        fail_count=fail_count,
        error_count=error_count,
        overall_verdict=overall_verdict,
    )


@router.post("/image-quality-gate", response_model=ImageQualityGateResponse)
async def image_quality_gate(req: ImageQualityGateRequest):
    """
    T-028: 단일 이미지 품질 게이트.

    PASS(total_score >= min_score) → {"action": "approve"}
    FAIL → {"action": "reject", "issues": [...]}
    """
    logger.info(
        "api_image_quality_gate_start",
        project_id=req.project_id,
        image_id=req.image_id,
        min_score=req.min_score,
    )

    if not req.image_base64:
        return ImageQualityGateResponse(
            action="error",
            project_id=req.project_id,
            image_id=req.image_id,
            verdict="ERROR",
            total_score=0,
            min_score=req.min_score,
            issues=["이미지 base64가 비어 있습니다"],
            summary="이미지 데이터 없음",
            error="empty_image_base64",
        )

    raw = await design_auditor.audit_product_image(req.image_base64, is_base64=True)

    if raw.get("verdict") == "ERROR":
        return ImageQualityGateResponse(
            action="error",
            project_id=req.project_id,
            image_id=req.image_id,
            verdict="ERROR",
            total_score=0,
            min_score=req.min_score,
            issues=raw.get("critical_issues", []),
            summary=raw.get("summary", ""),
            error=raw.get("error"),
        )

    total_score = raw.get("total_score", 0)
    verdict = raw.get("verdict", "FAIL")
    issues = raw.get("critical_issues", [])

    if total_score >= req.min_score:
        action = "approve"
    else:
        action = "reject"
        # 점수 미달 항목 이슈 수집
        for cat_scores in raw.get("scores", {}).values():
            if isinstance(cat_scores, dict):
                issues.extend(cat_scores.get("issues", []))

    logger.info(
        "api_image_quality_gate_done",
        project_id=req.project_id,
        image_id=req.image_id,
        total_score=total_score,
        verdict=verdict,
        action=action,
    )

    return ImageQualityGateResponse(
        action=action,
        project_id=req.project_id,
        image_id=req.image_id,
        verdict=verdict,
        total_score=total_score,
        min_score=req.min_score,
        issues=issues,
        summary=raw.get("summary", ""),
    )
