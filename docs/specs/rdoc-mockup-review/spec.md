# R-DOC 목업 필수 제출·승인 — 설계 명세
- 프로젝트: AADS · 버전: 2.0.0 · 작성일: 2026-10-03 KST (v1.0.1 → 2.0.0 개정, 같은 날)
- 상태: 검토용 초안(draft). 문서 작성 지시는 승인됐지만 이 설계·시안의 구현 승인은 아직 없다. 기존 승인본 지정은 바꾸지 않았다.
- 상위 목표: 0361c451-cc03-4bd1-a423-76051b0546b2 (기존 R-DOC 목표, 조회 확인)
- 근거: CEO의 “화면 구현이 필요한 기획·설계·PRD는 목업까지, 기존 화면 수정은 전후 페이지까지 보고” 지시.
- 이 패키지는 R-DOC의 목업 제출·승인 기능에 대한 독립 승인 단위다. 기존 저장 규칙 문서를 복제하거나 기존 document_key/승인본을 교체하지 않는다.

- document_key: rdoc-mockup-review-spec
- 연결: [기획](../../plans/20261003_AADS_RDOC_MOCKUP_REVIEW_PLAN.md), [PRD](../../prd/20261003_AADS_RDOC_MOCKUP_REVIEW_PRD.md), [v2 검증 기록](VERIFICATION-v2.md)
- 아래 API·DB·상태·게이트는 신규 설계다. 실제 존재하는 기능은 “현재 기반”에만 적는다.

## 현재 기반과 확장 경계
app/api/canonical_documents.py는 문서 revision, expected_generation, tenant/project 권한, approve/review와 감사 이벤트를 지원한다. 이를 문서 참조와 권한 모델의 기반으로 재사용한다. design_modifications.py의 상태 문자열 approved 존재만으로 목업 승인 전이를 구현했다고 판단하지 않는다.
기존 canonical_gate의 shadow/enforce 파싱은 신규 강제 게이트 대체물이 아니다. 별도 검증 서비스와 실행 진입점 연결, 실패 차단 검증이 필요하다.
AAG 최신 snapshot/coverage는 이번에 검증하지 않았다. 구현 전 현재 SHA 기준 대조를 요구하며 “AAG 통과”로 표기하지 않는다.

## 화면과 조작
**주 경로(v2): MR-C1 채팅 메시지 목업 카드 → 같은 채팅 아티팩트 패널**(아래 §채팅 중심 검토 경로). 아래 MR-01 은 확대·공유용 보조 경로다.
MR-01 /design/reviews/{review_id} 신규 페이지(보조): 상단에는 프로젝트·화면명·revision·검토 상태, 본문에는 전체 목업/변경 전후 비교·모바일 보기·변경점, 옆에는 연결 문서·제출 조건·의견·승인/수정 버튼을 둔다.
초기 진입은 해당 검토 본문이다. 관리자 설정 메뉴로 먼저 보내지 않는다. 모바일은 단일 열과 넓은 버튼, 키보드 포커스와 오류 알림을 제공한다.
v1 시안(별도 페이지 중심, 보존): https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v1/index.html
v2 시안(채팅 중심): https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v2/index.html
데모에는 synthetic 예시 내용만 포함하며 승인 저장과 네트워크 요청은 하지 않는다. 클릭해도 운영 승인되지 않음을 상단/완료 표시 모두에 쓴다.

## 불변 패키지 계약
- scope: tenant_id, project, goal_id, review_id, change_type, screens[].screen_id/route/requirement_ids.
- 문서 참조: plan/prd/spec 각각 document_key + revision_id + content_hash. 역할상 불필요한 문서는 사유와 승인된 면제 정책 참조.
- assets[]: asset_id, immutable_uri, sha256, byte_size, mime, screen_id, phase(before/mockup/implemented), viewport, fixture_id, state, captured_at(KST 표시), capture_source, redaction_status.
- mockup_revision: 단조 증가 revision, parent_revision_id, manifest_hash, design_tokens_version, source_sha(있을 때), created_by/at.
- evidence[]: 브라우저 캡처/스냅샷, 로그인 사용 여부(비밀값 제외), route, 성공/실패, fallback HTTP/API/process 결과.
- submit의 manifest_hash는 서버가 정규화한 manifest와 실제 asset bytes의 해시로 계산한다. 클라이언트가 보낸 해시만 신뢰하지 않는다.
- 승인 아티팩트는 덮어쓰기 불가. 최신 링크는 탐색용이며 승인 기록에는 불변 revision URL만 저장한다. 모든 자식 파일/CSS/JS/이미지 해시까지 포함해 HTML만 고정하는 우회 방지.
- 비공개 실운영 캡처에는 개인정보를 마스킹하고 테넌트 권한을 적용한다. 불특정 공개 파일 경로로 올리지 않는다. 파일 fetch는 허용된 내부 객체 저장소만, 임의 URL SSRF 차단.

## 상태 전이
draft → review_ready: 필수 증거·문서 참조 검사 통과. draft 자체 저장은 허용한다.
review_ready → changes_requested: 승인권자 사유·대상 화면·revision 기록.
changes_requested → revising(수정중) → 새 draft revision → review_ready: 수정 항목 결과 보고 후 재제출; 과거 revision 불변. revising 은 change_request 단위 진행 표시이며 승인 상태가 아니다.
review_ready → approved: 승인권한 + 최신 generation + 문서/asset hash 일치 + 최종 확인. 같은 idempotency_key 재요청은 같은 결과.
approved → revoked: 권한 있는 명시 회수와 사유. 이력 보존. 구현 전 재검증 실패.
blocked_evidence/stale_revision/unauthorized/error는 표시·판정 결과이며 감사 상태와 분리한다.
새 초안은 이전 승인 이력을 삭제하지 않는다. 기존 승인본에 이미 고정된 작업은 자동으로 새 초안으로 이동하지 않는다. 동일 변경건의 수정 요청/회수 시 미실행 작업을 보류하고, 이미 실행 중인 작업은 체크포인트에서 정지/재검토한다. 임의 프로세스 kill은 하지 않는다.

## 채팅 중심 검토 경로 (v2)
### 고정 식별자
카드·패널·수정 요청·승인 요청은 모두 message_id, artifact_id, review_id, screen_id, revision, manifest/아티팩트 해시를 함께 싣는다. 사용자에게는 카드 머리에 같은 값을 표기한다(해시는 앞 12자 이상). 이 값이 하나라도 다르면 같은 대상으로 보지 않는다.

### 카드와 패널
- 카드: 화면명·revision·판정(검토 가능/증거 부족)·미승인 표시·“패널에서 열기”·(새 버전이 있으면) “v1/v2 비교”.
- 패널: 같은 채팅 아티팩트 패널(html_preview). 탭은 미리보기·비교·변경점. 구버전을 열면 “구버전 열람 중”과 최신 버전 이동을 항상 표시한다. 모바일은 같은 대화 문맥의 전체화면 패널이며 “대화로” 복귀 버튼을 둔다(대화 밖 별도 이동 아님).
- 패널의 정적 HTML 은 iframe sandbox="" + CSP script-src 'none' 안에서만 그려진다(현행 ChatArtifactPanel.tsx:2621-2622, htmlPolicy.ts). 따라서 패널 내용은 읽기 전용 문서이고 상호작용은 신뢰 영역이 맡는다.

### 신뢰 영역(승인 버튼 위치)
최종 승인·수정 요청 버튼은 **iframe 밖** 채팅 UI/서버 렌더 영역에 둔다. 이 영역은 승인 대상(review_id·screen_id·revision·해시)을 서버 값으로 직접 표시하고, 승인 요청은 서버가 revision·manifest_hash·expected_generation 을 재검증한다. HTML 안의 버튼·링크·postMessage 는 승인 근거가 아니다. sandbox 속성·CSP 를 완화하지 않으며(allow-scripts/allow-same-origin 금지) 승인 UI 구현을 위해 HTML 신뢰 범위를 넓히지 않는다.

### “방금 것 수정” 대상 결정
1. 답장(reply_to_id)으로 가리킨 카드, 없으면 현재 패널에서 선택·열람 중인 아티팩트를 대상으로 한다.
2. 그 문맥이 없고 같은 세션에 후보가 둘 이상이면 버전 선택을 **한 번만** 묻는다. 선택 결과는 해당 change_request 에 고정되어 같은 질문을 반복하지 않는다.
3. 후보 범위는 같은 세션·같은 review 로 한정한다. 다른 세션이나 다른 screen_id 는 “최신이라서”라는 이유로 선택하지 않는다.
4. 후보가 없으면 접수하지 않고 거절 사유를 표시한다.

### 수정 요청 기록과 흐름
- 기록: change_request_id, source_message_id(요청 메시지), base_revision, 대상 review_id/screen_id, 요청 본문, idempotency_key, 접수 시각(서버). 수정 요청은 **승인이 아니며 구현 명령이 아니다**.
- 흐름: 접수(원본 보존) → 수정중 → 새 불변 revision 생성 → 요청별 “반영/미반영+사유” 보고 → v1↔v2 전후 비교 → 재검토 요청. 새 revision 은 미승인이며 이전 승인을 승계하지 않는다.
- 같은 변경건 영향: 새 revision 이 생기면 그 변경건의 미실행 구현 작업은 보류, 실행 중 작업은 영향·체크포인트 재검토. 프로세스 kill/restart 는 하지 않는다. 승인된 번들 자체는 새 revision 이 승인되기 전까지 바뀌지 않는다.

## 예외 계약 (v2)
공통 규칙: 모든 쓰기는 idempotency_key 를 가지며, 대상 head 에는 단조 증가 generation 이 있다. 쓰기는 expected_generation 이 현재 generation 과 같을 때만 반영한다.
| 상황 | 계약 | 사용자 표시 |
|---|---|---|
| 생성 중 추가 요청 | 진행 중 작업의 입력 집합은 접수 시점에 고정. 새 요청은 새 change_request_id 로 대기열에 들어가고 완료 뒤 base_revision 을 최신으로 재계산 | “대기” 표시, 사용자 확인은 한 번 |
| 완료 직후 추가 요청 | head.generation 을 비교해 최신 기준이면 즉시 접수 | 접수 카드 |
| 오래된 카드로 승인 | revision·manifest_hash·expected_generation 중 하나라도 다르면 409 stale_revision, 승인 이벤트 0건 | 거절 + 최신 버전 열기 |
| 중복 전송 | 같은 change_request_id/idempotency_key 는 같은 결과를 반환, 새 revision·이벤트 증가 0 | “이미 접수됨” |
| 연속 요청 | source_message 서버 순서로 정렬(서버 시각 + message id). 같은 base 충돌은 순서대로 한 revision 으로 묶거나 다음 revision 으로 넘김 | 요청별 처리 결과 |
| 세션 재진입·재연결 | head 를 다시 조회, 대상 선택·미전송 입력 복원, 승인 상태는 서버 기준 | 복구 안내 |
| 승인 저장 실패 | 서버에 기록이 없음을 확인한 뒤 같은 idempotency_key 로 재시도, 중복 이벤트 금지. 실패 중에는 승인됨을 표시하지 않음 | 오류 + 입력·대상 유지 |
| 늦게 도착한 구버전 결과 | 결과의 generation 이 head 보다 낮으면 최신 포인터를 덮어쓰지 않고 보관만 | “덮어쓰지 않음” |
| 승인된 작업 진행 중 같은 변경건 수정 | 미실행 보류, 실행 중은 체크포인트 영향 재검토, kill/restart 금지 | 보류/재검토 표시 |

## 재사용 진입점(실재 확인, 이번에 구현하지 않음)
| 용도 | 위치 |
|---|---|
| 답장 대상·멱등 키 전송 필드 | aads-server app/models/chat.py:128-129 (reply_to_id, idempotency_key ≤64) |
| 채팅 아티팩트 조회·저장 | app/routers/chat.py:4590 GET /chat/artifacts, app/services/chat_service.py:11106 INSERT INTO chat_artifacts, :16248 list_artifacts |
| 패널 렌더·샌드박스 | aads-dashboard src/app/chat/ChatArtifactPanel.tsx:2621-2622, src/features/chat/rendering/htmlPolicy.ts(staticArtifactHtml, CSP), src/hooks/useArtifactPanel.ts |
| 문서 정본(버전·동시성) | app/api/canonical_documents.py (expected_generation, idempotency_key, content_hash) |
이 진입점은 위치 확인만 했고 운영 기능으로 구현하지 않았다. 문서 저장 규칙(R-DOC)은 복제하지 않고 기존 정본 문서를 따른다.

## 문서 의무·시연·운영 강제의 구분
- 문서 의무: 이 plan/prd/spec 이 요구하는 제출 규칙.
- 시연: mockup-v2.html(동작 데모), artifact-v2.html(정적 문서). 저장·네트워크·운영 승인 없음.
- 운영 강제: 서버 승인 게이트·실행 직전 검증·채팅 UI 변경. **이번에 구현하지 않았다.**

## 제안 API와 저장
기존 정본 API와 별도로 /api/v1/projects/{project}/mockup-reviews 경로를 제안한다.
| Method/하위 경로 | 계약 | 거절 |
|---|---|---|
| POST / | 초안 생성, goal/doc references, scope | 403 scope |
| POST /{id}/revisions | 불변 manifest + expected_generation + idempotency_key | 409 conflict |
| POST /{id}/submit | 전 화면·상태 증거 검사 | 422 missing_artifacts |
| GET /{id} | 최신 초안·승인본·변경점·미해결 의견 | 403 scope |
| POST /{id}/changes | revision(base_revision) + comment + source_message_id + change_request_id + idempotency_key + expected_generation | 422 empty_comment / 409 stale |
| POST /{id}/approve | revision + manifest_hash + expected_generation + idempotency_key | 409 stale_revision |
| POST /{id}/revoke | approval_id + reason + expected_generation | 403/409 |
| POST /{id}/verify | task scope·승인 번들 대조(내부 실행 권한) | 409 approval_required |

제안 테이블: mockup_review_heads, mockup_review_revisions, mockup_review_events, mockup_review_task_bindings. tenant/project 복합 외래키·중복 키·리비전 불변 제약 적용. 승인 저장은 head FOR UPDATE → generation 검사 → 서버 해시 검사 → 승인 이벤트/포인터/증가를 단일 트랜잭션으로 처리한다.
게이트 검증 증표는 짧은 유효기간과 approval generation을 갖고 실행 직전 재확인한다. DB 불가 시 affected UI task만 fail-closed; 전체 조회·운영 API를 중단하지 않는다.
이는 테이블을 생성하라는 실행 지시가 아니다. 기존 스키마·AAG 대조 후 마이그레이션을 별도 구현한다.

## 실행 경로별 강제
- 채팅: UI 작업 보고 필수 필드 검사, 미첨부면 검토 준비 미완료 표시. L1 규칙은 보조이며 서버 게이트와 구분.
- Runner: submit + worker 실행 직전 승인 번들 검증. pending 코드가 새 버전을 사용하면 재승인 요구.
- CLI/직접 작업: 공용 실행 명령과 CI에서 동일 검증. 임의 root 파일 쓰기까지 막는 보안 경계라고 주장하지 않는다.
- 정본 등록: 초안 저장을 차단하지 않는다. approved_ready/구현 착수와 혼동 금지.
- QA/배포: 구현 화면 비교 합격과 코드/배포 승인 별개. API 200은 시각 합격이 아니다.

## 보고 템플릿과 증거
필수 보고 순서: 판정(검토 가능/증거 부족) → 문서 3종 링크/키/리비전 → 화면 목록 → 신규 전체 목업 또는 실제 Before/After → 미리보기 → 변경점 → 승인 대상 정확한 버전 → 검증/미검증 → 요청 결정.
신규는 Before=해당 없음/신규 사유. 기존은 Before 출처·route·viewport·실측 KST가 반드시 있어야 한다. 캡처 실패 시 HTTP→API health→프로세스 순 폴백을 보고하되 시각 검수 미완료는 해제하지 않는다.
문서 등록·UI 승인·구현·배포 상태를 구분한다. 이 패키지 자체도 미리보기·데스크톱/모바일 캡처·manifest·동작 검증을 함께 제공해야 제출 완료다.

## 롤아웃·롤백
이번 턴: 문서 3종 v2 초안과 v2 시안 등록/검증만. mockup은 화면 설계 산출물이며 실제 승인 API가 아니다. 기존 승인본 지정은 변경하지 않는다.
구현 후: 대상 프로젝트에서 shadow 증거 수집 → 거절 시나리오 검증 → 명시 승인된 범위에서 enforcement 활성화. 전 세션 반영은 provenance와 진입점 통합 테스트로 입증한다.
API 릴리스는 기존 bluegreen 계약을 따른다. 롤백은 운영 라우팅 복귀, 신규 gate mode 복구 및 승인 대상 UI 작업 보류; 감사 데이터는 삭제하지 않는다. 문서 롤백은 새 revision으로 복원하며 기존 승인본 유지.


## 검토 패키지 v1
- [클릭 가능한 목업](https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v1/index.html) · [데스크톱 전체 캡처](https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v1/evidence-v1/desktop.png) · [모바일 전체 캡처](https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v1/evidence-v1/mobile.png)
- 원본 HTML: `docs/specs/rdoc-mockup-review/mockup-v1.html`. 게시 사본은 같은 바이트로 보존하고 manifest로 대조한다.
- 시연 승인과 실제 승인은 분리한다. 이 문서와 시안은 초안이며 승인본 자동 지정·운영 강제 게이트 활성화를 수행하지 않았다.

## 검토 패키지 v2 (채팅 중심 개정)
- 동작 시안(별도 페이지, 보조): https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v2/index.html · 원본 `mockup-v2.html`
- 채팅 아티팩트용 정적 문서(CSS 전용): `artifact-v2.html` (src: `artifact-v2.src.html`, 생성기: `build_v2.py`)
- 증거: `evidence-v2/` — 데스크톱 1440×900·모바일 390×844 흐름 캡처, 패널 렌더 캡처, 실제 /chat Before 시도(로그인 화면 이동 → blocked_evidence) 캡처, verification-v2.json. 요약은 `VERIFICATION-v2.md`, 해시는 `manifest-v2.json`.
- 실제 /chat Before 는 마스킹된 로그인 세션이 없어 확보하지 못했다(blocked_evidence). 합성 화면을 Before 로 표기하지 않는다.
- v1 패키지는 변경하지 않았다. 공통 HANDOVER.md·docs/HANDOVER.md 는 runner-ff043498 완료 전이라 수정하지 않았고, 기록은 HANDOVER-v2.md 와 DB 핸드오버에 있다.
