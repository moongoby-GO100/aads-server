"""채팅 '계획만' 모드(response_mode='plan')의 도구 실행 차단 정책.

프롬프트만으로는 막히지 않는다. 도구 실행 직전(ToolExecutor.execute/_dispatch,
ceo_chat_tools.execute_tool, MCP 브리지)에서 요청-로컬 ContextVar 로 판정하고,
**명시적으로 읽기 전용인 도구만** 통과시킨다. 목록에 없는 도구는 신규·미지 도구를
포함해 전부 차단한다(fail-closed).

이 모드는 요청(턴) 단위다. 세션이나 전역 상태를 바꾸지 않으므로 이미 실행 중인
다른 요청·Runner 에는 소급 적용되지 않는다.
"""
from __future__ import annotations

import json
from contextvars import ContextVar, Token
from typing import Any, Dict, Optional, Tuple

RESPONSE_MODE_PLAN = "plan"

PLAN_MODE_BLOCK_MESSAGE = (
    "계획만 모드이므로 실행하지 않았습니다. 실행하려면 상단 모드를 변경하십시오"
)

current_response_mode: ContextVar[str] = ContextVar("current_response_mode", default="")


def plan_mode_active() -> bool:
    return current_response_mode.get("") == RESPONSE_MODE_PLAN


def set_response_mode(mode: Optional[str]) -> Token:
    normalized = str(mode or "").strip().lower()
    return current_response_mode.set("" if normalized in {"", "quality", "fast"} else RESPONSE_MODE_PLAN)


def is_missing_mode_column_error(exc: BaseException) -> bool:
    """response_mode 컬럼이 아직 없어서 난 오류인가.

    plan 저장은 실패하면 턴을 막으므로, 컬럼이 없는 DB 에는 plan 턴이 존재할 수 없다.
    이 오류만 '저장된 plan 없음(= quality)'으로 볼 수 있고, 다른 조회 실패는 plan 으로 본다.
    """
    try:
        import asyncpg
    except ImportError:
        return False
    return isinstance(exc, asyncpg.exceptions.UndefinedColumnError)


def reset_response_mode(token: Token) -> None:
    try:
        current_response_mode.reset(token)
    except (ValueError, RuntimeError):
        # 다른 Context 에서 reset 되는 경우(async generator 경계) 값만 비운다.
        current_response_mode.set("")


# 부작용이 없다고 코드로 확인한 도구만 올린다. 이름이 '조회'처럼 보인다는 이유로 올리지 않는다.
# 아래 계열은 입력 검증으로 안전을 증명할 수 없어 목록에서 제외한다(이름·인자와 무관하게 차단).
#  - SQL 입력 도구(query_database, query_db, query_project_database 등): SELECT 로 시작하는
#    문장도 pg_logical_emit_message/pg_create_restore_point/임의 UDF 로 쓰기가 가능하고,
#    DB helper 는 SET LOCAL default_transaction_read_only 뿐이라 실제 read-only 가 아니다.
#  - 셸 도구(run_remote_command)와 모델 인자를 셸 문자열로 조립하는 복합 도구
#    (code_explorer, search_all_projects, analyze_changes, read_remote_file, list_remote_dir,
#    git_remote_status, inspect_service).
#  - URL·임의 경로를 받는 조회(fetch_url, jina_read, crawl4ai_fetch, deep_crawl, deep_research,
#    fact_check*, search_crawl_match, read_github_file, read_uploaded_file, google_sheets_read)
#    와 DB 에 직접 접속하는 list_project_databases.
_READ_ONLY_TOOLS = frozenset({
    "plan_read_source", "plan_database_status", "plan_database_schema",
    "health_check", "server_status", "get_all_service_status",
    "dashboard_query", "task_history", "cost_report", "tool_metrics",
    "llm_response_metrics", "tool_layer_audit",
    "handover_search", "canonical_document_lookup", "skill_find", "skill_load",
    "web_search", "web_search_brave", "web_search_naver", "web_search_kakao",
    "web_search_google", "search_searxng", "search_naver_multi",
    "gemini_grounding_search", "gemini_search", "search_naver", "search_kakao",
    "recall_notes", "semantic_code_search", "aag_findings", "aag_brief",
    "check_directive_status", "get_agenda", "list_agendas", "search_agendas",
    "pipeline_runner_status", "pipeline_c_status", "my_milestones",
    "list_scheduled_tasks", "check_task_status", "read_task_logs",
    "query_timeline", "recall_tool_result", "query_decision_graph",
    "pc_list_agents", "device_list", "video_status", "media_job_status",
    "local_model_queue_status", "search_logs", "search_chat_history",
})


def classify(tool_name: str, tool_input: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """(허용 여부, 사유코드). 허용은 목록에 있는 읽기 전용 도구뿐이며 인자는 판정에 쓰지 않는다."""
    if str(tool_name or "").strip() in _READ_ONLY_TOOLS:
        return True, "read_only_tool"
    return False, "tool_not_allowlisted"


def filter_offered_tools(tools: Any) -> Any:
    """모델에 제시하는 도구 목록에서 허용목록에 없는 도구를 뺀다(차단은 check 가 한다)."""
    if not tools:
        return tools
    kept = []
    for tool in tools:
        name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
        if name in _READ_ONLY_TOOLS:
            kept.append(tool)
    return kept


def block_payload(tool_name: str, reason: str) -> str:
    return json.dumps(
        {
            "error": "plan_mode_blocked",
            "blocked": True,
            "plan_mode": True,
            "tool": str(tool_name or ""),
            "reason_code": reason,
            "message": PLAN_MODE_BLOCK_MESSAGE,
        },
        ensure_ascii=False,
    )


def check(tool_name: str, tool_input: Optional[Dict[str, Any]]) -> Optional[str]:
    """계획만 모드이고 차단 대상이면 차단 응답(JSON 문자열), 아니면 None.

    판정 중 예외가 나면 차단한다.
    """
    if not plan_mode_active():
        return None
    try:
        allowed, reason = classify(tool_name, tool_input)
    except Exception:
        allowed, reason = False, "plan_guard_error"
    if allowed:
        return None
    return block_payload(tool_name, reason)
