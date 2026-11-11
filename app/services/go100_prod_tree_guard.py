"""GO100 운영 폴더 직접 수정 차단 게이트 (CEO 지시 2026-11-11).

운영 폴더(/root/kis-autotrade-v4, /opt/go100/*-releases·backend-slots)를 세션이
직접 고치면 origin/main 에 없는 커밋이 쌓이고, main 기준 릴리스 때 사라진다
(contabo14 에서 30건 실측). 프롬프트 규칙만으로는 또 어겨지므로 코드로 막는다
(R-ERRBOOK 3항). 오류 사전 키: go100.prod_tree_direct_edit.

우회: tool_input.prod_write_ticket 이 환경변수 GO100_PROD_WRITE_TICKETS(쉼표 구분,
기본 빈 값, .env 에만 둔다)에 있을 때만 통과하며 감사 로그를 남긴다.
판단 중 예외는 통과 쪽으로 실패한다(direction_guard 와 같은 방식).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PROD_ROOT = "/root/kis-autotrade-v4"
_RELEASE_ROOT = "/opt/go100"
_TARGET_TOOLS = {"write_remote_file", "patch_remote_file"}

# 허용 접두사(운영 폴더 기준 상대경로). 코드가 아닌 산출물·런타임 경로다.
# 근거(2026-11-11 R2 확인 결과): 이 서버(/root/aads)에는 GO100 소스 사본이 없고
# (aads-server·aads-dashboard·runner-* 뿐), contabo14 는 운영 폴더 보호 때문에 이 작업에서
# 접속하지 않았다. 따라서 실제 산출물 경로는 확인하지 못했고 목록은 지시서 그대로다.
# 확인 방법: contabo14 에서 `ls -d /root/kis-autotrade-v4/{artifacts,logs,tmp,.cache,runtime}`
# 와 코드의 경로 상수 grep(`grep -rn "artifacts/\|runtime/" backend/`) 후 필요 시 추가.
# 근거 없는 경로는 추가하지 않는다.
_ALLOWED_PREFIXES = ("artifacts/", "logs/", "tmp/", ".cache/", "runtime/")

BLOCK_MESSAGE = (
    "GO100 운영 폴더 코드는 직접 수정할 수 없습니다(CEO 지시 2026-11-11). "
    "pipeline_runner_submit 으로 main 커밋→push→배포 시스템으로 반영하세요."
)


def _norm_abs(path: str) -> str:
    """쓰기 함수(ceo_chat_tools)와 같은 식: normpath(pjoin(workdir, file_path)).

    strip·치환을 하지 않는다 — 게이트가 정규화를 더 하면 게이트가 본 경로와 실제로 쓰이는
    경로가 갈라진다(' /../backend/x' 우회, 2026-11-11 리뷰 P1). GO100 workdir == PROD_ROOT.
    """
    return posixpath.normpath(posixpath.join(PROD_ROOT, path or ""))


def has_suspicious_chars(path: str) -> bool:
    """앞뒤 공백·백슬래시·제어문자가 있으면 True — GO100 에서는 거부 쪽으로 판정한다."""
    return (
        path != path.strip()
        or "\\" in path
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
    )


def is_prod_tree_code_path(path: str) -> bool:
    """운영 폴더 안의 코드 경로(허용 접두사 제외)이면 True."""
    full = _norm_abs(path)
    if full == PROD_ROOT or full.startswith(PROD_ROOT + "/"):
        rel = full[len(PROD_ROOT) + 1:]
        return not rel.startswith(_ALLOWED_PREFIXES)
    if full.startswith(_RELEASE_ROOT + "/"):
        parts = full[len(_RELEASE_ROOT) + 1:].split("/")
        if len(parts) >= 2 and (parts[0].endswith("-releases") or parts[0] == "backend-slots"):
            return True
    return False


def _tickets() -> set:
    return {t.strip() for t in os.getenv("GO100_PROD_WRITE_TICKETS", "").split(",") if t.strip()}


def check(tool_name: str, tool_input: Dict[str, Any]) -> Optional[str]:
    """차단이면 JSON 문자열, 통과면 None."""
    try:
        if tool_name not in _TARGET_TOOLS:
            return None
        if str(tool_input.get("project") or "").strip().upper() != "GO100":
            return None
        path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        if not path:
            return None
        if not has_suspicious_chars(path) and not is_prod_tree_code_path(path):
            return None
        ticket = str(tool_input.get("prod_write_ticket") or "").strip()
        if ticket and ticket in _tickets():
            # 티켓 원문은 로그에 남기지 않는다 — sha256 앞 8자리만.
            logger.warning(
                "go100_prod_tree_write_ticket_used tool=%s path=%s ticket_sha8=%s",
                tool_name, path[:200], hashlib.sha256(ticket.encode()).hexdigest()[:8],
            )
            return None
        return json.dumps(
            {"error": "go100_prod_tree_write_blocked", "blocked": True, "message": BLOCK_MESSAGE},
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.warning("go100_prod_tree_guard_error tool=%s error=%s", tool_name, str(exc)[:200])
        return None
