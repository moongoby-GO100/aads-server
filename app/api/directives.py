"""
Directive Submit 엔드포인트 (AADS-157)

CEO Chat에서 '진행해/만들어' 의도 감지 시 D-022 포맷 지시서를
/root/.genspark/directives/pending/ 에 파일로 생성.
bridge.py가 감지 → 기존 파이프라인 실행.
"""
import logging
import os
import re

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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
