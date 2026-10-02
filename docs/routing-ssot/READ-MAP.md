# 모델 라우팅 설정 읽기 지도 (READ-MAP)

TASK_ID: AADS-ROUTING-SSOT-DESIGN-20261002 · 작성 2026-10-02 · 설계 문서(코드·DB 변경 없음)

이 문서는 "모델을 어디서 정하는가"를 흩어진 저장소별로 풀어 적는다.
무엇이 이기는지, 같은 개념을 누가 어떻게 다르게 정의하는지를 코드 위치와 함께 남긴다.
정본 선택과 이행 계획은 `DESIGN.md` 에 있다.

**근거 두 가지.** 코드 위치는 이 저장소 `app/`, `scripts/` 를 grep 한 것이고,
`[실측]` 표시는 2026-10-02 운영 DB 를 SELECT 로만 조회한 값이다(쓰기 없음).
`[추정]` 은 코드를 읽고 내린 해석이며 실행해 확인하지 않았다.

---

## 0. 한눈에 — 저장소 6개 + 코드 상수

| # | 저장소 | 형태 | 키 | 행 수 [실측] | 쓰는 화면/API |
|---|---|---|---|---|---|
| 1 | `model_routing_preferences` (mrp) | 후보 목록 행 | PK (route_key, provider, model_id), route_key당 default 1개 | 라우트 52종 / 274행 | `PUT /llm-models/routing-preferences` (설정 > ModelSettingsPanel) |
| 2 | `intent_policies` | intent별 1행 | intent (UNIQUE) | 17 | `POST /governance/intent-policies`, `PATCH /llm-admin/intent-policies/{intent}` |
| 3 | `runner_model_config` | size별 JSONB 배열 | size (PK) | 6 (XS S M L XL AI_REVIEW) | `PUT /settings/runner-models` (설정 페이지) |
| 4 | `llm_fallback_chains` | route별 단계 행 | UNIQUE (route_key, step_order) | 15 (background 7 / chat_main 4 / runner 3) | `PATCH /llm-admin/chains/...` — 대시보드에서 호출하는 곳을 찾지 못함 [추정: 화면 없음] |
| 5 | `chat_model_preferences` | 모델별 표시 설정 | preference_key | — | `PUT /llm-models/chat-preferences` (ChatModelOrderPanel) |
| 6 | `directive_model_config` | role별 JSONB 배열 | role (PK) | 1 (generation) | `PUT /settings/directive-models` |
| 7 | env / 코드 상수 | 프로세스 기동 시 고정 | — | — | 코드 수정 + reload |

`llm_models`(모델 카탈로그: 실행 가능 여부·verification_status)는 "무엇을 쓸 수 있나"를 담는 별개의 레지스트리다.
라우팅 설정이 아니므로 이 지도의 저장소에서 뺐다. 다만 여러 읽기 코드가 mrp 와 JOIN 해서 가용성을 판정한다.

> `llm_fallback_chains` 는 저장소 안에 `CREATE TABLE` 이 없다(`grep` 0건). 운영 DB 에만 존재한다.
> 새 환경을 만들면 이 테이블이 없어 `get_fallback_steps` 가 빈 목록을 돌려주고 env 로 떨어진다.

---

## 1. 저장소별 읽기 지점

표기: 파일:함수 — 읽는 시점 — 충돌 시 우선순위.

### 1-1. `model_routing_preferences`

route_key 는 용도별로 갈린다. **읽는 코드가 있는 키와 없는 키를 구분하는 것이 이 지도의 핵심이다.**

| route_key | 읽는 코드 (파일:함수) | 시점 | 충돌 시 |
|---|---|---|---|
| `llm` (default 1 + 후보) | `services/model_selector.py:_get_default_llm_model_from_db` (L547, TTL 60s) | 자동/mixture/빈 모델 턴, 재개 턴 (`chat_service.py` L8809, L14104) | 사용자 지정 모델이 있으면 읽지 않는다. 자동 선택일 때만 이김 |
| `llm` (후보 전체) | `services/model_selector.py:_configured_llm_fallback_candidates` (L1905) | 모델 실패 후 폴백 후보 산출 | `llm_models` 가용성 필터 뒤에 적용 |
| `llm` | `services/cli_model_autoreg.py:register_chat_llm_candidates` (L303) | `GET /routing-preferences` 호출 때마다 | 읽고 **쓴다**: 새 CLI 모델을 `is_enabled=false` 로 추가 |
| `runner_llm` | `api/pipeline_runner.py:_get_model_cycle_for_size` (L678) | 러너 체인 산출 (3순위 꼬리) | `runner_model_config` 뒤. §2-C 참조 |
| `runner_llm` | `services/pipeline_runner_service.py:_get_db_model_config` (L423) | pipeline_c 실행 | 동일. 추가로 codex 실행가능 가드 |
| `runner_llm` | `scripts/pipeline-runner.sh:get_db_model_cycle` (L548) | 셸 러너 실행 | 동일 (Python 과 **SQL 이 따로 복제됨**) |
| `runner_llm`, `llm`, `code_exec` | `services/model_selector.py:_get_codex_cli_fallback_model_from_db` (L729) | codex 한도 소진 시 폴백 모델 | runner_llm → llm → code_exec 순 |
| `runner_llm`, `llm` | `services/cli_model_autoreg.py:register_runner_llm_candidates` (L230) | 모델 자동 등록 | 쓰기 (`is_enabled=false` 로만) |
| `image`, `embedding`, `deep_research` | `services/ai_route_resolver.py:get_route_candidates` (L142) ← `image_service`, `chat_embedding_service`, `deep_research_service` | 요청 시 | route 내 `is_default DESC, display_order` 순 |
| `image`, `edit_image`, `video`, `audio`, `music` | `services/media_generation_service.py:_fetch_default_route` (L370), `_fetch_route_preference` (L403) | 미디어 생성 요청 | default → 명시 지정 순 |
| (전체) provider=google/gemini | `services/model_registry.py:_google_commercial_routes_enabled` (L1165) | Gemini 카탈로그 호출 게이트 | env `…GOOGLE_ROUTE…` 가 참이면 DB 안 봄 |
| (전체) | `api/llm_models.py:get_model_routing_preferences` (L650) | 설정 화면 로드 | 표시용 |
| `background_llm` | **런타임 읽기 없음** [추정] | — | 설정 화면에만 보인다. §2-A |
| `agent_*` 11개 route | **런타임 읽기 없음** [추정] | — | 키 허용 목록(`ai_route_resolver.AI_ROUTE_KEYS`)에만 있다 |
| `intent_*` 23개 route | **런타임 읽기 없음** [추정] | — | 위와 같음 |
| `search`, `url_analyze`, `fact_check`, `visual_qa`, `semantic_search`, `image_analyze`, `video_analyze` | 호출처를 찾지 못함 [추정] | — | 위와 같음 |
| `fallback_chain_loader.get_fallback_chain` | **import 하는 곳 0건** (`grep -rn fallback_chain_loader app scripts` → 자기 자신뿐) | — | 죽은 모듈. docstring 만 "mrp 단일 소스" 라고 주장 |

"런타임 읽기 없음" 근거: `get_route_candidates(` / `get_first_route_candidate(` 호출처 전수는
`image`, `embedding`, `deep_research` 세 키뿐이고, 다른 mrp 읽기 SQL 은 위 표의 키를 리터럴로 지정한다.
`agent_*`·`intent_*`·`background_llm` 은 리터럴로 쿼리하는 코드가 없다.
단, 키를 문자열 조합으로 만드는 코드는 grep 으로 완전히 배제할 수 없다 — 이행 1단계 전에 로그로 확인한다(DESIGN §5).

### 1-2. `intent_policies`

| 읽는 코드 | 시점 | 읽는 컬럼 | 충돌 시 |
|---|---|---|---|
| `services/model_selector.py:_load_intent_policies` (L781, TTL 300s) → `_resolve_governed_intent_model` (L962) | 채팅 턴, 사용자 지정 모델이 **없을 때만** (L2288) | default_model, cascade_downgrade, allowed_models | 아래 [중요] |
| `api/governance.py:list_intent_policies/upsert` (L72/L91) | 관리 화면 | 전 컬럼 | — |
| `api/llm_admin.py:get_intent_policies/update` (L215/L230) | 관리 화면 | 4개 컬럼 | **`governance.py` 와 같은 테이블을 쓰는 두 번째 API 경로**(둘 다 `/api/v1` 아래 마운트) |

[중요] `intent_policies` 는 "이 intent 는 이 모델을 쓴다"가 아니다. **현재 모델이 허용 목록의 Claude 등급보다 높으면 낮춘다**는 상한 정책이다.
`default_model` 은 `allowed_models` 에 Claude 등급이 하나도 없을 때만 대체 기준으로 쓰인다(`_resolve_intent_policy_cascade_model`, L829).
그래서 이 테이블의 모델 id 를 바꾸면 값만 바뀌는 것이 아니라 **등급 매핑이 달라질 수 있다** — §2-D.

충돌 우선순위 (feature flag `intent_policies_db_primary` = **true** [실측]):
1. DB 정책이 있고 결과가 나오면 DB 결과.
2. DB 정책은 있으나 변경 없음 → 변경 없음 (코드 상수로 안 떨어진다).
3. DB 에 그 intent 가 없으면 코드 상수 `_resolve_legacy_intent_cascade_model` (L873).
플래그가 false 면 코드 상수가 이기고 DB 결과는 `governance_audit_log` 에 shadow 기록만 한다.

### 1-3. `runner_model_config`

| 읽는 코드 | 시점 | 비고 |
|---|---|---|
| `api/pipeline_runner.py:_get_model_cycle_for_size` (L678) | 러너 제출 시 모델 결정, `GET /settings/runner-models` 의 effective 계산 | size → AI_REVIEW → mrp `runner_llm`, 중복 제거 후 `filter_executable_models` |
| `services/pipeline_runner_service.py:_get_db_model_config` (L423) | pipeline_c 실행 | 같은 3단 병합을 SQL 하나로. `filter_executable_models` 는 **안 거친다** |
| `scripts/pipeline-runner.sh:get_db_model_cycle` (L548) | 셸 러너 | 같은 3단 병합의 SQL 복제본 + `normalize_runner_model` |
| `services/code_reviewer.py` (L372) | 코드 리뷰 | `AI_REVIEW` 만, **mrp 를 일부러 안 붙인다**(주석). CLI 모델만 통과 |
| `api/pipeline_runner.py` `PUT /settings/runner-models` (L3533) | 관리 화면 | 쓰기. `updated_by='CEO'` 고정 |

### 1-4. `llm_fallback_chains`

| 읽는 코드 | 시점 | 충돌 시 |
|---|---|---|
| `core/llm_fallback_engine.py:get_fallback_steps` (L58) | `route_key` 단위, TTL 있는 캐시 | — |
| `core/llm_fallback_engine.py:get_bg_fallback_models` (L99) | 배경 LLM 폴백 | `background` 체인의 **`is_free` 단계만**. 비면 env |
| `core/anthropic_client.py` L346, L492 | 폴백 루프 | DB 결과 우선, 비면 `_BG_FALLBACK_MODELS_ENV` |
| `api/llm_admin.py` `/chains` (L34~) | 관리 API | 쓰기 |

`chat_main`·`runner` 체인은 `get_fallback_steps` 로 읽는 코드를 찾지 못했다 [추정]. 행은 있으나 소비자가 없을 수 있다.

### 1-5. `chat_model_preferences`

`api/llm_models.py` 한 파일에서만 읽고 쓴다(L560/L589). 채팅 모델 선택창의 표시·고정·즐겨찾기용이며
**어떤 모델이 실행되는지를 정하지 않는다.** 라우팅 정본 후보가 아니다.

### 1-6. `directive_model_config`

| 읽는 코드 | 시점 | 충돌 시 |
|---|---|---|
| `services/directive_draft_service.py:_get_directive_model_config` (L50, TTL 60s) | 지시서 생성 | 행이 없거나 정규화 후 비면 코드 상수 `_DEFAULT_DIRECTIVE_MODELS` (L28) |
| `api/directives.py` (L305/L338) | 관리 화면 | 읽기·쓰기 |

### 1-7. env / 코드 상수

| 위치 | 값 | 비고 |
|---|---|---|
| `core/anthropic_client.py` L59 `_BG_PRIMARY_MODEL` | env `LLM_BG_PRIMARY_MODEL`, 기본 `claude-haiku-4-5-20251001` | **배경 1순위는 DB 를 읽지 않는다.** 모듈 import 시 한 번 고정 → 바꾸려면 reload |
| `core/anthropic_client.py` L45 `_BG_FALLBACK_MODELS_ENV` | env `LLM_BG_FALLBACK_MODELS`, 기본 `groq-llama-70b,groq-gpt-oss-120b,qwen-flash` | DB 가 비었을 때만 |
| `core/llm_fallback_engine.py` L107 | env 같은 이름, 기본 `groq-llama-70b,groq-gpt-oss-120b` | **기본값이 위와 다르다** (qwen-flash 없음) |
| `services/loop_executor.py` L44 `_LOOP_FALLBACK_MODELS` | env 같은 이름, 기본 위와 같음 | **DB 를 아예 안 본다** |
| `services/model_selector.py` L500~ `_INTENT_POLICY_MODEL_ALIASES / _CLAUDE_RANK / _RANK_MODEL` | 등급 매핑 | §2-D |
| `services/model_selector.py` L531~ `_HAIKU_FALLBACK_INTENTS`, `_SONNET_INTENTS` | 하향 대상 intent | DB 에 intent 가 없을 때 |
| `services/model_selector.py` L773, `chat_service.py` L27, `turn_model_contract.py` L24 | `_AUTO_ROUTED_DB_DEFAULT_MODELS = {"auto-default-llm","qwen-turbo"}` | **세 곳에 복제.** `qwen-turbo` 는 모델이 아니라 "자동 선택" 표식이다 — 지우면 안 된다(§2-F) |
| `services/chat_service.py` L7231 `_FALLBACK_CHAIN_429` | 모델별 교차 폴백 맵 | 429 재시도 때. 구형 id(`claude-opus-4-6`, `claude-opus-5`, `claude-sonnet-5`)가 키로 남아 있음 |
| `services/pipeline_runner_service.py` L65 `_CLAUDE_MODEL_BY_SIZE` | 전 size `claude-sonnet-5-5` | DB 가 비었거나 `worker_model=claude` 명시 시 |
| `api/pipeline_runner.py` `_get_model_for_size` (L652) | 하드코드 `claude-sonnet-5-5` | DB 조회 실패 시 |
| `services/directive_draft_service.py` L28 | `["claude-sonnet-5-5","codex:gpt-5.6-terra"]` | §1-6 |
| `services/model_selector.py` L1247 `_CODEX_MODELS` | 허용 codex 모델 집합 | 폴백 선택 검증 |
| `services/model_selector.py` L1065 `_COST_MAP` | 모델별 단가 | 라우팅 아님. 단가 표 |

---

## 2. 같은 개념을 여러 곳이 다르게 정의하는 사례

### 2-A. "배경(background) LLM 1순위" — 정의가 5곳

| 정의 위치 | 값 | 실제로 쓰이나 |
|---|---|---|
| env `LLM_BG_PRIMARY_MODEL` (`anthropic_client.py` L59) | `claude-haiku-4-5-20251001` (기본) | **쓰인다. 실제 1순위를 정하는 유일한 값** |
| `llm_fallback_chains` `background` step 1 | `claude-haiku-4-5-20251001` (oauth_direct) [실측] | 1순위로는 안 읽는다. `is_free` 단계만 폴백으로 읽음 |
| mrp `background_llm` default | **`qwen-turbo`** [실측] | 읽는 코드 없음. DashScope 계정 연체로 전량 실패한 모델 |
| mrp `background_llm` 2번째 | `claude-haiku-4-5-20251001` (default 아님) | 읽는 코드 없음 |
| `intent_policies` | 해당 개념 없음 | — |

설정 화면은 "배경 1순위 = qwen-turbo" 를 보여 주지만 실제 호출은 Haiku 다. 화면 값이 거짓이다.

### 2-B. "배경 폴백 목록" — 4곳, 서로 다름

| 위치 | 기본/실측 값 | 읽는 쪽 |
|---|---|---|
| `llm_fallback_chains` background (`is_free` ∧ `is_enabled`) [실측] | `groq-gpt-oss-120b`, `pc-qwen38-27b` (`groq-llama-70b` 는 `is_enabled=false`) | `get_bg_fallback_models` → `anthropic_client` |
| env 기본 (`anthropic_client.py` L45) | `groq-llama-70b, groq-gpt-oss-120b, qwen-flash` | DB 가 비었을 때 |
| env 기본 (`llm_fallback_engine.py` L107) | `groq-llama-70b, groq-gpt-oss-120b` | 같은 조건, 다른 기본값 |
| env 기본 (`loop_executor.py` L44) | `groq-llama-70b, groq-gpt-oss-120b, qwen-flash` | DB 를 **전혀** 안 봄 |

운영 DB 에서 `groq-llama-70b` 를 껐는데 루프 실행기(`loop_executor`)는 여전히 이것을 시도한다.
`qwen-flash` 는 DashScope 가 꺼져 있어(`AADS_DASHSCOPE_ENABLED=0` 기본) 헛 왕복만 만든다.

### 2-C. "러너 모델 체인" — 3단 병합이 3곳에 복제

`runner_model_config[size]` → `runner_model_config[AI_REVIEW]` → mrp `runner_llm` 순서로 병합하는 로직이
`api/pipeline_runner.py` (Python), `services/pipeline_runner_service.py` (Python, SQL 한 방),
`scripts/pipeline-runner.sh` (셸 + SQL) 에 각각 있다. 차이:

| 항목 | `pipeline_runner.py` | `pipeline_runner_service.py` | `pipeline-runner.sh` |
|---|---|---|---|
| 실행가능 필터 | `filter_executable_models` | 없음 (codex 가드만) | 없음 (codex 가드 SQL) |
| codex 가드 대상 | 없음 | provider=openai | provider ∈ {codex, openai} |
| 중복 제거 | 첫 등장 | 첫 등장 | 최소 rank |

**한 모델이 `codex:` 접두사 유무로 다르게 취급될 수 있고, 같은 입력에서 세 경로가 다른 체인을 만들 수 있다** [추정 — 코드 비교에 근거, 실행 비교는 안 함].
추가로 `code_reviewer.py` 는 AI_REVIEW 만 쓰고 mrp 꼬리를 의도적으로 뺀다 — 이 차이는 의도다.

러너 모델의 구형 잔존 [실측]: `runner_model_config.XS = [claude-sonnet-5-5, codex:gpt-6-luna, codex:gpt-5.6-luna, codex:gpt-6.1-sol]`.
`codex:gpt-5.6-luna` 는 같은 계열의 `codex:gpt-6-luna` 와 한 배열에 공존한다.
mrp `runner_llm` 에는 시드가 `claude-opus-5`, `gpt-5.6-*`, `gpt-5.5` 를 넣어 두었다(`llm_models.py` L229~234).

### 2-D. "intent 별 모델" — 3곳, 의미도 다름

| 위치 | 의미 | 값 [실측] |
|---|---|---|
| `intent_policies` (17 intent) | **상한 정책**(허용 등급 아래로 강등) | default 가 `claude-sonnet-5`(9건), `claude-opus-5`(2건), `codex:gpt-5.5`(3건), `claude-haiku-4-5-20251001`(2건), `codex:gpt-5.6-sol`(1건) |
| mrp `intent_*` (23개 route) | 읽는 코드 없음 | `claude-sonnet / claude-haiku / claude-opus / codex gpt-5.6-sol` 별칭 |
| 코드 상수 `_HAIKU_FALLBACK_INTENTS`, `_SONNET_INTENTS` | DB 에 intent 가 없을 때의 상한 | `greeting/casual`→Haiku, 7개 intent→Sonnet |

mrp `intent_*` 와 `intent_policies` 는 **키 어휘부터 다르다**(`intent_dashboard` ↔ `dashboard` 처럼 일부만 겹치고,
`code_modify`·`cto_directive`·`pipeline_runner` 등은 한쪽에만 있다). "중복"이라기보다 서로 이어지지 않은 두 벌이다.

등급 매핑의 함정: `_INTENT_POLICY_MODEL_ALIASES` 에서 `claude-sonnet-5` 와 `claude-sonnet-5-5` 는 둘 다 rank 2 라 바꿔도 동작이 같다.
반면 `claude-opus-5` 는 별칭이 자기 자신(`claude-opus-5`)이고 `_INTENT_POLICY_CLAUDE_RANK` 에 **없다**(등급 없음),
`claude-opus-5-5` 는 `claude-opus`(rank 3)로 간다. 따라서 허용 목록의 `claude-opus-5` 를 `claude-opus-5-5` 로 바꾸면
**Opus 5.5 가 허용 목록에 처음 들어가 강등되지 않게 된다** — 값 정리가 아니라 정책 변경이다. DESIGN §6-B 에서 SQL 초안과 분리했다.

### 2-E. "chat 기본 모델" — 2곳

| 위치 | 값 |
|---|---|
| mrp `llm` default [실측] | `anthropic/claude-opus-5-5` |
| `intent_policies` 상한 + 코드 상수 | intent 별로 이를 다시 강등 (§1-2) |

자동 선택 턴은 mrp `llm` 로 시작해서 `intent_policies` 가 낮춘다. 순서는 일관되지만, 관리자는 두 화면을 모두 봐야 "실제로 어떤 모델이 나가는지" 알 수 있다.

### 2-F. 구형·실패 모델이 남아 있는 곳 [실측]

| 위치 | 값 | 상태 |
|---|---|---|
| mrp `background_llm` default | `qwen/qwen-turbo` | `llm_models`: active. 그러나 DashScope 접근 거부로 실패 이력 (`anthropic_client.py` L35 주석) |
| mrp `agent_architect`, `agent_strategist_analyze`, `agent_supervisor` default | `anthropic/claude-opus-4-6` | `llm_models`: `is_active=false, is_executable=false` |
| `runner_model_config.XS` | `codex:gpt-5.6-luna` | 실행 가능은 하나 `gpt-6-luna` 와 동시 존재 |
| `intent_policies` 17행 전부(default 9행 + 나머지는 허용 목록) | `claude-sonnet-5` | rank 가 같아 동작 동일. 표기만 구세대. `claude-sonnet-5-5` 가 든 행은 0 |
| `intent_policies` 허용 목록 | `claude-opus-5` (8행) + default 2행 | 등급 없음 → §2-D |
| `directive_model_config.generation` | `claude-sonnet-5`, `claude-opus-5` | 목록 안 후보 |
| 코드 `_FALLBACK_CHAIN_429` | 키 `claude-opus-4-6` 등 | 키로만 남음 |
| 코드 `_AUTO_ROUTED_DB_DEFAULT_MODELS` | `qwen-turbo` | **의도된 표식. 정리 대상 아님** |

### 2-G. 같은 테이블을 두 API 가 쓴다

`intent_policies`: `governance.py` (POST upsert, 캐시 무효화 L122) 와 `llm_admin.py` (PATCH, 캐시 무효화 L255) 두 경로가 같은 행을 쓴다.
두 경로 모두 `invalidate_intent_policy_cache()` 를 부르므로 무효화 누락은 없다.
다만 검증 규칙은 다르다 — `governance.py` 는 pydantic validator (intent/default_model/allowed_models) 를 거치고, `llm_admin.py` PATCH 는 값 검증이 없다.
정본 이행 후 쓰기 경로는 하나만 남겨야 한다.

---

## 3. 충돌 해소 요약 — "정말 이기는 것"

| 질문 | 이기는 것 | 근거 |
|---|---|---|
| 사용자가 모델을 직접 골랐다 | 사용자 선택. 정책 강등을 건너뜀 | `model_selector.py` L2276 `_explicit_model_requested` |
| 선택창이 자동/mixture | mrp `llm` default → `intent_policies` 상한 | §2-E |
| 러너 작업 모델 | `runner_model_config[size]` → `AI_REVIEW` → mrp `runner_llm` | §2-C |
| 코드 리뷰 모델 | `runner_model_config.AI_REVIEW` 만 | `code_reviewer.py` L372 |
| 배경 LLM 1순위 | env `LLM_BG_PRIMARY_MODEL` (기본 Haiku) | §2-A |
| 배경 LLM 폴백 | `llm_fallback_chains` 의 무료·활성 단계, 비면 env | §2-B. 단 `loop_executor` 는 env 만 |
| 이미지/임베딩/딥리서치 | mrp 해당 route 의 default → order | §1-1 |
| 지시서 생성 | `directive_model_config`, 비면 코드 상수 | §1-6 |
