# 지시서 생성 모델 어드민 설정 PRD v1.0

- 문서 상태: 구현 승인
- 작성일: 2026-09-12 KST
- 대상: AADS 백엔드, 대시보드 Ops 세팅 페이지
- 관련 PRD: 20260910_OHVIS_DIRECTIVE_COPILOT_PRD.md

## 1. 문제

지시서 자동생성 시스템(`directive_draft_service.py`)의 LLM 모델이 `claude-haiku-4-5-20251001`로 하드코딩되어 있다.
- 91.7%가 폴백 템플릿으로 전환 (12건 중 11건 LLM 실패)
- CEO가 모델을 변경하려면 코드 수정 → 커밋 → 배포가 필요
- 러너 모델 설정은 이미 DB + Ops 세팅 UI로 실시간 변경 가능하나, 지시서 생성은 불가

## 2. 목표와 비목표

### 목표

1. 지시서 생성 모델을 DB 기반으로 관리하고, Ops 세팅 페이지에서 CEO가 직접 변경한다.
2. 러너 모델 설정(`runner_model_config`)과 동일한 패턴(우선순위 배열 + UPSERT + 폴백 체인)을 적용한다.
3. 초기 설정: 1순위 `claude-sonnet-5`, 2순위 `codex:gpt-5.6-tela`.
4. 코드 배포 없이 모델 변경이 즉시 반영된다.
5. 토큰/비용 추적 필드를 directive_drafts에 추가한다.

### 비목표

- 러너 모델 설정 자체를 변경하지 않는다 (이미 완료).
- 지시서 생성 프롬프트 내용을 이번 범위에서 변경하지 않는다.
- 자동 전송·자동 승인 정책은 이번 범위가 아니다.

## 3. 데이터 모델

### 신규 테이블: `directive_model_config`

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `role` | `varchar(50)` PK | 역할 키. `generation` = 지시서 초안 생성 |
| `models` | `jsonb` | 모델 ID 우선순위 배열. 예: `["claude-sonnet-5", "codex:gpt-5.6-tela"]` |
| `timeout_seconds` | `integer` DEFAULT 60 | LLM 호출 타임아웃 |
| `max_tokens` | `integer` DEFAULT 2000 | 최대 생성 토큰 |
| `updated_at` | `timestamptz` DEFAULT NOW() | 마지막 수정 시각 |
| `updated_by` | `varchar(100)` DEFAULT 'CEO' | 수정자 |

**역할 확장**: 현재는 `generation`만 사용. 향후 `review`(검수), `summary`(요약) 등 추가 가능.

### 기존 테이블 변경: `directive_drafts`

| 추가 컬럼 | 타입 | 설명 |
|-----------|------|------|
| `model_used` | `varchar(100)` | 실제 사용된 모델 ID |
| `input_tokens` | `integer` | 입력 토큰 수 |
| `output_tokens` | `integer` | 출력 토큰 수 |
| `total_cost` | `numeric(10,6)` | 추정 비용 (USD) |

## 4. API

### GET `/api/v1/settings/directive-models`

현재 설정된 지시서 모델 우선순위 조회.

응답:
```json
{
  "configs": [
    {
      "role": "generation",
      "role_label": "지시서 생성",
      "models": ["claude-sonnet-5", "codex:gpt-5.6-tela"],
      "timeout_seconds": 60,
      "max_tokens": 2000,
      "updated_at": "2026-09-12T10:00:00+09:00",
      "updated_by": "CEO"
    }
  ]
}
```

### PUT `/api/v1/settings/directive-models`

지시서 모델 우선순위 업데이트. UPSERT 패턴.

요청:
```json
{
  "configs": [
    {
      "role": "generation",
      "models": ["claude-sonnet-5", "codex:gpt-5.6-tela"],
      "timeout_seconds": 60,
      "max_tokens": 2000
    }
  ]
}
```

## 5. 서비스 변경

### `directive_draft_service.py`

| 현재 | 변경 후 |
|------|---------|
| `model="claude-haiku-4-5-20251001"` 하드코딩 | DB `directive_model_config` 조회 → 1순위 모델 사용 |
| `max_tokens=1800` 하드코딩 | DB `max_tokens` 값 사용 |
| `timeout=45` 하드코딩 | DB `timeout_seconds` 값 사용 |
| 토큰/비용 미추적 | `model_used`, `input_tokens`, `output_tokens`, `total_cost` 저장 |

폴백 체인: DB 1순위 모델 실패 → DB 2순위 모델 → 결정론적 폴백 템플릿.

### 캐싱

DB 조회 비용을 줄이기 위해 in-memory 캐시 60초 TTL 적용. 설정 변경 시 캐시 무효화.

## 6. 대시보드 UI

Ops 세팅 페이지(`/settings`)에 **"지시서 모델 설정"** 섹션 추가.

| UI 요소 | 설명 |
|---------|------|
| 역할 라벨 | "지시서 생성" (generation) |
| 모델 목록 | 드래그 또는 ↑↓로 우선순위 변경 |
| 모델 추가 | 기존 `LEGACY_AVAILABLE_MODELS` 풀에서 선택 |
| 타임아웃 | 숫자 입력 (30~120초) |
| 최대 토큰 | 숫자 입력 (500~4000) |
| 저장 | PUT API 호출 → 즉시 반영 |
| 마지막 수정 | updated_at, updated_by 표시 |

기존 러너 모델 설정 UI 패턴(`ModelSettingsPanel.tsx`)을 재사용.

## 7. 초기 데이터

```sql
INSERT INTO directive_model_config (role, models, timeout_seconds, max_tokens, updated_by)
VALUES ('generation', '["claude-sonnet-5", "codex:gpt-5.6-tela"]'::jsonb, 60, 2000, 'CEO');
```

## 8. 검증 기준

- DB 테이블 생성 및 초기 데이터 확인
- GET/PUT API 정상 응답 (200, UPSERT)
- `directive_draft_service.py` DB 모델 우선순위대로 호출 확인
- Ops 세팅 페이지에서 모델 변경 → 즉시 반영 → 다음 지시서 생성 시 변경된 모델 사용
- 기존 러너 모델 설정 UI 회귀 없음
- 토큰/비용 추적 필드 저장 확인

## 9. 출시 계획

1. Phase 1 (이번): DB + API + 서비스 + UI 구현, Sonnet 5 + Codex GPT 5.6 Tela 초기 설정
2. Phase 2: 모델별 성공률/비용 통계 대시보드, 자동 폴백 성능 리포트
3. Phase 3: 역할별 모델 확장 (review, summary 등)
