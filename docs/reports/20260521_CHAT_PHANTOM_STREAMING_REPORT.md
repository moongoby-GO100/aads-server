# CEO 채팅 응답버블 미출현 원인분석 + 전체 UX 검수 보고서

- 작성일: 2026-05-21
- 대상 세션: `ac5278a7-2f13-4cd7-9aa1-83d41fb23c97`
- URL: https://aads.newtalk.kr/chat#ac5278a7-2f13-4cd7-9aa1-83d41fb23c97
- 검수 범위: 백엔드 SSE 흐름, 프론트엔드 채팅 페이지, DB 메시지/실행 상태

---

## 1. 문제 진단 — "응답버블이 안 나오는" 근본 원인

### 1.1 관측 데이터 (실측)
| 항목 | 값 | 해석 |
|---|---|---|
| `GET /chat/messages?session_id=ac5278a7…` | 50건 | 메시지 자체는 전부 DB에 있음 |
| 마지막 메시지 (`[49]`) | role=user, `[추가 지시] 빨리 조치해`, 2026-05-21 00:59:34 | user가 마지막 — AI 응답 미생성 |
| 마지막 assistant (`[47]`) | 098b3b5e, 21,874자, 2026-05-21 00:20:10 | 정상 완료된 마지막 응답 |
| `streaming-status` 응답 | `is_streaming=true`, `execution_id=null`, `content_length=0`, `partial_content=null`, `placeholder_revision="0:0"` | **유령 상태** — 진짜로는 아무것도 안 돌아감 |
| `GET /chat/sessions/.../execution` | `404 execution not found` | DB상 진행 실행 없음 |
| `GET /chat/sessions/.../last-response` | `{"found": false}` | DB상 신규 응답 없음 |

### 1.2 핵심 원인 — **Phantom Streaming (유령 스트리밍 상태)**

백엔드 인메모리 사전 `_active_bg_tasks[session_id]`에 asyncio Task 객체가 남아있지만 실제로는 진행되지 않는 상태. DB 어디에도 흔적이 없고(placeholder/execution 모두 부재) finally 블록의 `set_streaming(False)`가 호출된 적이 없음.

이 상태가 만들어내는 연쇄 장애:

1. `app/services/chat_service.py:3485-3501` — `get_streaming_status`는 task가 있고 `task.done()` 이 False면 **무조건** `is_streaming=true`로 반환. **타임아웃/만료 안전망 없음** (참고: `_streaming_state` 경로(L3454)는 10분 만료가 있지만 task-only 경로는 없음).
2. `app/routers/chat.py:711-722` — `POST /chat/messages/send` 진입 시 `is_streaming(sid)` True면 새 SSE 스트림을 만들지 않고 `{"status":"interrupt_queued"}` JSON만 반환.
3. `aads-dashboard/src/app/chat/page.tsx:3197-3500` — 프론트는 SSE 텍스트(`data: …`)를 기대. JSON 한 줄짜리 응답은 모든 라인이 `data:`로 시작하지 않아 이벤트가 0개 처리되고 `gotFinal=false, full=""` 상태로 reader가 종료.
4. 같은 함수 L3572 — fallback으로 `/chat/messages` 폴링을 3회(3+6+9s) 시도. DB에 신규 assistant가 없으므로 침묵 종료.
5. 별도 폴링(L2489 setInterval) — `streaming-status`가 `is_streaming=true`만 계속 응답하므로 `waitingBgResponse` 유지, **placeholder가 없어서 표시할 버블도 없음** → 사용자에게는 **AI 응답 버블이 영영 나타나지 않음**.

### 1.3 왜 phantom 상태가 만들어졌나 (가설)
- `send_message_stream`의 try 블록에서 `set_streaming(True)` 후 LLM 호출 단계에서 producer task가 무한 대기/예외 미캐치로 `finally`까지 못 가는 케이스.
- `with_background_completion`이 클라이언트 끊김 후 백그라운드로 task를 살리지만, 백그라운드 task 자체가 hang하면 `_BG_AUTO_CANCEL_SEC=900s`(15분)이 효력을 발휘하지 못하는 경로 존재 가능 (재시도 루프/heartbeat 펌프 deadlock 등).
- 직전 동일 세션에서 stale 실행을 cleanup하면서 task 객체는 dict에 남았을 가능성도 있음(L2403/2507/3489 등 `.pop` 호출이 누락된 코드 경로).

### 1.4 즉시 조치(이 세션 한정 복구)
다음 1줄로 phantom 상태를 클리어할 수 있음(검증 후 실행):
```bash
curl -X POST -H "X-Monitor-Key: $AADS_MONITOR_KEY" \
  https://aads.newtalk.kr/api/v1/chat/sessions/ac5278a7-2f13-4cd7-9aa1-83d41fb23c97/stop
```
→ `_active_bg_tasks.pop`, `_streaming_state.pop`, `set_streaming(False)` 모두 호출됨(`chat_service.py:2859-2873`). 이후 사용자가 새 메시지 보내면 정상 SSE 흐름.

> 보고서 작성 시점 기준 미실행. CEO 승인 후 실행 권장.

---

## 2. 채팅창 전체 UX 검수 (사용자 관점)

채팅 페이지 `aads-dashboard/src/app/chat/page.tsx` (6,588 LOC) + 백엔드 `app/routers/chat.py` (2,415 LOC) 검수.

### 2.1 치명적 문제 (P0)
| # | 항목 | 현상 | 사용자 영향 |
|---|---|---|---|
| **P0-1** | Phantom streaming 복구 불가 | 위 1.2 | 새로고침해도 응답 안 나오고 무한 대기 |
| **P0-2** | `interrupt_queued` 응답 무처리 | 프론트가 SSE만 기대 → JSON 응답 무시, 토스트/안내 0건 | 사용자는 "보냈는데 아무 반응 없음" → 같은 메시지 재전송 → 누적 |
| **P0-3** | Stop 버튼 노출 조건 좁음 | `streaming && !hasInput && pendingFiles.length===0` 조건일 때만 (L6205, L6390) | 새로고침 후 polling이 `is_streaming=true`만 받으면 stop 버튼 없음 → 빠져나갈 방법 없음 |

### 2.2 중요 문제 (P1)
| # | 항목 | 현상 | 사용자 영향 |
|---|---|---|---|
| **P1-1** | "응답 확인 중..." 진행도 부재 | `waitingBgResponse=true` 시 toast 1줄만 있고 진행도/예상시간 없음 | 60-120초 동안 진행 여부 불확실 |
| **P1-2** | 메시지 큐(`msgQueueRef`) 누적 | 응답 안 나오면 사용자가 여러 번 입력 → 다음 정상 턴에 `[이전 추가 지시]`로 일괄 주입 (`chat_service.py:5299-5303`) | 한 응답에 3-5건 지시 합쳐져 LLM이 혼란 / 토큰 폭증 |
| **P1-3** | 폴링 reload 일관성 | `is_streaming=true`+placeholder 없음 케이스에서 빈 placeholder 만들지 않음 → 화면에 아무것도 안 보임 (대시보드 L2218-2245) | 사용자는 "스피너조차 안 보임" |
| **P1-4** | 부분 응답 보존 임계값 | `_has_meaningful_partial_content`(현재 기준 미공개, 과거 30자) — 짧은 응답은 silently 삭제 가능 | 짧지만 의미있는 답변(예: "예", "확인했습니다") 유실 |
| **P1-5** | 자동 트리거 무한 루프 위험 | `interrupt_queued` → 프론트가 60초 폴링 → 또 시도하는 사용자 패턴 → 큐 누적 | 같은 메시지 N건 DB 저장 |

### 2.3 일반 문제 (P2)
| # | 항목 | 현상 |
|---|---|---|
| **P2-1** | 스크롤 점프 | 초기 로드 시 `ResizeObserver` + setTimeout 3s (L2410-2419) — 도중 사용자 scroll이 reset됨 |
| **P2-2** | "분석 중..." placeholder 깜빡 | `stream_start`에서 `setStreamBuf("분석 중...")` 즉시 표시(L3254) — 100ms 안에 첫 delta 와도 한 번 깜빡임 |
| **P2-3** | 토큰 드레인 30ms 고정 (L3211-3217) | 매우 짧은 응답에서 끝까지 표시 지연 50-150ms |
| **P2-4** | 에러 메시지 위치 분산 | `yellowWarning`(상단 노란 바), toast, alert 3종 혼용 — 어느 채널인지 사용자 학습 필요 |
| **P2-5** | 접근성 | role/aria-* 없음, 키보드 단축키 안내 없음(Enter/Shift+Enter만) |
| **P2-6** | placeholder content 메시지 | "⏳ AI가 응답을 생성 중입니다…" 정적 텍스트 — 도구 진행 정보 없음 |
| **P2-7** | 모바일 sticky input | `pendingPreviewFiles` 표시 영역과 입력창 z-index 충돌 가능성 |

### 2.4 좋은 점 (유지할 항목)
- **revision 기반 폴링 최적화** (`message_revision`, `placeholder_revision`) — 변경 없으면 fetch skip → 트래픽 절감.
- **in-place 교체** — placeholder id 유지로 새 버블 생성 방지(L3343-3370 등) — 버블 중복 문제 대부분 해결됨.
- **idempotency_key** — 502/503/504 재시도 시 중복 저장 방지(L3137).
- **stream_id, Last-Event-ID** — SSE 끊김 복구 지원.
- **fallback 폴링 3회** — 도구만 실행되고 텍스트 없을 때 DB 복구.
- **재진입 partial_content 즉시 표시** — 세션 복귀 시 빈 버블 방지(L2227-2230).

---

## 3. 개선안 (우선순위별)

### 3.1 P0 — 즉시 (1일 이내)

**[P0-A] phantom streaming 자가복구**  
파일: `app/services/chat_service.py`

`get_streaming_status` 내 `_active_bg_tasks` 경로(L3485-3501)에 stale 감지 추가:
```python
if session_id in _active_bg_tasks:
    task = _active_bg_tasks[session_id]
    if task.done():
        _active_bg_tasks.pop(session_id, None)
        _streaming_state.pop(session_id, None)
        try: set_streaming(session_id, False)  # interrupt_queue 동기화
        except Exception: pass
    else:
        state = _streaming_state.get(session_id, {})
        _last_event = state.get("last_event_at") or state.get("started_at") or 0
        _age = _bg_time.monotonic() - _last_event if _last_event else 0
        # 신규: 90초간 어떤 이벤트도 없고 placeholder/execution_id도 없으면 phantom으로 판단
        _is_phantom = (
            _age > 90
            and not state.get("execution_id")
            and not state.get("content")
        )
        if _is_phantom:
            logger.warning("phantom_streaming_cleared session=%s age=%.0fs", session_id[:8], _age)
            task.cancel()
            _active_bg_tasks.pop(session_id, None)
            _streaming_state.pop(session_id, None)
            try: set_streaming(session_id, False)
            except Exception: pass
            return {"is_streaming": False, "just_completed": False, ...}
        return {"is_streaming": True, ...}
```
효과: 새로고침 1회로 phantom 자동 정리 → 다음 메시지 정상 처리.

**[P0-B] send_message에서 phantom 강제정리**  
파일: `app/routers/chat.py:711-722`

`is_streaming(sid)` True면서 (a) `_active_bg_tasks` 비었거나 (b) task.done() 또는 (c) 마지막 이벤트 후 90초 경과면 → `_set_interrupt_streaming(sid, False)` + `_active_bg_tasks.pop` 후 정상 send 진행. interrupt_queued 반환은 진짜 streaming일 때만.

**[P0-C] 프론트엔드 `interrupt_queued` 응답 처리**  
파일: `aads-dashboard/src/app/chat/page.tsx` (L3165 직후)

```ts
const _ct = res.headers.get("content-type") || "";
if (_ct.includes("application/json")) {
  const data = await res.json();
  if (data?.status === "interrupt_queued") {
    setYellowWarning("이전 응답이 아직 진행 중입니다. [중단하고 새로 시작] 버튼을 눌러주세요.");
    setForceShowStop(true);  // 신규 state — stop 버튼 강제 노출
    setStreaming(false); setStreamBuf("");
    return;  // SSE 파싱 스킵
  }
}
```

### 3.2 P1 — 단기 (1주 이내)

- **[P1-A] Stop 버튼 노출 조건 확장**: `streaming || waitingBgResponse || forceShowStop || (statusIsStreaming && stalePartial)` — 사용자가 항상 "탈출구"를 가지도록.
- **[P1-B] 백그라운드 watchdog** (`app/services/chat_service.py` 신규): 5분마다 `_active_bg_tasks` 스캔 — task.done()인데 dict에 남은 항목 / 90초간 무이벤트 항목 정리. `_streaming_state.started_at` 활용.
- **[P1-C] 메시지 큐 자동 비우기**: phantom 감지 시 `msgQueueRef`도 비워서 누적 방지.
- **[P1-D] "응답 확인 중" 진행도 표기**: 폴링 회수/경과 시간 노출, 30초 이상 정체 시 stop+재시도 옵션.
- **[P1-E] partial content 기준 명문화**: 1자~30자 사이도 보존되, "응답이 매우 짧게 끝났습니다" 라벨로 표시.
- **[P1-F] 모니터링**: `phantom_streaming_cleared` 카운터 → 5분당 3건 초과 시 텔레그램 알림(watchdog 규칙).

### 3.3 P2 — 중기 (2주 이내)

- **[P2-A] 토큰 드레인 30ms → 적응형**: 큐 길이 기반(짧으면 즉시, 길면 천천히).
- **[P2-B] "분석 중..." → skeleton bubble**: 텍스트 깜빡임 대신 회색 라인 3개 placeholder.
- **[P2-C] 도구 진행 상세화**: placeholder 메시지에 "🔧 read_remote_file 실행 중 (3/5)" 표시.
- **[P2-D] 접근성**: `role="log"`, `aria-live="polite"` (메시지 컨테이너), 단축키(`Ctrl+Enter`=전송, `Esc`=중지) 추가.
- **[P2-E] 에러 채널 통일**: yellowWarning/toast/alert → 단일 inline alert 컴포넌트.
- **[P2-F] 초기 스크롤 fix**: ResizeObserver 3초 → DOM mutation 종료 감지로 변경, 사용자 wheel 입력 시 즉시 해제.

---

## 4. 실측 데이터 부록

### 4.1 streaming-status 응답 (현재)
```json
{
  "is_streaming": true,
  "just_completed": false,
  "content_length": 0,
  "partial_content": null,
  "execution_id": null,
  "last_event_id": null,
  "last_message_id": "4cd37a34-d9ed-49c1-bb3f-02662a68ea94",
  "message_revision": "358:1779325174587443",
  "placeholder_revision": "0:0",
  "artifact_revision": "310:1779322895418731"
}
```
→ `partial_content=null` + `placeholder_revision="0:0"`은 “DB에 placeholder 행도 없음”을 의미. 그런데 `is_streaming=true` → in-memory `_active_bg_tasks`만 phantom으로 살아있다는 강력한 증거.

### 4.2 메시지 통계 (마지막 8건)
```
[42] 2026-05-20T09:34:34 user      len=51    | 그리고 채팅에 지시하면 브라우져가 멈추는 현상…
[43] 2026-05-20T10:21:47 assistant len=61    | ⚠️ 응답 생성이 중단되어 복구 응답을 만들지 못했습니다…
[44] 2026-05-20T22:36:58 user      len=2125  | [CEO가 지정한 이전 AI 응답 (reply_to)]…
[45] 2026-05-20T23:55:19 assistant len=21874 | 즉시 현재 코드 상태를 실측해서 미조치…
[46] 2026-05-21T00:00:44 user      len=73    | 위 대화 흐름 확인하고 중간 실시간 응답버블이…
[47] 2026-05-21T00:20:10 assistant len=3801  | 즉시 원인을 추적합니다. DB 저장 상태…
[48] 2026-05-21T00:27:46 user      len=174   | 1. 응답이 완료되었는데 새로고침을 안하면…
[49] 2026-05-21T00:59:34 user      len=14    | [추가 지시] 빨리 조치해
```
→ [48] 이후 32분간 AI 응답 0건. [49]의 "빨리 조치해" 메시지는 phantom 상태로 인해 `interrupt_queued`로 처리되어 큐에만 들어감(가설).

---

## 5. 권장 실행 순서

1. **CEO 승인** → 이 세션 `/stop` 호출로 phantom 즉시 정리 (사용자 즉시 복구)
2. **P0-A/B/C 패치** 동시 적용 (서버 hot reload + 대시보드 재배포)
3. **24시간 모니터링** — `phantom_streaming_cleared` 발생률, 사용자 보고
4. P1 항목 1주 내 순차 진행
5. P2 항목은 다음 스프린트

## 교훈

- **인메모리 상태 + finally 의존**은 위험. asyncio Task가 hang/cancel되면 finally는 즉시 실행되지 않을 수 있음 → 명시적 watchdog 필수.
- **JSON ≠ SSE**. 같은 엔드포인트가 분기에 따라 다른 미디어 타입을 반환하면 클라이언트가 반드시 Content-Type 확인 후 분기해야 함.
- **사용자 탈출구**(stop/cancel/refresh)는 어떤 상태에서도 누를 수 있어야 함. 조건부 노출은 stuck 상태 발생 시 사용자를 가둠.
