# AADS-GOAL-V12-W14A-R1-20260919

- 구현: immutable change set body hash/target version, 독립 다중 승인, reject-wins,
  W-14F 실행 직전 재검증, transaction 내부 mutation/effect/outbox, owner epoch outbox
  claim/ack fence, unknown outcome reconciliation.
- 보안 보강: change-set immutable DB trigger, 신규 3개 테이블 FORCE RLS,
  policy decision principal 결합, UUID/시간 envelope 정규화, ordered patch 적용.
- 검증: `pytest -q tests/unit/test_goal_*.py` **348 passed**; disposable PostgreSQL
  W14a multi-approval/rollback/exactly-once **1 passed**; 전체 migration/RLS **1 passed**;
  Ruff, py_compile, `git diff --check` PASS.
- 운영 DB migration·배포: 미실행.

---

# AADS-GOAL-V12-W14F-POLICY-FOUNDATION-20260919

## STEP 0 기존 구현 조사 및 분류

| 접점 | 분류 | 결과 |
|---|---|---|
| W-13 precondition 함수 3종 | 유지 | 서버 계산 snapshot/실행 직전 stale 계약 유지 |
| `goal_policy_decisions` 및 policy foundation stores | 수정 | 서명 key·ancestor epoch와 executor fence 보강 |
| assignment/grant/kill-switch DB 접점 | 수정 | mutable epoch 재검증용 additive 컬럼·trigger·함수 |
| 4축 evaluator/JCS/hash/HMAC/executor verifier | 신규 | W-14F foundation service 추가 |
| 단일 grant reservation/overrun guard | 신규 | 합성 금지·원자 차감·stale 처리 |
| 집중 unit/integration 검증 | 신규/수정 | T36/T38/T45/T46/T48~T58 및 migration 반복 적용 |
| 기존 router/API와 W12/W13 테스트 | 유지 | 삭제·통째 대체 없음 |
| 삭제 | 없음 | 호출처 영향 및 롤백 대상 삭제 없음 |

지시서 외 service/migration/test/handover 파일 변경 사유는 evaluator/executor 계약과
DB fence 및 독립 검증 증거를 구현하기 위해서다. 상세 분류와 롤백 범위는
`docs/handover/AADS-GOAL-V12-W14F-POLICY-FOUNDATION-20260919.md`에 기록했다.

## 변경 파일

- `app/services/goal_policy_foundation.py`
- `migrations/20260919_goal_policy_foundation_w14f.sql`
- `migrations/20260919_goal_policy_foundation_stores.verify.sql`
- `tests/unit/test_goal_policy_foundation_w14f.py`
- `tests/unit/test_goal_policy_foundation_migration.py`
- `tests/integration/test_goal_policy_foundation_migration.py`
- `docs/contracts/GOAL_POLICY_FOUNDATION_INTERFACE_V1.md`
- `docs/handover/AADS-GOAL-V12-W14F-POLICY-FOUNDATION-20260919.md`
- `RESULT.md`

## 검증 결과

- `git diff --check`: PASS.
- `pytest -q tests/unit/test_goal_*.py`: **340 passed**.
- W-14F/W-13/W-12 집중 unit 4개 파일: **41 passed**.
- disposable PostgreSQL `goal_w14f_r5b_test`에서
  `tests/integration/test_goal_policy_foundation_migration.py`: **1 passed**.
- 독립 검수에서 mutable epoch 서명, 현재 target version, 활성 policy/assignment,
  kill switch, grant ancestor 상태, RFC 8785 binary64/Unicode 경계를 추가 보강했다.
- 러너 생성물 `.runner_full_diff.patch`는 최종 커밋에서 제외했다. 원본은
  `/tmp/runner-2085d6bd-full-diff.patch`로 이동해 복구 가능하다.
- npm/next/docker build: 승인 후 Runner 빌드 검증 대상.
- production DB evidence: 조회·변경하지 않음. B-04 승인 완료를 주장하지 않음.
- commit SHA/push state: 최종 커밋 시 갱신 대상.
- 삭제 0건, production write 0건, 배포 0건.

---

# AADS-GOAL-V12-W13-PRECONDITIONS-BOUNDARY-20260919

## STEP 0 기존 구현 조사

| 접점 | 분류 | 처리 |
|---|---|---|
| `ActorScope`, `resolve_actor_scope`, `require_project_access` | 유지 | DB 원천 identity/project 판정 계약 유지 |
| `create_work_item`, `create_project_assignment`, tree/governance API | 유지 | 기존 M13 경로와 응답을 대체하지 않음 |
| `project_role_assignments` active role/session unique index | 유지 | DB 409 변환 계약 유지 |
| `work_item_dependencies`, `work_item_evidence`, review stores | 유지 | W-12 primary source로 읽기만 수행 |
| `app/routers/work_items.py` identity/API surface | 수정 | 빈 tenant/identity 401 fail-close, W-13 preview/input API 추가 |
| `compute_preconditions`, `create_policy_input`, `require_current_preconditions` | 신규 | 서버 계산·immutable snapshot·실행 직전 stale guard |
| `goal_precondition_snapshots`, `goal_policy_inputs` | 신규 | additive migration, FORCE RLS |
| W-13 unit/disposable PostgreSQL checks | 신규 | T56/T57, hash 결정성, repeat migration/RLS |
| 삭제 | 없음 | 기존 계약 삭제 불필요 |

지시서에 직접 열거되지 않은 파일 중 migration과 test 파일을 변경/추가한 사유는
버전 snapshot 영속화와 disposable PostgreSQL 완료기준을 코드로 검증하기 위해서다.

## 결과

- 기준 HEAD/origin-main: `0f98c5eefc56305944c90cce2d748a41623e0dee`
- 선행 `e1061c657a59fadf612c30bb294c432221aed6e6`: origin/main 조상 확인
- 서버 계산 필드: parent state/version, evidence completeness/hash, review verdict,
  blocker/dependency blocker count
- client precondition object는 request model에서 입력으로 채택하지 않으며
  `expected_parent_version`만 optimistic check에 사용
- CEO integrated는 active local project assignment가 없으면
  `403 local_assignment_required`; 일반 session/assignment project 불일치는 403
- 다른 tenant resource는 외부 `404 resource_not_found`, 내부
  `tenant_scope_denied` work-item event 기록
- policy input은 `automation_claimed=false`, `decision_id=null`,
  `reason_codes=[evaluation_required]`이며 AUTO를 주장하거나 grant를 소비하지 않음
- 변경 파일 삭제 0건, production write 0건

## 검증

- `python3 -m py_compile ...`: PASS
- `git diff --check`: PASS
- AAG baseline: PASS (`고정선 대비 증가 없음`)
- pytest target: HOLD — 현재 호스트 Python에 `fastapi`가 없어 collection 중단
- disposable PostgreSQL: 코드 추가, 미실행 (`M12_TEST_DATABASE_URL` 미제공)
- pre-commit: 미실행 (staging이 필요한 hook이며 `git add` 금지)
- npm/next/docker build: 승인 후 Runner 빌드 검증 대상
- commit/push/deploy: 미실행(사용자 필수 규칙)

---

# AADS-VISION-UNIFY-20260917-R3 — 비전 입력 통합 (extract_image_blocks 보존)

`build_vision_blocks()` 를 **신규 추가**하고, 기존 public 함수
`extract_image_blocks()` 는 선언·시그니처·docstring 을 그대로 둔 채 본문만
위임으로 바꿨다. R1 을 막았던 PRESERVATION_HARD_GATE(=public 함수 삭제·재추가)
를 피하려고, 새 함수를 `extract_image_blocks` **뒤에** 배치해 diff 에서 선언 줄이
context(변경 없음)로 남도록 했다.

## 전제 확인 — R2 내용은 이미 이 브랜치에 있다

지시서는 "R2 diff a6064a06 기준으로 재구성" 이라고 했으나, 실측 결과 a6064a06 의
내용은 `e4f9468f` 로 이미 현재 HEAD(a86fa739) 계보에 들어와 있다.

```
git diff a6064a06 HEAD -- app/core/document_context.py app/services/chat_service.py \
    app/core/anthropic_client.py tests/unit/test_vision_blocks.py app/services/design_auditor.py
# → 출력 없음 (동일)
```

따라서 R3 에서 실제로 남은 델타는 ①`build_vision_blocks` 분리 ②chat_service 호출부
이름 교체 ③anthropic_client 의 러너 경로 변환 ④heic/heif 사유 처리 ⑤테스트 보강
다섯 가지다. R2 를 통째로 다시 얹지 않았다(중복 재적용 방지).

## STEP 0 — 기존 구현 조사 및 분류

### `app/core/document_context.py` (수정 전 705행)

| 항목 | 분류 | 근거 |
|---|---|---|
| `estimate_tokens`, `extract_file_contents`, `_extract_pdf*`, `_extract_excel`, `build_ephemeral_document_layer`, `build_file_reference_summary`, `detect_file_rereference`, `build_rereference_context` | 유지 | 비전 경로와 무관 |
| `_is_sensitive_path`, `_downscale_to_limit`, `_convert_to_png` | 유지 | 손대지 않음 |
| `_build_block_from_bytes` | 수정 | HEIF 분기를 UNSUPPORTED 판정 **앞에** 삽입. 그 외 경로 불변 |
| `extract_image_blocks` | 수정(본문만) | 선언 줄·시그니처·docstring 보존, 본문 = `return build_vision_blocks(...)` |
| `build_vision_blocks` | 신규 | 실제 로직(기존 본문을 그대로 이동) |
| `_convert_heif_to_png` | 신규 | pillow_heif 있으면 PNG, 없으면 None |
| `HEIF_EXTENSIONS`, `HEIF_MEDIA_TYPES` | 신규 | 상수 |
| `UNSUPPORTED_IMAGE_EXTENSIONS` | 유지 | `.heic/.heif` 항목도 **제거하지 않았다** — HEIF 분기가 먼저 걸리고, 그 분기가 실패하면 여기 규칙이 그대로 남아 있어야 안전 |
| 삭제 | **0건** | — |

### `app/core/anthropic_client.py`

| 항목 | 분류 | 근거 |
|---|---|---|
| `_user_content`, `_to_openai_image_content`, `_try_user_key_claude`, `_call_dashscope`, `_call_litellm` | 유지 | R2 에서 이미 `images` 를 받는다 |
| `call_llm_with_fallback` | 수정 | 본문 첫 줄에 `images = _normalize_images(images)` 한 줄 + docstring 보강. 시그니처 불변 |
| `_normalize_images` | 신규 | 러너가 넘기는 디스크 경로(str/PathLike) → `build_vision_blocks(extra_paths=...)` |
| 삭제 | **0건** | — |

정규화를 `call_llm_with_fallback` 진입부 한 곳에만 넣은 이유: 그 아래 BYOK·OAuth·
DashScope·LiteLLM 네 경로가 모두 같은 `images` 를 재사용하므로, 여기서 한 번
바꾸면 폴백 경로까지 자동으로 덮인다.

### `app/services/chat_service.py`

| 항목 | 분류 | 근거 |
|---|---|---|
| `send_message_stream` attachments 경로(11739·11751) | 수정 | `extract_image_blocks` → `build_vision_blocks` (import + 호출 + 주석) |
| `uploaded_files` 경로(11824·11825) | 수정 | 동일 |
| `file_id` 첨부 인라인 image block(11762~11772) | **유지** | 지시서가 허용한 교체 범위는 `extract_image_blocks` 호출 2곳뿐이다. 이 경로는 아직 인라인으로 block 을 만들고 있어 포맷 변환·5MB 축소·중복 제거를 받지 못한다 — 다음 작업 후보로 남긴다(아래 "남긴 것") |
| 삭제 | **0건** | — |

### `app/services/design_auditor.py`

**변경 0건** (`git diff --stat` 출력 없음). 지시서 금지 항목.

## 변경 파일 (4개)

```
 app/core/anthropic_client.py     | +38
 app/core/document_context.py     | +61 -1
 app/services/chat_service.py     |  +3 -3
 tests/unit/test_vision_blocks.py | +95
```

## 동작

- png/jpg/jpeg/gif/webp → 네이티브 image block (원본 재인코딩 없음)
- bmp/tiff/tif/ico/ppm/pcx → Pillow 로 PNG 변환
- 5MB 초과 → 장변 1568px 로 축소해 **전송**(폐기하지 않음), 실패 시에만 스킵
- pdf → 네이티브 document block (32MB 초과 시 스킵)
- heic/heif → pillow_heif 있으면 PNG, 없으면 사유를 남기고 스킵
- SHA-256 중복 제거(첨부와 extra_paths 에 같은 바이트가 오면 1건)

### heic 사유 문자열에 대한 해석

지시서는 "pillow_heif 없으면 사유 문자열 반환" 이라고 했으나,
`_build_block_from_bytes`/`build_vision_blocks` 의 반환 타입은 content block 목록이라
사유 문자열을 섞으면 호출처(messages content)가 그대로 깨진다. 그래서 **사유는
로그로 남기고 block 목록에서는 제외**하는 R2 방식을 유지했다:

```
[Vision] [unsupported_image_format: .heic] photo.heic — pillow_heif 미설치로 건너뜀
```

기존 테스트(`test_heic_skipped_with_log`)도 `blocks == []` 를 요구하고 있어 이쪽이
계약과 일치한다. 사용자에게 사유를 노출해야 한다면 반환 타입을 바꾸는 별개 작업이다.

## 검증

| 항목 | 결과 |
|---|---|
| `grep '^def extract_image_blocks' document_context.py` | 616행 존재 ✅ |
| diff 에서 `def extract_image_blocks(` 가 `-`/`+` 로 안 나옴 | ✅ (context 로만 등장) |
| `grep '^def build_vision_blocks'` | 686행 신규 ✅ |
| `design_auditor.py` 변경 | **0건** ✅ |
| `run_unit_tests.sh tests/unit/test_vision_blocks.py` | **32 passed** ✅ |
| `run_unit_tests.sh tests/unit/test_tools_and_pipeline.py` | **73 passed** ✅ (기준선 68 이상) |
| `ruff check --select F821,F811` (변경 4파일) | All checks passed ✅ |
| 운영 이미지 + 워킹트리 마운트 import | `document_context`·`anthropic_client` import ok, `chat_service` 컴파일 ok ✅ |
| `extract_image_blocks` 잔존 호출처(app/) | 0건 — 정의만 남음 ✅ |

추가한 테스트 10건: 위임 확인(spy), 구/신 이름 결과 동일, 혼합 입력, 빈 입력,
HEIF 변환 가능/불가, `_normalize_images` 경로 변환·통과·잘못된 경로,
`call_llm_with_fallback(images=[경로])` end-to-end.

## 남긴 것

`chat_service.py` 의 `file_id` 첨부 경로(11762~11772)는 여전히 인라인으로 image
block 을 만든다. 지시서가 허용한 교체 범위(호출 2곳) 밖이라 건드리지 않았다.
이 경로로 들어온 5MB 초과 이미지나 bmp/heic 은 통합 파이프라인을 타지 못해
Anthropic 400 이 날 수 있다 — 다음 라운드에서 `build_vision_blocks` 로 옮길 대상.

---

# AADS-SMARTBROWSER-G2-UNTRUSTED-PAGE-DATA-20260919

## STEP 0 — 기존 구현 조사 및 분류

| 항목 | 분류 | 반영/판단 |
|---|---|---|
| `ChannelRouter.route_directive`, `validate_action_intent`, `route_observation` | 수정 | 인증된 서버 ingress provenance를 확인하고 DOM/ARIA/OCR/RAG/file taint가 명령으로 승격되면 `PAGE_DATA_COMMAND_ATTEMPT`로 차단한다. |
| `DirectiveEnvelope`, `ActionIntent` | 수정 | `authenticated_provenance`를 유지·실행 경계까지 전달한다. caller의 source 라벨만으로 신뢰하지 않는다. |
| `ObservationEnvelope` | 수정 | 기본·필수 taint를 `UNTRUSTED_PAGE_DATA`로 고정한다. |
| `directive_from_authenticated_context` | 신규 | request의 서버 인증 context에서 tenant/user/source를 결선하는 유일한 API ingress 생성기다. |
| `/browser-tasks` 생성 ingress | 수정 | request context 기반 directive 생성으로 교체했다. screenshot 관측은 기존 ObservationEnvelope 경로를 유지한다. |
| `/ohvis/console/command`, `/recipes/run` 및 `run_directive` | 수정 | 레시피 directive와 inputs를 같은 ActionIntent에 묶고, resume/별도 인자에 의한 taint 우회를 실행 직전 재검사한다. |
| `/pc-agent/execute`, `/pc-agent/route-execute` | 수정 | PC 전송 직전 params의 taint를 fail-closed 재검사한다. |
| `ToolExecutor.execute` | 수정 | LLM이 생성한 tool input에서 taint 발견 시 dispatch 전에 구조화된 차단 결과를 반환한다. |
| 기존 recipe guard의 `sanitize_page_text`, `assert_not_page_derived` | 유지 | 문자열 패턴 방어는 보조층으로 보존하며 구조적 provenance 검사를 대체하지 않는다. |
| 삭제 | 0건 | 호출처 삭제 및 롤백 대상 없음. 롤백은 이 작업의 변경 hunks만 되돌리면 된다. |

## 변경 및 보안 경계

- DOM/ARIA/OCR/screenshot OCR/downloaded file/RAG/file 관측은 `ObservationEnvelope`로만 수용하며 `UNTRUSTED_PAGE_DATA` taint를 유지한다.
- 페이지 텍스트의 태그 탈출 문자열은 신뢰 태그가 아니라 구조적 taint로 판정하므로 command/tool capability를 얻지 못한다.
- Browser recipe, PC command, LLM tool dispatch 각각에서 실행 직전 taint를 재검증한다. 차단은 감사 로그에 `reason_code=PAGE_DATA_COMMAND_ATTEMPT`로 남는다.
- 정상적인 사용자 directive 및 사용자 입력 검색어는 taint marker가 없으므로 계속 허용된다. tainted cross-tenant 값은 명령 채널로 들어오기 전에 차단된다.

## 검증

| 항목 | 결과 |
|---|---|
| 간접 프롬프트 인젝션 golden cases | `tests/unit/test_channel_router.py`에 DOM/ARIA/OCR/RAG/file, 태그 탈출, tool-call, cross-tenant tainted input 차단 케이스 추가 |
| 정상 추출 회귀 | 동일 테스트에 일반 사용자 검색 인자 허용 케이스 추가 |
| focused/affected test 실행 | 실행하지 않음 — 사용자 규칙상 코드 수정만 수행 |
| 빌드 검증 | 승인 후 Runner 빌드 검증 대상 |
| commit/push/deploy | 실행하지 않음 |

| AADS handover DB evidence | DB 변경/기록을 수행하지 않음 — 사용자 규칙상 파일 수정 외 작업 금지 |

---

# AADS-SMARTBROWSER-G4-ARIA-PARTIAL-SIGNATURE-20260919

## STEP 0 — 기존 구현 조사 및 분류

| 항목 | 분류 | 반영/판단 |
|---|---|---|
| `browser_recipes` 및 `browser_recipe_registry`의 recipe/version tenant 정본 | 유지 | 사이트·페이지 템플릿 정본을 새 테이블로 복제하지 않는다. `capture_rules.aria_signature`만 읽어 재방문 정책을 적용한다. |
| `normalize_recipe_payload`, `get_browser_recipe`, `plan_browser_recipe_run`, `create_browser_recipe_run` | 유지 | 실행·큐·승인 흐름을 변경하지 않는다. |
| `browser_page_structure_signatures` migration | 신규 | tenant·recipe·version·page/area별 signature version, hash, similarity, reason, decision과 Human Gateway 필요 여부를 additive하게 저장한다. |
| `aria_structure_signature` | 신규 | ARIA subtree의 role/안정 이름 해시/state/의미 관계/stable data attribute만 정규화하고 재사용·재탐색·Human Gateway를 판정한다. |
| `tests/unit/test_aria_structure_signature.py` | 신규 | 허용 변화, 필수 anchor/role 변경, ambiguity, ARIA 부재 폴백, 동적·개인화 텍스트 배제를 회귀한다. |
| 삭제 | 0건 | 호출처 영향 없음. 롤백은 신규 서비스·migration·테스트 변경만 되돌리면 된다. |

## 변경 및 보안 경계

- raw accessible name, raw relationship id, DOM id, 가격·재고·광고·시각·개인화 텍스트는 시그니처에 저장하지 않는다. 안정적인 이름은 정규화 후 SHA-256 해시만 저장한다.
- `data-testid`, `data-test`, `data-qa`, `data-cy`, `data-component`만 허용하며 값도 해시로 저장한다. 형제 순서는 정렬해 비교한다.
- ARIA 부재·중복 구조는 `rediscover`, 필수 anchor 삭제는 `human_gateway`로 판정한다. similarity가 recipe template의 threshold 이상일 때만 `reuse`다.
- 모든 DB 조회/저장은 `tenant_id`와 recipe/version/page/area 범위로 제한한다. 서명 이력에 사용자 맞춤 텍스트를 보관하지 않는다.

## 검증

| 항목 | 결과 |
|---|---|
| focused regression | `pytest -q tests/unit/test_aria_structure_signature.py` — **4 passed** (pytest 설정 경고 1건) |
| affected regression | `tests/unit/test_browser_task_policy.py` 포함 실행은 환경의 `asyncpg` 미설치로 collection 실패. 변경 전 의존성 문제이며 통과로 처리하지 않음. |
| migration/DB handover evidence | DB 변경·handover 기록 미수행 — 사용자 규칙상 파일 작업 외 실행 금지. migration은 Runner 적용 대상. |
| 빌드 검증 | 승인 후 Runner 빌드 검증 대상 |
| commit/push/deploy | 모두 미실행 |

---

# AADS-SMARTBROWSER-M4-RECOVERY-R3-20260919

## STEP 0 — 기존 구현 조사 및 분류

| 항목 | 분류 | 반영/판단 |
|---|---|---|
| `browser_recipes` / `browser_recipe_registry`의 tenant별 recipe/version 정본 | 유지 | 정본을 복제하거나 recovery가 직접 수정하지 않는다. 후보 patch는 기존 G6 learned artifact lifecycle에만 생성한다. |
| `aria_structure_signature.assess_revisit` / `record_revisit_signature` | 유지 | G4의 `reuse`/`rediscover`/`human_gateway` 판정 및 개인정보 배제는 그대로 사용한다. |
| `golden_promotion_gate.evaluate_promotion_gate`, learned artifact candidate→shadow→active 및 rollback | 유지 | recovery는 `candidate`만 만들며 승격을 호출하거나 우회하지 않는다. G6 gate와 기존 rollback만 active 전환을 허용한다. |
| `browser_recipe_recovery` | 신규 | selector/ARIA, 로그인 만료, 일시 네트워크 오류를 분류하고 bounded·idempotent 복구 결정을 기록한다. |
| `browser_recipe_recovery_events` migration | 신규 | tenant/recipe/version/site/page/skill/version 및 evidence hash·retry 한도·candidate 참조만 additive하게 보관한다. |
| `/browser-recipes/{recipe_id}/versions/{version}/recovery` | 신규 | MEMBER 권한과 기존 tenant-scoped recipe 존재 확인 뒤 recovery를 기록한다. |
| `tests/unit/test_browser_recipe_recovery.py` | 신규 | 실패 분류, 재시도 한도, 민감정보·페이지 명령문 미기록, tenant scope, candidate-only 경로를 회귀한다. |
| 삭제 | 0건 | 호출처 영향 및 별도 롤백 대상 없음. 롤백은 본 작업의 신규 migration/service/API/test hunks만 되돌리면 된다. |

## 변경 및 보안 경계

- recovery evidence는 허용된 SHA-256 hash와 node count만 저장한다. raw credential/cookie/OTP/error text/page command는 저장하지 않는다.
- selector 재발견은 제한된 CSS selector 형식만 후보 patch에 보관하며, `javascript:`·명령문·credential marker는 거부한다.
- tenant와 recipe/site/page/skill/version scope 및 idempotency key를 모든 recovery 조회·저장에 적용한다. 네트워크 재시도는 scope별 최대 2회이고 동일 idempotency key는 replay한다.
- 로그인 만료와 재시도 한도 초과는 명확한 Human Gateway 안내를 반환한다. selector 변경·ARIA 불일치는 rediscover로만 진행하고, candidate 상태의 G6 artifact version만 생성한다.
- active recipe 또는 active learned artifact로 자동 승격하지 않는다. shadow/active 전환은 기존 G6 golden·regression gate가 통과한 별도 promotion 경로에서만 가능하다.

## 검증

| 항목 | 결과 |
|---|---|
| focused/affected pytest, Ruff | 실행하지 않음 — 사용자 규칙상 파일 수정 외 명령 실행 금지 |
| migration `BEGIN/ROLLBACK` 검증 | 실행하지 않음 — 사용자 규칙상 DB 작업 금지 |
| `git diff --check` | 실행하지 않음 — 사용자 규칙상 파일 수정 외 명령 실행 금지 |
| 빌드 검증 | 승인 후 Runner 빌드 검증 대상 |
| commit/push/deploy | 실행하지 않음 |

# AADS-SMARTBROWSER-M8-AUTO-LEARNING-R6-20260920

## STEP 0 — 기존 구현 조사 및 분류

| 항목 | 분류 | 반영/판단 |
|---|---|---|
| `site_knowledge.create_page_template_candidate`, tenant/site canonical lookup | 유지 | 기존 수동 candidate 생성과 G6 정본을 변경하지 않고 자동 방문 경로가 동일 artifact/version 테이블을 사용한다. |
| `aria_structure_signature.build_partial_signature`, `assess_revisit` | 유지 | G4 role/name/state/관계 기반 비교와 동적 텍스트 배제 정책을 그대로 호출한다. |
| `ohvis_harness.validate_skill_manifest`, executor registry, G6 promotion gate | 유지 | eval/import 없이 등록 callable만 실행하는 계약 및 candidate→shadow→active gate를 우회하지 않는다. |
| `smart_browser_learning.auto_learn_site_visit` | 신규 | 최초 방문 paired candidate 생성, 재방문 reuse/Human Gateway/invalidation, 단조 version 할당을 scope row lock transaction으로 수행한다. |
| `smart_browser_learning._pending_candidate` | 신규 | 동일 transaction의 두 pending candidate 조회를 공통 private helper로 통합하여 중복 guard를 해소한다. |
| `/site-knowledge/profiles/{site_profile_id}/auto-visit` | 신규 | 인증 tenant context와 untrusted observation channel을 결선하고 후보 상태만 반환한다. |
| `browser_site_learning_scopes` migration/rollback | 신규 | tenant/site/page unique scope와 `FOR UPDATE` allocator, tenant-bound trigger를 additive/idempotent하게 제공한다. |
| focused tests | 수정 | 자동 skill 계약, candidate-only 경로, migration/rollback, API route를 정적 회귀한다. |
| 삭제 | 0건 | 호출처 삭제 및 데이터 삭제 없음. rollback은 scope allocator만 제거하고 canonical artifact/version은 보존한다. |

## 변경 파일

- `app/services/smart_browser_learning.py`
- `app/api/site_knowledge.py`
- `migrations/20260920_m8_auto_site_learning.sql`
- `migrations/rollback/20260920_m8_auto_site_learning.down.sql`
- `tests/unit/test_smart_browser_learning.py`
- `tests/integration/test_smart_browser_learning_postgres.py`
- `RESULT.md`

## 검증 결과

| 항목 | 결과 |
|---|---|
| pending candidate 중복 | 공통 `_pending_candidate()` helper 1개와 호출 2곳으로 통합한 소스 수준 확인. |
| focused + M7/G1/G2/G4/G6 영향 회귀 | `run_unit_tests.sh` — **83 passed**. |
| PostgreSQL 동시성·tenant 격리 | disposable `smartbrowser_m8_verify_0928` — **1 passed**; 동시 최초 방문이 candidate 1쌍만 생성하고 타 tenant 접근을 차단. |
| Ruff, `py_compile`, `git diff --check` | 모두 PASS. |
| disposable PostgreSQL migration 2회 적용 및 rollback/reapply | 2회 적용 성공 → rollback 후 테이블 `ABSENT` → reapply 성공. |
| npm/next/docker build | 실행하지 않음 — 승인 후 Runner 빌드 검증 대상. |
| commit SHA | 이 결과와 통합 테스트를 포함한 최종 amend 커밋으로 확정. |

## 미완료 항목

- push/deploy는 후속 승인·M9~M11 의존 체인에서 수행한다.

# AADS-SMARTBROWSER-M9-ROUTING-R2-20260920

## 변경 및 보안 경계

- `resolve_site_skill`은 tenant/site/active version/capability 범위 안에서 Exact → Qwen3 vector → bounded LLM 순서를 강제하고 단계별 reason/score/threshold/cost/latency를 감사 이벤트에 기록한다.
- `plan_skill_runtime`은 요청 capability가 manifest capability의 부분집합인지, manifest permission이 서버 인증 membership 권한에 포함되는지 각각 검증한다. 둘을 혼용하지 않는다.
- Browser/PC Agent executor 계약이 없거나 세션·로컬 환경·고위험 승인 조건이 부족하면 Human Gateway로 fail-closed 한다. 클라이언트가 capability를 비워 manifest permission을 우회할 수 없다.
- 기존 JSONB 감사 이벤트 저장소를 재사용해 신규 migration은 필요하지 않다.

## 변경 파일

- `app/services/smart_browser_learning.py`
- `app/api/site_knowledge.py`
- `tests/unit/test_smart_browser_learning.py`
- `RESULT.md`

## 검증 결과

| 항목 | 결과 |
|---|---|
| 최초 러너 회귀 | 1 failed, 87 passed — 누락된 `required_capabilities`를 재현하고 승인 차단 |
| 교정 후 focused + M7/M8/G1/G2/G6 영향 회귀 | **91 passed** |
| 권한 음성 테스트 | 빈 capability 우회, browser executor 부재, 고위험 승인 부재를 Human Gateway로 차단 |
| Ruff, `py_compile`, `git diff --check` | 모두 PASS |
| migration | 스키마 변경 없음; M7/M8 정본과 기존 JSONB 감사 저장소 재사용 |

## 미완료 항목

- push/deploy는 M10·M11 순차 완료 후 릴리스 단계에서 수행한다.

# AADS-SMARTBROWSER-M10-LIVE-DATA-R3-20260920

## STEP 0 기존 구현 조사 및 분류

| 접점 | 분류 | 처리 |
|---|---|---|
| `live_fact_gate.display_fact`, `guard_payload_for_display` | 수정 | 기존 G5 최종 표시 gate를 유지하고 STALE/CONFLICT/UNAVAILABLE `reason_code`와 복구 행동을 추가했다. |
| `live_fact_gate.revalidate_live_fact`, revalidator registry | 수정 | 기존 서버 등록형 read-only 원출처 재검증을 유지하고 context/source/evidence/value 불일치를 fail-closed로 분리했다. |
| `live_fact_gate.record_live_fact` | 수정 | fact/provenance/freshness와 최초 evidence event를 단일 transaction에 저장한다. |
| `site_knowledge.record_live_observation` 및 API | 수정 | M7 tenant/site 정본을 재사용하고 live fact 오류 변환과 raw DOM/ARIA/OCR key 차단을 보강했다. |
| browser task/artifact/chat 최종 응답, Redis SSE replay gate | 유지 | 기존 G5 결선을 재사용한다. 중복 구현하지 않았다. |
| `20260920_m10_live_fact_freshness` migration/rollback | 신규 | `fetched_at`, reason ledger, tenant/site·event composite FK, fact type/TTL constraint, tenant RLS를 additive/idempotent하게 추가한다. |
| focused/PostgreSQL tests | 신규·수정 | TTL boundary, mismatch, source failure, SSE, tenant/constraint/rollback 계약을 검증한다. |
| 삭제 | 0건 | 호출처·데이터 삭제 없음. rollback은 M10 enforcement만 제거하고 observation 데이터와 추가 column은 보존한다. |

지시서 밖 파일 변경은 없다. 기존 G5/M7 구현은 대체하지 않았다.

## 변경 파일

- `app/services/live_fact_gate.py`
- `app/services/site_knowledge.py`
- `app/api/site_knowledge.py`
- `migrations/20260920_m10_live_fact_freshness.sql`
- `migrations/rollback/20260920_m10_live_fact_freshness.down.sql`
- `tests/unit/test_live_fact_gate.py`
- `tests/unit/test_site_knowledge.py`
- `tests/integration/test_live_fact_m10_postgres.py`
- `RESULT.md`

## 검증 결과

| 항목 | 결과 |
|---|---|
| 기준선 | HEAD/origin/main `a8e7d431`; M9 포함 확인, 시작 시 clean detached worktree. |
| focused + M7~M9/G1/G2/G5/G6 영향 회귀 | `./scripts/run_unit_tests.sh ...` — **89 passed**. |
| 최초 focused | **28 passed**. |
| Ruff / py_compile / diff-check / pre-commit hook | 모두 PASS. |
| disposable PostgreSQL 2회/rollback/reapply | 격리 DB `smartbrowser_m10_verify_1002`에서 2회 적용, tenant/site FK, RLS 격리, rollback/reapply — **1 passed**. |
| npm/next/docker build | 실행하지 않음 — 승인 후 Runner 빌드 검증 대상. |
| commit SHA | 러너 산출물 `e95c52efd0be45ae26bf2f8c6903767975d65917`; 독립 검증 보강은 후속 커밋에 기록한다. |

## 미충족 항목

- 없음. 운영 적용은 M11 완료 후 단일 블루그린 릴리스에서 수행한다.
