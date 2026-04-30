# AADS Chat 변경 이력

_v1.0 | 2026-04-02 | 최초 작성_

## 변경 이력 (최신순)

### 2026-04-30

| 커밋 | 변경 | 구분 |
|------|------|------|
| 2026-04-30 | 스트리밍 중 active API 재시작 방지: `deploy.sh code`가 active stream 감지 시 peer slot을 먼저 재시작하고 nginx/복구 오너를 전환하도록 변경 | 🐛 Deploy |
| 2026-04-30 | blue/green 복구 오너 분리: inactive 컨테이너가 DB running/retrying 실행을 가로채지 않도록 active marker/env/owner flag 적용 | 🐛 Backend |
| 2026-04-30 | 첫 토큰 지연 구간 보존: `stream_start` 직후 DB placeholder 저장, heartbeat 중 10초 주기 interim save | 🐛 Backend |
| 2026-04-30 | 끊김 후 이전 응답 반환 방지: `last-response`를 현재 execution/message 기준으로 좁히고 system-trigger turn fallback 오판 수정 | 🐛 Backend |
| 2026-04-30 | 프론트 메시지 병합 보강: streaming placeholder와 최종 assistant를 같은 render key로 병합해 사라짐/중복 버블을 줄임 | 🐛 Frontend |

검증:
- `python3 -m pytest tests/unit/test_chat_service.py -q` → 10 passed
- `python3 /tmp/aads_stream_disconnect_e2e.py` → 강제 끊김 후 `resume_done`, assistant 1개, placeholder 0개, `current_execution_id=null`, 중복 replay 없음
- 브라우저 직접 확인: `https://aads.newtalk.kr/chat#e62f3c19-5558-4f89-87bf-709c7dccd4af` 로딩, chat/session/message/streaming-status API 200, `current_execution_id=null`

운영 조치:
- 2026-04-30 19:51 KST, 진행 중이던 `deploy.sh code`가 active API를 `STOPPING` 상태로 만들며 응답 끊김을 재현했다.
- 즉시 API upstream을 `8102(aads-server-green)`으로 failover하고 `.active_container/.active_port` 및 `/tmp/aads_execution_resume_owner`를 green 기준으로 전환했다.
- `claude-relay` 재시작으로 relay semaphore timeout 패치를 런타임에 반영했다.

### 2026-04-28

| 커밋 | 변경 | 구분 |
|------|------|------|
| `b24b47f` | **BUG #3**: streaming-status DB fallback에서 tool_count/last_tool 산출 (tools_called JSON parse) | 🐛 Backend |
| `56ed27c` (dashboard) | **Patch A+B**: URL 재진입 시 SSE 도구/사고/스트리밍 누락 해결. attachExecutionReplay 18종 SSE 핸들러 + partial_content 즉시 표시 | 🐛 Frontend |

증상: `https://aads.newtalk.kr/chat#{session_id}` 같은 진행 중 세션 재진입 시 도구 카드/사고 블록/스트리밍이 보이지 않고 빈 버블만 표시되던 문제. 백엔드는 18종 SSE를 정상 발행 중이었으나 attachExecutionReplay가 1/4(delta/heartbeat/done)만 처리해 정보 드롭. 또 streaming-status가 in-memory state 없을 때 tool_count=0/last_tool=""을 하드코딩 반환해 진입 시점 도구 진행 상태도 미표시.

해결:
- Backend: tools_called JSON에서 tool_use 카운트와 마지막 도구명 산출 (running/just_completed/placeholder-only 3개 분기)
- Frontend Patch A: status.partial_content를 즉시 setStreamBuf, status.tool_count/last_tool를 즉시 setToolStatus
- Frontend Patch B: attachExecutionReplay에 stream_start/stream_reset/tool_use/tool_result/thinking/yellow_limit/model_info/sdk_*/error + done 핸들러 추가 (sendMessage 메인 루프와 동등)

영향: 다른 워커/브라우저에서 진행 중 세션을 URL로 열 때 즉시 "🔧 X 실행 중..." + 도구 카드 + 사고 블록 + 스트리밍 텍스트 모두 정상 표시. SSE-STREAMING-ARCHITECTURE.md v2.1로 버전업.

### 2026-04-24

- 운영 조치: `claude-relay` 전역 동시성은 Pipeline Runner와 별개로 관리하며, live systemd override 기준 `max_concurrent=5`로 고정했다.
- 운영 조치: relay wrapper는 `.active_container`를 읽어 blue-green 활성 API 컨테이너를 따라가도록 보강했다. 배포 직후 inactive 컨테이너를 참조하며 MCP preflight가 실패하던 리스크를 낮췄다.
- 운영 조치: active stream 계측을 `executing / visible / recovery_pending / recent_placeholders`로 재정리했고, 실제 무중단 배포 drain에서 `2 → 1 → 0` 집계를 확인했다.

### 2026-04-02

| 커밋 | 변경 | 구분 |
|------|------|------|
| `e0b896d` | **A-1~A-4 끊김 시 새 버블 생성 방지** — 같은 버블에서 부드럽게 전환, rAF 교체, 복구 UI | 🔧 Frontend |
| `62f2fe7` | **A-2 offset→cursor 통일, A-3 타이머 cleanup, C-1 스켈레톤 UI** | 🔧 Frontend |

### 2026-03 (AADS-191 Redis Stream + SSE 안정화)

| 커밋 | 변경 | 구분 |
|------|------|------|
| `cd35304` | **AADS-191 Phase4 워커분리** — Redis Stream SSE 전송 분리 + Last-Event-ID + 프론트 버퍼링 | ✨ Backend+Frontend |
| `900f6a5` | **AADS-191 Phase1 Redis Stream 토큰 버퍼링** — 서버 재시작 시 스트리밍 복구 | ✨ Backend |
| `20ad11a` | **Phase4 프론트엔드** — 토큰 버퍼링 + Last-Event-ID SSE 재연결 | ✨ Frontend |
| `0d09965` | **CEO-019 SSE 끊김방지** — 아젠다/기술문서/개선보고서 + heartbeat 256byte CF flush 패딩 | 📝 Docs+Fix |
| `817dee1` | recovered 메시지 tool UI 복원 — placeholder에 tool_events 축적 + 인라인 렌더 | 🔧 Frontend |
| `8e3e68c` | 스트리밍 버블 2개 + 끊김 후 대화 이어가기 근본 수정 | 🐛 Fix |
| `da4071b` | recovered 후 대화종료 + 중복폭발 + 고아 placeholder 근본 수정 | 🐛 Fix |
| `29eb474` | 3단계 근본 수정 — DB unique index + promote dedup + idempotency key | 🐛 Fix |
| `18eaaea` | 사용자 메시지 중복 방지(30s dedup) + recovered 연속 중복 자동 정리 | 🐛 Fix |
| `a93f112` | streaming_placeholder 숨기지 않고 자동 promote — 부분 응답 보존 | 🐛 Fix |
| `cabd5ee` | streaming_placeholder 중복 버블 근본 수정 — 스마트 promote + 15초 cleanup | 🐛 Fix |
| `1565398` | stream-resume stale response bug — message_id validation + UPDATE 방식 | 🐛 Fix |
| `1df4355` | **Invisible Recovery** — SSE 끊김 시 AI 버블 유지 + 무음 재연결 | ✨ Frontend |
| `66e7781` | maxStreamTimeout 900s→3600s (1시간) — 200+ 도구 호출 세션 대응 | 🔧 Frontend |
| `faffdba` | SSE 끊김 후 recovered 응답 멈춤 근본 수정 3건 | 🐛 Fix |
| `edc3a77` | tool UI 접기/펼치기 + 풍부한 미리보기 — details 컴포넌트 | ✨ Frontend |

### 2026-03 (채팅 기능 개선)

| 커밋 | 변경 | 구분 |
|------|------|------|
| `7b32d06` | SearXNG 우선 실행 — Gemini Grounding 체인 앞에 삽입 | ✨ Backend |
| `22e7f6a` | P1-1 대화 중 실시간 교훈 추출 — LLM 제거, 키워드/패턴 기반 | ✨ Backend |
| `d67497a` | P2-1 multimodal memory — visual_memory store and recall | ✨ Backend |
| `8f78334` | logging kwargs TypeError + context_builder intent 파라미터 호환 | 🐛 Fix |
| `5aa92b1` | 대화 잘림·페이지네이션 중복·메시지 3중 표시 버그 수정 | 🐛 Frontend |
| `197b6ff` | 채팅 메시지 사라짐 버그 — 폴링 시 기존 메시지 보존 병합 | 🐛 Frontend |
| `25f3f89` | 채팅 폴링 15초 간격 최적화 (waitingBg=false 시 skip) — CPU 과부하 방지 | ⚡ Frontend |
| `b0e2b3d` | 채팅 UI 폴링 간격 최적화 + 스크롤 점프 수정 | 🔧 Frontend |
| `96a289e` | UI: user msg 버튼 하단 이동, edit textarea 크기 수정 | 🎨 Frontend |

### Dashboard Git 이력 (src/app/chat/)

```
e0b896d fix: 채팅 끊김 시 새 버블 생성 방지 (A-1~A-4)
62f2fe7 fix: A-2 offset→cursor, A-3 timer cleanup, C-1 skeleton
20ad11a feat: Phase4 프론트 — 토큰 버퍼링 + Last-Event-ID
5aa92b1 fix: 대화 잘림/중복/3중표시
197b6ff fix: 메시지 사라짐 — 폴링 보존 병합
25f3f89 perf: 폴링 15초 최적화
b0e2b3d fix: 폴링+스크롤 점프
66e7781 fix: maxStreamTimeout 3600s
faffdba fix: recovered 응답 멈춤 3건
edc3a77 feat: tool UI 접기/펼치기
```

## 관련 보고서

| 문서 | 경로 | 내용 |
|------|------|------|
| CEO-019 SSE 개선 | `docs/reports/CEO-019-SSE-IMPROVEMENT-REPORT.md` | SSE 끊김방지 13건 수정 보고 |
| SSE 아키텍처 | `docs/knowledge/SSE-STREAMING-ARCHITECTURE.md` | 6계층 방어 기술 문서 (v2.0) |
| SSE 신뢰성 아젠다 | `docs/agenda/AADS-SSE-STREAMING-RELIABILITY.md` | SSE 안정화 아젠다 |

## 이슈 태그 범례

| 태그 | 의미 |
|------|------|
| ✨ | 새 기능 (feat) |
| 🔧 | 개선 (improve) |
| 🐛 | 버그 수정 (fix) |
| ⚡ | 성능 개선 (perf) |
| 🎨 | UI/UX 개선 |
| 📝 | 문서 |

## 문서 버전 이력

| 버전 | 날짜 | 변경 |
|------|------|------|
| v1.0 | 2026-04-02 | 초기 작성 — 2026-03~04 변경 이력 통합 |
