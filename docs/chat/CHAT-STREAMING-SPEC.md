# AADS Chat Streaming & 끊김 복구 명세

_v1.0 | 2026-04-02 | 최초 작성_

## 1. 프록시 체인

```
CEO 브라우저 → Cloudflare (120s) → Nginx (600s) → FastAPI → Anthropic API
                                                      ↕
                                                Redis Stream (토큰 버퍼)
```

경쟁사(ChatGPT/Claude.ai/Gemini)는 0-hop 직접 스트리밍.  
AADS는 4-hop → 구조적으로 끊김 위험 높음 → 6계층 방어로 보완.

## 2. SSE 스트리밍 아키텍처

### 2.1 Producer-Consumer 분리 (Queue 기반)

```
┌─────────────────────────────────────────────────┐
│ Producer (LLM 호출)                              │
│  call_llm() → token 생성                         │
│  → asyncio.Queue.put(token)   ← Consumer용       │
│  → redis_stream.publish_token() ← 복구용 버퍼     │
│  * 클라이언트 끊김과 무관하게 독립 실행            │
└────────────────┬────────────────────────────────┘
                 │ asyncio.Queue
┌────────────────▼────────────────────────────────┐
│ Consumer (SSE 전송)                              │
│  Queue.get() → SSE event 전송                    │
│  * 클라이언트 끊기면 Consumer만 중단              │
│  * Producer는 계속 실행 → DB에 최종 저장          │
└─────────────────────────────────────────────────┘
```

### 2.2 Redis Stream 토큰 버퍼링 (AADS-191)

```
목적: 서버 재시작 시에도 토큰 보존

LLM 토큰 → [1] asyncio.Queue (실시간 SSE)
          → [2] Redis Stream XADD (영구 버퍼)
                 ├─ key: chat:stream:{session_id}
                 ├─ maxlen: 5000 이벤트/세션
                 ├─ TTL: 1시간 (완료 후 10분)
                 └─ fields: {data, idx, ts, done}

Redis 장애 시: Queue 경로 100% 유지 (Redis는 부가)
```

### 2.3 SSE 이벤트 포맷

```
data: {"type": "token", "content": "안녕"}\n\n
data: {"type": "tool_start", "tool": "query_database", "input": {...}}\n\n
data: {"type": "tool_result", "tool": "query_database", "content": "..."}\n\n
data: {"type": "thinking", "content": "분석 중..."}\n\n
data: {"type": "cost", "input_tokens": 150, "output_tokens": 80, "cost_usd": 0.003}\n\n
data: {"type": "artifact", "artifact_type": "report", "title": "...", "content": "..."}\n\n
data: {"type": "done", "message_id": "uuid"}\n\n
data: {"type": "error", "content": "...", "recoverable": true}\n\n
data: {"type": "heartbeat"}\n\n
```

## 3. 6계층 방어 체계

### Layer 1: Nginx (인프라)

```nginx
proxy_buffering off;
proxy_read_timeout 600s;
keepalive_timeout 60s;
```

### Layer 2: Heartbeat (서버 → 클라이언트)

```python
# chat_service.py — with_heartbeat()
interval = 3s (평시) / 2s (도구 실행 중)
payload = {"type": "heartbeat"} + 256byte 패딩  ← Cloudflare 즉시 flush
```

- Cloudflare는 120s 무응답 시 연결 종료 → heartbeat로 회피
- 256byte 패딩: CF 버퍼링 임계값(~200byte) 초과하여 즉시 전달

### Layer 3: Frontend sseTimeout (클라이언트)

```
sseTimeout = 90s (heartbeat 수신 시 리셋)
maxStreamTimeout = 3600s (1시간 절대 한도)
```

### Layer 4: Invisible Recovery (클라이언트 → 서버)

```
SSE 끊김 감지 (onerror)
  │
  ├─[1] stream-resume (GET /chat/sessions/{id}/stream-resume)
  │     ├─ Last-Event-ID 전송 → Redis Stream에서 이어읽기
  │     ├─ 최대 5회 재시도, 120s 타임아웃
  │     ├─ 성공: 기존 버블에 delta 토큰 이어붙임 (깜빡임 없음)
  │     └─ 실패: Layer 5로
  │
  ├─[2] last-response 폴링 (GET /chat/sessions/{id}/last-response)
  │     ├─ 3회 폴링 (2s → 3s → 4.5s 간격)
  │     ├─ 완성 응답 발견: 기존 버블을 rAF로 교체 (깜빡임 방지)
  │     └─ 미발견: Layer 6으로
  │
  └─[3] waitingBg (30s)
        ├─ 백그라운드 태스크 완료 대기
        └─ 타임아웃: 에러 표시
```

### Layer 5: Queue 기반 백그라운드 완료 (서버)

```python
# with_background_completion()
클라이언트 SSE 끊김
  → Consumer만 중단
  → Producer 계속 실행 (LLM 응답 완료까지)
  → DB에 최종 응답 저장
  → _BG_AUTO_CANCEL_SEC = 300s (5분 후 자동 취소)
```

### Layer 6: 서버 재시작 복구 (서버)

```python
# resume_interrupted_streams()
서버 시작 시 자동 호출 (lifespan)
  → DB에서 intent='streaming_placeholder' 인 메시지 검색
  → Redis Stream에 잔여 토큰 확인
  → _resume_single_stream(): LLM 재호출 → placeholder UPDATE (INSERT 아님!)
```

## 4. 타임아웃 정렬표

| 구간 | 값 | 비고 |
|------|-----|------|
| Cloudflare Proxy Read | 120s | heartbeat 3s로 회피 |
| Nginx proxy_read_timeout | 600s | 충분한 여유 |
| Frontend sseTimeout | 90s | heartbeat마다 리셋 |
| Frontend maxStreamTimeout | 3600s | 절대 한도 (1시간) |
| Server heartbeat (평시) | 3s | CF flush 패딩 포함 |
| Server heartbeat (도구) | 2s | 빈번한 도구 호출 대비 |
| stream-resume 타임아웃 | 120s | AbortController |
| stream-resume 재시도 | 최대 5회 | 지수 백오프 아님 |
| last-response 폴링 | 3회 | 2s → 3s → 4.5s |
| waitingBg 안전장치 | 30s | 최종 방어선 |
| BG_AUTO_CANCEL_SEC | 300s | 5분 후 백그라운드 자동 취소 |
| Redis Stream TTL | 3600s | 완료 후 600s |

## 5. 끊김 시 사용자 체감 (2026-04-02 A+B 적용 후)

| 상황 | 복구 시간 | 사용자 체감 |
|------|----------|------------|
| SSE 일시 끊김 | 1~3초 | ❌ 인지 불가 (기존 버블 유지, 토큰 이어붙임) |
| API 서버 재시작 | 5~15초 | "🔄 응답 복구 중..." → 이어서 표시 |
| 전체 다운 30초 | 30초 | "🔄" → 같은 버블에서 재개 |
| 전체 다운 1분+ | 1분+ | 같은 버블에 "응답 재생성 중..." → 완료 후 교체 |
| LLM API 한도 초과 | 즉시 | 에러 메시지 표시 + fallback 모델 전환 |

## 6. 관련 코드 위치

| 기능 | 파일 | 함수/영역 |
|------|------|----------|
| SSE 생성 | chat_service.py | `send_message_stream()` L2028 |
| Heartbeat | chat_service.py | `with_heartbeat()` L38 |
| 백그라운드 완료 | chat_service.py | `with_background_completion()` L227 |
| Redis 토큰 버퍼 | redis_stream.py | `publish_token()` / `mark_stream_done()` |
| stream-resume | routers/chat.py | `stream_resume()` L322 |
| last-response | routers/chat.py | `get_last_response()` L419 |
| 서버 복구 | chat_service.py | `resume_interrupted_streams()` L544 |
| 프론트 SSE | page.tsx | `connectSSE()` / `invisibleRecovery()` |
| 프론트 버블 유지 | page.tsx | A-1~A-4 (2026-04-02) |

## 버전 이력

| 버전 | 날짜 | 변경 |
|------|------|------|
| v1.0 | 2026-04-02 | 초기 작성 — 6계층 방어, 타임아웃표, A+B 적용 반영 |
