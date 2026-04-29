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
import json
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
    # ── 이미지/팩트체크/검색/샌드박스/알림 도구 ──────────────────────────────
    {
        "name": "generate_image",
        "description": "이미지 생성 (Imagen 4.0 / GPT-Image-1). 프롬프트로 이미지 생성 후 URL 반환.\n예: generate_image(prompt='한국 전통 한옥 마을 일러스트', size='1024x1024')",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "생성할 이미지 설명 프롬프트",
                },
                "size": {
                    "type": "string",
                    "description": "이미지 크기 (기본: 1024x1024)",
                    "default": "1024x1024",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "fact_check",
        "description": "팩트체크 (DB + 웹 교차검증). 주장의 사실 여부를 검증하고 근거 반환.\n예: fact_check(claim='삼성전자 2025년 매출이 300조를 넘었다')",
        "input_schema": {
            "type": "object",
            "properties": {
                "claim": {
                    "type": "string",
                    "description": "검증할 주장 또는 사실",
                },
            },
            "required": ["claim"],
        },
    },
    {
        "name": "fact_check_multiple",
        "description": "다건 팩트체크. 여러 주장을 한 번에 교차검증.\n예: fact_check_multiple(claims=['주장1', '주장2'])",
        "input_schema": {
            "type": "object",
            "properties": {
                "claims": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "검증할 주장 목록",
                },
            },
            "required": ["claims"],
        },
    },
    {
        "name": "gemini_grounding_search",
        "description": "Gemini 실시간 팩트 검색 (Google Search grounding). 질문에 대해 근거 있는 답변 + 출처 반환.\n예: gemini_grounding_search(query='현재 코스피 지수는?')",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "검색할 질문",
                },
                "context": {
                    "type": "string",
                    "description": "추가 컨텍스트 (선택)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "execute_sandbox",
        "description": "Docker 격리 환경에서 코드 실행. Python/JavaScript/Bash 지원. 타임아웃 최대 60초.\n예: execute_sandbox(code='print(2+2)', language='python')",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "실행할 코드",
                },
                "language": {
                    "type": "string",
                    "description": "언어 (python, javascript, bash)",
                    "enum": ["python", "javascript", "bash"],
                    "default": "python",
                },
                "timeout": {
                    "type": "integer",
                    "description": "타임아웃 초 (기본: 30, 최대: 60)",
                    "default": 30,
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "send_telegram",
        "description": "CEO 텔레그램 알림 전송. 즉시 메시지 발송.\n예: send_telegram(message='배포 완료: AADS v2.1')",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "전송할 메시지 내용",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "search_kakao",
        "description": "카카오 검색 API. 웹/블로그/카페 검색.\n예: search_kakao(query='FastAPI 배포 가이드', search_type='web')",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "검색어",
                },
                "search_type": {
                    "type": "string",
                    "description": "검색 유형 (web, blog, cafe)",
                    "enum": ["web", "blog", "cafe"],
                    "default": "web",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_naver",
        "description": "네이버 검색 API. 웹/블로그/뉴스/지식iN/백과/이미지/쇼핑 검색.\n예: search_naver(query='삼성전자 실적', search_type='news')",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "검색어",
                },
                "search_type": {
                    "type": "string",
                    "description": "검색 유형 (webkr, blog, news, kin, encyc, image, shop)",
                    "enum": ["webkr", "blog", "news", "kin", "encyc", "image", "shop"],
                    "default": "webkr",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_naver_multi",
        "description": "네이버 다중 검색. 여러 검색 유형을 동시에 실행하여 종합 결과 반환.\n예: search_naver_multi(query='코스피 전망', types=['news', 'blog', 'webkr'])",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "검색어",
                },
                "types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "검색 유형 목록 (기본: ['webkr', 'news', 'blog'])",
                    "default": ["webkr", "news", "blog"],
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "visual_qa_test",
        "description": "UI 비주얼 QA 테스트. URL의 페이지를 Playwright로 캡처 후 시각적 검증.\n예: visual_qa_test(url='https://aads.newtalk.kr/', checks=['로그인 버튼 존재', '헤더 렌더링'])",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "테스트할 URL",
                },
                "checks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "확인할 항목 목록 (선택)",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "evaluate_alerts",
        "description": "알림 규칙 평가 + 발송. 등록된 모든 알림 규칙을 평가하고 조건 충족 시 발송.\n예: evaluate_alerts()",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "send_alert_message",
        "description": "커스텀 알림 메시지 발송. 레벨별 텔레그램 알림.\n예: send_alert_message(message='서버 디스크 90% 초과', level='critical')",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "알림 메시지 내용",
                },
                "level": {
                    "type": "string",
                    "description": "알림 레벨 (info, warning, critical)",
                    "enum": ["info", "warning", "critical"],
                    "default": "info",
                },
            },
            "required": ["message"],
        },
    },
    # ── Pipeline Runner 도구 (호스트 독립 실행 — 권장) ─────────────────────
    {
        "name": "pipeline_runner_submit",
        "description": "코드 수정/배포 작업을 Pipeline Runner로 제출.\n각 서버의 Runner가 독립적으로 Claude Code를 실행. 서버 재시작 무영향.\n서버매핑: AADS→68서버, KIS/GO100→211서버, SF/NTV2→114서버.\n예: pipeline_runner_submit(project='KIS', instruction='order_executor.py null check 추가')",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "대상 프로젝트",
                    "enum": ["KIS", "GO100", "SF", "NTV2", "AADS"],
                },
                "instruction": {
                    "type": "string",
                    "description": "Claude Code에 보낼 작업 지시 (구체적으로)",
                },
                "max_cycles": {
                    "type": "integer",
                    "description": "최대 검수 반복 (기본: 3)",
                    "default": 3,
                },
                "session_id": {
                    "type": "string",
                    "description": "작업 완료보고를 받을 세션 ID. 생략 시 현재 세션 자동 감지.",
                },
                "size": {
                    "type": "string",
                    "description": "작업 규모 — 모델 자동 선택 (XS/S→Haiku, M/L→Sonnet, XL→Opus). worker_model 지정 시 무시됨.",
                    "enum": ["XS", "S", "M", "L", "XL"],
                    "default": "M",
                },
                "worker_model": {
                    "type": "string",
                    "description": "모델 직접 지정 (지정 시 size 무시).\n\nClaude Runner (기존):\n  claude-opus-4-6, claude-sonnet-4-6, claude-haiku-4-5\n\nLiteLLM Runner (신규, litellm: 접두사 — MCP 24개 도구 사용 가능):\n  litellm:gemini-2.5-flash ($0.15/task, SWE-bench 78.8%)\n  litellm:deepseek-chat ($0.02/task, SWE-bench 70%)\n  litellm:qwen3-235b ($0.03/task, 한국어 강점)",
                },
                "parallel_group": {
                    "type": "string",
                    "description": "병렬 실행 그룹명. 같은 그룹 내 작업은 프로젝트 Lock 없이 동시 실행.",
                },
                "depends_on": {
                    "type": "string",
                    "description": "의존 작업 job_id. 해당 작업 완료(done) 후에만 실행.",
                },
            },
            "required": ["project", "instruction"],
        },
    },
    # ── Pipeline Runner 배치 도구 (AADS-211: 병렬 오케스트레이션) ──────────
    {
        "name": "pipeline_runner_submit_batch",
        "description": "여러 작업을 한 번에 제출하여 병렬 실행.\n같은 배치 내 작업은 자동 parallel_group 할당. depends_on_key로 순서 제어 가능.\n예: pipeline_runner_submit_batch(project='AADS', jobs=[{key:'A', instruction:'...', worker_model:'claude-opus-4-6'}, {key:'B', instruction:'...', depends_on_key:'A'}])",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "대상 프로젝트",
                    "enum": ["KIS", "GO100", "SF", "NTV2", "AADS"],
                },
                "jobs": {
                    "type": "array",
                    "description": "작업 목록 (1~20개)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "배치 내 식별자 (예: 'A', 'B')"},
                            "instruction": {"type": "string", "description": "작업 지시"},
                            "size": {"type": "string", "default": "M"},
                            "worker_model": {"type": "string", "description": "모델 직접 지정"},
                            "depends_on_key": {"type": "string", "description": "선행 작업의 key (완료 후 실행)"},
                        },
                        "required": ["key", "instruction"],
                    },
                },
                "parallel_group": {
                    "type": "string",
                    "description": "병렬 그룹명 (미지정 시 batch-{uuid} 자동 생성)",
                },
                "max_cycles": {
                    "type": "integer",
                    "description": "최대 검수 반복 (기본: 3)",
                    "default": 3,
                },
                "session_id": {
                    "type": "string",
                    "description": "작업 완료보고를 받을 세션 ID. 생략 시 현재 세션.",
                },
            },
            "required": ["project", "jobs"],
        },
    },
    {
        "name": "pipeline_runner_status",
        "description": "Pipeline Runner 작업 상태 조회. error_detail(에러분류: timeout/claude_code_crash/git_conflict/build_fail/disk_full/rate_limit/process_died 등) 포함. status: queued/running/awaiting_approval/done/error",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "작업 ID (없으면 전체 목록)"},
                "status": {"type": "string", "description": "필터: queued, running, awaiting_approval, done, error"},
            },
        },
    },
    {
        "name": "pipeline_runner_approve",
        "description": "Pipeline Runner 작업 승인 또는 거부. awaiting_approval 상태에서만 가능.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "작업 ID"},
                "action": {"type": "string", "enum": ["approve", "reject"], "description": "승인/거부"},
                "feedback": {"type": "string", "description": "피드백 (거부 시 사유)"},
            },
            "required": ["job_id", "action"],
        },
    },
    # Pipeline Runner(구 Pipeline C) 도구 완전 제거 (2026-03-16) — pipeline_runner_submit으로 대체
    # execute_tool 디스패처에 핸들러는 남아있어 기존 호출 시 에러 안내 반환
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
    # ── AI-to-AI: 다관점 토론 ────────────────────────────────────────────
    {
        "name": "run_debate",
        "description": (
            "전략적 의사결정이 필요한 질문에 대해 기술/비즈니스/리스크 3관점으로 병렬 분석 후 종합.\n"
            "CEO가 '토론해봐', '다관점 분석', '장단점 비교', '어떻게 해야 할까' 등을 요청할 때 사용.\n"
            "소요: 10~30초, 비용: ~$1~2/토론 (Sonnet x3~4)\n"
            "예: run_debate(question='KIS에 새 전략을 추가해야 할까?')"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "토론할 질문 (전략, 의사결정, 설계 관련)",
                },
                "context": {
                    "type": "string",
                    "description": "추가 배경 정보 (선택)",
                },
                "perspectives": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "관점 이름 (예: '기술', '비즈니스')"},
                            "role": {"type": "string", "description": "에이전트 역할"},
                            "system": {"type": "string", "description": "관점별 시스템 프롬프트"},
                        },
                        "required": ["name", "system"],
                    },
                    "description": "커스텀 관점 목록 (선택, 기본: 기술/비즈니스/리스크 3관점)",
                },
            },
            "required": ["question"],
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
                    "description": "작업 ID (Pipeline B: directive ID, Pipeline Runner: runner-* ID)",
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
            "활성 작업 강제 종료. Pipeline Runner는 원격 Claude 프로세스도 정리.\n"
            "예: terminate_task(task_id='runner-abc12345')"
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
from app.core.project_config import PROJECT_MAP as _PROJECT_MAP_FULL, REMOTE_PROJECTS, get_workdir
_PROJECT_SERVER_MAP: Dict[str, Dict[str, str]] = {
    k: v for k, v in _PROJECT_MAP_FULL.items() if k in REMOTE_PROJECTS
}

# SSH 보안 규칙 (하드코딩, LLM 우회 불가)
# 화이트리스트: 영숫자, 유니코드(한글 등), 점, 하이픈(표준+비표준), 밑줄, 슬래시, 공백 허용
# 위험 문자(; | & ` $ ( ) > < \n \r 등) 차단 — shlex.quote()로도 방어
_SSH_PATH_WHITELIST = re.compile(r'^[\w._/\- \u2010-\u2015\u00a0]+$', re.UNICODE)
_SSH_KEYWORD_WHITELIST = re.compile(r'^[\w._\- \u2010-\u2015]+$', re.UNICODE)
_SSH_SENSITIVE_PATTERNS = re.compile(
    r'(\.env($|/)|\.ssh/|id_rsa|\.git/config'
    r'|\bsecrets\b|\bpassword\b|\btoken\b'
    r'|\.npmrc|\.pypirc|\.netrc|\bcredentials\b|private_key|kubeconfig'
    r'|\.aws/|\.kube/|\.docker/|\.pem$|\.key$|authorized_keys|known_hosts)',
    re.IGNORECASE,
)
_SSH_TIMEOUT = 120  # 초 — docker build 등 장시간 명령 대응 (기존 10초→120초)
_SSH_WRITE_TIMEOUT = 15  # 쓰기 작업은 조금 더 여유
_SSH_CMD_TIMEOUT = 50  # 원격 명령 실행 타임아웃 (MCP bridge 55s 이내 응답 보장)
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
    "python3",
    "python",
    "sed",
    "awk",
    "npm",
    "node",
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
    "docker compose pull",
    # deploy.sh 안전 배포 게이트웨이
    "/root/aads/aads-server/deploy.sh",
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
    # Supervisord (AADS-190) — status만 허용, restart/start/stop은 deploy.sh 경유 필수
    "supervisorctl status",
    # "supervisorctl restart" 제거 — SSE 끊김 유발. deploy.sh 또는 reload-api.sh 사용
    # "supervisorctl start" 제거
    # "supervisorctl stop" 제거
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

# run_remote_command 차단 패턴 (최소 안전장치만 유지 — CEO 지시로 전면 해제)
_REMOTE_CMD_BLOCKED = re.compile(
    r"(rm\s+-rf\s+/\s*$|mkfs\s+/dev/[sv]da|:(){:|fork\s*bomb)",
    re.IGNORECASE,
)

# ─── 보안 상수 ─────────────────────────────────────────────────────────────────
# CEO 지시: 경로 제한 해제 — 민감 파일만 블랙리스트
_FILE_BLACKLIST = ["/root/.ssh", "/root/.env", "/etc/shadow", "/etc/gshadow"]
# SQL 차단 — CEO 지시로 SELECT 외 쓰기도 허용 (DROP/TRUNCATE만 유지)
_SQL_BLOCKED = re.compile(
    r"\b(DROP\s+(TABLE|DATABASE)|TRUNCATE)\b",
    re.IGNORECASE,
)
_MAX_LOG_BYTES = 50 * 1024   # 50 KB (CEO 지시: 제한 완화)
_MAX_URL_BYTES = 100 * 1024  # 100 KB (CEO 지시: 제한 완화)
_MAX_DB_ROWS = 500           # CEO 지시: 50→500 확대

# ─── Browser 보안 상수 (AADS-159→블랙리스트 전환, CEO 지시, LLM 우회 불가) ───
import ipaddress as _ipaddress

_BROWSER_BLOCKED_HOSTS = frozenset([
    "metadata.google.internal", "metadata.google.internal.",
    "169.254.169.254",
])
_BROWSER_BLOCKED_PORTS = frozenset([5432, 6379, 3306, 27017, 9200, 2379, 8500])
_BROWSER_SAFE_HOSTS = frozenset(["localhost", "127.0.0.1", "::1"])
_BROWSER_PRIVATE_NETWORKS = [
    _ipaddress.ip_network("10.0.0.0/8"),
    _ipaddress.ip_network("172.16.0.0/12"),
    _ipaddress.ip_network("192.168.0.0/16"),
    _ipaddress.ip_network("169.254.0.0/16"),
    _ipaddress.ip_network("fc00::/7"),
]
_BROWSER_TIMEOUT_MS = 60_000   # 60초 세션 타임아웃
_BROWSER_MAX_TABS = 3          # 최대 3탭

# Playwright 싱글턴 (FastAPI event loop 내 유지)
_pw_handle = None
_pw_browser = None
_pw_context = None
_pw_init_lock: Optional[asyncio.Lock] = None


# ─── 도구 실행 함수들 ──────────────────────────────────────────────────────────

async def tool_read_file(path: str) -> str:
    """로컬 파일 읽기 (블랙리스트 기반 — CEO 지시로 경로 제한 해제)."""
    try:
        resolved = str(Path(path).resolve())
    except Exception as e:
        return f"[ERROR] 경로 처리 실패: {e}"

    for blocked in _FILE_BLACKLIST:
        if resolved.startswith(blocked):
            return f"[ERROR] 접근 거부: {blocked} 경로는 보안상 차단되어 있습니다."

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
    """Docker logs 또는 journalctl 검색 (최근 200줄, 최대 50KB). CEO 지시로 컨테이너 제한 해제."""
    try:
        if source.lower() == "journalctl":
            cmd = ["journalctl", "--no-pager", "-n", "200"]
        else:
            # 컨테이너 이름 형식 검증만 (인젝션 방지, 화이트리스트 제거)
            if not re.match(r'^[a-zA-Z0-9._-]+$', source):
                return f"[ERROR] 잘못된 컨테이너 이름 형식: {source}"
            cmd = ["docker", "logs", "--tail", "200", source]

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
                content += "\n...(100KB 초과, 잘림)"
            return content
    except Exception as e:
        return f"[ERROR] URL 조회 실패: {e}"


# ─── SSH 원격 접근 도구 함수 (AADS-165) ──────────────────────────────────────────

_SSH_BLOCKED_SYSTEM_DIRS = ("/etc/", "/proc/", "/sys/", "/dev/", "/boot/", "/sbin/", "/lib/", "/lib64/")

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
    # workdir="/"인 경우 시스템 디렉토리 접근 차단 (SF/NTV2 보안 강화)
    if workdir == "/":
        if any(resolved.startswith(sd) for sd in _SSH_BLOCKED_SYSTEM_DIRS):
            return f"[ERROR] 접근 거부: 시스템 디렉토리({resolved}) 접근 불가."
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
    host = mapping["server"]
    port = str(mapping.get("port", 22))
    workdir = mapping["workdir"]
    extra_workdirs = [mapping["workdir_v2"]] if "workdir_v2" in mapping else None
    # 보안 검증: 민감 패턴 + 경로 탈출 + 시스템 디렉토리 차단
    path_err = _validate_ssh_path(file_path, workdir, extra_workdirs)
    if path_err:
        return path_err
    from posixpath import normpath, join as pjoin
    resolved = normpath(pjoin(workdir, file_path))
    cmd = f"cat {shlex.quote(resolved)}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            "-p", port, f"root@{host}",
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

    # 보안 2: 화이트리스트 — CEO 지시로 전면 해제 (모든 명령 허용)
    try:
        cmd_tokens = shlex.split(command)
    except ValueError:
        return "[ERROR] 명령어 파싱 실패"

    # 보안 2.5, 3: docker exec 컨테이너 제한 + 파이프/세미콜론 차단 — CEO 지시로 전면 해제

    # AADS 프로젝트: docker compose 명령 → deploy.sh 안전 리다이렉트
    if project == "AADS":
        _deploy_redirect = None
        # docker compose down → 차단 (전체 컨테이너 삭제 위험)
        if re.search(r"docker[\s-]+compose\s+down", command):
            logger.warning("deploy_blocked_compose_down", command=command[:120])
            return "[BLOCKED] docker compose down은 postgres 데이터 유실 위험. deploy.sh를 사용하세요."
        # docker stop <aads 컨테이너> → 차단
        if re.search(r"docker\s+(stop|kill)\s+aads-(postgres|redis|socket-proxy|litellm)", command):
            logger.warning("deploy_blocked_container_stop", command=command[:120])
            return "[BLOCKED] 의존 컨테이너 직접 정지는 서비스 장애를 유발합니다."
        # docker compose up/build/restart → deploy.sh 리다이렉트 (하이픈 형식도 포함)
        if re.search(r"docker[\s-]+compose\s+(up|build|restart)", command) and "aads" in command.lower():
            _deploy_redirect = "/root/aads/aads-server/deploy.sh bluegreen"
        elif re.search(r"docker\s+restart\s+aads-server", command):
            _deploy_redirect = "/root/aads/aads-server/deploy.sh bluegreen"
        if _deploy_redirect:
            logger.warning("deploy_intercept", original=command[:120], redirect=_deploy_redirect)
            command = _deploy_redirect

        workdir = get_workdir("AADS") or "/root"
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
            # 좀비 SSH 프로세스 방지 — 타임아웃 시 즉시 kill
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return f"[ERROR] AADS 호스트 명령 타임아웃 ({_SSH_CMD_TIMEOUT}초)"
        except asyncio.CancelledError:
            # MCP 브릿지 타임아웃으로 취소된 경우에도 프로세스 정리
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            raise
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
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return f"[ERROR] SSH 명령 타임아웃 ({_SSH_CMD_TIMEOUT}초): {server}"
    except asyncio.CancelledError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise
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

    # ── 커밋 전 AI 코드 검수: staged diff를 독립 AI(Gemini)가 리뷰 ──
    try:
        staged_diff = await tool_run_remote_command(project, "git diff --cached -- '*.py' '*.ts' '*.tsx'")
        if staged_diff and staged_diff.strip() and "[ERROR]" not in staged_diff:
            from app.services.code_reviewer import review_code_diff
            verdict = await review_code_diff(
                project=project,
                job_id="chat-direct",
                diff=staged_diff,
                instruction=f"채팅 AI 직접 수정: {message[:200]}",
            )
            if verdict.verdict == "FLAG":
                logger.warning(f"git_commit_blocked: project={project} verdict=FLAG score={verdict.score} issues={verdict.issues}")
                return (
                    f"[AI 코드 검수 실패 — 커밋 차단] score={verdict.score:.2f}\n"
                    f"issues: {', '.join(verdict.issues)}\n"
                    f"문제를 수정 후 다시 커밋하세요."
                )
            elif verdict.verdict == "REQUEST_CHANGES":
                logger.warning(f"git_commit_warning: project={project} verdict=REQUEST_CHANGES score={verdict.score}")
                # 경고 포함하여 커밋 진행
                _review_warning = f"⚠️ [코드 검수 경고] score={verdict.score:.2f} — {', '.join(verdict.issues[:3])}\n"
            else:
                _review_warning = ""
                logger.info(f"git_commit_review_passed: project={project} score={verdict.score:.2f}")
        else:
            _review_warning = ""
    except Exception as _review_err:
        # 검수 AI 실패 시 → 커밋 차단 (안전 우선)
        logger.error(f"git_commit_review_failed: project={project} error={_review_err}")
        return (
            f"[AI 코드 검수 불가 — 커밋 차단] {str(_review_err)[:100]}\n"
            f"LLM API 상태 확인 후 다시 시도하세요."
        )

    # shlex.quote()로 안전한 메시지 이스케이프
    safe_msg = shlex.quote(message[:200])
    result = await tool_run_remote_command(project, f"git commit -m {safe_msg}")
    return _review_warning + result if _review_warning else result


async def tool_git_remote_push(project: str, branch: str = "") -> str:
    """원격 서버 git push (force push 차단). AI 코드 검수는 commit 단계에서 수행됨."""
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


# ─── Pipeline Runner 도구 함수 ────────────────────────────────────────────────


async def tool_pipeline_c_status(job_id: str) -> str:
    """Pipeline Runner 상태 조회."""
    from app.services.pipeline_runner_service import get_pipeline_status, list_pipelines

    if not job_id:
        # 전체 목록
        jobs = await list_pipelines()
        if not jobs:
            return "실행 중인 Runner 작업이 없습니다."
        lines = ["[활성 Runner 작업 목록]"]
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
        f"[Pipeline Runner 상태: {result['job_id']}]",
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
    """Pipeline Runner 승인/거부."""
    if not job_id:
        return "[ERROR] job_id 필수"

    from app.services.pipeline_runner_service import approve_pipeline, reject_pipeline

    if approved:
        result = await approve_pipeline(job_id)
        if "error" in result:
            return f"[ERROR] {result['error']}"
        return (
            f"[Pipeline Runner 배포 완료]\n"
            f"Job: {job_id}\n"
            f"결과: {result.get('summary', 'OK')}\n"
            f"Health: {result.get('health', 'N/A')[:200]}\n"
            f"에러: {result.get('errors', '없음')[:200]}"
        )
    else:
        result = await reject_pipeline(job_id, reason)
        if "error" in result:
            return f"[ERROR] {result['error']}"
        return f"[Pipeline Runner 거부] {result.get('message', '변경사항 원복됨')}"


async def tool_pipeline_c_cancel(job_id: str) -> str:
    """Pipeline Runner 강제 취소."""
    if not job_id:
        return "[ERROR] job_id 필수"
    from app.services.pipeline_runner_service import cancel_pipeline
    result = await cancel_pipeline(job_id)
    if "error" in result:
        return f"[ERROR] {result['error']}"
    return (
        f"[Pipeline Runner 취소 완료]\n"
        f"Job: {job_id}\n"
        f"Kill된 프로세스: {result.get('killed_pids', [])}\n"
        f"{result.get('message', '')}"
    )


async def tool_pipeline_c_retry(job_id: str) -> str:
    """에러/취소된 Pipeline Runner 재실행."""
    if not job_id:
        return "[ERROR] job_id 필수"
    from app.services.pipeline_runner_service import retry_pipeline
    result = await retry_pipeline(job_id)
    if "error" in result:
        return f"[ERROR] {result['error']}"
    return (
        f"[Pipeline Runner 재실행]\n"
        f"원본 Job: {job_id}\n"
        f"새 Job: {result['job_id']}\n"
        f"프로젝트: {result['project']}\n"
        f"{result.get('message', '')}\n\n"
        f"진행 확인: pipeline_c_status(job_id=\"{result['job_id']}\")"
    )


# ─── Browser 도구 함수 (AADS-159) ──────────────────────────────────────────────

def _browser_domain_ok(url: str) -> Optional[str]:
    """블랙리스트 보안 검사. 차단이면 에러 문자열, 통과이면 None."""
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        scheme = (parsed.scheme or "").lower()
        port = parsed.port
    except Exception:
        return "[접근 차단] URL 파싱 실패"
    if not hostname:
        return "[접근 차단] 호스트명이 없습니다"
    if scheme not in ("http", "https", ""):
        return f"[접근 차단] 허용되지 않은 프로토콜: {scheme}"
    if port and port in _BROWSER_BLOCKED_PORTS:
        return f"[접근 차단] 민감 포트 접근 불가: {port}"
    if hostname in _BROWSER_SAFE_HOSTS:
        return None
    if hostname in _BROWSER_BLOCKED_HOSTS:
        return f"[접근 차단] 보안 차단 호스트: {hostname}"
    try:
        addr = _ipaddress.ip_address(hostname)
        for net in _BROWSER_PRIVATE_NETWORKS:
            if addr in net:
                return f"[접근 차단] 내부 네트워크 접근 불가: {hostname}"
    except ValueError:
        pass
    return None


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
            import os

            if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
                for p in ["/root/.cache/ms-playwright", "/root/.cache"]:
                    if os.path.isdir(os.path.join(p, "chromium-1208")) or os.path.isdir(
                        os.path.join(p, "chromium_headless_shell-1208")
                    ):
                        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = p
                        break

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

_PROJECT_SCOPED_TOOLS = frozenset({
    "query_project_database",
    "read_remote_file",
    "list_remote_dir",
    "write_remote_file",
    "patch_remote_file",
    "run_remote_command",
    "git_remote_add",
    "git_remote_commit",
    "git_remote_push",
    "git_remote_status",
    "git_remote_create_branch",
    "pipeline_runner_submit",
})
_PROJECT_KEYS = ("GO100", "NTV2", "KIS", "SF", "AADS", "NAS", "KAKAOBOT")


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _project_from_settings(settings: Dict[str, Any]) -> str:
    for key in ("project_key", "active_project", "project", "db_profile"):
        value = str(settings.get(key) or "").upper().strip()
        if value in _PROJECT_KEYS:
            return value
    return ""


def _project_from_workspace_name(name: str) -> str:
    upper = (name or "").upper()
    aliases = {
        "GO100": ("GO100", "백억", "빡억"),
        "NTV2": ("NTV2", "NEWTALK V2", "뉴톡"),
        "KIS": ("KIS", "자동매매"),
        "SF": ("SF", "SHORTFLOW"),
        "AADS": ("AADS",),
    }
    for project, tokens in aliases.items():
        if any(token in upper or token in name for token in tokens):
            return project
    return ""


async def _infer_project_from_session(chat_session_id: str) -> str:
    if not chat_session_id:
        return ""
    try:
        uuid.UUID(str(chat_session_id))
    except Exception:
        return ""

    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    w.name,
                    COALESCE(w.settings, '{}'::jsonb) AS workspace_settings,
                    COALESCE(s.settings, '{}'::jsonb) AS session_settings
                FROM chat_sessions s
                JOIN chat_workspaces w ON w.id = s.workspace_id
                WHERE s.id = $1::uuid
                """,
                chat_session_id,
            )
    except Exception as exc:
        logger.debug("tool_project_autofill_lookup_failed: session=%s error=%s", chat_session_id[:8], exc)
        return ""

    if not row:
        return ""

    session_project = _project_from_settings(_json_object(row["session_settings"]))
    if session_project:
        return session_project
    workspace_project = _project_from_settings(_json_object(row["workspace_settings"]))
    if workspace_project:
        return workspace_project
    return _project_from_workspace_name(str(row["name"] or ""))


async def execute_tool(name: str, params: Dict[str, Any], dsn: str, chat_session_id: str = "") -> str:
    """도구 이름과 파라미터로 실제 실행."""
    params = dict(params or {})
    if name in _PROJECT_SCOPED_TOOLS and not str(params.get("project") or "").strip():
        inferred_project = await _infer_project_from_session(chat_session_id)
        if inferred_project:
            params["project"] = inferred_project
            logger.info(
                "tool_project_autofilled: tool=%s session=%s project=%s",
                name,
                chat_session_id[:8],
                inferred_project,
            )

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
            params.get("file_path") or params.get("path", ""),
            offset=int(params.get("offset", 1) or 1),
            limit=int(params.get("limit", 2000) or 2000),
        )
    # ── Pipeline Runner 도구 (호스트 독립 실행) ─────────────────────────────
    elif name == "pipeline_runner_submit":
        from app.services.tool_executor import current_chat_session_id
        from app.services.pipeline_runner_client import (
            INTERNAL_PIPELINE_HEADERS,
            get_pipeline_runner_api_url,
        )
        # session_id 강제: 1순위 도구파라미터, 2순위 함수인자, 3순위 ContextVar
        _sid = params.get("session_id", "") or chat_session_id or current_chat_session_id.get("")
        if not _sid:
            try:
                from app.services.pipeline_runner_service import _find_recent_session
                _sid = await _find_recent_session(params.get("project", "AADS"))
            except Exception:
                pass
        if not _sid:
            return "[ERROR] 활성 세션을 찾을 수 없습니다. session_id를 명시하세요."
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                get_pipeline_runner_api_url("jobs"),
                headers=INTERNAL_PIPELINE_HEADERS,
                json={
                    "project": params.get("project", "AADS"),
                    "instruction": params.get("instruction", ""),
                    "session_id": _sid,
                    "max_cycles": int(params.get("max_cycles", 3)),
                    "size": params.get("size", "M"),
                    "worker_model": params.get("worker_model", ""),
                    "parallel_group": params.get("parallel_group", ""),
                    "depends_on": params.get("depends_on", ""),
                },
                timeout=10,
            )
            return resp.text
    elif name == "pipeline_runner_status":
        import httpx
        from urllib.parse import quote
        from app.services.pipeline_runner_client import (
            INTERNAL_PIPELINE_HEADERS,
            get_pipeline_runner_api_url,
        )
        job_id = params.get("job_id", "")
        if job_id:
            url = get_pipeline_runner_api_url(f"jobs/{quote(job_id, safe='')}")
        else:
            status_val = params.get("status", "")
            url = get_pipeline_runner_api_url("jobs")
            _qp = {"limit": "10"}
            if status_val:
                _qp["status"] = status_val
        async with httpx.AsyncClient() as client:
            if job_id:
                resp = await client.get(url, headers=INTERNAL_PIPELINE_HEADERS, timeout=10)
            else:
                resp = await client.get(url, params=_qp, headers=INTERNAL_PIPELINE_HEADERS, timeout=10)
            return resp.text
    elif name == "pipeline_runner_approve":
        import httpx
        from urllib.parse import quote
        from app.services.pipeline_runner_client import (
            INTERNAL_PIPELINE_HEADERS,
            get_pipeline_runner_api_url,
        )
        job_id = params.get("job_id", "")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                get_pipeline_runner_api_url(f"jobs/{quote(job_id, safe='')}/approve"),
                headers=INTERNAL_PIPELINE_HEADERS,
                json={"action": params.get("action", "approve"), "feedback": params.get("feedback", "")},
                timeout=10,
            )
            return resp.text
    # ── pipeline_c_* 레거시 호환 → pipeline_runner_* 로 리다이렉트 ──────
    elif name == "pipeline_c_start":
        logger.info(f"pipeline_c_start → pipeline_runner_submit 리다이렉트")
        params.setdefault("project", "AADS")
        return await execute_tool("pipeline_runner_submit", params, dsn=dsn, chat_session_id=chat_session_id)
    elif name == "pipeline_c_status":
        return await execute_tool("pipeline_runner_status", params, dsn=dsn, chat_session_id=chat_session_id)
    elif name == "pipeline_c_approve":
        return await execute_tool("pipeline_runner_approve", params, dsn=dsn, chat_session_id=chat_session_id)
    elif name == "pipeline_c_cancel":
        return await execute_tool("pipeline_runner_approve", {**params, "action": "reject"}, dsn=dsn, chat_session_id=chat_session_id)
    elif name == "pipeline_c_retry":
        return await execute_tool("pipeline_runner_submit", params, dsn=dsn, chat_session_id=chat_session_id)
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
    # ── 이미지/팩트체크/검색/샌드박스/알림 도구 ──────────────────────────
    elif name == "generate_image":
        from app.services.image_service import image_service
        result = await image_service.generate(params.get("prompt", ""), params.get("size", "1024x1024"))
        return json.dumps(result, ensure_ascii=False)
    elif name == "fact_check":
        from app.services.fact_checker import FactChecker
        checker = FactChecker()
        result = await checker.check(params.get("claim", ""))
        return json.dumps(result.to_dict(), ensure_ascii=False)
    elif name == "fact_check_multiple":
        from app.services.fact_checker import FactChecker
        checker = FactChecker()
        result = await checker.check_multiple(params.get("claims", []))
        return json.dumps([r.to_dict() for r in result], ensure_ascii=False)
    elif name == "gemini_grounding_search":
        from app.services.gemini_search_service import GeminiSearchService
        svc = GeminiSearchService()
        result = await svc.search_grounded(params.get("query", ""), params.get("context", ""))
        return json.dumps({"text": result.text, "citations": result.citations}, ensure_ascii=False, default=str)
    elif name == "execute_sandbox":
        from app.services.sandbox import execute_code
        result = await execute_code(params.get("code", ""), params.get("language", "python"), params.get("timeout", 30))
        return json.dumps(result, ensure_ascii=False)
    elif name == "send_telegram":
        from app.services.telegram_bot import get_telegram_bot
        bot = get_telegram_bot()
        if bot and bot.is_ready:
            await bot.send_message(params.get("message", ""))
            return "텔레그램 전송 완료"
        return "[ERROR] 텔레그램 봇 미설정"
    elif name == "search_kakao":
        from app.services.kakao_search_service import KakaoSearchService
        svc = KakaoSearchService()
        if not svc.is_available():
            return "[ERROR] 카카오 API 키 미설정"
        result = await svc.search(params.get("query", ""))
        return json.dumps({"text": result.text, "citations": result.citations}, ensure_ascii=False, default=str)
    elif name == "search_naver":
        from app.services.naver_search_service import NaverSearchService
        svc = NaverSearchService()
        if not svc.is_available():
            return "[ERROR] 네이버 API 키 미설정"
        result = await svc.search(params.get("query", ""), params.get("search_type", "webkr"))
        return json.dumps({"text": result.text, "citations": result.citations}, ensure_ascii=False, default=str)
    elif name == "search_naver_multi":
        from app.services.naver_search_service import NaverSearchService
        svc = NaverSearchService()
        if not svc.is_available():
            return "[ERROR] 네이버 API 키 미설정"
        results = await svc.multi_search(params.get("query", ""), params.get("types", ["webkr", "news", "blog"]))
        return json.dumps([{"type": r.citations[0].get("type", "unknown") if r.citations else "unknown", "text": r.text, "citations": r.citations} for r in results], ensure_ascii=False, default=str)
    elif name == "visual_qa_test":
        return "[INFO] Visual QA는 현재 Playwright 기반 배치 모드만 지원. capture_screenshot + read_remote_file 조합 사용 권장."
    elif name == "evaluate_alerts":
        from app.services.alert_manager import get_alert_manager
        mgr = get_alert_manager()
        alerts = await mgr.evaluate_rules()
        for alert in alerts:
            await mgr.send_alert(alert)
        return f"알림 평가 완료: {len(alerts)}건 발송"
    elif name == "send_alert_message":
        from app.services.telegram_bot import get_telegram_bot
        bot = get_telegram_bot()
        level = params.get("level", "info")
        msg = f"[{level.upper()}] {params.get('message', '')}"
        if bot and bot.is_ready:
            await bot.send_message(msg)
            return f"알림 발송 완료: {msg[:100]}"
        return "[ERROR] 텔레그램 봇 미설정"
    # ── 원격 쓰기/실행 도구 (AADS-190 Phase 1) ─────────────────────────────
    elif name == "write_remote_file":
        return await tool_write_remote_file(
            params.get("project", ""),
            params.get("file_path") or params.get("path", ""),
            params.get("content", ""),
            params.get("backup", True),
        )
    elif name == "patch_remote_file":
        return await tool_patch_remote_file(
            params.get("project", ""),
            params.get("file_path") or params.get("path", ""),
            params.get("old_string", ""),
            params.get("new_string", ""),
        )
    elif name == "run_remote_command":
        return await tool_run_remote_command(
            params.get("project", ""),
            params.get("command", ""),
        )
    # ── Git 원격 도구 (AADS-190) ──────────────────────────────────────────
    elif name == "git_remote_status":
        return await tool_git_remote_status(params.get("project", ""))
    elif name == "git_remote_add":
        return await tool_git_remote_add(params.get("project", ""), params.get("files", "."))
    elif name == "git_remote_commit":
        return await tool_git_remote_commit(params.get("project", ""), params.get("message", ""))
    elif name == "git_remote_push":
        return await tool_git_remote_push(params.get("project", ""), params.get("branch", ""))
    elif name == "git_remote_create_branch":
        return await tool_git_remote_create_branch(params.get("project", ""), params.get("branch_name", ""))
    # ── 스크린샷 (독립 캡처) ──────────────────────────────────────────────
    elif name == "capture_screenshot":
        return await tool_capture_screenshot(params.get("url", ""), params.get("full_page", False))
    # ── 프로젝트 DB 도구 ─────────────────────────────────────────────────
    elif name == "query_project_database":
        from app.api.ceo_chat_tools_db import query_project_database
        return json.dumps(await query_project_database(
            project=params.get("project", ""),
            query=params.get("query", ""),
            db_name=params.get("db_name", ""),
            limit=params.get("limit", 100),
        ), ensure_ascii=False, default=str)
    elif name == "list_project_databases":
        from app.api.ceo_chat_tools_db import list_project_databases
        return json.dumps(await list_project_databases(), ensure_ascii=False, default=str)
    # ── 내보내기 도구 ─────────────────────────────────────────────────────
    elif name == "export_data":
        from app.api.ceo_chat_tools_export import export_data
        result = await export_data(
            data_source=params.get("data_source", ""),
            format=params.get("format", "csv"),
            query=params.get("query", ""),
            filters=params.get("filters", {}),
        )
        return json.dumps(result, ensure_ascii=False, default=str)
    # ── 스케줄러 도구 ─────────────────────────────────────────────────────
    elif name == "schedule_task":
        from app.api.ceo_chat_tools_scheduler import schedule_task
        result = await schedule_task(
            name=params.get("name", ""),
            schedule=params.get("schedule", ""),
            action_type=params.get("action_type", "tool_call"),
            action_config=params.get("action_config", {}),
            description=params.get("description", ""),
        )
        return json.dumps(result, ensure_ascii=False, default=str)
    elif name == "unschedule_task":
        from app.api.ceo_chat_tools_scheduler import unschedule_task
        result = await unschedule_task(name=params.get("name", ""))
        return json.dumps(result, ensure_ascii=False, default=str)
    elif name == "list_scheduled_tasks":
        from app.api.ceo_chat_tools_scheduler import list_scheduled_tasks
        result = await list_scheduled_tasks()
        return json.dumps(result, ensure_ascii=False, default=str)
    # ── 작업 모니터링 도구 ────────────────────────────────────────────────
    elif name == "check_task_status":
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            pc_rows = await conn.fetch(
                "SELECT job_id AS task_id, project, instruction AS title, "
                "'pipeline_c' AS pipeline, phase, status, created_at "
                "FROM pipeline_jobs "
                "WHERE status IN ('running','awaiting_approval','queued') "
                "OR updated_at > NOW() - interval '1 hour' "
                "ORDER BY created_at DESC LIMIT 10"
            )
        tasks = [{"task_id": r["task_id"], "project": r["project"] or "", "title": (r["title"] or "")[:150], "pipeline": r["pipeline"], "phase": r["phase"] or "", "status": r["status"] or ""} for r in pc_rows]
        return json.dumps({"tasks": tasks, "count": len(tasks)}, ensure_ascii=False, default=str)
    elif name == "read_task_logs":
        task_id = params.get("task_id", "")
        if not task_id:
            return "[ERROR] task_id 필수"
        last_n = min(int(params.get("last_n", 30) or 30), 100)
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT log_type, content, phase, created_at FROM task_logs "
                "WHERE task_id = $1 ORDER BY created_at DESC LIMIT $2",
                task_id, last_n,
            )
        logs = [{"type": r["log_type"], "content": r["content"], "phase": r["phase"] or "", "at": r["created_at"].isoformat()} for r in reversed(rows)]
        return json.dumps({"task_id": task_id, "logs": logs, "count": len(logs)}, ensure_ascii=False, default=str)
    elif name == "terminate_task":
        task_id = params.get("task_id", "")
        if not task_id:
            return "[ERROR] task_id 필수"
        reason = params.get("reason", "CEO 요청에 의한 강제 종료")
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT job_id, status FROM pipeline_jobs WHERE job_id = $1", task_id)
            if row and row["status"] in ("running", "queued", "awaiting_approval"):
                await conn.execute("UPDATE pipeline_jobs SET status = 'error', error_message = $2, updated_at = NOW() WHERE job_id = $1", task_id, reason)
                return json.dumps({"terminated": task_id, "reason": reason}, ensure_ascii=False)
            return json.dumps({"error": f"종료할 수 없는 상태: {row['status'] if row else '작업 없음'}"}, ensure_ascii=False)
    # ── 멀티에이전트/토론 도구 ────────────────────────────────────────────
    elif name == "run_agent_team":
        from app.services.agent_orchestrator import run_agent_team
        result = await run_agent_team(
            name=params.get("name", "Agent Team"),
            phases=params.get("phases", []),
            max_concurrent=params.get("max_concurrent", 5),
            cost_limit_usd=params.get("cost_limit_usd", 10.0),
        )
        return json.dumps(result, ensure_ascii=False, default=str) if isinstance(result, dict) else str(result)
    elif name == "run_debate":
        from app.services.debate_service import run_debate
        result = await run_debate(
            question=params.get("question", ""),
            context=params.get("context", ""),
            perspectives=params.get("perspectives"),
            session_id=params.get("session_id"),
        )
        return json.dumps({
            "question": result.question,
            "perspectives": [{"name": p.name, "analysis": p.analysis, "key_points": p.key_points} for p in result.perspectives],
            "synthesis": result.synthesis,
        }, ensure_ascii=False, default=str)
    # ── tool_executor 위임: 기본/조회/분석/메모리/에이전트 도구 ─────────────
    elif name in (
        "health_check", "dashboard_query", "task_history", "server_status",
        "directive_create", "read_github_file", "query_database", "cost_report",
        "web_search_brave", "web_search", "search_searxng",
        "inspect_service", "get_all_service_status", "generate_directive",
        "jina_read", "crawl4ai_fetch", "deep_crawl",
        "save_note", "recall_notes", "delete_note", "learn_pattern", "observe",
        "deep_research", "code_explorer", "analyze_changes", "search_all_projects",
        "check_directive_status", "delegate_to_agent", "delegate_to_research",
        "spawn_subagent", "spawn_parallel_subagents",
        "semantic_code_search", "read_uploaded_file",
        "add_agenda", "list_agendas", "get_agenda", "update_agenda",
        "decide_agenda", "search_agendas",
    ):
        from app.services.tool_executor import ToolExecutor
        _executor = ToolExecutor()
        return await _executor.execute(name, params)
    else:
        return f"[ERROR] 알 수 없는 도구: {name}"
