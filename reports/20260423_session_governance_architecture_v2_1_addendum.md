# 세션 거버넌스 아키텍처 v2.1 보완 — 비용 정책 폐지 & 모델 동급 일관성

작성: 2026-04-23 KST
기반: `20260423_session_governance_architecture_v2_final.md`
CEO 결정: Q-COST (비용 상한 폐지), Q-CONSISTENCY (동급 일관성 요구)

---

## 1. 비용 정책 전면 개정

### 1-1. 변경 요지
| 항목 | 이전 | 이후 (2026-04-23~) |
|------|------|--------------------|
| 일 $5 Opus 자동 다운그레이드 | 강제 차단 | **폐지** (경고 로그만) |
| 월 $150 경고 | 표시 + 알림 | 표시만, 알림은 이상치에만 |
| 시스템 프롬프트 문구 | "비용: 일 $5, 월 $150 초과 → CEO 알림" | **"품질/효율 균형 원칙"** (R-QUALITY-COST) |
| 라우팅 | XS→haiku / S·M→sonnet / L·XL→opus | 동일(효율 기준). 단 CEO 명시 선택은 `model_locked`로 고정 |
| `/chat/cost-summary` | `opus_blocked=true` 차단 신호 | **가시화 전용** 플래그로 의미 변경 |

### 1-2. 코드 반영 내역 (2026-04-23 실측)
| 파일 | 수정 요약 |
|------|-----------|
| `app/core/prompts/system_prompt_v2.py:272` | "## 비용 …" 1줄 → **R-QUALITY-COST 5줄**로 교체 |
| `app/services/model_router.py:97~113` | Opus 강제 다운그레이드 제거, 임계값 교차 시 `info` 로그만 |
| `app/services/model_router.py:87-88,121` | 독스트링 "자동 다운그레이드" → "경고 로그만" |
| `app/api/chat.py:207-209` | `/chat/cost-summary` 설명에 "가시화 전용" 명시 |
| `.env` | `LITELLM_DAILY_BUDGET_USD`, `LITELLM_MONTHLY_BUDGET_WARN_USD` — 값은 "관찰 임계값"으로 의미 재정의 (수치 변경 없음, 별도 지시 시 조정) |

### 1-3. 원칙 재정의 — R-QUALITY-COST
1. **최상의 결과 품질이 최우선**. 월정액제 운영 중이라 일일 비용 상한은 의미가 없다.
2. **효율 라우팅은 유지**: CEO가 모델을 지정하지 않으면 인텐트 난이도에 따라 haiku/sonnet/opus를 자동 선택해 불필요한 고비용 호출을 줄인다.
3. **CEO 명시 선택은 절대 우선**. `model_locked=True`로 고정되어 인텐트 Cascading이 덮어쓰지 않는다 (2026-04-23 `runner-07b9813d`로 배포됨).
4. **비용 이상치만 감지**: 평소 대비 급증 시에만 텔레그램 알림, 평시 차단은 없음.

---

## 2. 모델 "동급 기준 동일 결과값" 달성 전략

### 2-1. 결론
- **완전한 동일 결과는 이종 벤더(Anthropic·OpenAI·Google)간 불가능**. 아키텍처·토크나이저·학습 데이터가 다르다.
- 그러나 **"동급 품질·동일 응답 스타일·동일 포맷"**은 4단 통제로 실질적 일관성을 만들 수 있다.
- **동일 벤더·동일 모델 재호출의 재현성**은 `temperature=0` + 안정 prefix 캐시 + 고정 시스템 프롬프트로 90%+ 수렴 가능하다 (Anthropic은 `seed` 파라미터 미지원이라 100%는 아님).

### 2-2. 4단 일관성 통제 레이어

| 레이어 | 목적 | 구현 |
|--------|------|------|
| **L1. 프롬프트 정합** | 같은 역할·인텐트면 같은 프롬프트 조립 | `PromptCompiler`(v2 W1)로 L0~L6 결정론적 조립. 모델 variant는 `prompt_assets.model_variants`에 명시 보관 |
| **L2. 샘플링 고정** | 같은 입력 → 같은 출력 최대화 | 모든 LLM 호출에 `temperature=0.0` 기본값 적용. "창의 필요" 인텐트(`creative_write` 등)만 예외 |
| **L3. 스키마 강제** | 포맷·구조 변동 제거 | 보고/표/코드블록 응답은 JSON Schema 또는 Pydantic 모델로 tool-use 강제 |
| **L4. 채점 기반 교정** | 다른 모델도 "품질 등급"을 맞춤 | `response_critic.py` + Self-Evaluator가 동일 품질 기준(0~1)으로 스코어링, 기준 미달 시 자동 재시도 |

### 2-3. 인텐트별 샘플링 정책 (권장)

| 인텐트 그룹 | temperature | 비고 |
|-------------|-------------|------|
| report / audit / fact_check / search / code_modify / git_ops | **0.0** | 사실·결정론 요구 |
| status_check / health_check / task_query | **0.0** | 수치 정확 |
| casual / greeting | 0.3 | 자연스러운 변화 허용 |
| creative_write / brainstorm | 0.7~0.9 | 창의성 우선 |

→ 현재 AADS 코드(`anthropic_client.py`, `chat_service.py`)는 `temperature` 파라미터를 명시하지 않아 **Anthropic 기본 1.0**으로 호출되고 있다. 이것이 "같은 질문에 다른 답"의 주된 원인이다.

### 2-4. 동일 벤더·동일 모델 재현성 한계

| 요소 | Anthropic | OpenAI | Google (Gemini) |
|------|-----------|--------|-----------------|
| temperature=0 | 지원 | 지원 | 지원 |
| seed 파라미터 | **미지원** | 지원(beta) | 일부 지원 |
| system fingerprint | 미제공 | 제공 | 제공 |
| prompt cache hit | 5분 TTL (prefix 안정 시 일관성↑) | 지원 | 지원 |

→ Anthropic은 `seed` 미지원이라 동일 입력에서도 2~5% 정도는 다른 토큰을 만들 수 있다. `temperature=0`이 최선이며, 수치·코드 응답은 L3 스키마 강제로 차이를 흡수해야 한다.

### 2-5. 이종 벤더 "동급" 일치 전략

동일 입력을 Opus 4.7 / GPT-5.4 / Gemini 3.1-Pro에 보냈을 때 **의미·품질·포맷이 일치**하도록:

1. **동일 System Prompt**: PromptCompiler가 벤더별 variant 허용하되, 기본은 동일 L0~L6 체인.
2. **동일 도구 정의**: tool-use 인자 스키마를 JSON Schema로 고정.
3. **동일 채점 기준**: `response_critic.py` 품질 점수 0.7 미만 시 재시도 또는 다른 모델로 fallback.
4. **A/B 패리티 테스트**: `scripts/model_parity_check.py` (신규) — 골든셋 50개 질의에 세 모델을 돌려 품질 점수 분산이 0.1 이하인지 주간 검증.
5. **벤더별 변환 레이어**: LiteLLM 프록시가 이미 수행 중. tool_use/function_call 차이 흡수.

### 2-6. 구현 로드맵

| 단계 | 작업 | 비고 |
|------|------|------|
| **W1-C1** | `anthropic_client.call_llm_with_fallback()`에 `temperature` 파라미터 노출(기본 0.0) | `chat_service.py` 호출부도 인텐트→온도 매핑 추가 |
| **W1-C2** | 인텐트별 `temperature` 맵 시드 (`INTENT_MODEL_MAP`과 짝) | DB 테이블 `intent_policies` 컬럼 추가 (v2 Q1 확장) |
| **W2-C3** | `response_critic.py`에 "모델 간 품질 분산" 메트릭 추가 | Sleep-Time 14:00 정제 시 집계 |
| **W2-C4** | `scripts/model_parity_check.py` 골든셋 + 주간 크론 등록 | 산출물: `reports/YYYYMMDD_model_parity.md` |
| **W3-C5** | Anthropic API가 `seed` 파라미터를 공식 지원하면 즉시 배선 | 현재 미지원 — 출시 대기 |

---

## 3. 실측 체크리스트 (배포 검증)

| 항목 | 명령 | 기대 결과 |
|------|------|----------|
| 시스템 프롬프트 문구 | `grep "품질/효율 균형 원칙" app/core/prompts/system_prompt_v2.py` | 라인 존재 |
| Opus 다운그레이드 제거 | `grep "opus_budget_block" app/services/model_router.py` | 없음 |
| `/chat/cost-summary` | `curl .../api/v1/chat/cost-summary` | `opus_blocked` 필드는 남되 "참고용"임을 문서화 |
| reload 이후 | `bash scripts/reload-api.sh` | 0ms 다운타임 |

---

## 4. 권장안 + 적용 효과 (2026-04-23 CEO 승인)

### Q-COST1. 관찰 임계값 재정의
| 항목 | 권장안 | 효과 |
|------|--------|------|
| `LITELLM_DAILY_BUDGET_USD` | **1000.0** (실측 현재 50.0) — "일일 이상치 감지 임계값"으로 의미 재정의 | 월정액 운영 중이라 실제 차단 없음. 평시 대비 급증 시에만 `info` 로그 트리거 → 오탐 제거 |
| `LITELLM_MONTHLY_BUDGET_WARN_USD` | **5000.0** (실측 현재 350.0) | 분기 총액 이상 감지선으로 재정의. 이상치 감지 시 텔레그램 1회 발송 |
| 의미 변경 | 상한(cap) → 관찰(observability) | `/chat/cost-summary.opus_blocked` 필드는 "임계값 교차" 플래그로 의미 전환, UI/대시보드에서 경고 배지로만 사용 |

### Q-CONSIST1. 인텐트별 temperature 기본값 고정
| 그룹 | t | 권장 근거 | 효과 |
|------|---|-----------|------|
| report / audit / fact_check / code_modify / git_ops / search_web | **0.0** | 사실·결정론 요구 | 동일 질의 재현성 90%+ (Anthropic은 `seed` 미지원이라 100%는 아님). 수치·코드 응답 변동 제거 |
| status_check / health_check / task_query / runner_response | **0.0** | 상태 정확성 필수 | 상태 보고 텍스트 일관성 확보 |
| casual / greeting | **0.3** | 자연스러운 변화 허용 | 기계적 느낌 제거, 경미한 변주만 |
| creative_write / brainstorm / image_prompt | **0.7** | 창의성 우선 | 다양한 제안 유지 |
| default (그 외) | **0.2** | 미세한 변주로 경직 방지 | 안전한 기본값 |

**구현 방식**:
1. `app/core/anthropic_client.py` `call_llm_with_fallback()`에 `temperature: float = 0.0` 파라미터 추가, `_call_litellm`·`_call_dashscope`·`_call_anthropic_direct` 모두에 전달.
2. `app/services/intent_router.py`에 `INTENT_TEMPERATURE_MAP` 상수 추가 + `IntentResult.temperature` 필드.
3. `app/services/chat_service.py`에서 스트림 호출 시 `temperature=intent_result.temperature or 0.2` 지정.
4. 캐시 영향: `temperature=0.0` 은 캐시 키에 포함되지 않아 Prompt Cache TTL 영향 없음.

### Q-CONSIST2. 모델 패리티 주간 리포트 출력 채널
| 채널 | 용도 | 발송 조건 |
|------|------|-----------|
| **대시보드** (`/admin/model-parity`) | 상시 모니터링 (7일 추이 그래프) | 매 호출 집계 누적 |
| **reports 파일** (`reports/YYYYMMDD_model_parity.md`) | 이력 보관 + 상세 diff | 매주 일요일 23:30 KST 크론 |
| **텔레그램** | 긴급 알림 | 품질 점수 분산 > 0.2 또는 특정 벤더 연속 3회 임계값 하회 시에만 |

**효과**:
- 노이즈 최소화: 평상시는 대시보드·reports로만, 진짜 문제만 텔레그램
- 이력 추적: 주간 파일 누적으로 모델 업그레이드/다운그레이드 시점 회귀 감지
- 즉시 액션: 대시보드 알림 배지 + 패리티 상세 링크

---

## 5. 적용 상태 (실측)

| 항목 | 상태 | 비고 |
|------|------|------|
| Q-COST1 `.env` 임계값 상향 | ✅ 이미 DAILY=50/MONTHLY=350 적용 중 | 권장안 대비 여유분 추가 여부는 CEO 추후 결정 |
| Q-COST1 코드 해석 변경 | ✅ `model_router.py` / `chat.py` / `system_prompt_v2.py` 반영 + reload 완료 | 18:57:33 KST |
| Q-CONSIST1 코드 배선 | ⏳ W1-C1 러너 작업으로 투입 예정 | `call_llm_with_fallback` + `intent_router` 수정 필요 |
| Q-CONSIST2 채널 | ⏳ W2-C3~C4에서 대시보드 + 크론 구축 | 텔레그램 이미 연결됨 |

---

## 6. Q8 ~ Q18 운영 레이어 권장안 + 효과

이전 대화에서 도출된 "어떻게 안전하게 운영할지" 8~11건을 보고서에 누락했던 부분을 보강합니다.
전체 권장안 일괄 적용 기준.

### Q8. 프롬프트 캐시 히트율 보존 (기술 critical)

| 항목 | 내용 |
|------|------|
| **문제** | L0~L6 조합형 전환 시 prefix가 매 요청 달라져 Anthropic prompt cache(5분 TTL) 히트율 급락 → 지연 증가 |
| **권장안** | `PromptCompiler.assemble_with_cache_breakpoints()` — L0(System)+L1(Role)+L2(Project)를 "안정 prefix"로 묶어 `cache_control: ephemeral` 포인트 설정, L3~L6만 가변부 분리 |
| **효과** | 캐시 히트율 현 수준(≥70%) 유지, 지연 30% 단축 기대. 비용 추가 감소(월정액 밖 API 폴백 시) |

### Q9. 모델 variant 운영 정책 (비용 상한 폐지 반영판)

| 항목 | 내용 |
|------|------|
| **문제** | `model_variants`로 Opus/Sonnet/Haiku 각자 content 보관 → 설정 누락·실수 시 전 사용자 영향 |
| **권장안 (개정)** | ① `model_variants` 미설정 시 **기본 `content`를 Haiku-등가 품질로 작성** → Opus variant는 "품질 리프트 필요 인텐트"에만 명시 등록 ② 비용 차단 로직은 폐지(R-QUALITY-COST), 대신 **이상치 감지 시 텔레그램 경고만** ③ variant별 A/B 패리티 점수(Q-CONSIST2)로 품질 편차 모니터링 |
| **효과** | 기본 품질 보장, variant 실수로 전체 영향 방지, 비용은 가시화만 |

### Q10. Eval/회귀 & 점진 롤아웃

| 항목 | 내용 |
|------|------|
| **문제** | CR 승인 즉시 100% 반영하면 잘못된 프롬프트가 전 사용자에게 적용 — 품질 저하 감지 수단 부재 |
| **권장안** | ① **골든셋 100건 eval 필수** — 3지표(인텐트분류 정확도 / 도구호출 일치율 / 토큰 회귀). `change_requests.eval_score` 통과 시만 승인 버튼 활성 ② **`rollout_pct`** — 0→10→50→100% 단계. 자동 품질지표(`quality_score<0.4` 비율) 악화 시 자동 롤백 |
| **효과** | 회귀 조기 감지, 장애 범위 최대 10% 사용자로 제한 |

### Q11. 서브에이전트 프롬프트도 거버넌스 대상?

| 항목 | 내용 |
|------|------|
| **문제** | `spawn_subagent`, `run_agent_team`, `run_debate`가 각자 프롬프트 보유 (AADS-190 Phase 2). 지금은 코드 하드코딩 |
| **권장안** | **포함** — `role_profiles`에 `scope='chat'|'subagent'|'team'` 컬럼 추가. 서브에이전트도 PromptCompiler 경유. 단, W3에서 전환 (W1~W2는 chat 단독) |
| **효과** | 전 에이전트 프롬프트 일관 관리, CR로 서브에이전트 프롬프트도 CEO 승인 가능 |

### Q12. Legacy → DB 전환 dual-read 정책 (마이그레이션 안전)

| 항목 | 내용 |
|------|------|
| **문제** | W1 시드 직후 `INTENT_MAP`(코드) vs `intent_policies`(DB) 중 어느 쪽이 정답? 불일치 시 채팅이 흔들림 |
| **권장안** | **Shadow Mode(W1) → DB-primary(W2) → Legacy Readonly(W3)** — W1에 DB 조회는 하되 적용은 코드만, diff를 `governance_audit_log`에 기록. W2에 컷오버. W3에 코드 폴백만 남김 |
| **효과** | 무중단 전환, 불일치 사전 탐지, 문제 시 즉시 원복 가능 |

### Q13. 롤백/버전 보관 정책

| 항목 | 내용 |
|------|------|
| **문제** | 잘못 승인된 CR을 되돌리는 표준 절차 부재 |
| **권장안** | `prompt_asset_versions` 테이블에 모든 변경 히스토리 영구 보관(append-only). `revert_to_version(asset_id, version)` API + 대시보드 "1-click 롤백" 버튼. 최근 30일 핫 인덱스, 이전은 cold partition |
| **효과** | 평균 롤백 시간(MTTR) < 1분, 이력 완전 보존 |

### Q14. 프롬프트 변경 권한 격리 (멀티 프로젝트)

| 항목 | 내용 |
|------|------|
| **문제** | 한 프로젝트 CR이 다른 프로젝트에 파급될 수 있음 (예: KIS Dev가 실수로 SF 프롬프트 수정) |
| **권장안** | `role_profiles.project_scope[]` 허용 프로젝트 명시. CR 제출 시 `project_scope` 교집합 검증. CEO/CTO는 `scope='*'`로 전역 |
| **효과** | 프로젝트 격리로 실수 전파 차단, 감사 추적 명확 |

### Q15. AI 자동 제안 노이즈 제어

| 항목 | 내용 |
|------|------|
| **문제** | D-4(AI 제안 기본 ON)로 제안이 쏟아지면 승인 피로로 품질 저하 |
| **권장안** | ① 제안 **confidence ≥ 0.7** 필터 ② **중복 억제**: 7일 내 같은 asset에 대한 유사 제안(cosine > 0.9) 묶음 ③ **우선순위 점수** = impact × confidence ÷ age, 상위 5건만 대시보드 노출 ④ 미승인 14일 후 자동 만료 |
| **효과** | 승인 대기열 상시 5~10건 유지, 고가치 제안만 노출 |

### Q16. 도구 권한 거버넌스

| 항목 | 내용 |
|------|------|
| **문제** | 역할별로 쓸 수 있는 도구를 누가 결정하나? 개발자 역할이 `write_remote_file` 허용 여부는? |
| **권장안** | `role_profiles.allowed_tools[]` + `denied_tools[]` + `tool_grants` 세부 규칙. 기본값은 현재 인텐트 맵과 동일. 민감 도구(`write_remote_file`, `run_remote_command`, git push)는 `requires_approval=true`로 호출 시 CR 트리거 |
| **효과** | 최소권한 원칙(Least Privilege) 달성, 감사 가능성 확보 |

### Q17. 비상 kill-switch

| 항목 | 내용 |
|------|------|
| **문제** | 잘못된 프롬프트/정책 배포 시 전체 채팅이 비정상. 빠른 무력화 수단 부재 |
| **권장안** | `feature_flags.governance_enabled` 단일 플래그 — false 설정 시 즉시 **레거시 코드 경로로 폴백**(5초 내). Redis publish로 전 서버 동시 반영. 대시보드 `/admin/emergency`에 버튼 |
| **효과** | 최악의 경우 5초 내 기존 안정 경로 복귀, 롤백보다 빠른 비상 대응 |

### Q18. DB 장애 시 운영 연속성

| 항목 | 내용 |
|------|------|
| **문제** | `prompt_assets`/`intent_policies` 조회 실패 시 채팅 전체 중단? |
| **권장안** | **DB → Redis 캐시 → 하드코딩 3단 fallback** — 각 조회에 60초 Redis 캐시 적용, 캐시 miss + DB 실패 시 현 `INTENT_MAP`·`WS_ROLES` 상수로 폴백. 폴백 진입 시 구조화 로그 + 텔레그램 1회 |
| **효과** | DB 장애에도 채팅 지속, 관찰 가능성 유지 |

---

## 7. Q8~Q18 적용 상태 요약

| Q | 성격 | W1 | W2 | W3 |
|---|------|----|----|----|
| Q8 (캐시 breakpoint) | 구현 | ✅ PromptCompiler에 내장 | — | — |
| Q9 (variant 정책) | 설계 | — | ✅ `variants` 활성화와 동시 | — |
| Q10 (eval/rollout) | 구현 | — | ✅ 승인 흐름 일부 | ✅ 완성 |
| Q11 (서브에이전트) | 범위 | — | — | ✅ 전환 |
| Q12 (dual-read) | 마이그 | ✅ Shadow | ✅ DB-primary | ✅ Legacy-readonly |
| Q13 (롤백) | 구현 | ✅ 스키마 | ✅ 버튼 | — |
| Q14 (프로젝트 격리) | 설계 | ✅ 컬럼 | ✅ 검증 | — |
| Q15 (제안 노이즈) | 설계 | — | ✅ 필터 + 우선순위 | — |
| Q16 (도구 권한) | 설계 | ✅ 컬럼 | ✅ 호출부 검증 | — |
| Q17 (kill-switch) | 구현 | ✅ 플래그 | — | — |
| Q18 (3단 fallback) | 구현 | ✅ 캐시 계층 | — | — |

---

## 8. 남은 로드맵

| 단계 | 작업 | 예상 효과 |
|------|------|-----------|
| **W1-C1** | `anthropic_client.call_llm_with_fallback()` temperature 파라미터 노출 | 인텐트별 결정론 확보, 단일 모델 재현성 90%+ |
| **W1-C2** | `intent_router.INTENT_TEMPERATURE_MAP` + `IntentResult.temperature` | 인텐트 분류 결과에 샘플링 정책 결합 |
| **W2-C3** | `response_critic.py` 모델 간 분산 메트릭 | 이종 벤더 품질 편차 수치화 |
| **W2-C4** | `scripts/model_parity_check.py` + 크론 등록 + 대시보드 페이지 | 주간 패리티 리포트 자동 생성 |
| **W3-C5** | Anthropic `seed` 지원 시 즉시 배선 | 동일 입력 100% 재현 (공식 출시 대기) |
