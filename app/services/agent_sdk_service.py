"""
AADS-188C: Claude Agent SDK 서비스 — bridge.py 파이프라인 대체.
CEO Chat에서 실시간 자율 실행 루프 지원.
Agent SDK primary + 기존 AutonomousExecutor fallback 구조.

환경 플래그: AGENT_SDK_ENABLED=true (기본값)
max_turns=30, max_budget_usd=10 (환경 변수로 CEO 조정 가능)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncGenerator, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─── 환경 플래그 ───────────────────────────────────────────────────────────────

AGENT_SDK_ENABLED: bool = os.getenv("AGENT_SDK_ENABLED", "true").lower() == "true"
# 무한 대화 지원: 턴/예산 제한 제거 (0 = 무제한)
_MAX_TURNS: int = int(os.getenv("AGENT_SDK_MAX_TURNS", "0"))
_MAX_BUDGET_USD: float = float(os.getenv("AGENT_SDK_MAX_BUDGET_USD", "0"))
_CWD: str = os.getenv("AGENT_SDK_CWD", "/root/aads")

# ─── SDK 임포트 (graceful degradation) ────────────────────────────────────────

try:
    from claude_agent_sdk import (  # type: ignore[import]
        ClaudeSDKClient,
        ClaudeAgentOptions,
        HookMatcher,
        AssistantMessage,
        TextBlock,
        ResultMessage,
        SystemMessage,
        tool as sdk_tool,
        create_sdk_mcp_server,
        CLINotFoundError,
        CLIConnectionError,
    )
    _SDK_AVAILABLE = True
    logger.info("claude-agent-sdk 로드 완료")
except ImportError:
    _SDK_AVAILABLE = False
    logger.warning("claude-agent-sdk 미설치 — bridge.py fallback 모드 활성화")

    # 타입 힌트용 더미
    ClaudeSDKClient = None  # type: ignore[assignment,misc]
    ClaudeAgentOptions = None  # type: ignore[assignment,misc]
    HookMatcher = None  # type: ignore[assignment,misc]
    AssistantMessage = None  # type: ignore[assignment,misc]
    TextBlock = None  # type: ignore[assignment,misc]
    ResultMessage = None  # type: ignore[assignment,misc]
    SystemMessage = None  # type: ignore[assignment,misc]
    CLINotFoundError = RuntimeError  # type: ignore[assignment,misc]
    CLIConnectionError = RuntimeError  # type: ignore[assignment,misc]


# ─── 도구 등급 (Green/Yellow/Red) ─────────────────────────────────────────────

_TOOL_GRADES: Dict[str, str] = {
    # Green: 항상 허용 (읽기/조회 전용)
    "health_check":         "Green",
    "query_database":       "Green",
    "query_project_database": "Yellow",
    "list_project_databases":  "Green",
    "export_data":             "Yellow",
    "schedule_task":           "Yellow",
    "unschedule_task":         "Yellow",
    "list_scheduled_tasks":    "Green",
    "read_remote_file":     "Green",
    "list_remote_dir":      "Green",
    "cost_report":          "Green",
    "jina_read":            "Green",
    "code_explorer":        "Green",
    "analyze_changes":      "Green",
    "save_note":            "Green",
    "recall_notes":         "Green",
    "semantic_code_search": "Green",
    # Yellow: CEO 확인 권장 (쓰기/부작용)
    "write_remote_file":    "Yellow",
    "patch_remote_file":    "Yellow",
    "run_remote_command":   "Yellow",
    "git_remote_add":       "Yellow",
    "git_remote_commit":    "Yellow",
    "git_remote_push":      "Yellow",
    "git_remote_status":    "Green",
    "git_remote_create_branch": "Yellow",
    "deep_crawl":           "Yellow",
    "deep_research":        "Yellow",
    # AADS-190: 서브에이전트 (Yellow — LLM 비용 발생)
    "spawn_subagent":       "Yellow",
    "spawn_parallel_subagents": "Yellow",
    # Red: 항상 차단 (파이프라인 트리거)
    "directive_create":     "Red",
    "submit_directive":     "Red",
}

_GREEN_TOOLS: List[str] = [k for k, v in _TOOL_GRADES.items() if v == "Green"]
_YELLOW_TOOLS: List[str] = [k for k, v in _TOOL_GRADES.items() if v == "Yellow"]

# SDK Built-in 도구 (파일 읽기/쓰기/실행 전체 허용 — CEO 승인 완료)
_BUILTIN_ALLOWED: List[str] = [
    "Read", "Glob", "Grep",        # 읽기
    "Write", "Edit",                # 쓰기 (agent_hooks.py에서 민감 경로 차단)
    "Bash",                         # 실행 (agent_hooks.py에서 위험 명령 차단)
]


# ─── AADS 도구 → SDK MCP @tool 래퍼 ─────────────────────────────────────────

def _build_aads_sdk_tools() -> list:
    """
    AADS ToolExecutor 도구를 SDK @tool 데코레이터로 래핑.
    ToolExecutor.execute()를 통해 타임아웃/에러핸들링을 재사용.
    """
    if not _SDK_AVAILABLE:
        return []

    from app.services.tool_executor import ToolExecutor
    _exec = ToolExecutor()

    def _wrap(name: str, description: str, schema: Dict[str, Any]):
        """단일 AADS 도구 → SDK @tool 래퍼 팩토리."""
        @sdk_tool(name, description, schema)
        async def _handler(args: Dict[str, Any]) -> Dict[str, Any]:
            result = await _exec.execute(name, args)
            return {"content": [{"type": "text", "text": result}]}
        _handler.__name__ = f"_sdk_{name}"
        return _handler

    tools = [
        _wrap("health_check",   "서버 68 및 6개 서비스 헬스체크",
              {"server": str}),
        _wrap("query_database", "PostgreSQL SELECT 쿼리 실행 (읽기 전용, AADS 내부 DB)",
              {"query": str, "limit": int}),
        _wrap("query_project_database", "프로젝트별 원격 DB SELECT 쿼리 (KIS/GO100/SF/NTV2)",
              {"project": str, "query": str, "limit": int}),
        _wrap("list_project_databases", "프로젝트 DB 목록 및 연결 상태 조회",
              {}),
        _wrap("read_remote_file", "원격 서버(68/211/114) 파일 읽기",
              {"path": str, "server": str}),
        _wrap("list_remote_dir", "원격 서버 디렉토리 파일 목록",
              {"path": str, "server": str}),
        _wrap("cost_report",    "프로젝트별 비용 리포트 조회",
              {"days": int, "project": str}),
        _wrap("jina_read",      "URL 콘텐츠를 마크다운으로 읽기 (Jina Reader)",
              {"url": str}),
        _wrap("code_explorer",  "6개 프로젝트 코드베이스 탐색 및 분석",
              {"query": str, "project": str, "depth": int}),
        _wrap("analyze_changes", "코드 변경사항 영향 범위 분석 (CKP 기반)",
              {"path": str, "project": str}),
        _wrap("save_note",      "세션 노트/관찰 저장 (메모리 레이어)",
              {"title": str, "content": str}),
        _wrap("recall_notes",   "저장된 노트 의미론적 검색",
              {"query": str, "limit": int}),
        _wrap("semantic_code_search", "전체 프로젝트 시맨틱 코드 검색",
              {"query": str, "project": str, "limit": int}),
        _wrap("deep_research",  "Gemini 딥리서치 — 다수 소스 탐색 종합 (Yellow)",
              {"query": str, "max_sources": int}),
        # AADS-190: 원격 파일 쓰기/패치/명령 실행 (Yellow)
        _wrap("write_remote_file", "원격 서버(68/211/114) 파일 쓰기 — 자동 백업 포함 (Yellow)",
              {"project": str, "file_path": str, "content": str, "backup": bool}),
        _wrap("patch_remote_file", "원격 서버 파일 부분 수정 — old→new 교체 (Yellow)",
              {"project": str, "file_path": str, "old_string": str, "new_string": str}),
        _wrap("run_remote_command", "원격 서버 명령 실행 — 화이트리스트 기반 (Yellow)",
              {"project": str, "command": str}),
        # AADS-190: Git 원격 쓰기 도구 (Yellow)
        _wrap("git_remote_add", "원격 서버 git add (파일 스테이징)",
              {"project": str, "files": str}),
        _wrap("git_remote_commit", "원격 서버 git commit",
              {"project": str, "message": str}),
        _wrap("git_remote_push", "원격 서버 git push (force push 차단)",
              {"project": str, "branch": str}),
        _wrap("git_remote_status", "원격 서버 git status",
              {"project": str}),
        _wrap("git_remote_create_branch", "원격 서버 새 브랜치 생성",
              {"project": str, "branch_name": str}),
        # AADS-190 Phase2-A: 서브에이전트
        _wrap("spawn_subagent", "독립적 서브에이전트 실행 — 복잡한 작업을 분할 위임 (Yellow)",
              {"task": str, "model": str, "context": str, "enable_tools": bool}),
        _wrap("spawn_parallel_subagents", "여러 서브에이전트를 병렬 실행 후 결과 취합 (Yellow)",
              {"tasks": list, "max_concurrent": int}),
        # AADS-190: 내보내기 + 스케줄러
        _wrap("export_data", "데이터를 Excel/CSV/PDF로 내보내기 — 다운로드 링크 제공 (Yellow)",
              {"data": list, "project": str, "query": str, "format": str, "title": str}),
        _wrap("schedule_task", "예약 작업 등록 — cron/interval/once (Yellow)",
              {"name": str, "schedule_type": str, "action_type": str, "action_config": dict}),
        _wrap("unschedule_task", "예약 작업 삭제 (Yellow)",
              {"name": str}),
        _wrap("list_scheduled_tasks", "예약 작업 목록 조회",
              {}),
    ]

    logger.debug(f"_build_aads_sdk_tools: {len(tools)}개 도구 생성")
    return tools


# ─── AgentSDKService ──────────────────────────────────────────────────────────

class AgentSDKService:
    """
    AADS-188C: Claude Agent SDK 자율 실행 서비스.

    - CEO Chat에서 execute/code_modify 인텐트 처리
    - AADS 전용 도구 MCP 서버 내장
    - PreToolUse/PostToolUse 훅으로 위험 명령 차단
    - session_id resume으로 대화 이어서 실행
    - SDK 장애 시 AutonomousExecutor fallback
    """

    def __init__(
        self,
        max_turns: Optional[int] = None,
        max_budget_usd: Optional[float] = None,
    ) -> None:
        self.max_turns = max_turns or _MAX_TURNS
        self.max_budget_usd = max_budget_usd or _MAX_BUDGET_USD
        self._mcp_server: Any = None

    def is_available(self) -> bool:
        """SDK 사용 가능 여부 확인."""
        return _SDK_AVAILABLE and AGENT_SDK_ENABLED

    def _get_mcp_server(self) -> Any:
        """AADS 도구 MCP 서버 지연 초기화 (싱글턴)."""
        if not _SDK_AVAILABLE:
            return None
        if self._mcp_server is None:
            tools = _build_aads_sdk_tools()
            self._mcp_server = create_sdk_mcp_server("aads-tools", tools=tools)
            logger.info(f"AgentSDKService: AADS MCP 서버 초기화 ({len(tools)}개 도구)")
        return self._mcp_server

    def _build_options(
        self,
        resume_session_id: Optional[str] = None,
    ) -> Any:
        """ClaudeAgentOptions 구성."""
        from app.services.agent_hooks import pre_tool_use_hook, post_tool_use_hook

        mcp_server = self._get_mcp_server()
        mcp_servers = {"aads": mcp_server} if mcp_server else {}

        # 훅: 전체 도구 자동 승인 (위험 패턴만 차단)
        hooks = {
            "PreToolUse": [
                HookMatcher(
                    matcher=None,  # 모든 도구에 적용
                    hooks=[pre_tool_use_hook],
                )
            ],
            "PostToolUse": [
                HookMatcher(
                    matcher="Write|Edit|write_remote_file|patch_remote_file|run_remote_command|git_remote_commit|git_remote_push|spawn_subagent|spawn_parallel_subagents",
                    hooks=[post_tool_use_hook],
                )
            ],
        }

        options = ClaudeAgentOptions(
            cwd=_CWD,
            model="claude-opus-4-6",
            max_turns=self.max_turns,
            max_budget_usd=self.max_budget_usd,
            permission_mode="default",  # 훅에서 자동 승인 (root 환경 bypassPermissions 불가)
            mcp_servers=mcp_servers,
            hooks=hooks,
            allowed_tools=_BUILTIN_ALLOWED + _GREEN_TOOLS + _YELLOW_TOOLS,
            system_prompt=(
                "당신은 AADS 자율 실행 에이전트입니다. "
                "CEO moongoby의 요청을 처리하며 /root/aads 코드베이스와 6개 서비스를 관리합니다. "
                "파괴적 작업(rm -rf, DROP TABLE, shutdown)은 반드시 먼저 CEO에게 확인하세요."
            ),
        )

        if resume_session_id:
            # resume은 속성으로 직접 설정
            try:
                options.resume = resume_session_id
            except AttributeError:
                pass

        return options

    async def execute_stream(
        self,
        prompt: str,
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Agent SDK 실행 — CEO Chat SSE 스트림.
        session_id 제공 시 이전 세션 resume.

        SSE 이벤트:
          sdk_session   : 세션 ID 캡처
          delta         : 텍스트 조각
          sdk_complete  : 완료 (stop_reason 포함)
          error         : 오류
        """
        if not self.is_available():
            raise RuntimeError(
                "Agent SDK 사용 불가 (미설치 또는 AGENT_SDK_ENABLED=false)"
            )

        from claude_agent_sdk import query as sdk_query  # type: ignore[import]

        options = self._build_options(resume_session_id=session_id)
        captured_session_id: Optional[str] = None

        try:
            HEARTBEAT_SSE = f'data: {json.dumps({"type": "heartbeat"})}\n\n'
            sdk_iter = sdk_query(prompt=prompt, options=options).__aiter__()

            while True:
                # 8초 내에 SDK 메시지가 안 오면 heartbeat 전송 (Cloudflare 100s 대비)
                try:
                    message = await asyncio.wait_for(
                        sdk_iter.__anext__(), timeout=8.0
                    )
                except asyncio.TimeoutError:
                    yield HEARTBEAT_SSE
                    continue
                except StopAsyncIteration:
                    break

                # ── 세션 ID 캡처 ────────────────────────────────────────────
                if isinstance(message, SystemMessage) and getattr(message, "subtype", "") == "init":
                    _data = getattr(message, "data", {}) or {}
                    captured_session_id = _data.get("session_id", "")
                    yield f"data: {json.dumps({'type': 'sdk_session', 'session_id': captured_session_id})}\n\n"

                # ── 텍스트 스트리밍 ─────────────────────────────────────────
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text:
                            yield f"data: {json.dumps({'type': 'delta', 'content': block.text})}\n\n"

                # ── 최종 결과 ───────────────────────────────────────────────
                elif isinstance(message, ResultMessage):
                    if message.result:
                        yield f"data: {json.dumps({'type': 'delta', 'content': message.result})}\n\n"
                    yield f"data: {json.dumps({'type': 'sdk_complete', 'session_id': captured_session_id, 'stop_reason': getattr(message, 'stop_reason', 'end_turn')})}\n\n"

                # ── 기타 메시지 → heartbeat 겸 keep-alive ──────────────────
                else:
                    yield HEARTBEAT_SSE

        except CLINotFoundError:
            msg = "Claude Code CLI 미설치. 설치: pip install claude-agent-sdk"
            logger.error(f"AgentSDKService: {msg}")
            yield f"data: {json.dumps({'type': 'error', 'content': msg})}\n\n"
            raise RuntimeError(msg)

        except CLIConnectionError as e:
            msg = f"Agent SDK 연결 오류: {e}"
            logger.error(f"AgentSDKService: {msg}")
            yield f"data: {json.dumps({'type': 'error', 'content': msg})}\n\n"
            raise RuntimeError(msg)

        except Exception as e:
            msg = f"Agent SDK 실행 오류: {type(e).__name__}: {e}"
            logger.exception(f"AgentSDKService: {msg}")
            yield f"data: {json.dumps({'type': 'error', 'content': msg})}\n\n"
            raise


# ─── 싱글턴 ───────────────────────────────────────────────────────────────────

_instance: Optional[AgentSDKService] = None


def get_agent_sdk_service() -> AgentSDKService:
    """싱글턴 AgentSDKService 반환."""
    global _instance
    if _instance is None:
        _instance = AgentSDKService()
    return _instance
