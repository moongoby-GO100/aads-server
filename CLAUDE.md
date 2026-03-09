# AADS — 자율 AI 개발 시스템 (서버 68)

## 기술 스택
FastAPI 0.115, PostgreSQL 15, LangGraph 1.0.10, Docker Compose, Python 3.11, Next.js 16

## CEO 절대 규칙
- CEO-DIRECTIVES: https://raw.githubusercontent.com/moongoby-GO100/aads-docs/main/CEO-DIRECTIVES.md
- 핵심: Supavisor 금지, langgraph-supervisor 금지, LLM 15회/task, 비용 효율 최우선
- HANDOVER 업데이트 없이 완료 선언 금지 (R-001)
- GitHub 브라우저 경로로 보고 (R-008)

## API 키 보안 절대 규칙 (R-KEY)
- **절대 API 키를 소스코드/docker-compose/yaml에 하드코딩하지 않는다**
- 모든 시크릿은 `.env` 파일에만 저장하고, docker-compose에서는 `${VAR:-}` 참조
- `.env`는 `.gitignore`에 포함되어 있으므로 git에 커밋되지 않음
- 커밋/푸시 전 pre-commit hook이 API 키 패턴을 자동 감지하여 차단
- 위반 시: Google 등 제공사가 키를 leaked 처리하여 영구 비활성화됨

## FLOW 프레임워크
Find→Layout→Operate→Wrap up. 상세: .claude/rules/flow-rules.md

## 공유 교훈
docs/shared-lessons/INDEX.md 참조. 작업 전 관련 교훈 확인 필수.

## AADS 전용 지식
docs/knowledge/AADS-KNOWLEDGE.md — 아키텍처, 파이프라인, 교차검증, 함정

## 메모리 자동 주입 시스템 (AADS-186E, 2026-03-09)
- **모듈**: `app/core/memory_recall.py` — 5섹션 메모리 빌더 (session_notes/preferences/tool_strategy/directives/discoveries)
- **프로젝트별 필터**: ai_observations.project 컬럼으로 AADS/KIS/GO100/SF/NTV2/NAS 분리 주입
- **자동 축적**: 20턴마다 session_notes 저장 + CEO 패턴 관찰, 에이전트 완료 시 discovery 기록
- **시드 데이터**: 37건 (공통 12 + 프로젝트별 25) — `scripts/init_memory_schema.sql`
- **DB 테이블**: session_notes, ai_observations (project 컬럼), ai_meta_memory

## 현재 상태
- Phase: Phase 2 운영
- 최근: AADS-186E(메모리 자동 주입), AADS-188C(도구 우선순위), AADS-121(Claude Code 설정)
- 긴급: 없음

## 빌드/배포
docker compose -f docker-compose.prod.yml up -d --build aads-server
curl -s https://aads.newtalk.kr/api/v1/ops/health-check | python3 -m json.tool
