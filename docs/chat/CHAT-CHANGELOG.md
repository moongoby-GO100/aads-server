# AADS Chat 변경 이력

_v1.0 | 2026-04-02 | 최초 작성_

## 변경 이력 (최신순)

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
