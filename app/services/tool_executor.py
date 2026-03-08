"""
AADS-184: 도구 실행 엔진 — 인텐트→도구 매핑 + 병렬 실행 + 결과 주입 포맷

흐름:
  classify_intent() → INTENT_TOOL_MAP 조회 → execute_tools() 병렬 실행
  → 결과 합산 → LLM 시스템 메시지 주입

타임아웃:
  - 개별 도구: 10초
  - 전체 실행: 15초
  - casual 인텐트: 도구 없음 → 즉시 LLM

도구 결과:
  - 최대 2000 토큰 (~6000자)
  - 초과 시 잘라내기
  - 실패 시 fallback 메시지 접두사 추가
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine, Dict, List

logger = logging.getLogger(__name__)

# ─── 도구 함수 임포트 ──────────────────────────────────────────────────────────
from app.services.chat_tools import (
    health_check,
    dashboard_query,
    search_web,
    read_github_file,
    query_database,
    read_remote_file,
    fetch_url,
    generate_directive,
    list_workspaces_sessions,
)

# ─── 인텐트 → 도구 매핑 ───────────────────────────────────────────────────────
# 각 인텐트에 대해 실행할 도구 함수 리스트 정의
# 빈 리스트 = 도구 없이 바로 LLM 응답

ToolFn = Callable[..., Coroutine[Any, Any, Dict[str, Any]]]

INTENT_TOOL_MAP: Dict[str, List[ToolFn]] = {
    # ── 실시간 데이터 필요 인텐트 ──────────────────────────────────────────────
    "health_check":     [health_check],
    "dashboard":        [dashboard_query],
    "diagnosis":        [dashboard_query, health_check],
    # ── 웹/파일 리서치 인텐트 ─────────────────────────────────────────────────
    "search":           [search_web],
    "research":         [read_github_file, fetch_url, search_web],
    "deep_research":    [search_web, read_github_file],
    "url_analyze":      [fetch_url],
    # ── GitHub/원격 파일 인텐트 ───────────────────────────────────────────────
    "memory_recall":    [read_github_file, query_database],
    # ── DB 쿼리 인텐트 ────────────────────────────────────────────────────────
    # dashboard 이미 포함, 추가로 DB 직접 조회 원할 때
    # ── 지시서 생성 인텐트 ────────────────────────────────────────────────────
    "directive_gen":    [dashboard_query, generate_directive],
    "execute":          [dashboard_query, generate_directive],
    # ── 워크스페이스/세션 조회 인텐트 ─────────────────────────────────────────
    "workspace_switch": [list_workspaces_sessions],
    # ── 원격 파일 접근 인텐트 ─────────────────────────────────────────────────
    "qa":               [read_remote_file],
    "execution_verify": [read_remote_file],
    # ── 도구 없이 바로 LLM 응답 ───────────────────────────────────────────────
    "casual":           [],      # 잡담: 빠른 응답 (<2초)
    "strategy":         [],      # 전략: LLM 지식 기반
    "planning":         [],      # 기획: LLM 지식 기반
    "decision":         [],      # 결정: LLM 지식 기반
    "design":           [],      # 디자인: LLM 지식 기반
    "design_fix":       [],      # 디자인 수정: LLM 지식 기반
    "architect":        [],      # 아키텍처: LLM 지식 기반
    "code_exec":        [],      # 코드 실행: 향후 샌드박스 연동
    "browser":          [],      # 브라우저: 기존 ceo_chat.py 핸들러 사용
    "image_analyze":    [],      # 이미지: 멀티모달 LLM
    "video_analyze":    [],      # 동영상: 향후 연동
}

# 도구 결과 최대 토큰 (약 2000토큰 = 6000자)
_MAX_TOOL_RESULT_CHARS = 6000

# 도구 이름 매핑 (로깅/표시용)
_TOOL_NAME_MAP: Dict[str, str] = {
    "health_check":            "서버헬스",
    "dashboard_query":         "대시보드",
    "search_web":              "웹검색",
    "read_github_file":        "GitHub파일",
    "query_database":          "DB조회",
    "read_remote_file":        "원격파일",
    "fetch_url":               "URL조회",
    "generate_directive":      "지시서생성",
    "list_workspaces_sessions": "워크스페이스",
}


# ─── 메인 실행 함수 ────────────────────────────────────────────────────────────

async def execute_tools(intent: str, message: str, workspace_id: str) -> str:
    """
    인텐트에 매핑된 도구들을 병렬 실행하고 결과를 문자열로 반환.

    Args:
        intent: 분류된 인텐트 (예: "health_check", "dashboard", "casual")
        message: 사용자 메시지 원문
        workspace_id: 현재 워크스페이스 ID

    Returns:
        도구 결과 합산 문자열 (없으면 "")
        형식:
          [도구명]
          {JSON 결과}

          [도구명2]
          ...
    """
    tools = INTENT_TOOL_MAP.get(intent, [])
    if not tools:
        return ""  # 도구 없음 → LLM만으로 응답

    async def _run_tool(tool_fn: ToolFn) -> tuple[str, str]:
        """단일 도구 실행. (도구명, 결과 문자열) 반환."""
        tool_name = _TOOL_NAME_MAP.get(tool_fn.__name__, tool_fn.__name__)
        try:
            result = await asyncio.wait_for(
                tool_fn(message, workspace_id),
                timeout=10,
            )
            result_str = json.dumps(result, ensure_ascii=False, indent=2)
            return tool_name, result_str
        except asyncio.TimeoutError:
            logger.warning(f"tool_executor_timeout: tool={tool_fn.__name__} intent={intent}")
            return tool_name, '{"error": "타임아웃 (10초 초과)"}'
        except Exception as e:
            logger.error(f"tool_executor_error: tool={tool_fn.__name__} error={e}")
            return tool_name, f'{{"error": "{str(e)}"}}'

    # 전체 도구 병렬 실행 (최대 15초)
    try:
        task_results = await asyncio.wait_for(
            asyncio.gather(*[_run_tool(t) for t in tools]),
            timeout=15,
        )
    except asyncio.TimeoutError:
        logger.warning(f"tool_executor_total_timeout: intent={intent}")
        return "[도구 조회 타임아웃 (15초 초과)]"

    # 결과 조합
    parts: List[str] = []
    has_error_only = True
    for tool_name, result_str in task_results:
        parts.append(f"[{tool_name}]\n{result_str}")
        # 에러가 아닌 실제 데이터가 있는지 확인
        if '"error"' not in result_str or len(result_str) > 100:
            has_error_only = False

    combined = "\n\n".join(parts)

    # 크기 제한
    if len(combined) > _MAX_TOOL_RESULT_CHARS:
        combined = combined[:_MAX_TOOL_RESULT_CHARS] + "\n\n...(도구 결과 잘림)"

    logger.info(
        f"tool_executor_done: intent={intent} tools={len(tools)} "
        f"result_chars={len(combined)} has_error_only={has_error_only}"
    )
    return combined


def build_tool_injection(tool_result: str) -> str:
    """
    도구 결과를 LLM 메시지 주입 포맷으로 변환.

    Returns:
        빈 문자열 (도구 결과 없음) 또는 주입 문자열
    """
    if not tool_result:
        return ""
    return (
        "[시스템 도구 조회 결과 — 아래 데이터를 기반으로 정확하게 답변하세요]\n\n"
        + tool_result
    )


def has_tools_for_intent(intent: str) -> bool:
    """해당 인텐트에 도구가 매핑되어 있는지 확인."""
    return bool(INTENT_TOOL_MAP.get(intent))
