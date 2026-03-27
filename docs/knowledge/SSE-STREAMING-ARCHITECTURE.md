# SSE Streaming Architecture — AADS CEO Chat

_v2.0 | 2026-03-27_

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

## 타임아웃 정렬표
| 구간 | 값 |
|------|-----|
| Cloudflare Proxy Read | 120s (heartbeat로 회피) |
| Frontend sseTimeout | 90s |
| Frontend maxStreamTimeout | 3600s (1시간) |
| Server heartbeat (평시) | 3s |
| Server heartbeat (도구) | 2s |
| stream-resume 타임아웃 | 120s |
| waitingBg 안전장치 | 30s |

## [recovered] 2가지 구분
1. AADS SSE 끊김 → 6계층 방어로 해결
2. AI 에이전트 컨텍스트 초과 → SSE와 무관, 긴 세션에서 발생

## 버전 이력
| v1.0 | 2026-03-27 | 초기 생성 |
| v2.0 | 2026-03-27 | 7건 추가 수정, heartbeat 패딩 |
