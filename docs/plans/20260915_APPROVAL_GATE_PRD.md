# 승인 게이트 재설계 PRD

- 기획서: `20260915_APPROVAL_GATE_기획서.md`
- 작성: 2026-09-15

## 1. 목표

대표님 승인을 **되돌릴 수 없고 돈이 걸린 것**에만 받는다. 나머지는 막지 않고
알린다. 오탐률을 실측으로 확인 가능한 수준까지 낮춘다.

## 2. 성공 기준

| 지표 | 지금 | 목표 | 측정 |
|---|---|---|---|
| 대기 요청 중 진짜 T1 비율 | 2/8 (25%) | 90% 이상 | `agent_permission_requests` 등급별 집계 |
| 읽기 명령이 막힌 건수 | 최소 2건 확인 | 0 | 회귀 시험 |
| 문서·보고서가 막힌 건수 | 재현됨 | 0 | 회귀 시험 |
| `risk_level` 분포 | `high` 100% | 3등급 분산 | DB 집계 |

## 3. 데이터

### 3.1 스키마 변경

`agent_permission_requests` 에 두 칼럼을 더한다.

```sql
ALTER TABLE agent_permission_requests
    ADD COLUMN IF NOT EXISTS gate_source text NOT NULL DEFAULT 'live_trading',
    ADD COLUMN IF NOT EXISTS tier        text NOT NULL DEFAULT 'approve';
```

- `gate_source`: `live_trading` | `direction` — 어느 게이트가 올렸나
- `tier`: `approve`(T1) | `notify`(T2)

`risk_level` 은 그대로 쓰되 상수를 떼고 계산값을 넣는다:
`critical` (주문·자금) / `high` (실매매 코드) / `medium` (방향 변경).

기존 행은 `gate_source='live_trading'`, `tier='approve'` 로 남는다 — 지나간
판정을 소급해 고치지 않는다. 그때 그렇게 판정했다는 사실이 기록이다.

### 3.2 `decision` 값

`notify` 등급은 승인 개념이 없다. `decision='acknowledged'` 를 추가하고,
대기 목록 질의는 `decision='pending' AND tier='approve'` 로 좁힌다.

## 4. 판정 규칙

### 4.1 `live_trading_guard.classify()` 수정

**R1 — stderr/stdout 버리기는 쓰기가 아니다**

```python
# `2>/dev/null` 은 거의 모든 조회 명령에 붙는다. 이것을 쓰기로 보면
# 읽기 명령이 전부 막힌다 — 2026-09-15 실제로 그랬다.
_NULL_REDIRECT = re.compile(r"\d?>>?\s*(/dev/null|&\d)")
def _has_write_redirect(cmd: str) -> bool:
    return bool(_REDIRECT_WRITE.search(_NULL_REDIRECT.sub("", cmd)))
```

**R2 — 문서·보고서·시험은 실매매가 아니다**

```python
_NON_CODE_PATH = re.compile(
    r"(^|/)(docs?|reports?|tests?|plans?)/|\.(md|txt|html|csv|png|json)$",
    re.IGNORECASE,
)
```
경로가 여기 걸리면 `_GUARDED_PATH` 를 보지 않고 통과.

**R3 — `risk` 는 심볼로만**

`[/_-]risk|risk[_-]` → `risk_limit|risk_manager|risk_engine|risk_config`
(파일명 `risk_report.md` 같은 것은 R2 에서 이미 빠진다.)

**R4 — 등급 계산**

```python
_CRITICAL = re.compile(
    r"(order[_-]executor|place[_-]order|cancel[_-]order|send[_-]order|"
    r"is[_-]live|allocated[_-]amount|account[_-]no|dedicated[_-]account|"
    r"systemctl\s+(restart|stop|start)\s+go100)", re.IGNORECASE)
```
걸리면 `critical` + T1. 아니면 `_GUARDED_PATH` 에 걸린 쓰기는 `high` + T1.
그 밖에 실매매 주변은 `medium` + T2.

### 4.2 `direction_guard` 분리

`request_approval()` 에 인자를 더한다.

```python
async def request_approval(tool_name, tool_input, reason, *,
                           session_id="", tenant_id="",
                           gate_source="live_trading", tier="approve",
                           risk_level="high", label="실매매"):
```

`direction_guard` 는 `gate_source="direction", tier="notify",
risk_level="medium", label="방향"` 으로 부른다. 프리픽스 `[실매매]` 상수를
없애고 `label` 을 쓴다.

`tier="notify"` 면 **도구를 막지 않는다.** `direction_guard.check()` 는
차단 응답 대신 `None` 을 돌려주고, 기록과 오비스 알림만 남긴다.

## 5. API

| 메서드 | 경로 | 변경 |
|---|---|---|
| GET | `/approvals/pending` | `tier='approve'` 만. 응답에 `tier`·`gate_source`·`risk` 포함 |
| GET | `/approvals/notifications` | **신설** — `tier='notify'` 최근 50건 |
| POST | `/approvals/{id}/acknowledge` | **신설** — 알림 확인 (`decision='acknowledged'`) |
| POST | `/approvals/acknowledge-all` | **신설** — 대기 중 알림 일괄 확인 |
| GET | `/approvals/active` | 그대로 |
| POST | `/approvals/{id}/decide` | 그대로 (사유 NOT NULL 문제는 `f6a091c7` 에서 수정 완료) |
| GET | `/approvals/gate-status` | **신설** — `LIVE_TRADING_GATE_ENABLED` 상태. 꺼져 있으면 화면이 경고를 띄운다 |

## 6. 화면

`aads-dashboard` `/approvals`. 목업은 기획서 4절.

- 두 구역: **승인 필요**(T1, 빨강) / **알림**(T2, 노랑)
- T1 카드: 등급 배지 · 도구 · 대상 · 요청 세션(역할키) · 남은 시간 · 요약
  · 버튼 3개(`이번 건만` / `이 미션 동안` / `거부`)
- T2 줄: 한 줄 요약 + `모두 확인` 버튼 하나
- 하단: 유효한 미션 승인 목록과 `회수하기`
- 게이트가 꺼져 있으면 상단에 빨간 띠

버튼 문구와 파라미터는 서버 `choices` 를 그대로 쓴다(이미 그렇게 내려준다).
화면에 규칙을 다시 적지 않는다 — 두 벌이 되면 한쪽이 낡는다.

## 7. 시험

`tests/unit/test_live_trading_gate_tiers.py`

| # | 입력 | 기대 |
|---|---|---|
| 1 | `grep … live_engine.py 2>/dev/null` | 통과 |
| 2 | `psql -c 'select …' 2>/dev/null` | 통과 |
| 3 | `cat /srv/live_trading/config.yaml` | 통과 |
| 4 | `docs/risk_report.md` 쓰기 | 통과 |
| 5 | `reports/scalping_backtest.md` 쓰기 | 통과 |
| 6 | `patch live_engine.py` | T1 · high |
| 7 | `systemctl restart go100-kiwoom-scalping` | T1 · critical |
| 8 | `db_safe_write` `UPDATE go100_strategy_cards SET is_live=true` | T1 · critical |
| 9 | `db_safe_write` `UPDATE goals SET status='archived'` | T2 · medium (막지 않음) |
| 10 | `patch live_engine.py` 주석만 바꿈 | T2 (주문 경로 아님) — **판정 불가 시 T1 로 올린다** |

10번은 본문 판정이 어려우므로 **보수적으로 T1** 로 둔다. 애매하면 막는 쪽이
맞다 — 돈이 걸린 쪽에서만 그렇다. T2 판정이 애매하면 통과시킨다.

## 8. 되돌리기

- `LIVE_TRADING_GATE_ENABLED=false` 로 전체 해제 (배포 없이)
- `APPROVAL_TIERS_ENABLED=false` 로 이번 변경만 해제 → 옛 동작(전부 T1)
- 스키마 변경은 칼럼 추가뿐이라 되돌릴 필요가 없다

## 9. 작업 순서

1. 마이그레이션 (`gate_source`, `tier`)
2. `live_trading_guard` 판정 수정 + 등급 계산 + 시험 10건
3. `direction_guard` 분리 (막지 않고 알림)
4. API 3개 신설 + `pending` 질의 좁히기
5. 대시보드 `/approvals` 두 구역 · 목업대로
6. 배포 후 24시간 등급 분포 실측 → 오탐 0 확인

1~4 는 반나절, 5 는 반나절로 본다.

---

# 10. 개정 — 2026-09-15 오후

3단계 판정(1~9절)을 올린 뒤 실사용에서 **판정이 아니라 "승인이 먹지 않는"
문제**가 드러났다. 이 절은 그날 오후에 확정한 원인과 조치를 기록한다.

## 10.1 무엇이 잘못됐나

### 결함 1 — 승인이 구조적으로 재사용될 수 없었다 (P0)

`work_key` 를 파이썬 내장 `hash()` 로 만들고 있었다.

```python
work_key = f"{session_id[:8]}:{tool_name}:{hash(summary) & 0xFFFFFFF:07x}"
```

문자열 해시는 프로세스마다 시드가 다르다(`PYTHONHASHSEED` 미설정). 그리고
도구는 **채팅 턴마다 새로 뜨는 `mcp_servers.aads_tools_bridge` 프로세스**에서
돈다. 즉 카드를 남긴 프로세스와 승인을 조회하는 프로세스가 항상 다르다.

실측 근거:

| 관측 | 값 |
|---|---|
| 동일 문자열 `hash()` 3회 (별도 프로세스) | `0x318bd19` / `0xba249d` / `0x5221d9` |
| 동일 요청 1건이 만든 카드 (09:15~10:39) | **21장 / 키 21종** |
| 동일 패치 4회 시도의 키 | `9c32ed4` → `2e87772` → `b6ff8e4` → `e464974` |

대표님이 네 번 승인하셨고 네 번 다 무효였다. "승인해도 계속 다시 묻는다" 의
정체가 이것이다.

**조치**: `hashlib.sha1(summary)[:7]` 로 교체 (`c65319ee`). 3개 프로세스에서
같은 값(`1f25729`)을 내는 것으로 실증했다.

### 결함 2 — 미션 승인이 이름뿐이었다 (P0)

`approvals_decide` 는 `approval_scope.mission_key` 를 저장하고 있었는데
`is_approved()` 가 **그것을 조회하지 않았다.** 정확한 `work_key` 일치만 봤다.
명령이 한 글자만 달라도 다시 물었다. 사용 횟수(`used`)도 증가시키지 않아
`max_executions` 가 장식이었다.

**조치**: 매칭을 세 갈래로 넓히고 사용 횟수를 센다 (`c65319ee`).

```sql
WHERE decision = 'approved' AND expires_at > now()
  AND ( work_key = $1                                              -- single
     OR (scope='mission' AND requested_by=$2 AND action_type=$3)   -- mission
     OR (scope='goal'    AND action_type=$3 AND goal_id = ANY($4)) )  -- goal
```

**미션 승인은 명령 내용을 보지 않는다** — `세션 + 도구` 만 본다. 이것이
"끊김없이" 의 본체다. 무제한은 아니다: 횟수·시간 상한을 둘 다 건다.

### 결함 3 — 재개 턴이 두 번 왔다 (P1)

`chat_deferred_reactions` 의 리스가 15분인데 긴 턴은 그보다 오래 간다.
리스가 먼저 끊기면 스윕이 "놓친 작업" 으로 보고 다시 집었다.

실측: 거절 1건(`232b5b36`)이 10:37:40·10:57:19 두 번 배달, 행 `2258dd76` 의
`attempts=2`. 코드에 프로세스 로컬 가드(`_active_bg_tasks`)가 있었지만 그
사이 슬롯이 바뀌면 새 프로세스의 표가 비어 있어 통과한다.

승인 건에서 같은 일이 나면 **같은 작업이 두 번 실행된다.**

**조치**: 재청구 후보에서 live execution 이 있는 세션을 **DB 기준**으로
제외하고, 리스를 45분으로 늘렸다 (`e201f9f2`).

## 10.2 새로 넣은 것

### 기능 1 — 체크박스 일괄 승인

대기가 열 건이면 열 번을 눌러야 했고, 누르는 동안 새 카드가 또 쌓여 끝이
나지 않았다.

| 항목 | 내용 |
|---|---|
| API | `POST /api/v1/approvals/decide-bulk` (`2cc11c8f`) |
| 입력 | `{ids[], decision, scope, hours, max_executions}` — 최대 100건 |
| 실패 처리 | 한 건이 실패해도 나머지는 계속. `{decided, failed[]}` 로 돌려준다 |
| 화면 | 채팅 팝업 다중선택 (`365ec4c`) — **`critical` 은 일괄 선택에서 제외** |

`critical`(주문·자금)을 일괄에서 뺀 것은 의도적이다. 한 번에 누르는 UI는
읽지 않고 누르게 만든다. 돈이 걸린 것은 건건이 본다.

### 기능 2 — 다음 단계 제안 카드 (`next_step`)

보고 끝의 "→ 다음 단계 1·2·3" 은 그냥 글자였다. 어디에도 올라가지 않아
대표님이 매번 "진행해" 를 치셔야 다음이 돌았다.

| 항목 | 내용 |
|---|---|
| 도구 | `propose_next_steps(steps[], context)` — 1~5건 |
| 구현 | `app/services/next_step_proposals.py` |
| 저장 | `agent_permission_requests`, `gate_source='next_step'`, `action_type='next_step'` |
| 화면 | 기존 대기 목록 질의가 `tier='approve'` 만 보므로 **추가 작업 없이** 팝업·`/approvals` 에 함께 뜬다 |
| 승인 시 | 재개 턴 문구가 제안용으로 분기 — "막혀 중단됐던" 이 아니라 "이 제안을 수행하라" |

**이미 승인받은 범위는 묻지 않는다.** 제안에 `tool` 이 적혀 있고 그 도구를
덮는 미션·골 승인이 살아 있으면 카드를 만들지 않고 `auto` 로 돌려준다.
판정 자체는 실행 시점에 `is_approved()` 가 다시 하므로(횟수도 거기서 센다)
이 사전 확인은 읽기 전용이다 — 두 곳에서 판정하면 한쪽이 반드시 어긋난다.

제안 카드는 **막힌 것을 푸는 카드와 성격이 다르다**. 저쪽은 "하려다 막혔다",
이쪽은 "이걸 할까요". `gate_source` 를 나눠 둔 이유이며, 화면에서도
구분해야 한다(미구현 — 10.4 참조).

## 10.3 검증 기준

| 지표 | 개정 전 | 목표 | 측정 |
|---|---|---|---|
| 동일 요청이 만든 카드 수 | 21장 | **1장** | `work_key` distinct 집계 |
| 승인 후 재질문 | 매 호출 | 0 (상한 내) | `approval_scope.used` 증가 확인 |
| 재개 턴 중복 | 1건 확인 | 0 | `chat_deferred_reactions.attempts > 1` |
| 일괄 승인 1회 처리량 | 1건 | N건 | `decide-bulk` 응답 `decided` |
| "진행해" 수동 지시 | 반복 | 감소 | 제안 카드 승인 건수 대비 |

## 10.4 남은 것

- 제안 카드와 차단 카드의 **화면 구분**(색·라벨) — 미구현
- `/approvals` 화면 체크박스 — 지금은 채팅 팝업에만 있다
- 제안 승인 시 **실행 페이로드 자동 바인딩** — 지금은 담당이 제안 문구를
  읽고 수행한다. 도구·인자를 카드에 실어 그대로 실행하는 것이 다음 단계다

## 10.5 배포 이력

| 배포 | 커밋 | 내용 | 결과 |
|---|---|---|---|
| #516 | `c65319ee` | 고정키 + 미션·골 매칭 + 횟수 차감 | success 11:02 |
| #517 | `365ec4c` | 팝업 체크박스 다중선택 | success 11:02 |
| #518 | `2cc11c8f` | `decide-bulk` API | success 11:10 |
| — | `e201f9f2` | 재개 턴 중복 수정 | 미배포 |
| — | (이 커밋) | `propose_next_steps` + PRD 개정 | 미배포 |

배포 이력 보정 (2026-09-15 저녁 실측, `deploy_runs`):

| 배포 | 커밋 | 내용 | 결과 |
|---|---|---|---|
| #521 | `1496e89a` | `propose_next_steps` 제안 카드 | success |
| #528 | `1add3fe3` | 목표 승인에 프로젝트 경계 | success |
| #530 | `64bce0a3` | 승인 범위 4단계(API) | success |
| #531 | `db761859` | 승인 범위 4단계(화면) | success |

---

# 11. 개정 — 2026-09-15 저녁: 상위 권한 3층 모델

대표님 지시 — "이 대화 동안보다 더 상위인 채팅창 권한, 프로젝트 권한에
대해 어떻게 적용하고 어떻게 화면에 반영할지".

10절까지는 **카드 한 장의 범위를 넓히는** 이야기였다. 이 절은 그 위,
**카드가 뜨기 전에 미리 정해 두는 권한**을 다룬다.

## 11.1 지금 서버가 적용하는 계층 (실측)

`live_trading_guard.is_approved()` 조회절(`app/services/live_trading_guard.py:576~614`)
과 `goal_policy_allows()`(같은 파일 `:451`) 이 판정하는 것 전부다.

| 계층 | 통과 조건 | 세션을 넘나 | 상한 | critical |
|---|---|---|---|---|
| single | `work_key` 완전일치 | ✕ | 1회 / 2h | 허용 |
| mission | 세션+도구+대상지문+**파일지문** | ✕ | 20회 / 2h | 허용 |
| session | 세션+도구 (대상 불문) | ✕ | 50회 / 4h | **차단** |
| project | 프로젝트+도구 | **✅** | 100회 / 8h | **차단** |
| goal | `goals.approval_policy` + 프로젝트 일치 | **✅** | 설정값 / 목표 종료까지 | 설정 시 허용 |

넓은 것부터 소비한다(`ORDER BY goal→project→session→mission→single`).
좁은 승인을 남겨 두어야 그 대상을 다시 묻지 않는다.

가동 중인 최상위 권한 — `goals.approval_policy` 3건, 전부 GO100:
`#310` used 24/200, `#119` used 42/200, `순자산 100억` used 0/200.

승인 카드 24시간 분포: mission 90건(실사용 38) · single 10건 ·
**session/project 0건**. 범위는 배포됐지만 아직 눌린 적이 없다.

## 11.2 무엇이 빠졌나 — 전부 화면 쪽이다

| # | 문제 | 근거 |
|---|---|---|
| 1 | 켜진 권한이 화면에 **안 보인다** | `/approvals/active` 대시보드 호출 0건(`grep -rn`) |
| 2 | **회수 버튼이 없다** | `/approvals/{id}/revoke` 호출 0건 |
| 3 | `/approvals` 페이지가 구버전 | `src/app/approvals/page.tsx:135` = `"single" \| "mission"` |
| 4 | project 권한이 **다른 대화에 안 보인다** | `/approvals/active` 가 `requested_by = session_id` 로 거른다 |
| 5 | **미리 켤 수 없다** | 카드가 떠야만 범위 선택 가능. goal 만 예외 |

2번이 핵심이다. **회수가 안 되니 넓은 권한을 줄 수 없고, 넓은 권한을
안 주니 카드가 계속 뜬다.** 12시간에 카드 89장, 실사용 18회가 그 결과다.

서버 API는 이미 있다(`app/api/project_docs.py:1105`, `:1151`). 화면이
부르지 않을 뿐이다. 그래서 P0 은 서버 추가 개발이 거의 없다.

## 11.3 설계 — 3층 권한 모델

| 층 | 이름 | 저장 위치 | 설정 지점 | critical |
|---|---|---|---|---|
| L1 | 요청 카드 (기존 4종) | `agent_permission_requests` | 팝업 버튼 | single/mission만 |
| **L2** | **채팅창 권한** (신규) | `chat_sessions.approval_policy` | 채팅 상단 권한 칩 → 패널 | 구조적 차단 |
| **L3** | **프로젝트 권한** (신규) | `project_approval_policies` (신규) | 프로젝트 설정 화면 | 구조적 차단 + 2단계 확인 |
| L3′ | 목표 권한 (기존) | `goals.approval_policy` | 목표 상세 | 명시 설정 시만 |

원칙 넷.

1. **도구 하나가 아니라 도구군으로 준다.** 지금은 도구 1개씩이라
   파일을 고치고 명령 한 줄 실행할 때 또 묻는다.
2. **무제한은 없다.** L2 4시간/50회, L3 8시간/100회. 시간과 횟수가
   둘 다 걸린다. 하나만 걸면 그것은 게이트 해제다.
3. **자동 통과는 흔적을 남긴다.** 채팅에 `이 프로젝트 권한으로 통과
   (12/100)` 한 줄. 조용히 통과하면 권한이 켜진 줄 모른다.
4. **L3 는 감사 대상이다.** 세션을 넘으므로 어느 대화가 썼는지 남긴다.

도구군:

| 군 | 포함 도구 | 기본 |
|---|---|---|
| `code_write` | `write_remote_file`, `patch_remote_file` | 꺼짐 |
| `shell` | `run_remote_command` | 꺼짐 |
| `db_write` | `db_safe_write` | 꺼짐 |
| `deploy` | `deploy_safe`, 배포 계열 | 꺼짐 |
| `order` | 주문·자금 계열 | **L2·L3 에 없음** |

## 11.4 데이터

```sql
-- L2: 채팅창 권한
ALTER TABLE chat_sessions
  ADD COLUMN IF NOT EXISTS approval_policy jsonb NOT NULL DEFAULT '{}'::jsonb;
-- {"code_write": {"max": 50, "used": 3, "expires_at": "...", "set_by": "CEO"}}

-- L3: 프로젝트 권한
CREATE TABLE IF NOT EXISTS project_approval_policies (
  project      text PRIMARY KEY,
  policy       jsonb NOT NULL DEFAULT '{}'::jsonb,
  set_by       text,
  updated_at   timestamptz NOT NULL DEFAULT now()
);
```

`agent_permission_requests.approval_scope` 에 `tool_group` 키를 추가한다.
기존 행에는 없으므로 `COALESCE(..., '')` 로 읽고, 없으면 지금처럼
`action_type` 단일 도구로 판정한다 — 옛 승인이 갑자기 넓어지면 안 된다.

## 11.5 API

| 경로 | 상태 | 용도 |
|---|---|---|
| `GET /approvals/active` | **있음** — 필터 보정 필요 | 살아 있는 권한 + 잔여·만료 |
| `POST /approvals/{id}/revoke` | **있음** — 화면만 붙이면 됨 | 즉시 회수 |
| `GET /approvals/gate-status` | 있음 | 게이트 on/off, 대기 건수 |
| `GET/PUT /sessions/{id}/approval-policy` | 신규 | L2 조회·설정 |
| `GET/PUT /projects/{key}/approval-policy` | 신규 | L3 조회·설정 |

`/approvals/active` 보정 — 지금은 `($2 = '' OR requested_by = $2)` 라
세션을 넘는 project 권한이 다른 대화에서 조회되지 않는다. `project`
파라미터를 받아 `scope='project'` 행을 OR 로 합친다.

## 11.6 화면

**① 채팅 상단 권한 칩** (목표 띠와 같은 줄에 두지 않는다 — 가린다)

```
🔓 이 프로젝트 동안 · 코드수정 · 12/100 · 7h 12m 남음    [회수]
🔒 건건 확인                                    ← 권한 없을 때
```

꺼진 상태도 그려야 한다. 안 그리면 대표님이 "승인할 게 없다" 로 읽는다.

**② 권한 패널** (칩 클릭 → 우측 슬라이드)

- 상단: 살아 있는 권한 목록, 행마다 `회수` 버튼
- 하단: 미리 켜기 토글 — 도구군 4종 × 범위 2종(이 대화 / 이 프로젝트)
- 주문·자금 줄은 토글을 **그리지 않고** "카드로만 승인" 문구를 둔다

**③ `/approvals` 페이지** — 범위 4단계로 통일 + `활성 권한` 탭

**④ 프로젝트 설정 화면** — L3 는 여기서만 켜진다. 켤 때 빨간 확인 모달
(`이 프로젝트의 모든 대화에 적용됩니다`).

**⑤ 자동 통과 흔적** — 통과한 메시지에 범위·잔여를 한 줄로 남긴다.

## 11.7 판정 순서

```
check(tool, input)
  tier == PASS        → 실행
  tier == NOTIFY      → 알림만, 실행
  goal_policy_allows()          → 통과 (프로젝트 일치 필수)
  project_policy_allows()  L3   → 통과 (critical 제외)
  session_policy_allows()  L2   → 통과 (critical 제외)
  is_approved()            L1   → 통과 (카드 소비)
  그 외                          → 카드 발급 후 차단
```

넓은 것을 먼저 본다. 좁은 카드를 아껴 두어야 그 대상에 다시 묻지 않는다.
어느 층이든 통과하면 `used` 를 센다 — 세지 않으면 상한이 장식이 된다.

## 11.8 작업 순서

| 순위 | 조치 | 병렬 | 의존 | 검증기준 |
|---|---|---|---|---|
| P0 | 권한 칩 + 패널(활성표시·회수) | A | 없음 | 칩에 잔여·만료 표시, 회수 후 다시 물음 |
| P0 | `/approvals/active` 프로젝트 필터 보정 | A | 없음 | 다른 세션에서 project 권한 1건 조회 |
| P1 | `/approvals` 페이지 4단계 통일 | B | 없음 | 페이지·팝업 선택지 동일 |
| P1 | 도구군(`tool_group`) 서버+화면 | C | P0 후 | 승인 1회로 write+run 연속 통과 |
| P2 | `project_approval_policies` + 설정 화면 | — | P1 후 | 새 세션이 카드 없이 통과, 감사 남음 |
| P2 | 자동 통과 흔적 1줄 | — | P1 후 | 통과 메시지에 범위·잔여 노출 |

P0 둘만 나가도 체감은 대부분 해결된다. 넓은 권한을 **되돌릴 수 있게**
되는 순간부터 넓게 줄 수 있다.

## 11.9 시험

| # | 항목 | 통과 기준 |
|---|---|---|
| 1 | 권한 칩 | 잔여/상한·만료가 실값으로 표시 |
| 2 | 회수 | 클릭 후 같은 도구 재호출 시 카드 재출현 |
| 3 | 세션 넘김 | 다른 대화에서 project 권한 1건 조회 |
| 4 | 연속 작업 | 승인 1회로 서로 다른 파일 2개 연속 수정 |
| 5 | critical | L2·L3 버튼 미노출 + 서버 차단 로그 |
| 6 | 만료 | 상한 초과·시간 경과 후 자동으로 다시 물음 |

## 11.10 되돌리기

L2·L3 는 **읽는 쪽에 조건을 더하는** 변경이다. 되돌리면 그 조건이
사라지고 L1 카드 판정만 남는다 — 게이트가 느슨해지지 않는다.
컬럼·테이블은 남겨도 무해하다(빈 `{}` 는 아무것도 허용하지 않는다).

## 11.11 열어 둔 것

- 도구군 4종 구성이 맞는지 — `shell` 을 읽기/쓰기로 더 쪼갤지
- L3 를 프로젝트 전체가 아니라 **경로 prefix** 로 더 좁힐지
- `target` 없는 구 승인 카드 46장 처리 — 만료 대기 / 일괄 거절
