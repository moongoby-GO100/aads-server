"""
Directive Submit + Pre-Flight Check 엔드포인트 (AADS-157, AADS-178)
AADS-181: GET /directives/all — 3서버 통합 디렉티브 조회.

CEO Chat에서 '진행해/만들어' 의도 감지 시 D-022 포맷 지시서를
/root/.genspark/directives/pending/ 에 파일로 생성.
bridge.py가 감지 → 기존 파이프라인 실행.

AADS-178: GET /directives/preflight — 지시서 발행 전 큐 상태 확인.
AADS-181: GET /directives/all — 3서버 통합 디렉티브 조회.
"""
import logging
import os
import re

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services.preflight_checker import run_preflight

logger = logging.getLogger(__name__)
router = APIRouter()

_PENDING_DIR = "/root/.genspark/directives/pending"
_TASK_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


class DirectiveSubmitRequest(BaseModel):
    task_id: str
    project: str = "AADS"
    priority: str = "P2"
    size: str = "S"
    model: str = "claude-sonnet-4-6"
    description: str
    success_criteria: Optional[str] = None
    files_owned: Optional[List[str]] = None
    impact: str = "M"
    effort: str = "M"
    review_required: bool = False


class DirectiveSubmitResponse(BaseModel):
    status: str
    task_id: str
    filename: str
    path: str


def build_directive_content(req: DirectiveSubmitRequest) -> str:
    """D-022 포맷 지시서 YAML 생성."""
    files_list = (
        "\n".join(f"  - {f}" for f in req.files_owned)
        if req.files_owned
        else "  - (미정)"
    )

    # description 들여쓰기 정렬
    desc_lines = req.description.strip().split("\n")
    desc_indented = "\n  ".join(desc_lines)

    # success_criteria 처리
    sc_raw = req.success_criteria or "구현 완료 및 테스트 통과"
    sc_lines = sc_raw.strip().split("\n")
    sc_indented = "\n  - ".join(line.lstrip("- ").strip() for line in sc_lines if line.strip())
    sc_block = f"  - {sc_indented}"

    review_val = "true" if req.review_required else "false"

    return f"""task_id: {req.task_id}
project: {req.project}
priority: {req.priority}
size: {req.size}
model: {req.model}
description: |
  {desc_indented}

success_criteria:
{sc_block}

files_owned:
{files_list}

impact: {req.impact}
effort: {req.effort}
review_required: {review_val}
parallel_group: null
subagents: null
"""


@router.post("/directives/submit", response_model=DirectiveSubmitResponse)
async def submit_directive(req: DirectiveSubmitRequest):
    """
    D-022 포맷 지시서를 /root/.genspark/directives/pending/ 에 생성.
    bridge.py가 감지하여 파이프라인 실행.
    """
    # task_id 검증
    if not _TASK_ID_RE.match(req.task_id):
        raise HTTPException(
            status_code=400,
            detail=f"task_id 형식 오류: '{req.task_id}' (예: AADS-157, GO100-42)",
        )

    # 지시서 내용 생성
    content = build_directive_content(req)

    # 파일명: {task_id}_{timestamp}.md
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{req.task_id}_{ts}.md"
    filepath = os.path.join(_PENDING_DIR, filename)

    try:
        os.makedirs(_PENDING_DIR, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("directive_submitted", task_id=req.task_id, file=filepath)
        return DirectiveSubmitResponse(
            status="ok",
            task_id=req.task_id,
            filename=filename,
            path=filepath,
        )
    except PermissionError as e:
        logger.error("directive_submit_permission_error", path=filepath, error=str(e))
        raise HTTPException(
            status_code=500,
            detail="지시서 파일 쓰기 권한 없음 (root 소유 디렉토리). 관리자 확인 필요.",
        )
    except Exception as e:
        logger.error("directive_submit_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"지시서 생성 실패: {e}")


def submit_directive_sync(req: DirectiveSubmitRequest) -> DirectiveSubmitResponse:
    """내부 직접 호출용 동기 버전 (CEO Chat execute 핸들러에서 사용)."""
    if not _TASK_ID_RE.match(req.task_id):
        raise ValueError(f"task_id 형식 오류: '{req.task_id}'")

    content = build_directive_content(req)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{req.task_id}_{ts}.md"
    filepath = os.path.join(_PENDING_DIR, filename)

    os.makedirs(_PENDING_DIR, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info("directive_submitted_sync", task_id=req.task_id, file=filepath)
    return DirectiveSubmitResponse(
        status="ok",
        task_id=req.task_id,
        filename=filename,
        path=filepath,
    )


# ── AADS-178: Pre-Flight Check ────────────────────────────────────────────────

class PreflightResponse(BaseModel):
    queue_clear: bool
    depends_met: bool
    duplicate: bool
    conflicts: List[str]
    recommendation: str  # "PROCEED" | "WAIT" | "BLOCKED"


# ── AADS-181: 3서버 통합 디렉티브 조회 ───────────────────────────────────────

@router.get("/directives/all")
async def get_all_directives(
    status: Optional[str] = Query(
        None,
        description="필터: pending|running|done|archived|all (기본: all)",
    ),
    project: Optional[str] = Query(
        None,
        description="프로젝트 필터: AADS|KIS|GO100|SF|NTV2|NAS|all (기본: all)",
    ),
    force_refresh: bool = Query(False, description="캐시 무시 강제 스캔"),
):
    """
    3대 서버(68/211/114) directives 통합 조회.

    - SSH 직접 스캔 (211/114), 로컬 스캔 (68)
    - SSH 실패 시 HTTP fallback
    - 30초 캐싱 (반복 SSH 방지)
    - project/status 필터 지원
    """
    from app.services.cross_server_checker import scan_all_servers
    from app.services.server_registry import ALL_STATUSES

    # statuses 목록 결정
    if not status or status.lower() in ("all", ""):
        statuses = ALL_STATUSES
    elif status in ALL_STATUSES:
        statuses = [status]
    else:
        raise HTTPException(400, f"status must be one of {ALL_STATUSES} or 'all'")

    # project 필터 정규화
    proj_filter = None
    if project and project.lower() not in ("all", ""):
        proj_filter = project.upper()

    try:
        data = await scan_all_servers(
            statuses=statuses,
            project_filter=proj_filter,
            force_refresh=force_refresh,
        )
        return {
            "status": "ok",
            "total_count": data["total_count"],
            "counts": data["counts"],
            "by_server": {
                sid: {
                    "reachable": sdata.get("reachable", False),
                    "method": sdata.get("method", "unknown"),
                    "counts": sdata.get("counts", {}),
                    "total": sdata.get("total", 0),
                }
                for sid, sdata in data["by_server"].items()
            },
            "directives": data["directives"],
            "cached": data.get("cached", False),
            "scanned_at": data.get("scanned_at"),
        }
    except Exception as e:
        logger.error("get_all_directives_error", error=str(e))
        raise HTTPException(500, f"3서버 디렉티브 조회 실패: {e}")


# ── AADS-178: Pre-Flight Check ────────────────────────────────────────────────

@router.get("/directives/preflight", response_model=PreflightResponse)
async def get_directive_preflight(
    task_id: Optional[str] = Query(None, description="발행 예정 task_id (중복 검사용)"),
    depends_on: Optional[str] = Query(None, description="선행 task_id (완료 여부 확인)"),
):
    """
    매니저 지시서 발행 전 Pre-Flight Check.

    - pending/running 큐에서 중복 task_id 감지
    - depends_on이 있으면 done 폴더에서 완료 여부 확인
    - recommendation: PROCEED | WAIT | BLOCKED 반환
    """
    try:
        result = run_preflight(task_id=task_id, depends_on=depends_on)
        return PreflightResponse(**result)
    except Exception as e:
        logger.error("preflight_check_failed", task_id=task_id, depends_on=depends_on, error=str(e))
        raise HTTPException(status_code=500, detail=f"Pre-Flight Check 실패: {e}")
