# 외부 시스템 알림 → 담당 세션 수신 경로 (AADS-RELAY-INGEST-N1)

작성 2026-09-17 · 대상 `POST /api/v1/notifications/relay`

## 왜 만들었나

CEO 지시: **"텔레그램은 알림에서 제외한다. 담당 세션에 알림 주고 조치할 수
있게 해라."**

빼기만 하면 알림이 사라진다. 실측(2026-09-17 17:5x KST)으로
`grep -rn "session_relay" app --include=*.py` 의 `app/api` 참조가 **0건**이었다
— 담당 세션에 무언가를 넣는 경로는 `ask_session` MCP 도구 하나뿐이고,
외부 스크립트(GO100 cron, contabo14)가 쓸 HTTP 입구가 없었다.
**받을 곳을 먼저 만들고** 텔레그램을 끄는 순서여야 한다.

사람이 텔레그램을 읽고 다시 담당에게 옮기는 단계가 없어진다. 알림을 받은
담당 세션이 그 자리에서 조치한다.

## 엔드포인트

```
POST /api/v1/notifications/relay
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `target_role` | ✅ | 담당 `role_key` (`DataEngineOwner`, `LiveTradingOwner`, `WaveEngineOwner`, `OpsInfraOwner`, `StrategyCardLead` …). 한글 별칭·세션 제목도 받는다. |
| `title` | ✅ | 알림 제목 (200자) |
| `body` | | 본문 (20,000자까지 받고 4,000자로 잘라 전달) |
| `project` | | `chat_workspaces.project_key` (`GO100`, `NTV2` …). 주면 그 워크스페이스 안에서만 찾는다. |
| `severity` | | `info` \| `warn` \| `critical` (기본 `info`) |
| `dedup_key` | | 비우면 역할·제목·본문·프로젝트 해시로 서버가 만든다(`auto:` 접두사) |
| `source` | | 보낸 주체(`go100-cron@contabo14`). 로그와 알림 본문에 남는다. |

### 인증 — 새 시크릿을 만들지 않는다 (R-KEY)

기존 테넌트 인증(`get_current_user`)을 그대로 쓴다. 사람이 없는 호출은
이미 있는 서비스 경로를 쓴다.

```
x-monitor-key: $AADS_MONITOR_KEY     # internal 테넌트로 인증된다
```

`AADS_MONITOR_KEY` 는 `.env` 에만 있고 컨테이너에 주입돼 있다(2026-09-17 확인).
cron 스크립트에도 값을 적지 말고 환경변수로 읽어라.

### 응답

성공(대기열 등록):

```json
{
  "ok": true, "queued": true,
  "relay_id": "…", "target_session": "…", "target_role": "DataEngineOwner",
  "matched_by": "role_key", "candidates": 1,
  "severity": "critical", "dedup_key": "go100:ingest-delay"
}
```

- `matched_by` — `role_key` / `role_alias` / `title` 중 어느 갈래로 찾았는지.
  "왜 이 세션에 갔지" 를 되짚을 때 이것부터 본다.
- `candidates` — 같은 조건에 걸린 세션 수. 2 이상이면 역할 세션이 중복돼 있다는 뜻이다.

중복 차단(**200 이다, 오류가 아니다**):

```json
{"ok": true, "queued": false, "deduplicated": true, "error": "duplicate",
 "relay_id": "<앞서 전달된 건>", "dedup_key": "…"}
```

5분마다 도는 cron 에 4xx 를 주면 그 cron 은 알림이 안 갔다고 판단해 재시도하거나
자기 로그를 오류로 채운다. 중복은 정상 동작이므로 200 으로 돌려준다.

실패:

| 상황 | 코드 | `error` |
|---|---|---|
| 해당 역할의 세션이 없다 | 404 | `target_not_found` |
| `target_role`/`title`/테넌트 누락 | 400 | `target_role_required` 등 |

## GO100 cron 에서 부르는 예시

```bash
#!/bin/bash
# contabo14 — 수집 지연 감시 (5분 주기)
set -euo pipefail

AADS_API="${AADS_API:-https://aads.newtalk.kr}"
# 값은 /root/.aads.env 같은 파일에서 읽는다. 스크립트에 적지 않는다.
source /root/.aads.env      # AADS_MONITOR_KEY=...

curl -sS -X POST "$AADS_API/api/v1/notifications/relay" \
  -H 'Content-Type: application/json' \
  -H "x-monitor-key: $AADS_MONITOR_KEY" \
  -d '{
        "target_role": "DataEngineOwner",
        "title": "KRX 일봉 수집 지연",
        "body": "09:20 까지 2,431 종목 중 118 종목 미도착. 마지막 성공 09:05.\n로그: /root/go100/logs/ingest_20260917.log",
        "project": "GO100",
        "severity": "critical",
        "dedup_key": "go100:ingest-delay:20260917",
        "source": "go100-cron@contabo14"
      }'
```

`dedup_key` 에 **날짜나 주기 버킷을 넣는 것**을 권한다. 같은 사고가 하루 내내
5분마다 보고되면 담당 세션이 그 알림만으로 LLM 예산을 태운다(CEO 규칙: LLM
15회/task). 위 예시는 같은 날 같은 사고를 한 번만 올린다 — 다만 창은 기본
300초이므로, 하루 단위로 완전히 막고 싶으면 cron 쪽에서 상태 파일을 두는 편이
정확하다.

### 전달까지 걸리는 시간

즉시 넣지 않는다. **대상이 응답 중일 때 밀어 넣으면 그 응답이 통째로 버려진다**
(`stale_superseded_by_newer_user_message` — 2026-09-14 실측 54,301자 유실).
그래서 `session_relay` 에 `queued` 로 넣고, 배달기가 대상이 한가해질 때 전달한다.

- 배달 주기: 30초 (`AADS_RELAY_QUEUE_EVERY_TICKS`, 5초 × 6)
- 대상이 오래 바쁘면 계속 기다린다. **6시간**을 넘기면 `failed(queue_expired)` 로 버린다
  (`SESSION_RELAY_QUEUE_MAX_AGE_HOURS`).
- 즉 "지금 당장 사람을 깨우는" 용도가 아니다. 그 성질이 필요한 알림은 별도 경로다.

## 대상 세션을 고르는 규칙

1. `role_key` 정확 일치(대소문자 무시) — `cto` / `CTO` 가 실제로 섞여 있다.
2. 한글 별칭 — 역할 별칭 표(`role_scope`)에 영문 키와 같이 등록된 이름.
3. 세션 제목 부분 일치.

세 갈래 모두 **테넌트**로 좁히고, `project` 를 주면 `chat_workspaces.project_key`
로 한 번 더 좁힌다. 같은 역할 세션이 여럿이면 **가장 최근에 움직인(`updated_at`)
하나만** 고른다 — 전부에 넣으면 같은 알림에 담당 셋이 각자 조치해 서로를 덮어쓴다.
몇 개가 후보였는지는 `candidates` 로 돌려준다.

**못 찾으면 조용히 버리지 않는다.** 404 + 경고 로그
(`session_relay_notify_target_not_found role=… project=… severity=… title=…`)를 남긴다.
알림이 갈 곳이 없다는 사실 자체가 운영 사고다.

## 기존 ask_session 흐름은 건드리지 않았다

담당끼리 주고받는 보호 장치(홉 상한 3, 같은 쌍 중복, busy 큐잉)는 그대로다.
시스템 알림만 갈라서 처리한다.

| | ask_session | 시스템 알림 |
|---|---|---|
| 발신자 | 실제 세션 | 고정 UUID `00000000-0000-0000-0000-0000000a1e27` |
| `hop` | 1~3, 초과 시 차단 | **0** (협업 홉 예산을 깎지 않는다) |
| 같은 쌍 중복(`_pair_in_flight`) | 차단 | 적용 안 함 — 같은 쌍이 연달아 오는 것이 알림에서는 정상 |
| 중복 방지 | 없음 | `dedup_key` + 300초 창 |
| 대상이 바쁘면 | 대기열 | 대기열 (같은 배달기) |
| 답 | 물어본 세션으로 회신 | **없음** — 돌려줄 세션이 없다 |

배달기(`dispatch_queued_relays`)는 발신자가 시스템 UUID 인 행을
`_run_notification` 으로 보낸다. `_run_relay` 로 태우면 없는 세션에 회신하려다
실패한다.

## 변경 파일

| 파일 | 내용 |
|---|---|
| `app/services/session_relay.py` | `notify()` · `_resolve_role_session()` · `_recent_duplicate()` · `_build_notification()` · `_run_notification()` 신규, `dispatch_queued_relays()` 에 시스템 발신 분기 추가 |
| `app/api/notifications.py` | `POST /notifications/relay` 신규 (기존 push 엔드포인트 4개는 그대로) |
| `migrations/20260917_session_relay_notify.sql` | `session_relay.dedup_key` 컬럼 + 부분 인덱스 (ADD COLUMN IF NOT EXISTS) |
| `tests/unit/test_session_relay_notify.py` | 신규 14케이스 |

컬럼이 아직 없는 동안(이미지가 먼저 뜨고 DB 자산이 나중에 적용되는 순서)에도
알림은 간다 — 중복 방지만 쉬고 경고 로그를 남긴다. 긴급 알림이 사라지는 쪽이
중복 한 건보다 훨씬 나쁘다.

## 아직 안 한 것

- **텔레그램 전환**. GO100(contabo14) cron·텔레그램 스크립트는 건드리지 않았다.
  이 경로가 실제로 도달하는 것을 확인한 뒤 별도 작업으로 옮긴다.
- 알림 도달/조치 여부 대시보드. 지금은 `session_relay` 행과 로그
  (`session_relay_notify_queued` / `_dispatched` / `_delivered` / `_failed` /
  `_deduped` / `_target_not_found`)로만 본다.
