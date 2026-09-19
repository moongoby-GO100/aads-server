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
