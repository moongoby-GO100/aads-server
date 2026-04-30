# AADS-SSE: SSE 끊김방지 및 자연스럽게 이어지는 대화응답 구현

_아젠다 생성: 2026-03-27 | CEO 직접 지시_

## 목표
SSE 스트리밍 끊김을 원천 방지하고, 끊기더라도 자동으로 이어지고 완료되는 시스템 구현.

## 운영 보강 (v2.2 — 2026-04-30)

2026-04-30 19:50~19:52 KST 재현된 끊김은 LLM 자체 장애가 아니라 `deploy.sh code`가 active API를 스트리밍 중 `STOPPING` 상태로 만든 것이 직접 원인이었다. 동시에 inactive green이 복구 scanner를 잡을 수 있던 구조 때문에 DB는 생성 중으로 보이나 브라우저가 붙은 active 프로세스에는 응답 소유권이 없는 상태가 발생했다.

수정 사항:
- `deploy.sh code`는 active stream이 있으면 active를 재시작하지 않고 peer slot을 먼저 재시작한 뒤 nginx upstream과 복구 오너를 전환한다.
- active marker(`.active_container`, `.active_port`)와 컨테이너 env/mount를 통해 DB resume scanner는 현재 공개 API만 실행한다.
- stream 시작 즉시 `streaming_placeholder`를 DB에 저장하고 heartbeat 중에도 interim save를 수행해 첫 토큰 지연/도구 대기 중에도 브라우저 재진입 상태가 비지 않게 했다.
- relay 슬롯 대기에는 timeout을 둬, LLM CLI 슬롯이 장시간 막힐 때 무한 대기 대신 명시적 503을 반환하도록 했다.

검증:
- 단위 테스트: `tests/unit/test_chat_service.py` 10 passed
- 실제 gpt-5.5 강제 끊김 e2e: `resume_done`, 최종 assistant 1개, placeholder 0개, 중복 replay 없음
- 브라우저 직접 확인: 문제 세션 `e62f3c19-5558-4f89-87bf-709c7dccd4af` 로딩 및 chat API 200, `current_execution_id=null`

## 완료된 수정 (v2.0 — 2026-03-27)
| # | 수정 | 일자 |
|---|------|------|
| 1 | streamingSessionRef 항상 해제 | 03-27 |
| 2 | waitingBgResponse 차단 제거 | 03-27 |
| 3 | waitingBg 타임아웃 180s→30s | 03-27 |
| 4 | maxStreamTimeout 300s→3600s | 03-27 |
| 5 | 서버 heartbeat 5s→3s/2s | 03-27 |
| 6 | Nginx HTTPS keepalive_timeout | 03-27 |
| 7 | 중지버튼 스크롤 점프 방지 | 03-27 |
| 8 | stream-resume AbortController 120s | 03-27 |
| 9 | resume_generating 즉시 polling | 03-27 |
| 10 | waitingBgRef 경쟁조건 방지 | 03-27 |
| 11 | finally setTimeout ref 저장 | 03-27 |
| 12 | DB 동기화 후 스크롤 재발 방지 | 03-27 |
| 13 | Heartbeat 256byte CF flush 패딩 | 03-27 |

## 향후 과제
| 우선순위 | 과제 |
|----------|------|
| P1 | X-Accel-Buffering: no 전수 확인 |
| P2 | Last-Event-ID SSE 표준 구현 |
| P2 | WebSocket 폴백 |
| P3 | Background Mode + Webhook |
| P3 | Cloudflare Enterprise 검토 |

## 관련 문서
- docs/knowledge/SSE-STREAMING-ARCHITECTURE.md
- docs/reports/CEO-019-SSE-IMPROVEMENT-REPORT.md

## 버전 이력
| 버전 | 일자 | 내용 |
|------|------|------|
| v1.0 | 2026-03-27 | 초기 생성 — 13건 수정 완료 |
