# GO100 P0 상세 설계 보고서

작성 시각: 2026-04-20 16:26:55 KST  
대상: GO100(백억이) 투자 파트너 시스템  
목적: "백억이를 사용자의 실질 투자 파트너로 고도화"하기 위한 P0 설계를 실제 코드/DB 기준으로 정리

## 1. 보고서 요약

현재 GO100은 이미 단순 챗봇 단계는 넘었습니다. 실제로는 다음 4가지를 이미 일부 갖고 있습니다.

1. 멀티 LLM 실행 경로
2. KIS/KIWOOM 브로커 분기 실거래 엔진
3. 목표(go100_goals)와 전략카드(go100_strategy_cards), 포트폴리오(go100_portfolios) 테이블
4. 가설 진화(go100_strategy_hypotheses)와 라이브 주문(go100_live_orders) 이력

문제는 이 요소들이 하나의 "사용자 자산 운영 OS"로 연결되어 있지 않다는 점입니다.

핵심 병목은 아래 5개입니다.

1. 프롬프트/응답 계층에 강한 금지 규칙이 남아 있어 투자 파트너 톤과 실행성이 제한됩니다.
2. LLM 호출 경로가 `agent_core`와 `llm_gateway`로 이원화되어 인증/모델 정책이 분산돼 있습니다.
3. 멀티브로커는 코드에 존재하지만 사용자 중심 자산 통합 계층이 없습니다.
4. 목표 엔진은 저장은 되지만 운영 엔진과 자동 연결되지 않습니다.
5. 전략 진화는 데이터가 쌓이지만 승격이 멈춰 있어 성장 엔진이 닫혀 있습니다.

추가로, 일부 GO100 도구는 여전히 `user_id = 2`를 하드코딩하고 있어 "사용자 개인 투자 파트너"로 확장하는 데 구조적 장애가 있습니다.

이 보고서의 결론은 단순합니다.

백억이를 "모든 것을 할 수 있는 투자 파트너"로 만들려면, 금제를 무작정 해제하는 것이 아니라 아래 순서로 P0를 실행해야 합니다.

1. P0-1 멀티브로커 자산 OS
2. P0-2 Goal OS
3. P0-3 전략 진화 승격 OS
4. P0-4 LLM Control Plane 통합
5. P0-5 단일 사용자 하드코딩 제거 및 권한 체계 정비

---

## 2. 근거 범위

본 보고서는 아래 실측과 실제 코드에 근거합니다.

### 2.1 코드 확인 파일

- `backend/app/services/go100/ai/prompt_layers/core.py`
- `backend/app/services/go100/ai/prompts.py`
- `backend/app/services/go100/ai/agent_core.py`
- `backend/app/services/go100/ai/tool_executors.py`
- `backend/app/services/go100/goal/goal_engine.py`
- `backend/app/services/go100/ai/goal_engine.py`
- `backend/app/services/go100/live_trading/live_engine.py`
- `backend/app/services/go100/risk_engine.py`
- `backend/app/services/go100/risk/position_sizing.py`
- `backend/app/routers/go100/ai_router.py`
- `backend/app/routers/go100/live_trading_router.py`
- `backend/app/core/llm_gateway.py`
- `scripts/go100_relay_server.py`

### 2.2 운영 DB 실측 요약

2026-04-20 16:26 KST 기준 조회 결과:

| 항목 | 실측값 | 출처 |
|---|---:|---|
| 목표 수 | 6 | [DB 조회] |
| ACTIVE 목표 수 | 5 | [DB 조회] |
| PLANNING 목표 수 | 1 | [DB 조회] |
| 활성 목표 보유 사용자 수 | 3 | [DB 조회] |
| 전략 가설 수 | 535 | [DB 조회] |
| BT_PASS 가설 수 | 25 | [DB 조회] |
| BT_FAIL 가설 수 | 237 | [DB 조회] |
| PENDING 가설 수 | 242 | [DB 조회] |
| BT_PASS 후 카드 미생성 수 | 25 | [DB 조회] |
| 전략 카드 수 | 86 | [DB 조회] |
| live 카드 수 | 11 | [DB 조회] |
| ACTIVE 카드 수 | 0 | [DB 조회] |
| 포트폴리오 수 | 19 | [DB 조회] |
| ACTIVE 포트폴리오 수 | 18 | [DB 조회] |
| is_live=true 포트폴리오 수 | 6 | [DB 조회] |
| live_trading_config 수 | 1 | [DB 조회] |
| enabled live_trading_config 수 | 1 | [DB 조회] |
| 라이브 주문 수 | 109 | [DB 조회] |
| FILLED 주문 수 | 104 | [DB 조회] |
| 모델 라우팅 row 수 | 18 | [DB 조회] |
| GPT primary 라우팅 수 | 0 | [DB 조회] |
| Claude primary 라우팅 수 | 3 | [DB 조회] |
| Gemini primary 라우팅 수 | 15 | [DB 조회] |
| user profile 수 | 0 | [DB 조회] |
| risk rule 수 | 3 | [DB 조회] |
| risk disclaimer 수 | 0 | [DB 조회] |

### 2.3 계좌/브로커 실측

`accounts` 테이블 집계:

| broker_type | is_mock | is_active | count | 출처 |
|---|---|---|---:|---|
| KIS | false | true | 1 | [DB 조회] |
| KIS | true | true | 2 | [DB 조회] |
| KIS | true | false | 2 | [DB 조회] |
| KIWOOM | false | true | 2 | [DB 조회] |
| KIWOOM | true | true | 1 | [DB 조회] |

`go100_portfolios`와 `accounts` 조인 결과:

| broker_type | portfolio_count | live_count | active_count | 출처 |
|---|---:|---:|---:|---|
| KIS | 9 | 2 | 9 | [DB 조회] |
| KIWOOM | 7 | 4 | 7 | [DB 조회] |
| UNSET | 3 | 0 | 2 | [DB 조회] |

---

## 3. 현재 백억이에 걸린 실제 제약

### 3.1 언어/응답 금제

`backend/app/services/go100/ai/prompt_layers/core.py:7-19`에 다음 규칙이 직접 박혀 있습니다.

- "데이터 기반 분석만 제공"
- "투자 권유가 아닌 정보 제공임을 명시"
- "미래 주가 예측 절대 금지"
- "매수/매도 직접 추천 금지"
- "확인되지 않은 루머/뉴스 언급 금지"
- "도구 없이 수치 날조 금지"

또한 `backend/app/services/go100/ai/prompts.py:8-14`에는 할루시네이션 가드가 별도로 중복 선언되어 있습니다.

평가:

- 이 규칙은 "날조 방지" 측면에서는 필요합니다.
- 하지만 "투자 파트너" 역할을 하려면 금지 문구 전체를 유지하는 대신, `분석 → 제안 → 실행대기 → 승인 → 주문` 구조로 재정의해야 합니다.
- 즉, 금지를 제거하는 것이 아니라 "행동 가능한 제안 허용"으로 바꿔야 합니다.

### 3.2 LLM 호출 경로 금제

현재 GO100은 2개의 제어 평면이 공존합니다.

1. `backend/app/services/go100/ai/agent_core.py`
2. `backend/app/core/llm_gateway.py`

`agent_core.py` 기준:

- `gemini-*` → Google GenAI 직접
- `claude-*` → Anthropic OAuth 직접 또는 CLI Relay
- `gpt-*` → Codex Relay
- `deepseek-*` 등 → LiteLLM

근거:

- `agent_core.py:46-58`
- `agent_core.py:88-131`
- `agent_core.py:294-304`
- `agent_core.py:450-465`
- `agent_core.py:566-579`

반면 `llm_gateway.py`는 별도 라우팅 테이블과 failover 체인을 가집니다.

근거:

- `llm_gateway.py:80-116`
- `llm_gateway.py:137-159`

평가:

- 현재 구조는 "기능은 된다".
- 하지만 인증정책, 모델승인정책, 비용정책, 장애대응이 두 군데로 갈라져 있어 운영자가 한 번에 통제하기 어렵습니다.
- "백억이의 두뇌"가 하나가 아니라 두 개라는 의미입니다.

### 3.3 멀티브로커는 존재하지만 자산 OS는 없음

`live_engine.py`는 실제로 KIS/KIWOOM 분기를 구현하고 있습니다.

근거:

- `live_engine.py:109-115` executor 분기
- `live_engine.py:494-520` KIWOOM/KIS executor 분기
- `live_engine.py:414-437` 브로커별 잔고 조회

즉, "코드상 멀티브로커 지원"은 이미 있습니다.

그러나 사용자 관점에서는 아래가 없습니다.

1. 계좌별 자산을 하나의 투자 OS로 묶는 총계층
2. 계좌별 리스크 버짓 분배 엔진
3. 브로커별 성과 비교/우선순위 엔진
4. 사용자가 연결한 모든 증권사 현황을 한 화면에서 보는 자산 통합 뷰

결론:

- 브로커 연결은 이미 P0 출발점에 와 있습니다.
- 부족한 것은 "브로커 실행기"가 아니라 "브로커 통합 운영체제"입니다.

### 3.4 Goal Engine은 저장되지만 운영 시스템이 아님

`backend/app/services/go100/goal/goal_engine.py`는 꽤 발전해 있습니다.

- CAGR 계산
- 위험성향 분류
- 단계별 plan_phases 생성
- 전략 intent 생성
- 몬테카를로
- 목표 생성/조회/진행률 갱신

근거:

- `goal/goal_engine.py:62-155`
- `goal/goal_engine.py:157-203`
- `goal/goal_engine.py:206-258`

반면 `backend/app/services/go100/ai/goal_engine.py`도 별도로 존재합니다.  
라우터는 `goal/goal_engine.py`를 사용합니다.

근거:

- `ai_router.py:87`
- `goal_router.py:19`

평가:

- 목표 엔진이 중복 구현돼 있습니다.
- 실제 DB에는 목표 6건이 있으나 user profile은 0건입니다.
- 즉, 목표는 저장되지만 사용자 장기 운영 상태와 연결되지 않습니다.
- `tool_executors.py:1067-1070`의 `get_goal_progress()`는 `user_id = 2` 하드코딩과 `status = 'active'` 소문자 조건을 사용합니다. 현재 운영 데이터는 `ACTIVE` 대문자이므로 이 도구는 사실상 실제 목표를 읽지 못할 가능성이 큽니다.

### 3.5 전략 진화는 쌓이지만 승격이 막혀 있음

실측:

- 가설 535건 [DB 조회]
- BT_PASS 25건 [DB 조회]
- created_card_id not null 0건 [DB 조회]
- BT_PASS 후 카드 미생성 25건 [DB 조회]

관련 코드:

- `strategy_evolution.py`에는 `created_card_id` 업데이트 경로가 존재
- `tool_executors.py:1659-1698`는 가설 목록 노출 가능

평가:

- 시스템은 "전략 진화"를 할 수 있도록 설계되었지만 운영에서는 "검증 통과 후 카드 승격"이 막혀 있습니다.
- 이는 백억이가 시간이 지날수록 강해지는 구조를 스스로 잃고 있다는 뜻입니다.

### 3.6 리스크 엔진은 존재하지만 사용자별 운영정책이 미완성

실제 코드:

- `risk_engine.py`는 pre-trade 검사, 일일손실 검사, kill switch, risk status 제공
- 기본 규칙: 일일 -3%, 종목당 20%, 섹터 40%
- 킬스위치 해제는 `user_id = 2` CEO 전용

근거:

- `risk_engine.py:1-10`
- `risk_engine.py:40`
- `risk_engine.py:214-238`
- `risk_engine.py:493-505`
- `risk_engine.py:543-549`

실측:

- risk_rules 3건 모두 `user_id = 2` [DB 조회]
- risk_disclaimer 0건 [DB 조회]

평가:

- 현재 리스크 체계는 "서비스 전체의 기본 실험 가드" 수준입니다.
- 사용자별 버짓, 목표별 위험한도, 계좌별 손실한도, 전략별 kill switch가 아직 OS 레벨로 정리되어 있지 않습니다.

### 3.7 단일 사용자 하드코딩이 남아 있음

실제 코드:

- `tool_executors.py:592`
- `tool_executors.py:602`
- `tool_executors.py:620`
- `tool_executors.py:1070`
- `risk_engine.py:40`

평가:

- 이 상태에서는 백억이가 "모든 사용자"의 투자 파트너가 아니라 "특정 운영 사용자(user_id=2) 중심 서비스"로 굳어집니다.
- CEO용 실험기에서 개인 자산 OS로 넘어가려면 가장 먼저 걷어내야 할 기술부채입니다.

---

## 4. P0 설계 원칙

P0의 목표는 안전장치를 제거하는 것이 아닙니다. 아래 형태로 바꾸는 것입니다.

1. 금지형 시스템 → 승인형 시스템
2. 단일 계좌형 구조 → 멀티브로커 자산 OS
3. 응답형 챗봇 → 목표 중심 운영형 파트너
4. 고정 전략형 구조 → 진화/승격형 구조
5. 모델별 임시 연결 → 정책 기반 통합 Control Plane

P0 설계 원칙:

1. 사용자가 자산 목표를 입력하면 Goal OS가 운영 기준이 된다.
2. Goal OS가 브로커별 자산 버짓을 나눈다.
3. 전략 진화 엔진은 목표/버짓에 맞는 카드만 승격한다.
4. LLM은 임의로 말하지 않고 Control Plane 정책에 따라 선택된다.
5. 주문은 항상 승인 레벨과 리스크 레벨을 통과한다.
6. 모든 조회/제안/실행은 `user_id`와 `account_id` 중심으로 추적된다.

---

## 5. P0-1 멀티브로커 자산 OS

### 5.1 목표

사용자가 연결한 KIS/KIWOOM 계좌를 단순히 "주문 가능한 계좌 목록"이 아니라 하나의 자산 운영 시스템으로 승격합니다.

### 5.2 현재 상태

이미 있는 것:

- `accounts`에 브로커 정보 존재
- `go100_portfolios.account_id`로 포트폴리오-계좌 연결
- `live_engine`에서 KIS/KIWOOM executor 분기
- 정합성 검증 API 존재 (`live_trading_router.py:177-190`)

없는 것:

- 사용자 전체 자산 총계 view
- 계좌별 투자 목적 태그
- 계좌별 위험 버짓/전략 버짓
- 브로커별 성과 비교
- 브로커별 주문 권한 정책

### 5.3 목표 구조

```text
User
  -> Broker Accounts
    -> Account Buckets
      -> Goal Allocation
        -> Strategy Portfolio
          -> Live Orders / Positions / Risk
```

핵심 개념:

- `accounts`: 실제 연결 계좌
- `go100_account_buckets`: 계좌의 역할 정의
  - 예: 공격형, 장기형, 현금대기, 실험용
- `go100_goal_allocations`: 목표별 자금 배분
- `go100_execution_policies`: 브로커/계좌별 주문 정책

### 5.4 신규 DB 설계

#### a. `go100_account_buckets`

목적: 계좌를 단순 브로커 연결이 아니라 운용 단위로 승격

권장 컬럼:

- `bucket_id`
- `user_id`
- `account_id`
- `bucket_name`
- `bucket_role`
- `target_goal_id`
- `risk_budget_pct`
- `max_daily_loss_pct`
- `max_position_pct`
- `is_primary`
- `status`
- `created_at`
- `updated_at`

#### b. `go100_goal_allocations`

목적: 목표별 자본이 어느 계좌/버킷에 얼마 배정되는지 기록

권장 컬럼:

- `allocation_id`
- `goal_id`
- `bucket_id`
- `allocated_capital`
- `allocation_pct`
- `rebalance_rule`
- `status`
- `created_at`
- `updated_at`

#### c. `go100_execution_policies`

목적: 브로커/계좌별 실행 정책

- `policy_id`
- `account_id`
- `allow_market_order`
- `allow_after_hours`
- `max_orders_per_day`
- `require_manual_approval`
- `max_order_amount`
- `status`

### 5.5 핵심 API 설계

#### `GET /api/go100/accounts/overview`

반환:

- 총 자산
- 계좌별 평가금액
- 브로커별 비중
- 미연결/오류 계좌
- 목표 미할당 자금

#### `POST /api/go100/accounts/buckets`

용도:

- 계좌 역할 생성
- 계좌를 목표와 연결

#### `POST /api/go100/accounts/rebalance`

용도:

- 목표 달성률/위험도 기반으로 버킷별 자금 재배치

### 5.6 AI 도구 설계

새 도구:

- `get_account_unified_overview`
- `allocate_goal_to_accounts`
- `rebalance_account_buckets`
- `compare_broker_performance`
- `set_execution_policy`

이 도구들은 기존 `agent_tools`에 추가하되, 반드시 `user_id`와 `account_id`를 입력받도록 설계해야 합니다.

### 5.7 UI 설계

백억이 채팅과 커맨드센터에 아래 3개 패널 추가:

1. 전체 자산 현황
2. 목표별 자금 배분
3. 브로커별 성과/위험 비교

핵심 UI 규칙:

- "총 자산"
- "목표별 배분"
- "오늘 손익"
- "리스크 경고"
- "주문 대기"

를 한 화면에서 보여줘야 합니다.

### 5.8 리스크 정책

멀티브로커 OS에서는 리스크 규칙도 단일 사용자 수준에서 버킷 수준으로 내려가야 합니다.

현재:

- `go100_risk_rules` 3건
- 모두 `user_id = 2`

개선:

- `user_id` 공통 규칙
- `bucket_id` 규칙
- `goal_id` 규칙
- `account_id` 규칙

의 4단계 우선순위로 재설계

### 5.9 완료 기준

1. 사용자가 연결한 모든 계좌의 총 자산을 1개 API에서 조회 가능
2. 각 목표가 어느 계좌 자금을 쓰는지 보임
3. 계좌별 주문 정책 차등 적용 가능
4. 브로커 정합성 검증이 대시보드에서 한 번에 가능

### 5.10 러너 작업 분해

1. 계좌 통합 DB 스키마 추가
2. 계좌 개요 API/서비스 구현
3. 목표-계좌 배분 로직 구현
4. 채팅 도구 추가
5. 커맨드센터 UI 추가

---

## 6. P0-2 Goal OS

### 6.1 목표

현재의 "목표 저장"을 "목표 운영 시스템"으로 승격합니다.

### 6.2 현재 문제

실측:

- 목표 6건
- ACTIVE 5건
- PLANNING 1건
- user profile 0건

즉, 목표는 존재하지만 사용자 운영 컨텍스트가 비어 있습니다.

또한 목표 엔진이 2곳에 분산되어 있습니다.

- `go100/goal/goal_engine.py` 실제 사용
- `go100/ai/goal_engine.py` 레거시 잔존

### 6.3 목표 구조

Goal OS는 아래 상태 머신이 필요합니다.

```text
PLANNING
  -> APPROVED
  -> FUNDING
  -> ACTIVE
  -> PAUSED
  -> ACHIEVED
  -> FAILED
  -> ARCHIVED
```

현재 DB에는 `ACTIVE/PLANNING`만 사실상 쓰이고 있어 상태 의미가 약합니다.

### 6.4 Goal OS 핵심 기능

#### a. 목표 템플릿화

목표를 단순 숫자가 아니라 운영 계약으로 저장

- 목표 자산
- 목표 기간
- 허용 MDD
- 월 추가 납입
- 허용 레버리지
- 허용 브로커
- 자동실행 허용 레벨

#### b. 목표 단계화

기존 `plan_phases`를 실제 운영 규칙으로 전환

- Phase A: 자본 성장기
- Phase B: 안정/복리기
- Phase C: 방어/유지기

#### c. 목표 기반 전략 우선순위

동일 전략이라도 목표마다 승격 기준이 달라져야 합니다.

예:

- 100만원 → 100억: 공격형, 승격 조건 완화
- 은퇴형 목표: 방어형, 승격 조건 강화

### 6.5 DB 확장

`go100_goals` 추가 권장 컬럼:

- `goal_type`
- `monthly_contribution`
- `max_drawdown_limit`
- `execution_mode`
- `approval_policy`
- `target_broker_scope`
- `goal_policy_json`
- `last_rebalanced_at`

신규 테이블:

#### `go100_goal_events`

- 목표 상태 변화
- 전략 배정
- 자금 이동
- 경고/달성/실패 기록

#### `go100_goal_strategy_links`

- 목표와 전략카드 연결
- 각 전략 비중
- 전략 역할(공격/방어/현금대기)

### 6.6 Goal OS API

- `POST /api/go100/goals/{goal_id}/approve`
- `POST /api/go100/goals/{goal_id}/activate`
- `POST /api/go100/goals/{goal_id}/pause`
- `POST /api/go100/goals/{goal_id}/allocate-strategies`
- `GET /api/go100/goals/{goal_id}/operating-status`

### 6.7 AI 동작 변화

백억이 응답은 "좋은 목표네요" 수준이 아니라 다음 형식으로 바뀌어야 합니다.

1. 목표 현실성 판단
2. 필요한 CAGR과 위험 수준 제시
3. 계좌 배분안 제시
4. 전략군 제시
5. 승인 요청
6. 승인 후 자동 운영 시작

### 6.8 완료 기준

1. 목표가 ACTIVE가 되면 전략과 계좌가 자동 연결됨
2. 목표 진행률이 실제 자산 기준으로 계산됨
3. 목표 상태 변화가 이벤트 로그로 남음
4. 목표별 승인 정책이 적용됨

### 6.9 러너 작업 분해

1. Goal OS 상태머신/스키마
2. 목표-전략 연결 테이블
3. 목표 운영 서비스
4. 목표 운영 대시보드
5. 채팅 승인 플로우

---

## 7. P0-3 전략 진화 승격 OS

### 7.1 목표

가설이 쌓이기만 하고 끝나는 구조를 "검증 통과 → 카드 승격 → 포트폴리오 배정" 구조로 바꿉니다.

### 7.2 현재 상태

실측:

- 가설 535건
- BT_PASS 25건
- CARD_CREATED 연결 0건

이 수치는 현재 백억이가 "배우고는 있지만 승진시키지 못하는" 상태라는 뜻입니다.

### 7.3 근본 원인

1. 가설 검증 통과 후 자동 승격 파이프가 실운영에서 닫혀 있음
2. 목표별 승격 기준이 없음
3. 카드 생성 후 포트폴리오 배정이 약함
4. 승격 실패 사유의 운영 노출이 부족함

### 7.4 목표 구조

```text
PENDING
 -> TESTING
 -> BT_PASS
 -> REVIEW_QUEUE
 -> CARD_CREATED
 -> PAPER_LIVE
 -> LIVE_ELIGIBLE
 -> LIVE_ACTIVE
```

현재는 `BT_PASS`와 `created_card_id` 사이의 OS가 약합니다.

### 7.5 승격 정책 엔진

신규 서비스: `strategy_promotion_service`

판단 요소:

1. 백테스트 성능
2. MDD
3. 거래 빈도
4. 목표 적합도
5. 현재 시장 레짐 적합도
6. 브로커 실행 가능성
7. 기존 전략과 중복도

### 7.6 DB 확장

#### `go100_strategy_hypotheses`

추가 권장:

- `promotion_status`
- `promotion_score`
- `promotion_reason`
- `target_goal_id`
- `eligible_account_scope`

#### `go100_strategy_promotion_reviews`

- `review_id`
- `hypothesis_id`
- `review_type`
- `score_breakdown`
- `decision`
- `decided_by`
- `decided_at`

### 7.7 자동 승격 규칙

예시:

- BT_PASS
- MDD < 목표 허용치
- 최근 레짐 적합
- 유사 카드 중복도 낮음
- 계좌 버킷에 빈 슬롯 존재

를 만족하면 `REVIEW_QUEUE`로 이동

이후:

- 자동 승인 정책이면 `CARD_CREATED`
- 수동 승인 정책이면 CEO/사용자 승인 대기

### 7.8 UI/운영

필수 화면:

1. 승격 대기 큐
2. 승격 사유/거절 사유
3. 목표별 추천 전략
4. 적용 가능 계좌 목록

### 7.9 완료 기준

1. BT_PASS 가설은 자동으로 REVIEW_QUEUE 이상 상태로 이동
2. 승격률/거절률/거절사유가 집계됨
3. 승격된 카드가 목표/계좌에 배정 가능
4. 더 이상 `BT_PASS but no card`가 누적되지 않음

### 7.10 러너 작업 분해

1. 승격 서비스/상태 전이 구현
2. promotion review 테이블 추가
3. 배치/스케줄러 연결
4. 대시보드 UI 추가

---

## 8. P0-4 LLM Control Plane 통합

### 8.1 목표

Claude CLI Relay, GPT Codex CLI Relay, 기타 LiteLLM, Gemini 직접 호출을 "정책 기반 단일 제어 평면"으로 통합합니다.

### 8.2 현재 상태

이미 존재하는 경로:

- Claude: CLI Relay + direct OAuth
- GPT: Codex Relay
- Gemini: direct SDK
- 기타 모델: LiteLLM

근거:

- `scripts/go100_relay_server.py:34-66`
- `agent_core.py:46-58`
- `agent_core.py:294-304`
- `agent_core.py:450-465`
- `agent_core.py:566-579`

문제:

- `llm_gateway.py`도 별도 모델 정책을 가짐
- `go100_model_routing`에는 GPT primary 라우팅이 0건
- 일부 레거시 코드가 여전히 Gemini key/Anthropic key 직참조

실측:

- model routing 18건
- GPT primary 0건
- Claude primary 3건
- Gemini primary 15건

### 8.3 목표 구조

신규 공통 규칙:

- Claude 계열: 무조건 CLI Relay 우선
- GPT 계열: 무조건 Codex Relay 우선
- 나머지 외부 모델: 무조건 LiteLLM
- Gemini 직접 사용은 예외 경로로만 허용

즉, "인증 소스"와 "실행 경로"를 분리합니다.

### 8.4 단일 정책 엔진

신규 서비스: `go100_llm_control_plane`

책임:

1. 모델 선택
2. 인증 방식 선택
3. 비용 정책
4. failover 정책
5. 사용자/목표/의도 기반 모델 승격
6. 감사 로그 저장

### 8.5 DB/설정 확장

#### `go100_model_routing`

추가 권장:

- `execution_path`
- `auth_policy`
- `cost_tier`
- `approved_for_trading`
- `approved_for_autonomous_mode`

#### `go100_model_usage_audit`

- request_id
- user_id
- goal_id
- intent
- selected_model
- execution_path
- auth_policy
- fallback_chain
- latency_ms
- token_usage
- cost_usd

### 8.6 단계별 통합 전략

#### 1단계

`agent_core`를 기준 제어면으로 승격

#### 2단계

`llm_gateway` 사용 지점을 어댑터 뒤로 숨김

#### 3단계

의도별/목표별/브로커별 모델 정책 적용

#### 4단계

자동 모드와 수동 모드 분리

### 8.7 완료 기준

1. 모델 호출 정책이 한 서비스에서 결정됨
2. 모델별 인증 경로가 문서화/로그화됨
3. GPT primary 라우팅도 DB에서 제어 가능
4. 챗봇/보고서/브리핑/전략 생성이 동일 정책을 따름

### 8.8 러너 작업 분해

1. control plane 서비스 추가
2. agent_core와 llm_gateway 어댑터화
3. model_routing 스키마 확장
4. 감사 로그 추가

---

## 9. P0-5 단일 사용자 하드코딩 제거

### 9.1 목표

백억이를 CEO 실험 계정 중심 구조에서 모든 사용자 대상 투자 파트너 구조로 바꿉니다.

### 9.2 현재 문제

실제 코드에서 `user_id = 2` 하드코딩이 남아 있습니다.

대표 예:

- `tool_executors.py:592`
- `tool_executors.py:602`
- `tool_executors.py:620`
- `tool_executors.py:1070`
- `risk_engine.py:40`

이 중 일부는 단순 테스트 흔적이 아니라 실제 도구 로직입니다.

특히 `get_goal_progress()`는:

- `user_id = 2` 고정
- `status = 'active'` 소문자

라서 현재 운영 데이터와 불일치합니다.

### 9.3 정리 방향

1. 모든 GO100 서비스는 `effective_user_id`를 명시 입력받음
2. 도구 실행 시 `context.user_id`를 강제 주입
3. CEO 전용 권한은 role/policy로 분리
4. 하드코딩된 상태값은 enum 상수화

### 9.4 권한 모델

권장:

- `owner`
- `advisor`
- `trader`
- `auditor`
- `ceo_override`

킬스위치 해제 같은 기능은 `user_id == 2`가 아니라 `ceo_override` 권한으로 변경해야 합니다.

### 9.5 완료 기준

1. GO100 코드에서 `user_id = 2` 제거
2. 모든 도구/라우터가 현재 사용자 기준으로 동작
3. 운영자 권한은 role/policy 기반

---

## 10. 실행 순서

P0는 아래 순서가 맞습니다.

### Phase 1

- P0-5 단일 사용자 하드코딩 제거
- P0-4 LLM Control Plane 기초 통합

이유:

- 운영 권한과 모델 정책이 정리되지 않으면 이후 자동화가 모두 위험해집니다.

### Phase 2

- P0-1 멀티브로커 자산 OS

이유:

- "사용자의 연결된 모든 증권사 상황"을 보려면 이 계층이 먼저 필요합니다.

### Phase 3

- P0-2 Goal OS

이유:

- 자산 총계를 목표와 연결해야 백억이가 "무엇을 위해 운용하는지"를 알 수 있습니다.

### Phase 4

- P0-3 전략 진화 승격 OS

이유:

- 목표와 자산 버킷이 정의된 뒤에야, 어떤 전략을 승격시킬지 의미가 생깁니다.

---

## 11. 바로 투입할 러너 작업 패키지

### Runner A

제목: `GO100-P0-USER-CONTEXT-CLEANUP`

범위:

- `tool_executors.py`의 `user_id=2` 제거
- GO100 상태 문자열 enum 정리
- risk_engine CEO 권한 로직 role 기반으로 치환

### Runner B

제목: `GO100-P0-LLM-CONTROL-PLANE`

범위:

- `agent_core`/`llm_gateway` 통합 어댑터
- 모델 실행 경로 표준화
- 모델 사용 감사 로그 추가

### Runner C

제목: `GO100-P0-ACCOUNT-OS`

범위:

- account bucket/allocation 스키마
- 계좌 통합 개요 API
- 브로커 비교 서비스

### Runner D

제목: `GO100-P0-GOAL-OS`

범위:

- goal 상태머신
- goal events
- goal-strategy links
- goal operating status API

### Runner E

제목: `GO100-P0-STRATEGY-PROMOTION-OS`

범위:

- promotion queue
- BT_PASS 자동 승격
- promotion review 로그
- 대시보드 연결

---

## 12. 최종 결론

백억이를 더 강하게 만드는 방향은 명확합니다.

지금 필요한 것은 "매수/매도 금지 문구 삭제"가 아닙니다.  
필요한 것은 "사용자 목표를 기준으로, 연결된 모든 계좌를 통합하고, 적절한 모델과 전략을 선택해, 승인 가능한 형태로 제안하고, 리스크 통제 아래 실행하는 운영체계"입니다.

현재 GO100은 그 기반을 이미 상당 부분 갖고 있습니다.

- 멀티브로커 분기 있음
- 실거래 엔진 있음
- 목표 엔진 있음
- 가설 엔진 있음
- 모델 라우팅 있음

하지만 이것들이 각각 따로 놀고 있습니다.

P0의 본질은 기능 추가가 아니라 "운영체계로 묶는 것"입니다.

정리하면:

1. 먼저 사용자/권한/모델 제어면을 정리하고
2. 계좌를 자산 OS로 묶고
3. 목표를 운영 중심축으로 세우고
4. 진화된 전략을 실제 승격시키면

그때부터 백억이는 단순 분석 AI가 아니라 "사용자의 자산을 장기 목표에 맞춰 실제로 운용하는 투자 파트너"가 됩니다.
