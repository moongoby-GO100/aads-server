# OHVIS 문서 정본관리 게이트 — 목표·마일스톤 계층 설계서

_v1.0.0 | 2026-09-22 | 담당 역할: PM(기획/설계/PRD 게이트 정본관리자)_

> 상위 문서: `docs/plans/20260922_OHVIS_DOC_GOVERNANCE_OPS_기획서.md`,
> `docs/prd/20260922_OHVIS_DOC_GOVERNANCE_OPS_PRD.md`
> 이 문서는 위 두 문서의 실행 계층(목표→하위목표→마일스톤)을 DB 정본과 1:1로 고정한다.

---

## 1. 왜 계층이 필요했나

2026-09-22 04:39 UTC 실측 기준, 이 게이트를 운영하는 역할에게 **마일스톤이 0건**이었다
(`my_milestones(scope=all)` → `count: 0`). 목표 레코드
`OHVIS 문서 정본관리 게이트 운영`은 존재했으나 `owner_role_key`·`owner_session_id`·
`success_criteria`가 전부 NULL이어서, 담당이 역할로 걸리지 않았고 완료 판정 기준도 없었다.

결과적으로 게이트 작업은 러너 단건 제출로만 흘렀고, **무엇이 끝났는지 DB로 판정할 수 없는
상태**가 유지됐다. 문서 정본을 강제하는 역할이 자기 작업에 대해서는 정본 계층을 갖고 있지
않았다는 뜻이다.

---

## 2. 3계층 구조

| 계층 | 테이블 | 개수 | 의미 |
|---|---|---|---|
| L0 목표 | `goals` (`parent_goal_id IS NULL`) | 1 | 게이트 운영 전체. 성공기준 4항 |
| L1 하위목표 | `goals` (`parent_goal_id` = L0) | 4 | 업무 영역 분할(DG1~DG4) |
| L2 마일스톤 | `milestones` (`goal_id` = L1) | 10 | 판정 가능한 단위. `completion_criteria` 필수 |

GO100 `#310·#119` 목표가 쓰는 것과 같은 패턴이다(ST1~ST5 하위목표 → ST4.1 마일스톤).
계층을 새로 만들지 않고 기존 구조를 따른다.

### L0 — OHVIS 문서 정본관리 게이트 운영

- `goal_id`: `a6cc6511-ac41-4733-bbb7-ddd58ba05912`
- `owner_role_key`: `PM`
- 성공기준 4항
  1. 정본 대장에 기획문서가 빠짐없이 `is_latest=true`로 등록되고 이중 정본 0건
  2. `doc_drift_check.py` 전체 스캔 EXIT=0이 pre-commit 차단으로 강제됨
  3. 정본 문서가 `doc_chunks`에 색인되어 Auto-RAG 조회 가능
  4. 게이트 우회·오진 사례가 `ohvis_wiki_error_book`에 등록되어 재발 시 자동 매칭

### L1 — 하위목표 4개

| 키 | 제목 | 우선순위 | 하위목표 완료기준 |
|---|---|---|---|
| DG1 | 정본 대장 완전성 — 등록 누락·이중 정본 제거 | P1 | specs 99건 등록 AND 이중 정본 0 |
| DG2 | 게이트 강제력 — 경고를 차단으로 승격 | P1 | drift EXIT=0 상태에서 차단 동작 AND 저장소본=설치본 |
| DG3 | 검색·색인 신뢰성 — Auto-RAG 정합 | P2 | 정본 doc_path별 청크수 > 0 AND 확장자 정책 = PRD S1-2 |
| DG4 | 운영 회귀 방지 — 오진·우회 사례의 사전 등록 | P2 | 확인된 원인이 error_book에 등록되고 signature 매칭 |

### L2 — 마일스톤 10건

| 키 | 제목 | 상태 | 완료기준(판정 쿼리·명령) |
|---|---|---|---|
| DG1.1 | docs/specs 99건 정본 대장 등록 | in_progress | `goal_documents` 중 `doc_path LIKE '%/docs/specs/%' AND is_latest` = 99 |
| DG1.2 | 이중 정본 3건 해소 | pending | 동일 `doc_path`가 2개 이상 goal에 active·is_latest인 건수 0 |
| DG1.3 | 정본 등록 API 중복등록 결함 수정 | completed | `app/routers/goals.py` 수정본 origin/main 반영 + health HEALTHY |
| DG2.1 | 문서 표류 27건 정정 후 pre-commit 차단 승격 | in_progress | drift 전체 스캔 EXIT=0 AND 표류 시 커밋 차단 |
| DG2.2 | 훅 저장소본↔설치본 동기화 강제 | pending | `diff -q scripts/hooks/pre-commit .git/hooks/pre-commit` → SAME |
| DG2.3 | analyze/converge 수렴 미완 하드 게이트 | in_progress | `owner_resolved` 아닌 목표의 구현 러너 제출이 실제 거부 |
| DG3.1 | 색인 대상 확장자 .md 전용 정책 적용 | blocked | 수집 확장자 `.md` 단일 AND 기존 비-md 청크 처리 방침 확정 |
| DG3.2 | 신규 등록 정본 문서 색인 반영 | pending | 이 목표의 각 `doc_path`에 대해 `doc_chunks` 청크수 > 0 |
| DG4.1 | 리뷰 인프라 연속 장애 규명 | in_progress | 원인 확정 후 error_book 등록 AND signature가 실제 로그와 매칭 |
| DG4.2 | 러너 허위 실패원인 보고 사례 등록 | pending | error_book에 키 등록 + prevention/fix 분리 기재 |

---

## 3. 상태 전이와 판정 권한

```
pending ──착수──> in_progress ──신고──> (report_milestone_done)
                      │                        │
                   차단 발생                 주도 판정
                      ↓                        ↓
                  blocked                completed / 반려
```

- **자기가 담당인 마일스톤은 자기가 판정하지 않는다.** `confirm_milestone`은 주도 전용이고,
  담당이 자기 것을 판정하려 하면 대표님께 올라간다.
- `blocked`는 실패가 아니라 **외부 결정 대기**다. DG3.1이 여기 해당한다(색인 정책은 CEO 결정 사항).
- 완료기준은 문장이 아니라 **실행 가능한 쿼리·명령**으로 적는다. "개선한다", "보강한다"는
  완료기준이 아니다 — 무엇을 보면 끝났는지 판정할 수 없기 때문이다.

---

## 4. 이 계층이 막는 실패

| 실패 유형 | 이전에 실제로 일어난 일 | 계층이 막는 방식 |
|---|---|---|
| 완료 판정 불가 | 러너가 `done`이어도 운영 검증이 빠진 채 완료로 보고됨 | `completion_criteria`를 쿼리로 고정 |
| 중복 제출 | 같은 목적 작업이 R2·R3로 반복 제출됨 | 마일스톤 1건에 시도 이력을 귀속 |
| 미결 유실 | CEO 결정 대기 항목이 대화에서 밀려 사라짐 | `blocked` 상태로 DB에 남김 |
| 자기지시 공백 | 게이트 운영자 자신이 대장 밖에서 일함 | 이 문서를 goal_documents 정본으로 등록 |

---

## 5. 문서 반영 규칙

이 설계서는 L0 목표의 정본 문서로 `goal_documents`에 `kind=design`으로 등록된다.
계층이 바뀌면(하위목표 추가, 마일스톤 증감, 완료기준 변경) **이 문서를 먼저 고치고
새 version으로 등록한 뒤 DB를 바꾼다.** 순서를 뒤집으면 문서가 표류하고, 표류는
`doc_drift_check.py`가 잡는다.
