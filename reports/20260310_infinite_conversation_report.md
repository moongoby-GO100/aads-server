# AADS 무한 대화 + 자동 컨텍스트 관리 시스템 구현 보고서

**작업일**: 2026-03-10
**Directive**: 무한 대화 + 자동 컨텍스트 관리

---

## 구현 항목 (6/6 완료)

### 1. 도구 출력 자동 압축 ✅
- **파일**: `app/services/context_compressor.py` (신규, 300줄)
- `compress_tool_output(tool_name, raw)` — 도구별 규칙 기반 압축 (LLM 호출 없음)
  - health_check → 상태 1줄 요약
  - read_remote_file → 앞 80줄 + 뒤 20줄 (중간 생략)
  - query_database → 첫 30행만
  - list_remote_dir → 첫 50항목
  - 기본 → 2000자 절단
  - 에러 메시지는 항상 전체 보존
- **적용 위치**: `model_selector.py` 도구 루프 내 — 프론트에는 원본, 컨텍스트에는 압축본

### 2. Observation Masking ✅
- **파일**: `context_compressor.py` + `context_builder.py`
- 슬라이딩 윈도우 (기본 10턴)
- 이전 턴: `[도구 결과: {tool_name} — 상세 내용 생략]` 플레이스홀더
- AI 추론/결정 텍스트는 100% 보존
- 60K 토큰 초과 시 공격적 마스킹 (window=5)

### 3. 자동 구조화 요약 ✅
- **파일**: `compaction_service.py` (전면 개편)
- 6섹션 강제 템플릿: 현재 목표 / 수정된 파일 / 내려진 결정 / 실패한 접근 / 보류 작업 / 활성 Directive
- 증분 병합 (`_merge_summaries`): 기존 요약에 새 정보를 Haiku로 병합
- 80K 토큰 초과 시 `context_builder`에서 자동 트리거
- 도구 raw 출력 사전 정리 후 요약 (토큰 절약)

### 4. 턴/예산 제한 제거 ✅
- Agent SDK: `_MAX_TURNS=0`, `_MAX_BUDGET_USD=0` (0 = 무제한)
- 도구 루프: 5 → **20턴** (`MAX_TOOL_TURNS` 환경변수)
- 히스토리 조회: 25 → **200 메시지** (무한 대화 지원)
- `_build_layer3_messages()`: 20개 제한 제거 → 전체 메시지 사용 + observation masking
- **비용 표시**: done 이벤트에 `session_cost`, `session_turns` 포함
  - 프론트엔드 StreamState에 `sessionCost`, `sessionTurns` 추가

### 5. Yellow 도구 연속 실행 제한 ✅
- **파일**: `model_selector.py`
- Yellow 등급 도구 11종 연속 5회 이상 → `yellow_limit` SSE 이벤트 발행
- 프론트엔드 StreamState에 `yellowLimitWarning` 추가
- 경고만 (차단 아님) — CEO가 UI에서 확인 가능
- 읽기/분석 도구는 제한 없음

### 6. Prompt Caching 적용 ✅
- **시스템 프롬프트**: Layer 1 (정적) → `cache_control: ephemeral` (기존 유지)
- **도구 정의**: `build_cached_tools()` 적용 — 마지막 도구에 cache_control
  - 1024 토큰 이상일 때만 활성화 (프로덕션 20+ 도구 시 자동 적용)
- **캐시 히트율 로깅**: `prompt_cache: read=N create=N input=N turn=N`
- `cache_read_input_tokens`, `cache_creation_input_tokens` 추적

---

## 수정 파일 요약

| 파일 | 변경 |
|------|------|
| `app/services/context_compressor.py` | **신규** — 도구 압축 + observation masking + 토큰 추정 |
| `app/services/compaction_service.py` | **전면 개편** — 구조화 템플릿 + 증분 병합 |
| `app/services/context_builder.py` | 수정 — 히스토리 제한 제거 + observation masking + 80K 자동 요약 |
| `app/services/model_selector.py` | 수정 — 도구 루프 20턴 + Yellow 제한 + 도구 캐싱 + 캐시 로그 |
| `app/services/chat_service.py` | 수정 — 히스토리 200개 + 세션 비용 SSE |
| `app/services/agent_sdk_service.py` | 수정 — 턴/예산 0(무제한) |
| `src/services/chatApi.ts` | 수정 — SSEChunk yellow_limit + session_cost/turns |
| `src/hooks/useChatSSE.ts` | 수정 — StreamState 확장 + yellow_limit/session_cost 처리 |

---

## 테스트 결과

| 항목 | 결과 |
|------|------|
| 도구 압축 (health_check) | 1줄 요약 ✅ |
| 도구 압축 (read_file 200줄) | 5489 → 2735자 ✅ |
| Observation masking (15턴) | 이전: 마스킹, 최근: 보존 ✅ |
| 토큰 추정 | 12K 텍스트 → 3006 토큰 ✅ |
| 구조화 요약 6섹션 | 템플릿 확인 ✅ |
| Agent SDK 제한 | 0/0 (무제한) ✅ |
| 도구 루프 | 20턴 ✅ |
| SSE session_cost | `$0.00 | 7턴` ✅ |
| 서버 시작 | 에러 없음 ✅ |
| 프론트엔드 빌드 | 성공 ✅ |

---

## 추가 개선 의견

### 즉시 적용 가능
1. **프론트엔드 비용 표시 UI**: `sessionCost`/`sessionTurns`를 채팅 입력란 옆에 `$1.23 | 27턴` 형태로 표시 — CSS만 추가하면 됨
2. **Yellow 도구 확인 UI**: `yellowLimitWarning` 시 [계속]/[중단] 버튼 표시 — 프론트 컴포넌트 추가 필요

### 중기 개선
3. **적응형 Observation Window**: 토큰 사용량에 따라 window를 10→5→3으로 동적 조정
4. **도구 결과 캐싱**: 동일 도구+입력 조합의 결과를 Redis에 캐싱 (30분 TTL) — re-fetch 비용 절감
5. **선택적 Extended Thinking**: 복잡도 높은 턴에만 Opus+Thinking 사용, 일반 대화는 Sonnet — 비용 50% 절감
6. **세션 자동 이어가기**: 세션이 끊겨도 구조화 요약이 자동 주입되어 맥락 유지 (현재 memory_recall로 부분 구현)

### 장기 개선
7. **RAG 기반 도구 결과 검색**: 마스킹된 도구 결과를 벡터DB에 저장, AI가 필요할 때 검색 → Factory.ai의 re-fetch 문제 해결
8. **멀티모달 컨텍스트**: 스크린샷/차트 이미지를 컨텍스트에 포함할 때의 토큰 관리
