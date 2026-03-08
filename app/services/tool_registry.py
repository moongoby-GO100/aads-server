"""
AADS-186A: 도구 레지스트리 — Anthropic Tool Use API 포맷
- 각 도구에 input_examples 추가 (실제 AADS 데이터 기반)
- list_remote_dir/read_remote_file/query_database에 response_format 파라미터 추가
- 신규 고수준 워크플로우 도구: inspect_service, get_all_service_status, generate_directive
tool_group: 'system' | 'action' | 'search' | 'workflow' | 'all'
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
        "input_examples": [
            {"server": "all"},
            {"server": "68"},
        ],
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
        "input_examples": [
            {"filter_status": "all"},
            {"filter_status": "running", "limit": 5},
        ],
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
        "input_examples": [
            {"limit": 10},
            {"project": "AADS", "limit": 5},
            {"project": "KIS"},
        ],
    },
    "server_status": {
        "name": "server_status",
        "description": "서버 인프라 상태 요약 조회. Docker 컨테이너 상태, 포트, 메모리 사용량 포함.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "input_examples": [{}],
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
        "input_examples": [
            {
                "task_id": "NTV2-045",
                "title": "NTV2 헬스체크 실패 수정",
                "priority": "P1-HIGH",
                "size": "S",
                "model": "sonnet",
                "description": "NTV2 서버114 헬스체크 엔드포인트가 500 에러 반환. 원인 파악 후 수정.",
            },
            {
                "task_id": "AADS-190",
                "title": "대시보드 통계 카드 추가",
                "priority": "P2-MEDIUM",
                "size": "M",
                "model": "sonnet",
                "description": "ops/page.tsx에 일별 비용 트렌드 차트 추가.",
                "depends_on": "AADS-189",
            },
        ],
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
        "input_examples": [
            {"repo": "moongoby-GO100/aads-docs", "path": "HANDOVER.md"},
            {"repo": "moongoby-GO100/aads-docs", "path": "CEO-DIRECTIVES.md", "branch": "main"},
        ],
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
                "response_format": {
                    "type": "string",
                    "description": "응답 형식. 'concise'(기본, 행 수+핵심 컬럼) | 'detailed'(전체 내용+메타데이터)",
                    "enum": ["concise", "detailed"],
                    "default": "concise",
                },
            },
            "required": ["query"],
        },
        "input_examples": [
            {
                "query": "SELECT count(*) FROM chat_messages WHERE created_at > now() - interval '1 day'",
            },
            {
                "query": "SELECT task_id, title, status FROM directive_lifecycle ORDER BY completed_at DESC LIMIT 10",
                "response_format": "concise",
            },
            {
                "query": "SELECT model_used, SUM(cost) as total_cost FROM chat_messages GROUP BY model_used",
                "response_format": "detailed",
            },
        ],
    },
    "read_remote_file": {
        "name": "read_remote_file",
        "description": "원격 서버의 파일 내용을 읽습니다 (SSH, 프로젝트별 서버 자동 매핑). KIS/GO100/SF/NTV2 프로젝트 지정 가능.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트명 (서버 자동 매핑). KIS, GO100, SF, NTV2 중 하나.",
                    "enum": ["KIS", "GO100", "SF", "NTV2"],
                },
                "path": {
                    "type": "string",
                    "description": "WORKDIR 기준 상대 경로 (예: app/main.py, config.py)",
                },
                "response_format": {
                    "type": "string",
                    "description": "응답 형식. 'concise'(기본, 핵심 내용) | 'detailed'(전체+크기/수정일 메타데이터)",
                    "enum": ["concise", "detailed"],
                    "default": "concise",
                },
            },
            "required": ["project", "path"],
        },
        "input_examples": [
            {"project": "SF", "path": "/data/shortflow/app/main.py"},
            {"project": "KIS", "path": "/root/kis-autotrade-v4/config.py", "response_format": "concise"},
            {"project": "NTV2", "path": "/var/www/newtalk/app/Http/Controllers/AuthController.php", "response_format": "detailed"},
        ],
    },
    "list_remote_dir": {
        "name": "list_remote_dir",
        "description": "원격 서버의 디렉터리/파일을 검색합니다 (SSH). 프로젝트별 서버(KIS/GO100/SF/NTV2)에서 파일 목록 또는 키워드 검색.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트명. KIS, GO100, SF, NTV2 중 하나.",
                    "enum": ["KIS", "GO100", "SF", "NTV2"],
                },
                "path": {
                    "type": "string",
                    "description": "WORKDIR 기준 상대 경로 (선택, 기본: 루트)",
                    "default": "",
                },
                "keyword": {
                    "type": "string",
                    "description": "파일명 검색어 (선택). 포함된 파일만 나열.",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "탐색 깊이 (기본 3, 최대 5)",
                },
                "response_format": {
                    "type": "string",
                    "description": "응답 형식. 'concise'(기본, 파일명 목록) | 'detailed'(크기/수정일/권한 포함)",
                    "enum": ["concise", "detailed"],
                    "default": "concise",
                },
            },
            "required": ["project"],
        },
        "input_examples": [
            {"project": "KIS", "path": "/root/kis-autotrade-v4", "keyword": "config"},
            {"project": "SF", "path": "/data/shortflow", "max_depth": 2, "response_format": "concise"},
            {"project": "NTV2", "keyword": "Controller", "response_format": "detailed"},
        ],
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
        "input_examples": [
            {"days": 7},
            {"days": 30},
        ],
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
        "input_examples": [
            {"query": "FastAPI MCP 통합 가이드"},
            {"query": "AI 에이전트 트렌드 2025", "freshness": "pw"},
            {"query": "LangGraph Tool Use best practices", "count": 8},
        ],
    },
    # ── workflow 그룹 (신규) ──────────────────────────────────────────────────
    "inspect_service": {
        "name": "inspect_service",
        "description": (
            "서비스 종합 점검. 지정된 프로젝트 서버에 접속하여 프로세스, Docker 컨테이너, "
            "최근 로그, 헬스체크를 수행하고 결과를 요약합니다. "
            "checks 파라미터로 수행할 점검 항목을 선택할 수 있습니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "점검할 프로젝트명. KIS, GO100, SF, NTV2 중 하나.",
                    "enum": ["KIS", "GO100", "SF", "NTV2"],
                },
                "checks": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["process", "docker", "log_tail", "health", "all"],
                    },
                    "description": "수행할 점검 항목 리스트. 기본: ['all'] (전체 수행)",
                    "default": ["all"],
                },
            },
            "required": ["project"],
        },
        "input_examples": [
            {"project": "NTV2"},
            {"project": "KIS", "checks": ["process", "health"]},
            {"project": "SF", "checks": ["docker", "log_tail"]},
        ],
    },
    "get_all_service_status": {
        "name": "get_all_service_status",
        "description": (
            "6개 서비스(AADS/KIS/GO100/SF/NTV2/NAS) 헬스체크를 병렬로 수행하여 "
            "마크다운 테이블 형태로 반환합니다. 전체 서비스 상태 대시보드 용도."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "include_details": {
                    "type": "boolean",
                    "description": "응답 시간, 버전 등 상세 정보 포함 여부 (기본 false)",
                },
            },
            "required": [],
        },
        "input_examples": [
            {},
            {"include_details": True},
        ],
    },
    "generate_directive": {
        "name": "generate_directive",
        "description": (
            "CEO 자연어 설명으로 AADS 형식 지시서를 자동 생성합니다. "
            "TASK_ID를 자동 채번하고, auto_submit=true 시 API로 바로 제출합니다. "
            "지시서 작성이 필요할 때 directive_create 대신 이 도구를 사용하면 "
            "자연어 설명만으로 완성된 지시서를 얻을 수 있습니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "CEO가 원하는 작업의 자연어 설명",
                },
                "priority": {
                    "type": "string",
                    "description": "우선순위. 기본 'P1-HIGH'",
                    "enum": ["P0-CRITICAL", "P1-HIGH", "P2-MEDIUM", "P3-LOW"],
                },
                "size": {
                    "type": "string",
                    "description": "작업 크기. 기본 'M'",
                    "enum": ["XS", "S", "M", "L", "XL"],
                },
                "project": {
                    "type": "string",
                    "description": "대상 프로젝트 (TASK_ID 채번에 사용). 기본 'AADS'",
                },
                "auto_submit": {
                    "type": "boolean",
                    "description": "true 시 지시서를 API로 즉시 제출. 기본 false (CEO 확인 후 제출)",
                },
            },
            "required": ["description"],
        },
        "input_examples": [
            {
                "description": "NTV2 헬스체크 엔드포인트가 500 에러 반환. 원인 파악 후 수정 필요.",
                "priority": "P1-HIGH",
                "size": "S",
                "project": "NTV2",
            },
            {
                "description": "KIS 자동매매 일일 손익 리포트를 Telegram으로 전송하는 기능 추가",
                "priority": "P2-MEDIUM",
                "size": "M",
                "project": "KIS",
                "auto_submit": False,
            },
        ],
    },
}

# ─── 그룹 → 도구 매핑 ─────────────────────────────────────────────────────────

_GROUPS: Dict[str, List[str]] = {
    "system": ["health_check", "dashboard_query", "task_history", "server_status"],
    "action": ["directive_create", "read_github_file", "query_database", "read_remote_file", "list_remote_dir", "cost_report"],
    "search": ["web_search_brave"],
    "workflow": ["inspect_service", "get_all_service_status", "generate_directive"],
    "all": list(_TOOLS.keys()),
}


class ToolRegistry:
    """Anthropic Tool Use API 포맷으로 도구 목록 반환."""

    def get_tools(self, group: str) -> List[Dict[str, Any]]:
        """
        group에 해당하는 도구 목록을 Anthropic Tool Use 포맷으로 반환.
        input_examples는 Anthropic API 비지원 필드이므로 제외.

        Args:
            group: 'system' | 'action' | 'search' | 'workflow' | 'all' | ''

        Returns:
            Anthropic messages.create(tools=...) 파라미터용 리스트
        """
        if not group:
            return []
        tool_names = _GROUPS.get(group, [])
        result = []
        for name in tool_names:
            if name not in _TOOLS:
                continue
            # input_examples는 API 전송 시 제외
            tool = {k: v for k, v in _TOOLS[name].items() if k != "input_examples"}
            result.append(tool)
        return result

    def get_tool(self, name: str) -> Dict[str, Any]:
        return _TOOLS.get(name, {})

    def get_tool_examples(self, name: str) -> List[Dict[str, Any]]:
        """도구의 input_examples 반환 (테스트/문서화용)."""
        return _TOOLS.get(name, {}).get("input_examples", [])

    def list_all(self) -> List[str]:
        return list(_TOOLS.keys())

    def list_groups(self) -> Dict[str, List[str]]:
        return dict(_GROUPS)
