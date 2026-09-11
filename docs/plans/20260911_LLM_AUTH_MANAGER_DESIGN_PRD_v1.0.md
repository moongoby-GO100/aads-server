# AADS LLM 인증관리 시스템 — 설계서 및 PRD v1.0

**Task ID:** AADS-LLM-AUTH-MANAGER  
**작성일:** 2026-09-11 21:20 KST  
**버전:** v1.0  
**작성자:** AADS CTO AI  
**세션:** AADS-012[LLM 인증관리자]

---

## 1. 현황 분석 (AS-IS)

### 1.1 아키텍처 개요

현재 LLM 인증/키 관리 시스템은 6개 모듈에 분산되어 있다.

| # | 모듈 | 역할 | 라인 수 |
|---|------|------|------:|
| 1 | `app/core/auth_provider.py` | OAuth 토큰 로딩, 순서 교환, 클라이언트 생성 | 385 |
| 2 | `app/core/anthropic_client.py` | 백그라운드 LLM 호출 + 폴백 체인 | 818 |
| 3 | `app/core/llm_key_provider.py` | DB 기반 키 CRUD, Fernet 암호화, 캐시 | 251 |
| 4 | `app/services/model_selector.py` | 채팅 모델 선택, CLI Relay, LiteLLM 분기 | 4,973 |
| 5 | `app/services/model_registry.py` | 모델 자동 발견, 레지스트리 동기화 | ~600 |
| 6 | `app/services/oauth_usage_tracker.py` | OAuth 사용량 추적 로깅 | ~200 |

### 1.2 DB 테이블 현황

| 테이블 | 용도 | 현재 row 수 |
|--------|------|----------:|
| `llm_api_keys` | 12개 프로바이더 키 (Fernet 암호화) | 12 |
| `model_routing_preferences` | 라우트별 모델 우선순위 | 30+ |
| `llm_models` | 모델 레지스트리 (자동 발견) | 524 (실행가능 109) |
| `intent_policies` | 인텐트별 모델 정책 | 7 |
| `chat_model_preferences` | 사용자 모델 선호도 | - |
| `bg_llm_usage_log` | 백그라운드 LLM 사용 로그 | - |
| `llm_model_discovery_runs` | 자동 발견 실행 이력 | - |

### 1.3 현재 폴백 체인 구조

#### A. 채팅 메인 (model_selector.py)

```
사용자 선택 모델
  → intent_policies 다운그레이드 확인
  → Claude CLI Relay (OAuth slot1 → slot2 → 쿨다운 시 교대)
  → LiteLLM 경유 (Anthropic SDK → LiteLLM proxy)
  → 429/한도 시: OAT 토큰 교대 (_switch_oat_token)
```

#### B. 백그라운드 LLM (anthropic_client.py)

```
0순위: BYOK 사용자 키 (user_api_keys)
1순위: Claude OAuth (moong76@gmail, slot:naver PRIMARY)
2순위: Claude OAuth (moongoby@gmail, slot:gmail FALLBACK)
3순위: qwen3-235b (비활성화 — CEO 지시: 유료 DashScope 폴백 금지)
4순위: LiteLLM 저비용 체인 (groq-llama-70b, groq-gpt-oss-120b)
```

#### C. 러너 (pipeline-runner.sh)

```
MODEL_CYCLE 배열 하드코딩 → claude --model 순차 시도
codex:* → codex exec --full-auto
litellm:* → docker exec python3 litellm_runner.py
```

### 1.4 발견된 문제점

| # | 심각도 | 문제 | 위치 | 영향 |
|---|:------:|------|------|------|
| 1 | P0 | **폴백 체인 4곳 하드코딩** — 변경 시 코드 수정+배포 필수 | anthropic_client.py, model_selector.py, auth_provider.py, pipeline-runner.sh | CEO가 즉시 폴백 순서 변경 불가 |
| 2 | P0 | **model_selector.py 4,973행 모놀리식** — 변경 위험도 높음 | model_selector.py | 단일 수정이 채팅 전체에 영향 |
| 3 | P1 | **토큰 쿨다운 상태 분산** — 런타임 메모리 + DB 이중 관리 | auth_provider._token_cooldowns + llm_api_keys.rate_limited_until | 재시작 시 쿨다운 상태 유실 |
| 4 | P1 | **qwen3-235b 유료 폴백 코드 잔존** — 주석으로만 비활성화 | anthropic_client.py:390 | 실수로 재활성화 가능 |
| 5 | P1 | **키 건강성 자동 검증 없음** — 만료/잔액부족 사전 감지 불가 | 전체 | 장애 시점에 발견 |
| 6 | P2 | **폴백 설정 Admin UI 없음** — CEO가 코드 없이 관리 불가 | 대시보드 | 운영 불편 |
| 7 | P2 | **비용 추적 분산** — oauth_usage_tracker + bg_llm_usage_log 분리 | 추적 모듈 2개 | 통합 비용 현황 파악 어려움 |
| 8 | P2 | **524개 모델 중 415개 미사용** — 레지스트리 정리 필요 | llm_models | 선택 UI 혼잡 |
| 9 | P2 | **intent_policies 7개만** — 전체 인텐트 커버리지 부족 | intent_policies | 미등록 인텐트는 레거시 하드코딩 경로 |

---

## 2. 목표 아키텍처 (TO-BE)

### 2.1 설계 원칙

1. **DB-Driven**: 모든 폴백 체인, 모델 순서, 쿨다운 상태를 DB에서 관리
2. **Zero-Deploy 변경**: CEO가 대시보드에서 폴백 순서/활성화를 즉시 변경
3. **단일 진입점**: `call_llm_unified()` → 채팅/백그라운드/러너 모두 통합
4. **자동 건강 검증**: 주기적 키 헬스체크 + 자동 쿨다운/알림
5. **비용 가시성**: 프로바이더별/모델별 비용 대시보드

### 2.2 모듈 구조 (TO-BE)

```
app/core/
├── auth_provider.py          ← 유지 (OAuth 토큰 로딩만, 경량화)
├── anthropic_client.py       ← 유지 (백그라운드 LLM, 폴백 DB화)
├── llm_key_provider.py       ← 유지 (키 CRUD)
├── llm_fallback_engine.py    ← [신규] 통합 폴백 엔진
├── llm_health_checker.py     ← [신규] 키 건강 검증 스케줄러
└── credential_vault.py       ← 유지

app/services/
├── model_selector.py         ← 리팩터링 (4,973→~2,000행, 라우팅만)
├── model_selector_relay.py   ← [신규] CLI Relay 분리
├── model_selector_litellm.py ← [신규] LiteLLM 분리
├── model_registry.py         ← 유지
└── oauth_usage_tracker.py    ← 확장 (통합 비용)

app/api/v1/
├── llm_admin.py              ← [신규] 폴백/키/정책 Admin API
└── ...

대시보드 (Next.js)
├── /admin/llm-auth           ← [신규] LLM 인증 관리 페이지
│   ├── 키 관리 탭 (등록/순서변경/테스트)
│   ├── 폴백 체인 탭 (시각적 편집기)
│   ├── 인텐트 정책 탭 (모델 매핑)
│   └── 비용/사용량 탭 (차트)
```

### 2.3 통합 폴백 엔진 (`llm_fallback_engine.py`)

```python
# 핵심 구조
class FallbackChain:
    """DB 기반 폴백 체인. route_key별 provider+model 순서."""
    
    async def resolve(self, route_key: str, model: str, intent: str) -> list[FallbackStep]:
        """DB에서 폴백 순서 조회, 쿨다운 필터링, 정렬 반환."""
        
    async def execute_with_fallback(self, steps: list[FallbackStep], request) -> Response:
        """순서대로 시도, 실패 시 다음 단계, 쿨다운 자동 기록."""

class FallbackStep:
    provider: str        # anthropic, groq, codex, litellm
    model_id: str        # claude-sonnet-5, groq-llama-70b
    backend: str         # cli_relay, litellm_proxy, openai_direct, dashscope
    priority: int        # DB priority
    is_free: bool        # 무료 여부
    cooldown_until: datetime | None
```

### 2.4 DB 스키마 변경

```sql
-- 1. 폴백 체인 테이블 (신규)
CREATE TABLE llm_fallback_chains (
    id SERIAL PRIMARY KEY,
    route_key TEXT NOT NULL,          -- 'chat_main', 'background', 'runner'
    step_order INT NOT NULL,          -- 1, 2, 3...
    provider TEXT NOT NULL,           -- 'anthropic', 'groq', 'codex'
    model_id TEXT NOT NULL,           -- 'claude-sonnet-5'
    backend TEXT NOT NULL DEFAULT 'auto', -- 'cli_relay', 'litellm_proxy', 'openai_direct'
    is_free BOOLEAN DEFAULT FALSE,
    is_enabled BOOLEAN DEFAULT TRUE,
    max_retries INT DEFAULT 3,
    timeout_seconds INT DEFAULT 120,
    conditions JSONB DEFAULT '{}',    -- {"intent": ["code_modify"], "min_tokens": 1000}
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (route_key, step_order)
);

-- 2. 키 건강 검증 로그 (신규)
CREATE TABLE llm_key_health_log (
    id SERIAL PRIMARY KEY,
    key_name TEXT NOT NULL REFERENCES llm_api_keys(key_name),
    check_type TEXT NOT NULL,         -- 'auth', 'quota', 'latency', 'model_list'
    status TEXT NOT NULL,             -- 'healthy', 'degraded', 'failed'
    response_ms INT,
    error_message TEXT,
    checked_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3. 통합 LLM 비용 로그 (신규)
CREATE TABLE llm_cost_log (
    id BIGSERIAL PRIMARY KEY,
    route_key TEXT NOT NULL,          -- 'chat_main', 'background', 'runner'
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    input_tokens INT DEFAULT 0,
    output_tokens INT DEFAULT 0,
    cost_usd NUMERIC(10,6) DEFAULT 0,
    cache_read_tokens INT DEFAULT 0,
    cache_creation_tokens INT DEFAULT 0,
    session_id TEXT,
    tenant_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_llm_cost_log_daily ON llm_cost_log (created_at, provider);

-- 4. intent_policies 확장 (기존 테이블 ALTER)
ALTER TABLE intent_policies ADD COLUMN IF NOT EXISTS fallback_chain_key TEXT;
ALTER TABLE intent_policies ADD COLUMN IF NOT EXISTS max_cost_per_call NUMERIC(10,6);
ALTER TABLE intent_policies ADD COLUMN IF NOT EXISTS description TEXT;
```

---

## 3. PRD (Product Requirements Document)

### 3.1 제품 목표

> CEO가 코드 수정 없이 대시보드에서 LLM 폴백 체인, 키 순서, 인텐트별 모델 정책, 비용 한도를 실시간 관리할 수 있는 통합 인증관리 시스템.

### 3.2 사용자 시나리오

| # | 시나리오 | 현재 | 개선 후 |
|---|----------|------|---------|
| S1 | Claude 429 발생 → 폴백 순서 변경 | 코드 수정 + 배포 (10분+) | 대시보드 드래그 (3초) |
| S2 | 새 프로바이더 키 등록 | `.env` 수정 + 컨테이너 재시작 | 대시보드에서 등록 + 즉시 반영 |
| S3 | 유료 폴백 비활성화 | 코드 주석 처리 + 배포 | 토글 스위치 (1초) |
| S4 | 인텐트별 모델 변경 | DB INSERT + 캐시 만료 대기 | 대시보드 드롭다운 선택 |
| S5 | 일일 비용 확인 | 수동 쿼리 | 대시보드 차트 자동 |
| S6 | 키 만료 사전 감지 | 장애 발생 후 인지 | 자동 헬스체크 + 텔레그램 알림 |

### 3.3 기능 요구사항

#### FR-1: 통합 폴백 엔진 (Backend)
- FR-1.1: `llm_fallback_chains` 테이블 기반 폴백 순서 관리
- FR-1.2: route_key별 독립 폴백 체인 (chat_main, background, runner)
- FR-1.3: 조건부 폴백 (인텐트, 토큰 수, 시간대 기반)
- FR-1.4: 쿨다운 상태 DB 영속화 (재시작 시 유지)
- FR-1.5: 폴백 실행 시 자동 비용 로깅
- FR-1.6: 무료 모델 우선 폴백 옵션 (CEO 비용 절감 정책)

#### FR-2: 키 관리 강화 (Backend)
- FR-2.1: 대시보드에서 키 등록/수정/비활성화 (Fernet 암호화 유지)
- FR-2.2: 키 순서 변경 API (`PATCH /api/v1/llm-admin/keys/reorder`)
- FR-2.3: 키 테스트 API (`POST /api/v1/llm-admin/keys/test`) — 실제 API 호출 검증
- FR-2.4: 주기적 건강 검증 (30분 간격) + `llm_key_health_log` 기록
- FR-2.5: 연속 실패 3회 시 텔레그램 알림

#### FR-3: 인텐트 정책 관리 (Backend)
- FR-3.1: 전체 인텐트 커버리지 (현재 7 → 15+ 인텐트)
- FR-3.2: 대시보드에서 인텐트별 기본 모델/허용 모델/다운그레이드 편집
- FR-3.3: 인텐트별 비용 한도 설정
- FR-3.4: 정책 변경 즉시 반영 (캐시 무효화 API)

#### FR-4: Admin 대시보드 (Frontend)
- FR-4.1: `/admin/llm-auth` 페이지 — 4개 탭 구성
- FR-4.2: **키 관리 탭**: 프로바이더별 키 목록, 상태, 순서 변경, 테스트 버튼
- FR-4.3: **폴백 체인 탭**: route_key별 시각적 폴백 순서, 드래그 정렬, 활성/비활성 토글
- FR-4.4: **인텐트 정책 탭**: 인텐트×모델 매트릭스 편집
- FR-4.5: **비용/사용량 탭**: 일별/프로바이더별 비용 차트, 토큰 사용량

#### FR-5: 비용 추적 (Backend + Frontend)
- FR-5.1: `llm_cost_log` 통합 비용 로깅 (채팅+백그라운드+러너)
- FR-5.2: 프로바이더별 단가 테이블 (`llm_model_pricing`)
- FR-5.3: 일별 비용 집계 API
- FR-5.4: 일일 비용 한도 초과 시 알림
- FR-5.5: 월간 비용 리포트 자동 생성

#### FR-6: model_selector.py 리팩터링
- FR-6.1: CLI Relay 로직 → `model_selector_relay.py` 분리
- FR-6.2: LiteLLM 호출 로직 → `model_selector_litellm.py` 분리
- FR-6.3: OpenAI-compatible direct 로직 분리
- FR-6.4: 메인 모듈 2,000행 이하로 경량화
- FR-6.5: 하드코딩된 모델 목록/상수 → DB 조회로 전환

### 3.4 비기능 요구사항

| # | 요구사항 | 기준 |
|---|----------|------|
| NFR-1 | 폴백 전환 지연 | < 100ms (DB 조회 포함) |
| NFR-2 | 키 건강 검증 간격 | 30분 (환경변수 조정 가능) |
| NFR-3 | 비용 로그 보존 | 90일 (이후 집계 테이블로 압축) |
| NFR-4 | Admin API 인증 | CEO 전용 세션 검증 |
| NFR-5 | 폴백 체인 캐시 TTL | 60초 (변경 시 즉시 무효화) |
| NFR-6 | 무중단 배포 호환 | reload-api.sh / bluegreen 모두 지원 |

---

## 4. 구현 단계 (Phase Plan)

### Phase 1: 폴백 엔진 DB화 + 하드코딩 제거 (P0, 3일)

**목표:** 폴백 체인을 DB 테이블로 이관하고 하드코딩 제거

| # | 작업 | 대상 파일 | 크기 |
|---|------|-----------|:----:|
| 1-1 | `llm_fallback_chains` 테이블 생성 + 시드 데이터 | 마이그레이션 SQL | S |
| 1-2 | `llm_fallback_engine.py` 신규 모듈 | app/core/ | M |
| 1-3 | `anthropic_client.py` 폴백 체인 DB화 | app/core/ | M |
| 1-4 | qwen3-235b 유료 폴백 코드 완전 제거 | anthropic_client.py | XS |
| 1-5 | 쿨다운 상태 DB 영속화 | auth_provider.py + llm_key_provider.py | S |
| 1-6 | 단위 테스트 | tests/unit/ | M |

**완료 기준:**
- `llm_fallback_chains`에서 폴백 순서 조회 → 실제 폴백 동작 확인
- 쿨다운 상태가 서버 재시작 후에도 유지
- 기존 테스트 전체 PASS

### Phase 2: Admin API + 키 건강 검증 (P1, 3일)

**목표:** Admin REST API와 자동 키 헬스체크

| # | 작업 | 대상 파일 | 크기 |
|---|------|-----------|:----:|
| 2-1 | `llm_admin.py` API 라우터 (CRUD + reorder + test) | app/api/v1/ | M |
| 2-2 | `llm_health_checker.py` 스케줄러 | app/core/ | M |
| 2-3 | `llm_key_health_log` 테이블 + 알림 연동 | 마이그레이션 + telegram | S |
| 2-4 | 인텐트 정책 확장 (7 → 15+) | intent_policies INSERT | S |
| 2-5 | API 테스트 + 통합 테스트 | tests/ | M |

**완료 기준:**
- `/api/v1/llm-admin/chains` CRUD 동작
- 30분 간격 헬스체크 → 실패 시 텔레그램 알림
- 인텐트 정책 전체 커버리지

### Phase 3: 대시보드 UI (P1, 4일)

**목표:** `/admin/llm-auth` 4탭 대시보드 페이지

| # | 작업 | 대상 파일 | 크기 |
|---|------|-----------|:----:|
| 3-1 | 키 관리 탭 (목록/순서변경/테스트) | aads-dashboard/src/ | M |
| 3-2 | 폴백 체인 탭 (시각적 편집기) | aads-dashboard/src/ | L |
| 3-3 | 인텐트 정책 탭 (매트릭스 편집) | aads-dashboard/src/ | M |
| 3-4 | 비용/사용량 탭 (차트) | aads-dashboard/src/ | M |
| 3-5 | E2E 검증 (브라우저 테스트) | - | S |

**완료 기준:**
- CEO가 대시보드에서 폴백 순서 변경 → 3초 내 반영
- 키 테스트 버튼 → 실시간 결과 표시
- 비용 차트 일별/프로바이더별 표시

### Phase 4: model_selector.py 리팩터링 + 비용 통합 (P2, 3일)

**목표:** 모놀리식 모듈 분리 + 비용 로깅 통합

| # | 작업 | 대상 파일 | 크기 |
|---|------|-----------|:----:|
| 4-1 | CLI Relay 분리 → `model_selector_relay.py` | app/services/ | L |
| 4-2 | LiteLLM 분리 → `model_selector_litellm.py` | app/services/ | M |
| 4-3 | `llm_cost_log` 테이블 + 통합 비용 로깅 | 마이그레이션 + 코드 | M |
| 4-4 | 비활용 모델 정리 (524 → ~120) | llm_models 비활성화 | S |
| 4-5 | 회귀 테스트 전체 | tests/ | M |

**완료 기준:**
- model_selector.py 2,000행 이하
- 채팅/백그라운드/러너 모든 LLM 호출의 비용이 단일 테이블에 기록
- 기존 기능 회귀 없음

---

## 5. 리스크 및 대안

| # | 리스크 | 영향 | 대안 |
|---|--------|------|------|
| 1 | 폴백 엔진 전환 중 채팅 장애 | 높음 | Phase 1에서 기존 코드와 병행 운영, feature flag로 전환 |
| 2 | model_selector.py 리팩터링 시 회귀 | 높음 | Phase 4를 마지막으로, 충분한 테스트 후 전환 |
| 3 | Admin API 보안 취약점 | 중간 | CEO 전용 세션 검증 + IP 화이트리스트 |
| 4 | DB 조회 지연으로 폴백 느려짐 | 낮음 | 60초 캐시 + 인메모리 폴백 |

---

## 6. 우선순위 및 일정

```
Phase 1 (P0, 3일) ─── 폴백 DB화 + 하드코딩 제거
    ↓
Phase 2 (P1, 3일) ─── Admin API + 키 헬스체크
    ↓
Phase 3 (P1, 4일) ─── 대시보드 UI 4탭
    ↓
Phase 4 (P2, 3일) ─── model_selector 리팩터링 + 비용 통합
```

**총 예상 기간:** 13일 (Phase 1~2 병렬 가능 시 10일)  
**예상 비용:** 러너 실행 기준 $15~25

---

## 7. 검증 기준

| # | 검증 항목 | 방법 | 성공 기준 |
|---|----------|------|----------|
| V1 | 폴백 DB 전환 | 헬스체크 + 채팅 테스트 | 기존과 동일한 폴백 동작 |
| V2 | 하드코딩 제거 | grep 검색 | 폴백 관련 하드코딩 0건 |
| V3 | Admin API | curl 테스트 | CRUD + reorder + test 정상 |
| V4 | 대시보드 | E2E 스크린샷 | 4탭 정상 렌더링 + 편집 동작 |
| V5 | 비용 로깅 | DB 쿼리 | 모든 LLM 호출 비용 기록 |
| V6 | 키 헬스체크 | 30분 대기 후 로그 확인 | 건강/비건강 상태 정확 기록 |
| V7 | 회귀 테스트 | pytest 전체 | 기존 테스트 100% PASS |

---

## 변경 이력

| 날짜 | 버전 | 변경 내용 |
|------|------|----------|
| 2026-09-11 | v1.0 | 초기 작성 — AS-IS 분석, TO-BE 설계, PRD, 4단계 구현 계획 |
