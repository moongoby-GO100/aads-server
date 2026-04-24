# AADS Admin Visualization — Roadmap v1.1 (Governance-Integrated)

- **문서 유형**: Lay out (L) 산출물 — v1.0 보강판
- **작성일**: 2026-04-24 KST (Friday)
- **작성자**: AADS CTO AI
- **통합 대상**:
  - `20260424_ADMIN_VISUALIZATION_ROADMAP_v1.md` (v1.0)
  - `20260423_session_governance_architecture_v2_1_addendum.md` (v2.1 addendum)
  - 현재 대시보드 디자인 시스템 실측 (2026-04-24 11:xx KST)
- **핵심 변경**: **거버넌스 레이어(CR/Prompt/Intent/Kill-switch) 3페이지 신규 + 6페이지 대폭 확장 + 디자인 시스템 장 신설**

---

## 0. v1.0 대비 무엇이 바뀌었나 (Δ 요약)

| 구분 | v1.0 | v1.1 |
|------|------|------|
| 페이지 수 | 45 | **48** (+3) |
| 신규 페이지 | — | **P46 Intent Policies / P47 Governance Audit / P48 Emergency Control** |
| 대폭 확장 | — | **P07 CR 승인 큐 / P25 도구 거버넌스 / P27 패리티 센터 / P32 비용 센터 / P39 프롬프트 거버넌스 / P43 에이전트 관리** |
| 디자인 시스템 장 | 없음 | **제11장 신설** (Governance Component Library 7종) |
| Phase 1 | AADS-200~203 (4건) | **AADS-199 백엔드 temperature 배선 선행 + AADS-200~203** |
| Phase 2 | 일반 Safety | **addendum 직접 대응: CR/Prompt/Kill-switch/Intent Policies** |
| 반영률 분모 | 45 | 48 (이하 표 모두 재계산) |

### 0.1 왜 수정이 필요했나

addendum v2.1(2026-04-23)이 요구하는 **11개 거버넌스 요건**(Q-COST1 / Q-CONSIST1~2 / Q8~Q18)이 v1.0 로드맵에는 UI 단에서 **대부분 미반영**. 특히 다음 5가지가 결정적.

1. **`intent_policies` DB 테이블 + `temperature` 파라미터 배선** — W1-C1/C2 선행해야 다른 UI가 의미 가짐
2. **Change Request(CR) 승인 흐름** — 단순 "문서 리뷰 큐"로는 eval_score + rollout_pct를 담지 못함
3. **Prompt Asset 버전·롤백 시스템** — 현 `/admin/prompts`는 편집만 가능, 버전 없음
4. **Kill-switch (`feature_flags.governance_enabled`)** — 비상 제어 페이지 전무
5. **골든셋 50건 패리티 리포트** — 현 `/admin/model-parity`는 호출 통계만

---

## 1. 현재 실측 (2026-04-24 11:xx KST) — v1.0 보강

### 1.1 `/admin` 계열 실측

| 경로 | 줄 수 | 수정일 | 탭 구성 | addendum 요건 반영률 |
|------|-------|--------|---------|---------------------|
| `/admin/model-parity` | 455 | 2026-04-24 10:08 | models / routing / daily | 🟡 **호출 통계만** — 골든셋·품질 분산·주간 리포트 미반영 |
| `/admin/prompts` | 466 | 2026-04-15 07:42 | dashboard / editor / preview / tokens | 🟡 **편집만** — 버전·variant·eval_score·rollout_pct·롤백 미반영 |

### 1.2 디자인 시스템 실측

| 항목 | 현황 | 평가 |
|------|------|------|
| CSS 토큰 | `var(--bg-card)`, `var(--accent)`, `var(--text-primary)`, `var(--border)` — 다크 테마 | ✅ 유지 |
| 컴포넌트 라이브러리 | shadcn/ui 미도입. Tailwind + 인라인 style 혼용 | 🟡 공통화 필요 |
| 공통 컴포넌트 | Header, Sidebar, ClientLayout, CostTracker, AgentStatus, PipelineHealthCard, SSEMonitor, UpdateBanner | ✅ |
| 거버넌스 전용 컴포넌트 | 0개 | ❌ 신규 필요 |
| Sidebar 계층 | 평탄 (23 카테고리) | 🟡 3-depth 재편 필요 |
| 레이아웃 bak 파일 | `globals.css.bak_bottomnav`, `layout.tsx.bak_aads`, `page.tsx.bak.T049/T060` 등 | ⚠️ 실험 이력 — 정리 필요 |

### 1.3 addendum v2.1 DB 스키마 요구 vs 현재 AADS DB

| addendum 요구 테이블 | 현재 AADS DB | 상태 |
|--------------------|---------------|------|
| `intent_policies` | 없음 | ❌ 신규 |
| `prompt_asset_versions` | `prompt_versions`(0 rows) 스키마만 | 🟡 확장 필요 |
| `change_requests` | 없음 (유사: `approval_queue`(59), `design_reviews`) | 🟡 재활용 + 확장 |
| `role_profiles` (project_scope/allowed_tools/scope) | 없음 | ❌ 신규 |
| `governance_audit_log` | 없음 (유사: `error_log`, `oauth_usage_log`) | ❌ 신규 |
| `feature_flags` (governance_enabled) | 없음 | ❌ 신규 (단일 row 테이블) |
| `tool_grants` | 없음 | ❌ 신규 |

→ **W1 병렬로 5개 테이블 신규 + 1개 확장 필요**. DROP/RENAME 금지 원칙 준수.

---

## 2. 48개 페이지 매핑 매트릭스 (v1.0 → v1.1)

상태: ✅ 완전 / 🟡 부분 / 🟦 DB만 / ❌ 미반영 / **🔶 addendum 추가 요건 미반영**

### 2.1 변경된 페이지만 요약

| # | 페이지 | v1.0 상태 | v1.1 상태 | 변경 사유 |
|---|--------|-----------|-----------|----------|
| P07 | **CR 승인 큐** (구 문서 리뷰 큐) | ❌ | 🔶 | addendum Q10/Q15: eval_score + rollout_pct + confidence filter |
| P25 | **도구 거버넌스** (구 도구 모니터링) | 🟦 | 🟦+🔶 | Q16: allowed_tools/denied_tools/tool_grants + requires_approval |
| P27 | **모델 패리티 센터** (구 모델 벤치마크) | ✅ | 🟡+🔶 | Q-CONSIST2: 골든셋 50건 + 주간 리포트 + 7일 추이 |
| P31 | 정책 관리 | ❌ | 🔶 | Q-COST1 / R-QUALITY-COST — CLAUDE.md 규칙 DB화 + 인텐트 정책 포함 |
| P32 | 비용 센터 | 🟦 | 🟦+🔶 | R-QUALITY-COST: 차단 UI 제거 → 이상치 감지 배지로 의미 전환 |
| P33 | 인시던트 센터 | 🟦 | 🟦+🔶 | Q18: DB→Redis→하드코딩 fallback 이력 추가 |
| P34 | 감사 로그 | 🟦 | 🟦+🔶 | Q12: `governance_audit_log` Shadow Mode diff 스트림 추가 |
| P39 | **프롬프트 거버넌스** (구 프롬프트 레지스트리) | 🟡 | 🟡+🔶 | Q13: version/롤백 + Q8: cache breakpoint + Q9: variants + model parity |
| P40 | 피드백 & 교정 허브 | 🟦 | 🟦+🔶 | Q15: confidence ≥ 0.7 필터 + 중복 억제 + 14일 자동 만료 |
| P41 | 알림 센터 | 🟦 | 🟦+🔶 | Q17: kill-switch 알림 이력 + 텔레그램 연동 |
| P43 | 에이전트 관리 | ❌ | ❌+🔶 | Q11: scope(chat/subagent/team) + project_scope[] |

### 2.2 신규 3페이지

| # | 페이지 | 우선 | 근거 addendum 항목 | 재사용 자원 |
|---|--------|------|-------------------|-------------|
| **P46** | **Intent Policies** 관리 | HIGH | Q-CONSIST1: temperature 맵 + tool 권한 | 신규 `intent_policies` 테이블 |
| **P47** | **Governance Audit** 로그 | HIGH | Q12: dual-read diff, 롤백 이력 | 신규 `governance_audit_log` 테이블 |
| **P48** | **Emergency Control** (kill-switch) | **CRIT** | Q17: 5초 내 레거시 폴백 | 신규 `feature_flags` 테이블 + Redis pub/sub |

### 2.3 v1.0 기존 34개 페이지

**v1.0 매트릭스 그대로 유지** (P01~P06, P08~P24, P26, P28~P30, P35~P38, P42, P44, P45). 본 v1.1에서 Δ만 명시.

### 2.4 반영률 재계산 (분모 48)

| 상태 | v1.0 (45) | v1.1 (48) |
|------|-----------|-----------|
| ✅ 완전 | 2 (4.4%) | 2 (4.2%) |
| 🟡 부분 | 9 (20.0%) | 9 (18.8%) |
| 🟦 DB만 | 14 (31.1%) | 14 (29.2%) |
| 🔶 addendum 미반영 | 0 | **11 (22.9%)** |
| ❌ 미반영 | 20 (44.4%) | 23 (47.9%) |

**데이터 레이어 준비율**: v1.0 55.5% → v1.1 52.1% (신규 3개로 분모 증가). 단 Phase 1 선행 5개 테이블 생성 후 **62.5%** 로 회복.

---

## 3. Phase 재구성 (14주 → 14주, 내용 재편)

### Phase 1 (1~2.5주) — **Governance Backbone + Control Loop**

| Task | 제목 | Size | 상세 | 소요 |
|------|------|------|------|------|
| **AADS-199** ⭐ 신규 | 백엔드 거버넌스 배선 (addendum W1-C1/C2) | L | `anthropic_client.temperature` 노출 + `intent_router.INTENT_TEMPERATURE_MAP` + `intent_policies` 시드 + `feature_flags.governance_enabled` Shadow Mode | 1.5일 |
| AADS-200 | P35 태스크 보드 칸반화 | M | (v1.0 유지) | 2일 |
| AADS-201 | P43 에이전트 관리 (**scope 포함**) | M | v1.0 + `scope='chat/subagent/team'` + `project_scope[]` | 2.5일 |
| AADS-202 | P36 세션 리플레이 간이판 | M | (v1.0 유지) | 1.5일 |
| AADS-203 | P37 배포 현황 | S | (v1.0 유지) | 1일 |

**변경 사유**: AADS-199가 선행되지 않으면 P46/P27 고도화/P39 확장 전부 "빈 껍데기 UI". 2~3일 투자로 이후 Phase 전체 품질 확보.

### Phase 2 (3~5주) — **Governance & Safety (addendum 직접)**

| Task | 페이지 | 근거 | Size |
|------|--------|------|------|
| AADS-204 | **P07 CR 승인 큐** (신규 UX) | Q10 eval_score + Q15 confidence filter | M |
| AADS-205 | **P39 프롬프트 거버넌스** (대폭 확장) | Q8 cache bp + Q9 variants + Q13 1-click 롤백 | L |
| AADS-206 | **P46 Intent Policies** (신규) | Q-CONSIST1 temperature 맵 UI | M |
| AADS-207 | **P48 Emergency Control** (신규, CRIT) | Q17 kill-switch 버튼 | S |
| AADS-208 | P31 정책 관리 (CLAUDE.md DB화) | R-QUALITY-COST + R-AUTH + R-DOCKER | M |
| AADS-209 | P29 보안 센터 + P33 인시던트 (fallback 이력) | Q18 | M |
| AADS-210 | P22 시크릿 관리 + P23 로그 | HIGH(v1.0) | M |
| AADS-211 | P41 알림 센터 (kill-switch 연동) | Q17 후속 | S |

**Phase 2 총계**: 8건, 병렬화 시 2~2.5주.

### Phase 3 (6~9주) — Architecture Visualization (v1.0 유지)

P09 Module View / P11 ERD 뷰어 / P12 ERD Diff / P19 API 카탈로그 / P17 아키텍처 히스토리 / P18 ADR 관리

### Phase 4 (10~12주) — Cost & Quality (**v1.0 + addendum 통합**)

| Task | 페이지 | v1.0 대비 변경 |
|------|--------|----------------|
| AADS-220 | **P27 모델 패리티 센터** | 골든셋 50건 + 주간 리포트 + 7일 추이 + 품질 분산 그래프 |
| AADS-221 | **P32 비용 센터** | 차단 배지 제거 → 이상치 감지 배지 |
| AADS-222 | **P40 피드백 허브** | confidence 필터 + 14일 자동 만료 |
| AADS-223 | **P34 감사 + P47 Governance Audit** | `governance_audit_log` Shadow diff 스트림 |
| AADS-224 | **P25 도구 거버넌스** | `allowed/denied/tool_grants` + requires_approval |
| AADS-225 | P28 모델 업데이트 | (v1.0 유지) |

### Phase 5 (13~14주) — Ecosystem (v1.0 유지)

P24 MCP / P44 의존성 / P14·P15 모듈 / P08·P10 코드맵 / P26 AGENTS.md / P07 리뷰 잔여 / P38 환경 / P45 분석 고도화 / P20 API 테스트 / P02 사용자·팀

---

## 4. 신규 3페이지 상세 설계

### 4.1 P46 — Intent Policies 관리 (`/admin/intent-policies`)

**목적**: 인텐트별 temperature / 라우팅 / 도구 권한을 DB에서 직접 관리. CR 승인 경유.

| UI 블록 | 내용 |
|---------|------|
| 상단 카드 | 현재 활성 인텐트 수 / Shadow vs DB-primary 상태 / 다음 CR 승인 대기 |
| 중앙 테이블 | `intent` × [`temperature`, `model`, `tools_enabled`, `thinking`, `rollout_pct`, `active`] |
| 편집 드로어 | 변경 시 즉시 반영하지 않고 **CR 제출** → P07로 이동 |
| 비교 뷰 | `temperature=default` vs 제안값 diff 렌더 (L0~L6 리프트 미리보기) |

**API**: `GET/POST /api/v1/governance/intent-policies`, `POST /api/v1/governance/cr/submit`

**DB 스키마** (신규):
```sql
CREATE TABLE IF NOT EXISTS intent_policies (
  intent VARCHAR(50) PRIMARY KEY,
  model_primary VARCHAR(100),
  model_fallback VARCHAR(100)[],
  temperature NUMERIC(3,2) DEFAULT 0.2,
  tools_enabled BOOLEAN DEFAULT true,
  thinking_enabled BOOLEAN DEFAULT false,
  rollout_pct INT DEFAULT 100 CHECK (rollout_pct BETWEEN 0 AND 100),
  active BOOLEAN DEFAULT true,
  version INT DEFAULT 1,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

### 4.2 P47 — Governance Audit Log (`/admin/governance-audit`)

**목적**: Shadow/DB-primary/Legacy-readonly 3단계 전환 중 **코드 경로 vs DB 경로 diff**를 실시간 관찰. CR/롤백 이력 영구 보관.

| UI 블록 | 내용 |
|---------|------|
| 상단 필터 | 시간범위 / intent / mode(shadow/primary) / diff-only 토글 |
| 중앙 스트림 | 이벤트별 "코드=X, DB=Y, diff=±Δ" 한 줄 표시. 확장 시 전체 payload JSON |
| 우측 패널 | 선택 이벤트의 PromptCompiler L0~L6 체인 렌더 |
| 하단 | 일별 diff 빈도 차트 (Recharts) |

**DB 스키마** (신규):
```sql
CREATE TABLE IF NOT EXISTS governance_audit_log (
  id BIGSERIAL PRIMARY KEY,
  at TIMESTAMPTZ DEFAULT NOW(),
  event VARCHAR(50) NOT NULL,  -- intent_resolve / prompt_assemble / tool_grant
  mode VARCHAR(20) NOT NULL,   -- shadow / primary / fallback
  legacy_result JSONB,
  db_result JSONB,
  diff_summary TEXT,
  trace_id VARCHAR(64),
  INDEX (at DESC), INDEX (event), INDEX (mode)
);
```

### 4.3 P48 — Emergency Control (`/admin/emergency`) ⚠️ CRIT

**목적**: 5초 내 레거시 경로 폴백. Redis pub/sub 전파.

| UI 블록 | 내용 |
|---------|------|
| 최상단 | 거대한 **KILL-SWITCH 버튼** (빨강, 2차 확인 다이얼로그) |
| 현재 상태 | `governance_enabled` (true/false) + 마지막 전환 시각 + 전환자 |
| 서브 스위치 | intent_policies / prompt_variants / tool_grants 개별 off |
| 우측 | 최근 비상 이벤트 히스토리 10건 |
| 하단 | "현재 폴백 중 영향 받는 세션 N건" (실시간) |

**DB 스키마** (신규):
```sql
CREATE TABLE IF NOT EXISTS feature_flags (
  flag_key VARCHAR(100) PRIMARY KEY,
  enabled BOOLEAN DEFAULT true,
  scope VARCHAR(20) DEFAULT 'global',
  last_changed_by VARCHAR(100),
  last_changed_at TIMESTAMPTZ DEFAULT NOW(),
  notes TEXT
);
-- 시드
INSERT INTO feature_flags (flag_key, enabled) VALUES
  ('governance_enabled', true),
  ('intent_policies_db_primary', false),  -- Shadow 시작
  ('prompt_variants_enabled', false),
  ('tool_grants_enforced', false);
```

**백엔드 연동**: `app/core/feature_flags.py` + Redis publish `aads:flags:changed`, 전 서버 60초 캐시 즉시 무효화.

---

## 5. 기존 6페이지 확장 상세

### 5.1 P07 — Change Request(CR) 승인 큐 (구 문서 리뷰 큐)

- 기존 `approval_queue`(59) 재활용 + `change_requests` 신규 테이블 분리(프롬프트/정책/intent 대상 전용)
- 카드 구조: 제목 / 변경 diff / **eval_score**(골든셋 100건 통과율) / **rollout_pct** / 제출자 / confidence
- 승인 조건: `eval_score ≥ 기준` 일 때만 [승인] 버튼 활성
- 승인 흐름: 0% → 10% → 50% → 100% 단계 버튼. 자동 롤백 조건 표시

### 5.2 P25 — 도구 거버넌스 (구 도구 모니터링)

- 두 영역 분리:
  - **관측**(v1.0): `tool_results_archive` 기반 사용률/에러율
  - **제어**(신규): `tool_grants` + `role_profiles.allowed_tools/denied_tools` 편집
- 민감 도구(`write_remote_file`, `run_remote_command`, `git_remote_push`, `terminate_task`): `requires_approval=true` 배지 + CR 경유

### 5.3 P27 — 모델 패리티 센터 (기존 `/admin/model-parity`)

- 기존 3탭 유지 + **신규 2탭**:
  - **골든셋 패리티** — 50건 질의에 Opus/Sonnet/Haiku/Gemini 결과 + 품질 점수 분산
  - **주간 리포트** — `reports/YYYYMMDD_model_parity.md` 히스토리 + diff
- 경보 배지: 분산 > 0.2 시 상단 빨간 배너

### 5.4 P32 — 비용 센터 (의미 전환)

- **제거**: `opus_blocked` 차단 배지, 일/월 상한 초과 알림
- **유지**: 일/월/인텐트별 비용 추이 (`cost_tracking`, `task_cost_log`, `bg_llm_usage_log`)
- **신규**: 이상치 감지 배지 (평시 대비 ×3 급증 시 노랑, ×10 빨강)
- CEO 명시 모델 선택 시 `model_locked=true` 배지 표시 (비용과 무관하게 유지)

### 5.5 P39 — 프롬프트 거버넌스 (기존 `/admin/prompts` 대폭 확장)

- **v1.0 4탭 유지** (dashboard / editor / preview / tokens)
- **신규 3탭**:
  - **versions** — `prompt_asset_versions` 타임라인 + 1-click 롤백
  - **variants** — Opus/Sonnet/Haiku variant별 content diff
  - **cache** — L0~L2 안정 prefix 히트율 실시간 (Redis 연동)
- 편집 시 즉시 저장 금지 → **CR 제출**로 전환 (P07과 연결)

### 5.6 P43 — 에이전트 관리 (Phase 1 이미 포함)

- Meta-Agent(6) + Worker-Agent(Coder/Reviewer/Debater) 레지스트리
- 신규: `role_profiles.scope in ('chat','subagent','team')` 컬럼 + `project_scope[]`
- 각 에이전트: 허용 도구 / 거부 도구 / 기본 temperature / 허용 프로젝트 필드

---

## 6. DB 마이그레이션 계획 (v1.0 + Phase 1 확장)

v1.0의 4개 ALTER/CREATE에 **6개 추가**:

```sql
-- 신규 (addendum Phase 1)
CREATE TABLE IF NOT EXISTS intent_policies (...);          -- P46
CREATE TABLE IF NOT EXISTS feature_flags (...);            -- P48
CREATE TABLE IF NOT EXISTS governance_audit_log (...);     -- P47
CREATE TABLE IF NOT EXISTS change_requests (
  id BIGSERIAL PRIMARY KEY,
  type VARCHAR(30),                  -- prompt / intent / tool_grant / policy
  target_id VARCHAR(100),
  diff JSONB,
  eval_score NUMERIC(3,2),
  rollout_pct INT DEFAULT 0,
  status VARCHAR(20) DEFAULT 'pending',
  confidence NUMERIC(3,2),
  submitted_by VARCHAR(100),
  approved_by VARCHAR(100),
  submitted_at TIMESTAMPTZ DEFAULT NOW(),
  approved_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ              -- 14일 자동 만료
);
CREATE TABLE IF NOT EXISTS tool_grants (
  role_id VARCHAR(50),
  tool_name VARCHAR(100),
  requires_approval BOOLEAN DEFAULT false,
  project_scope VARCHAR(50)[],
  PRIMARY KEY (role_id, tool_name)
);

-- 확장 (기존 활용)
ALTER TABLE prompt_versions
  ADD COLUMN IF NOT EXISTS variant VARCHAR(20) DEFAULT 'default',  -- opus/sonnet/haiku/default
  ADD COLUMN IF NOT EXISTS cache_breakpoint_hash VARCHAR(64),
  ADD COLUMN IF NOT EXISTS rollout_pct INT DEFAULT 100;
```

**주의**: 전부 `IF NOT EXISTS` + 컬럼 추가만. DROP/RENAME 금지.

---

## 7. 디자인 시스템 변경 기획 (신설 장)

### 7.1 결정 사항

| 항목 | 결정 | 사유 |
|------|------|------|
| CSS 토큰 | **기존 변수 그대로 유지** (`--bg-card`, `--accent`, `--text-primary`, `--border`) | 48페이지 일관성 + 기존 학습 비용 0 |
| 컴포넌트 라이브러리 | **신규 내부 라이브러리** `@aads/ui` (shadcn/ui 벤더링 대신 경량 내부 구현) | 외부 의존 최소 + AADS 맞춤 |
| 아이콘 | Lucide React (이미 사용 중) | 변경 없음 |
| 차트 | Recharts (Phase 1~4) → D3 (Phase 3+ 그래프 필요 시) | v1.0 합의 유지 |
| 그래프 | React Flow (코드맵/ERD 전용) | v1.0 합의 유지 |

### 7.2 거버넌스 컴포넌트 라이브러리 (7종, Phase 1 동시 구축)

| 컴포넌트 | 용도 | 사용 페이지 |
|----------|------|-------------|
| `<RiskBadge>` | LOW/MED/HIGH/CRIT 색상 배지 | P07, P31, P35, P43, P48, P07 |
| `<ChangeRequestCard>` | CR 한 건 카드 (eval_score + rollout_pct + diff 미리보기) | P07, P39, P46 |
| `<RolloutPctDial>` | 0/10/50/100 단계 조작 | P07, P39, P46 |
| `<PromptVersionTimeline>` | version 히스토리 + 롤백 버튼 | P39 |
| `<GoldenSetResult>` | 50건 골든셋 결과 테이블 + 품질 점수 분산 | P27 |
| `<AuditLogStream>` | 실시간 audit 스트림 (가상 스크롤) | P47, P34 |
| `<EmergencyButton>` | 2차 확인 다이얼로그 + 텔레그램 알림 | P48 |

→ Phase 1 AADS-199와 병렬로 **AADS-199B 컴포넌트 라이브러리 구축** (1일, Next.js + Tailwind).

### 7.3 Sidebar 재편 (평탄 23 → 3-depth 5 카테고리)

```
🗂 Work          : /chat, /tasks, /projects, /agenda, /decisions, /flow, /braming
🛡 Governance ⭐ : /admin/prompts, /admin/intent-policies ★, /admin/cr-queue (P07) ★,
                  /admin/model-parity, /admin/governance-audit ★, /admin/emergency ★,
                  /admin/policies (P31) ★, /admin/tool-grants (P25) ★
📊 Observability : /ops, /server-status, /reports, /memory, /lessons, /docs
🤖 Agents        : /managers, /admin/agents (P43) ★, /ops/pc-agents
⚙️ Settings      : /settings, /admin/env-config (P38) ★, /login 관련
```

⭐ = Phase 1~2에서 신설

### 7.4 기존 `.bak` 파일 정리 방침

- `globals.css.bak_bottomnav`, `layout.tsx.bak_aads`, `page.tsx.bak.T049/T060/202603071016` → Phase 1 완료 시 일괄 삭제 (git history 로 복구 가능, 현재 리포 비대화 유발)
- AADS-199에 "bak 파일 정리" 서브태스크 추가

---

## 8. 기술 스택 결정 (v1.0 + 거버넌스 추가)

v1.0 표에 **5행 추가**:

| 영역 | 기획 제안 | AADS 결정 | 사유 |
|------|-----------|-----------|------|
| Feature Flag | LaunchDarkly | **PostgreSQL + Redis pub/sub** | 월정액 외부 의존 제거 |
| Change Request | ProductBoard 등 | **자체 구현** (`change_requests` 테이블) | AADS 규칙과 결합 필요 |
| Golden Set Eval | PromptFoo | **`scripts/model_parity_check.py`** (자체) | 크론 + reports/ 파일로 관리 단순 |
| 감사 로그 | Datadog | **`governance_audit_log` + Grafana 연동** | 내부 보관 우선 |
| Kill-switch | Unleash | **Redis pub/sub 직접** | 5초 SLA 충족 가능 |

---

## 9. 위험 & 완화 (v1.0 + 거버넌스 추가)

v1.0 6건 + **4건 추가**:

| 위험 | 완화 |
|------|------|
| Shadow→Primary 전환 시 불일치 누적 | 전환 전 7일 shadow + diff 비율 <1% 확인 |
| Kill-switch 오작동 (실수로 켜면 대혼란) | 2차 확인 다이얼로그 + 텔레그램 1회 알림 + 10초 cool-down |
| CR 승인 병목 (CEO 단독 승인) | 자동 eval_score ≥ 기준이면 CTO AI 승인 위임 가능 |
| 골든셋 편향 | 월 1회 CEO가 golden set 10건 수동 추가·제외 |

---

## 10. 성공 지표 (v1.0 + addendum)

| 지표 | 현재 | Phase 1 목표 | Phase 2 목표 | Phase 4 목표 |
|------|------|-------------|-------------|-------------|
| 기획서 반영률 (48 기준) | 22.9% | **35.4%** | **56.3%** | 83.3% |
| **인텐트별 temperature 준수율** | 0% | **100%** | 100% | 100% |
| **CR eval_score 평균** | — | — | **≥ 0.8** | ≥ 0.85 |
| **Prompt 1-click 롤백 MTTR** | N/A | — | **< 1분** | < 30초 |
| **Kill-switch 검증 훈련** | 0회 | 0회 | **월 1회** | 주 1회 |
| **Shadow diff 비율** | N/A | **< 5%** | **< 1%** | < 0.5% |
| **모델 패리티 분산** | 미측정 | — | **< 0.15** | < 0.1 |

---

## 11. 다음 액션 (CEO 승인 요청)

### Phase 1 즉시 제출 가능 (총 5건, 7.5 영업일 — 순차 / 2.5~3일 — Worktree 병렬)

| Task ID | 제목 | Size | Model | 소요 |
|---------|------|------|-------|------|
| **AADS-199** ⭐ | 백엔드 거버넌스 배선 (temperature + intent_policies + feature_flags Shadow) | L | opus | 1.5일 |
| **AADS-199B** | 거버넌스 컴포넌트 라이브러리 7종 구축 | S | sonnet | 1일 |
| AADS-200 | P35 태스크 보드 칸반화 | M | sonnet | 2일 |
| AADS-201 | P43 에이전트 관리 (**scope 포함**) | M | sonnet | 2.5일 |
| AADS-202 | P36 세션 리플레이 간이판 | M | sonnet | 1.5일 |
| AADS-203 | P37 배포 현황 | S | sonnet | 1일 |

**제출 전략 옵션**:
- **A. 순차**: 199 → 199B → 200~203 (안전, 7.5일)
- **B. 199 선행 후 199B+200~203 병렬**: Worktree 분기, **3~4일**
- **C. AADS-199만 우선**: CEO 결재 후 나머지 재검토 (가장 보수적)

---

## 부록 A. v1.0 → v1.1 교체 매핑 (변경/유지 한눈에)

| v1.0 섹션 | v1.1 상태 |
|-----------|-----------|
| §0 Executive Summary | 교체 (§0 v1.0 대비 Δ) |
| §1 실측 현황 | 보강 (§1.2 디자인 시스템, §1.3 DB 스키마 요구) |
| §2 매핑 매트릭스 | 확장 (§2.1 변경만 / §2.2 신규 3 / §2.3 기존 유지 / §2.4 재계산) |
| §3 AADS 고유 기능 | **변경 없음** (v1.0 §3 그대로) |
| §4 14주 Phase | 재편 (§3 Phase 재구성) |
| §5 Phase 1 상세 | 확장 (§4 신규 3 + §5 기존 6 확장) |
| §6 기술 스택 | 확장 (§8 거버넌스 5행 추가) |
| §7 DB 마이그레이션 | 확장 (§6 Phase 1 확장) |
| §8 위험 | 확장 (§9 거버넌스 4건 추가) |
| §9 성공 지표 | 교체 (§10 재정의) |
| §10 다음 액션 | 교체 (§11 AADS-199/199B 추가) |
| — | §7 **디자인 시스템 장 신설** |

---

## 부록 B. 변경 이력

| v | 날짜 | 작성자 | 변경 |
|---|------|--------|------|
| 1.0 | 2026-04-24 | CTO AI | 최초 작성, 45 페이지 매핑 + 14주 로드맵 |
| **1.1** | **2026-04-24** | **CTO AI** | **addendum v2.1 통합, 48 페이지(+3), 디자인 시스템 장 신설, Phase 1 재편(AADS-199/199B 선행)** |

끝.
