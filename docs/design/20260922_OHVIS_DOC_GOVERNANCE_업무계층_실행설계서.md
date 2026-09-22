# OHVIS 문서 정본관리 게이트 — 업무계층 실행설계서

_v1.0.0 | 2026-09-22 | 담당 역할: PM(기획/설계/PRD 게이트 정본관리자)_

> 상위 정본: `docs/design/20260922_OHVIS_DOC_GOVERNANCE_목표계층_설계서.md`
> 이 문서는 단일 목표와 14개 마일스톤 아래의 `Epic → Story → Task` 실행계층을 고정한다.

## 1. 등록 구조

| 계층 | 저장소 | 등록 수 | 계약 |
|---|---|---:|---|
| 목표 | `goals` | 1 | `OHVIS 문서 정본관리 게이트 운영`만 active |
| 마일스톤 | `milestones` | 14 | DG1~DG4 묶음 4건 + 세부 10건 |
| Epic | `work_items.type=epic` | 10 | 세부 마일스톤마다 1건 |
| Story | `work_items.type=story` | 12 | DG2.1은 3건, 나머지는 각 1건 |
| Task | `work_items.type=task` | 18 | 명령·쿼리·파일 단위 완료기준 |

업무계층은 `migrations/20260922_ohvis_doc_governance_work_items.sql`로 등록한다.
스크립트는 idempotency key를 사용하며 재실행해도 같은 계층을 중복 생성하지 않는다.

## 2. 마일스톤별 실행계층

| 마일스톤 | Epic | Story | Task 완료기준 |
|---|---|---|---|
| DG1.1 | 정본 대장 등록 실행 | 등록 범위 확정·반영 | specs 최신 정본 99건 |
| DG1.2 | 이중 정본 해소 실행 | 중복 물리경로 판별·정리 | active/latest 이중 정본 0건 |
| DG1.3 | 등록 API 회귀방지 | identity 정규화 검증 | 회귀 테스트와 origin/main 반영 |
| DG2.1 | 표류 해소·차단 승격 | 정정 / 하드 게이트 / 인수검증 | 전체 스캔 0건, 차단·예외·설치본 검증 |
| DG2.2 | 훅 동기화 강제 | 저장소본·설치본 일치 | 두 훅 `diff -q` 성공 |
| DG2.3 | 수렴 하드 게이트 | owner_resolved 전제 강제 | 미수렴 구현 제출 실제 거부 |
| DG3.1 | 색인 확장자 정책 | Markdown 단일 색인 | `.md` 단일 + 기존 청크 방침 |
| DG3.2 | 정본 색인 반영 | 색인 파이프라인 검증 | 최신 정본별 `doc_chunks > 0` |
| DG4.1 | 리뷰 장애 규명 | 설정 오류 확정 | error_book 등록·signature 매칭 |
| DG4.2 | 허위 원인 회귀방지 | 사례 정본화 | prevention/fix 근거 분리 |

## 3. DG2.1 상세 Task

| Story | Task | 완료기준 |
|---|---|---|
| S1 표류 기준값 정정 | T1 백엔드 문서 수치 정정 | 백엔드 문서 대상 표류 0건 |
| S1 표류 기준값 정정 | T2 대시보드 문서 수치 정정 | 프론트 문서 대상 표류 0건 |
| S1 표류 기준값 정정 | T3 전체 스캔 0건 확인 | `python3 scripts/doc_drift_check.py` EXIT=0 |
| S2 pre-commit 하드 게이트 | T4 `--warn` 제거·비정상 종료 차단 | 표류 staged 문서에서 hook EXIT!=0 |
| S2 pre-commit 하드 게이트 | T5 명시적 예외 | `ALLOW_DOC_DRIFT=1`에서만 EXIT=0 |
| S2 pre-commit 하드 게이트 | T6 설치 훅 동기화 | 저장소본과 설치본 `diff -q` 성공 |
| S3 인수검증·증거 | T7 차단 fixture 테스트 | 자동 테스트가 차단 경로 재현 |
| S3 인수검증·증거 | T8 예외 fixture 테스트 | 자동 테스트가 예외 경로 재현 |
| S3 인수검증·증거 | T9 증거·마일스톤 보고 | 명령·커밋·DB evidence 연결 |

## 4. 상태와 완료 판정

- 최초 등록 시 DG2.1 Epic/Story는 `in_progress`, Task는 `ready`로 둔다.
- 다른 마일스톤의 업무 상태는 마일스톤 상태를 그대로 따라 초기화한다.
- 담당자는 Task를 수행하고 증거를 연결하지만 자기 마일스톤을 직접 `completed`로 판정하지 않는다.
- DG2.1 완료는 전체 스캔 EXIT=0과 하드 게이트·예외 경로가 모두 재현된 뒤 주도 검수로 확정한다.

