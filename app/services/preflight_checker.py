"""
Pre-Flight Checker (AADS-178)

매니저 지시서 발행 전 큐 상태를 확인하는 서비스.
- pending/running 큐 중복 감지
- depends_on 충족 여부 확인 (done 폴더 파일명 매칭)
- running 큐 상태 및 충돌 파일 목록 반환

GET /api/v1/directives/preflight?task_id={id}&depends_on={id}
응답: { queue_clear, depends_met, duplicate, conflicts, recommendation }
"""
import logging
import os
import re
from typing import List, Optional

logger = logging.getLogger(__name__)

_PENDING_DIR = "/root/.genspark/directives/pending"
_RUNNING_DIR = "/root/.genspark/directives/running"
_DONE_DIR = "/root/.genspark/directives/done"
_TASK_ID_RE = re.compile(r"(AADS|KIS|GO100|SF|NT|SALES|NAS|T)-\d+", re.IGNORECASE)


def _extract_task_id_from_file(filepath: str) -> Optional[str]:
    """파일 내용 또는 파일명에서 task_id 추출."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(2000)
        # YAML 헤더 TASK_ID 필드 우선
        m = re.search(r"^TASK_ID:\s*([A-Z][A-Z0-9]*-\d+)", content, re.MULTILINE | re.IGNORECASE)
        if m:
            return m.group(1).upper()
        # 내용 내 패턴 검색
        m = _TASK_ID_RE.search(content)
        if m:
            return m.group(0).upper()
    except Exception:
        pass
    # 파일명에서 추출
    m = _TASK_ID_RE.search(os.path.basename(filepath))
    if m:
        return m.group(0).upper()
    return None


def _list_files(directory: str) -> List[str]:
    """디렉토리의 .md 파일 목록 반환."""
    if not os.path.isdir(directory):
        return []
    try:
        return [
            os.path.join(directory, f)
            for f in os.listdir(directory)
            if f.endswith(".md")
        ]
    except Exception:
        return []


def _check_done_folder(depends_on: str) -> bool:
    """done 폴더에서 depends_on task_id RESULT 파일 존재 확인."""
    done_files = _list_files(_DONE_DIR)
    pattern = re.compile(re.escape(depends_on), re.IGNORECASE)
    for filepath in done_files:
        filename = os.path.basename(filepath)
        if pattern.search(filename) and "RESULT" in filename.upper():
            return True
    return False


def run_preflight(
    task_id: Optional[str] = None,
    depends_on: Optional[str] = None,
) -> dict:
    """
    Pre-Flight Check 실행.

    로직:
    1. pending+running 큐 스캔 → 중복 task_id 체크
    2. running 큐 비어있으면 queue_clear=True
    3. depends_on 있으면 done 폴더에서 RESULT 파일 확인
    4. recommendation: PROCEED | WAIT | BLOCKED

    Returns:
        {
            queue_clear: bool,       # running 큐에 작업 없으면 True
            depends_met: bool,       # depends_on 충족 여부 (의존성 없으면 True)
            duplicate: bool,         # 동일 task_id가 pending/running에 존재하면 True
            conflicts: List[str],    # 충돌 파일명 목록 (중복된 파일명)
            recommendation: str,     # "PROCEED" | "WAIT" | "BLOCKED"
        }
    """
    conflicts: List[str] = []
    duplicate = False
    depends_met = True

    # ── 1. pending + running 큐 스캔 ──────────────────────────
    pending_files = _list_files(_PENDING_DIR)
    running_files = _list_files(_RUNNING_DIR)
    all_queue_files = pending_files + running_files

    for filepath in all_queue_files:
        filename = os.path.basename(filepath)
        if "RESULT" in filename.upper():
            continue
        file_task_id = _extract_task_id_from_file(filepath)
        if task_id and file_task_id and file_task_id.upper() == task_id.upper():
            duplicate = True
            conflicts.append(filename)

    # ── 2. running 큐 상태 (RESULT 제외한 실제 작업 파일 기준) ──
    running_active = [
        f for f in running_files
        if "RESULT" not in os.path.basename(f).upper()
    ]
    queue_clear = len(running_active) == 0

    # ── 3. depends_on 충족 확인 ────────────────────────────────
    if depends_on:
        depends_met = _check_done_folder(depends_on)
        if not depends_met:
            logger.info(
                "preflight_depends_not_met",
                task_id=task_id,
                depends_on=depends_on,
            )

    # ── 4. recommendation 판정 ─────────────────────────────────
    if duplicate:
        recommendation = "BLOCKED"
    elif not depends_met:
        recommendation = "WAIT"
    else:
        recommendation = "PROCEED"

    logger.info(
        "preflight_result",
        task_id=task_id,
        depends_on=depends_on,
        queue_clear=queue_clear,
        depends_met=depends_met,
        duplicate=duplicate,
        conflicts=conflicts,
        recommendation=recommendation,
    )

    return {
        "queue_clear": queue_clear,
        "depends_met": depends_met,
        "duplicate": duplicate,
        "conflicts": conflicts,
        "recommendation": recommendation,
    }
