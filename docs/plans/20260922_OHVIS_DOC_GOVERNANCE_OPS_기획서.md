# OHVIS 문서 정본관리 게이트 — 운영 기획서

- 작성 2026-09-22 KST · 작성자: AADS CTO AI (문서 정본관리 게이트 담당)
- 관련 계약: [PRD](../prd/20260914_OHVIS_DOC_SYSTEM_PRD.md)(S1~S5), [PRD(운영판)](../prd/20260922_OHVIS_DOC_GOVERNANCE_OPS_PRD.md)

## 왜 이 문서가 지금 생기는가

CEO 지시("기획 설계 PRD 작성을 안 하지?")로 드러난 공백을 메운다. 이번 대화에서
정본 중복 3건 백필, git 발산 정리, index_docs.py 정책 결정, 문서표류 27건 러너
제출까지 **실행(Operate)만 하고 설계(Layout) 문서를 남기지 않았다** — 담당
역할이 "문서 정본관리 게이트"인데 그 게이트 자신의 작업 이력이 게이트에
등록되지 않은 자기지시적 공백이었다.

## 목표

1. 오비스 문서 정본 게이트(담당: 이 세션)의 운영 범위·권한·완료기준을 문서로
   고정하고 `goal_documents`에 정본으로 등록한다.
2. 이후 이 게이트를 다루는 모든 조치(백필·게이트 승격·정책 결정)는 이 목표
   아래 `goal_documents`에 변경요약(change_summary)으로 남긴다.

## 범위

- 정본 대장 무결성(document_key 충돌·중복 is_latest 방지)
- 문서 표류 게이트(S4) 운영 — 경고→차단 전환과 재발 방지
- Auto-RAG 색인 정책(S1-2) 예외 관리
- 이 세션이 CEO 승인 없이 결정할 수 있는 범위: 데이터 무결성 백필(가역적,
  트랜잭션 검증), 명백한 오류 diff 반려. CEO 결정이 필요한 범위: 게이트
  차단 승격의 최종 스위치, 정책(S1~S5) 조항 자체의 변경.

## 오늘 처리한 것 (근거)

| 조치 | 상태 | 근거 |
|---|---|---|
| document_key 충돌 3건(plan/prd/design.md) 백필 | 완료 | `db_safe_write` 6건, `is_latest` 중복 0행 재확인 |
| AADS git 발산(ahead1/behind3) 정리 | 완료 | `git stash`→`reset --hard origin/main`→`stash pop`, 다른 세션 WIP 보존 |
| index_docs.py `.html` 삭제 시도 반려 | 완료(결정) | PRESERVATION_HARD_GATE, PRD S1-2에 예외 명시로 정정(`eb7d16d5`) |
| 문서표류 27건 정정 + 게이트 승격 | 진행중 | `runner-f4cfbf95` |

## 리스크

- 게이트를 차단으로 올리기 전 표류 0건을 실측으로 확인하지 못하면 "거짓
  0건"이 커밋을 막는 상태로 고정된다(runner-afd8458d 반려 사유).
- 이 문서 자신도 `goal_documents`에 등록하지 않으면 같은 공백이 반복된다.
