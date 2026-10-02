# 모델 라우팅 정본 단일화 설계 (DESIGN)

TASK_ID: AADS-ROUTING-SSOT-DESIGN-20261002 · 2026-10-02 · **설계만. 코드·DB·env 를 바꾸지 않았다.**
읽는 순서: `READ-MAP.md`(현황) → 이 문서(정본안·이행) → `migrations/drafts/*_draft`(정리 SQL 초안).

---

## 1. 결론

**권장안 A′ — CEO 승인안(`intent_policies` + `runner_model_config`)을 정본으로 하되,
`model_routing_preferences`(mrp)를 "능력 라우트" 전용 정본으로 범위를 한정해 남긴다.**

| 개념 | 정본 | 정본에서 빠지는 것 |
|---|---|---|
| intent → 모델 상한 정책 | `intent_policies` | mrp `intent_*` 23개 route, 코드 상수 `_HAIKU_FALLBACK_INTENTS`·`_SONNET_INTENTS` |
| 러너 size별 체인 | `runner_model_config` | mrp `runner_llm` 꼬리 병합(3곳 복제) |
| 능력 라우트(`llm` 기본·후보, 이미지·영상·오디오·임베딩·검색·딥리서치, **배경 1순위**) | mrp | mrp `agent_*` 11개 route, mrp 의 `intent_*` |
| 지시서 생성 모델 | `directive_model_config` (도메인 전용, 그대로) | — |
| 표시 순서·즐겨찾기 | `chat_model_preferences` (라우팅 아님, 그대로) | — |
| 장애 시 실행 백엔드 순서(oauth→cli→codex…) | `llm_fallback_chains` (모델 선택이 아니라 **백엔드 failover** 로 역할 한정) | env 기본값 3벌 |

"2곳" 으로 끝나지 않는다는 점을 CEO 에게 분명히 해야 한다. 문자 그대로의 2곳 안으로는
이미지·임베딩·딥리서치를 읽는 코드(`image_service`, `chat_embedding_service`, `deep_research_service`,
`media_generation_service`)와 `llm` 기본 모델(`_get_default_llm_model_from_db`)이 갈 곳이 없다 — §3 에서 구체적으로 보인다.
A′ 는 "개념당 정본 1곳"이다. 같은 개념을 둘 이상이 정하는 상태(READ-MAP §2)를 없애는 것이 목표다.

**가장 먼저 알아야 할 사실.** mrp 의 `intent_*`(23개 route)·`agent_*`(11개 route)·`background_llm` 은
**읽는 런타임 코드를 찾지 못했다**(READ-MAP §1-1). 설정 화면에 값만 보이고 실제 호출에는 영향이 없다.
그래서 이들을 정리하는 이행 비용은 코드 변경이 아니라 "정말 아무도 안 읽는지"의 확인이다.
이 확인이 이 설계의 가장 큰 가정이며, §5 이행 0단계에서 검증한다.

---

## 2. 후보 비교

비교한 안 세 가지.

- **A** — CEO 승인안 그대로: `intent_policies` + `runner_model_config` 2곳.
- **B** — mrp 를 유일한 정본으로: 나머지 저장소를 mrp 행으로 흡수.
- **A′** — A 에 mrp 범위 한정을 더한 권장안.

### 2-1. 이전 비용 (옮겨야 하는 데이터와 바꿔야 하는 읽기 코드)

| | A | B | A′ |
|---|---|---|---|
| 옮길 데이터 | mrp 의 media·`llm`·embedding 등을 갈 곳 없음 → **옮길 수 없음** | `runner_model_config` 6행(JSONB 배열) → 행으로 풀기, `intent_policies` 17행 → 컬럼이 안 맞음, `directive_model_config` 1행 | 없음(비활성화만: intent_* 23행, agent_* 40행, background_llm 2행) |
| 바꿀 읽기 코드 | 배경 1순위 + media 경로 중 일부 | **러너 3곳**(`pipeline_runner.py`, `pipeline_runner_service.py`, `pipeline-runner.sh` 의 SQL), `code_reviewer.py`, `model_selector` 의 intent 정책 로더, `governance.py`, `llm_admin.py`, `directive_draft_service.py`, `directives.py` | 배경 1순위 읽기 1곳 신설 + 폴백 목록 helper 통합. 러너 3벌 병합은 SQL 뷰/함수로 하나로 |
| 스키마 변경 | 없음 | **필수.** `intent_policies` 는 `allowed_models[]`, `cascade_downgrade`, `tool_allowlist[]`, `temperature` 가 있어 mrp(행당 model 하나)에 안 들어간다. 컬럼 추가 또는 JSONB 칸 신설이 필요 | 없음 |
| 위험 크기 | 낮음 | **높음** — mrp 는 읽는 파일이 가장 많은 테이블이라 한 번의 오염이 채팅·러너·미디어를 동시에 흔든다 | 낮음 |

B 를 고르면 `intent_policies` 의 의미가 흐려지는 문제가 있다. 이 테이블은 "intent 별 모델"이 아니라
"허용 등급 상한 + 강등 여부"다(READ-MAP §1-2 [중요]). mrp 의 `(route_key, provider, model_id)` 행 모델은
"후보 목록"이라서 상한 정책을 담으려면 의미를 뜯어고쳐야 한다.

### 2-2. 호환성

| | A | B | A′ |
|---|---|---|---|
| `intent_policies_db_primary=true` 로 이미 돌고 있는 경로 | 유지 | 폐기 후 재구현 | 유지 |
| 셸 러너(`pipeline-runner.sh`)가 SQL 로 직접 읽음 | 유지 | **셸 SQL 을 다시 써야 함.** 배포 없이 외부 러너 호스트에 퍼진 스크립트가 구 스키마를 읽으면 즉시 깨진다 | 유지(병합 뷰를 도입하면 셸 SQL 은 뷰 하나를 읽도록 단순화) |
| 대시보드 API 계약 (`/settings/runner-models`, `/governance/intent-policies`, `/llm-models/routing-preferences`) | 유지 | 앞의 둘을 mrp 어댑터로 재구현해야 응답 모양을 지킬 수 있음 | 유지 |
| 읽는 코드가 이미 있는 route 의 모양 | 해당 없음 | 유지 | 유지 |

### 2-3. 관리 화면 영향

| | A | B | A′ |
|---|---|---|---|
| 설정 > 러너 모델 패널 | 변경 없음 | 저장 포맷이 바뀜 → 패널 재작성 | 변경 없음 |
| 설정 > ModelSettingsPanel (mrp + intent 정책을 한 패널에서 읽음) | 변경 없음 | intent 정책 영역 재작성 | `intent_*`·`agent_*` 그룹 숨김 한 곳 |
| 운영자가 "어디를 고쳐야 하나" | 개념마다 한 곳 | 한 곳 | **개념마다 한 곳.** 화면의 가짜 값(배경 1순위=qwen-turbo)이 사라짐 |

### 2-4. 롤백 용이성

| | A | B | A′ |
|---|---|---|---|
| 단계별 되돌리기 | 쉬움 | 어려움 — 데이터 이동 후 되돌리려면 역변환 필요 | **쉬움** — 단계마다 `UPDATE` 한 번. 삭제 없음 |
| 부분 적용 | 가능 | 거의 불가(스키마·API·셸이 함께 움직여야 함) | 가능 |

### 2-5. 총평

| | 이전 비용 | 호환성 | 관리 화면 | 롤백 | 결과 |
|---|---|---|---|---|---|
| A | 낮음 | 높음 | 좋음 | 쉬움 | **범위 부족** — 능력 라우트·배경 LLM 의 집이 없다 |
| B | **높음** | 낮음 | 나쁨 | 어려움 | 비권장 |
| A′ | 낮음 | 높음 | 좋음 | 쉬움 | **권장** |

---

## 3. 왜 mrp 를 정본으로 삼지 않는가 (B 비권장 사유)

사용자 지시가 "mrp 는 읽는 코드가 가장 많으니 정본으로 삼는 안과 반드시 비교하라" 였다. 그 논거는 타당하다 —
mrp 는 읽는 파일이 8개 넘고(READ-MAP §1-1) 미디어·에이전트 route 까지 가졌다. 그럼에도 B 를 권하지 않는 이유는 아래와 같다.

1. **읽는 파일이 많다는 사실이 "정본 자격"을 뜻하지 않는다.** 많이 읽히는 부분은 `llm`, `runner_llm`, `image`, `embedding`, `deep_research` 5개 route 뿐이다.
   mrp 의 52개 route 중 35개(`intent_*` 23, `agent_*` 11, `background_llm` 1)는 읽는 코드가 없다.
   "읽는 코드가 많음"은 `llm` 한 route 의 성질이다.
2. **스키마가 안 맞는 개념이 있다.** `intent_policies`(상한 정책), `runner_model_config`(size 별 JSONB 배열, `codex:` 같은 접두사 포함 spec).
   mrp 에 넣으려면 컬럼을 늘리거나 모델 id 표기를 바꿔야 하고, 후자는 3곳의 러너 파서와 충돌한다.
3. **영향이 큰 테이블에 영향이 큰 변경을 얹는다.** 정본을 바꾸는 이행은 어떻게든 한 번 오염 위험을 지난다. 그 위험을 가장 넓게 읽히는 테이블에서 감수할 이유가 없다.
4. **롤백이 어렵다.** 삭제 없는 단계별 `UPDATE` 로 되돌릴 수 있는 A′ 와 달리, B 는 데이터를 다른 모양으로 바꾼 뒤라 역변환이 필요하다.
5. **mrp 의 강점은 A′ 에서 그대로 쓴다.** 능력 라우트(미디어·임베딩 등)의 정본은 mrp 다. 즉 B 의 "읽는 코드가 많은 영역"은 인정하고, 그 영역에 한정한다.

## 4. 왜 A(문자 그대로 2곳)도 아닌가

A 는 두 가지를 비워 둔다.

- **배경 LLM 1순위.** 현재 env(`LLM_BG_PRIMARY_MODEL`)가 이긴다(READ-MAP §2-A). `intent_policies` 도 `runner_model_config` 도 이 개념을 담지 않는다.
  `call_background_llm` 은 intent 도 러너 size 도 아니다. 억지로 `intent_policies` 에 `background` intent 를 넣는 것은 "상한 정책" 의미를 오염시킨다.
- **능력 라우트.** 이미지 19행·영상 11행·오디오·임베딩 등은 둘 중 어디에도 모양이 맞지 않는다. 그리고 이미 UI·API(`PUT /llm-models/routing-preferences`)·읽기 코드가 갖춰져 있다.

그래서 mrp 를 "없애는" 것이 아니라 "정본인 범위를 좁힌다". 이 점이 CEO 승인안에서 확인이 필요한 유일한 차이다(§8 결정 D0).

---

## 5. 이행 단계

각 단계는 독립적으로 되돌릴 수 있고, 다음 단계는 앞 단계의 확인 뒤에 한다. **어느 단계도 이 작업(설계)에서 실행하지 않았다.**

### 0단계 — 가정 검증 (코드·DB 변경 없음)

mrp 의 `intent_*`·`agent_*`·`background_llm` 을 읽는 런타임이 정말 없는지 확인한다. grep 은 문자열 조합으로 만든 키를 못 잡는다.

- 쿼리 로그/`pg_stat_statements` 에서 `route_key` 파라미터에 이 키가 오는지 일주일 샘플링.
- 대시보드·셸 스크립트·외부 러너 호스트(`scripts/` 외 복사본)에서 `intent_`·`agent_` 접두 쿼리가 있는지.
- 결과가 "읽는 곳 있음"이면 해당 route 는 retired 목록에서 빼고 이 설계를 수정한다.

### 1단계 — 읽기 전용 전환 (코드 변경, DB 데이터 변경 없음)

정본이 아닌 곳이 **더는 쓰기를 받지 않고, 읽혀도 정본과 어긋나지 않게** 한다. 변경 지점은 §7 목록.
요점: retired route 쓰기 거부, 배경 1순위가 mrp 를 읽도록 신설(env 는 비상 override 로 격하), 폴백 목록 helper 통합, 러너 3단 병합 단일화.
이 단계 배포 후 며칠은 실제 호출 모델(`bg_llm_usage_log.model`, `turn_model` 기록)이 이전과 같은지 비교한다.

### 2단계 — 정리 마이그레이션 (DB 데이터 변경, 삭제 없음)

초안: `migrations/drafts/20261002_routing_ssot_cleanup.sql_draft` + `…_rollback.sql_draft`. 상세는 §6.
CEO 승인 뒤 점검 창구에서 수동 적용. 적용 직전 `routing_ssot_backup_20261002` 로 변경 대상 행이 보존되고, 롤백은 이 백업에서 복원한다.

### 3단계 — 구 저장소 폐기 (관찰 기간 뒤)

- 1~2단계 후 2주 동안 retired route 에 대한 읽기가 0 인 것을 확인.
- `fallback_chain_loader.py`(import 0건) 파일 삭제.
- 코드 상수 `_HAIKU_FALLBACK_INTENTS`, `_SONNET_INTENTS`, `_resolve_legacy_intent_cascade_model` 과 `intent_policies_db_primary` shadow 분기 삭제 — 단, DB 에 17개 intent 가 실제 코드가 쓰는 intent 를 모두 덮는지 확인한 뒤.
- mrp retired route 행: 우선 `is_enabled=false` 유지. 물리 삭제는 **별도 승인**이다(이 설계의 SQL 에는 삭제가 없다).
- 백업 테이블 `routing_ssot_backup_20261002` 정리는 마지막에 별도 승인.

---

## 6. 정리 마이그레이션 초안

위치: `migrations/drafts/`. 적용 대상과 제외 대상이 모두 중요하다.

### 6-A. 초안이 하는 것 (값만 바뀌고 동작은 같다)

| # | 대상 | 변경 | 읽는 코드 영향 |
|---|---|---|---|
| 1 | mrp `background_llm` | default 를 `qwen/qwen-turbo`(실패 이력) → `anthropic/claude-haiku-4-5-20251001`. qwen 행은 `is_default=false, is_enabled=false` | **없음**(읽는 코드 없음). 실제 호출은 이미 Haiku |
| 2 | mrp `agent_architect`, `agent_strategist_analyze`, `agent_supervisor` | `claude-opus-4-6` → `claude-opus-5-5` (같은 행의 model_id 교체. 순서·default·enabled 유지) | 없음 |
| 3 | `runner_model_config` (XS) | `codex:gpt-5.6-luna` → `codex:gpt-6-luna`, 중복 제거(첫 등장 유지). XS 결과 `[claude-sonnet-5-5, codex:gpt-6-luna, codex:gpt-6.1-sol]` | **XS 러너 체인이 한 칸 짧아진다.** `gpt-5.6-luna` 는 `llm_models` 상 실행 가능이므로 "실패 모델 제거"가 아니라 "같은 계열 중복 정리"다 |
| 4 | `intent_policies` | `claude-sonnet-5` → `claude-sonnet-5-5` (default·허용 목록, 17행) | **없음.** 두 id 가 모두 rank 2 로 매핑되고, rank 2 의 대표 모델이 이미 `claude-sonnet-5-5` 다(READ-MAP §2-D) |
| 5 | `directive_model_config` (generation) | `claude-sonnet-5`→`claude-sonnet-5-5`, `claude-opus-5`→`claude-opus-5-5` | **지시서 생성에 Opus 5.5 가 쓰인다**(버전 상향. 단가·성능이 다를 수 있음) |

모든 UPDATE 는 "현재 값이 예상과 같을 때만" 조건을 걸었다. 그 사이 CEO 가 대시보드에서 바꾼 값을 덮어쓰지 않는다.
이미 바뀐 상태면 0행 갱신(멱등)이고, 마지막 SELECT 가 남은 구형 id 를 보여 준다.

### 6-B. 초안이 하지 않는 것 (결정이 필요)

| 항목 | 이유 |
|---|---|
| `intent_policies` 의 `claude-opus-5` → `claude-opus-5-5` (허용 목록 8행, default 2행) | **정책 변경이다.** `claude-opus-5` 는 등급 매핑에 없고 `claude-opus-5-5` 는 rank 3 이라, 바꾸면 Opus 5.5 가 허용 목록에 처음 들어가 해당 intent(dashboard, report, code_modify, code_task, diagnosis, deployment, cto_*)에서 **강등되지 않게 된다.** 현재는 Sonnet 으로 낮춘다. 비용 정책이므로 CEO 결정(D1) |
| mrp `llm`·`runner_llm` 의 구형 후보 (`claude-opus-5`, `gpt-5.5` 등) | 후보 목록은 폴백 후보로 읽힌다(`_configured_llm_fallback_candidates`, 러너 꼬리). 바꾸면 폴백 순서가 변한다 |
| 코드 상수 `qwen-turbo` (`_AUTO_ROUTED_DB_DEFAULT_MODELS`) | "자동 선택" 표식이지 모델이 아니다. 바꾸면 자동 라우팅이 깨진다 |
| `chat_service._FALLBACK_CHAIN_429` 의 구형 키 | 코드다. §7 |
| `llm_fallback_chains` | 값은 이미 현행(Haiku 1순위). 정리할 구형 모델이 없다 |

### 6-C. 롤백

`routing_ssot_backup_20261002` 에서 `updated_by='routing_ssot'` 인 행(= 이 초안이 건드린 뒤 아무도 안 고친 행)만 원래 값으로 복원한다.
그 사이 누군가 다시 바꾼 행은 건드리지 않는다. 롤백 초안에도 삭제는 없다.

### 6-D. 파일 이름이 `.sql_draft` 인 이유 (중요)

처음에는 `…_draft.sql` 로 만들려 했으나 **그러면 다음 릴리스에서 자동 적용된다.** 코드로 확인했다.

- `scripts/apply_release_migrations.sh` L142: `find "$ROOT/migrations" -maxdepth 1 -name '*.sql'` 이 모든 미적용 파일을 적용한다. 파일 이름에 `rollback` 이 있어야 제외된다(L120). 파괴적 SQL 게이트(L66~68)는 `DROP TABLE`·`TRUNCATE`·WHERE 없는 `DELETE` 만 막으므로 `UPDATE` 초안은 통과한다.
- `scripts/deploy_release_assets.sh` L37: `[[ "$relative" == migrations/*.sql ]]` — bash 패턴의 `*` 는 `/` 도 맞추므로 **`migrations/drafts/x.sql` 도 선택된다.** 파일명에 `routing`·`model` 이 있으면 `config` 자산으로 분류돼 config 배포에서 적용된다(L43).

그래서 하위 디렉터리만으로는 부족하다. 확장자를 `.sql_draft` 로 해서 두 스크립트의 `*.sql` 패턴에 걸리지 않게 했다.
파일 이름이 `_draft` 로 끝난다는 요구도 그대로 지킨다. 적용하려면 사람이 `.sql` 로 이름을 바꿔 확인 후 실행해야 한다.
`tests/unit/test_routing_ssot_draft.py` 가 "`migrations/` 아래에 `routing_ssot` 를 담은 `*.sql` 이 없음"을 검사해 이 안전장치가 풀리지 않게 한다.

**후속 과제(코드 변경이라 이번엔 안 함):** 두 적용 스크립트가 `*_draft*` 를 명시적으로 제외하게 하면 이름 꼼수에 기대지 않는다.

---

## 7. 정본 외 저장소를 읽기 전용으로 만들 때 바꿀 코드 지점

1단계(§5)에서 바꿀 목록이다. 이번 작업에서는 **변경하지 않았다.**

| # | 파일:함수/위치 | 현재 | 바꿀 내용 |
|---|---|---|---|
| 1 | `app/api/llm_models.py:update_model_routing_preferences` (L768) | 모든 route_key 쓰기 허용 | retired route(`intent_*`, `agent_*`, `background_llm` 정본 이관 후 제외분) 쓰기를 409 로 거부 |
| 2 | `app/api/llm_models.py` 시드 (L225~226 부근 `INSERT ... VALUES` 목록, `ON CONFLICT DO NOTHING`) | `background_llm` 에 `qwen-turbo` default 를 시드 | **새 DB 에서 구형 값이 부활하지 않도록** 시드를 `claude-haiku-4-5-20251001` default 로 교체. 마이그레이션만 고치면 DB 재구성 시 되돌아간다 |
| 3 | `app/services/ai_route_resolver.py` `AI_ROUTE_KEYS`, `ROUTE_GROUPS` (L12, L68) | `intent_*`, `agent_*` 허용 | retired 키 제거(허용 목록과 UI 그룹) |
| 4 | `app/api/llm_models.py:get_model_routing_preferences` (L651) | 전 route 반환 | retired 그룹을 읽기 전용·접힘으로 표시 |
| 5 | `app/core/anthropic_client.py:_BG_PRIMARY_MODEL` (L59), `call_background_llm` (L519~) | import 시 env 로 고정 | mrp `background_llm` default 를 호출 시점(TTL 캐시)에 읽고, env 는 비상 override. 실패 시 현재 `claude-haiku-4-5-20251001` 로 폴백 |
| 6 | `app/core/llm_fallback_engine.py:get_bg_fallback_models` (L99), `app/core/anthropic_client.py` (L45, L346, L492), `app/services/loop_executor.py` (L44, L75) | 기본값 3벌, `loop_executor` 는 DB 무시 | helper 하나로 통합. 기본값 한 벌. `loop_executor` 도 같은 helper 사용 |
| 7 | `app/services/fallback_chain_loader.py` | import 0건. docstring 이 "mrp 단일 소스"라고 주장 | 삭제(3단계) |
| 8 | `app/api/pipeline_runner.py:_get_model_cycle_for_size` (L678), `app/services/pipeline_runner_service.py:_get_db_model_config` (L423), `scripts/pipeline-runner.sh:get_db_model_cycle` (L548) | 같은 3단 병합이 3벌, 필터·가드가 서로 다름 | DB 뷰/함수 `runner_effective_models(size)` 하나로 병합을 옮기고 세 호출처는 그 결과만 읽는다. 가드(codex 실행가능)는 뷰 안에 한 번 |
| 9 | `app/api/llm_admin.py:update_intent_policy` (L231) | 값 검증 없는 PATCH | `governance.py` 의 validator 와 같은 규칙을 쓰거나, 이 경로를 폐기하고 쓰기를 `governance.py` 한 곳으로 |
| 10 | `app/services/model_selector.py` `_HAIKU_FALLBACK_INTENTS`, `_SONNET_INTENTS` (L531~532), `_resolve_legacy_intent_cascade_model` (L873), `_resolve_governed_intent_model` 의 shadow 분기 (L962~) | 코드 상수 + `intent_policies_db_primary` | DB 가 모든 intent 를 덮는 것을 확인한 뒤(3단계) 삭제 |
| 11 | `app/services/chat_service.py:_FALLBACK_CHAIN_429` (L7231) | 구형 키(`claude-opus-4-6`, `claude-opus-5`, `claude-sonnet-5`)가 하드코딩 | mrp `llm` route 순서에서 생성하거나 최소한 구형 키 제거 |
| 12 | `_AUTO_ROUTED_DB_DEFAULT_MODELS` — `model_selector.py` L773, `chat_service.py` L27, `turn_model_contract.py` L26 | 3곳 복제(일치 검사 테스트가 있음) | `turn_model_contract` 한 곳으로 모으고 나머지는 import. **`qwen-turbo` 표식은 유지** |
| 13 | `app/services/pipeline_runner_service.py:_CLAUDE_MODEL_BY_SIZE` (L65), `app/api/pipeline_runner.py:_get_model_for_size` (L652) | 같은 기본값 `claude-sonnet-5-5` 가 두 곳 | 상수 하나 |
| 14 | `app/services/cli_model_autoreg.py` (L230, L303) | mrp `runner_llm`, `llm` 에 후보를 `is_enabled=false` 로만 추가 | **변경 없음.** mrp 가 능력 라우트 정본이므로 계속 허용 |
| 15 | 대시보드 `components/settings/ModelSettingsPanel.tsx` | mrp 와 intent 정책을 한 패널에서 표시 | retired 그룹 접기(§7-4 와 짝). 러너·지시서 패널은 변경 없음 |

---

## 8. 결정이 필요한 항목

| ID | 질문 | 권장 | 근거 |
|---|---|---|---|
| D0 | CEO 승인안 "2곳" 을 "개념당 1곳(3곳 + 지시서)" 으로 범위 정정하는가 | 정정 | §4. 문자 그대로는 능력 라우트·배경 LLM 의 집이 없다 |
| D1 | `intent_policies` 허용 목록에 `claude-opus-5-5` 를 넣어 Opus 가 강등되지 않게 할 것인가 | 별도 결정. 이번 정리와 분리 | 비용 정책이다(§6-B) |
| D2 | 배경 1순위 모델 id 를 `claude-haiku-4-5-20251001` 로 할 것인가 `claude-haiku` 로 할 것인가 | 현 코드 상수와 맞춰 `…-20251001` | 단, `llm_models` 에서 `anthropic/claude-haiku-4-5-20251001` 은 `is_active=false, is_executable=false` 이고 `claude-haiku` 가 active 다. mrp 읽기를 신설하면 가용성 판정이 `not_configured` 로 나올 수 있다. **5번 변경 전에 레지스트리 별칭을 먼저 정리해야 한다** |
| D3 | `agent_*` route 를 폐기할 것인가, 실제로 읽도록 연결할 것인가 | 폐기 | 읽는 코드가 없고, 에이전트별 모델은 `intent_policies`/러너가 이미 결정한다. 초안 #2 는 폐기 전까지의 표시 정리일 뿐 |
| D4 | mrp `runner_llm` 꼬리를 유지할 것인가 | 유지(그러나 병합은 뷰로) | 세 호출처가 의존. `code_reviewer` 는 의도적으로 안 쓴다 |

## 9. 위험과 가정

- **가정**: retired route 를 읽는 코드가 없다 — 0단계에서 검증. 틀리면 §6-A #1·#2 의 영향이 "없음"이 아니게 된다.
- **캐시**: `intent_policies` 는 300초 TTL(`_INTENT_POLICY_CACHE_TTL_SECONDS`), mrp `llm` default 는 60초, `directive_model_config` 는 60초.
  적용 후 반영까지 최대 5분이 걸리고, 이 반영에 재시작은 필요 없다. 5번(배경 1순위 신설)은 코드 변경이므로 reload 가 필요하다.
- **자동 마이그레이션**: §6-D. 초안을 `.sql` 로 바꿔 `migrations/` 에 넣는 순간 다음 릴리스가 적용한다.
- **데이터는 실측, 코드 판단은 추정**: DB 값은 2026-10-02 SELECT 결과다. "읽는 코드 없음"은 grep 해석이고, 실행 추적은 하지 않았다.
- **이 설계가 다루지 않은 것**: `llm_models` 레지스트리 정합성, `_COST_MAP` 단가, `intent_router` 의 intent 분류 어휘.
