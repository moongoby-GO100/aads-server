"""
AADS-185: 도구 레지스트리 — Anthropic Tool Use API 포맷
tool_group: 'system' | 'action' | 'search' | 'all'
"""
from __future__ import annotations

from typing import Any, Dict, List

# ─── 도구 스키마 정의 (Anthropic Tool Use 포맷) ──────────────────────────────

_TOOLS: Dict[str, Dict[str, Any]] = {
    # ── system 그룹 ──────────────────────────────────────────────────────────
    "health_check": {
        "name": "health_check",
        "description": "AADS 서버 헬스체크. 서버68/211/114의 프로세스, 메모리, 디스크 상태를 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "server": {
                    "type": "string",
                    "description": "조회할 서버. 'all'(기본), '68', '211', '114'",
                    "enum": ["all", "68", "211", "114"],
                }
            },
            "required": [],
        },
    },
    "dashboard_query": {
        "name": "dashboard_query",
        "description": "AADS 파이프라인 대시보드 조회. pending/running/done 작업 목록, 회로 차단기 상태, 최근 실행 내역을 반환합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_status": {
                    "type": "string",
                    "description": "필터링할 상태. 'all'(기본), 'pending', 'running', 'done', 'failed'",
                    "enum": ["all", "pending", "running", "done", "failed"],
                },
                "limit": {
                    "type": "integer",
                    "description": "반환할 최대 항목 수 (기본 10)",
                },
            },
            "required": [],
        },
    },
    "task_history": {
        "name": "task_history",
        "description": "최근 완료/실패한 작업 이력을 DB에서 조회합니다. task_id, title, status, completed_at 포함.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "반환할 최대 항목 수 (기본 10)",
                },
                "project": {
                    "type": "string",
                    "description": "필터링할 프로젝트명 (예: 'AADS', 'SF', 'KIS'). 미입력 시 전체.",
                },
            },
            "required": [],
        },
    },
    "server_status": {
        "name": "server_status",
        "description": "서버 인프라 상태 요약 조회. Docker 컨테이너 상태, 포트, 메모리 사용량 포함.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    # ── action 그룹 ──────────────────────────────────────────────────────────
    "directive_create": {
        "name": "directive_create",
        "description": "지시서 블록을 생성합니다. >>>DIRECTIVE_START 포맷으로 반환. CEO 확인 후 시스템에 제출됩니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "작업 ID (예: AADS-186)"},
                "title": {"type": "string", "description": "작업 제목"},
                "priority": {"type": "string", "enum": ["P0-CRITICAL", "P1-HIGH", "P2-MEDIUM", "P3-LOW"]},
                "size": {"type": "string", "enum": ["XS", "S", "M", "L", "XL"]},
                "model": {"type": "string", "enum": ["haiku", "sonnet", "opus"]},
                "description": {"type": "string", "description": "작업 상세 설명"},
                "depends_on": {"type": "string", "description": "선행 작업 ID (선택)"},
            },
            "required": ["task_id", "title", "priority", "size", "model", "description"],
        },
    },
    "read_github_file": {
        "name": "read_github_file",
        "description": "GitHub raw 파일 내용을 읽습니다. HANDOVER.md, CEO-DIRECTIVES.md 등 문서 조회에 사용합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "저장소 경로 (예: 'moongoby-GO100/aads-docs')",
                },
                "path": {
                    "type": "string",
                    "description": "파일 경로 (예: 'HANDOVER.md', 'CEO-DIRECTIVES.md')",
                },
                "branch": {
                    "type": "string",
                    "description": "브랜치 이름 (기본: 'main')",
                },
            },
            "required": ["repo", "path"],
        },
    },
    "query_database": {
        "name": "query_database",
        "description": "PostgreSQL DB에 SELECT 쿼리를 실행합니다. 읽기 전용 (SELECT만 허용).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "실행할 SELECT SQL 쿼리 (SELECT만 허용, 최대 1000자)",
                },
                "limit": {
                    "type": "integer",
                    "description": "반환할 최대 행 수 (기본 20, 최대 100)",
                },
            },
            "required": ["query"],
        },
    },
    "read_remote_file": {
        "name": "read_remote_file",
        "description": "원격 서버의 파일 내용을 읽습니다 (SSH 우회, AADS API 경유).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "읽을 파일의 절대 경로 (예: '/root/aads/aads-server/app/main.py')",
                },
            },
            "required": ["path"],
        },
    },
    "cost_report": {
        "name": "cost_report",
        "description": "LiteLLM API 비용 사용 내역을 조회합니다. 일별/모델별 비용 분석.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "조회할 기간 (일 단위, 기본 7)",
                },
            },
            "required": [],
        },
    },
    # ── search 그룹 ──────────────────────────────────────────────────────────
    "web_search_brave": {
        "name": "web_search_brave",
        "description": "Brave Search API로 웹 검색을 수행합니다. 최신 정보, 뉴스, 기술 문서 검색에 사용합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "검색 쿼리 (한국어 또는 영어)",
                },
                "count": {
                    "type": "integer",
                    "description": "반환할 결과 수 (기본 5, 최대 10)",
                },
                "freshness": {
                    "type": "string",
                    "description": "최신성 필터. 'pd'(24시간), 'pw'(1주), 'pm'(1달), 'py'(1년)",
                    "enum": ["pd", "pw", "pm", "py"],
                },
            },
            "required": ["query"],
        },
    },
}

# ─── 그룹 → 도구 매핑 ─────────────────────────────────────────────────────────

_GROUPS: Dict[str, List[str]] = {
    "system": ["health_check", "dashboard_query", "task_history", "server_status"],
    "action": ["directive_create", "read_github_file", "query_database", "read_remote_file", "cost_report"],
    "search": ["web_search_brave"],
    "all": list(_TOOLS.keys()),
}


class ToolRegistry:
    """Anthropic Tool Use API 포맷으로 도구 목록 반환."""

    def get_tools(self, group: str) -> List[Dict[str, Any]]:
        """
        group에 해당하는 도구 목록을 Anthropic Tool Use 포맷으로 반환.

        Args:
            group: 'system' | 'action' | 'search' | 'all' | ''

        Returns:
            Anthropic messages.create(tools=...) 파라미터용 리스트
        """
        if not group:
            return []
        tool_names = _GROUPS.get(group, [])
        return [_TOOLS[name] for name in tool_names if name in _TOOLS]

    def get_tool(self, name: str) -> Dict[str, Any]:
        return _TOOLS.get(name, {})

    def list_all(self) -> List[str]:
        return list(_TOOLS.keys())
