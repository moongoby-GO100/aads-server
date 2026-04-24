# AADS 세션 거버넌스 아키텍처 v2.0 — 최종 확정본

- **문서 ID**: `20260423_session_governance_architecture_v2_final`
- **작성일**: 2026-04-23 (목) KST
- **작성자**: AADS CTO AI (Opus 4.7)
- **기반 문서**: `reports/20260423_aads_prompt_session_governance_plan.md` (v1)
- **CEO 결정 통합**: D-1 ~ D-4 + Q0 ~ Q4
- **상태**: 구현 착수 전 최종 검토 완료

---

## Executive Summary

AADS 시스템 프롬프트/세션 거버넌스를 **"단일 거대 프롬프트 + 하드코딩 매핑"** 에서 **"조합형 자산(prompt/role/intent/tool/memory) + DB 엔터티 + CR 승인 흐름"** 으로 재설계한다.

v1 보고서 검토 결과 **12개 갭**이 발견됐고, CEO가 **Q0~Q4 5건 방향**을 결정했다. 본 문서는 이를 모두 반영한 **구현 가능한 확정 아키텍처**이다.

---

## 1. CEO 결정사항 (구속력 있음)

### 1.1 v1 보고서 단계 (D-1 ~ D-4)

| ID | 결정 | 설계 반영 |
|----|------|----------|
| **D-1** | 역할 축은 6개로 한정하지 않고 **무한 확장/세분화/전문화**한다 | `role_profiles` 테이블, `hierarchy_level` 0~N, AI가 특화 역할 자동 제안 |
| **D-2** | 프롬프트 변경 승인은 **각 상위 역할이 하위를 승인**하는 계층 위임 구조 | `ApprovalChain` 서비스, CR 승인 권한 매트릭스 |
| **D-3** | 새 프로젝트 생성 시 **기본 세션 세트 자동 생성 + 지속 최적화** | `ProjectBootstrap` 서비스, 생성 직후 PM/Dev/QA + 특화 역할 자동 시드 |
| **D-4** | "AI 제안 자동 생성"은 **기본 ON**, 수정/개선/추가 가능 | `ai_suggestion_policies.default_enabled=true`, CR 형태로 대시보드 노출 |

### 1.2 v2 검토 단계 (Q0 ~ Q4, 이번 확정)

| ID | 질문 | CEO 결정 | 구현 방향 |
|----|------|----------|----------|
| **Q0** | 현재 AADS 메모리/진화 시스템을 고려했는가? | 고려해야 함 | `memory_recall.py` 10섹션(session_notes/preferences/tool_strategy/directives/discoveries/learned/correction/experience/visual/strategy_updates) + Reflexion + Sleep-Time + error_pattern을 거버넌스 대상에 포함 |
| **Q1** | LLM 모델별 프롬프트는? | 모델별 분기 필요 | `prompt_assets`에 `model_variants` 컬럼 — Opus/Sonnet/Haiku/Gemini/DeepSeek 각각 다른 content 보관, `PromptCompiler`가 선택된 모델에 맞춰 조립 |
| **Q2** | `INTENT_MAP` DB화 범위는? | **전체 DB화** | `intent_policies` 테이블 — 56+ 인텐트 × 5 필드(model/tools/thinking/gemini_direct/naver_type) 전체 DB 이관, 코드는 폴백만 유지 |
| **Q3** | `PromptCompiler` 위치 권장안은? | **B안 채택** | 독립 서비스 `app/services/prompt_compiler.py` — `chat_service`가 조립된 프롬프트만 받도록 책임 분리 |
| **Q4** | 초기 특화 역할 범위는? | **AI가 자동 제안** | `RoleEvolution`이 프로젝트 생성 직후 24시간 관찰 후 특화 역할 3~5건 자동 제안 → CEO/PM 승인 시 활성화 |

---

## 2. v1 대비 보완된 12 Gap 통합 해결안

### 2.1 현재 코드와 불일치 (G-1 ~ G-5)

| Gap | v1 문서 표현 | 실제 코드 | v2 최종 해결안 |
|-----|-------------|----------|---------------|
| **G-1** 인텐트별 도구 매핑 | `tool_policies` 테이블 | `_INTENT_TOOL_MAP` + `_CORE_TOOLS` + `get_tools_for_intent()` | `intent_policies.allowed_tools`로 통합, 폴백은 `_INTENT_TOOL_MAP` 유지 |
| **G-2** 도구 로딩 전략 | 미언급 | `defer_loading=true/false` (상시 30 + 온디맨드 45) | `tool_policies.defer_loading`, `tool_policies.load_strategy=eager\|deferred\|conditional` |
| **G-3** 인텐트 모델 라우팅 | `role_profiles.model_override_rules` | `INTENT_MAP` 5필드 동시 결정 | `intent_policies`로 전부 이관 (Q2 결정) — model/tools/thinking/gemini_direct/naver_type 5컬럼 |
| **G-4** Adaptive Prompt skip | `session_blueprints.skip_sections` | `_INTENT_SECTIONS` (5그룹) | skip은 **인텐트 단위** 유지, `session_blueprints.extra_skip_sections`로 오버라이드만 |
| **G-5** 메모리 섹션 구조 | 간략 | **10개 섹션** (memory_recall.py) | `memory_policies.section_budgets` — 10섹션 키 명시 + 섹션별 confidence/budget/ttl |

### 2.2 누락된 설계 (G-6 ~ G-12)

| Gap | 항목 | 최종 반영 |
|-----|------|----------|
| **G-6** `_CLASSIFY_PROMPT` 거버넌스 | 분류 프롬프트 자체가 자산 | `prompt_assets`에 `slug='system.intent_classifier'` 등록, CR 필수 |
| **G-7** 경량 프롬프트 모드 | `_LITE_PROMPT_INTENTS` 토큰 60%↓ | `session_blueprints.lite_mode=true\|false` + lite 전용 블루프린트 |
| **G-8** Layer4 연결 | 동적 컨텍스트 블록 미기술 | `prompt_assets.layer_id=0~6` 지정 (system/role/project/session/memory/runtime/corrections) |
| **G-9** 모델별 프롬프트 분기 | 없음 | `prompt_assets.model_variants JSONB` — Q1 결정 반영 |
| **G-10** thinking/토큰 한도 | 부분 언급 | `intent_policies.thinking_budget`, `max_tokens_override` |
| **G-11** gemini_direct / LiteLLM 라우팅 | 없음 | `intent_policies.llm_route=anthropic\|gemini_direct\|litellm_gemini\|litellm_deepseek` |
| **G-12** 진화/메모리 파이프라인 연동 | 없음 (Q0) | `evolution_hooks` 테이블 — Reflexion/Sleep-Time/error_pattern/quality_score가 CR을 자동 제안하는 경로 연결 |

---

## 3. 확정 아키텍처

### 3.1 논리 계층 (Layer 0 ~ 6)

```
L0 System      : AADS 공통 프롬프트 + R-AUTH/R-COMMIT/R-DOCKER 등 불변 규칙
L1 Role        : 역할별 (CTO/PM/Dev/QA/특화) 프롬프트 — hierarchy_level 0~N
L2 Project     : 프로젝트별 (AADS/KIS/GO100/SF/NTV2/NAS) 컨텍스트
L3 Session     : 세션 블루프린트 — 도구/모델/메모리 프로파일
L4 Memory      : 10섹션 동적 주입 (memory_recall)
L5 Runtime     : KST 시각, 워크스페이스, 현재 상태, 반성지시
L6 Corrections : Reflexion 반성문, strategy_updates, error_patterns
```

**PromptCompiler(B안, Q3)** 는 `chat_service`와 독립된 `app/services/prompt_compiler.py` 서비스로, L0~L6을 조합해 최종 system prompt를 반환한다.

### 3.2 DB 스키마 (8 → 11개로 확장)

```sql
-- Core (v1 유지)
prompt_assets          -- (확장) model_variants JSONB, layer_id INT
prompt_asset_versions
role_profiles          -- hierarchy_level, specialization_tags
project_profiles
session_blueprints     -- lite_mode, extra_skip_sections
memory_policies        -- section_budgets JSONB (10섹션)
change_requests
cr_approvals

-- v2 신규 (3개)
intent_policies        -- ★ Q2: INTENT_MAP 전체 DB화
tool_policies          -- ★ G-2: defer_loading, load_strategy
evolution_hooks        -- ★ Q0: Reflexion/Sleep-Time → CR 자동 제안 경로
```

#### 3.2.1 `intent_policies` 스키마 (Q2 반영)

```sql
CREATE TABLE intent_policies (
  intent_slug       TEXT PRIMARY KEY,          -- 'casual', 'cto_strategy', ...
  model             TEXT,                      -- 'claude-opus' | 'mixture' | ...
  thinking_budget   INT DEFAULT 0,
  max_tokens_override INT,
  llm_route         TEXT DEFAULT 'anthropic',  -- anthropic|gemini_direct|litellm_*
  allowed_tools     TEXT[] DEFAULT '{}',       -- 화이트리스트
  core_tools_override TEXT[],                  -- 덮어쓸 core
  defer_loading_override BOOLEAN,              -- 인텐트별 defer 정책
  naver_search_type TEXT,                      -- webkr|news|blog|...
  skip_sections     TEXT[] DEFAULT '{}',       -- Adaptive Prompt
  lite_mode         BOOLEAN DEFAULT FALSE,
  notes             TEXT,
  updated_at        TIMESTAMPTZ DEFAULT NOW()
);
```

**마이그레이션 전략**: 현재 `INTENT_MAP` 56+ 엔트리를 `init_intent_policies.sql`로 시드. 코드에는 DB 실패 시 폴백으로만 `INTENT_MAP` 유지.

#### 3.2.2 `prompt_assets.model_variants` (Q1 반영)

```sql
ALTER TABLE prompt_assets ADD COLUMN model_variants JSONB DEFAULT '{}'::jsonb;
-- 예시 데이터
-- model_variants = {
--   "claude-opus":   { "content": "...opus 최적화...", "token_est": 1200 },
--   "claude-sonnet": { "content": "...sonnet용...",   "token_est": 900  },
--   "claude-haiku":  { "content": "...haiku 경량...",  "token_est": 400  },
--   "gemini-flash":  { "content": "...gemini용...",    "token_est": 500  }
-- }
-- 선택된 모델에 해당 variant이 없으면 기본 content 사용
```

#### 3.2.3 `evolution_hooks` (Q0 반영)

```sql
CREATE TABLE evolution_hooks (
  id              SERIAL PRIMARY KEY,
  source          TEXT,     -- 'reflexion' | 'sleep_time' | 'error_pattern' | 'quality_score'
  trigger_rule    JSONB,    -- 예: {"quality_lt": 0.4, "window_hours": 24}
  target_layer    INT,      -- L0~L6 중 어디에 CR 제안할지
  cr_template     TEXT,     -- CR 생성용 템플릿 prompt
  auto_open       BOOLEAN DEFAULT TRUE, -- D-4: 기본 ON
  last_fired_at   TIMESTAMPTZ,
  enabled         BOOLEAN DEFAULT TRUE
);
```

### 3.3 PromptCompiler (Q3 — B안 확정)

```python
# app/services/prompt_compiler.py  (신규)
class PromptCompiler:
    async def compile(
        self,
        *,
        project: str,
        role: str,
        intent: str,
        model: str,
        session_id: str,
        corrections: list[str],
        memory_sections: dict[str, str],
    ) -> CompiledPrompt:
        # 1) DB에서 layer별 prompt_assets 로드 (model_variants 적용, Q1)
        # 2) intent_policies 조회 (Q2)
        # 3) session_blueprint의 skip_sections/extra_skip 적용 (G-4, G-7)
        # 4) memory_policies.section_budgets로 메모리 자르기 (G-5)
        # 5) corrections/reflexion 주입 (G-12)
        # 6) 최종 system prompt + tool 리스트 + model + thinking 반환
        ...
```

`chat_service.send_message_stream()`는 `PromptCompiler.compile()` 결과만 소비한다. 기존 인라인 조립 코드는 점진 제거.

### 3.4 역할 진화 (Q4 — AI 자동 제안)

```python
# app/services/role_evolution.py (신규)
class RoleEvolution:
    async def observe_and_suggest(self, project: str, window_hours: int = 24):
        # 1) 최근 24h chat_history / 도구 사용 / 오류 패턴 집계
        # 2) 빈도 높은 전문 영역 클러스터링 (예: 백테스트, 영상편집, DB튜닝)
        # 3) 3~5건의 특화 역할 spec 생성
        # 4) change_requests에 auto_open=TRUE로 등록 (D-4)
        # 5) 대시보드에서 CEO/상위 역할(D-2)이 승인 시 role_profiles에 insert
        ...
```

### 3.5 프로젝트 부트스트랩 (D-3)

```python
# app/services/project_bootstrap.py (신규)
class ProjectBootstrap:
    async def create_project(self, slug: str, name: str, server: str):
        # 1) project_profiles insert
        # 2) PM/Dev/QA 기본 role_profiles 자동 시드 (hierarchy 1~3)
        # 3) 기본 session_blueprint 3종 시드 (standard/lite/debug)
        # 4) intent_policies 공통 세트 복사
        # 5) RoleEvolution.schedule(after_hours=24) — Q4
        # 6) evolution_hooks 기본 ON (D-4)
```

---

## 4. CR (Change Request) 흐름 (D-2 계층 승인)

```
제안 → preview/diff → eval → 승인 → 반영
  ↑                              ↑
  AI 자동 제안 (D-4 ON)         계층 승인 (D-2)

승인 권한 매트릭스:
- L0 System 변경      → CEO만
- L1 Role (CTO/PM)    → CEO
- L1 Role (Dev/QA)    → PM 이상
- L1 Role (특화)      → 해당 상위 역할
- L2 Project          → PM 이상
- L3~L5               → 해당 프로젝트 PM
- L6 Corrections      → 즉시 반영(자동), 사후 검토
```

---

## 5. 무중단 마이그레이션 계획 (3주)

| 주차 | 산출물 | 검증 |
|------|-------|------|
| **W1** | DB 스키마 11개 생성 + `init_intent_policies.sql`(56+ 시드) + `PromptCompiler` 스켈레톤 + 관리 API(/api/v1/governance/*) | DB 조회 실패 시 기존 코드 폴백 동작 확인, eval 세트 회귀 0건 |
| **W2** | CR 승인 흐름 + AI 제안 엔진(evolution_hooks) + 대시보드 거버넌스 UI + `prompt_assets.model_variants` 활성화 (Q1) | diff 뷰어에서 변경 미리보기, 승인 후 반영까지 E2E |
| **W3** | `RoleEvolution` 자동 제안 (Q4) + `ProjectBootstrap` + `_CLASSIFY_PROMPT` DB화(G-6) + Legacy `INTENT_MAP`/`WS_ROLES` 폴백만 남기고 주 경로 DB로 전환 | 새 프로젝트 생성 시 24h 후 AI가 특화 역할 제안하는지 확인 |

**롤백 전략**: 각 주차 끝에 `FEATURE_FLAGS.use_db_governance=false`로 즉시 복귀 가능.

---

## 6. 구현 전 체크리스트

- [ ] DB 마이그레이션 SQL 리뷰 (11개 테이블)
- [ ] `intent_policies` 시드 데이터 정합성 (현 `INTENT_MAP`과 1:1)
- [ ] `PromptCompiler` 단위 테스트 (모델별 variant 선택 로직)
- [ ] `memory_recall.py` 10섹션 → `memory_policies.section_budgets` 매핑 검증
- [ ] `evolution_hooks` 기본 시드: Reflexion(quality<0.4), error_pattern(3회 이상 반복)
- [ ] 대시보드 `/governance` 라우트 설계 (CR 목록/diff/승인)
- [ ] Eval 세트 구성: 인텐트 분류 정확도 / 도구 호출 일치율 / 토큰 회귀
- [ ] FEATURE_FLAGS 토글 작동 확인

---

## 7. 리스크 & 완화

| 리스크 | 영향 | 완화 |
|--------|------|------|
| `INTENT_MAP` DB 이관 중 라우팅 오류 | 채팅 전체 품질 저하 | DB 실패 시 폴백, W1 말 eval 100건 회귀 테스트 |
| `model_variants` 미설정 모델 | 프롬프트 공백 | 기본 `content` 필드를 fallback으로 강제 |
| `RoleEvolution` 노이즈 역할 제안 | CR 폭주 | 자동 제안은 `auto_open=true`지만 `confidence>=0.7`만 목록 상단 노출, 나머지 archive |
| 계층 승인 순환 참조 | 데드락 | `hierarchy_level` 엄격 DAG, 자기 자신 승인 불가 제약 |

---

## 8. 결론 & 다음 액션

- v1 → v2는 **12 Gap 해소 + CEO 결정 9건(D-1~4, Q0~4) 반영**으로 구현 착수 준비 완료.
- 핵심은 **"조합형 자산(L0~L6) + DB 엔터티 + CR 계층 승인 + AI 자동 제안"** 4요소.
- 진화 시스템(Reflexion/Sleep-Time/error_pattern)이 **CR을 자동 생성**하여 거버넌스 루프가 자체 개선하는 구조.

### 즉시 착수 가능 항목

1. **W1 DB 마이그레이션 SQL 작성 지시서** → Pipeline Runner 투입
2. **`init_intent_policies.sql`** 생성 — 현 `INTENT_MAP` 56+ 엔트리 시드
3. **`PromptCompiler` 스켈레톤** (B안, Q3) — 독립 서비스 모듈

→ CEO 승인 시 W1 러너 지시서 3건을 순차 투입하겠습니다.

---

**문서 버전**: v2.0 (최종 확정)
**다음 개정**: W1 완료 후 v2.1에서 실측 회귀 결과 반영
