# AADS-KNOWLEDGE: AADS 시스템 전용 지식

## 아키텍처
- 8-agent LangGraph 체인: Supervisor→PM→Architect→Developer→QA→Judge→DevOps→Researcher
- 5계층 메모리: Working→Project→Experience(pgvector)→System→Procedural
- MCP 상시 4개(Filesystem, Git, Memory, PostgreSQL) + 온디맨드 3개(GitHub, Brave, Fetch)
- Backend: FastAPI 0.115 + Uvicorn, PostgreSQL 15, Upstash Redis, Docker Compose
- Frontend: Next.js 16 + React 19 + Tailwind CSS 4
- 인증/LLM 키는 DB `llm_api_keys`에 Fernet 암호화 저장, 런타임은 DB 우선 후 `.env` 폴백
- `app/core/llm_key_provider.py`가 provider별 키 조회, 우선순위 정렬, 300초 캐시 및 폴백 체인을 담당

## 지시서 자동화 파이프라인 (TECH-002)
8단계: CEO지시 → Bridge감지 → 사전검증 → 우선순위전송 → Claude실행 → 결과보고 → DB기록 → 교차검증
교차검증 9종: pending정체, running초과, DB-파일정합, 커밋완전성, 비용$0, 디스크75%, 에이전트무활동, seen_tasks차단, 미감지복원
자동복구 12건: pipeline_monitor, watchdog, cross_validator, approval_queue 등

## Bridge.py 동작 원리
- GenSpark 채팅 → Selenium 폴링 → 분류(7카테고리) → 의사결정 추출 → 지시서 .md 생성 → pending/
- 중복 방지: SKIP_PATTERNS 10개, [BRIDGE-SENT] 마커, SHA256 해시, seen_tasks.json
- 결과 보고: done/ 감시 → Telegram 발송 → archived/ 이동

## Watchdog 주의사항 (L-001, L-006 참조)
- 서비스명은 반드시 docker ps --filter name=xxx 확인 후 등록
- error_log INSERT는 error_hash UPSERT (occurrence_count 증가)
- 배포 직후 5분 error_log 증가 추이 모니터링 필수

## 세션 관리 (AADS-117)
- 글로벌 ≤4세션 (3서버 합산), 서버별 동적 1~3슬롯
- 211=Hub(SSH 집계, 캐시 생성 TTL 40s), 68/114=Client(캐시 읽기)
- 계정 2개(gmail/naver) MAX-200, 전환 쿨다운 5분

## 인증/키 관리
- Anthropic은 OAuth 토큰만 사용하며 `call_llm_with_fallback()` 경유가 원칙
- 외부 LLM(Gemini/DeepSeek)은 LiteLLM 프록시 경유, 직접 REST API 호출 금지
- Gemini 키는 `newtalk`/`aads` 2계정을 로드밸런싱하며 DB `llm_api_keys`에서 중앙 관리

## 함정 (과거 실패)
- Supavisor 경유 → AsyncPipeline 충돌 → 직접 연결만 사용 (R-011)
- langgraph-supervisor MCP 루프 버그 #249 → 프로덕션 금지 (R-010)
- HANDOVER 22k토큰 → 컨텍스트 낭비 → v6.0에서 50줄로 축소 완료
- seen_tasks에 실패 작업 잔류 → 영구 차단 → 체크 8로 자동 해제
