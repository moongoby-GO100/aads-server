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

## 인증 토큰 절대 규칙 (R-AUTH)
- **AADS는 Auth Token(OAuth) 방식 사용** — `ANTHROPIC_AUTH_TOKEN` (sk-ant-oat01-...)
- aads-server 컨테이너에 `ANTHROPIC_API_KEY`는 **존재하지 않음** — 코드에서 직접 사용 금지
- **2계정 자동 스위치**: `ANTHROPIC_AUTH_TOKEN` (1순위) → `ANTHROPIC_API_KEY_FALLBACK` (2순위) → Gemini LiteLLM (3순위)
- **Gemini/DeepSeek 등 외부 LLM**: 반드시 LiteLLM 프록시 경유 (`LITELLM_BASE_URL`) — 직접 REST API 호출 금지
- **중앙 클라이언트**: `app/core/anthropic_client.py`의 `call_llm_with_fallback()` 사용 — 직접 Anthropic SDK 초기화 금지
- 코드 수정 시 `ANTHROPIC_API_KEY`를 새로 추가하거나 참조하면 **R-AUTH 위반** → CEO 승인 필수

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

## AADS-190: 원격 쓰기/실행 + 서브에이전트 (2026-03-10)
- **Phase 0**: 에러 리포팅(`/chat/errors/report`), StreamManager(멀티세션), CEO Chat 메모리 주입, 임베딩 검증
- **Phase 1**: 9개 원격 도구 — write_remote_file, patch_remote_file, run_remote_command, git 5종
  - 보안: blocked regex → whitelist → pipe 제한, 민감 경로 차단, force push 차단
- **Phase 2**: 서브에이전트(`spawn_subagent`, `spawn_parallel_subagents`), 턴 100/예산 $50, 압축 환경변수화
  - 서브에이전트: 독립 LLM 호출, 읽기 도구 7종, asyncio.gather 병렬 실행
- **리포트**: `reports/20260310_AADS190_phase0_phase1_phase2_report.md`

## 현재 상태
- Phase: Phase 2 운영
- 최근: AADS-190(원격 쓰기+서브에이전트), AADS-186E(메모리 자동 주입), AADS-188C(도구 우선순위)
- 긴급: 없음

## 빌드/배포
docker compose -f docker-compose.prod.yml up -d --build aads-server
curl -s https://aads.newtalk.kr/api/v1/ops/health-check | python3 -m json.tool
