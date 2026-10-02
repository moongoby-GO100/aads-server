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
> version 1.1.0 (직전 승인본 1.0.0 = revision 1). 위쪽 승인본 원문은 글자 그대로이며 sha256 `8cacc274a62a1108001211109139c32c13dbd1b57e4d09f594d344b011ab1d7f` 이다 [DB 조회: project_document_revisions 686c817c… 의 content_hash].
> 배경과 로드맵: `docs/plans/20261003_AADS_RDOC_STORAGE_RULE_PLAN.md`. 저장 위치·파일명·등록 절차·게이트 현황: `docs/specs/rdoc-storage-rule/spec.md`.

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
