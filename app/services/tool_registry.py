"""
AADS-186A/186D: 도구 레지스트리 — Anthropic Tool Use API 포맷
- 각 도구에 input_examples 추가 (실제 AADS 데이터 기반)
- list_remote_dir/read_remote_file/query_database에 response_format 파라미터 추가
- 신규 고수준 워크플로우 도구: inspect_service, get_all_service_status, generate_directive
- AADS-186D: Tool Search Tool — defer_loading 메타데이터 추가
  * defer_loading: false → 상시 로드 (항상 Anthropic API에 포함)
  * defer_loading: true  → 온디맨드 (Tool Search Tool 검색 후 사용)
tool_group: 'system' | 'action' | 'search' | 'workflow' | 'all'
"""
from __future__ import annotations

from typing import Any, Dict, List

# ─── AADS-186D: defer_loading 분류 ───────────────────────────────────────────
# false (상시 로드): AI가 매 요청마다 반드시 알아야 하는 핵심 도구
# true  (온디맨드):  특정 작업 시에만 필요한 도구
_DEFER_LOADING: Dict[str, bool] = {
    # ── Tier 1: 상시 로드 (내부 데이터, 빈번 사용) ──────────────────────
    "health_check": False,
    "get_all_service_status": False,
    "check_directive_status": False,
    "check_task_status": False,           # 작업 모니터 — 상시 로드
    "read_task_logs": False,              # 작업 로그 — 상시 로드
    "terminate_task": False,              # 작업 강제종료 — 상시 로드
    "capture_screenshot": False,          # 스크린샷 캡처 — 상시 로드
    "read_remote_file": False,           # 코드 분석 1순위 — 상시 로드
    "query_database": False,             # DB 조회 2순위 — 상시 로드
    "query_project_database": False,     # 프로젝트 DB 조회 — 상시 로드
    "list_project_databases": True,      # DB 목록 — 온디맨드
    "task_history": False,               # 작업 현황 — 빈번 조회
    "list_remote_dir": False,            # 파일 탐색 — 빈번 사용
    "dashboard_query": True,
    "server_status": True,
    "read_github_file": True,
    # ── Tier 2: 분석/탐색 (온디맨드) ─────────────────────────────────────
    "code_explorer": True,
    "semantic_code_search": True,
    "analyze_changes": True,
    "inspect_service": True,
    # ── Tier 3: 액션/실행 ────────────────────────────────────────────────
    "directive_create": False,           # 지시서 — 핵심 액션
    "generate_directive": False,
    "delegate_to_agent": False,          # Orchestrator 핵심
    "delegate_to_research": False,
    "spawn_subagent": False,              # 서브에이전트 — 핵심 위임 도구
    "spawn_parallel_subagents": True,     # 병렬 서브에이전트 — 지연 로드
    "save_note": True,
    "recall_notes": True,
    "delete_note": True,
    "learn_pattern": True,
    "cost_report": True,
    # ── Tier 4: 외부 검색 (온디맨드, API 비용) ───────────────────────────
    "web_search_brave": True,
    "web_search": True,
    "jina_read": True,
    "crawl4ai_fetch": True,
    # ── Tier 5: 고비용/장시간 (온디맨드) ─────────────────────────────────
    "deep_research": True,
    "deep_crawl": True,
    "search_all_projects": True,
    # ── Tier 6: 브라우저 보조 (핵심 2개 상시로드, 나머지 온디맨드) ──────
    "browser_navigate": False,     # 상시 로드 — AI가 항상 브라우저 접근 가능
    "browser_snapshot": False,     # 상시 로드 — 페이지 구조 확인 필수
    "browser_screenshot": True,
    "browser_click": True,
    "browser_fill": True,
    "browser_tab_list": True,
    # ── 기타 ─────────────────────────────────────────────────────────────
    "code_execution": True,
    "observe": True,
    # ── AADS-190: 내보내기 + 스케줄러 ──────────────────────────────────
    "export_data": True,              # 온디맨드
    "schedule_task": True,            # 온디맨드
    "read_uploaded_file": False,      # 첨부파일 재읽기 — 상시 로드
    "unschedule_task": True,
    "list_scheduled_tasks": True,
    # ── Pipeline C: 자율 작업 파이프라인 ──────────────────────────────
    "pipeline_c_start": False,        # 상시 로드 — CEO 핵심 도구
    "pipeline_c_status": False,
    "pipeline_c_approve": False,
    # ── 원격 쓰기/실행/Git 도구 (AADS-190) ──────────────────────────
    "write_remote_file": False,       # 코드 수정 핵심 — 상시 로드
    "patch_remote_file": False,       # 코드 수정 핵심 — 상시 로드
    "run_remote_command": False,      # 명령 실행 핵심 — 상시 로드
    "git_remote_add": True,           # Git — 온디맨드
    "git_remote_commit": True,
    "git_remote_push": True,
    "git_remote_status": True,
    "git_remote_create_branch": True,
}

# 도구 카테고리 안내 (시스템 프롬프트 주입용 — context_builder.py에서 사용)
TOOL_CATEGORY_GUIDE = """\
## 도구 우선순위 가이드 (총 43개)

### 🔴 Tier 1 — 즉시 사용 (내부 데이터, 무료, <3초) ★ 최우선
- read_remote_file: 원격 서버 소스 코드/설정 읽기 (AADS/KIS/GO100/SF/NTV2) — 코드 분석 1순위
- list_remote_dir: 원격 디렉터리 탐색/검색
- query_database: PostgreSQL SELECT — 데이터 확인 2순위 (AADS 내부 DB)
- query_project_database: 프로젝트별 원격 DB SELECT — AADS/KIS/GO100/SF/NTV2
- health_check: 서버 헬스체크
- get_all_service_status: 6개 서비스 상태 병렬 조회
- check_directive_status: 지시사항 진행 종합 확인
- task_history: 작업 이력
- dashboard_query: 파이프라인 현황
- server_status: Docker 컨테이너 상태
- read_github_file: GitHub 문서 읽기

### 🟠 Tier 2 — 분석/탐색 (내부, 무료, 3~15초)
- code_explorer: 함수 호출 체인 추적 (depth 3)
- semantic_code_search: 벡터 코드 검색
- analyze_changes: Git 변경 + 위험도
- inspect_service: 서비스 종합 점검

### 🟡 Tier 3 — 액션/실행 (요청 시 즉시)
- write_remote_file: 원격 서버 파일 쓰기 (자동 백업, SSH)
- patch_remote_file: 원격 서버 파일 부분 수정 (diff 기반 패치)
- run_remote_command: 원격 서버 명령 실행 (화이트리스트 제한)
- git_remote_add/commit/push/status/create_branch: 원격 Git 조작
- directive_create / generate_directive: 지시서 생성
- delegate_to_agent: 복잡한 작업 위임
- delegate_to_research: 심층 리서치 위임
- save_note / recall_notes / delete_note / learn_pattern: 기억 관리
- cost_report: 비용 분석

### 🟢 Tier 4 — 외부 검색 (API 비용, 3~10초)
- web_search_brave / web_search: 통합 웹 검색 (Google/Naver/Kakao 자동 폴백)
- jina_read / crawl4ai_fetch: URL 페이지 추출

### 🔵 Tier 5 — 고비용/장시간 (CEO 명시 요청 시)
- deep_research: Gemini Deep Research ($2~5, 3~10분)
- deep_crawl: 다수 URL 동시 크롤링
- search_all_projects: 6개 프로젝트 동시 검색

### ⚪ Tier 6 — 브라우저 보조 (소스 분석 후 렌더링 확인 시)
- browser_navigate/snapshot/screenshot/click/fill/tab_list

### Agent SDK (execute/code_modify 인텐트 시 자동 활성화)
- 코드 수정/작성, Bash, git — 자율 실행. 위험 명령 자동 차단.

### 🟣 Pipeline C — 자율 작업 파이프라인 (CEO 요청 시)
- pipeline_c_start: Claude Code 자율 작업 시작 (작업→검수→재지시→승인대기)
- pipeline_c_status: 파이프라인 진행 상태 확인
- pipeline_c_approve: CEO 승인/거부 → 배포 또는 원복\
"""

# ─── AADS-188C Phase 2: 인텐트별 필수 도구 매핑 ──────────────────────────────
# 이 매핑에 있는 인텐트는 반드시 해당 도구가 호출되어야 한다.
INTENT_REQUIRED_TOOLS: Dict[str, list] = {
    # Tier 1: 반드시 해당 도구 호출 필요
    "task_query":         ["check_directive_status", "check_task_status"],
    "task_terminate":     ["terminate_task"],
    "screenshot":         ["capture_screenshot"],
    "status_check":       ["check_directive_status", "check_task_status", "get_all_service_status"],
    "health_check":       ["health_check"],
    "all_service_status": ["get_all_service_status"],
    "cost_report":        ["cost_report"],
    "dashboard":          ["dashboard_query"],
    "database_query":     ["query_project_database", "query_database"],
    "project_db":         ["query_project_database", "list_project_databases"],
    "export":             ["export_data"],
    "scheduler":          ["schedule_task", "list_scheduled_tasks"],
    "pipeline_c":         ["pipeline_c_start", "pipeline_c_status", "pipeline_c_approve"],
    "task_history":       ["task_history"],
    "file_read":          ["read_uploaded_file"],
    # Tier 2: 분석 인텐트
    "cto_code_analysis":  ["read_remote_file"],         # 소스 코드 우선
    "code_explorer":      ["code_explorer"],
    "analyze_changes":    ["analyze_changes"],
    "service_inspection": ["inspect_service"],
    # Tier 2.5: 코드 수정/배포 인텐트
    "code_modify":        ["read_remote_file", "write_remote_file", "patch_remote_file", "run_remote_command"],
    "code_fix":           ["read_remote_file", "patch_remote_file", "run_remote_command"],
    "deploy":             ["run_remote_command", "git_remote_status", "git_remote_add", "git_remote_commit", "git_remote_push"],
    "git_operation":      ["git_remote_status", "git_remote_add", "git_remote_commit", "git_remote_push", "git_remote_create_branch"],
    "remote_execute":     ["run_remote_command", "read_remote_file"],
    # Tier 3: 액션 인텐트
    "directive":          ["generate_directive"],
    "directive_gen":      ["generate_directive"],
    "cto_directive":      ["generate_directive"],
    # Tier 4: 외부 검색
    "search":             ["web_search"],
    "url_read":           ["jina_read"],
    # Tier 6: 브라우저 — 명시적 요청 시만
    "browser":            ["browser_navigate"],
}

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
        "allowed_callers": ["code_execution_20250825"],
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
        "description": (
            "GitHub 저장소 파일을 읽습니다. "
            "사용 가능한 리포: aads-docs(HANDOVER/CEO-DIRECTIVES), aads-server(백엔드), aads-dashboard(프론트). "
            "⚠️ SF/NTV2/GO100/KIS 프로젝트는 GitHub 리포 없음 → read_remote_file(SSH) 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "저장소명 또는 전체 경로 (예: 'aads-docs', 'aads-server', 'moongoby-GO100/aads-dashboard')",
                },
                "path": {
                    "type": "string",
                    "description": "파일 경로 (예: 'HANDOVER.md', 'app/services/chat_service.py')",
                },
                "branch": {
                    "type": "string",
                    "description": "브랜치 이름 (기본: 'main')",
                },
            },
            "required": ["repo", "path"],
        },
        "input_examples": [
            {"repo": "aads-docs", "path": "HANDOVER.md"},
            {"repo": "aads-server", "path": "app/services/chat_service.py", "branch": "main"},
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
        "allowed_callers": ["code_execution_20250825"],
    },
    "query_project_database": {
        "name": "query_project_database",
        "description": "프로젝트별 원격 DB에 SELECT 쿼리를 실행합니다. KIS(주식자동매매), GO100, SF, NTV2 프로젝트의 PostgreSQL DB에 직접 접근. SELECT/WITH/EXPLAIN만 허용, 민감 컬럼 자동 마스킹.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트명. KIS(주식자동매매), GO100, SF, NTV2 중 하나.",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
                "query": {
                    "type": "string",
                    "description": "실행할 SELECT SQL 쿼리 (최대 2000자). SELECT/WITH/EXPLAIN만 허용.",
                },
                "limit": {
                    "type": "integer",
                    "description": "반환할 최대 행 수 (기본 100, 최대 1000)",
                    "default": 100,
                },
                "db_name": {
                    "type": "string",
                    "description": "DB 이름 (미지정 시 프로젝트 메인 DB 사용). NTV2 V1 DB 조회 시: project=NTV2, db_name=autoda",
                },
            },
            "required": ["project", "query"],
        },
        "input_examples": [
            {"project": "KIS", "query": "SELECT count(*) FROM users"},
            {"project": "KIS", "query": "SELECT id, username, created_at FROM users ORDER BY created_at DESC LIMIT 10"},
            {"project": "KIS", "query": "SELECT symbol, side, qty, price, status FROM auto_trade_orders WHERE created_at > now() - interval '1 day' ORDER BY created_at DESC", "limit": 50},
            {"project": "NTV2", "query": "SHOW TABLES", "db_name": "autoda"},
        ],
    },
    "list_project_databases": {
        "name": "list_project_databases",
        "description": "설정된 프로젝트 DB 목록과 연결 상태를 조회합니다. 어떤 프로젝트 DB가 사용 가능한지 확인할 때 사용.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "input_examples": [{}],
    },
    # ── AADS-190: 내보내기 도구 ────────────────────────────────────────────
    "export_data": {
        "name": "export_data",
        "description": "데이터를 Excel/CSV/PDF 파일로 내보내 다운로드 링크를 제공합니다. 직접 데이터 전달 또는 project+query로 자동 조회 가능.",
        "input_schema": {
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "description": "내보낼 데이터 (dict 배열). project+query 지정 시 생략 가능.",
                    "items": {"type": "object"},
                },
                "project": {
                    "type": "string",
                    "description": "data 없을 때 자동 조회할 프로젝트 (KIS/GO100/SF/NTV2)",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
                "query": {
                    "type": "string",
                    "description": "data 없을 때 자동 조회할 SELECT 쿼리",
                },
                "format": {
                    "type": "string",
                    "description": "출력 포맷: csv, xlsx(기본), pdf",
                    "enum": ["csv", "xlsx", "pdf"],
                    "default": "xlsx",
                },
                "title": {"type": "string", "description": "파일 제목 (선택)"},
                "filename": {"type": "string", "description": "파일명 (선택, 자동 생성)"},
                "limit": {"type": "integer", "description": "쿼리 최대 행 수 (기본 1000)", "default": 1000},
            },
        },
        "input_examples": [
            {"project": "KIS", "query": "SELECT id, email, is_active FROM users", "format": "xlsx", "title": "KIS 사용자 목록"},
            {"project": "KIS", "query": "SELECT symbol, side, qty, price, status FROM orders ORDER BY created_at DESC", "format": "csv"},
            {"data": [{"name": "A", "value": 100}, {"name": "B", "value": 200}], "format": "xlsx"},
        ],
    },
    # ── AADS-190: 스케줄러 도구 ────────────────────────────────────────────
    "schedule_task": {
        "name": "schedule_task",
        "description": "예약 작업을 등록합니다. 매일/매주/주기적 서버 점검, DB 조회, URL 체크 등을 예약. 결과는 텔레그램으로 알림.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "작업 이름 (고유 ID)"},
                "schedule_type": {
                    "type": "string",
                    "description": "스케줄 유형: cron(반복), interval(주기), once(1회)",
                    "enum": ["cron", "interval", "once"],
                },
                "action_type": {
                    "type": "string",
                    "description": "실행 유형: remote_command, health_check, db_query, url_check",
                    "enum": ["remote_command", "health_check", "db_query", "url_check"],
                },
                "action_config": {
                    "type": "object",
                    "description": "실행 설정. remote_command: {project, command}, db_query: {project, query}, url_check: {url}",
                },
                "schedule_config": {
                    "type": "object",
                    "description": "스케줄 설정. cron: {hour, minute, day_of_week}, interval: {minutes 또는 hours}, once: {delay_minutes}",
                },
            },
            "required": ["name", "schedule_type", "action_type", "action_config"],
        },
        "input_examples": [
            {"name": "매일아침서버점검", "schedule_type": "cron", "action_type": "health_check", "action_config": {}, "schedule_config": {"hour": 9, "minute": 0}},
            {"name": "KIS디스크체크", "schedule_type": "interval", "action_type": "remote_command", "action_config": {"project": "KIS", "command": "df -h"}, "schedule_config": {"hours": 6}},
            {"name": "1회테스트", "schedule_type": "once", "action_type": "url_check", "action_config": {"url": "https://aads.newtalk.kr"}, "schedule_config": {"delay_minutes": 5}},
        ],
    },
    "unschedule_task": {
        "name": "unschedule_task",
        "description": "등록된 예약 작업을 삭제합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "삭제할 작업 이름"},
            },
            "required": ["name"],
        },
        "input_examples": [{"name": "매일아침서버점검"}],
    },
    "list_scheduled_tasks": {
        "name": "list_scheduled_tasks",
        "description": "등록된 예약 작업 목록과 다음 실행 시간을 조회합니다.",
        "input_schema": {"type": "object", "properties": {}},
        "input_examples": [{}],
    },
    "read_remote_file": {
        "name": "read_remote_file",
        "description": (
            "프로젝트 서버의 파일을 읽습니다 (SSH 자동 매핑, Claude Code Read tool과 동일). "
            "AADS=68서버(/root/aads/), KIS/GO100=211서버(/root/webapp/), "
            "SF=114서버(/data/shortflow/), NTV2=114서버(/var/www/newtalk/). "
            "기본 2000줄 읽기, offset/limit으로 대용량 파일 분할 읽기 가능. "
            "⚠️ AADS는 read_github_file도 가능하지만, SF/NTV2/KIS/GO100은 이 도구만 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트명 (서버 자동 매핑).",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
                "path": {
                    "type": "string",
                    "description": "WORKDIR 기준 상대 경로 (예: app/main.py, config.py)",
                },
                "offset": {
                    "type": "integer",
                    "description": "읽기 시작 줄 번호 (1부터). 생략 시 처음부터.",
                },
                "limit": {
                    "type": "integer",
                    "description": "읽을 최대 줄 수 (기본 2000, 제한 없음).",
                },
            },
            "required": ["project", "path"],
        },
        "input_examples": [
            {"project": "SF", "path": "/data/shortflow/app/main.py"},
            {"project": "KIS", "path": "/root/webapp/backend/app/core/config.py", "response_format": "concise"},
            {"project": "NTV2", "path": "/var/www/newtalk/app/Http/Controllers/AuthController.php", "response_format": "detailed"},
        ],
        "allowed_callers": ["code_execution_20250825"],
    },
    "list_remote_dir": {
        "name": "list_remote_dir",
        "description": "원격 서버의 디렉터리/파일을 검색합니다 (SSH). 프로젝트별 서버(KIS/GO100/SF/NTV2)에서 파일 목록 또는 키워드 검색.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트명. AADS, KIS, GO100, SF, NTV2 중 하나.",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
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
            {"project": "KIS", "path": "", "keyword": "config"},
            {"project": "SF", "path": "/data/shortflow", "max_depth": 2, "response_format": "concise"},
            {"project": "NTV2", "keyword": "Controller", "response_format": "detailed"},
        ],
        "allowed_callers": ["code_execution_20250825"],
    },
    # ── 원격 쓰기/실행/Git 도구 (AADS-190) ──────────────────────────────
    "write_remote_file": {
        "name": "write_remote_file",
        "description": (
            "원격 서버 파일 쓰기 (SSH). 기존 파일은 자동 백업(.bak_aads). "
            "민감 파일(.env, .ssh 등) 차단. 최대 1MB. "
            "KIS=211서버, SF/NTV2=114서버. AADS는 로컬."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트명",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
                "file_path": {
                    "type": "string",
                    "description": "WORKDIR 기준 상대 경로 (예: app/main.py)",
                },
                "content": {
                    "type": "string",
                    "description": "파일에 쓸 전체 내용",
                },
                "backup": {
                    "type": "boolean",
                    "description": "기존 파일 백업 여부 (기본 true)",
                    "default": True,
                },
            },
            "required": ["project", "file_path", "content"],
        },
        "input_examples": [
            {"project": "KIS", "file_path": "backend/app/test.py", "content": "print('hello')"},
        ],
    },
    "patch_remote_file": {
        "name": "patch_remote_file",
        "description": (
            "원격 서버 파일 부분 수정 (diff 기반). old_string을 찾아 new_string으로 교체. "
            "정확히 1회만 매치되어야 함 (중복 시 실패). 자동 백업 포함. "
            "전체 파일 재작성보다 안전 — 부분 수정 시 우선 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트명",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
                "file_path": {
                    "type": "string",
                    "description": "WORKDIR 기준 상대 경로",
                },
                "old_string": {
                    "type": "string",
                    "description": "교체할 기존 문자열 (정확히 1회 매치 필요)",
                },
                "new_string": {
                    "type": "string",
                    "description": "새 문자열",
                },
            },
            "required": ["project", "file_path", "old_string", "new_string"],
        },
        "input_examples": [
            {"project": "KIS", "file_path": "backend/app/main.py", "old_string": "DEBUG = True", "new_string": "DEBUG = False"},
        ],
    },
    "run_remote_command": {
        "name": "run_remote_command",
        "description": (
            "원격 서버에서 명령 실행 (SSH, 화이트리스트 제한). "
            "허용: systemctl, docker, pip, python, pytest, cat, df, free, ps, tail, head, "
            "grep, find, ls, uptime, crontab, nginx, supervisorctl, journalctl, ss, curl, git 등. "
            "차단: rm -rf, DROP, shutdown, reboot 등 위험 명령."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트명 (서버+workdir 자동 매핑)",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
                "command": {
                    "type": "string",
                    "description": "실행할 명령어 (화이트리스트 검사)",
                },
            },
            "required": ["project", "command"],
        },
        "input_examples": [
            {"project": "KIS", "command": "git status --short"},
            {"project": "SF", "command": "docker ps"},
            {"project": "KIS", "command": "supervisorctl status"},
        ],
    },
    "git_remote_add": {
        "name": "git_remote_add",
        "description": "원격 서버 git add (스테이징). 기본 '.'(전체).",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"]},
                "files": {"type": "string", "description": "스테이징할 파일 (기본: '.')", "default": "."},
            },
            "required": ["project"],
        },
        "input_examples": [{"project": "KIS", "files": "backend/app/main.py"}],
    },
    "git_remote_commit": {
        "name": "git_remote_commit",
        "description": "원격 서버 git commit. 메시지 필수.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"]},
                "message": {"type": "string", "description": "커밋 메시지"},
            },
            "required": ["project", "message"],
        },
        "input_examples": [{"project": "KIS", "message": "fix: 잔고 조회 오류 수정"}],
    },
    "git_remote_push": {
        "name": "git_remote_push",
        "description": "원격 서버 git push. force push 차단.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"]},
                "branch": {"type": "string", "description": "브랜치명 (빈 값이면 현재 브랜치)", "default": ""},
            },
            "required": ["project"],
        },
        "input_examples": [{"project": "KIS"}, {"project": "KIS", "branch": "main"}],
    },
    "git_remote_status": {
        "name": "git_remote_status",
        "description": "원격 서버 git status --short 조회.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"]},
            },
            "required": ["project"],
        },
        "input_examples": [{"project": "KIS"}],
    },
    "git_remote_create_branch": {
        "name": "git_remote_create_branch",
        "description": "원격 서버 새 브랜치 생성 및 체크아웃.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"]},
                "branch_name": {"type": "string", "description": "새 브랜치명 (영문/숫자/._-/ 허용)"},
            },
            "required": ["project", "branch_name"],
        },
        "input_examples": [{"project": "KIS", "branch_name": "feature/balance-fix"}],
    },
    # ── action 그룹 (기존) ─────────────────────────────────────────────────
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
        "allowed_callers": ["code_execution_20250825"],
    },
    # ── search 그룹 ──────────────────────────────────────────────────────────
    "web_search_brave": {
        "name": "web_search_brave",
        "description": (
            "통합 웹 검색 — Google(Gemini Grounding), Naver, Kakao(Daum) 3개 엔진 자동 폴백. "
            "engine='auto'(기본): Google→Naver→Kakao 순 시도. "
            "engine='all': 3개 병렬 실행 후 통합. "
            "engine='google'/'naver'/'kakao': 특정 엔진만 사용."
        ),
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
                "engine": {
                    "type": "string",
                    "description": "검색 엔진 선택 (기본 auto)",
                    "enum": ["auto", "all", "google", "naver", "kakao"],
                },
                "search_type": {
                    "type": "string",
                    "description": "Naver 전용: 검색 타입 (기본 webkr)",
                    "enum": ["webkr", "blog", "news", "kin", "encyc", "book", "image", "shop", "cafearticle"],
                },
            },
            "required": ["query"],
        },
        "input_examples": [
            {"query": "FastAPI MCP 통합 가이드"},
            {"query": "AI 에이전트 트렌드 2026", "engine": "all"},
            {"query": "삼성전자 주가", "engine": "naver", "search_type": "news"},
        ],
    },
    "web_search": {
        "name": "web_search",
        "description": "web_search_brave의 별칭. 통합 웹 검색 (Google/Naver/Kakao 자동 폴백).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 쿼리"},
                "engine": {"type": "string", "enum": ["auto", "all", "google", "naver", "kakao"]},
                "count": {"type": "integer"},
            },
            "required": ["query"],
        },
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
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
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
    # ── crawl 그룹 (AADS-186E-1) ──────────────────────────────────────────────
    "jina_read": {
        "name": "jina_read",
        "description": "URL의 전체 내용을 깨끗한 마크다운으로 변환하여 읽는다. 기술 문서, 블로그, 뉴스 등 모든 웹페이지 지원.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "읽을 URL (http:// 또는 https:// 포함)",
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "최대 토큰 수 (기본 25000)",
                    "default": 25000,
                },
            },
            "required": ["url"],
        },
        "input_examples": [
            {"url": "https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking"},
            {"url": "https://fastapi.tiangolo.com/tutorial/background-tasks/", "max_tokens": 10000},
        ],
        "defer_loading": True,
        "allowed_callers": ["code_execution_20250825"],
    },
    "crawl4ai_fetch": {
        "name": "crawl4ai_fetch",
        "description": "JavaScript 렌더링이 필요한 SPA 페이지를 크롤링한다. jina_read 실패 시 폴백으로 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "크롤링할 URL",
                },
                "js_render": {
                    "type": "boolean",
                    "description": "JS 렌더링 여부 (기본 true)",
                    "default": True,
                },
            },
            "required": ["url"],
        },
        "input_examples": [
            {"url": "https://example.com/spa-page"},
            {"url": "https://dashboard.example.com", "js_render": True},
        ],
        "defer_loading": True,
    },
    "deep_crawl": {
        "name": "deep_crawl",
        "description": "주제에 대해 검색 후 상위 페이지를 자동 크롤링하고 내용을 종합 분석한다. 시장 조사, 기술 비교, 트렌드 파악에 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "검색 및 분석할 주제",
                },
                "max_pages": {
                    "type": "integer",
                    "description": "크롤링할 최대 페이지 수 (기본 5)",
                    "default": 5,
                },
                "summarize": {
                    "type": "boolean",
                    "description": "종합 요약 수행 여부 (기본 true)",
                    "default": True,
                },
            },
            "required": ["query"],
        },
        "input_examples": [
            {"query": "AI 코딩 에이전트 2026 비교", "max_pages": 5},
            {"query": "FastAPI MCP 통합 방법", "max_pages": 3, "summarize": False},
        ],
        "defer_loading": True,
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
    # ── AADS-186E-2: PTC 도구 ─────────────────────────────────────────────────
    "code_execution": {
        "type": "code_execution_20250825",
        "name": "code_execution",
        # description/input_schema 등 추가 필드 금지 — Anthropic 내장 도구 타입은 type+name만 허용
        "allowed_callers": ["code_execution_20250825"],
    },
    # ── AADS-186E-2/186E-3: 메모리 도구 ────────────────────────────────────────
    "save_note": {
        "name": "save_note",
        "description": (
            "중요한 정보, 결정, 분석 결과를 영구 저장. "
            "다음 세션에서 recall_notes로 검색 가능. "
            "중요한 결정이나 이슈가 나오면 반드시 호출한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "노트 제목 (50자 이내)",
                },
                "content": {
                    "type": "string",
                    "description": "노트 내용 (500자 이내)",
                },
                "category": {
                    "type": "string",
                    "description": "카테고리 (선택). 예: 'decision', 'analysis', 'general'",
                },
            },
            "required": ["title", "content"],
        },
        "input_examples": [
            {"title": "마이크로서비스 전환 결정", "content": "6개 서비스 중 KIS와 NTV2를 우선 분리", "category": "decision"},
            {"title": "서버211 SSH 불안정", "content": "ConnectTimeout=30 설정 필요, 이유: 망 레이턴시", "category": "known_issue"},
        ],
        "defer_loading": True,
    },
    "recall_notes": {
        "name": "recall_notes",
        "description": (
            "이전에 저장한 노트를 키워드로 검색. "
            "지난 세션의 결정, 분석, 메모를 찾을 때 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "검색 쿼리 (키워드)",
                },
                "limit": {
                    "type": "integer",
                    "description": "반환할 최대 건수 (기본 5, 최대 20)",
                },
            },
            "required": ["query"],
        },
        "input_examples": [
            {"query": "마이크로서비스"},
            {"query": "KIS 주문", "limit": 3},
        ],
        "defer_loading": True,
    },
    "delete_note": {
        "name": "delete_note",
        "description": (
            "저장된 노트를 삭제. note_id(정확한 ID) 또는 keyword(키워드 매칭)로 삭제. "
            "recall_notes로 먼저 검색하여 ID를 확인한 후 삭제하는 것을 권장."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note_id": {
                    "type": "integer",
                    "description": "삭제할 노트 ID (recall_notes 결과에서 확인)",
                },
                "keyword": {
                    "type": "string",
                    "description": "키워드로 매칭되는 노트 삭제 (summary/content 검색)",
                },
            },
        },
        "input_examples": [
            {"note_id": 42},
            {"keyword": "NTV2 V2 구조"},
        ],
        "defer_loading": True,
    },
    "learn_pattern": {
        "name": "learn_pattern",
        "description": (
            "CEO 선호도, 프로젝트 특이사항, 반복 패턴을 기억한다. "
            "예: CEO가 항상 한국어로 답하길 원한다, 서버211이 불안정하다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "카테고리. 'ceo_preference' | 'project_pattern' | 'known_issue' | 'decision_history'",
                    "enum": ["ceo_preference", "project_pattern", "known_issue", "decision_history"],
                },
                "key": {
                    "type": "string",
                    "description": "패턴 키 (영문 snake_case, 예: response_language)",
                },
                "value": {
                    "type": "object",
                    "description": "저장할 값 (임의 JSON 객체)",
                },
            },
            "required": ["category", "key", "value"],
        },
        "defer_loading": True,
    },
    # ── AADS-186E-3: 자동 관찰 도구 ────────────────────────────────────────────
    "observe": {
        "name": "observe",
        "description": (
            "CEO 선호도, 반복 패턴, 결정 사항을 자동 관찰 기록. "
            "대화에서 발견한 패턴이나 새로운 정보를 장기 메모리에 저장할 때 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "카테고리",
                    "enum": ["ceo_preference", "project_pattern", "recurring_issue", "decision", "learning"],
                },
                "key": {
                    "type": "string",
                    "description": "관찰 키 (영문 snake_case)",
                },
                "value": {
                    "type": "string",
                    "description": "관찰 내용 (한국어)",
                },
                "confidence": {
                    "type": "number",
                    "description": "확신도 0.0~1.0 (기본 0.5)",
                },
            },
            "required": ["category", "key", "value"],
        },
        "input_examples": [
            {"category": "ceo_preference", "key": "response_language", "value": "한국어 응답 선호", "confidence": 0.9},
            {"category": "recurring_issue", "key": "server_211_ssh", "value": "SSH 자주 타임아웃", "confidence": 0.7},
        ],
        "defer_loading": True,
    },
    # ── AADS-186E-3: 딥리서치 + 코드탐색 도구 ──────────────────────────────────
    "deep_research": {
        "name": "deep_research",
        "description": (
            "주제에 대해 수십 개 웹 소스를 자동 탐색하여 상세 보고서를 생성한다. "
            "시장 분석, 기술 동향, 경쟁 비교에 사용. 3~10분 소요. 비용 $2~5/건. "
            "일일 최대 5건. '딥리서치', '조사해서 보고서 써줘', '깊이 분석해'에 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "리서치 주제/질문",
                },
                "context": {
                    "type": "string",
                    "description": "추가 배경 컨텍스트 (선택). 예: '우리 회사는 B2B SaaS 스타트업'",
                },
                "format": {
                    "type": "string",
                    "description": "보고서 형식 프리셋. summary=간결요약, detailed=상세분석, report=공식보고서",
                    "enum": ["summary", "detailed", "report"],
                },
                "format_instructions": {
                    "type": "string",
                    "description": "보고서 형식 자유 지시 (선택). 예: '1. 요약 2. 주요 플레이어 3. 비용 비교'",
                },
            },
            "required": ["query"],
        },
        "input_examples": [
            {"query": "AI 코딩 에이전트 시장 동향 2026", "format": "report"},
            {"query": "FastAPI vs Django 성능 비교 최신", "format": "detailed"},
            {"query": "경쟁사 분석", "context": "우리 회사는 B2B SaaS HR 플랫폼", "format": "summary"},
        ],
        "defer_loading": True,
    },
    "code_explorer": {
        "name": "code_explorer",
        "description": (
            "프로젝트 소스코드의 함수 호출 체인을 추적한다. "
            "'이 함수가 어디서 호출되는지', '이 로직의 전체 흐름' 분석. depth 3까지 재귀 탐색."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트명. AADS, KIS, GO100, SF, NTV2, NAS 중 하나.",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2", "NAS"],
                },
                "entry_point": {
                    "type": "string",
                    "description": "진입점. 'file.py::function_name' 형식 (예: app/services/order_service.py::create_order)",
                },
                "depth": {
                    "type": "integer",
                    "description": "추적 깊이 (기본 3, 최대 3)",
                    "default": 3,
                },
            },
            "required": ["project", "entry_point"],
        },
        "input_examples": [
            {"project": "KIS", "entry_point": "app/order_handler.py::process_order", "depth": 3},
            {"project": "AADS", "entry_point": "app/services/chat_service.py::send_message"},
        ],
        "defer_loading": True,
    },
    "analyze_changes": {
        "name": "analyze_changes",
        "description": (
            "프로젝트의 최근 Git 변경사항을 분석하고 위험도를 평가한다. "
            "커밋 카테고리(기능추가/버그수정/리팩터), 핵심 파일 변경 감지, 영향 범위 포함."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트명. AADS, KIS, GO100, SF, NTV2, NAS 중 하나.",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2", "NAS"],
                },
                "days": {
                    "type": "integer",
                    "description": "분석 기간 (일 단위, 기본 7)",
                    "default": 7,
                },
            },
            "required": ["project"],
        },
        "input_examples": [
            {"project": "KIS", "days": 7},
            {"project": "AADS", "days": 14},
            {"project": "SF"},
        ],
        "defer_loading": True,
    },
    "search_all_projects": {
        "name": "search_all_projects",
        "description": (
            "6개 프로젝트(AADS/KIS/GO100/SF/NTV2/NAS)의 코드베이스를 동시 검색한다. "
            "중복 코드, 공유 패턴, 특정 함수 위치 파악에 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "검색어 (파일명, 함수명, 클래스명, 키워드)",
                },
            },
            "required": ["query"],
        },
        "input_examples": [
            {"query": "health_check"},
            {"query": "authenticate"},
            {"query": "config.py"},
        ],
        "defer_loading": True,
    },
    # ── AADS-159: 브라우저 도구 (Playwright 기반) ──────────────────────────
    "browser_navigate": {
        "name": "browser_navigate",
        "description": (
            "Playwright 헤드리스 브라우저로 URL에 접속한다. "
            "AADS 대시보드(aads.newtalk.kr), GitHub 등 허용 도메인만 접근 가능. "
            "'여기 확인해', '이 페이지 봐줘', '화면 열어봐'에 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "접속할 URL (https://aads.newtalk.kr/chat 등)",
                },
            },
            "required": ["url"],
        },
        "input_examples": [
            {"url": "https://aads.newtalk.kr/chat"},
            {"url": "https://aads.newtalk.kr/ops"},
        ],
    },
    "browser_snapshot": {
        "name": "browser_snapshot",
        "description": (
            "현재 열린 페이지의 접근성 트리를 텍스트로 추출한다. "
            "화면에 보이는 모든 UI 요소(버튼, 텍스트, 입력칸 등)를 파악할 수 있다. "
            "스크린샷 대신 텍스트 기반 분석이므로 LLM이 직접 UI를 이해할 수 있다. "
            "'화면 분석해', '뭐가 보여?', 'UI 구조 알려줘'에 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "browser_screenshot": {
        "name": "browser_screenshot",
        "description": (
            "현재 열린 페이지의 PNG 스크린샷을 촬영한다. base64 인코딩으로 반환. "
            "'스크린샷 찍어', '화면 캡처', '어떻게 보이는지 확인'에 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "capture_screenshot": {
        "name": "capture_screenshot",
        "description": (
            "웹 페이지 스크린샷을 캡처하여 채팅에 이미지로 표시한다. "
            "URL을 입력하면 해당 페이지를 캡처하여 이미지 링크를 반환한다. "
            "CEO에게 화면을 보여줘야 할 때 사용. 허용 도메인: *.newtalk.kr, localhost. "
            "'스크린샷 찍어줘', '화면 보여줘', '페이지 캡처해서 보여줘'에 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "캡처할 웹 페이지 URL (예: https://aads.newtalk.kr/)",
                },
                "full_page": {
                    "type": "boolean",
                    "description": "전체 페이지 캡처 여부 (기본: false, 뷰포트만)",
                },
            },
            "required": ["url"],
        },
    },
    "browser_click": {
        "name": "browser_click",
        "description": "현재 페이지에서 CSS selector로 요소를 클릭한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "클릭할 요소의 CSS selector 또는 'text=버튼텍스트'",
                },
            },
            "required": ["selector"],
        },
        "input_examples": [
            {"selector": "button:has-text('새 대화')"},
            {"selector": "#submit-btn"},
        ],
        "defer_loading": True,
    },
    "browser_fill": {
        "name": "browser_fill",
        "description": "현재 페이지의 입력 필드에 텍스트를 채운다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "입력 필드의 CSS selector",
                },
                "value": {
                    "type": "string",
                    "description": "입력할 텍스트",
                },
            },
            "required": ["selector", "value"],
        },
        "defer_loading": True,
    },
    "browser_tab_list": {
        "name": "browser_tab_list",
        "description": "헤드리스 브라우저에 열린 탭 목록을 조회한다.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "defer_loading": True,
    },
    # ── AADS-188C Phase 2: 메타 도구 (Orchestrator) ────────────────────────
    "check_task_status": {
        "name": "check_task_status",
        "description": (
            "현재 활성 중이거나 최근 완료된 Pipeline B/C 작업 목록 조회. "
            "'지금 작업 어떻게 돼?', '에이전트 뭐 하고 있어?', '작업 상태', '진행 상황' 등에 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "read_task_logs": {
        "name": "read_task_logs",
        "description": (
            "특정 작업(task_id)의 실시간 로그 조회. 도구 실행, 출력, 에러 등 상세 기록. "
            "'그 작업 로그 보여줘', '에이전트 로그', '무슨 작업하고 있는지 자세히'에 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "조회할 작업 ID (예: agent-abc12345, pc-1234567890-abcdef)"},
                "last_n": {"type": "integer", "description": "최근 N줄 (기본 30, 최대 100)"},
                "log_type": {"type": "string", "description": "로그 타입 필터: info, command, output, error, phase_change (생략 시 전체)"},
            },
            "required": ["task_id"],
        },
    },
    "terminate_task": {
        "name": "terminate_task",
        "description": (
            "스톨되거나 문제 있는 작업(에이전트/클로드봇)을 강제 종료. "
            "Pipeline C는 원격 프로세스 kill + DB 상태 변경, Pipeline B는 DB 상태 변경. "
            "'그 작업 중단해', '에이전트 종료시켜', '멈춰있는거 죽여', '다시 시작하려면 먼저 종료'에 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "종료할 작업 ID (예: agent-abc12345, pc-1234567890-abcdef)"},
                "reason": {"type": "string", "description": "종료 사유 (선택, 기본='AI 판단에 의한 강제 종료')"},
            },
            "required": ["task_id"],
        },
    },
    "check_directive_status": {
        "name": "check_directive_status",
        "description": (
            "지시사항 진행 상태 종합 확인. task_history와 get_all_service_status를 "
            "동시 호출하여 작업 이력 + 서비스 상태를 통합 보고한다. "
            "'다른 친구한테 시킨거 됐나?', '진행 확인해줘', '작업 현황 알려줘'에 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트 필터 (선택). AADS, KIS, GO100, SF, NTV2, NAS",
                },
                "limit": {
                    "type": "integer",
                    "description": "작업 이력 최대 건수 (기본 10)",
                },
            },
            "required": [],
        },
        "input_examples": [
            {},
            {"project": "KIS", "limit": 5},
        ],
    },
    "delegate_to_agent": {
        "name": "delegate_to_agent",
        "description": (
            "복잡한 다단계 작업을 Agent SDK 자율 실행 에이전트에게 위임한다. "
            "코드 분석/수정, 5턴 이상 필요한 복잡 작업에 사용. "
            "'이거 직접 수정해줘', '코드 고쳐서 배포해', '걔한테 시켜'에 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "위임할 작업 설명",
                },
                "project": {
                    "type": "string",
                    "description": "대상 프로젝트 (기본 'AADS')",
                },
                "model": {
                    "type": "string",
                    "description": "사용할 모델. 작업 복잡도에 따라 선택: 단순작업→claude-sonnet, 복잡분석/아키텍처→claude-opus (기본: claude-sonnet)",
                    "enum": ["claude-sonnet", "claude-opus", "claude-haiku"],
                },
            },
            "required": ["task"],
        },
        "input_examples": [
            {"task": "chat_service.py의 SSE 하트비트 로직 개선", "project": "AADS", "model": "claude-sonnet"},
            {"task": "전체 아키텍처 리팩토링 설계", "project": "KIS", "model": "claude-opus"},
        ],
    },
    "delegate_to_research": {
        "name": "delegate_to_research",
        "description": (
            "심층 리서치를 Deep Research 에이전트에게 위임한다. "
            "시장 분석, 기술 트렌드, 경쟁 분석에 사용. "
            "'시장 조사해서 보고서 써줘', '경쟁사 분석 해줘'에 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "리서치 주제/질문",
                },
                "context": {
                    "type": "string",
                    "description": "추가 배경 컨텍스트 (선택)",
                },
                "format": {
                    "type": "string",
                    "description": "보고서 형식. summary/detailed/report",
                    "enum": ["summary", "detailed", "report"],
                },
            },
            "required": ["query"],
        },
        "input_examples": [
            {"query": "AI 코딩 에이전트 시장 동향 2026", "format": "report"},
        ],
    },
    # AADS-190 Phase2-A: 서브에이전트
    "spawn_subagent": {
        "name": "spawn_subagent",
        "description": (
            "독립적 서브에이전트를 실행하여 복잡한 작업을 분할 위임한다. "
            "서브에이전트는 자체 LLM 호출로 작업을 수행하며 읽기 도구를 사용할 수 있다. "
            "'이 부분 분석해줘', '동시에 여러 파일 조사해', '코드 리뷰 해줘'에 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "서브에이전트에게 위임할 작업 설명",
                },
                "model": {
                    "type": "string",
                    "description": "사용할 모델 (sonnet/opus/haiku, 기본 sonnet)",
                    "enum": ["sonnet", "opus", "haiku"],
                },
                "context": {
                    "type": "string",
                    "description": "추가 컨텍스트 (파일 내용, DB 결과 등)",
                },
                "enable_tools": {
                    "type": "boolean",
                    "description": "도구 사용 허용 여부 (기본 true)",
                },
            },
            "required": ["task"],
        },
        "input_examples": [
            {"task": "KIS 프로젝트의 order_executor.py 분석 후 개선점 보고", "model": "sonnet"},
            {"task": "DB 스키마 분석하고 인덱스 최적화 방안 제시", "enable_tools": True},
        ],
    },
    "spawn_parallel_subagents": {
        "name": "spawn_parallel_subagents",
        "description": (
            "여러 서브에이전트를 병렬로 동시 실행하여 결과를 취합한다. "
            "각 서브에이전트는 독립적으로 LLM 호출을 수행한다. "
            "'4개 프로젝트 동시에 헬스체크해', '여러 파일 동시에 분석해'에 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {"type": "string"},
                            "model": {"type": "string"},
                            "context": {"type": "string"},
                            "enable_tools": {"type": "boolean"},
                        },
                        "required": ["task"],
                    },
                    "description": "실행할 서브에이전트 작업 리스트",
                },
                "max_concurrent": {
                    "type": "integer",
                    "description": "최대 동시 실행 수 (기본 5)",
                },
            },
            "required": ["tasks"],
        },
        "input_examples": [
            {
                "tasks": [
                    {"task": "KIS 프로젝트 헬스체크 및 현황 보고"},
                    {"task": "GO100 프로젝트 최근 에러 로그 분석"},
                    {"task": "AADS 서버 DB 커넥션 상태 확인"},
                ],
            },
        ],
    },
    # AADS-188B: 시맨틱 코드 검색
    "semantic_code_search": {
        "name": "semantic_code_search",
        "description": (
            "ChromaDB 벡터 인덱스로 코드베이스를 시맨틱 검색한다. "
            "'인증 로직 어디 있어?', '헬스체크 함수 찾아줘' 같은 자연어 질의에 "
            "관련 코드 청크(파일, 라인, 스니펫, 유사도 점수)를 반환한다. "
            "index_project를 먼저 실행해야 결과가 나온다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "자연어 검색 질의 (예: '헬스체크 로직', '인텐트 분류 함수')",
                },
                "project": {
                    "type": "string",
                    "description": "프로젝트 필터 (AADS/KIS/GO100/SF/NTV2/NAS). 생략 시 전체 검색.",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2", "NAS"],
                },
                "top_k": {
                    "type": "integer",
                    "description": "반환할 결과 수 (기본 5, 최대 20)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        "input_examples": [
            {"query": "헬스체크 로직", "project": "AADS", "top_k": 5},
            {"query": "인텐트 분류", "project": "AADS"},
            {"query": "인증 미들웨어"},
        ],
        "defer_loading": True,
    },
    # ── Pipeline C: 자율 작업 파이프라인 도구 ──────────────────────────────────
    "pipeline_c_start": {
        "name": "pipeline_c_start",
        "description": "프로젝트별 Claude Code 자율 작업 파이프라인 시작. 작업→자동검수→재지시→승인대기까지 자율 수행. 현재 세션과 무관하게 어떤 프로젝트든 지정 가능 (크로스 프로젝트).",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "대상 프로젝트 (KIS, GO100, SF, NTV2, AADS)",
                    "enum": ["KIS", "GO100", "SF", "NTV2", "AADS"],
                },
                "instruction": {
                    "type": "string",
                    "description": "Claude Code에 보낼 작업 지시 내용 (구체적으로 작성)",
                },
                "max_cycles": {
                    "type": "integer",
                    "description": "최대 검수-재지시 반복 횟수 (기본: 3)",
                    "default": 3,
                },
            },
            "required": ["project", "instruction"],
        },
        "input_examples": [
            {"project": "KIS", "instruction": "order_executor.py에서 NoneType 에러 방어 코드 추가"},
            {"project": "AADS", "instruction": "헬스체크 API에 디스크 사용량 지표 추가", "max_cycles": 2},
        ],
    },
    "pipeline_c_status": {
        "name": "pipeline_c_status",
        "description": "실행 중인 파이프라인C 작업 상태 확인. job_id 없으면 전체 목록 반환.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "파이프라인 작업 ID (예: pc-1741654800-abc123). 생략 시 전체 목록.",
                },
            },
            "required": [],
        },
        "input_examples": [
            {"job_id": "pc-1741654800-abc123"},
            {},
        ],
    },
    "pipeline_c_approve": {
        "name": "pipeline_c_approve",
        "description": "파이프라인C 작업 승인(배포) 또는 거부(원복). 승인 시 git commit+push+서비스 재시작 자동 수행.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "승인할 파이프라인 작업 ID",
                },
                "approved": {
                    "type": "boolean",
                    "description": "true=승인(배포), false=거부(원복)",
                },
                "reason": {
                    "type": "string",
                    "description": "거부 사유 (거부 시에만)",
                },
            },
            "required": ["job_id", "approved"],
        },
        "input_examples": [
            {"job_id": "pc-1741654800-abc123", "approved": True},
            {"job_id": "pc-1741654800-abc123", "approved": False, "reason": "테스트 실패"},
        ],
    },
    # ─── Memory Upgrade: F12 Timeline + F5 Tool Recall ───────────────────────
    "query_timeline": {
        "name": "query_timeline",
        "description": "프로젝트별 시간순 이력 조회. memory_facts에서 타임라인 형태로 프로젝트 이벤트, 결정, 변경 이력을 보여줌.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트명 (KIS, AADS, GO100, SF, NTV2 등)",
                },
                "period": {
                    "type": "string",
                    "description": "기간 (예: '7d', '30d', '2026-03-01~2026-03-13'). 기본=7d",
                },
                "category": {
                    "type": "string",
                    "description": "카테고리 필터 (decision, file_change, error_resolution 등, 선택)",
                },
                "limit": {
                    "type": "integer",
                    "description": "최대 결과 수 (기본 20, 최대 50)",
                },
            },
            "required": ["project"],
        },
    },
    "recall_tool_result": {
        "name": "recall_tool_result",
        "description": "과거 도구 실행 결과를 검색. 재실행 없이 이전 도구 결과를 즉시 참조.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "도구 이름 (query_db, read_file 등, 선택)",
                },
                "keyword": {
                    "type": "string",
                    "description": "결과 내 검색 키워드 (선택)",
                },
                "limit": {
                    "type": "integer",
                    "description": "최대 결과 수 (기본 5)",
                },
            },
            "required": [],
        },
    },
    # ─── 첨부파일 재읽기 도구 ─────────────────────────────────────────────────
    "read_uploaded_file": {
        "name": "read_uploaded_file",
        "description": "이전에 업로드된 첨부파일을 다시 읽습니다. 파일명(일부 가능) 또는 워크스페이스 내 전체 파일 목록 조회.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "파일명 또는 검색어 (부분 일치). 비워두면 최근 파일 목록 반환.",
                    "default": "",
                },
                "workspace_id": {
                    "type": "string",
                    "description": "워크스페이스 ID (선택, 현재 세션 워크스페이스 자동 사용).",
                    "default": "",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "최대 읽기 문자 수 (기본 100000).",
                    "default": 100000,
                },
            },
            "required": [],
        },
        "input_examples": [
            {"filename": "DESK-MANAGER"},
            {"filename": "보고서"},
            {"filename": ""},
        ],
    },
}


# ─── 그룹 → 도구 매핑 ─────────────────────────────────────────────────────────

_GROUPS: Dict[str, List[str]] = {
    "system": ["health_check", "dashboard_query", "task_history", "server_status"],
    "action": ["directive_create", "read_github_file", "query_database", "query_project_database", "read_remote_file", "list_remote_dir", "cost_report", "export_data", "schedule_task", "read_uploaded_file"],
    "search": ["web_search"],
    "workflow": ["inspect_service", "get_all_service_status", "generate_directive"],
    # AADS-159: 브라우저 도구 그룹 (소스 분석 도구도 함께 제공 — Tier 6 원칙)
    "browser": ["read_remote_file", "list_remote_dir", "browser_navigate", "browser_snapshot", "browser_screenshot", "capture_screenshot", "browser_click", "browser_fill", "browser_tab_list"],
    # AADS-188C Phase 2: 메타 도구 그룹 (Orchestrator)
    "meta": ["check_directive_status", "check_task_status", "read_task_logs", "terminate_task", "delegate_to_agent", "delegate_to_research", "spawn_subagent", "spawn_parallel_subagents"],
    # AADS-186E-1: 크롤링 도구 그룹
    "crawl": ["jina_read", "crawl4ai_fetch", "deep_crawl"],
    # AADS-186E-2: 메모리 도구 그룹 (+ Memory Upgrade F5/F12)
    "memory": ["save_note", "recall_notes", "delete_note", "learn_pattern", "observe", "query_timeline", "recall_tool_result"],
    # AADS-186E-3 / AADS-188B: 딥리서치 + 코드탐색 + 시맨틱 검색 도구 그룹
    "research": ["deep_research", "code_explorer", "analyze_changes", "search_all_projects", "semantic_code_search"],
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
            # input_examples, defer_loading, allowed_callers는 API 전송 시 제외
            _EXCLUDE_KEYS = {"input_examples", "defer_loading", "allowed_callers"}
            tool = {k: v for k, v in _TOOLS[name].items() if k not in _EXCLUDE_KEYS}
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

    # ─── AADS-186D: Tool Search Tool 지원 ──────────────────────────────────

    def get_eager_tools(self) -> List[Dict[str, Any]]:
        """상시 로드 도구 반환 (defer_loading=false). Anthropic API 매 요청 포함."""
        _EXCLUDE = {"input_examples", "defer_loading", "allowed_callers"}
        return [
            {k: v for k, v in _TOOLS[name].items() if k not in _EXCLUDE}
            for name in _TOOLS
            if not _DEFER_LOADING.get(name, True)
        ]

    def get_deferred_tools(self) -> List[Dict[str, Any]]:
        """온디맨드 도구 반환 (defer_loading=true). Tool Search Tool 검색 결과용."""
        _EXCLUDE = {"input_examples", "defer_loading", "allowed_callers"}
        return [
            {k: v for k, v in _TOOLS[name].items() if k not in _EXCLUDE}
            for name in _TOOLS
            if _DEFER_LOADING.get(name, True)
        ]

    def get_tool_category_guide(self) -> str:
        """시스템 프롬프트 주입용 도구 카테고리 안내 텍스트 반환."""
        return TOOL_CATEGORY_GUIDE

    def is_deferred(self, name: str) -> bool:
        """도구가 온디맨드(defer_loading=true) 여부 반환."""
        return _DEFER_LOADING.get(name, True)
