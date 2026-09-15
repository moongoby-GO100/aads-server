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
