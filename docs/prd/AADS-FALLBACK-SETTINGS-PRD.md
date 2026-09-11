# PRD: 폴백 모델 설정 관리 페이지

**Task ID**: AADS-PRD-FALLBACK-SETTINGS
**작성일**: 2026-09-11 KST
**요청자**: CEO moongoby
**우선순위**: P1
**크기**: L (프론트 + 백엔드 + DB 연동)

---

## 1. 배경 및 목적

현재 AADS의 LLM 폴백 체인은 소스코드에 하드코딩되어 있어, 변경마다 코드 수정 → 커밋 → 배포가 필요합니다.
CEO 지시에 따라 **모든 폴백 설정을 Ops 설정 페이지에서 실시간으로 관리**할 수 있도록 전환합니다.

### 현재 하드코딩 위치 (전수 조사 결과)

| 파일 | 상수/변수 | 용도 |
|------|-----------|------|
| `anthropic_client.py` | `_BG_FALLBACK_MODELS` | 백그라운드 LLM 최종 폴백 체인 |
| `model_selector.py` | `_CODEX_FB` | Codex CLI 장애 시 Claude 폴백 매핑 |
| `model_selector.py` | `_MODEL_DOWNGRADE` | Claude 등급 내 다운그레이드 체인 |
| `model_selector.py` | `_SAMEGRADE_FALLBACK` | 동급 모델 폴백 (Claude↔Codex) |
| `model_selector.py` | `_GEMINI_SAMEGRADE` | Gemini 실패 시 Claude 전환 |
| `model_selector.py` | `_HAIKU_FALLBACK_INTENTS` | Haiku 우선 인텐트 목록 |
| `model_selector.py` | `_SONNET_INTENTS` | Sonnet 우선 인텐트 목록 |
| `model_selector.py` | `_COOLDOWN_SECS` | 슬롯 쿨다운 시간 (300초) |
| `pipeline_runner_service.py` | `_LITELLM_FALLBACK_MODELS` | Runner LiteLLM 크기별 폴백 |
| `pipeline_runner_service.py` | `_CLAUDE_MODEL_BY_SIZE` | Runner Claude 크기별 매핑 |

---

## 2. 기존 DB 인프라 현황

### 활용 가능한 기존 테이블

| 테이블 | 컬럼 | 현재 상태 |
|--------|-------|-----------|
| `model_routing_preferences` | route_key, provider, model_id, display_order, is_enabled, is_default, notes, family, category | route_key별(llm, runner_llm, video) 모델 우선순위 관리 중. **확장 가능** |
| `runner_model_config` | size, models(jsonb), updated_at, updated_by | Runner 크기별 모델 매핑. 단순 구조. **확장 가능** |

### 결론
신규 테이블 불필요. 기존 2개 테이블을 확장하여 하드코딩 상수를 DB 기반으로 전환.

---

## 3. 기능 요구사항

### 3.1 백엔드 API

#### 3.1.1 폴백 체인 CRUD API (`/api/v1/ops/fallback-config`)

| Method | Endpoint | 설명 |
|--------|----------|------|
| GET | `/fallback-config` | 전체 폴백 설정 조회 (route_key별 그룹핑) |
| GET | `/fallback-config/{route_key}` | 특정 라우트 폴백 체인 조회 |
| PUT | `/fallback-config/{route_key}` | 폴백 체인 순서/활성화 수정 |
| POST | `/fallback-config/{route_key}/models` | 새 모델 추가 |
| DELETE | `/fallback-config/{route_key}/models/{model_id}` | 모델 제거 |
| POST | `/fallback-config/test` | 설정 변경 전 dry-run 검증 |

#### 3.1.2 Route Key 분류

| route_key | 대상 | 설명 |
|-----------|------|------|
| `chat_primary` | 채팅 1순위 | Claude OAuth 체인 |
| `chat_samegrade` | 채팅 동급 폴백 | Claude↔Codex 동급 전환 |
| `chat_downgrade` | 채팅 다운그레이드 | Claude 등급 하향 체인 |
| `chat_bg_fallback` | 채팅 최종 폴백 | 무료 모델 (Groq 등) |
| `runner_primary` | Runner 1순위 | 크기별 Claude/Codex |
| `runner_fallback` | Runner 폴백 | 크기별 대체 모델 |
| `gemini_samegrade` | Gemini 폴백 | Gemini→Claude 전환 |
| `intent_routing` | 인텐트별 모델 | Haiku/Sonnet 인텐트 매핑 |

#### 3.1.3 설정 적용 메커니즘

```
DB 설정 조회 → 있으면 DB 우선
                → 없으면 하드코딩 기본값 폴백 (안전장치)
```

- **캐시**: 메모리 캐시 TTL 60초, `/fallback-config/reload` 엔드포인트로 즉시 무효화
- **이력**: 변경 시 `model_routing_audit_log` 테이블에 who/when/before/after 기록
- **안전장치**: 최소 1개 모델은 항상 활성 (전체 비활성화 차단)

### 3.2 프론트엔드 (대시보드 Ops 설정 페이지)

#### 3.2.1 페이지 위치
`/ops/model-fallback` — Ops 메뉴 하위에 "모델 폴백 설정" 추가

#### 3.2.2 UI 구성

**섹션 A: 채팅 모델 폴백 체인**
- route_key별 카드 형태 표시
- 드래그 앤 드롭으로 우선순위 변경
- 토글 스위치로 모델 활성/비활성
- 모델별 provider, 비용 등급(무료/유료), 가용 상태 표시
- "유료 모델 일괄 비활성화" 버튼

**섹션 B: Runner 모델 설정**
- 크기별(XS/S/M/L/XL) 모델 매핑 표
- 각 크기에 1순위/2순위/3순위 설정
- Claude/Codex/LiteLLM 프로바이더 구분 표시

**섹션 C: 인텐트별 모델 라우팅**
- 인텐트 목록(greeting, casual, search, browser 등)과 매칭 모델
- 인텐트 추가/제거/모델 변경

**섹션 D: 글로벌 설정**
- 쿨다운 시간 (현재 300초)
- LiteLLM 유료 폴백 전역 ON/OFF
- Gemini 폴백 전역 ON/OFF
- CLI Relay 활성화 여부

#### 3.2.3 UX 요구사항
- 변경 사항 미리보기 (현재 vs 변경 후 폴백 체인 비교)
- "저장" 클릭 시 dry-run 검증 후 적용
- 변경 이력 타임라인 (누가 언제 무엇을 변경했는지)
- 현재 활성 폴백 체인 시각화 (화살표 플로우차트)

---

## 4. DB 스키마 변경

### 4.1 `model_routing_preferences` 확장

```sql
-- 기존 컬럼 유지 + 추가 컬럼
ALTER TABLE model_routing_preferences
  ADD COLUMN IF NOT EXISTS cost_tier VARCHAR(10) DEFAULT 'paid',     -- free/paid/premium
  ADD COLUMN IF NOT EXISTS fallback_order INT DEFAULT 999,           -- 폴백 순서 (낮을수록 우선)
  ADD COLUMN IF NOT EXISTS size_scope VARCHAR(5) DEFAULT NULL,       -- Runner용: XS/S/M/L/XL
  ADD COLUMN IF NOT EXISTS intent_scope TEXT[] DEFAULT NULL,         -- 인텐트 라우팅용
  ADD COLUMN IF NOT EXISTS provider_type VARCHAR(20) DEFAULT 'api';  -- api/cli_relay/litellm/local
```

### 4.2 변경 이력 테이블 (신규)

```sql
CREATE TABLE IF NOT EXISTS model_routing_audit_log (
  id SERIAL PRIMARY KEY,
  route_key VARCHAR(50) NOT NULL,
  changed_by VARCHAR(100) NOT NULL,
  changed_at TIMESTAMPTZ DEFAULT NOW(),
  change_type VARCHAR(20) NOT NULL,   -- create/update/delete/reorder
  before_value JSONB,
  after_value JSONB,
  reason TEXT
);
```

---

## 5. 코드 마이그레이션 계획

### Phase 1: DB 읽기 우선 패턴 적용 (1~2일)

각 하드코딩 상수에 DB 조회 → 없으면 기존값 폴백 패턴 적용:

```python
# Before
_SAMEGRADE_FALLBACK = {"claude-fable-5-1": ["gpt-5.6-sol", "claude-opus"], ...}

# After
async def _get_samegrade_fallback() -> dict:
    db_val = await _load_route_config("chat_samegrade")
    return db_val or _SAMEGRADE_FALLBACK  # DB 없으면 하드코딩 폴백
```

### Phase 2: Admin API 구현 (2~3일)
- `/api/v1/ops/fallback-config` CRUD 엔드포인트
- 캐시 레이어 + 무효화
- 감사 로그

### Phase 3: 프론트엔드 UI (3~4일)
- `/ops/model-fallback` 페이지 구현
- 드래그 앤 드롭 순서 변경
- 실시간 폴백 체인 시각화

### Phase 4: 하드코딩 제거 (1일)
- DB 시드 데이터로 현재 하드코딩 값 마이그레이션
- 하드코딩 상수를 DB 미설정 시의 안전 기본값으로만 유지
- E2E 검증

---

## 6. 검증 기준

| # | 항목 | 완료 기준 |
|---|------|-----------|
| 1 | API CRUD | 모든 route_key에 대해 CRUD 정상 동작 |
| 2 | 폴백 체인 변경 | 설정 변경 후 60초 이내 반영 확인 |
| 3 | 안전장치 | 전체 모델 비활성화 시도 → 차단 확인 |
| 4 | 감사 로그 | 변경 이력 정확 기록 확인 |
| 5 | UI 렌더링 | 드래그 앤 드롭, 토글, 저장 정상 동작 |
| 6 | 폴백 시각화 | 현재 활성 체인 화살표 플로우 표시 |
| 7 | 하드코딩 제거 | DB 시드 후 상수 참조 없이 정상 동작 |
| 8 | 롤백 | DB 값 삭제 시 하드코딩 기본값으로 자동 복원 |

---

## 7. 리스크

| 리스크 | 영향 | 대안 |
|--------|------|------|
| DB 장애 시 폴백 설정 조회 불가 | 모델 선택 실패 | 하드코딩 기본값 자동 폴백 (Phase 1에서 보장) |
| 잘못된 설정 적용 | 유료 모델 과금 폭주 | dry-run 검증 + 비용 등급 경고 UI |
| 캐시 무효화 지연 | 설정 변경 후 최대 60초 지연 | /reload 엔드포인트로 즉시 무효화 지원 |

---

## 8. 일정 (예상)

| Phase | 기간 | 산출물 |
|-------|------|--------|
| Phase 1 | 1~2일 | DB 읽기 우선 코드 패턴 |
| Phase 2 | 2~3일 | Admin API + 감사 로그 |
| Phase 3 | 3~4일 | 프론트엔드 설정 UI |
| Phase 4 | 1일 | 하드코딩 제거 + E2E 검증 |
| **합계** | **7~10일** | 전체 폴백 설정 동적 관리 |
