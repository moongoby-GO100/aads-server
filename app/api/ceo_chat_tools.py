"""
CEO Chat 도구 정의 및 실행 (AADS-157 + AADS-159)

5개 기존 도구: read_file, read_github, search_logs, query_db, fetch_url
6개 browser 도구: browser_navigate, browser_snapshot, browser_screenshot,
                  browser_click, browser_fill, browser_tab_list

보안 규칙 (하드코딩, LLM 우회 불가):
  - read_file: /root/aads/ 하위만 허용. /etc, /proc, /root/.ssh 차단
  - query_db: SELECT만 허용. INSERT/UPDATE/DELETE/DROP/ALTER 차단
  - search_logs: 최근 100줄, 최대 10KB
  - fetch_url: 최대 20KB
  - browser: 허용 도메인만 접근 (*.newtalk.kr, github.com, localhost)
"""
import asyncio
import asyncpg
import base64
import httpx
import ipaddress
import logging
import re
import shlex
import socket
import subprocess
import uuid
from datetime import datetime

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ─── 도구 정의 (Anthropic tool_use 포맷) ──────────────────────────────────────
TOOL_DEFINITIONS: List[Dict] = [
    {
        "name": "read_file",
        "description": "서버 68 로컬 파일 읽기. /root/aads/ 하위만 허용. 최대 50KB.\n예: read_file(path='/root/aads/aads-server/app/main.py')",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "읽을 파일의 절대 경로 (예: /root/aads/aads-docs/HANDOVER.md)",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_github",
        "description": "moongoby-GO100 GitHub 레포의 파일을 raw URL로 읽기. 최대 50KB.\n예: read_github(path='HANDOVER.md', repo='aads-docs')",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "레포 내 파일 경로 (예: aads-docs/HANDOVER.md 또는 HANDOVER.md)",
                },
                "repo": {
                    "type": "string",
                    "description": "레포 이름 (기본값: aads-docs)",
                    "default": "aads-docs",
                },
                "branch": {
                    "type": "string",
                    "description": "브랜치 이름 (기본값: main)",
                    "default": "main",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_logs",
        "description": "Docker 컨테이너 로그 또는 journalctl에서 최근 100줄 검색. 최대 10KB.\n허용 소스: aads-server, aads-dashboard, aads-postgres, aads-redis, aads-litellm, aads-core, journalctl.\n예: search_logs(source='aads-server', keyword='ERROR')",
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "로그 소스: Docker 컨테이너 이름(예: aads-server) 또는 'journalctl'",
                },
                "keyword": {
                    "type": "string",
                    "description": "검색할 키워드 (선택, 없으면 전체 최근 100줄 반환)",
                },
            },
            "required": ["source"],
        },
    },
    {
        "name": "query_db",
        "description": "AADS 내부 PostgreSQL SELECT 쿼리 실행. SELECT 전용, 최대 50행.\n다른 프로젝트 DB는 query_project_database 사용.\n예: query_db(sql='SELECT * FROM chat_sessions ORDER BY updated_at DESC LIMIT 5')",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "실행할 SELECT SQL 쿼리 (예: SELECT * FROM task_tracking LIMIT 10)",
                }
            },
            "required": ["sql"],
        },
    },
    {
        "name": "fetch_url",
        "description": "외부 URL GET 요청. 응답 최대 20KB. HTML/JSON 모두 지원.\n예: fetch_url(url='https://aads.newtalk.kr/api/v1/health')",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "GET 요청할 URL (예: https://aads.newtalk.kr/api/v1/health)",
                }
            },
            "required": ["url"],
        },
    },
    # ── Browser 도구 (AADS-159) ────────────────────────────────────────────
    {
        "name": "browser_navigate",
        "description": "Playwright 브라우저로 URL 이동. 허용 도메인: *.newtalk.kr, github.com, localhost.\n이동 후 browser_snapshot으로 페이지 확인.\n예: browser_navigate(url='https://aads.newtalk.kr/')",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "이동할 URL (예: https://aads.newtalk.kr/)",
                }
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_snapshot",
        "description": "현재 브라우저 페이지의 접근성 트리를 텍스트로 추출. 스크린샷보다 정확한 구조 분석. browser_navigate 후 사용.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "browser_screenshot",
        "description": "현재 브라우저 페이지 PNG 스크린샷. base64 반환. 시각적 레이아웃 확인용. browser_navigate 후 사용.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "browser_click",
        "description": "브라우저 페이지에서 CSS selector 또는 텍스트로 요소 클릭.\n예: browser_click(selector='button#submit') 또는 browser_click(selector='text=로그인')",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "클릭할 요소의 CSS selector (예: button#submit, text=로그인)",
                }
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_fill",
        "description": "브라우저 입력 필드에 텍스트 입력.\n예: browser_fill(selector='input[name=username]', value='admin')",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "입력 필드의 CSS selector (예: input[name=username])",
                },
                "value": {
                    "type": "string",
                    "description": "입력할 텍스트",
                },
            },
            "required": ["selector", "value"],
        },
    },
    {
        "name": "browser_tab_list",
        "description": "현재 열린 브라우저 탭 목록 (URL + 제목). 최대 3탭.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    # ── SSH 원격 파일 접근 도구 (AADS-165) ────────────────────────────────────
    {
        "name": "list_remote_dir",
        "description": "원격 서버의 디렉터리 구조 탐색. 프로젝트명으로 서버·WORKDIR 자동 매핑.\n예: list_remote_dir(project='KIS', path='backend/app', keyword='executor')",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트명 (AADS, KIS, GO100, SF, NTV2)",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
                "path": {
                    "type": "string",
                    "description": "WORKDIR 기준 상대경로 (선택, 기본: 루트)",
                    "default": "",
                },
                "keyword": {
                    "type": "string",
                    "description": "파일명 검색어 (선택)",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "탐색 깊이 (기본: 3, 최대: 5)",
                    "default": 3,
                },
            },
            "required": ["project"],
        },
    },
    {
        "name": "read_remote_file",
        "description": "원격 서버의 파일 내용 읽기 (코드 분석 1순위 도구). 프로젝트명으로 서버·WORKDIR 자동 매핑. offset/limit으로 부분 읽기 지원.\n예: read_remote_file(project='KIS', file_path='backend/app/main.py')",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트명 (AADS, KIS, GO100, SF, NTV2)",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
                "file_path": {
                    "type": "string",
                    "description": "WORKDIR 기준 상대 파일 경로 (예: src/main.py)",
                },
                "offset": {
                    "type": "integer",
                    "description": "읽기 시작 줄 번호 (1부터 시작). 생략 시 처음부터.",
                    "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "읽을 최대 줄 수. 생략 시 2000줄. 제한 없음.",
                    "default": 2000,
                },
            },
            "required": ["project", "file_path"],
        },
    },
    # ── Pipeline C 도구 (자율 작업 파이프라인) ──────────────────────────────
    {
        "name": "pipeline_c_start",
        "description": "프로젝트별 Claude Code 자율 작업 파이프라인 시작.\n워크플로우: 작업→AI검수→재지시(max_cycles)→CEO 승인대기→배포.\n예: pipeline_c_start(project='KIS', instruction='order_executor.py의 null check 추가', model='sonnet')",
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
                "model": {
                    "type": "string",
                    "description": "Claude Code가 사용할 모델. 단순작업→sonnet, 복잡작업→opus (기본: sonnet)",
                    "enum": ["sonnet", "opus", "haiku"],
                },
            },
            "required": ["project", "instruction"],
        },
    },
    {
        "name": "pipeline_c_status",
        "description": "파이프라인C 작업 상태 확인 (phase, cycle 수, 리뷰 피드백). job_id 없으면 전체 목록.\n예: pipeline_c_status(job_id='pc-1741654800-abc123')",
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
    },
    {
        "name": "pipeline_c_approve",
        "description": "파이프라인C 작업 CEO 승인/거부. 승인 시 git commit+push+서비스 재시작 자동 수행. 거부 시 git stash로 원복.\n예: pipeline_c_approve(job_id='pc-...', approved=true)",
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
    },
    {
        "name": "pipeline_c_cancel",
        "description": "파이프라인C 작업 강제 취소. 원격 Claude 프로세스도 kill. 멈춘 작업 정리용.\n예: pipeline_c_cancel(job_id='pc-...')",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "취소할 파이프라인 작업 ID",
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "pipeline_c_retry",
        "description": "에러/취소된 파이프라인C 작업을 동일 지시로 재실행. 먼저 cancel 후 retry 권장.\n예: pipeline_c_retry(job_id='pc-...')",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "재실행할 파이프라인 작업 ID (에러/취소 상태여야 함)",
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "search_chat_history",
        "description": "과거 대화 내용을 키워드(FTS+LIKE) 또는 시맨틱(임베딩 유사도)으로 검색.\n컴팩션으로 사라진 오래된 대화도 DB 원문에서 검색 가능.\n예: search_chat_history(query='토큰 갱신 오류', mode='semantic', limit=5)",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "검색 키워드 또는 자연어 질의 (한국어 가능)",
                },
                "mode": {
                    "type": "string",
                    "enum": ["keyword", "semantic"],
                    "description": "검색 모드: keyword(FTS+LIKE), semantic(임베딩 유사도). 기본=keyword",
                },
                "session_id": {
                    "type": "string",
                    "description": "특정 세션 ID (생략 시 전체 세션 검색)",
                },
                "date_from": {
                    "type": "string",
                    "description": "시작 날짜 (YYYY-MM-DD, 선택)",
                },
                "date_to": {
                    "type": "string",
                    "description": "종료 날짜 (YYYY-MM-DD, 선택)",
                },
                "role": {
                    "type": "string",
                    "enum": ["user", "assistant", "all"],
                    "description": "발화자 필터 (기본=all)",
                },
                "limit": {
                    "type": "integer",
                    "description": "최대 결과 수 (1-30, 기본=10)",
                },
            },
            "required": ["query"],
        },
    },
    # ── F12: Timeline Memory ─────────────────────────────────────────────
    {
        "name": "query_timeline",
        "description": "프로젝트별 시간순 이력 조회 (memory_facts 기반). 이벤트/결정/변경 이력을 타임라인 형태로 표시.\n예: query_timeline(project='KIS', period='7d', category='decision')",
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
    # ── F5: Tool Result Recall ───────────────────────────────────────────
    {
        "name": "recall_tool_result",
        "description": "과거 도구 실행 결과를 검색하여 재실행 없이 즉시 참조. tool_results_archive에서 검색.\n예: recall_tool_result(tool_name='query_db', keyword='users', limit=3)",
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
    # ── C4: Decision Dependency Graph ────────────────────────────────────
    {
        "name": "query_decision_graph",
        "description": (
            "결정/사실의 의존관계 트리를 BFS 탐색. "
            "subject(부분 일치) 또는 fact_id(UUID)로 시작점을 지정하면 related_facts를 재귀적으로 추적하여 "
            "상위/하위 결정 체인을 보여줌. 결정 변경 시 영향 범위 파악에 활용.\n"
            "예: query_decision_graph(subject='auth middleware') → 해당 결정에 의존하는 모든 후속 결정 표시."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "검색할 사실의 subject (부분 일치). 예: 'auth middleware', '토큰 갱신'",
                },
                "fact_id": {
                    "type": "string",
                    "description": "시작 사실의 UUID (정확히 지정). subject 대신 사용 가능.",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "탐색 깊이 (1~3, 기본 3). 1=직접 관계만, 3=3단계까지 재귀.",
                },
            },
            "required": [],
        },
    },
    # ── 멀티에이전트 팀 오케스트레이션 ─────────────────────────────────
    {
        "name": "run_agent_team",
        "description": (
            "전문 에이전트 팀을 구성하여 단계별로 실행. "
            "단계(phase) 내 태스크는 병렬 실행, 단계 간은 순차 실행. "
            "에이전트 간 발견사항 자동 공유 + 결과 종합.\n"
            "역할: researcher(조사), developer(코드수정), qa(테스트), devops(배포), architect(설계).\n"
            "예: run_agent_team(name='KIS 수정', phases=[\n"
            "  {name:'조사', tasks:[{task:'에러로그확인', role:'researcher'}, {task:'코드분석', role:'researcher'}]},\n"
            "  {name:'수정', tasks:[{task:'버그수정', role:'developer'}]},\n"
            "  {name:'검증', tasks:[{task:'문법확인', role:'qa'}]}\n"
            "])"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "팀/작업 이름 (예: 'KIS 주문 버그 수정')",
                },
                "phases": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "단계 이름 (예: '조사', '수정', '검증')"},
                            "tasks": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "task": {"type": "string", "description": "작업 지시"},
                                        "role": {
                                            "type": "string",
                                            "enum": ["researcher", "developer", "qa", "devops", "architect", "general"],
                                            "description": "에이전트 역할",
                                        },
                                        "model": {
                                            "type": "string",
                                            "enum": ["sonnet", "opus", "haiku"],
                                            "description": "모델 (기본 sonnet)",
                                        },
                                    },
                                    "required": ["task", "role"],
                                },
                            },
                            "model": {"type": "string", "description": "단계 기본 모델"},
                        },
                        "required": ["name", "tasks"],
                    },
                    "description": "실행 단계 목록. 단계 내 태스크 병렬, 단계 간 순차.",
                },
                "max_concurrent": {
                    "type": "integer",
                    "description": "동시 실행 에이전트 수 (기본 5)",
                },
                "cost_limit_usd": {
                    "type": "number",
                    "description": "비용 한도 USD (기본 10.0)",
                },
            },
            "required": ["name", "phases"],
        },
    },
    # ── SSH 원격 쓰기 도구 (Yellow 등급) ─────────────────────────────────
    {
        "name": "write_remote_file",
        "description": (
            "원격 서버에 파일 쓰기 (SSH). 쓰기 전 자동 .bak_aads 백업 생성.\n"
            "보안: .env/.ssh/credentials 등 민감 파일 차단. 최대 1MB.\n"
            "예: write_remote_file(project='KIS', file_path='backend/config.py', content='...')"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "대상 프로젝트",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
                "file_path": {
                    "type": "string",
                    "description": "WORKDIR 기준 상대 파일 경로. 예: backend/app/main.py",
                },
                "content": {
                    "type": "string",
                    "description": "파일에 쓸 전체 내용 (최대 1MB)",
                },
                "backup": {
                    "type": "boolean",
                    "description": "쓰기 전 .bak_aads 백업 생성 여부 (기본 true)",
                },
            },
            "required": ["project", "file_path", "content"],
        },
    },
    {
        "name": "patch_remote_file",
        "description": (
            "원격 서버 파일의 특정 부분만 교체 (diff 기반 패치). "
            "old_string이 파일 내 정확히 1회만 나타나야 성공. 자동 백업 생성.\n"
            "예: patch_remote_file(project='AADS', file_path='app/main.py', "
            "old_string='port=8000', new_string='port=8080')"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "대상 프로젝트",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
                "file_path": {
                    "type": "string",
                    "description": "WORKDIR 기준 상대 파일 경로",
                },
                "old_string": {
                    "type": "string",
                    "description": "교체할 원본 문자열 (정확히 1회만 매치되어야 함)",
                },
                "new_string": {
                    "type": "string",
                    "description": "새로 교체할 문자열 (old_string과 달라야 함)",
                },
            },
            "required": ["project", "file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "run_remote_command",
        "description": (
            "원격 서버에서 화이트리스트 명령 실행 (SSH). 출력 최대 50KB.\n"
            "허용 명령: ls, cat, grep, find, git, docker, pip, python, systemctl, "
            "supervisorctl, nginx, journalctl, curl, du, ps, top, df, free, "
            "crontab -l, kill/pkill 등.\n"
            "차단: rm -rf, sudo, force push, hard reset, bash -c, 파이프 체인(; && ||).\n"
            "예: run_remote_command(project='KIS', command='git status --short')"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "대상 프로젝트",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
                "command": {
                    "type": "string",
                    "description": "실행할 셸 명령 (화이트리스트 기반, 위험 명령 차단)",
                },
            },
            "required": ["project", "command"],
        },
    },
    # ── Git 원격 도구 ────────────────────────────────────────────────────
    {
        "name": "git_remote_status",
        "description": "원격 서버의 git 작업 트리 상태 조회 (git status --short).\n예: git_remote_status(project='KIS')",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "대상 프로젝트",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
            },
            "required": ["project"],
        },
    },
    {
        "name": "git_remote_add",
        "description": "원격 서버에서 git add (스테이징).\n예: git_remote_add(project='KIS', files='backend/') 또는 files='.' (전체)",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "대상 프로젝트",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
                "files": {
                    "type": "string",
                    "description": "스테이징할 파일/디렉터리 (기본 '.' = 전체)",
                },
            },
            "required": ["project"],
        },
    },
    {
        "name": "git_remote_commit",
        "description": "원격 서버에서 git commit. 메시지는 shlex 이스케이프 적용.\n예: git_remote_commit(project='KIS', message='fix: order executor null check')",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "대상 프로젝트",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
                "message": {
                    "type": "string",
                    "description": "커밋 메시지 (최대 200자)",
                },
            },
            "required": ["project", "message"],
        },
    },
    {
        "name": "git_remote_push",
        "description": "원격 서버에서 git push. force push 차단.\n예: git_remote_push(project='KIS') 또는 git_remote_push(project='KIS', branch='main')",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "대상 프로젝트",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
                "branch": {
                    "type": "string",
                    "description": "푸시할 브랜치 (선택, 생략 시 현재 브랜치)",
                },
            },
            "required": ["project"],
        },
    },
    {
        "name": "git_remote_create_branch",
        "description": "원격 서버에서 새 브랜치 생성 및 체크아웃.\n예: git_remote_create_branch(project='KIS', branch_name='feature/order-fix')",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "대상 프로젝트",
                    "enum": ["AADS", "KIS", "GO100", "SF", "NTV2"],
                },
                "branch_name": {
                    "type": "string",
                    "description": "새 브랜치 이름 (영숫자, 점, 하이픈, 슬래시 허용)",
                },
            },
            "required": ["project", "branch_name"],
        },
    },
    # ── 프로젝트 DB 조회 도구 ────────────────────────────────────────────
    {
        "name": "query_project_database",
        "description": (
            "프로젝트별 원격 DB에 SELECT 쿼리 실행.\n"
            "- KIS/GO100: PostgreSQL (211서버)\n"
            "- SF: MariaDB (114서버, SSH 터널)\n"
            "- NTV2: MySQL 8.0 (114서버, SSH 터널)\n"
            "보안: SELECT/WITH/EXPLAIN만 허용. DML/DDL 차단. password/token 컬럼 자동 마스킹.\n"
            "예: query_project_database(project='KIS', query='SELECT * FROM users LIMIT 5')"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "대상 프로젝트",
                    "enum": ["KIS", "GO100", "SF", "NTV2"],
                },
                "query": {
                    "type": "string",
                    "description": "SELECT SQL 쿼리",
                },
                "limit": {
                    "type": "integer",
                    "description": "최대 반환 행 수 (기본 100, 최대 1000)",
                },
                "db_name": {
                    "type": "string",
                    "description": "DB 이름 (미지정 시 프로젝트 메인 DB). NTV2의 autoda DB 접근 시 사용.",
                },
            },
            "required": ["project", "query"],
        },
    },
    {
        "name": "list_project_databases",
        "description": "설정된 프로젝트 DB 목록 및 연결 상태 조회. 각 프로젝트의 호스트/포트/DB종류/연결 성공 여부 반환.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    # ── 데이터 내보내기 도구 ──────────────────────────────────────────────
    {
        "name": "export_data",
        "description": (
            "데이터를 CSV/Excel/PDF로 내보내기. 파일은 /exports/에서 다운로드 가능.\n"
            "예: export_data(data=[{'이름':'삼성','가격':70000}], fmt='xlsx', title='종목 리스트')\n"
            "반환: {url: 'https://aads.newtalk.kr/exports/filename.xlsx', rows: N}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "내보낼 데이터 (dict 리스트). 예: [{'col1': 'val1', 'col2': 'val2'}, ...]",
                },
                "fmt": {
                    "type": "string",
                    "enum": ["csv", "xlsx", "pdf"],
                    "description": "출력 포맷 (기본 xlsx)",
                },
                "filename": {
                    "type": "string",
                    "description": "파일명 (선택, 자동 생성)",
                },
                "title": {
                    "type": "string",
                    "description": "문서 제목 (선택)",
                },
            },
            "required": ["data"],
        },
    },
    # ── 스케줄러 도구 ────────────────────────────────────────────────────
    {
        "name": "schedule_task",
        "description": (
            "예약 작업 등록 (cron/interval/once). 결과는 텔레그램으로 알림.\n"
            "action_type: remote_command(명령실행), health_check(헬스체크), "
            "db_query(DB조회), url_check(URL 확인).\n"
            "예(매일 9:30 KST): schedule_task(name='daily_check', schedule_type='cron', "
            "action_type='health_check', action_config={}, schedule_config={'hour':9,'minute':30})"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "작업 이름 (고유 ID로 사용)",
                },
                "schedule_type": {
                    "type": "string",
                    "enum": ["cron", "interval", "once"],
                    "description": "스케줄 유형. cron=크론, interval=주기, once=1회",
                },
                "action_type": {
                    "type": "string",
                    "enum": ["remote_command", "health_check", "db_query", "url_check"],
                    "description": "실행할 액션 유형",
                },
                "action_config": {
                    "type": "object",
                    "description": (
                        "액션별 설정. remote_command: {project, command}. "
                        "db_query: {project, query}. url_check: {url}. "
                        "health_check: {} (빈 객체)"
                    ),
                },
                "schedule_config": {
                    "type": "object",
                    "description": (
                        "스케줄 설정. cron: {hour, minute, day_of_week(선택)}. "
                        "interval: {minutes} 또는 {hours}. "
                        "once: {delay_minutes} (N분 후 1회)"
                    ),
                },
            },
            "required": ["name", "schedule_type", "action_type", "action_config"],
        },
    },
    {
        "name": "unschedule_task",
        "description": "등록된 예약 작업 삭제.\n예: unschedule_task(name='daily_check')",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "삭제할 예약 작업 이름",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_scheduled_tasks",
        "description": "등록된 예약 작업 목록 조회. 시스템 작업과 사용자 작업 구분하여 반환. next_run(다음 실행 시각) 포함.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    # ── 스크린샷 캡처 도구 ───────────────────────────────────────────────
    {
        "name": "capture_screenshot",
        "description": (
            "URL 스크린샷을 캡처하여 이미지 URL 반환. 채팅 내 인라인 표시용.\n"
            "browser_screenshot과 달리 독립 캡처→이미지 파일 저장→URL 반환.\n"
            "예: capture_screenshot(url='https://aads.newtalk.kr/')"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "캡처할 URL (허용 도메인: *.newtalk.kr, github.com, localhost)",
                },
                "full_page": {
                    "type": "boolean",
                    "description": "전체 페이지 캡처 여부 (기본 false = 뷰포트만)",
                },
            },
            "required": ["url"],
        },
    },
    # ── 작업 모니터 도구 ─────────────────────────────────────────────────
    {
        "name": "check_task_status",
        "description": (
            "Pipeline B/C 활성 작업 현황 조회. 진행 중인 작업의 phase, 경과시간, stall 감지 정보 반환.\n"
            "예: check_task_status() → 전체 활성 작업 목록"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "read_task_logs",
        "description": (
            "특정 작업의 실행 로그 조회. 최근 N건 또는 특정 시점 이후 로그.\n"
            "예: read_task_logs(task_id='pc-1741654800-abc123', last_n=20)"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "작업 ID (Pipeline B: directive ID, Pipeline C: pc-* ID)",
                },
                "last_n": {
                    "type": "integer",
                    "description": "최근 N건 조회 (기본 50, 최대 200)",
                },
                "log_type": {
                    "type": "string",
                    "enum": ["info", "command", "output", "error", "phase_change"],
                    "description": "로그 유형 필터 (선택)",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "terminate_task",
        "description": (
            "활성 작업 강제 종료. Pipeline C는 원격 Claude 프로세스도 정리.\n"
            "예: terminate_task(task_id='pc-1741654800-abc123')"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "종료할 작업 ID",
                },
            },
            "required": ["task_id"],
        },
    },
]

# ─── SSH 원격 접근 상수 (중앙 설정에서 import, AADS-165) ──────────────────────
from app.core.project_config import PROJECT_MAP as _PROJECT_MAP_FULL, REMOTE_PROJECTS
_PROJECT_SERVER_MAP: Dict[str, Dict[str, str]] = {
    k: v for k, v in _PROJECT_MAP_FULL.items() if k in REMOTE_PROJECTS
}

# SSH 보안 규칙 (하드코딩, LLM 우회 불가)
# 화이트리스트: 영숫자, 유니코드(한글 등), 점, 하이픈(표준+비표준), 밑줄, 슬래시, 공백 허용
# 위험 문자(; | & ` $ ( ) > < \n \r 등) 차단 — shlex.quote()로도 방어
_SSH_PATH_WHITELIST = re.compile(r'^[\w._/\- \u2010-\u2015\u00a0]+$', re.UNICODE)
_SSH_KEYWORD_WHITELIST = re.compile(r'^[\w._\- \u2010-\u2015]+$', re.UNICODE)
_SSH_SENSITIVE_PATTERNS = re.compile(
    r'(\.env|\.ssh/|id_rsa|\.git/config|secrets|password|token'
    r'|\.npmrc|\.pypirc|\.netrc|credentials|private_key|kubeconfig'
    r'|\.aws/|\.kube/|\.docker/|\.pem$|\.key$|authorized_keys|known_hosts)',
    re.IGNORECASE,
)
_SSH_TIMEOUT = 10  # 초 (ConnectTimeout=5 + CommandTimeout=5)
_SSH_WRITE_TIMEOUT = 15  # 쓰기 작업은 조금 더 여유
_SSH_CMD_TIMEOUT = 30  # 원격 명령 실행 타임아웃
_SSH_MAX_RESULT_BYTES = 1024 * 1024  # 1MB (제한 없음 — Claude Code 동일)
_SSH_MAX_WRITE_BYTES = 1024 * 1024  # 1MB 쓰기 제한
_SSH_MAX_FILES = 100
_SSH_MAX_DEPTH = 5

# run_remote_command 허용 명령 화이트리스트 (보안 하드코딩, LLM 우회 불가)
_REMOTE_CMD_WHITELIST: List[str] = [
    "systemctl restart",
    "systemctl start",
    "systemctl stop",
    "systemctl status",
    "docker restart",
    "docker start",
    "docker stop",
    "docker ps",
    "docker logs",
    "pip install",
    "pip list",
    "python -m py_compile",
    "python3 -m py_compile",
    "python -m py_compile",
    "pytest",
    "cat /proc/meminfo",
    "cat /proc/cpuinfo",
    "df -h",
    "free",
    "ps aux",
    "tail -n",
    "head -n",
    "wc -l",
    "grep",
    "find",
    "ls",
    "pwd",
    "whoami",
    "date",
    "uptime",
    "crontab -l",
    # Docker 확장 (AADS-190)
    "docker compose up",
    "docker compose down",
    "docker compose build",
    "docker compose pull",
    "docker exec",
    "docker images",
    "docker stats",
    "docker inspect",
    "docker network ls",
    "docker volume ls",
    # Nginx (AADS-190)
    "nginx -t",
    "nginx -s reload",
    "nginx -s stop",
    "cat /etc/nginx",
    # Supervisord (AADS-190)
    "supervisorctl status",
    "supervisorctl restart",
    "supervisorctl start",
    "supervisorctl stop",
    # 추가 시스템 도구
    "journalctl",
    "netstat -tlnp",
    "ss -tlnp",
    "curl -s",
    "wget -q",
    "top -bn1",
    "du -sh",
    # "env" 제거됨 (시크릿 노출 위험)
    "cat /etc/os-release",
    # Git 명령 (AADS-190)
    "git status",
    "git log",
    "git diff",
    "git add",
    "git commit",
    "git push",
    "git pull",
    "git checkout",
    "git branch",
    "git stash",
    "git show",
    "git remote",
    # 프로세스 관리 (Chromium 등 좀비 프로세스 정리용)
    "kill",
    "pkill",
    "killall",
    "top",
    "htop",
    # Swap 관리 (CEO 요청: 메모리 확장용)
    "swapon",
    "swapoff",
    "fallocate",
    "mkswap",
]

# run_remote_command 차단 패턴 (보안 하드코딩, LLM 우회 불가)
_REMOTE_CMD_BLOCKED = re.compile(
    r"(rm\s+-[rf]|mkfs|dd\s+if=|shutdown|halt|reboot|kill\s+-9\s+1\b"
    r"|>\s*/dev/|chmod\s+[0-7]{3,4}\s+/"
    r"|DROP\s+(TABLE|DATABASE)|DELETE\s+FROM|TRUNCATE"
    r"|curl.*\|.*sh|wget.*\|.*sh|bash\s+-c|python3?\s+-c|python3?\s.*\beval\b|python3?\s.*\bexec\b"
    r"|\brm\b.*\s+/[a-z]"  # rm /anything 차단
    r"|:(){:|fork\s*bomb"
    r"|git\s+push\s+.*--force"  # force push 차단
    r"|git\s+reset\s+--hard"  # hard reset 차단
    r"|git\s+clean\s+-[fd]"  # clean 차단
    r"|\bfind\b.*\s-exec\b"  # find -exec 차단
    r"|\bfind\b.*\s-delete\b"  # find -delete 차단
    r"|\bfind\b.*\s-ok\b)",  # find -ok 차단
    re.IGNORECASE,
)

# ─── 보안 상수 ─────────────────────────────────────────────────────────────────
_FILE_WHITELIST = "/root/aads/"
_FILE_BLACKLIST = ["/etc", "/proc", "/root/.ssh", "/root/.genspark/directives"]
_SQL_BLOCKED = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE|UNION|INTO\s+OUTFILE|LOAD_FILE)\b",
    re.IGNORECASE,
)
_MAX_LOG_BYTES = 10 * 1024   # 10 KB
_MAX_URL_BYTES = 20 * 1024   # 20 KB
_MAX_DB_ROWS = 50

# ─── Browser 보안 상수 (AADS-159, 하드코딩 — LLM 우회 불가) ──────────────
_BROWSER_ALLOWED_DOMAINS = frozenset([
    "newtalk.kr",
    "aads.newtalk.kr",
    "github.com",
    "raw.githubusercontent.com",
    "localhost",
    "127.0.0.1",
])
_BROWSER_ALLOWED_SUFFIX = ".newtalk.kr"
_BROWSER_TIMEOUT_MS = 60_000   # 60초 세션 타임아웃
_BROWSER_MAX_TABS = 3          # 최대 3탭

# Playwright 싱글턴 (FastAPI event loop 내 유지)
_pw_handle = None
_pw_browser = None
_pw_context = None
_pw_init_lock: Optional[asyncio.Lock] = None


# ─── 도구 실행 함수들 ──────────────────────────────────────────────────────────

async def tool_read_file(path: str) -> str:
    """로컬 파일 읽기 (화이트리스트 검사)."""
    try:
        resolved = str(Path(path).resolve())
    except Exception as e:
        return f"[ERROR] 경로 처리 실패: {e}"

    if not resolved.startswith(_FILE_WHITELIST):
        return f"[ERROR] 접근 거부: /root/aads/ 하위 경로만 허용됩니다. (요청: {resolved})"
    for blocked in _FILE_BLACKLIST:
        if resolved.startswith(blocked):
            return f"[ERROR] 접근 거부: {blocked} 경로는 차단되어 있습니다."

    try:
        p = Path(resolved)
        if not p.exists():
            return f"[ERROR] 파일 없음: {resolved}"
        if not p.is_file():
            return f"[ERROR] 파일이 아닙니다: {resolved}"
        content = p.read_text(encoding="utf-8", errors="replace")
        if len(content) > 50_000:
            content = content[:50_000] + "\n...(50KB 초과, 잘림)"
        return content
    except Exception as e:
        return f"[ERROR] 파일 읽기 실패: {e}"


async def tool_read_github(
    path: str, repo: str = "aads-docs", branch: str = "main"
) -> str:
    """GitHub raw 파일 읽기 (moongoby-GO100 레포)."""
    raw_url = f"https://raw.githubusercontent.com/moongoby-GO100/{repo}/{branch}/{path}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(raw_url)
            if r.status_code == 404:
                return f"[ERROR] GitHub 파일 없음: {raw_url}"
            r.raise_for_status()
            content = r.text
            if len(content) > 50_000:
                content = content[:50_000] + "\n...(50KB 초과, 잘림)"
            return content
    except Exception as e:
        return f"[ERROR] GitHub 읽기 실패: {e}"


async def tool_search_logs(source: str, keyword: Optional[str] = None) -> str:
    """Docker logs 또는 journalctl 검색 (최근 100줄, 최대 10KB)."""
    _ALLOWED_LOG_SOURCES = {"aads-server", "aads-dashboard", "aads-postgres", "aads-redis", "aads-litellm", "aads-core", "journalctl"}
    try:
        if source.lower() == "journalctl":
            cmd = ["journalctl", "--no-pager", "-n", "100"]
        else:
            # 컨테이너 이름 검증 (인젝션 방지)
            if source not in _ALLOWED_LOG_SOURCES:
                return f"[ERROR] 허용되지 않는 로그 소스: {source}. 허용 목록: {', '.join(sorted(_ALLOWED_LOG_SOURCES))}"
            cmd = ["docker", "logs", "--tail", "100", source]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = result.stdout + result.stderr

        if keyword:
            lines = [l for l in output.splitlines() if keyword.lower() in l.lower()]
            output = "\n".join(lines[-100:])

        # 크기 제한
        encoded = output.encode("utf-8", errors="replace")
        if len(encoded) > _MAX_LOG_BYTES:
            output = encoded[-_MAX_LOG_BYTES:].decode("utf-8", errors="replace").lstrip()
            output = "[...앞부분 잘림...]\n" + output

        return output if output.strip() else f"[로그 없음: {source}]"
    except subprocess.TimeoutExpired:
        return f"[ERROR] 로그 조회 타임아웃: {source}"
    except Exception as e:
        return f"[ERROR] 로그 조회 실패: {e}"


async def tool_query_db(sql: str, dsn: str) -> str:
    """PostgreSQL SELECT 쿼리 실행 (SELECT 전용, 최대 50행)."""
    sql_stripped = sql.strip()
    if not sql_stripped.upper().startswith("SELECT"):
        return "[ERROR] SELECT 쿼리만 허용됩니다."
    if _SQL_BLOCKED.search(sql_stripped):
        return "[ERROR] 허용되지 않는 SQL 명령어가 포함되어 있습니다."

    try:
        conn = await asyncpg.connect(dsn=dsn)
        try:
            rows = await conn.fetch(sql_stripped)
            if not rows:
                return "(결과 없음)"
            rows = list(rows[:_MAX_DB_ROWS])
            cols = list(rows[0].keys())
            lines = [" | ".join(cols)]
            lines.append("-" * max(len(lines[0]), 10))
            for r in rows:
                lines.append(" | ".join(str(v) if v is not None else "NULL" for v in r.values()))
            suffix = f"\n(최대 {_MAX_DB_ROWS}행 제한)" if len(rows) == _MAX_DB_ROWS else ""
            return "\n".join(lines) + suffix
        finally:
            await conn.close()
    except Exception as e:
        return f"[ERROR] DB 쿼리 실패: {e}"


_SSRF_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fd00::/8"),
]
_SSRF_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "169.254.169.254"}


def _is_ssrf_target(url: str) -> Optional[str]:
    """Return error string if URL resolves to a private/blocked address, else None."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return "[ERROR] SSRF 차단: 호스트명을 파싱할 수 없습니다."
        if hostname.lower() in _SSRF_BLOCKED_HOSTS:
            return f"[ERROR] SSRF 차단: 차단된 호스트 ({hostname})"
        # Resolve hostname and check all IPs
        try:
            addrinfos = socket.getaddrinfo(hostname, parsed.port or 80, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            return f"[ERROR] SSRF 차단: DNS 확인 실패 ({hostname})"
        for family, _, _, _, sockaddr in addrinfos:
            ip = ipaddress.ip_address(sockaddr[0])
            for net in _SSRF_BLOCKED_NETWORKS:
                if ip in net:
                    return f"[ERROR] SSRF 차단: 내부 네트워크 접근 불가 ({ip})"
    except Exception as e:
        return f"[ERROR] SSRF 검증 실패: {e}"
    return None


async def tool_fetch_url(url: str) -> str:
    """외부 URL GET (최대 20KB)."""
    # SSRF protection: block private IPs and internal hosts
    ssrf_err = _is_ssrf_target(url)
    if ssrf_err:
        return ssrf_err
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            content = r.text
            encoded = content.encode("utf-8", errors="replace")
            if len(encoded) > _MAX_URL_BYTES:
                content = encoded[:_MAX_URL_BYTES].decode("utf-8", errors="replace")
                content += "\n...(20KB 초과, 잘림)"
            return content
    except Exception as e:
        return f"[ERROR] URL 조회 실패: {e}"


# ─── SSH 원격 접근 도구 함수 (AADS-165) ──────────────────────────────────────────

def _validate_ssh_path(raw_path: str, workdir: str, extra_workdirs: Optional[List[str]] = None) -> Optional[str]:
    """SSH 경로 보안 검증. 위반 시 에러 문자열, 통과 시 None."""
    if not _SSH_PATH_WHITELIST.match(raw_path):
        return "[ERROR] 접근 거부: 경로에 허용되지 않는 문자가 포함되어 있습니다."
    if _SSH_SENSITIVE_PATTERNS.search(raw_path):
        return "[ERROR] 접근 거부: 민감한 파일 패턴이 감지되었습니다."
    # WORKDIR 탈출 방지: .. resolve (메인 + 추가 workdir 모두 허용)
    from posixpath import normpath, join as pjoin
    allowed_dirs = [workdir] + (extra_workdirs or [])
    resolved = normpath(pjoin(workdir, raw_path))
    if not any(resolved.startswith(d) for d in allowed_dirs):
        return f"[ERROR] 접근 거부: 허용 경로({', '.join(allowed_dirs)}) 바깥 접근 불가."
    return None


async def tool_list_remote_dir(
    project: str, path: str = "", keyword: str = "", max_depth: int = 3
) -> str:
    """원격 서버 디렉터리 탐색 (읽기 전용, find)."""
    project = project.upper()

    # AADS 프로젝트: 로컬 직접 탐색 (SSH 불필요)
    if project == "AADS":
        from app.core.project_config import PROJECT_MAP
        # 컨테이너 내부 경로 사용 (호스트 /root/aads/aads-server/app → 컨테이너 /app/app)
        workdir = "/app"
        max_depth = min(max(1, max_depth), _SSH_MAX_DEPTH)
        from posixpath import normpath, join as pjoin
        target = normpath(pjoin(workdir, path)) if path else workdir
        if not target.startswith(workdir):
            return f"[ERROR] 경로 탈출 차단: {target}"
        try:
            find_cmd = f"find {shlex.quote(target)} -maxdepth {max_depth} -type f"
            if keyword:
                find_cmd += f" -name {shlex.quote('*' + keyword + '*')}"
            find_cmd += f" | head -{_SSH_MAX_FILES}"
            proc = await asyncio.create_subprocess_shell(
                find_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode("utf-8", errors="replace")
            if not output.strip():
                return f"[AADS] 파일 없음 (경로: {target})"
            if len(output.encode("utf-8")) > _SSH_MAX_RESULT_BYTES:
                output = output[:_SSH_MAX_RESULT_BYTES] + "\n...(50KB 초과, 잘림)"
            return f"[AADS 디렉터리 — {target}]\n{output}"
        except Exception as e:
            return f"[ERROR] 로컬 탐색 실패: {e}"

    mapping = _PROJECT_SERVER_MAP.get(project)
    if not mapping:
        avail = ", ".join(["AADS"] + list(_PROJECT_SERVER_MAP.keys()))
        return f"[ERROR] 알 수 없는 프로젝트: {project}. 사용 가능: {avail}"

    server = mapping["server"]
    workdir = mapping["workdir"]
    ssh_port = mapping.get("port", "22")
    _extra = [mapping["workdir_v2"]] if "workdir_v2" in mapping else []
    max_depth = min(max(1, max_depth), _SSH_MAX_DEPTH)

    # 보안 검증
    if path:
        err = _validate_ssh_path(path, workdir, _extra)
        if err:
            return err
    if keyword and not _SSH_KEYWORD_WHITELIST.match(keyword):
        return "[ERROR] 접근 거부: keyword에 허용되지 않는 문자가 포함되어 있습니다."

    from posixpath import normpath, join as pjoin
    target = normpath(pjoin(workdir, path)) if path else workdir

    # find 명령 조립 (읽기 전용, shlex.quote로 인젝션 방지)
    find_cmd = f"find {shlex.quote(target)} -maxdepth {max_depth} -type f"
    if keyword:
        find_cmd += f" -name {shlex.quote('*' + keyword + '*')}"
    find_cmd += f" | head -{_SSH_MAX_FILES}"

    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            "-p", ssh_port,
            f"root@{server}", find_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SSH_TIMEOUT)
        output = stdout.decode("utf-8", errors="replace")
        if not output.strip():
            err_msg = stderr.decode("utf-8", errors="replace")[:500]
            if err_msg:
                logger.warning(f"ssh_list_remote_dir_stderr project={project} err={err_msg}")
            return f"[{project}] 파일 없음 (경로: {target})"
        if len(output.encode("utf-8")) > _SSH_MAX_RESULT_BYTES:
            output = output[:_SSH_MAX_RESULT_BYTES] + "\n...(50KB 초과, 잘림)"
        return f"[{project} 디렉터리 — {target}]\n{output}"
    except asyncio.TimeoutError:
        return f"[ERROR] SSH 타임아웃 ({_SSH_TIMEOUT}초): {server}"
    except Exception as e:
        return f"[ERROR] SSH 접속 실패: {e}"


async def tool_read_remote_file(project: str, file_path: str, offset: int = 1, limit: int = 2000) -> str:
    """원격 서버 파일 읽기 (Claude Code Read tool과 동일한 offset/limit 지원).

    Args:
        offset: 읽기 시작 줄 번호 (1부터, 기본 1)
        limit: 읽을 최대 줄 수 (기본 2000, Claude Code 동일)
    """
    project = project.upper()
    offset = max(1, int(offset or 1))
    limit = max(1, int(limit or 2000))  # 제한 없음 (Claude Code 동일)

    def _apply_line_range(content: str) -> tuple:
        """offset/limit 적용 + 줄 번호 추가 (cat -n 형식). returns (result, total_lines)"""
        lines = content.splitlines(keepends=True)
        total = len(lines)
        start_idx = offset - 1  # 0-based
        end_idx = min(start_idx + limit, total)
        selected = lines[start_idx:end_idx]

        # Claude Code cat -n 형식: "     1\tcontent"
        numbered = []
        for i, line in enumerate(selected, start=offset):
            numbered.append(f"{i:>6}\t{line.rstrip()}")
        result = "\n".join(numbered)

        meta_parts = []
        if offset > 1:
            meta_parts.append(f"offset={offset}")
        if end_idx < total:
            meta_parts.append(f"showing {end_idx - start_idx}/{total} lines")
        elif total > 0:
            meta_parts.append(f"{total} lines total")
        meta = f" ({', '.join(meta_parts)})" if meta_parts else f" ({total} lines)"
        return result, meta

    # AADS 프로젝트: 로컬 파일 직접 읽기 (SSH 불필요)
    if project == "AADS":
        file_path = _normalize_aads_path(file_path)
        from app.core.project_config import PROJECT_MAP
        workdir = "/app"
        from posixpath import normpath, join as pjoin
        resolved = normpath(pjoin(workdir, file_path))
        if not resolved.startswith(workdir):
            return f"[ERROR] 경로 탈출 차단: {resolved}"
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            result, meta = _apply_line_range(content)
            return f"[AADS 파일 — {resolved}{meta}]\n{result}"
        except FileNotFoundError:
            return (
                f"[ERROR] 파일 없음: {resolved}\n"
                f"→ AADS 경로 규칙: 상대 경로 사용 (예: app/main.py, app/api/ceo_chat_tools.py)\n"
                f"→ aads-dashboard 파일은 run_remote_command로: cat /root/aads/aads-dashboard/src/..."
            )
        except Exception as e:
            return f"[ERROR] 파일 읽기 실패: {e}"

    mapping = _PROJECT_SERVER_MAP.get(project)
    if not mapping:
        avail = ", ".join(["AADS"] + list(_PROJECT_SERVER_MAP.keys()))
        return f"[ERROR] 알 수 없는 프로젝트: {project}. 사용 가능: {avail}"

    server = mapping["server"]
    workdir = mapping["workdir"]
    ssh_port = mapping.get("port", "22")
    _extra = [mapping["workdir_v2"]] if "workdir_v2" in mapping else []

    # 보안 검증
    err = _validate_ssh_path(file_path, workdir, _extra)
    if err:
        return err

    from posixpath import normpath, join as pjoin
    resolved = normpath(pjoin(workdir, file_path))

    try:
        # SSH로 sed를 사용하여 서버 측에서 줄 범위 추출 (대용량 파일 효율)
        end_line = offset + limit - 1
        cmd = f"sed -n '{offset},{end_line}p' {shlex.quote(resolved)}"
        # 전체 줄 수도 함께 조회
        full_cmd = f"wc -l < {shlex.quote(resolved)} && echo '---SEPARATOR---' && {cmd}"
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            "-p", ssh_port,
            f"root@{server}", full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SSH_TIMEOUT)
        output = stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace")[:500]
            logger.warning(f"ssh_read_remote_file_failed project={project} path={resolved} err={err_msg}")
            return f"[ERROR] 파일 읽기 실패: 파일이 존재하지 않거나 읽기 권한이 없습니다."

        # 줄 수와 내용 분리
        if "---SEPARATOR---" in output:
            total_str, content = output.split("---SEPARATOR---\n", 1)
            total_lines = int(total_str.strip())
        else:
            content = output
            total_lines = content.count("\n")

        # 줄 번호 추가 (cat -n 형식)
        lines = content.splitlines()
        numbered = []
        for i, line in enumerate(lines, start=offset):
            numbered.append(f"{i:>6}\t{line}")
        result = "\n".join(numbered)

        meta_parts = []
        if offset > 1:
            meta_parts.append(f"offset={offset}")
        shown = len(lines)
        if offset + shown - 1 < total_lines:
            meta_parts.append(f"showing {shown}/{total_lines} lines")
        else:
            meta_parts.append(f"{total_lines} lines total")
        meta = f" ({', '.join(meta_parts)})" if meta_parts else ""

        return f"[{project} 파일 — {resolved}{meta}]\n{result}"
    except asyncio.TimeoutError:
        return f"[ERROR] SSH 타임아웃 ({_SSH_TIMEOUT}초): {server}"
    except Exception as e:
        return f"[ERROR] SSH 접속 실패: {e}"


# ─── SSH 원격 쓰기 도구 함수 (AADS-190: write_remote_file, patch_remote_file, run_remote_command) ───


async def tool_write_remote_file(project: str, file_path: str, content: str, backup: bool = True) -> str:
    """원격 서버 파일 쓰기 (SSH, 자동 백업 포함). Yellow 등급."""
    project = project.upper()

    if not file_path:
        return "[ERROR] file_path 필수"
    if not content:
        return "[ERROR] content 필수 (빈 파일 쓰기 차단)"

    # AADS 프로젝트: 로컬 직접 쓰기 (SSH 불필요)
    if project == "AADS":
        file_path = _normalize_aads_path(file_path)
        from app.core.project_config import PROJECT_MAP
        # 컨테이너 내부 경로 사용 (호스트 /root/aads/aads-server/app → 컨테이너 /app/app)
        workdir = "/app"
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > _SSH_MAX_WRITE_BYTES:
            return f"[ERROR] 파일 크기 초과: {len(content_bytes):,} bytes > 1MB 제한"
        err = _validate_ssh_path(file_path, workdir)
        if err:
            return err
        from posixpath import normpath, join as pjoin, dirname as pdirname
        resolved = normpath(pjoin(workdir, file_path))
        if not resolved.startswith(workdir):
            return f"[ERROR] 경로 탈출 차단: {resolved}"
        _write_blocked = [".env", ".ssh/", "id_rsa", "id_ed25519", "credentials",
                          "private_key", ".pem", ".key", "authorized_keys", ".netrc",
                          ".aws/", ".kube/", ".docker/"]
        for pattern in _write_blocked:
            if pattern in resolved.lower():
                return f"[ERROR] 민감 파일 쓰기 차단: {file_path}"
        try:
            import os
            os.makedirs(pdirname(resolved), exist_ok=True)
            if backup and os.path.exists(resolved):
                import shutil
                shutil.copy2(resolved, resolved + ".bak_aads")
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"write_remote_file OK | project=AADS path={resolved} size={len(content_bytes)}")
            backup_note = " (백업: .bak_aads)" if backup else ""
            return f"[AADS 파일 쓰기 완료 — {resolved}] {len(content_bytes):,} bytes{backup_note}"
        except Exception as e:
            return f"[ERROR] 로컬 파일 쓰기 실패: {e}"

    mapping = _PROJECT_SERVER_MAP.get(project)
    if not mapping:
        avail = ", ".join(["AADS"] + list(_PROJECT_SERVER_MAP.keys()))
        return f"[ERROR] 알 수 없는 프로젝트: {project}. 사용 가능: {avail}"

    server = mapping["server"]
    workdir = mapping["workdir"]
    ssh_port = mapping.get("port", "22")
    _extra = [mapping["workdir_v2"]] if "workdir_v2" in mapping else []

    # 크기 제한
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > _SSH_MAX_WRITE_BYTES:
        return f"[ERROR] 파일 크기 초과: {len(content_bytes):,} bytes > 1MB 제한"

    # 보안 검증 (읽기와 동일 경로 검증)
    err = _validate_ssh_path(file_path, workdir, _extra)
    if err:
        return err

    from posixpath import normpath, join as pjoin
    resolved = normpath(pjoin(workdir, file_path))

    # 추가 쓰기 보안: .env, .ssh, credentials 등 민감 파일 차단
    _write_blocked = [".env", ".ssh/", "id_rsa", "id_ed25519", "credentials",
                      "private_key", ".pem", ".key", "authorized_keys", ".netrc",
                      ".aws/", ".kube/", ".docker/"]
    for pattern in _write_blocked:
        if pattern in resolved.lower():
            return f"[ERROR] 민감 파일 쓰기 차단: {file_path}"

    try:
        # 1단계: 백업 (기존 파일이 있으면)
        if backup:
            backup_cmd = (
                f"test -f {shlex.quote(resolved)} && "
                f"cp {shlex.quote(resolved)} {shlex.quote(resolved + '.bak_aads')}"
            )
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                "-p", ssh_port,
                f"root@{server}", backup_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=_SSH_WRITE_TIMEOUT)

        # 2단계: 디렉토리 생성 + 파일 쓰기 (stdin pipe)
        from posixpath import dirname as pdirname
        mkdir_and_cat = f"mkdir -p {shlex.quote(pdirname(resolved))} && cat > {shlex.quote(resolved)}"
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            "-p", ssh_port,
            f"root@{server}", mkdir_and_cat,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=content_bytes), timeout=_SSH_WRITE_TIMEOUT
        )
        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace")[:500]
            logger.error(f"ssh_write_remote_file_failed project={project} path={resolved} err={err_msg}")
            return f"[ERROR] 파일 쓰기 실패: {err_msg}"

        logger.info(f"write_remote_file OK | project={project} path={resolved} size={len(content_bytes)}")
        backup_note = " (백업: .bak_aads)" if backup else ""
        return f"[{project} 파일 쓰기 완료 — {resolved}] {len(content_bytes):,} bytes{backup_note}"

    except asyncio.TimeoutError:
        return f"[ERROR] SSH 쓰기 타임아웃 ({_SSH_WRITE_TIMEOUT}초): {server}"
    except Exception as e:
        return f"[ERROR] SSH 쓰기 실패: {e}"


def _normalize_aads_path(file_path: str) -> str:
    """AADS 프로젝트 경로 자동교정 — AI가 자주 혼동하는 패턴 보정."""
    # /root/aads/aads-server/app/... → app/...
    if file_path.startswith("/root/aads/aads-server/"):
        file_path = file_path[len("/root/aads/aads-server/"):]
    # /app/aads-server/... → strip
    if file_path.startswith("/app/aads-server/"):
        file_path = file_path[len("/app/aads-server/"):]
    # aads-server/app/... → app/...
    if file_path.startswith("aads-server/"):
        file_path = file_path[len("aads-server/"):]
    # /app/app/... → app/...  (double prefix)
    if file_path.startswith("/app/"):
        file_path = file_path[len("/app/"):]
    return file_path


async def _read_raw_file(project: str, file_path: str) -> str:
    """줄 번호 없는 순수 파일 내용 반환 (patch 매칭용)."""
    project = project.upper()
    if project == "AADS":
        file_path = _normalize_aads_path(file_path)
        workdir = "/app"
        from posixpath import normpath, join as pjoin
        resolved = normpath(pjoin(workdir, file_path))
        if not resolved.startswith(workdir):
            return f"[ERROR] 경로 탈출 차단: {resolved}"
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            return f"[ERROR] 파일 없음: {resolved}"
        except Exception as e:
            return f"[ERROR] 파일 읽기 실패: {e}"

    mapping = _PROJECT_SERVER_MAP.get(project)
    if not mapping:
        return f"[ERROR] 알 수 없는 프로젝트: {project}"
    server = mapping["server"]
    workdir = mapping["workdir"]
    from posixpath import normpath, join as pjoin
    resolved = normpath(pjoin(workdir, file_path))
    if not resolved.startswith(workdir):
        return f"[ERROR] 경로 탈출 차단: {resolved}"
    cmd = f"cat {shlex.quote(resolved)}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            f"{server['user']}@{server['host']}", "-p", str(server.get("port", 22)),
            cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return f"[ERROR] 파일 읽기 실패: {stderr.decode('utf-8', errors='replace')[:200]}"
        return stdout.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        return "[ERROR] SSH 타임아웃"
    except Exception as e:
        return f"[ERROR] {e}"


async def tool_patch_remote_file(project: str, file_path: str, old_string: str, new_string: str) -> str:
    """원격 서버 파일 부분 수정 (diff 기반 패치). Yellow 등급.
    old_string을 찾아 new_string으로 교체. 정확히 1개만 매치되어야 함."""
    project = project.upper()
    # 프로젝트 유효성 검사 (AADS 포함 — read/write가 내부에서 각각 로컬 처리)
    from app.core.project_config import PROJECT_MAP
    if project not in PROJECT_MAP:
        avail = ", ".join(PROJECT_MAP.keys())
        return f"[ERROR] 알 수 없는 프로젝트: {project}. 사용 가능: {avail}"

    if not file_path:
        return "[ERROR] file_path 필수"
    if not old_string:
        return "[ERROR] old_string 필수"
    if old_string == new_string:
        return "[ERROR] old_string과 new_string이 동일"

    # 1단계: 현재 파일 읽기 (줄 번호 없는 raw content — 패치 매칭용)
    file_content = await _read_raw_file(project, file_path)
    if file_content.startswith("[ERROR]"):
        return file_content

    # 2단계: old_string 매치 확인
    count = file_content.count(old_string)
    if count == 0:
        # 유사 문자열 힌트 — 첫 줄 기준으로 가장 가까운 매치 찾기
        _first_line = old_string.split("\n")[0].strip()
        _hints = []
        if _first_line and len(_first_line) > 10:
            for i, line in enumerate(file_content.splitlines(), 1):
                if _first_line[:20] in line:
                    _hints.append(f"  Line {i}: {line.strip()[:120]}")
                    if len(_hints) >= 3:
                        break
        _hint_msg = ""
        if _hints:
            _hint_msg = "\n\n[힌트] old_string 첫 줄과 유사한 부분:\n" + "\n".join(_hints)
            _hint_msg += "\n\n→ read_remote_file로 해당 라인 주변을 다시 읽고, 정확한 문자열을 복사하세요."
        else:
            _hint_msg = "\n\n→ read_remote_file로 파일을 먼저 읽고, 줄 번호를 제외한 실제 코드를 old_string에 사용하세요."
        return f"[ERROR] old_string을 찾을 수 없음 (파일에 해당 문자열 없음){_hint_msg}"
    if count > 1:
        return f"[ERROR] old_string이 {count}회 중복 발견. 더 구체적인 문자열 필요"

    # 3단계: 교체 후 쓰기
    patched_content = file_content.replace(old_string, new_string, 1)
    result = await tool_write_remote_file(project, file_path, patched_content, backup=True)

    if result.startswith("[ERROR]"):
        return result

    # 변경 요약
    old_lines = old_string.count("\n") + 1
    new_lines = new_string.count("\n") + 1
    return f"[{project} 파일 패치 완료 — {file_path}] {old_lines}줄 → {new_lines}줄 교체\n{result}"


async def tool_run_remote_command(project: str, command: str) -> str:
    """원격 서버 명령 실행 (허용 명령 화이트리스트 기반). Yellow 등급."""
    project = project.upper()

    if not command or not command.strip():
        return "[ERROR] command 필수"

    command = command.strip()

    # 보안 1: 차단 패턴 검사 (안전한 리다이렉트는 제외 후 검사)
    _cmd_for_block_check = re.sub(r'2>&1', '', command)
    _cmd_for_block_check = re.sub(r'2>/dev/null', '', _cmd_for_block_check)
    if _REMOTE_CMD_BLOCKED.search(_cmd_for_block_check):
        logger.warning(f"run_remote_command BLOCKED | project={project} cmd={command[:120]}")
        return f"[ERROR] 위험 명령 차단: {command[:80]}"

    # 보안 2: 화이트리스트 검사 (첫 토큰 exact match 또는 "prefix " match)
    try:
        cmd_tokens = shlex.split(command)
    except ValueError:
        return "[ERROR] 명령어 파싱 실패"
    first_cmd = cmd_tokens[0] if cmd_tokens else ""
    # first-token 직접 허용 (cat, tail, head 등 기본 Unix 도구)
    _FIRST_TOKEN_ALLOW = {
        "cat", "tail", "head", "wc", "sort", "uniq", "stat", "file",
        "pgrep", "lsof", "readlink", "realpath", "dirname", "basename",
        "md5sum", "sha256sum", "tee", "tr", "cut",
        "id", "hostname", "uname", "swapon", "swapoff",
    }
    cmd_allowed = first_cmd in _FIRST_TOKEN_ALLOW
    if not cmd_allowed:
        for allowed in _REMOTE_CMD_WHITELIST:
            if first_cmd == allowed or command.startswith(allowed + " ") or command == allowed:
                cmd_allowed = True
                break
    if not cmd_allowed:
        logger.warning(f"run_remote_command WHITELIST_DENY | project={project} cmd={command[:120]}")
        _first_tok_list = sorted(set(c.split()[0] for c in _REMOTE_CMD_WHITELIST)) + sorted(_FIRST_TOKEN_ALLOW)
        return (
            f"[ERROR] 허용되지 않은 명령: {command[:80]}\n"
            f"허용 명령: {', '.join(sorted(set(_first_tok_list)))}\n"
            f"→ 이 명령 대신 허용된 명령으로 같은 결과를 얻을 수 있는지 검토하세요."
        )

    # 보안 2.5: docker exec 컨테이너 허용 목록 검사
    if command.startswith("docker exec"):
        _ALLOWED_CONTAINERS = {"aads-server", "aads-dashboard", "aads-postgres", "aads-redis", "aads-litellm", "aads-core"}
        parts = command.split()
        # docker exec [-it] CONTAINER CMD...
        container = None
        for p in parts[2:]:
            if not p.startswith("-"):
                container = p
                break
        if container not in _ALLOWED_CONTAINERS:
            return f"[ERROR] 허용되지 않는 컨테이너: {container}"

    # 보안 3: 파이프/리다이렉트/세미콜론 차단 (단일 명령만 허용)
    # 사전 정규화: grep의 \| (이스케이프 파이프)와 안전한 리다이렉트는 검사에서 제외
    _check_cmd = command.replace("\\|", "__ESC_PIPE__")  # grep "foo\|bar" 오탐 방지
    _check_cmd = re.sub(r'2>&1', '', _check_cmd)  # stderr→stdout 리다이렉트 허용
    _check_cmd = re.sub(r'2>/dev/null', '', _check_cmd)  # stderr 억제 허용
    if any(c in _check_cmd for c in ["|", ";", "&&", "`", "$(", ">>", ">", "\n", "\r", "${"]):
        # 단, grep | head 같은 안전한 파이프는 허용
        if "|" in _check_cmd:
            pipe_parts = _check_cmd.split("|")
            for part in pipe_parts[1:]:
                part_cmd = part.strip().split()[0] if part.strip() else ""
                if part_cmd not in ("head", "tail", "wc", "grep", "sort", "uniq", "cat", "less", "tr"):
                    return (
                        f"[ERROR] 파이프/체인 명령 차단 (보안): {command[:80]}\n"
                        f"→ 허용 파이프: cmd | head/tail/wc/grep/sort/uniq/cat/tr\n"
                        f"→ 2>&1, 2>/dev/null 리다이렉트는 허용됩니다."
                    )
        elif "||" not in _check_cmd:
            return (
                f"[ERROR] 파이프/체인 명령 차단 (보안): {command[:80]}\n"
                f"→ 세미콜론(;), 리다이렉트(>), 백틱(`) 사용 불가.\n"
                f"→ 명령을 분리하여 각각 실행하세요."
            )

    # AADS 프로젝트: 호스트 OS SSH 실행 (컨테이너→호스트)
    if project == "AADS":
        workdir = "/root/aads/aads-server"
        full_cmd = f"cd {shlex.quote(workdir)} && {command}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                "root@host.docker.internal", full_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SSH_CMD_TIMEOUT)
            out = stdout.decode("utf-8", errors="replace")
            err_out = stderr.decode("utf-8", errors="replace")
            if len(out.encode("utf-8")) > _SSH_MAX_RESULT_BYTES:
                out = out[:_SSH_MAX_RESULT_BYTES] + "\n...(50KB 초과, 잘림)"
            result_parts = [f"[AADS 명령 실행 — exit={proc.returncode}]", f"$ {command}"]
            if out.strip():
                result_parts.append(out.strip())
            if err_out.strip() and proc.returncode != 0:
                result_parts.append(f"[STDERR] {err_out.strip()[:2000]}")
            logger.info(f"run_remote_command OK | project=AADS cmd={command[:80]} exit={proc.returncode}")
            return "\n".join(result_parts)
        except asyncio.TimeoutError:
            return f"[ERROR] AADS 호스트 명령 타임아웃 ({_SSH_CMD_TIMEOUT}초)"
        except Exception as e:
            return f"[ERROR] AADS 호스트 명령 실행 실패: {e}"

    mapping = _PROJECT_SERVER_MAP.get(project)
    if not mapping:
        avail = ", ".join(["AADS"] + list(_PROJECT_SERVER_MAP.keys()))
        return f"[ERROR] 알 수 없는 프로젝트: {project}. 사용 가능: {avail}"

    server = mapping["server"]
    workdir = mapping["workdir"]
    ssh_port = mapping.get("port", "22")

    # 실행: workdir에서 명령 수행
    full_cmd = f"cd {shlex.quote(workdir)} && {command}"

    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            "-p", ssh_port,
            f"root@{server}", full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SSH_CMD_TIMEOUT)
        out = stdout.decode("utf-8", errors="replace")
        err_out = stderr.decode("utf-8", errors="replace")

        # 결과 크기 제한
        if len(out.encode("utf-8")) > _SSH_MAX_RESULT_BYTES:
            out = out[:_SSH_MAX_RESULT_BYTES] + "\n...(50KB 초과, 잘림)"

        result_parts = [f"[{project} 명령 실행 — exit={proc.returncode}]"]
        result_parts.append(f"$ {command}")
        if out.strip():
            result_parts.append(out.strip())
        if err_out.strip() and proc.returncode != 0:
            result_parts.append(f"[STDERR] {err_out.strip()[:2000]}")

        logger.info(f"run_remote_command OK | project={project} cmd={command[:80]} exit={proc.returncode}")
        return "\n".join(result_parts)

    except asyncio.TimeoutError:
        return f"[ERROR] SSH 명령 타임아웃 ({_SSH_CMD_TIMEOUT}초): {server}"
    except Exception as e:
        return f"[ERROR] SSH 명령 실행 실패: {e}"


# ─── Git 쓰기 도구 함수 (AADS-190: git_add, git_commit, git_push) ────────────


async def tool_git_remote_add(project: str, files: str = ".") -> str:
    """원격 서버 git add (스테이징)."""
    return await tool_run_remote_command(project, f"git add {files}")


async def tool_git_remote_commit(project: str, message: str) -> str:
    """원격 서버 git commit (커밋 전 자동 검수 포함)."""
    if not message or not message.strip():
        return "[ERROR] commit message 필수"

    # ── 커밋 전 자동 검수: staged .py 파일 구문 + import 검증 ──
    if project == "AADS":
        # staged 파일 목록 조회
        staged_out = await tool_run_remote_command(project, "git diff --cached --name-only -- '*.py'")
        staged_files = [
            f.strip() for f in staged_out.split("\n")
            if f.strip().endswith(".py") and not f.startswith("[")
        ]
        if staged_files:
            check_errors = []
            for sf in staged_files[:20]:  # 최대 20파일
                # 구문 검사
                syntax_result = await tool_run_remote_command(
                    project, f"python3 -m py_compile {shlex.quote(sf)}"
                )
                if "OK" not in syntax_result:
                    check_errors.append(f"구문오류: {sf}")
                    continue
                # Docker 내부 import 검증 (app/ 하위만)
                if sf.startswith("app/"):
                    module = sf.replace("/", ".").replace(".py", "")
                    import_cmd = f"docker exec aads-server python3 -m importlib {shlex.quote(module)}"
                    import_result = await tool_run_remote_command(project, import_cmd)
                    if "Error" in import_result and "exit=0" not in import_result:
                        err_line = [l for l in import_result.split("\n") if "Error" in l]
                        check_errors.append(f"import실패: {module} — {err_line[-1][:100] if err_line else '?'}")
            if check_errors:
                return (
                    f"[ERROR] 커밋 전 검수 실패 ({len(check_errors)}건):\n"
                    + "\n".join(f"  - {e}" for e in check_errors)
                    + "\n\n수정 후 다시 커밋하세요."
                )
            logger.info(f"git_commit_pre_check PASSED | project={project} files={len(staged_files)}")

    # shlex.quote()로 안전한 메시지 이스케이프
    safe_msg = shlex.quote(message[:200])
    return await tool_run_remote_command(project, f"git commit -m {safe_msg}")


async def tool_git_remote_push(project: str, branch: str = "") -> str:
    """원격 서버 git push (force push 차단)."""
    cmd = "git push"
    if branch:
        if not re.match(r'^[a-zA-Z0-9._/\-]+$', branch):
            return "[ERROR] 브랜치명에 허용되지 않는 문자"
        cmd += f" origin {branch}"
    return await tool_run_remote_command(project, cmd)


async def tool_git_remote_status(project: str) -> str:
    """원격 서버 git status."""
    return await tool_run_remote_command(project, "git status --short")


async def tool_git_remote_create_branch(project: str, branch_name: str) -> str:
    """원격 서버 새 브랜치 생성 및 체크아웃."""
    if not branch_name or not re.match(r'^[a-zA-Z0-9._/\-]+$', branch_name):
        return "[ERROR] 유효하지 않은 브랜치명"
    return await tool_run_remote_command(project, f"git checkout -b {branch_name}")


# ─── Pipeline C 도구 함수 ─────────────────────────────────────────────────────


async def tool_pipeline_c_start(project: str, instruction: str, max_cycles: int, dsn: str, chat_session_id: str = "", model: str = "") -> str:
    """파이프라인C 시작."""
    if not project:
        return "[ERROR] project 필수"
    if not instruction or len(instruction.strip()) < 10:
        return "[ERROR] instruction 필수 (최소 10자 이상의 구체적 지시 필요)"

    try:
        from app.services.pipeline_c import start_pipeline
        result = await start_pipeline(
            project=project.upper(),
            instruction=instruction,
            chat_session_id=chat_session_id or "",  # 빈 문자열이면 _post_to_chat 비활성
            max_cycles=min(max_cycles, 5),
            dsn=dsn,
            model=model or "",
        )
        return (
            f"[Pipeline C 시작]\n"
            f"Job ID: {result['job_id']}\n"
            f"프로젝트: {result['project']}\n"
            f"상태: {result['status']}\n\n"
            f"{result['message']}\n\n"
            f"진행 확인: pipeline_c_status(job_id=\"{result['job_id']}\")\n"
            f"작업이 완료되면 승인 요청이 표시됩니다."
        )
    except ValueError as e:
        return f"[ERROR] {e}"
    except Exception as e:
        logger.error(f"pipeline_c_start_error: {e}")
        return f"[ERROR] 파이프라인 시작 실패: {e}"


async def tool_pipeline_c_status(job_id: str) -> str:
    """파이프라인C 상태 조회."""
    from app.services.pipeline_c import get_pipeline_status, list_pipelines

    if not job_id:
        # 전체 목록
        jobs = await list_pipelines()
        if not jobs:
            return "실행 중인 파이프라인이 없습니다."
        lines = ["[활성 파이프라인 목록]"]
        for j in jobs:
            lines.append(
                f"  {j['job_id']} | {j['project']} | {j['phase']} | "
                f"{j['status']} | {j['elapsed_sec']}초 | {j['instruction'][:60]}"
            )
        return "\n".join(lines)

    result = await get_pipeline_status(job_id)
    if "error" in result:
        return f"[ERROR] {result['error']}"

    lines = [
        f"[Pipeline C 상태: {result['job_id']}]",
        f"프로젝트: {result['project']}",
        f"지시: {result.get('instruction', '')[:200]}",
        f"단계: {result['phase']}",
        f"검수 횟수: {result['cycle']}",
        f"상태: {result['status']}",
    ]
    if result.get("review_feedback"):
        lines.append(f"검수 결과: {result['review_feedback']}")
    if result.get("git_diff"):
        lines.append(f"\n[변경사항]\n{result['git_diff'][:1500]}")
    if result.get("logs"):
        lines.append("\n[최근 로그]")
        for log in result["logs"][-5:]:
            lines.append(f"  [{log.get('timestamp', '')[-8:]}] {log['phase']}: {log['message'][:150]}")

    if result["status"] == "awaiting_approval":
        lines.append(f"\n** CEO 승인 대기 중 **")
        lines.append(f"승인: pipeline_c_approve(job_id=\"{job_id}\", approved=true)")
        lines.append(f"거부: pipeline_c_approve(job_id=\"{job_id}\", approved=false, reason=\"사유\")")

    return "\n".join(lines)


async def tool_pipeline_c_approve(job_id: str, approved: bool, reason: str) -> str:
    """파이프라인C 승인/거부."""
    if not job_id:
        return "[ERROR] job_id 필수"

    from app.services.pipeline_c import approve_pipeline, reject_pipeline

    if approved:
        result = await approve_pipeline(job_id)
        if "error" in result:
            return f"[ERROR] {result['error']}"
        return (
            f"[Pipeline C 배포 완료]\n"
            f"Job: {job_id}\n"
            f"결과: {result.get('summary', 'OK')}\n"
            f"Health: {result.get('health', 'N/A')[:200]}\n"
            f"에러: {result.get('errors', '없음')[:200]}"
        )
    else:
        result = await reject_pipeline(job_id, reason)
        if "error" in result:
            return f"[ERROR] {result['error']}"
        return f"[Pipeline C 거부] {result.get('message', '변경사항 원복됨')}"


async def tool_pipeline_c_cancel(job_id: str) -> str:
    """파이프라인C 강제 취소."""
    if not job_id:
        return "[ERROR] job_id 필수"
    from app.services.pipeline_c import cancel_pipeline
    result = await cancel_pipeline(job_id)
    if "error" in result:
        return f"[ERROR] {result['error']}"
    return (
        f"[Pipeline C 취소 완료]\n"
        f"Job: {job_id}\n"
        f"Kill된 프로세스: {result.get('killed_pids', [])}\n"
        f"{result.get('message', '')}"
    )


async def tool_pipeline_c_retry(job_id: str) -> str:
    """에러/취소된 파이프라인C 재실행."""
    if not job_id:
        return "[ERROR] job_id 필수"
    from app.services.pipeline_c import retry_pipeline
    result = await retry_pipeline(job_id)
    if "error" in result:
        return f"[ERROR] {result['error']}"
    return (
        f"[Pipeline C 재실행]\n"
        f"원본 Job: {job_id}\n"
        f"새 Job: {result['job_id']}\n"
        f"프로젝트: {result['project']}\n"
        f"{result.get('message', '')}\n\n"
        f"진행 확인: pipeline_c_status(job_id=\"{result['job_id']}\")"
    )


# ─── Browser 도구 함수 (AADS-159) ──────────────────────────────────────────────

def _browser_domain_ok(url: str) -> Optional[str]:
    """도메인 화이트리스트 검사. 차단이면 에러 문자열, 통과이면 None."""
    try:
        hostname = (urlparse(url).hostname or "").lower()
    except Exception:
        return "[접근 차단] URL 파싱 실패"
    if hostname in _BROWSER_ALLOWED_DOMAINS:
        return None
    if hostname.endswith(_BROWSER_ALLOWED_SUFFIX):
        return None
    return f"[접근 차단] 허용되지 않은 도메인입니다: {hostname}"


async def _acquire_pw_context() -> Tuple[Any, Optional[str]]:
    """Playwright 컨텍스트 싱글턴 취득. 실패 시 (None, 에러메시지)."""
    global _pw_handle, _pw_browser, _pw_context, _pw_init_lock
    if _pw_init_lock is None:
        _pw_init_lock = asyncio.Lock()
    async with _pw_init_lock:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return None, "[브라우저 도구 사용 불가] playwright 패키지가 설치되지 않았습니다."
        try:
            need_init = (
                _pw_context is None
                or _pw_browser is None
                or not _pw_browser.is_connected()
            )
            if need_init:
                if _pw_handle is not None:
                    try:
                        await _pw_handle.stop()
                    except Exception:
                        pass
                _pw_handle = await async_playwright().start()
                _pw_browser = await _pw_handle.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--memory-pressure-off",
                    ],
                )
                _pw_context = await _pw_browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    java_script_enabled=True,
                )
            return _pw_context, None
        except Exception as e:
            return None, f"[브라우저 도구 사용 불가] 초기화 실패: {e}"


async def _current_page(ctx: Any) -> Any:
    """현재(최신) 페이지 반환. 없으면 새 페이지 생성."""
    pages = ctx.pages
    return pages[-1] if pages else await ctx.new_page()


def _snapshot_to_text(node: Dict, depth: int = 0) -> str:
    """접근성 트리 노드를 들여쓰기 텍스트로 변환."""
    indent = "  " * depth
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")
    line = f"{indent}[{role}]{(' ' + name) if name else ''}{(' = ' + str(value)) if value else ''}"
    child_lines = "\n".join(
        _snapshot_to_text(c, depth + 1) for c in node.get("children", [])
    )
    return line + ("\n" + child_lines if child_lines else "")


async def _ensure_aads_auth(page: Any) -> None:
    """AADS 대시보드 인증 토큰 자동 주입 (내부 서비스용)."""
    try:
        from app.auth import create_token
        token = create_token(user_id="browser-agent", email="ceo@aads.dev")
        await page.evaluate(f"() => localStorage.setItem('aads_token', '{token}')")
    except Exception as e:
        logger.debug(f"browser auth inject failed: {e}")


async def _do_aads_login(page: Any) -> None:
    """AADS 대시보드 로그인 페이지에서 자동 로그인 수행."""
    import os
    email = os.getenv("AADS_ADMIN_EMAIL", "admin@aads.dev")
    password = os.getenv("AADS_ADMIN_PASSWORD", "")
    if not password:
        # 비밀번호 없으면 토큰 직접 주입 시도
        await _ensure_aads_auth(page)
        return

    # 이메일 입력 (첫 번째 input 필드)
    email_input = page.locator("input").first
    await email_input.clear(timeout=5000)
    await email_input.fill(email, timeout=5000)
    # 비밀번호 입력
    pw_input = page.locator("input[type='password']").first
    await pw_input.fill(password, timeout=5000)
    # 로그인 버튼 클릭
    login_btn = page.locator("button:has-text('로그인')").first
    await login_btn.click(timeout=5000)
    # 로그인 후 페이지 전환 대기
    await page.wait_for_timeout(3000)


async def tool_browser_navigate(url: str) -> str:
    """브라우저로 URL 이동 (도메인 화이트리스트 검사 포함)."""
    blocked = _browser_domain_ok(url)
    if blocked:
        return blocked
    ctx, err = await _acquire_pw_context()
    if err:
        return err
    try:
        pages = ctx.pages
        if len(pages) >= _BROWSER_MAX_TABS:
            page = pages[-1]  # 마지막 탭 재사용
        else:
            page = await ctx.new_page()

        await page.goto(url, timeout=_BROWSER_TIMEOUT_MS, wait_until="domcontentloaded")

        # AADS 대시보드 로그인 리다이렉트 감지 → 자동 로그인
        if "/login" in page.url and "/login" not in url and "newtalk.kr" in url:
            try:
                await _do_aads_login(page)
                await page.goto(url, timeout=_BROWSER_TIMEOUT_MS, wait_until="domcontentloaded")
            except Exception as login_err:
                logger.warning(f"browser auto-login failed: {login_err}")

        title = await page.title()
        return f"[탐색 완료]\n제목: {title}\nURL: {page.url}"
    except Exception as e:
        return f"[ERROR] 브라우저 탐색 실패: {e}"


async def tool_browser_snapshot() -> str:
    """현재 페이지의 UI 구조를 텍스트로 추출 (LLM 최적)."""
    ctx, err = await _acquire_pw_context()
    if err:
        return err
    try:
        page = await _current_page(ctx)
        url = page.url
        title = await page.title()

        # Playwright 1.47+ : page.accessibility 제거됨
        # aria snapshot 사용 (1.49+), 실패 시 DOM 텍스트 추출 폴백
        snap_text = ""
        try:
            snap_text = await page.locator("body").aria_snapshot()
        except Exception:
            pass

        if not snap_text:
            # 폴백: 주요 UI 요소 텍스트 추출
            elements = await page.evaluate("""() => {
                const items = [];
                const els = document.querySelectorAll(
                    'button, a, input, select, textarea, h1, h2, h3, h4, [role], label, nav, header, footer, main, aside'
                );
                for (const el of els) {
                    const tag = el.tagName.toLowerCase();
                    const role = el.getAttribute('role') || '';
                    const text = (el.textContent || '').trim().substring(0, 100);
                    const placeholder = el.getAttribute('placeholder') || '';
                    const type = el.getAttribute('type') || '';
                    const href = el.getAttribute('href') || '';
                    if (text || placeholder) {
                        items.push({tag, role, text, placeholder, type, href});
                    }
                    if (items.length >= 200) break;
                }
                return items;
            }""")
            lines = [f"[UI 요소 추출 — {url}]", f"제목: {title}", ""]
            for el in elements:
                parts = [f"<{el['tag']}>"]
                if el.get('role'):
                    parts.append(f"role={el['role']}")
                if el.get('type'):
                    parts.append(f"type={el['type']}")
                if el.get('text'):
                    parts.append(f"'{el['text'][:80]}'")
                if el.get('placeholder'):
                    parts.append(f"placeholder='{el['placeholder']}'")
                if el.get('href'):
                    parts.append(f"href={el['href'][:80]}")
                lines.append("  " + " ".join(parts))
            snap_text = "\n".join(lines)

        if len(snap_text) > 20_000:
            snap_text = snap_text[:20_000] + "\n...(20KB 초과, 잘림)"
        return snap_text if snap_text.startswith("[") else f"[ARIA 스냅샷 — {url}]\n{snap_text}"
    except Exception as e:
        return f"[ERROR] 스냅샷 실패: {e}"


async def tool_browser_screenshot() -> str:
    """현재 페이지 PNG 스크린샷 촬영 (base64 반환)."""
    ctx, err = await _acquire_pw_context()
    if err:
        return err
    try:
        page = await _current_page(ctx)
        data = await page.screenshot(full_page=False, timeout=_BROWSER_TIMEOUT_MS)
        b64 = base64.b64encode(data).decode("ascii")
        return f"[스크린샷 PNG — base64]\nURL: {page.url}\nDATA:{b64}"
    except Exception as e:
        return f"[ERROR] 스크린샷 실패: {e}"


async def tool_capture_screenshot(url: str, full_page: bool = False) -> str:
    """URL 스크린샷을 캡처하여 이미지 URL 반환 (채팅에 인라인 표시용)."""
    if not url:
        return "[ERROR] url 필수"
    blocked = _browser_domain_ok(url)
    if blocked:
        return blocked
    ctx, err = await _acquire_pw_context()
    if err:
        return err
    try:
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(1)  # 렌더링 대기
            data = await page.screenshot(full_page=full_page, timeout=_BROWSER_TIMEOUT_MS)
        finally:
            await page.close()
        import base64 as b64mod
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{ts}_{uuid.uuid4().hex[:6]}.png"
        # 호스트에 SSH로 저장 (컨테이너→호스트, 볼륨 마운트 없어도 동작)
        b64_data = b64mod.b64encode(data).decode("ascii")
        save_cmd = (
            f"mkdir -p /var/www/aads_exports/screenshots && "
            f"echo '{b64_data}' | base64 -d > /var/www/aads_exports/screenshots/{filename}"
        )
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            "root@host.docker.internal", save_cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return f"[ERROR] 호스트에 스크린샷 저장 실패 (exit={proc.returncode})"
        image_url = f"https://aads.newtalk.kr/screenshots/{filename}"
        return f"스크린샷 저장 완료.\n\n![{url} 스크린샷]({image_url})"
    except Exception as e:
        return f"[ERROR] 스크린샷 캡처 실패: {e}"


async def tool_browser_click(selector: str) -> str:
    """CSS selector로 요소 클릭."""
    ctx, err = await _acquire_pw_context()
    if err:
        return err
    try:
        page = await _current_page(ctx)
        await page.click(selector, timeout=30_000)
        return f"[클릭 완료] selector={selector}"
    except Exception as e:
        return f"[ERROR] 클릭 실패 ({selector}): {e}"


async def tool_browser_fill(selector: str, value: str) -> str:
    """입력 필드에 텍스트 채우기."""
    ctx, err = await _acquire_pw_context()
    if err:
        return err
    try:
        page = await _current_page(ctx)
        await page.fill(selector, value, timeout=30_000)
        return f"[입력 완료] selector={selector}, value='{value[:50]}'"
    except Exception as e:
        return f"[ERROR] 입력 실패 ({selector}): {e}"


async def tool_browser_tab_list() -> str:
    """열린 탭 목록 반환."""
    ctx, err = await _acquire_pw_context()
    if err:
        return err
    try:
        pages = ctx.pages
        if not pages:
            return f"(열린 탭 없음 — 최대 {_BROWSER_MAX_TABS}개)"
        lines = [f"[열린 탭 {len(pages)}/{_BROWSER_MAX_TABS}]"]
        for i, p in enumerate(pages):
            title = await p.title()
            lines.append(f"  [{i}] {title} — {p.url}")
        return "\n".join(lines)
    except Exception as e:
        return f"[ERROR] 탭 목록 조회 실패: {e}"


# ─── 대화 히스토리 검색 ────────────────────────────────────────────────────────

async def tool_search_chat_history(
    query: str,
    dsn: str,
    mode: str = "keyword",
    session_id: str = "",
    date_from: str = "",
    date_to: str = "",
    role: str = "all",
    limit: int = 10,
) -> str:
    """과거 대화 검색 — keyword(FTS+LIKE) / semantic(임베딩 유사도)."""
    if not query or not query.strip():
        return "[ERROR] query 필수"
    query = query.strip()
    limit = max(1, min(30, limit))

    # ── Semantic 모드 ──
    if mode == "semantic":
        try:
            from app.services.chat_embedding_service import search_semantic
            from app.core.db_pool import get_pool
            pool = get_pool()
            results = await search_semantic(pool, query, session_id or None, limit)
            if not results:
                return f"시맨틱 검색 '{query}' — 결과 없음 (임베딩 미생성 메시지가 많으면 backfill 필요)"
            lines = [f"[시맨틱 검색] '{query}' — {len(results)}건"]
            for r in results:
                lines.append(
                    f"\n📅 {r['created_at'][:16]} | {r['role']} | 유사도 {r['similarity']}"
                    f" | 세션: {r['session_name']}"
                    f"\n{r['content']}"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"[ERROR] 시맨틱 검색 실패: {e}"

    # ── Keyword 모드 (FTS → LIKE 폴백) ──
    try:
        conn = await asyncpg.connect(dsn, timeout=10)
    except Exception as e:
        return f"[ERROR] DB 연결 실패: {e}"

    try:
        # 필터 조건 구성
        conditions = []
        params: list = []
        param_idx = 1

        # FTS 조건
        conditions.append(f"to_tsvector('simple', m.content) @@ plainto_tsquery('simple', ${param_idx})")
        params.append(query)
        param_idx += 1

        if session_id:
            conditions.append(f"m.session_id = ${param_idx}::uuid")
            params.append(session_id)
            param_idx += 1
        if role and role != "all":
            conditions.append(f"m.role = ${param_idx}")
            params.append(role)
            param_idx += 1
        if date_from:
            conditions.append(f"m.created_at >= ${param_idx}::date")
            params.append(date_from)
            param_idx += 1
        if date_to:
            conditions.append(f"m.created_at < (${param_idx}::date + interval '1 day')")
            params.append(date_to)
            param_idx += 1

        where_clause = " AND ".join(conditions)
        sql = f"""
            SELECT m.id, m.role, m.content, m.created_at, s.title AS session_name
            FROM chat_messages m
            JOIN chat_sessions s ON s.id = m.session_id
            WHERE {where_clause}
            ORDER BY m.created_at DESC
            LIMIT {limit}
        """
        rows = await conn.fetch(sql, *params)

        # FTS 결과 0건 → LIKE 폴백
        if not rows:
            conditions[0] = f"m.content ILIKE ${1}"
            params[0] = f"%{query}%"
            where_clause = " AND ".join(conditions)
            sql = f"""
                SELECT m.id, m.role, m.content, m.created_at, s.title AS session_name
                FROM chat_messages m
                JOIN chat_sessions s ON s.id = m.session_id
                WHERE {where_clause}
                ORDER BY m.created_at DESC
                LIMIT {limit}
            """
            rows = await conn.fetch(sql, *params)
            search_type = "LIKE"
        else:
            search_type = "FTS"

        if not rows:
            return f"키워드 검색 '{query}' — 결과 없음"

        lines = [f"[{search_type} 검색] '{query}' — {len(rows)}건"]
        for r in rows:
            ts = r["created_at"].strftime("%Y-%m-%d %H:%M")
            content_preview = r["content"][:500].replace("\n", " ")
            lines.append(
                f"\n📅 {ts} | {r['role']} | 세션: {r['session_name']}"
                f"\n{content_preview}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"[ERROR] 검색 실패: {e}"
    finally:
        await conn.close()


# ─── F12: Timeline Memory ─────────────────────────────────────────────────────

async def tool_query_timeline(
    project: str,
    period: str = "7d",
    category: str = "",
    limit: int = 20,
) -> str:
    """프로젝트별 시간순 이력 조회 (memory_facts 기반)."""
    if not project:
        return "[ERROR] project 필수"

    project = project.upper().strip()

    try:
        from datetime import timedelta
        from app.core.db_pool import get_pool
        pool = get_pool()

        # 기간 파싱
        interval_td = timedelta(days=7)
        date_filter = ""
        date_start = None
        date_end = None
        if "~" in period:
            # 날짜 범위: 2026-03-01~2026-03-13
            parts = period.split("~")
            date_start = parts[0].strip()
            date_end = parts[1].strip()
            date_filter = "range"
        elif period.endswith("d"):
            days = int(period[:-1])
            interval_td = timedelta(days=days)

        async with pool.acquire() as conn:
            if date_filter:
                # Build parameterized query for date range
                params_list = [project, date_start, date_end]
                param_idx = 4  # next available $N
                cat_clause = ""
                if category:
                    cat_clause = f"AND category = ${param_idx}"
                    params_list.append(category)
                    param_idx += 1
                params_list.append(limit)
                limit_param = f"${param_idx}"
                sql = f"""
                    SELECT category, subject, detail, created_at, confidence
                    FROM memory_facts
                    WHERE project = $1
                      AND superseded_by IS NULL
                      AND created_at >= $2::date AND created_at < $3::date + interval '1 day'
                      {cat_clause}
                    ORDER BY created_at ASC
                    LIMIT {limit_param}
                """
                rows = await conn.fetch(sql, *params_list)
            else:
                if category:
                    rows = await conn.fetch(
                        """
                        SELECT category, subject, detail, created_at, confidence
                        FROM memory_facts
                        WHERE project = $1
                          AND superseded_by IS NULL
                          AND category = $2
                          AND created_at > NOW() - $3
                        ORDER BY created_at ASC
                        LIMIT $4
                        """,
                        project, category, interval_td, limit,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT category, subject, detail, created_at, confidence
                        FROM memory_facts
                        WHERE project = $1
                          AND superseded_by IS NULL
                          AND created_at > NOW() - $2
                        ORDER BY created_at ASC
                        LIMIT $3
                        """,
                        project, interval_td, limit,
                    )

            if not rows:
                return f"[{project}] 기간 '{period}' 내 타임라인 이벤트 없음"

            lines = [f"📅 [{project}] 타임라인 ({period}) — {len(rows)}건"]
            for r in rows:
                ts = r["created_at"].strftime("%m/%d %H:%M") if r["created_at"] else ""
                conf = f"{r['confidence']:.1f}" if r["confidence"] else ""
                lines.append(f"  {ts} | [{r['category']}] {r['subject']} (신뢰도:{conf})")
                if r["detail"]:
                    lines.append(f"         {r['detail'][:150]}")

            return "\n".join(lines)

    except Exception as e:
        return f"[ERROR] 타임라인 조회 실패: {e}"


# ─── F5: Tool Result Recall ───────────────────────────────────────────────────

async def tool_recall_tool_result(
    tool_name: str = "",
    keyword: str = "",
    limit: int = 5,
) -> str:
    """과거 도구 실행 결과를 검색."""
    try:
        from app.services.tool_archive import recall_tool_result
        results = await recall_tool_result(
            tool_name=tool_name or None,
            keyword=keyword or None,
            limit=limit,
        )

        if not results:
            filters = []
            if tool_name:
                filters.append(f"도구={tool_name}")
            if keyword:
                filters.append(f"키워드={keyword}")
            return f"도구 결과 검색 결과 없음 ({', '.join(filters) if filters else '전체'})"

        lines = [f"🔧 도구 결과 검색 — {len(results)}건"]
        for r in results:
            ts = r.get("created_at", "")[:16] if r.get("created_at") else ""
            lines.append(f"\n  [{ts}] {r['tool_name']}")
            if r.get("input_params"):
                params_str = str(r["input_params"])[:100]
                lines.append(f"    입력: {params_str}")
            lines.append(f"    결과: {r.get('output_preview', '')[:300]}")

        return "\n".join(lines)

    except Exception as e:
        return f"[ERROR] 도구 결과 검색 실패: {e}"


async def tool_query_decision_graph(
    subject: str = "",
    fact_id: str = "",
    max_depth: int = 3,
) -> str:
    """C4: 결정 의존관계 그래프 탐색 — related_facts를 최대 3단계 재귀 추적."""
    if not subject and not fact_id:
        return "[ERROR] subject 또는 fact_id 중 하나는 필수"

    max_depth = min(max(max_depth, 1), 3)

    try:
        from app.core.db_pool import get_pool
        pool = get_pool()

        async with pool.acquire() as conn:
            # 시작 사실 찾기
            if fact_id:
                try:
                    root_facts = await conn.fetch(
                        """
                        SELECT id, project, category, subject, detail, confidence,
                               related_facts, created_at
                        FROM memory_facts
                        WHERE id = $1 AND superseded_by IS NULL
                        """,
                        uuid.UUID(fact_id),
                    )
                except (ValueError, Exception):
                    return f"[ERROR] 유효하지 않은 fact_id: {fact_id}"
            else:
                root_facts = await conn.fetch(
                    """
                    SELECT id, project, category, subject, detail, confidence,
                           related_facts, created_at
                    FROM memory_facts
                    WHERE subject ILIKE $1 AND superseded_by IS NULL
                    ORDER BY confidence DESC, created_at DESC
                    LIMIT 5
                    """,
                    f"%{subject}%",
                )

            if not root_facts:
                return f"관련 사실을 찾을 수 없습니다: {subject or fact_id}"

            # BFS로 의존관계 트리 구성
            lines = ["의존관계 그래프:"]
            visited = set()

            async def _traverse(fact_ids: list, depth: int, prefix: str):
                if depth > max_depth:
                    return
                for fid in fact_ids:
                    if fid in visited:
                        continue
                    visited.add(fid)

                    row = await conn.fetchrow(
                        """
                        SELECT id, project, category, subject, detail, confidence,
                               related_facts, created_at
                        FROM memory_facts
                        WHERE id = $1
                        """,
                        fid,
                    )
                    if not row:
                        continue

                    ts = row["created_at"].strftime("%m/%d") if row["created_at"] else ""
                    proj = row["project"] or ""
                    indent = "  " * depth
                    marker = "|-- " if depth > 0 else ""
                    lines.append(
                        f"{indent}{marker}[{proj}:{row['category']}] {row['subject']} "
                        f"(conf={float(row['confidence'] or 0):.2f}, {ts})"
                    )
                    if row["detail"]:
                        lines.append(f"{indent}{'    ' if depth > 0 else ''}  -> {row['detail'][:120]}")

                    # 재귀 탐색
                    children = row["related_facts"]
                    if children and depth < max_depth:
                        await _traverse(children, depth + 1, prefix + "  ")

            # 루트 사실들에서 시작
            for root in root_facts:
                visited.add(root["id"])
                ts = root["created_at"].strftime("%m/%d") if root["created_at"] else ""
                proj = root["project"] or ""
                lines.append(
                    f"[{proj}:{root['category']}] {root['subject']} "
                    f"(conf={float(root['confidence'] or 0):.2f}, {ts})"
                )
                if root["detail"]:
                    lines.append(f"  -> {root['detail'][:120]}")

                children = root["related_facts"]
                if children:
                    await _traverse(children, 1, "")

                # 역방향 탐색: 이 사실을 참조하는 다른 사실
                reverse_refs = await conn.fetch(
                    """
                    SELECT id FROM memory_facts
                    WHERE $1 = ANY(related_facts) AND superseded_by IS NULL
                    LIMIT 10
                    """,
                    root["id"],
                )
                if reverse_refs:
                    reverse_ids = [r["id"] for r in reverse_refs if r["id"] not in visited]
                    if reverse_ids:
                        lines.append("  [역참조 (이 사실을 참조하는 노드):]")
                        await _traverse(reverse_ids, 1, "")

            if len(lines) <= 1:
                return f"의존관계 없음: {subject or fact_id}"

            lines.append(f"\n탐색 노드: {len(visited)}개, 최대 깊이: {max_depth}")
            return "\n".join(lines)

    except Exception as e:
        return f"[ERROR] 의존관계 그래프 탐색 실패: {e}"


# ─── 디스패처 ──────────────────────────────────────────────────────────────────

async def execute_tool(name: str, params: Dict[str, Any], dsn: str, chat_session_id: str = "") -> str:
    """도구 이름과 파라미터로 실제 실행."""
    if name == "read_file":
        return await tool_read_file(params.get("path", ""))
    elif name == "read_github":
        return await tool_read_github(
            params.get("path", ""),
            params.get("repo", "aads-docs"),
            params.get("branch", "main"),
        )
    elif name == "search_logs":
        return await tool_search_logs(
            params.get("source", ""),
            params.get("keyword"),
        )
    elif name == "query_db":
        return await tool_query_db(params.get("sql", ""), dsn)
    elif name == "fetch_url":
        return await tool_fetch_url(params.get("url", ""))
    # ── Browser 도구 (AADS-159) ─────────────────────────────────────────────
    elif name == "browser_navigate":
        return await tool_browser_navigate(params.get("url", ""))
    elif name == "browser_snapshot":
        return await tool_browser_snapshot()
    elif name == "browser_screenshot":
        return await tool_browser_screenshot()
    elif name == "browser_click":
        return await tool_browser_click(params.get("selector", ""))
    elif name == "browser_fill":
        return await tool_browser_fill(params.get("selector", ""), params.get("value", ""))
    elif name == "browser_tab_list":
        return await tool_browser_tab_list()
    # ── SSH 원격 접근 도구 (AADS-165) ────────────────────────────────────────
    elif name == "list_remote_dir":
        return await tool_list_remote_dir(
            params.get("project", ""),
            params.get("path", ""),
            params.get("keyword", ""),
            params.get("max_depth", 3),
        )
    elif name == "read_remote_file":
        return await tool_read_remote_file(
            params.get("project", ""),
            params.get("file_path", ""),
            offset=int(params.get("offset", 1) or 1),
            limit=int(params.get("limit", 2000) or 2000),
        )
    # ── Pipeline C 도구 ────────────────────────────────────────────────────
    elif name == "pipeline_c_start":
        # 명시적 chat_session_id 우선, 없으면 ContextVar 폴백
        from app.services.tool_executor import current_chat_session_id
        _sid = chat_session_id or current_chat_session_id.get("")
        logger.info(f"[DIAG] execute_tool(pipeline_c_start): session_id='{_sid}' (explicit={chat_session_id}, cv={current_chat_session_id.get('')})")
        return await tool_pipeline_c_start(
            params.get("project", ""),
            params.get("instruction", ""),
            params.get("max_cycles", 3),
            dsn,
            chat_session_id=_sid,
            model=params.get("model", ""),
        )
    elif name == "pipeline_c_status":
        return await tool_pipeline_c_status(params.get("job_id", ""))
    elif name == "pipeline_c_approve":
        return await tool_pipeline_c_approve(
            params.get("job_id", ""),
            params.get("approved", True),
            params.get("reason", ""),
        )
    elif name == "pipeline_c_cancel":
        return await tool_pipeline_c_cancel(params.get("job_id", ""))
    elif name == "pipeline_c_retry":
        return await tool_pipeline_c_retry(params.get("job_id", ""))
    # ── 대화 히스토리 검색 ─────────────────────────────────────────────
    elif name == "search_chat_history":
        return await tool_search_chat_history(
            query=params.get("query", ""),
            dsn=dsn,
            mode=params.get("mode", "keyword"),
            session_id=params.get("session_id", ""),
            date_from=params.get("date_from", ""),
            date_to=params.get("date_to", ""),
            role=params.get("role", "all"),
            limit=params.get("limit", 10),
        )
    # ── F12: Timeline Memory ──────────────────────────────────────────
    elif name == "query_timeline":
        return await tool_query_timeline(
            project=params.get("project", ""),
            period=params.get("period", "7d"),
            category=params.get("category", ""),
            limit=min(int(params.get("limit", 20) or 20), 50),
        )
    # ── F5: Tool Result Recall ────────────────────────────────────────
    elif name == "recall_tool_result":
        return await tool_recall_tool_result(
            tool_name=params.get("tool_name", ""),
            keyword=params.get("keyword", ""),
            limit=min(int(params.get("limit", 5) or 5), 20),
        )
    # ── C4: Decision Dependency Graph ─────────────────────────────────
    elif name == "query_decision_graph":
        return await tool_query_decision_graph(
            subject=params.get("subject", ""),
            fact_id=params.get("fact_id", ""),
            max_depth=min(int(params.get("max_depth", 3) or 3), 3),
        )
    else:
        return f"[ERROR] 알 수 없는 도구: {name}"
