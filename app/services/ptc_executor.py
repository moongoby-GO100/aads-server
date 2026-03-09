"""
AADS-186E-2: Programmatic Tool Calling (PTC) 실행기
Claude가 Python 코드로 여러 도구를 병렬 실행 → 토큰 37% 절감.
- CALLABLE_TOOLS: 읽기 전용 도구만 허용 (쓰기 도구 제외)
- 병렬 실행: asyncio.gather
- 중간 결과는 Claude 컨텍스트에 넣지 않고 최종 print() 출력만 전달
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─── 읽기 전용 허용 도구 목록 ────────────────────────────────────────────────

CALLABLE_TOOLS: List[str] = [
    "list_remote_dir",
    "read_remote_file",
    "health_check",
    "query_database",
    "task_history",
    "cost_report",
    "get_all_service_status",
    "inspect_service",
    "server_status",
    "dashboard_query",
    "web_search_brave",
    "read_github_file",
    # jina_read (186E-1 완료 후 추가)
]

# 쓰기 도구 — PTC 허용 불가
_WRITE_TOOLS = {"directive_create", "generate_directive", "save_note", "learn_pattern"}


@dataclass
class PTCToolCall:
    """단일 PTC 도구 호출 요청."""
    tool_name: str
    tool_input: Dict[str, Any]
    alias: str = ""  # 결과를 참조할 별칭 (선택)


@dataclass
class PTCResult:
    """PTC 실행 전체 결과."""
    results: Dict[str, Any] = field(default_factory=dict)  # alias → 결과
    final_output: str = ""   # print() 출력 — Claude에 전달할 텍스트
    token_estimate: int = 0  # 절약 토큰 추정
    errors: List[str] = field(default_factory=list)


class PTCExecutor:
    """
    Claude가 Python 코드로 도구를 호출하는 PTC 실행기.
    - 병렬 실행으로 토큰 절감
    - 중간 결과는 Claude 컨텍스트에 넣지 않음
    - 최종 output만 Claude에 전달
    """

    def __init__(self) -> None:
        from app.services.tool_executor import ToolExecutor
        self._executor = ToolExecutor()

    async def execute_parallel(
        self,
        tool_calls: List[PTCToolCall],
    ) -> PTCResult:
        """
        여러 도구를 병렬 실행하고 집계된 결과를 반환.
        쓰기 도구 호출 시 에러로 처리.
        """
        result = PTCResult()

        # 읽기 전용 필터
        safe_calls = []
        for call in tool_calls:
            if call.tool_name in _WRITE_TOOLS:
                result.errors.append(
                    f"PTC 거부: {call.tool_name}는 쓰기 도구 — PTC 불허"
                )
                logger.warning(f"ptc_executor: write tool blocked: {call.tool_name}")
            elif call.tool_name not in CALLABLE_TOOLS:
                result.errors.append(
                    f"PTC 거부: {call.tool_name}는 CALLABLE_TOOLS 목록에 없음"
                )
            else:
                safe_calls.append(call)

        if not safe_calls:
            result.final_output = json.dumps({"errors": result.errors}, ensure_ascii=False)
            return result

        # 병렬 실행
        async def _run_one(call: PTCToolCall) -> tuple[str, Any]:
            alias = call.alias or call.tool_name
            try:
                res = await self._executor.execute(call.tool_name, call.tool_input)
                return alias, res
            except Exception as e:
                logger.error(f"ptc_executor execute error: tool={call.tool_name} e={e}")
                return alias, {"error": str(e)}

        tasks = [_run_one(c) for c in safe_calls]
        gathered = await asyncio.gather(*tasks, return_exceptions=False)

        for alias, res in gathered:
            result.results[alias] = res

        # 토큰 절감 추정: 직렬 대비 병렬 — 메시지 왕복 횟수 절감
        sequential_msgs = len(safe_calls) * 2  # 요청 + 응답
        parallel_msgs = 2  # 1회 요청 + 1회 응답
        result.token_estimate = max(0, (sequential_msgs - parallel_msgs) * 200)

        # 최종 출력 — Claude에 전달할 텍스트 (print() 형식)
        result.final_output = _format_ptc_output(result.results, result.errors)
        return result

    async def execute_ptc_code(
        self,
        code: str,
        tool_calls: List[Dict[str, Any]],
    ) -> PTCResult:
        """
        코드 블록에서 도구 호출 목록을 받아 실행.
        tool_calls 포맷: [{"tool_name": str, "tool_input": dict, "alias": str}]
        """
        parsed_calls = []
        for tc in tool_calls:
            parsed_calls.append(
                PTCToolCall(
                    tool_name=tc.get("tool_name", ""),
                    tool_input=tc.get("tool_input", {}),
                    alias=tc.get("alias", ""),
                )
            )
        return await self.execute_parallel(parsed_calls)


# ─── 출력 포맷 ───────────────────────────────────────────────────────────────

def _format_ptc_output(results: Dict[str, Any], errors: List[str]) -> str:
    """PTC 결과를 Claude가 읽기 좋은 형식으로 변환."""
    lines = []
    if errors:
        lines.append(f"[PTC 오류] {'; '.join(errors)}")
    for alias, res in results.items():
        try:
            if isinstance(res, str):
                summary = res[:1000]
            else:
                summary = json.dumps(res, ensure_ascii=False, indent=2)[:1000]
        except Exception:
            summary = str(res)[:500]
        lines.append(f"[{alias}]\n{summary}")
    return "\n\n".join(lines) if lines else "(결과 없음)"


# ─── 편의 함수 ───────────────────────────────────────────────────────────────

async def run_parallel_health_check() -> PTCResult:
    """6개 서버 병렬 헬스체크 (PTC 대표 사례)."""
    executor = PTCExecutor()
    calls = [
        PTCToolCall("health_check", {"server": "68"}, alias="server68"),
        PTCToolCall("health_check", {"server": "211"}, alias="server211"),
        PTCToolCall("health_check", {"server": "114"}, alias="server114"),
        PTCToolCall("get_all_service_status", {"include_details": False}, alias="all_services"),
    ]
    return await executor.execute_parallel(calls)
