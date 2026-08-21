# AADS 5-Layer 프롬프트 거버넌스

_작성·실측: 2026-08-21 | 정본 근거: `app/services/prompt_compiler.py`, `app/services/chat_service.py`, `app/routers/chat.py`, 운영 PostgreSQL_

## 1. 개요

AADS의 최종 system prompt는 먼저 `base_system_prompt`를 만들고, `PromptCompiler`가 활성화된 `prompt_assets` 중 네 scope 조건을 모두 통과한 행을 L1부터 L5까지 순서대로 덧붙이는 구조다.

| 레이어 | 코드상 이름 | 책임 | 주 매칭 scope |
|---|---|---|---|
| L1 | `global` | 전역 운영 원칙·응답 계약 | 보통 전체(`*` 또는 무제한); 실제로는 네 scope를 모두 검사 |
| L2 | `project` | 프로젝트/워크스페이스 문맥과 경계 | `workspace_scope` |
| L3 | `role` | 세션에 지정된 역할과 프로젝트별 역할 overlay | `role_scope`, 필요 시 `workspace_scope` |
| L4 | `intent` | 분류된 작업 의도별 실행·출력 계약 | `intent_scope`, 필요 시 `workspace_scope` |
| L5 | `model` | 실행 모델/provider/family/capability별 지침 | `target_models` |

레이어별로 별도 선택기를 쓰는 것이 아니다. 모든 행에 동일한 네 scope 조건을 적용한 뒤 `layer_id ASC, priority ASC, slug ASC`로 정렬한다. 따라서 L1이 가장 먼저, L5가 가장 나중에 배치된다. 숫자가 작은 priority가 먼저 배치되며, 같은 레이어·priority에서는 slug 오름차순이다.

컴파일러에는 이미 조립된 base prompt를 대체하거나 앞선 문장을 삭제하는 충돌 해소 연산이 없다. 매칭된 비어 있지 않은 asset 본문을 모두 `\n\n`으로 연결한다. “L1 우선”의 코드상 의미는 L1을 먼저 배치하는 것까지이며, 자연어 지시가 충돌할 때 어느 문장이 의미적으로 승리하는지는 컴파일러가 판정하지 않는다.

## 2. `prompt_assets` 운영 스키마

아래는 2026-08-21 운영 DB의 `information_schema.columns` 실측이다. 초기 마이그레이션 파일의 UUID/default와 다른 현재 DB 정의가 있으므로 운영 판정에는 이 표를 사용한다.

| 컬럼 | 운영 타입 / null / default | 용도 |
|---|---|---|
| `id` | `bigint`, NOT NULL, sequence | PK |
| `slug` | `varchar`, NOT NULL | asset 고유 키; UNIQUE |
| `title` | `varchar`, nullable | 관리용 제목 |
| `layer_id` | `integer`, NOT NULL, `1` | 1~5 레이어 번호. 운영 DB에는 layer 범위 CHECK가 없음 |
| `content` | `text`, NOT NULL, `''` | 기본 본문 |
| `model_variants` | `jsonb`, NOT NULL, `{}` | model match key별 대체 본문 |
| `workspace_scope` | `text[]`, nullable | workspace/project 키 필터 |
| `intent_scope` | `text[]`, nullable | intent 키 필터 |
| `target_models` | `text[]`, nullable | 모델 및 파생 match key 필터 |
| `priority` | `integer`, NOT NULL, `100` | 작은 값부터 조립 |
| `enabled` | `boolean`, NOT NULL, `true` | `TRUE`인 행만 후보 |
| `created_by` | `varchar`, nullable | 생성 주체 |
| `created_at` | `timestamptz`, NOT NULL, `now()` | 생성 시각 |
| `updated_at` | `timestamptz`, NOT NULL, `now()` | 수정 시각 |
| `role_scope` | `text[]`, nullable, `{'*'}` | role key 필터 |

운영 인덱스는 PK, slug UNIQUE, `(layer_id, priority, slug)`이며 scope 컬럼 인덱스는 없다.

### scope 매칭 규칙

활성 행 하나가 적용되려면 다음 네 조건을 **모두** 통과해야 한다.

```sql
(workspace_scope IS NULL OR array_length(workspace_scope, 1) IS NULL
 OR workspace = ANY(workspace_scope) OR '*' = ANY(workspace_scope))
AND
(intent_scope IS NULL OR array_length(intent_scope, 1) IS NULL
 OR intent = ANY(intent_scope) OR '*' = ANY(intent_scope))
AND
(target_models IS NULL OR array_length(target_models, 1) IS NULL
 OR target_models && model_match_keys OR '*' = ANY(target_models))
AND
(role_scope IS NULL OR array_length(role_scope, 1) IS NULL
 OR role = ANY(role_scope) OR '*' = ANY(role_scope))
```

- `NULL`, 빈 배열(`{}`), `*` 포함은 해당 축을 제한하지 않는다.
- workspace, intent, role 비교는 PostgreSQL 배열의 정확한 문자열 비교다. 컴파일러 내부에서 대소문자 정규화를 하지 않는다.
- `target_models`는 단일 값 비교가 아니라 산출된 `model_match_keys` 배열과 overlap(`&&`)을 검사한다.
- wildcard 행과 구체 scope 행이 함께 일치하면 둘 다 적용된다. 구체 scope가 wildcard를 대체하지 않는다.
- `model_variants`는 확장된 model key 순서대로 정확 key 또는 소문자 key를 찾고, 첫 truthy variant를 쓴다. dict이면 `content`, 문자열이면 그 문자열을 사용하며, 없으면 기본 `content`를 쓴다.
- 선택된 최종 본문이 빈 문자열이면 조립 및 `applied_assets` 기록에서 제외된다.

## 3. `PromptCompiler` 흐름

### 입력과 정규화

`compile()`의 입력은 `workspace_name`, `intent`, `model`, `session_id`, 선택 인자인 `role`, `selected_model_id`, `execution_model_id`, `model_match_keys`, `base_system_prompt`다. 호출부의 `role` 인자가 서비스/DB의 `role_key`에 해당한다.

1. workspace가 비면 `CEO`, intent/model/role은 trim한 문자열로 만든다.
2. base prompt가 비면 `build_layer1(workspace, "", intent=...)`와 `build_layer4()`를 연결한다. 채팅 주 경로는 `context_builder.build_messages_context(..., apply_prompt_assets=False)`가 만든 system prompt와 응답 모드 등의 블록을 `base_system_prompt`로 전달한다.
3. `model`, selected model, execution model, 호출자가 준 key를 중복 없이 모은다.
4. 모델 문자열에서 provider/family/capability/performance/cost 등의 key를 추론한다. `llm_models` 테이블이 있으면 registry의 provider, family, category, capability, cost tier key도 추가한다.
5. feature flag `governance_enabled`가 false이면 asset/blueprint를 조회하지 않고 base prompt의 hash/길이만 계산해 반환한다.
6. 활성 asset을 네 scope로 필터하고 `layer_id`, `priority`, `slug` 오름차순으로 조회한다.
7. 각 행의 model variant 또는 content를 선택해 base prompt 뒤에 모두 연결한다.
8. 별도로 활성 `session_blueprints` 중 workspace/intent가 맞는 첫 행(`priority`, `slug` 오름차순)을 provenance의 `blueprint`에 기록한다. 현재 `PromptCompiler`는 blueprint 필드를 system prompt에 직접 조립하지 않는다.
9. 최종 SHA-256과 문자 수를 계산해 `CompiledPrompt(system_prompt, provenance)`를 반환한다.

DB 조회/조립 try 블록에서 예외가 나면 예외 문자열을 `provenance.compile_error`에 넣고, 그 시점까지의 prompt로 hash/길이를 계산해 반환한다.

### 채팅 전달 경로

1. `POST /chat/sessions`의 `SessionCreate.role_key`가 `chat_service.create_session()`으로 전달된다.
2. 요청 role이 비면 workspace `settings.default_role_key`를 사용한다. customer tenant이면 무조건 `GeneralAssistant`로 바꾼다.
3. `PUT /chat/sessions/{id}`도 `SessionUpdate.role_key`를 `chat_sessions.role_key`에 저장할 수 있다.
4. `POST /chat/messages/send` 자체에는 role/workspace 필드가 없다. router는 session id와 메시지/model 등을 `send_message_stream()`에 넘긴다.
5. 서비스가 session과 workspace를 JOIN해 `w.name`, `w.system_prompt`, `w.settings`, `s.role_key`, `s.settings`를 읽는다.
6. role은 `chat_sessions.role_key` → `chat_sessions.settings.role_key` → `workspace.settings.default_role_key` 순으로 보완된다.
7. workspace 이름은 알려진 project key 포함 여부로 정규화하고, 메시지에서 프로젝트 mention을 찾았으면 첫 mention project가 이를 덮는다. 이 값이 compiler의 `workspace_name` 즉 workspace scope 비교값이다.
8. intent는 분류 결과/override를 사용하되 직접 실행 조건에 따라 `pipeline_runner` 등으로 변경된 뒤 compiler에 전달될 수 있다.
9. selected model은 명시적 override, execution model은 intent router 결과이며 둘을 compiler에 전달한다.
10. 일반 LLM 경로는 컴파일 직후 별도 connection으로 provenance를 INSERT한다. 직접 Agent SDK 경로도 동일 compiler로 조립하지만, 성공하여 조기 return하는 경로에는 `record_prompt_provenance()` 호출이 없다.

## 4. `compiled_prompt_provenance`

### 운영 스키마

| 컬럼 | 운영 타입 / null / default | 의미 |
|---|---|---|
| `id` | `bigint`, NOT NULL, sequence | PK |
| `session_id` | `uuid`, NOT NULL | 채팅 세션 |
| `execution_id` | `uuid`, nullable | turn execution |
| `intent` | `varchar`, nullable | 컴파일 시 intent |
| `model` | `varchar`, nullable | 컴파일 대표 model |
| `system_prompt_hash` | `char`, NOT NULL | 최종 prompt SHA-256 |
| `system_prompt_chars` | `integer`, NOT NULL, `0` | 최종 prompt 문자 수 |
| `provenance` | `jsonb`, NOT NULL, `{}` | 입력, 적용 asset, 오류 등 상세 |
| `created_at` | `timestamptz`, NOT NULL, `now()` | 기록 시각 |

인덱스는 PK와 `(session_id, created_at DESC)`다. `session_id`/`execution_id`에 대한 운영 DB foreign key는 없다.

`provenance`의 코드 생성 필드는 `workspace`, `intent`, `model`, `selected_model_id`, `execution_model_id`, `model_match_keys`, `role`, `session_id`, `governance_enabled`, `base_prompt_chars`, `applied_assets`, `layers_applied`, `blueprint`, `fallback_used`, 선택적 `compile_error`, `system_prompt_hash`, `system_prompt_chars`다. 채팅 주 경로는 기록 전에 `context_policy`도 추가한다.

`applied_assets` 원소는 `slug`, `layer_id`, `layer_name`, `priority`, `chars`를 가진다. `layers_applied`는 layer name별 적용 개수다. `fallback_used`는 적용 asset과 blueprint가 모두 없을 때 true다.

### 적용 판정 방법

특정 turn에 asset이 적용됐다는 최종 판정은 해당 execution(없으면 session의 최신 시각)의 provenance 행에서 한다.

```sql
SELECT execution_id, intent, model, system_prompt_chars,
       provenance->>'workspace' AS workspace,
       provenance->>'role' AS role,
       provenance->'applied_assets' AS applied_assets,
       provenance->>'compile_error' AS compile_error,
       created_at
FROM compiled_prompt_provenance
WHERE session_id = $1
ORDER BY created_at DESC
LIMIT 1;
```

- slug가 `applied_assets`에 있으면 그 asset은 실제 조립되었다.
- L3 적용 여부는 `applied_assets[*].layer_id = 3`으로 판정한다. `layers_applied.role`은 보조 집계다.
- `system_prompt_chars > 0`만으로 특정 asset이나 특정 layer 적용을 증명할 수 없다. base prompt도 길이에 포함된다.
- `compile_error`가 있으면 정상 적용으로 단정하지 말고 오류 발생 시점과 `applied_assets`를 함께 본다.
- provenance 행 자체가 없으면 미적용이라고 단정할 수 없다. provenance table 부재 시 recorder는 조용히 return하고, INSERT 실패는 로그만 남기며, 성공한 직접 Agent SDK 조기-return 경로도 provenance를 기록하지 않는다.
- `governance_enabled=false`이면 `applied_assets=[]`, `layers_applied={}`인 base prompt 결과가 정상이다.
- 모델의 자기소개, workspace 고정 문구, 이전 메시지 본문은 적용 판정 근거가 아니다.

## 5. 레이어 및 충돌 해결 규칙

1. base prompt가 항상 asset 앞에 온다.
2. asset은 L1 → L2 → L3 → L4 → L5 순서로 온다.
3. 같은 레이어는 작은 priority → 큰 priority 순서다.
4. 같은 레이어·priority는 slug 오름차순이다.
5. scope 특이도에 따른 승자 선택이나 override는 없다. 일치하는 global/base/overlay 행이 모두 누적된다.
6. 같은 slug는 DB UNIQUE이므로 중복될 수 없다.
7. 의미 충돌을 자동 검출하거나 L1 문장을 다시 뒤에 배치하는 코드는 없다. 따라서 권위 규칙을 강제하려면 asset 본문 자체에 명시하고 provenance로 실제 배치를 검증해야 한다.

## 6. `role_key` 누락 시 L3 미적용 진단

1. 대상 turn의 `session_id`와 가능하면 `execution_id`를 확정한다.
2. `chat_sessions.role_key`, `chat_sessions.settings->>'role_key'`, 연결 workspace의 `settings->>'default_role_key'`를 확인한다. customer tenant인지도 확인한다.
3. 최신 provenance의 `provenance.role`을 확인한다. 이것이 compiler가 실제 받은 role 값이다.
4. provenance의 `workspace`, `intent`, `model_match_keys`, `governance_enabled`, `compile_error`를 확인한다.
5. 기대 L3 asset이 `enabled=TRUE`인지 확인하고, 네 scope 모두를 실제 provenance 입력값과 대조한다. role만 맞아도 workspace/intent/model scope 중 하나가 실패하면 미적용이다.
6. 기대 slug가 `applied_assets`에 있는지 확인한다. L3 전체 누락은 layer_id 3 원소가 없는지 확인한다.
7. role이 빈 문자열이면 `role_scope={'*'}`, `NULL`, 빈 배열인 L3는 여전히 매칭될 수 있다. 따라서 “role 누락 = 모든 L3 누락”으로 판정하면 안 된다. 특정 역할 scope asset만 빠지는지 slug 단위로 본다.
8. DB role은 있는데 provenance role이 비었다면 해당 실행의 세션 조회/보완 시점과 로그의 `[PROMPT_COMPILER] enter`를 확인한다. role fallback 순서는 session column → session settings → workspace default다.
9. provenance가 없으면 recorder 실패 로그와 직접 Agent SDK 성공 경로 여부를 먼저 확인한다. 응답 문구만으로 역판정하지 않는다.

진단용 조회 예시는 다음과 같다.

```sql
SELECT s.id, s.role_key,
       s.settings->>'role_key' AS settings_role_key,
       w.name, w.settings->>'default_role_key' AS default_role_key
FROM chat_sessions s
JOIN chat_workspaces w ON w.id = s.workspace_id
WHERE s.id = $1;

SELECT slug, layer_id, workspace_scope, intent_scope,
       target_models, role_scope, priority, enabled
FROM prompt_assets
WHERE layer_id = 3
ORDER BY priority, slug;
```

## 7. 실측 데이터: 현재 `prompt_assets` 전체 목록

실측 쿼리: `SELECT slug, layer_id, enabled, priority FROM prompt_assets ORDER BY layer_id, priority, slug`. 총 137행이며 활성 136행, 비활성 1행이다.

| slug | layer | enabled | priority |
|---|---:|:---:|---:|
| global-chat-completion-contract | 1 | true | 6 |
| global-core-directives | 1 | true | 10 |
| global-direct-work-dependency-gate | 1 | true | 15 |
| global-response-quality | 1 | true | 20 |
| global-report-depth-contract | 1 | true | 22 |
| global-cost-control | 1 | true | 30 |
| global-search-strategy | 1 | true | 40 |
| global-layer-governance | 1 | true | 50 |
| project-ceo-orchestration-context | 2 | true | 1 |
| project-aads-context | 2 | true | 10 |
| project-go100-context | 2 | true | 10 |
| project-kis-context | 2 | true | 10 |
| project-nas-context | 2 | true | 10 |
| project-ntv2-context | 2 | true | 10 |
| project-sf-context | 2 | true | 10 |
| project-remote-access-contract | 2 | true | 20 |
| project-aads-pc-device-agent-capability-boundary | 2 | true | 21 |
| role-prompt-context-harness-engineer | 3 | true | 7 |
| role-prompt-engineer | 3 | true | 8 |
| role-ceo-command | 3 | true | 10 |
| role-cto-strategist | 3 | true | 10 |
| role-developer-implementer | 3 | true | 10 |
| role-judge-evaluator | 3 | true | 10 |
| role-kakaobot-handler | 3 | true | 10 |
| role-ops-monitor | 3 | true | 10 |
| role-pm-coordinator | 3 | true | 10 |
| role-qa-verifier | 3 | true | 10 |
| role-data-engineer | 3 | true | 11 |
| role-sre-reliability | 3 | true | 11 |
| role-ai-image-generation-admin | 3 | true | 12 |
| role-real-trading-expert | 3 | true | 12 |
| role-risk-compliance | 3 | true | 12 |
| role-security-privacy | 3 | true | 12 |
| role-vibe-coding-lead | 3 | true | 12 |
| role-brand-marketing-lead | 3 | true | 13 |
| role-customer-success-lead | 3 | true | 13 |
| role-finance-fundraising-lead | 3 | true | 13 |
| role-gtm-strategist | 3 | true | 13 |
| role-legal-ip-advisor | 3 | true | 13 |
| role-pricing-monetization-strategist | 3 | true | 13 |
| role-research-analyst | 3 | true | 13 |
| role-revenue-operations-analyst | 3 | true | 13 |
| role-sales-partnership-lead | 3 | true | 13 |
| role-ux-product-designer | 3 | true | 13 |
| role-ai-ml-engineer | 3 | true | 14 |
| role-growth-content | 3 | true | 14 |
| project-role-aads-prompt-context-harness | 3 | true | 20 |
| project-role-ceo-prompt-context-harness | 3 | true | 20 |
| project-role-aads-cto | 3 | true | 21 |
| project-role-aads-ops | 3 | true | 21 |
| project-role-aads-pm | 3 | true | 21 |
| project-role-aads-sre | 3 | true | 21 |
| project-role-go100-cto | 3 | true | 21 |
| project-role-go100-ops | 3 | true | 21 |
| project-role-go100-pm | 3 | true | 21 |
| project-role-go100-research | 3 | true | 21 |
| project-role-kis-ops | 3 | true | 21 |
| project-role-kis-pm | 3 | true | 21 |
| project-role-nas-ops | 3 | true | 21 |
| project-role-nas-pm | 3 | true | 21 |
| project-role-ntv2-cto | 3 | true | 21 |
| project-role-ntv2-ops | 3 | true | 21 |
| project-role-ntv2-pm | 3 | true | 21 |
| project-role-sf-ops | 3 | true | 21 |
| project-role-sf-pm | 3 | true | 21 |
| project-role-aads-ai-image-admin | 3 | true | 22 |
| project-role-aads-security | 3 | true | 22 |
| project-role-aads-ux | 3 | true | 22 |
| project-role-aads-vibe-coding-lead | 3 | true | 22 |
| project-role-ceo-trading-expert | 3 | true | 22 |
| project-role-ceo-vibe-coding-lead | 3 | true | 22 |
| project-role-go100-data | 3 | true | 22 |
| project-role-go100-risk | 3 | true | 22 |
| project-role-go100-trading-expert | 3 | true | 22 |
| project-role-go100-ux | 3 | true | 22 |
| project-role-go100-vibe-coding-lead | 3 | true | 22 |
| project-role-kis-trading-expert | 3 | true | 22 |
| project-role-kis-ux | 3 | true | 22 |
| project-role-kis-vibe-coding-lead | 3 | true | 22 |
| project-role-nas-ux | 3 | true | 22 |
| project-role-nas-vibe-coding-lead | 3 | true | 22 |
| project-role-ntv2-ux | 3 | true | 22 |
| project-role-ntv2-vibe-coding-lead | 3 | true | 22 |
| project-role-sf-ai-image-admin | 3 | true | 22 |
| project-role-sf-ux | 3 | true | 22 |
| project-role-sf-vibe-coding-lead | 3 | true | 22 |
| project-role-aads-ai-ml | 3 | true | 23 |
| project-role-go100-ai-ml | 3 | true | 23 |
| project-role-ntv2-growth | 3 | true | 23 |
| project-role-ntv2-security | 3 | true | 23 |
| project-role-aads-developer | 3 | true | 24 |
| project-role-go100-developer | 3 | true | 24 |
| project-role-go100-ux-growth | 3 | true | 24 |
| project-role-kis-developer | 3 | true | 24 |
| project-role-nas-developer | 3 | true | 24 |
| project-role-ntv2-data | 3 | true | 24 |
| project-role-ntv2-developer | 3 | true | 24 |
| project-role-ntv2-sre | 3 | true | 24 |
| project-role-sf-developer | 3 | true | 24 |
| project-role-aads-qa | 3 | true | 25 |
| project-role-go100-qa | 3 | true | 25 |
| project-role-kis-qa | 3 | true | 25 |
| project-role-nas-qa | 3 | true | 25 |
| project-role-ntv2-qa | 3 | true | 25 |
| project-role-ntv2-qa-judge | 3 | false | 25 |
| project-role-sf-qa | 3 | true | 25 |
| project-role-aads-judge | 3 | true | 26 |
| project-role-go100-judge | 3 | true | 26 |
| project-role-kis-judge | 3 | true | 26 |
| project-role-nas-judge | 3 | true | 26 |
| project-role-ntv2-judge | 3 | true | 26 |
| project-role-sf-judge | 3 | true | 26 |
| intent-remote-code-db-preflight | 4 | true | 5 |
| intent-code-modify | 4 | true | 10 |
| intent-cto-strategy | 4 | true | 10 |
| intent-deep-research | 4 | true | 10 |
| intent-status-check | 4 | true | 10 |
| intent-status-report-output | 4 | true | 12 |
| intent-search-research-output | 4 | true | 13 |
| intent-code-deploy-output | 4 | true | 14 |
| intent-report-analysis-output | 4 | true | 15 |
| intent-analysis-output | 4 | true | 24 |
| intent-report-output | 4 | true | 24 |
| model-provider-codex | 5 | true | 9 |
| model-claude-haiku | 5 | true | 10 |
| model-claude-opus | 5 | true | 10 |
| model-claude-sonnet | 5 | true | 10 |
| model-provider-anthropic | 5 | true | 10 |
| model-provider-openai | 5 | true | 10 |
| model-provider-gemini | 5 | true | 12 |
| model-provider-qwen | 5 | true | 14 |
| model-provider-groq | 5 | true | 16 |
| model-provider-deepseek | 5 | true | 18 |
| model-provider-kimi | 5 | true | 18 |
| model-provider-minimax | 5 | true | 20 |
| model-capability-thinking | 5 | true | 24 |
| model-capability-vision | 5 | true | 24 |
