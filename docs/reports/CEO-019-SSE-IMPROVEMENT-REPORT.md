# CEO-019 SSE 끊김방지 개선 보고서

_2026-03-27 | CEO 직접 지시_

## 1. 왜 AADS만 유독 끊기나

ChatGPT/Claude.ai/Gemini는 자사 서버에서 직접 스트리밍 — Cloudflare 없음.
AADS는 Cloudflare CDN 경유 → 120초 Proxy Read Timeout 제약.

## 2. 오늘 배포된 수정 13건

### A그룹: recovered 후 응답 이어짐 (runner-7c84b8bf)
1. streamingSessionRef 항상 해제
2. waitingBgResponse 차단 제거
3. waitingBg 타임아웃 180s→30s

### B그룹: SSE 끊김 원천 방지 (runner-4259d471 + 직접)
4. maxStreamTimeout 1시간
5. heartbeat 5s→3s
6. 도구 실행 중 heartbeat 2s
7. Nginx keepalive_timeout
8. heartbeat 256byte Cloudflare flush 패딩

### C그룹: 중지/재연결 버그 (runner-7fdcf520)
9. 중지버튼 스크롤 점프 방지
10. stream-resume AbortController 120s
11. resume_generating 즉시 polling 전환
12. waitingBgRef 경쟁조건 방지
13. finally setTimeout ref 저장

## 3. 권장 개선 방향
- P1: X-Accel-Buffering 헤더 전수 확인
- P2: Last-Event-ID SSE 표준 자동 재연결
- P3: 도구 100회+ → Background Mode + 완료 알림

## 4. CEO 핵심 답변

"타 AI 채팅은 왜 문제없나?" → 구조 차이. 경쟁사는 Cloudflare 없이 직접 스트리밍.
"끊기더라도 이어지는 게 중요" → Invisible Recovery가 정확히 이 역할. SSE 끊겨도 서버는 계속 생성 → 자동 재연결.
"[recovered]가 계속 나온다" → 오늘 이 세션의 [recovered]는 AI 에이전트 컨텍스트 초과 (도구 100회+). 일반 업무에서는 발생 안 함.
