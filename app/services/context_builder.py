"""
AADS-183: 채팅 시스템 프롬프트 풍부화 — HANDOVER 컨텍스트 + 날짜 + 도구 정보 자동 주입
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ─── 공통 컨텍스트 (모든 워크스페이스) ────────────────────────────────────────

_COMMON_CONTEXT = """당신은 AADS (Autonomous AI Development System)의 AI 어시스턴트입니다.
AADS는 CEO→PM→개발자→QA→운영 역할 분리 멀티 AI 에이전트 자율 개발 시스템입니다.
대시보드: https://aads.newtalk.kr/
GitHub: https://github.com/moongoby-GO100/
서버 3대: 서버211(Hub, bridge.py), 서버68(AADS Backend+Dashboard), 서버114(SF/NTV2/NAS 실행, 포트7916)
프로젝트 6개: AADS, SF(ShortFlow 숏폼), KIS(자동매매), GO100(빡억이 투자분석), NTV2(NewTalk V2), NAS(이미지처리)"""

_TOOL_CONTEXT = """당신은 다음 도구를 사용할 수 있습니다: 대시보드 조회, 헬스체크, GitHub 파일 읽기, DB 쿼리, 웹 검색, 지시서 생성.
질문에 답하기 전에 관련 도구를 호출하여 실제 데이터를 확인하세요."""

# ─── 워크스페이스별 추가 컨텍스트 ──────────────────────────────────────────────

_WORKSPACE_CONTEXTS: dict[str, str] = {
    "CEO": (
        "CEO(moongoby)의 전략적 지시를 받아 전체 프로젝트를 조율합니다. "
        "지시서(>>>DIRECTIVE_START) 작성, 작업 현황 조회, Deep Research, 파이프라인 모니터링을 수행합니다.\n"
        "지시서 포맷: >>>DIRECTIVE_START / TASK_ID: {PROJECT}-{NUM} / TITLE: ... / PRIORITY: P0-P3 / SIZE: XS/S/M/L/XL / >>>DIRECTIVE_END\n"
        "보고 형식: 완료보고는 GitHub 브라우저 URL 포함, 비용($) 명시.\n"
        "최근 완료: AADS-183(채팅 프롬프트 풍부화), AADS-182(Chat SSE 렌더링 수정), AADS-181(통합 작업현황 API)."
    ),
    "AADS": (
        "AADS 프로젝트 매니저입니다. "
        "서버68, FastAPI 0.115 + Next.js 16 + PostgreSQL 15 + Docker Compose.\n"
        "Task ID: AADS-xxx. 최근 완료: AADS-183(프롬프트 풍부화), AADS-182(SSE), AADS-181(작업현황 API).\n"
        "API: /api/v1/chat/*, /api/v1/ops/*, /api/v1/directives/*, /api/v1/managers, /api/v1/directives/preflight\n"
        "파이프라인: auto_trigger.sh → claude_exec.sh → RESULT 파일 → done 폴더."
    ),
    "SF": (
        "ShortFlow 숏폼 동영상 자동화 프로젝트입니다. "
        "서버114(116.120.58.155, 포트7916). Task ID: SF-xxx."
    ),
    "KIS": (
        "KIS 자동매매 프로젝트입니다. "
        "서버211(211.188.51.113). Task ID: KIS-xxx."
    ),
    "GO100": (
        "GO100 빡억이 투자분석 프로젝트입니다. "
        "서버211(211.188.51.113). Task ID: GO100-xxx."
    ),
    "NTV2": (
        "NewTalk V2 소셜플랫폼 프로젝트입니다. "
        "서버114(116.120.58.155), Laravel 12. Task ID: NT-xxx."
    ),
    "NAS": (
        "NAS 이미지처리 프로젝트입니다. "
        "Cafe24, Flask/FastAPI. Task ID: NAS-xxx."
    ),
}

# ─── 동적 컨텍스트 (매 요청 시 갱신) ───────────────────────────────────────────

def _build_dynamic_context() -> str:
    """현재 날짜/시간 + 간략 시스템 상태 (매 요청마다 새로 생성)."""
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    date_str = now.strftime("%Y-%m-%d %H:%M KST (%A)")
    return f"현재 날짜/시간: {date_str}"


# ─── 메인 함수 ─────────────────────────────────────────────────────────────────

MAX_CONTEXT_CHARS = 14000  # ~4000 토큰 이내 (한글 기준 3.5자/토큰)


def build_system_context(workspace_name: str) -> str:
    """
    워크스페이스 이름에 따라 풍부화된 시스템 프롬프트 컨텍스트를 반환한다.
    반환값은 기존 system_prompt 앞에 붙인다.

    Args:
        workspace_name: 워크스페이스 이름 (예: "CEO", "AADS", "SF" 등)

    Returns:
        시스템 컨텍스트 문자열 (최대 ~4000 토큰)
    """
    # 워크스페이스 이름 정규화 (대소문자 무관 + "[CEO] 통합지시" 형식 처리)
    ws_upper = (workspace_name or "").upper().strip()
    # "[CEO] 통합지시" → "CEO" 추출 시도
    if ws_upper.startswith("["):
        end = ws_upper.find("]")
        if end != -1:
            ws_upper = ws_upper[1:end].strip()

    # 워크스페이스별 컨텍스트 (없으면 기본값)
    ws_context = _WORKSPACE_CONTEXTS.get(ws_upper, "")

    # 동적 컨텍스트 (날짜/시간)
    dynamic = _build_dynamic_context()

    parts = [
        dynamic,
        "",
        _COMMON_CONTEXT,
    ]
    if ws_context:
        parts.extend(["", f"[{ws_upper} 워크스페이스]", ws_context])
    parts.extend(["", _TOOL_CONTEXT, ""])

    context = "\n".join(parts)

    # 크기 제한 (초과 시 워크스페이스별 컨텍스트 생략)
    if len(context) > MAX_CONTEXT_CHARS:
        logger.warning(
            "context_builder: context too large (%d chars), trimming ws_context",
            len(context),
        )
        parts_trimmed = [
            dynamic,
            "",
            _COMMON_CONTEXT,
            "",
            _TOOL_CONTEXT,
            "",
        ]
        context = "\n".join(parts_trimmed)

    return context
