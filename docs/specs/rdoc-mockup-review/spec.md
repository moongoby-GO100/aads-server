# R-DOC 목업 필수 제출·승인 — 설계 명세
- 프로젝트: AADS · 버전: 1.0.1 · 작성일: 2026-10-03 KST
- 상태: 검토용 초안. 문서 작성 지시는 승인됐지만 이 설계·시안의 구현 승인은 아직 없다.
- 상위 목표: 0361c451-cc03-4bd1-a423-76051b0546b2 (기존 R-DOC 목표, 조회 확인)
- 근거: CEO의 “화면 구현이 필요한 기획·설계·PRD는 목업까지, 기존 화면 수정은 전후 페이지까지 보고” 지시.
- 이 패키지는 R-DOC의 목업 제출·승인 기능에 대한 독립 승인 단위다. 기존 저장 규칙 문서를 복제하거나 기존 document_key/승인본을 교체하지 않는다.

- document_key: rdoc-mockup-review-spec
- 연결: [기획](../../plans/20261003_AADS_RDOC_MOCKUP_REVIEW_PLAN.md), [PRD](../../prd/20261003_AADS_RDOC_MOCKUP_REVIEW_PRD.md)
- 아래 API·DB·상태·게이트는 신규 설계다. 실제 존재하는 기능은 “현재 기반”에만 적는다.

## 현재 기반과 확장 경계
app/api/canonical_documents.py는 문서 revision, expected_generation, tenant/project 권한, approve/review와 감사 이벤트를 지원한다. 이를 문서 참조와 권한 모델의 기반으로 재사용한다. design_modifications.py의 상태 문자열 approved 존재만으로 목업 승인 전이를 구현했다고 판단하지 않는다.
기존 canonical_gate의 shadow/enforce 파싱은 신규 강제 게이트 대체물이 아니다. 별도 검증 서비스와 실행 진입점 연결, 실패 차단 검증이 필요하다.
AAG 최신 snapshot/coverage는 이번에 검증하지 않았다. 구현 전 현재 SHA 기준 대조를 요구하며 “AAG 통과”로 표기하지 않는다.

## 화면과 조작
MR-01 /design/reviews/{review_id} 신규 페이지: 상단에는 프로젝트·화면명·revision·검토 상태, 본문에는 전체 목업/변경 전후 비교·모바일 보기·변경점, 옆에는 연결 문서·제출 조건·의견·승인/수정 버튼을 둔다.
초기 진입은 해당 검토 본문이다. 관리자 설정 메뉴로 먼저 보내지 않는다. 모바일은 단일 열과 넓은 버튼, 키보드 포커스와 오류 알림을 제공한다.
이번 시안: https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v1/index.html
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
changes_requested → 새 draft revision → review_ready: 수정 항목 결과 보고 후 재제출; 과거 revision 불변.
review_ready → approved: 승인권한 + 최신 generation + 문서/asset hash 일치 + 최종 확인. 같은 idempotency_key 재요청은 같은 결과.
approved → revoked: 권한 있는 명시 회수와 사유. 이력 보존. 구현 전 재검증 실패.
blocked_evidence/stale_revision/unauthorized/error는 표시·판정 결과이며 감사 상태와 분리한다.
새 초안은 이전 승인 이력을 삭제하지 않는다. 기존 승인본에 이미 고정된 작업은 자동으로 새 초안으로 이동하지 않는다. 동일 변경건의 수정 요청/회수 시 미실행 작업을 보류하고, 이미 실행 중인 작업은 체크포인트에서 정지/재검토한다. 임의 프로세스 kill은 하지 않는다.

## 제안 API와 저장
기존 정본 API와 별도로 /api/v1/projects/{project}/mockup-reviews 경로를 제안한다.
| Method/하위 경로 | 계약 | 거절 |
|---|---|---|
| POST / | 초안 생성, goal/doc references, scope | 403 scope |
| POST /{id}/revisions | 불변 manifest + expected_generation + idempotency_key | 409 conflict |
| POST /{id}/submit | 전 화면·상태 증거 검사 | 422 missing_artifacts |
| GET /{id} | 최신 초안·승인본·변경점·미해결 의견 | 403 scope |
| POST /{id}/changes | revision + comment + expected_generation | 422 empty_comment |
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
이번 턴: 문서 3종 초안과 시안 등록/검증만. mockup은 화면 설계 산출물이며 실제 승인 API가 아니다.
구현 후: 대상 프로젝트에서 shadow 증거 수집 → 거절 시나리오 검증 → 명시 승인된 범위에서 enforcement 활성화. 전 세션 반영은 provenance와 진입점 통합 테스트로 입증한다.
API 릴리스는 기존 bluegreen 계약을 따른다. 롤백은 운영 라우팅 복귀, 신규 gate mode 복구 및 승인 대상 UI 작업 보류; 감사 데이터는 삭제하지 않는다. 문서 롤백은 새 revision으로 복원하며 기존 승인본 유지.


## 검토 패키지 v1
- [클릭 가능한 목업](https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v1/index.html) · [데스크톱 전체 캡처](https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v1/evidence-v1/desktop.png) · [모바일 전체 캡처](https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v1/evidence-v1/mobile.png)
- 원본 HTML: `docs/specs/rdoc-mockup-review/mockup-v1.html`. 게시 사본은 같은 바이트로 보존하고 manifest로 대조한다.
- 시연 승인과 실제 승인은 분리한다. 이 문서와 시안은 초안이며 승인본 자동 지정·운영 강제 게이트 활성화를 수행하지 않았다.
