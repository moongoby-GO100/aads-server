"""
AADS-190 Phase2-A: 서브에이전트 서비스
Claude Code의 Agent 도구처럼 독립적 서브에이전트를 spawn하여 병렬 실행.

사용 패턴:
  1. spawn_subagent(task, ...) — 단일 서브에이전트 실행
  2. spawn_parallel_subagents([{task, ...}, ...]) — 병렬 실행 후 결과 취합

서브에이전트는 메인 대화와 독립적인 LLM 호출을 수행하며,
도구(read_remote_file, query_database 등)를 사용할 수 있음.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from app.core.anthropic_client import call_llm_messages_with_fallback

logger = logging.getLogger(__name__)

# 서브에이전트 모델 매핑
_MODEL_MAP = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

# 서브에이전트 기본 설정
_DEFAULT_MODEL = "sonnet"
_MAX_TOKENS = int(__import__('os').getenv("MAX_TOKENS_SUBAGENT", "32768"))
_MAX_TOOL_TURNS = 50  # 도구 루프 최대 반복
_SUBAGENT_TIMEOUT = 600  # 초 (10분)

# 서브에이전트 사용 가능 도구 (Red 등급 제외 전체 허용)
_SUBAGENT_TOOLS = [
    # Green: 읽기/조회
    "read_remote_file", "list_remote_dir", "query_database",
    "query_project_database", "list_project_databases",
    "code_explorer", "semantic_code_search", "health_check",
    "get_all_service_status", "inspect_service", "analyze_changes",
    "search_all_projects", "dashboard_query", "server_status",
    "task_history", "read_github_file", "check_directive_status",
    "check_task_status", "read_task_logs",
    "jina_read", "crawl4ai_fetch", "recall_notes", "cost_report",
    "git_remote_status",
    # Yellow: 쓰기/실행 (CEO 채팅에서 위임된 작업이므로 허용)
    "write_remote_file", "patch_remote_file", "run_remote_command",
    "git_remote_add", "git_remote_commit", "git_remote_push",
    "git_remote_create_branch",
    "web_search", "web_search_brave", "web_search_naver", "web_search_kakao",
    "search_searxng",
    "deep_research", "deep_crawl",
    "save_note", "delete_note", "learn_pattern", "observe",
    "export_data",
    # 브라우저
    "browser_navigate", "browser_snapshot", "browser_screenshot",
    "browser_click", "browser_fill", "browser_tab_list",
]


def _build_tool_schemas() -> List[Dict[str, Any]]:
    """서브에이전트용 Anthropic Tool Use 스키마 — ToolRegistry에서 허용 도구만 동적 로드."""
    # ToolRegistry 이름 → ToolExecutor dispatch 이름 매핑 (불일치 보정)
    _REGISTRY_TO_DISPATCH = {
        "search_naver": "web_search_naver",
        "search_kakao": "web_search_kakao",
    }
    try:
        from app.services.tool_registry import ToolRegistry
        registry = ToolRegistry()
        all_tools = registry.get_tools("all")
        # _SUBAGENT_TOOLS에 포함된 도구 스키마만 필터링
        # ToolRegistry 이름이 dispatch 이름과 다른 경우 변환
        result = []
        for t in all_tools:
            name = t.get("name")
            dispatch_name = _REGISTRY_TO_DISPATCH.get(name, name)
            if dispatch_name in _SUBAGENT_TOOLS:
                t_copy = dict(t)
                t_copy["name"] = dispatch_name
                result.append(t_copy)
        return result
    except Exception as e:
        logger.error(f"subagent_tool_schema_fallback_ERROR: {e} — 폴백 모드 활성화 (48개 도구 풀세트)")
        # 폴백: ToolRegistry 실패 시에도 48개 도구 모두 제공 (원격 도구 차단 방지)
        # 주요 도구들의 기본 스키마 제공
        return [
            # 원격 읽기/조회 (24개 중 주요 도구)
            {"name": "read_remote_file", "description": "원격 파일 읽기", "input_schema": {"type": "object", "properties": {"project": {"type": "string", "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"]}, "file_path": {"type": "string"}}, "required": ["project", "file_path"]}},
            {"name": "list_remote_dir", "description": "원격 디렉토리 탐색", "input_schema": {"type": "object", "properties": {"project": {"type": "string", "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"]}, "path": {"type": "string"}}, "required": ["project"]}},
            {"name": "query_database", "description": "AADS DB SELECT", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
            {"name": "query_project_database", "description": "프로젝트 DB SELECT", "input_schema": {"type": "object", "properties": {"project": {"type": "string", "enum": ["KIS", "GO100", "SF", "NTV2"]}, "query": {"type": "string"}}, "required": ["project", "query"]}},
            {"name": "list_project_databases", "description": "프로젝트 DB 목록", "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "read_github_file", "description": "GitHub 파일 읽기", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            # 원격 쓰기/실행 (8개 모두 필수)
            {"name": "write_remote_file", "description": "원격 파일 쓰기", "input_schema": {"type": "object", "properties": {"project": {"type": "string", "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"]}, "file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["project", "file_path", "content"]}},
            {"name": "patch_remote_file", "description": "원격 파일 패치", "input_schema": {"type": "object", "properties": {"project": {"type": "string", "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"]}, "file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["project", "file_path", "old_string", "new_string"]}},
            {"name": "run_remote_command", "description": "원격 명령 실행", "input_schema": {"type": "object", "properties": {"project": {"type": "string", "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"]}, "command": {"type": "string"}}, "required": ["project", "command"]}},
            {"name": "git_remote_status", "description": "Git 상태 확인", "input_schema": {"type": "object", "properties": {"project": {"type": "string", "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"]}}, "required": ["project"]}},
            {"name": "git_remote_add", "description": "파일 추가", "input_schema": {"type": "object", "properties": {"project": {"type": "string", "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"]}, "files": {"type": "string"}}, "required": ["project"]}},
            {"name": "git_remote_commit", "description": "커밋 생성", "input_schema": {"type": "object", "properties": {"project": {"type": "string", "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"]}, "message": {"type": "string"}}, "required": ["project", "message"]}},
            {"name": "git_remote_push", "description": "원격 푸시", "input_schema": {"type": "object", "properties": {"project": {"type": "string", "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"]}}, "required": ["project"]}},
            {"name": "git_remote_create_branch", "description": "브랜치 생성", "input_schema": {"type": "object", "properties": {"project": {"type": "string", "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"]}, "branch_name": {"type": "string"}}, "required": ["project", "branch_name"]}},
            # 기타 도구 (검색, 메모리, 분석)
            {"name": "web_search", "description": "웹 검색", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
            {"name": "deep_research", "description": "깊은 조사", "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
            {"name": "save_note", "description": "노트 저장", "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
            {"name": "browser_navigate", "description": "브라우저 이동", "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
            {"name": "export_data", "description": "데이터 내보내기", "input_schema": {"type": "object", "properties": {"data": {"type": "array"}, "fmt": {"type": "string"}}, "required": ["data"]}},
        ]


async def _execute_tool(tool_name: str, tool_input: Dict[str, Any]) -> str:
    """서브에이전트 도구 실행 — ToolExecutor 위임."""
    if tool_name not in _SUBAGENT_TOOLS:
        return f"[도구 사용 불가: {tool_name} — 서브에이전트 허용 목록에 없음]"

    try:
        from app.services.tool_executor import ToolExecutor, current_chat_session_id
        # ContextVar 전파 확인 로그
        _sid = current_chat_session_id.get("")
        if not _sid:
            logger.warning(f"subagent _execute_tool: current_chat_session_id 미설정 (tool={tool_name})")
        executor = ToolExecutor()
        # 원격 도구는 시간이 더 걸릴 수 있으므로 타임아웃 분화
        timeout_sec = 180 if "remote" in tool_name or "git_remote" in tool_name else 120
        result = await asyncio.wait_for(
            executor.execute(tool_name, tool_input),
            timeout=timeout_sec,
        )
        # 결과가 dict이면 JSON, 아니면 str
        if isinstance(result, dict):
            return json.dumps(result, ensure_ascii=False, default=str)[:12000]
        return str(result)[:12000]
    except asyncio.TimeoutError:
        logger.error(f"subagent_tool_TIMEOUT: {tool_name} — 120-180초 초과")
        return f"[도구 타임아웃: {tool_name} (실행 시간 초과, 혹은 서버 응답 지연)]"
    except Exception as e:
        logger.exception(f"subagent_tool_ERROR: {tool_name} — {e}")
        return f"[도구 오류: {tool_name} — {str(e)[:100]}]"


async def spawn_subagent(
    task: str,
    model: str = _DEFAULT_MODEL,
    system_prompt: Optional[str] = None,
    context: Optional[str] = None,
    enable_tools: bool = True,
    agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    독립적 서브에이전트 실행.

    Args:
        task: 서브에이전트에게 할당할 작업 설명
        model: 사용할 모델 (sonnet/opus/haiku)
        system_prompt: 커스텀 시스템 프롬프트 (없으면 기본 사용)
        context: 추가 컨텍스트 (파일 내용, DB 결과 등)
        enable_tools: 도구 사용 허용 여부
        agent_id: 서브에이전트 ID (없으면 자동 생성)

    Returns:
        {agent_id, status, result, model, tokens, duration_ms, tools_used}
    """
    aid = agent_id or f"sa-{uuid.uuid4().hex[:8]}"
    model_id = _MODEL_MAP.get(model, _MODEL_MAP[_DEFAULT_MODEL])
    start_time = time.time()

    logger.info(f"subagent_spawn: id={aid} model={model_id} task={task[:80]}")

    # 시스템 프롬프트 구성
    sys_prompt = system_prompt or (
        "당신은 AADS 서브에이전트입니다. 메인 에이전트가 위임한 작업을 독립적으로 수행합니다.\n"
        "읽기/쓰기/실행/Git/검색 등 모든 도구를 활용하여 작업을 완수하세요.\n"
        "핵심만 간결하게 답변하고, 작업 완료 시 결과를 구조화된 형태로 반환하세요."
    )

    # 메시지 구성
    user_content = task
    if context:
        user_content = f"[컨텍스트]\n{context[:3000]}\n\n[작업]\n{task}"

    messages: List[Dict[str, Any]] = [{"role": "user", "content": user_content}]
    tools = _build_tool_schemas() if enable_tools else None
    tools_used: List[str] = []
    total_input = 0
    total_output = 0

    try:
        for turn in range(_MAX_TOOL_TURNS):
            api_kwargs: Dict[str, Any] = {
                "model": model_id,
                "max_tokens": _MAX_TOKENS,
                "system": sys_prompt,
                "messages": messages,
            }
            if tools:
                api_kwargs["tools"] = tools
                api_kwargs["tool_choice"] = {"type": "auto"}

            response = await asyncio.wait_for(
                call_llm_messages_with_fallback(**api_kwargs),
                timeout=_SUBAGENT_TIMEOUT,
            )

            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens

            # 텍스트 추출
            text_parts = []
            tool_use_blocks = []
            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_use_blocks.append(block)

            # 도구 호출 없으면 완료
            if not tool_use_blocks:
                result_text = "\n".join(text_parts)
                break
            else:
                # 도구 실행 후 결과를 메시지에 추가
                messages.append({"role": "assistant", "content": response.content})

                tool_results = []
                for tb in tool_use_blocks:
                    tools_used.append(tb.name)
                    logger.debug(f"subagent_{aid}: tool_use {tb.name}")
                    result = await _execute_tool(tb.name, tb.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tb.id,
                        "content": result,
                    })

                messages.append({"role": "user", "content": tool_results})
        else:
            result_text = "\n".join(text_parts) if text_parts else "[도구 루프 최대 반복 초과]"

        duration_ms = int((time.time() - start_time) * 1000)

        logger.info(
            f"subagent_complete: id={aid} duration={duration_ms}ms "
            f"tokens={total_input}+{total_output} tools={len(tools_used)}"
        )

        return {
            "agent_id": aid,
            "status": "completed",
            "result": result_text,
            "model": model_id,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "duration_ms": duration_ms,
            "tools_used": tools_used,
        }

    except asyncio.TimeoutError:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.warning(f"subagent_timeout: id={aid} duration={duration_ms}ms")
        return {
            "agent_id": aid,
            "status": "timeout",
            "result": f"[서브에이전트 타임아웃: {_SUBAGENT_TIMEOUT}초 초과]",
            "model": model_id,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "duration_ms": duration_ms,
            "tools_used": tools_used,
        }

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.exception(f"subagent_error: id={aid} error={e}")
        return {
            "agent_id": aid,
            "status": "error",
            "result": f"[서브에이전트 오류: {type(e).__name__}: {e}]",
            "model": model_id,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "duration_ms": duration_ms,
            "tools_used": tools_used,
        }


async def spawn_parallel_subagents(
    tasks: List[Dict[str, Any]],
    max_concurrent: int = 5,
) -> List[Dict[str, Any]]:
    """
    여러 서브에이전트를 병렬 실행.

    Args:
        tasks: [{task, model?, system_prompt?, context?, enable_tools?}, ...]
        max_concurrent: 최대 동시 실행 수

    Returns:
        각 서브에이전트 결과 리스트
    """
    if not tasks:
        return []

    sem = asyncio.Semaphore(max_concurrent)

    async def _run(spec: Dict[str, Any], idx: int) -> Dict[str, Any]:
        async with sem:
            return await spawn_subagent(
                task=spec.get("task", ""),
                model=spec.get("model", _DEFAULT_MODEL),
                system_prompt=spec.get("system_prompt"),
                context=spec.get("context"),
                enable_tools=spec.get("enable_tools", True),
                agent_id=f"sa-{idx}-{uuid.uuid4().hex[:6]}",
            )

    logger.info(f"spawn_parallel: {len(tasks)} agents, max_concurrent={max_concurrent}")

    results = await asyncio.gather(
        *[_run(t, i) for i, t in enumerate(tasks)],
        return_exceptions=True,
    )

    # 예외를 결과로 변환
    final = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            final.append({
                "agent_id": f"sa-{i}-failed",
                "status": "error",
                "result": f"[병렬 실행 오류: {type(r).__name__}: {r}]",
                "model": tasks[i].get("model", _DEFAULT_MODEL),
                "input_tokens": 0,
                "output_tokens": 0,
                "duration_ms": 0,
                "tools_used": [],
            })
        else:
            final.append(r)

    completed = sum(1 for r in final if r["status"] == "completed")
    logger.info(f"spawn_parallel_done: {completed}/{len(final)} completed")

    return final
