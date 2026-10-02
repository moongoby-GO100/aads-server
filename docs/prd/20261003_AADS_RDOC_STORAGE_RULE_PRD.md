# OHVIS 문서 정본관리 게이트 — 운영 PRD

- 작성 2026-09-22 KST · 배경: [기획서](../plans/20260922_OHVIS_DOC_GOVERNANCE_OPS_기획서.md)
- 상위 계약: [20260914 PRD](20260914_OHVIS_DOC_SYSTEM_PRD.md) S1~S5 (원본, 이 문서는 대체하지 않음)

## 담당 범위

| 자산 | 정본 위치 |
|---|---|
| 기획서·PRD·설계서 대장 | DB `goal_documents` |
| 문서 표류 게이트 | `scripts/doc_drift_check.py` + pre-commit |
| Auto-RAG 색인 정책 | `scripts/index_docs.py` + `docs/prd/20260914_OHVIS_DOC_SYSTEM_PRD.md` S1-2 |

## 요구사항

| ID | 요구 |
|---|---|
| OPS-1 | `document_key` 충돌(같은 키, 다른 goal_id, 둘 다 is_latest=true)은 발견 즉시 백필한다. 삭제하지 않고 재계산한다 |
| OPS-2 | 문서표류 게이트를 경고→차단으로 올리기 전, 전체 스캔이 종료코드 0(표류 없음)임을 실측으로 확인한다 |
| OPS-3 | 색인 대상 확장자 예외(현재: `app/static/reports/*.html`)를 코드에서 제거하려는 diff는 PRD 예외 조항과 대조해 PRESERVATION 위반 여부를 먼저 판정한다 |
| OPS-4 | 이 게이트가 수행한 조치(백필·정정·정책결정)는 이 PRD를 부모로 하는 `goal_documents`에 `change_summary`로 남긴다 |
| OPS-5 | 게이트 차단 승격의 최종 스위치는 CEO 승인 카드(`propose_next_steps`)를 거친다. 백필·반려처럼 가역적 무결성 조치는 게이트 담당 권한으로 즉시 처리한다 |

## 수용 기준

- `goal_documents`에 이 PRD·기획서가 정본으로 등록되어 있다
- 이후 이 게이트 관련 조치 보고에 `entry_key`/문서 참조가 포함된다

## R-DOC 문서 저장 규칙 — 요구사항·수용 기준

> 개정 근거: 대상 head `prd:848c81d57565` 는 "OHVIS 문서 정본관리 게이트 — 운영 PRD"(AADS)이고, 위 담당 범위 표에 `goal_documents` 대장·문서 표류 게이트가 있다. R-DOC 는 그 직접 연장이므로 같은 head 의 새 revision 으로 등록한다 [DB 조회: project_document_heads 2c4e95fb…, 2026-10-03].
> version 1.2.0 (직전 승인본 1.0.0 = revision 1. 1.1.0 = 저장 규칙 절, 1.2.0 = 아래 화면 개발 절 추가이며 둘 다 승인 전 초안 내용). 위쪽 승인본 원문은 글자 그대로이며 sha256 `8cacc274a62a1108001211109139c32c13dbd1b57e4d09f594d344b011ab1d7f` 이다 [DB 조회: project_document_revisions 686c817c… 의 content_hash].
> 배경과 로드맵: `docs/plans/20261003_AADS_RDOC_STORAGE_RULE_PLAN.md`(1.2.0). 저장 위치·파일명·등록 절차·게이트 현황: `docs/specs/rdoc-storage-rule/spec.md`(1.1.0 의 1~5항). 화면 개발 절차의 상세 계약: 같은 spec 의 6~14항.

### 요구사항

| ID | 요구 |
|---|---|
| RDOC-1 | 정본 후보 문서는 spec 의 저장 위치 표에 따른 경로에 둔다. 저장소 루트 `reports/` 와 `uploads/chat` 에 신규 정본 문서를 두지 않는다 |
| RDOC-2 | 파일명 규칙은 CEO 가 spec 의 비교표에서 하나로 정한다. 정하기 전에는 새 규칙을 현재 동작처럼 적용하지 않는다 |
| RDOC-3 | 문서를 만들면 정본 API(`POST /api/v1/projects/{project_key}/documents`)로 초안 revision 을 등록한다. `document_key` 는 소문자 kebab `{주제}-{kind}` 이고 슬래시·날짜·버전을 넣지 않는다. 프로젝트는 `project_key` 로 구분하고 `kind` 를 명시한다 |
| RDOC-4 | 채팅 등록 도구는 초안 등록까지만 한다. 승인(`approve`)은 기존 승인 경로를 쓴다. 도구는 미구현이며 runner-0d1b55b7 로 제출돼 있다 [DB 조회: pipeline_jobs, 2026-10-03] |
| RDOC-5 | 갱신은 새 파일이 아니라 같은 `document_key` 의 새 revision 이다. 날짜만 바꾼 사본 파일을 만들지 않는다 |
| RDOC-6 | 완료 보고에 `document_key` 를 적는다. 등록하지 못했으면 "정본 미등록"으로 분리 보고한다. 채팅 본문에만 남기고 "문서 저장 완료"라고 보고하지 않는다 |
| RDOC-7 | 정본 게이트는 off 와 shadow 두 단계만 있다. off 는 DB 쿼리 0건, shadow 는 경고·기록만 하며 300ms 상한이고 모든 오류에서 fail-open 이다 [코드 확인: app/services/canonical_gate.py] |
| RDOC-8 | enforce 는 미구현이다. 구현·활성화는 shadow 실측 뒤 CEO 별도 승인이며 기존 OPS-5 와 같은 승인 경로를 쓴다 |
| RDOC-9 | 이 기획서·PRD·spec 자신도 정본으로 등록한다(OPS-4 정합). 등록은 커밋 뒤 CEO 세션이 한다 |

### 수용 기준

| ID | 기준 | baseline | 출처 |
|---|---|---|---|
| AC-1 | 정본 등록률 = (7일 창에서 등록된 신규 정본 후보 문서 수) / (같은 창의 신규 정본 후보 문서 수). 목표값은 CEO 가 정한다 | 0건/17건(heads 기준), 1건/17건(`goal_documents` 기준) | [지시서 인용] 목표값 [미측정] |
| AC-2 | 이중 정본 0건: 같은 `source_path` 를 가리키는 서로 다른 `document_key` head 가 없고, 같은 문서가 다른 경로에 사본으로 남아 있지 않다 | [미측정] | 측정 쿼리는 아래 검증 방법 |
| AC-3 | 파일명 규칙 준수율. CEO 가 규칙을 정한 뒤부터 센다 | 2건/17건(현행 FLOW 기준) | [지시서 인용] |
| AC-4 | shadow 는 어떤 요청·커밋도 막지 않는다. 판정·기록 오류 주입 시에도 원래 흐름이 성공한다 | 단위 테스트가 이미 다룬다 | [코드 확인: tests/unit/test_canonical_gate_entrypoints.py] |
| AC-5 | shadow 기록(`canonical_gate_events`)이 판단 가능한 표본 수에 도달한다. 표본 수 기준은 CEO 가 정한다 | 1행 | [DB 조회] 기준 [미측정] |

### 검증 방법

- AC-1: 7일 창의 신규 파일은 `git log --since='7 days ago' --diff-filter=A --name-only -- docs reports`, 등록 여부는 `project_document_revisions.source_path` 와 대조한다. 읽기 전용이다.
- AC-2: `project_document_revisions` 를 `(project_key, source_path)` 로 묶어 서로 다른 `head_id` 가 2개 이상인 행을 센다. 읽기 전용이다.
- AC-3: 신규 파일명을 spec 이 정한 규칙의 정규식과 대조한다.
- AC-4·5: `bash scripts/run_unit_tests.sh tests/unit/test_canonical_gate_entrypoints.py`, `SELECT verdict, count(*) FROM canonical_gate_events GROUP BY 1`.
- 이 문서 3종의 보존·내용 검증: `bash scripts/run_unit_tests.sh tests/unit/test_rdoc_docs.py`.

### 롤백

- 문서 개정: 1.1.0 revision 은 승인 전에는 `latest` 일 뿐이고 `approved` 는 1.0.0 을 유지한다. 승인한 뒤 되돌리려면 `archive` 로 `approved_revision_id` 를 비우거나 이전 내용으로 새 revision 을 올려 승인한다. `approve` 는 latest revision 만 받으므로 옛 revision 을 직접 재승인할 수 없다 [코드 확인: app/api/canonical_documents.py:294-315]. 이력은 지워지지 않는다.
- shadow 게이트: 환경변수 `CANONICAL_GATE_MODE=off` 로 끈다. off 는 DB 를 건드리지 않는다 [코드 확인].
- L1 R-DOC 프롬프트 자산: 롤백 마이그레이션 `migrations/20261003_l1_doc_storage_rule_draft_rollback.sql` 이 있다 [코드 확인]. 활성화 전에는 `enabled=false` 라 어떤 세션에도 주입되지 않는다.
- 이 3종 파일: 일반 git revert. 정본 등록 전이면 DB 영향이 없다.

## R-DOC 화면 개발 절차와 디자인 조사 — 요구사항·수용 기준 (RDOC-UI)

> 1.2.0 추가. 위 RDOC-1~9 와 AC-1~5 의 번호·의미·`document_key` 는 바꾸지 않고 새 ID 만 더한다. 상세 계약은 spec 6~14항이다. 근거: CEO 범위 결정 "R-DOC 기획·설계·PRD에 위 화면 절차를 반영" [지시서 인용: AADS-RDOC-UI-WORKFLOW-DESIGN-REFERENCES-20261003, GOAL_ID 0361c451-cc03-4bd1-a423-76051b0546b2]. 아래는 설계 계약이며 현재 구현된 동작이 아니다.

### 요구사항 (RDOC-UI)

| ID | 요구 | spec |
|---|---|---|
| RDOC-UI-1 | 사용자 화면을 새로 만들거나 화면·상호작용을 바꾸는 요구사항에 화면 절차를 적용한다. backend-only 는 "UI 불필요" 와 이유를 PRD 에 적는다. 화면 명세는 기능 단위 plan·prd·spec 에 연결하고, 독립 변경·별도 승인이 필요할 때만 design 문서로 분리한다 | 6항 |
| RDOC-UI-2 | 화면 명세에 대상 사용자, 첫 진입, 반복 사용, 실패 복구, 범위/비범위를 적는다 | 6항 |
| RDOC-UI-3 | 절차는 PM 분류 → 사용자 흐름·PRD 초안 → 디자인 담당 조사·시안 → 아키텍트 API·DB·권한·AAG 대조 → 정본관리자 revision 연결 → 승인권자 기준 확정 → 개발자 구현 → QA 실제 브라우저 검수 → 별도 승인 릴리스·사후 검증 순이다. 작은 변경은 기존 승인 디자인을 재사용하고 신규·중대한 시각 변경만 시안을 비교한다. 새 담당자 계정이나 목표 상태를 임의로 만들지 않는다 | 7항 |
| RDOC-UI-4 | 추적 필드(goal_id, milestone_id(확인된 경우), requirement_id, screen_id/route, document_key/revision_id, mockup asset/version, design-system version, context-pack reference, task/job, implementation SHA, test/evidence URL, reviewer/verdict)를 설계 계약으로 정한다. 현재 API 가 모두 지원한다고 쓰지 않는다. approved 와 latest 를 구분하고 최신 초안이 실행 기준을 자동으로 교체하지 않는다 | 8항 |
| RDOC-UI-5 | 기능 착수·주요 리디자인 때 공식 디자인 시스템과 동종 실제 서비스 사례를 조사한다. 참조마다 원 URL, 발행/갱신일(없으면 시점 불명), KST 확인일, 버전, 캡처 가능 여부, 적용 요소, 제외 이유, 라이선스·출처를 기록한다. 최신 글만 모으지 않고 기존 브랜드·업무·접근성·성능에 맞는지 비교한다 | 9항 |
| RDOC-UI-6 | 조사 갱신은 새 revision 제안으로 올리고 승인된 디자인을 자동으로 덮어쓰지 않는다. 자동 스케줄을 만들지 않는다. 큰 신규 화면은 기존 체계 유지안과 선별 트렌드 적용안을 비교해 권장안을 적고, 사소한 변경마다 두 시안을 요구하지 않는다 | 9항 |
| RDOC-UI-7 | 설계 패키지에 와이어프레임, 핵심 화면 시안, 이동·상태 전이, 색·폰트·간격·모서리·모션 토큰, 재사용 컴포넌트, 반응형, 한국어 장문, loading/empty/error/permission/session-expired/offline 상태를 넣는다. 첫 화면은 핵심 업무 우선이고 설정·권한은 보조이며, 한 손 조작·터치·세션 복구를 포함한다. 검증 목표는 명세(목표)로 표시하고 실측 성과로 쓰지 않는다 | 10항 |
| RDOC-UI-8 | QA 는 기능·시각·접근성·성능을 각각 판정하고 값은 통과/불합격/평가불가이다. 접근성은 WCAG 2.2 AA 로 검토한다. 시각은 승인 시안과 같은 viewport·fixture 의 Playwright 캡처 비교와 사람 판단이다. 채점기 장애·인증 실패는 평가불가이고 디자인 불합격이 아니다. API 200 으로 시각 검수 성공을 선언하지 않으며, 브라우저 실패 시 HTTP·API health·프로세스 결과를 따로 기록하고 시각 검수는 미완료로 둔다. 현재 운영 게이트와 새 제안을 구분하고 기존 게이트를 우회하는 구현은 하지 않는다 | 11항 |
| RDOC-UI-9 | 문서 승인·디자인 승인·코드 검수·배포 승인은 별개다. 신규 route 는 가능하면 프리뷰·후보 환경에서 먼저 검증한다. 승인된 사후 검증 예외는 해당 계약과 증거를 참조만 하며 이 문서로 승인한 것으로 보지 않는다 | 12항 |
| RDOC-UI-10 | 현재 기반(`app/api/design_modifications.py`, `app/services/design_context_pack_service.py` 의 screen·allowed_scope·acceptance_criteria)과 미구현을 구분한다. 2026-10-03 `aag_brief` 는 stale / not_proven, coverage=0 이라 구조 정합 통과 근거로 쓰지 않고, 최신 SHA 고정 AAG 갱신과 코드 대조를 후속 검수 조건으로 둔다. 앱 코드와 AAG 스캐너는 수정하지 않는다. 아키텍트 질문(relay_id=2f575da0-1613-40ba-9fe3-594d4c5f4fc4)의 회신 미수신을 합의 완료로 쓰지 않는다. 실제 화면 구현, 디자인 자동 변경, L1 활성화, 정본 승인, enforce 구현·활성화(enforce 는 미구현)는 범위 밖이다 | 13항 |
| RDOC-UI-11 | 외부 1차 근거(Google Expressive Material Design, Apple HIG Materials, WCAG 2.2 목표 크기·포커스 가림)는 확인한 범위와 날짜만 적고 업계 최신 순위를 주장하지 않는다. Google 자료의 수치를 당사 성과로 인용하지 않고, Apple 가이드를 웹 필수 효과로 삼지 않는다. OHVIS 방향은 가독성·작업 상태·핵심 행동이 뚜렷한 업무 화면이고 장식·투명 효과는 검증 뒤 제한 적용한다는 제안이다 | 14항 |

### 수용 기준 (RDOC-UI)

기준은 문서 검수로 대조하며, 화면 구현 결과는 이 작업의 범위가 아니라 측정값이 없다. 아래 baseline 은 모두 [미측정] 이다.

| ID | 대응 | 기준 | 확인 방법 |
|---|---|---|---|
| AC-UI-1 | RDOC-UI-1 | 화면 요구사항마다 화면 필요성 분류가 있고, backend-only 에는 "UI 불필요" 사유가 있다. design 문서 분리는 독립 변경·승인 사유가 있을 때만이다 | spec 6항 표와 PRD 본문 대조 |
| AC-UI-2 | RDOC-UI-2 | 화면 명세에 필수 항목 5개가 모두 있다 | spec 6항 목록 대조 |
| AC-UI-3 | RDOC-UI-3 | 단계 9개에 담당과 산출물이 있고, 새 계정·목표 상태를 만들지 않는다고 적혀 있다 | spec 7항 표 대조 |
| AC-UI-4 | RDOC-UI-4 | 필드 12개가 모두 정의돼 있고, "현재 지원" 서술이 없으며, approved/latest 구분과 자동 교체 없음이 적혀 있다 | spec 8항 표와 목록 대조 |
| AC-UI-5 | RDOC-UI-5 | 참조 기록 항목 8개가 모두 있고, 조사 대상에 공식 디자인 시스템과 동종 서비스가 둘 다 있다 | spec 9항 표 대조 |
| AC-UI-6 | RDOC-UI-6 | 새 revision 제안으로만 갱신, 자동 덮어쓰기·자동 스케줄 없음, 큰 신규 화면에만 두 안 비교, 사소한 변경은 비교 요구 없음이 적혀 있다 | spec 9항 목록 대조 |
| AC-UI-7 | RDOC-UI-7 | 패키지 항목 전부와 상태 6종이 있고, 검증 목표가 실측 성과로 쓰이지 않는다 | spec 10항 대조 |
| AC-UI-8 | RDOC-UI-8 | 4축 판정, WCAG 2.2 AA, 같은 viewport·fixture 비교, 평가불가 규칙, API 200 불인정, 폴백 기록 분리, 기존 게이트 구분이 모두 있다 | spec 11항 대조 |
| AC-UI-9 | RDOC-UI-9 | 승인 4종이 별개로 적혀 있고, 사후 검증 예외가 이 문서로 승인되지 않는다고 적혀 있다 | spec 12항 대조 |
| AC-UI-10 | RDOC-UI-10 | 기반과 미구현이 구분돼 있고, AAG 후속 조건과 회신 미수신 서술이 있다. 코드 diff 에 앱 코드·AAG 스캐너 변경이 없다 | spec 13항 대조 + `git diff --stat` |
| AC-UI-11 | RDOC-UI-11 | 외부 근거 3종에 원 URL 과 확인일이 있고, 성과 수치 인용·웹 필수 강제가 없다 | spec 14항 대조 |
| AC-UI-12 | RDOC-UI-1~11 전체 | 승인 prefix sha256 이 보존되고 RDOC-1~9, AC-1~5 의 본문이 변하지 않았다. 문서 3종의 버전(1.2.0 / 1.2.0 / 1.1.0)과 상호 참조가 일치한다 | `bash scripts/run_unit_tests.sh tests/unit/test_rdoc_docs.py`, `git diff` |

### 검증 방법 (RDOC-UI)

- 문서 보존·내용: `bash scripts/run_unit_tests.sh tests/unit/test_rdoc_docs.py`, `git diff --check`.
- AC-UI-1~11 은 사람이 위 표의 확인 방법으로 대조한다. 자동 테스트는 이 요구사항을 따라 쓰지 않는다(구현이 없다).

### 롤백 (RDOC-UI)

- 이 절은 문서 diff 이며 일반 git revert 로 되돌린다. 승인된 기존 revision 은 그대로 유지된다. 정본 draft revision 을 올렸다면 approved 포인터는 건드리지 않았고, 되돌릴 때는 위 롤백 절차(새 revision 또는 archive)를 따른다.
