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
