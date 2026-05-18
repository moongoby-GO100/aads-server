# Codex App-Server — Rate Limits 조회 가이드 (AADS-193)

작성: 2026-05-15 / 검증 환경: codex-cli 0.130.0 / ChatGPT Pro OAuth

## 개요
Codex CLI에는 공개된 quota API가 없다. 그러나 CLI 내부의 **`app-server` 프로세스가 JSON-RPC 2.0 프로토콜**을 stdio로 노출하며,
`account/rateLimits/read` 메서드로 5시간 / 1주일 윈도우의 사용률과 리셋 시각을 정확히 조회할 수 있다.

이는 **CLI 내부용 실험(experimental) 인터페이스**이며 OpenAI 공식 공개 API가 아니다. CLI 업데이트로 메서드/응답 필드가 바뀔 위험이 있으므로 운영 코드에서는 응답 파싱을 방어적으로 작성해야 한다.

## 호출 흐름

```
relay (python) -> subprocess.Popen("codex app-server")
  send: initialize                    -> response: {userAgent, codexHome, ...}
  send: notifications/initialized     (no response, fire-and-forget)
  send: account/rateLimits/read       -> response: {result: {rateLimits, rateLimitsByLimitId}}
  terminate
```

stdio 전송, 메시지 1줄당 JSON 1개 (JSONL). LSP 식 `Content-Length` 헤더 불필요.

## 요청/응답 예시

### initialize
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "clientInfo": {"name": "aads", "title": "AADS", "version": "1.0"},
    "capabilities": {}
  }
}
```
응답:
```json
{
  "id": 1,
  "result": {
    "userAgent": "aads/0.130.0 (CentOS 7.0.0; x86_64) unknown (aads; 1.0)",
    "codexHome": "/root/.codex",
    "platformFamily": "unix",
    "platformOs": "linux"
  }
}
```

### account/rateLimits/read
```json
{"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}}
```
응답 (2026-05-15 측정):
```json
{
  "id": 2,
  "result": {
    "rateLimits": {
      "limitId": "codex",
      "limitName": null,
      "primary": {
        "usedPercent": 16,
        "windowDurationMins": 300,
        "resetsAt": 1778837065
      },
      "secondary": {
        "usedPercent": 79,
        "windowDurationMins": 10080,
        "resetsAt": 1779078704
      },
      "credits": {"hasCredits": false, "unlimited": false, "balance": "0"},
      "planType": "pro",
      "rateLimitReachedType": null
    },
    "rateLimitsByLimitId": {
      "codex": { ... },
      "gpt-5.3-codex-spark": { ... }
    }
  }
}
```

## 응답 스키마 (실측 기반)

| 필드 | 의미 |
|---|---|
| `limitId` | `codex`, `gpt-5.3-codex-spark` 등 limit별 식별자 |
| `primary.usedPercent` | 5시간 윈도우 사용률 (0-100) |
| `primary.windowDurationMins` | `300` = 5h |
| `primary.resetsAt` | Unix epoch seconds |
| `secondary.usedPercent` | 1주일(10080분) 사용률 |
| `secondary.windowDurationMins` | `10080` = 7d |
| `secondary.resetsAt` | Unix epoch seconds |
| `credits.balance` | 잔여 크레딧 (Pro는 보통 "0", unlimited=false) |
| `planType` | `pro`, `plus`, `team` 등 |
| `rateLimitsByLimitId` | limit별 상세 — 여러 모델/티어가 있을 수 있음 |

## 부수 메시지

`initialize` 후 server가 자동으로 푸시하는 알림:
- `configWarning` — 예: bubblewrap 미설치 경고
- `remoteControl/status/changed` — `disabled`/`enabled`
- 이들은 `id` 없는 notification. 파싱 시 method != response 기준으로 무시.

## 인증
호스트 `~/.codex/auth.json` (auth_mode=chatgpt, tokens.id_token/access_token/refresh_token/account_id) 자동 사용.
별도 토큰 주입 불필요. 단 **HOME 환경변수가 올바른 codex 디렉토리를 가리켜야** 함.

## AADS 연동 설계

```
[Dashboard /ops/account-usage]
       ↓ (30-60s 폴링)
[aads-server FastAPI: GET /api/v1/ops/codex-usage]
       ↓ (loopback 호출)
[claude-relay: GET /codex-usage  ← shared secret 보호]
       ↓ (60s 캐시 + subprocess)
[codex app-server (JSON-RPC)]
       ↓
[OpenAI / ChatGPT Plus quota]
```

### 보안
- relay endpoint는 X-Claude-Relay-Secret 헤더 검증 (기존 동일 시크릿 사용)
- 응답에서 이메일/토큰 prefix는 마스킹
- aads-server 측은 admin/operator 권한 검사 (기존 `/ops/*` 패턴)

### 캐시
- relay: 60초 메모리 캐시 (codex CLI subprocess 비용 절감)
- aads-server: 30초 추가 캐시 가능 (dashboard 폴링과 동기)

## 변경 위험 / 모니터링
- codex CLI 업데이트 시:
  - 메서드명 변경 (`account/rateLimits/read` → ?)
  - 필드명 변경 (`usedPercent` → ?)
  - `app-server` subcommand 자체 제거 가능성
- 대응:
  - relay 파싱은 try/except + 빈 응답 반환
  - 응답 형식 변경 시 로그 경고 (`codex_rate_limit_parse_failed`)
  - 일일 1회 정상 호출 헬스체크 추가 권장

## 운영 측정값 (참고, 2026-05-15 13:00 KST)

| limit | 5h 사용률 | 5h 리셋 | 7d 사용률 | 7d 리셋 | plan |
|---|---|---|---|---|---|
| codex | 16% | 18:24 KST | 79% | 5/18 13:31 | pro |
| gpt-5.3-codex-spark | 0% | 22:06 KST | 0% | 5/21 11:48 | pro |

## 관련 파일 (구현 후 추가)
- `scripts/claude_relay_server.py` — `/codex-usage` endpoint
- `app/api/ops.py` — `/api/v1/ops/codex-usage` 라우터
- `aads-dashboard/src/app/ops/account-usage/page.tsx` — UI 섹션

## 출처
- codex-cli 0.130.0 stdio 응답 직접 검증
- 추가 참고: `codex app-server help` 출력
