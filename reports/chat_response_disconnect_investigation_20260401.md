# AADS 채팅 응답 끊김 조사 보고 (2026-04-01 KST)

## 요약

CEO 채팅(`POST /api/v1/chat/messages/send`, SSE)은 **heartbeat(2~3초) + 백그라운드 완료**로 끊김을 완화하도록 설계되어 있다. 그럼에도 “응답이 끊긴다”는 증상은 **프록시/클라이언트 유휴 타임아웃**, **장시간 스트림 상한**, **상위 LLM·도구 타임아웃**, **이벤트 루프 블로킹** 등이 겹칠 때 발생할 수 있다.

## 확인한 설정·코드

| 구간 | 내용 |
|------|------|
| Nginx `location /api/v1/` | `proxy_read_timeout` / `proxy_send_timeout` **600s**, `proxy_buffering off`, SSE용 `gzip off` (`nginx-aads.conf`) |
| 백엔드 `with_background_completion` | **3초(평시) / 2초(도구 중)** heartbeat, Cloudflare 100s·nginx 600s 대비 주석 명시 (`chat_service.py`) |
| 대시보드 `page.tsx` | `fetch` + `AbortController`: **유휴 150초** 동안 `data:` JSON 이벤트가 없으면 abort (heartbeat/`stream_start`/delta 등으로 `resetSseTimeout` 호출). 절대 상한 **1시간** |
| 폴링 `get_streaming_status` | 메모리 상태가 **600초** 넘게 미완료면 “완료”로 만료 처리(폴링 UI용, SSE 자체를 자르지는 않음) |

## 끊김 원인 후보 (우선순위)

1. **150초 클라이언트 유휴 타임아웃**  
   네트워크/프록시가 SSE 청크를 밀어주지 않아 **heartbeat가 브라우저까지 도달하지 않으면** 프론트가 연결을 끊는다.

2. **Nginx 600초(10분) 읽기 타임아웃**  
   상위(upstream)에서 **10분간 읽을 바이트가 없으면** 연결 종료. 정상이라면 heartbeat가 2~3초마다 있어 리셋되어야 하나, **앱 프로세스가 이벤트 루프를 오래 점유**하면 heartbeat가 멈출 수 있다.

3. **장시간 도구·리서치·LLM 호출**  
   `research_stream(..., timeout=600)` 등 상위 API 타임아웃·재시도 실패 시 스트림이 중간에 끝날 수 있다.

4. **세션 전환**  
   `activeSessionRef !== requestSessionId` 시 `reader.cancel()` — 사용자가 채팅 중 다른 세션으로 바꾸면 스트림이 의도적으로 끊긴다.

5. **서버 재시작·502/503**  
   대시보드는 502/503/504에 대해 재전송 로직이 있으나, **스트리밍 도중** 재시작이면 부분 응답·복구 플로우에 의존한다.

## 권장 확인 절차

- [ ] 프로덕션 로그에서 `client_disconnected`, `bg_auto_cancel`, `streaming_state_expired`, `heartbeat_pump_died` 검색
- [ ] 끊김 시각 전후 **nginx error log** (upstream timed out 등)
- [ ] 브라우저 DevTools **Network**: 해당 `messages/send`가 **얼마간 idle**인지, **Status**·**Transferred** 중단 여부
- [ ] 장문·다도구 응답이면 **600s nginx**·**150s 프론트 idle** 한도와 겹치는지 검토

## 개선 제안 (선택)

- SSE 전용 `location`으로 분리해 `proxy_read_timeout`을 **86400s** 등으로 상향(채팅 URL만).
- 프론트 유휴 타임아웃 **150s → 300s** 이상 검토(heartbeat가 정상 도달하는 전제).
- 끊김 후 **메시지 리로드·`streaming-status` 폴링**이 사용자에게 명확히 보이도록 UX 점검.

## 검증

- 정적 코드·설정 리뷰 기준. 런타임 로그·네트워크 캡처는 미실행.

## 상태

- 적용: 보고서만 추가 (코드 변경 없음)
- 배포: 해당 없음
