# AADS-190: Phase 0 + Phase 1 + Phase 2 구현 완료 보고서

**작업일**: 2026-03-10
**작업자**: Claude Code (Opus 4.6)
**세션**: Claude Code → AADS Chat 기능 강화 전체 구현

---

## 1. 작업 배경

CEO moongoby 지시: AADS Chat (aads.newtalk.kr/chat)을 Claude Code보다 뛰어나게 만들기 위한 3단계 구현.
- Phase 0: 기반 인프라 (에러 리포팅, 멀티세션 스트리밍, 메모리 통합, 임베딩)
- Phase 1: 원격 쓰기/실행 도구 (write_remote_file, patch, run_command, git 도구)
- Phase 2: 확장 (턴/예산 확장, 압축 설정, 서브에이전트)

---

## 2. Phase 0 — 기반 인프라 (4/4 완료)

### Item 1: 에러 리포팅 시스템
- **백엔드**: `POST /api/v1/chat/errors/report` 엔드포인트 (`app/routers/chat.py`)
  - structlog 구조화 로깅 + ai_observations 자동 저장 → AI 메모리 주입
  - Pydantic 모델: `ErrorReportRequest`, `ErrorReportOut`
- **프론트엔드**: `src/services/errorReporter.ts` (신규)
  - 6가지 에러 타입: SSE_DISCONNECT, API_ERROR, STREAM_TIMEOUT, SESSION_SWITCH, TOOL_FAILURE, UNHANDLED
  - 60초 중복 방지 (dedup), fire-and-forget 전송
  - `initGlobalErrorHandlers()` — window.onerror + unhandledrejection 캡처
- **통합**: `ClientLayout.tsx`에서 1회 초기화

### Item 2: 멀티세션 백그라운드 스트리밍
- **StreamManager**: `src/services/streamManager.ts` (신규)
  - 인메모리 세션별 스트림 상태 저장소
  - registerStream → updateStreamText → completeStream 라이프사이클
  - 5분 이상 경과 스트림 자동 정리, 상태 변경 리스너
- **useChatSSE.ts 통합**: 스트림 시작/업데이트/완료 시 StreamManager 동기화
- **useChatSession.ts 통합**: `getBackgroundStream()`, `activeStreamSessionIds()` API 노출

### Item 3: CEO Chat 메모리 통합
- `app/api/ceo_chat.py:1492` — `build_memory_context()` 자동 주입
- 5섹션 메모리 (세션요약/CEO선호/도구전략/활성Directive/학습사항) ~2000 토큰

### Item 4: 임베딩 API 검증
- `gemini-embedding-001` (3072차원) 정상 동작 확인
- `code_indexer_service.py` 독스트링 모델명 수정 완료

---

## 3. Phase 1 — 원격 쓰기/실행 도구 (8/8 완료)

### 신규 도구 목록

| 도구 | 등급 | 파일 | 기능 |
|------|------|------|------|
| `write_remote_file` | Yellow | ceo_chat_tools.py | SSH 파일 쓰기 + .bak_aads 자동 백업 |
| `patch_remote_file` | Yellow | ceo_chat_tools.py | old_string→new_string 교체 (1-match 검증) |
| `run_remote_command` | Yellow | ceo_chat_tools.py | 화이트리스트 기반 원격 명령 실행 |
| `git_remote_add` | Yellow | ceo_chat_tools.py | 원격 git add |
| `git_remote_commit` | Yellow | ceo_chat_tools.py | 원격 git commit |
| `git_remote_push` | Yellow | ceo_chat_tools.py | 원격 git push (force push 차단) |
| `git_remote_status` | Green | ceo_chat_tools.py | 원격 git status |
| `git_remote_create_branch` | Yellow | ceo_chat_tools.py | 원격 브랜치 생성 |

### 보안 3단계
1. `_REMOTE_CMD_BLOCKED` regex — rm -rf, DROP, force push 등 위험 패턴 차단
2. `_REMOTE_CMD_WHITELIST` — 31개 허용 명령어 프리픽스
3. 파이프/체인(|, &&, ;, $()) 제한

### MCP Git Server 확장
- `mcp_servers/git_server.py` — 5개 쓰기 도구 추가 (git_add, commit, push, create_branch, checkout)

---

## 4. Phase 2 — 확장 (3/3 완료)

### Phase 2-A: 서브에이전트 시스템 (신규)
- **파일**: `app/services/subagent_service.py` (신규)
- **도구**: `spawn_subagent`, `spawn_parallel_subagents`
- **구현**:
  - 독립적 Anthropic API 호출 (sonnet/opus/haiku 선택 가능)
  - 읽기 전용 도구 7종 자동 사용 (Green 등급만)
  - Tool Use 루프 최대 5턴, 전체 120초 타임아웃
  - `asyncio.gather` 병렬 실행, Semaphore(5) 동시성 제한
- **테스트 결과**:
  - 단일 실행: haiku 972ms ✅
  - 병렬 3개: ~740ms 동시 완료 ✅
  - 도구 사용(health_check): 4.3초, 자율 보고서 생성 ✅
- **등록**: tool_executor, tool_registry(meta 그룹), agent_sdk_service(Yellow 등급)

### Phase 2-B: 턴/예산 확장
- `_MAX_TURNS`: 30 → **100**
- `_MAX_BUDGET_USD`: $10 → **$50**
- 환경변수: `AGENT_SDK_MAX_TURNS`, `AGENT_SDK_MAX_BUDGET_USD`

### Phase 2-C: 압축 설정 유연화
- `COMPACTION_TRIGGER_TURNS` 환경변수로 조정 가능 (기본값 20)

---

## 5. 수정 파일 요약

### aads-server (백엔드)
| 파일 | 변경 유형 |
|------|-----------|
| `app/api/ceo_chat_tools.py` | 수정 — 9개 원격 도구 추가 |
| `app/api/ceo_chat.py` | 수정 — 메모리 주입 코드 |
| `app/routers/chat.py` | 수정 — 에러 리포팅 엔드포인트 |
| `app/services/tool_executor.py` | 수정 — 11개 핸들러 추가 |
| `app/services/tool_registry.py` | 수정 — 10개 도구 스키마 + meta 그룹 |
| `app/services/agent_sdk_service.py` | 수정 — 도구 래퍼 + 등급 + 제한 확장 |
| `app/services/agent_hooks.py` | 수정 — 원격 쓰기 보안 훅 |
| `app/services/compaction_service.py` | 수정 — 환경변수화 |
| `app/services/code_indexer_service.py` | 수정 — 독스트링 모델명 |
| `app/services/subagent_service.py` | **신규** — 서브에이전트 서비스 |
| `mcp_servers/git_server.py` | 수정 — 5개 쓰기 도구 |

### aads-dashboard (프론트엔드)
| 파일 | 변경 유형 |
|------|-----------|
| `src/services/errorReporter.ts` | **신규** — 에러 리포팅 |
| `src/services/streamManager.ts` | **신규** — 멀티세션 스트리밍 |
| `src/hooks/useChatSSE.ts` | 수정 — StreamManager + errorReporter 통합 |
| `src/hooks/useChatSession.ts` | 수정 — 백그라운드 스트림 API |
| `src/components/ClientLayout.tsx` | 수정 — 글로벌 에러 핸들러 |

---

## 6. 배포 이슈 & 해결

### `/app` vs 바인드마운트 불일치
- **문제**: Docker 컨테이너의 `/app`(COPY)와 바인드마운트(`/root/aads/aads-server`)가 다른 경로
- **해결**: 수정 파일을 `/app`으로 복사 후 `supervisorctl restart aads-api`
- **프론트엔드**: 프로덕션 multi-stage 빌드이므로 `docker compose build --no-cache` 후 재생성

### 에러 엔드포인트 404
- **원인**: 수정한 `chat.py`가 바인드마운트에만 있고 실행 경로(`/app`)에 반영 안 됨
- **해결**: `cp /root/aads/aads-server/app/routers/chat.py /app/app/routers/chat.py` + 재시작

---

## 7. 최종 검증 결과

| 항목 | 결과 |
|------|------|
| 에러 리포팅 (내부) | HTTP 200 ✅ |
| 에러 리포팅 (외부/nginx) | HTTP 200 ✅ |
| StreamManager 컴파일 | 번들 2개 파일 포함 ✅ |
| CEO Chat 메모리 주입 | build_memory_context 2회 참조 ✅ |
| 임베딩 API | dim=3072 정상 ✅ |
| Phase 1 도구 8종 | 전체 OK ✅ |
| SDK 제한 확장 | 100턴/$50 ✅ |
| 서브에이전트 단일 | haiku 972ms ✅ |
| 서브에이전트 병렬 | 3개 동시 ~740ms ✅ |
| 서브에이전트+도구 | health_check 자율 실행 ✅ |

---

## 8. 백업 위치
- `/root/aads/backups/aads_190_before_20260310/` — Phase 1 이전 5개 원본 파일
