# SSE Streaming Architecture — AADS CEO Chat

_v2.2 | 2026-04-30_

## 프록시 체인
CEO 브라우저 → Cloudflare(120s) → Nginx(600s) → FastAPI → Anthropic API

## 경쟁사 비교
- ChatGPT/Claude.ai/Gemini: 0-hop (직접 스트리밍, Cloudflare 없음)
- AADS: 4-hop (CF→Nginx→FastAPI→Anthropic) — 구조적 불리

## 6계층 방어 체계
1. Nginx: proxy_buffering off, 600s timeout, keepalive 60s
2. Cloudflare: heartbeat 3s + 256byte 패딩 → 즉시 flush
3. Frontend sseTimeout: 90s (heartbeat마다 리셋)
4. Server heartbeat_pump: 독립 asyncio.Task, 3s/2s
5. Queue 기반 백그라운드: 클라이언트 끊김과 무관하게 응답 완료
6. Invisible Recovery: stream-resume(5회,120s) → polling → waitingBg(30s)
7. **Re-attach Full SSE Replay (v2.1)** — URL 재진입/세션 전환 시 attachExecutionReplay 가
   18종 SSE 이벤트(delta/heartbeat/done + tool_use/tool_result/thinking/stream_start/
   stream_reset/yellow_limit/model_info/sdk_*/error)를 모두 처리. 재진입 시 도구 카드/
   사고 블록/스트리밍 텍스트가 sendMessage 메인 루프와 동등하게 보임.
8. **Deploy-safe Stream Ownership (v2.2)** — active API에 스트림이 있으면 `deploy.sh code`가
   active를 재시작하지 않고 peer slot을 먼저 기동/검증한 뒤 nginx upstream과 resume owner를
   전환한다. inactive 컨테이너는 DB running/retrying execution을 claim하지 않는다.
   `stream_start` 직후 DB placeholder를 즉시 저장하고 heartbeat 중 10초 단위 interim save로
   첫 토큰 지연/도구 대기 중에도 브라우저 재진입 상태를 보존한다.

## SSE 이벤트 18종 (백엔드 → 프론트엔드)
| Type | 의미 | Producer |
|------|------|---------|
| stream_start | 실행 시작 알림 + execution_id | chat_service.send_message_stream |
| model_info | 모델명 | model_selector._stream_anthropic |
| delta | 텍스트 토큰 | model_selector (Claude/LiteLLM) |
| thinking | Extended Thinking 사고 토큰 | model_selector._stream_anthropic |
| tool_use | 도구 호출 (이름+입력) | model_selector tool loop |
| tool_result | 도구 실행 결과 | model_selector tool loop |
| heartbeat | 연결 유지 (+ tool_count/last_tool 메타) | chat_service._heartbeat_pump |
| stream_reset | 응답 재검증 → 텍스트 초기화 | chat_service contradiction guard |
| yellow_limit | 쓰기 도구 연속 한도 경고 | model_selector |
| interrupt_applied | CEO 인터럽트 LLM 반영됨 | model_selector |
| sdk_session/sdk_complete | Agent SDK 진입/종료 | chat_service Agent SDK 경로 |
| diff_preview | Yellow tool diff 승인 요청 | tool_executor |
| message_done | legacy 종료 마커 | chat_service |
| done | 정상 종료 + 비용/토큰 통계 | chat_service |
| error | 에러 (recoverable 플래그) | 전 경로 |

## 진입 경로별 SSE 핸들러
| 경로 | 위치 | 처리 이벤트 |
|------|------|------------|
| 신규 메시지 전송 | aads-dashboard `sendMessage` (page.tsx:2700~) | 18종 전부 |
| 진행중 세션 URL 재진입 | aads-dashboard `attachExecutionReplay` (page.tsx:1322~) | **v2.1부터 18종 전부** (이전엔 3종만) |
| stream-resume (끊김 복구) | `/chat/sessions/{id}/stream-resume` → frontend resume 핸들러 (page.tsx:3100~) | 18종 |

## streaming-status API (재진입 우선)
재진입 시 `GET /chat/sessions/{id}/streaming-status` 응답:
- `is_streaming` / `just_completed` / `recovered`
- `partial_content` — 진행 중 텍스트 (즉시 streamBuf 주입, v2.1)
- `tool_count` / `last_tool` — `tools_called` JSON에서 산출 (v2.1, 이전엔 0/"")
- `execution_id` / `last_event_id` — re-attach용

## 타임아웃 정렬표
| 구간 | 값 |
|------|-----|
| Cloudflare Proxy Read | 120s (heartbeat로 회피) |
| Frontend sseTimeout | 90s |
| Frontend maxStreamTimeout | 3600s (1시간) |
| Server heartbeat (평시) | 3s |
| Server heartbeat (도구) | 2s |
| DB interim save heartbeat | 10s |
| First response grace | 150s |
| stream-resume 타임아웃 | 120s |
| waitingBg 안전장치 | 30s |

## [recovered] 2가지 구분
1. AADS SSE 끊김 → 6계층 방어로 해결
2. AI 에이전트 컨텍스트 초과 → SSE와 무관, 긴 세션에서 발생

## 버전 이력
| 버전 | 일자 | 변경 |
|------|------|------|
| v1.0 | 2026-03-27 | 초기 생성 |
| v2.0 | 2026-03-27 | 7건 추가 수정, heartbeat 패딩 |
| **v2.1** | **2026-04-28** | **Layer 7 추가: Re-attach Full SSE Replay (Patch A+B+BUG#3)**.<br/>이전엔 attachExecutionReplay가 delta/heartbeat/done 3종만 처리해 URL 재진입 시 도구/사고/스트리밍 모두 누락. streaming-status도 tool_count/last_tool 하드코딩 0 반환. 모두 해소. |
| **v2.2** | **2026-04-30** | **Layer 8 추가: Deploy-safe Stream Ownership**.<br/>스트리밍 중 active API 재시작 방지, blue/green resume owner 분리, `stream_start` 즉시 DB placeholder 저장, heartbeat interim save, 강제 끊김 e2e 통과. |
