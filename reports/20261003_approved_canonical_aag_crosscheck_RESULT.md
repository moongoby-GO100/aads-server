# 승인 정본 4문서 × 최신 코드 × AAG × 기존 테스트 교차검증 결과

- TASK_ID: AADS-APPROVED-CANONICAL-AAG-CROSSCHECK-20261003 (P1, size S, PUSH_ONLY)
- 성격: **분석·검증 전용**. 기능 구현, goal 완료, 전 서버 문서 반영이 **아니다**.
- 산출물: 본 파일 + `reports/20261003_approved_canonical_aag_crosscheck.csv` (95행). 커밋·푸시는 하지 않았다(Runner 가 CEO 승인 후 수행).

## 0. 이 결과의 한계 (먼저 읽을 것)

1. **최신 검증·비구현 단정 없음.** DB AAG 스냅샷 `3d2432db` 는 `state=stale`, `conclusion=not_proven`, `negative_assertion_allowed=false`, coverage 0/0 이다. 이 스냅샷으로는 "최신 코드가 충족한다"도 "구현이 없다"도 주장하지 않는다. 아래 판정은 **AAG 가 아니라 기준 SHA 의 소스를 직접 읽은 조사 증거**이며, CSV 의 `aag_evidence` 열에 그 구분을 행마다 적었다.
2. 로컬에서 격리 rescan(`tools/aag/scan_aads.py` v1.1, L1/L2 구조 스캔)을 한 번 돌렸으나 이 버전은 coverage 객체를 내지 않는다. 라우트·SQL 테이블 참조·엣지 존재 확인에만 쓰고 부재 근거로는 쓰지 않았다.
3. **대시보드 및 다른 프로젝트 범위는 읽을 수 없어 전부 미검증**이다(예: B-UF3 의 화면 표기).
4. 정적 구현·단위 통과·운영 반영은 서로 다른 판정이며 아래 §6 에서 분리한다.

## 1. 고정 기준선

| 항목 | 값 |
|---|---|
| 작업 기준 SHA | `520b5326e51cda499babb185b9db3df8e15f86e4` (워크트리 HEAD, 작업 종료 시점에도 동일) |
| 종료 시점 origin/main | `68f9290775d8987d289e29cd117cf5085f71871d` (L1 doc-storage-rule migration 커밋). 감사 대상 파일은 변경되지 않음 |
| 결과 기준 | **520b5326 기준**. 복구 작업이 SHA 를 바꾸면 이 결과는 520b5326 시점 값이다 |

### 승인 포인터 (production DB 읽기전용 SELECT, tenant 2d701a8c-9596-4757-8588-faa4f7837112, project AADS)

| document_key | head_id | approved revision | revision# | sha256 | 시작 | 종료 |
|---|---|---|---|---|---|---|
| contract:b4ad294d9869 | bb9e8e7e-06c3-409a-8c63-80dd38a4f621 | ebda53f5-91b9-4517-a3c3-40c76540fb30 | 1 | 2651d430221d…0df7 | 동일 | 동일 |
| plan:7d9f483b5dba | 679ce7d2-b686-4669-a96e-618f230500a6 | 6dfc2221-6219-4f75-bc11-47e505dd2b4c | 1 | b0a54223627600…50df | 동일 | 동일 |
| prd:848c81d57565 | 2c4e95fb-e221-426a-93be-9fef83a9235e | 686c817c-c69e-41d3-a89a-0cba301e447b | 1 | 8cacc274a62a1108…1d7f | 동일 | 동일 |
| prd:e677db2cb301 | 39974168-c7cc-4195-8439-1b991fdbe8ea | 79944e05-9c4a-4bd8-9e31-a81a8d017299 | 1 | ac94fa364775d6b1…d62f | 동일 | 동일 |

- generation 2, 이벤트는 id 16~27 만 존재(최대 id 27), 시작·종료 동일. 4문서 모두 저장소 파일 sha256 이 DB revision content_hash 와 일치함을 확인했다.
- DB 쓰기·import·approve·link/unlink 는 하지 않았다.

### AAG 스냅샷

| 항목 | 값 |
|---|---|
| DB 스냅샷 | `3d2432db` — stale / not_proven / negative_assertion_allowed=false / coverage 0/0 (**최신성 근거로 사용 불가**) |
| 로컬 rescan | 기준 SHA 520b5326, scanner `tools/aag/scan_aads.py` v1.1, ruleset: DUP_MODULE·DOUBLE_MOUNT·ROUTE_SHADOWED·ORPHAN_ROUTER·TABLE_NO_MODEL·PATH_DRIFT·ROUTE_MISSING·STALE_BACKUP |
| 로컬 관측 범위 | app py 462, 라우터 모듈 93, 마운트 라우트 1178, 프런트 파일 250(호출 360 해석), SQL 테이블 참조 327, 그래프 노드 952 / 엣지 1503 |
| 로컬 findings | 11건(ORPHAN_ROUTER 1, TABLE_NO_MODEL 9, PATH_DRIFT 1), unresolved 129. 이번 4문서 요구사항과 직접 관련된 finding 은 확인하지 못함(미관측일 수 있음) |
| **비관측 범위** | `deploy.sh`·`scripts/*.sh`·hooks(shell), 문서 사실, 인프라/DB role/네트워크, 동적 import 호출 그래프, 운영 런타임 상태, 대시보드 저장소 |

## 2. 실행한 검증 (기존 테스트만, 신규 테스트 작성 없음)

모두 `bash scripts/run_unit_tests.sh`(운영 이미지 + 워킹트리 마운트, 운영 DB·외부 LLM 미접근)로 실행했다.

| 묶음 | 결과 |
|---|---|
| 계약 계열: test_goal_policy_foundation_w14f / _migration / preconditions / rollout_w14c / test_goal_workflow_approval / goal_work_hierarchy_* | **105 passed** |
| 배포·문서 계열 10파일(deploy_terminal_state_contract, sync_standby_contract, deploy_stream_reconcile, deploy_observability, deploy_build_guards, execution_lease_contract, goal_document_versions, canonical_documents, doc_index_origin_mirror, pipeline_runner_notify_contract) | **104 passed** |
| disposable fixture DB 통합 시험 | **미실행** (이 환경에 fixture DB 를 준비하지 않음) |
| ruff / compileall / dup_guard | 구현 파일을 수정하지 않았으므로 대상 변화 없음. 최종 `python3 scripts/dup_guard.py` 종료코드 0(출력 없음) |

주의: 배포 계열 시험은 전부 **소스 문자열 계약 검사**이며 `deploy.sh` 동작을 실행하지 않는다. "PASS" 가 곧 "동작 증명"이 아니다. CSV `test_result` 는 PASS 43 / SOURCE_GREP_ONLY 16 / NO_TEST 30 / NOT_RUN 6 으로 구분했다. 빌드·배포 관련 항목은 실행하지 않았고 **통과로 보고하지 않는다**(승인 후 Runner 빌드 검증 대상).

## 3. 요구사항 수 대조

| 문서 | 요구사항 수 | CSV 행 | 일치 |
|---|---|---|---|
| contract:b4ad294d9869 (§1~§13) | 52 | 52 | O |
| plan:7d9f483b5dba | 12 | 12 | O |
| prd:848c81d57565 | 10 | 10 | O |
| prd:e677db2cb301 | 21 | 21 | O |
| **합계** | **95** | **95** | O |

- requirement_id 95개 모두 유일(스크립트로 확인). 빈 verdict 없음. verdict 는 MATCH/MISMATCH/UNVERIFIED 만 사용.
- 계약 섹션별: §1:5, §2:2, §3:5, §4:3, §5:6, §6:7, §7:3, §8:3, §9:3, §10:4, §11:5, §12:3, §13:3. 누락 섹션 없음.
- 요구사항으로 세지 않은 부분(누락 아님): 계약 머리말·메타, 기획서 "왜 지금" 배경, PRD 의 상위계약 주석.
- 중복 요구사항: 없음. 단 B-NG3 과 B-FR04H-2 는 같은 탈출구(`AADS_DEPLOY_ALLOW_BUSY_TARGET`)를 다른 문구로 가리키며 별도 행으로 유지했다.
- 근거 없는 판정 점검: UNVERIFIED 27건은 모두 `gap_note` 로 사유를 적었다. 계획서 P-T1/T2 등 과거 작업 주장은 MATCH 로 올리지 않았다.

## 4. 판정 결과

정적 구현 판정 전체: **MATCH 46 / MISMATCH 22 / UNVERIFIED 27**.

| 문서 | MATCH | MISMATCH | UNVERIFIED |
|---|---|---|---|
| contract | 31 | 8 | 13 |
| plan | 2 | 4 | 6 |
| OHVIS PRD | 2 | 3 | 5 |
| Blue/Green PRD | 11 | 7 | 3 |

(원 조사에서 PARTIAL 이던 항목은 구체적 결함이 있으면 MISMATCH, 없으면 UNVERIFIED 로 환원하고 `gap_note` 에 부분 충족 내용을 적었다.)

## 5. 요구사항별 격차와 후속 작업 (우선순위순)

### P0
1. **B-FR04K-1 / B-UF4 — standby digest 미확인 상태에서 success 가 될 수 있음.** `deploy.sh` 의 `sync_standby_slot_after_drain` 이 lock busy·stale generation·ownership 변경 3개 경로(약 2403–2409, 2478–2479행)에서 digest 비교 없이 `return 0`(skipped) 하고, 호출부가 rc 0 을 phase `success` 로 기록한다(약 3044행). 기존 시험은 문자열 검사라 이 경로를 잡지 못한다. 직접 소스 확인 근거. → 별도 수정 작업 필요(승인 후 Runner 빌드 검증 대상).

### P1
2. C-S1-02 / C-S1-01: 계약은 "executor 는 검증키만" 인데 구현은 HMAC 대칭키 한 벌(`goal_policy_foundation.py:286-290`). 분리 여부(identity·DB role·배포 권한·네트워크)는 운영 구성이라 미검증.
3. C-S3-01: A0~A2 매핑표를 코드가 도출하지 않고 호출자 입력으로 받는다. 강제되는 것은 A3·mandatory-human 뿐.
4. C-S4-03: `require_current_preconditions` 가 호출처·테스트 모두 없음. 실행 직전 precondition 재계산이 실행 경로에 연결되지 않았다.
5. C-S5-06: 정책 입력 경로의 `patch_hash`(`goal_policy_preconditions.py` `_canonical_hash`, non-JCS)와 change-set 경로(JCS)가 다른 방식.
6. C-S6-06: AUTO 시 decision·reservation·usage·outbox 단일 트랜잭션 기록을 운영 코드가 아직 호출하지 않는다. `evaluate_and_persist`/`reserve_single_grant` 의 app/ 호출처 0건(직접 grep), 운영 DB W-14F 테이블 전부 0행.
7. C-S13-01/02: B-04 DB 승인 이벤트와 B-02/B-02R ACCEPT 순서 미검증. W-14F 코드는 2026-09-19 19:38–19:57 KST 에 반입됐고 문서의 B-04 시각은 17:09 KST — 선후는 맞아 보이나 ACCEPT 근거는 확인 불가.
8. C-S8-03 / C-S12-02 / C-S12-03: grant issuer/principal 분리, T01·T08·T37·T39–T44·T47 명세 연결, 승인 중단 규칙은 PRD v1.2 등 범위 밖 자료가 필요해 미검증.
9. P-S1 / R-OPS1: `uq_goal_documents_latest` 는 (goal_id, document_key) 단위라 서로 다른 goal 간 document_key 충돌을 막지 않는다. 신규 정본 `canonical_documents` 는 UNIQUE(tenant, project, document_key) 로 예방.
10. B-FR04H-2 / B-NG3: `AADS_DEPLOY_ALLOW_BUSY_TARGET=true` 로 busy target 재생성 가능. B-FR04J-2: retry worker 의 busy 판정이 DB lease 분류기와 다르고 약 1시간 후 포기. B-FR04L-3: rollback 시험·사례 없음.

### P2
- 상태명 `pending_assignment` vs 계약 `review_pending_assignment`(C-S10-01), change-set 멱등 오류코드 `idempotency_conflict` vs `idempotency_key_reused`(C-S11-02), change-set 경로 `tenant_scope_denied` 감사 누락(C-S11-04), entity resolution 실패의 result 가 DENY(C-S6-02), lease 회수 전이 writer 없음(C-S10-04), B-UF3 stream 수 API 미노출, B-NG1 legacy 재시작 탈출구, 기획서 runner-f4cfbf95 "진행중" 표기 낡음(P-T4), PRD 가 goal_documents 만 명시(R-SCOPE1), `.html` 색인 예외와 dg3-1 정책 상충 가능(P-S3), 표류 게이트·`.html` 예외 시험 부재.

## 6. 정적 구현 / 단위 통과 / 운영 반영 — 분리 판정

| 구분 | 판정 |
|---|---|
| 정적 구현 (소스 직접 조사, 기준 SHA) | 95건 중 MATCH 46 · MISMATCH 22 · UNVERIFIED 27. W-14F 계약의 핵심 안전 장치(서명·hash·epoch 재검증, A3 인간 필수, ledger 실패 시 AUTO 금지)는 함수 수준에서 MATCH. **운영 경로 연결은 없음** |
| 단위 통과 | 기존 시험 105 + 104 passed. 단 배포 계열은 소스 문자열 검사, W-14F 는 fake conn. disposable DB 통합은 미실행 |
| 운영 반영 | **미검증.** 측정한 사실은 production DB 에 W-14F 테이블이 존재하고 모두 0행이라는 것(운영 AUTO 결정 사례 없음)뿐. 배포 이력·deploy_phase_events·host retry unit 은 조회하지 않음 |

## 7. STEP 0 기존 구현 분류

| 대상 | 분류 | 사유 |
|---|---|---|
| goal_policy_foundation / preconditions / rollout / workflow_approval 서비스, W-14F 마이그레이션, 관련 테스트 | **유지** | 계약 핵심 항목이 MATCH. 격차는 §5 후속 작업으로 분리(이번 작업은 수정 금지) |
| deploy.sh, scripts/sync-standby.sh, classify_deploy_streams.py, deploy_observability.py | **유지** | 결함(B-FR04K-1 등)은 별도 수정 작업 대상 |
| doc_drift_check / index_docs / pre-commit drift 게이트, canonical_documents, goal_document_versions | **유지** | 요구와 대체로 일치, 격차는 후속 |
| 위 두 보고서 파일 | **신규** | 이번 작업 산출물 |
| 삭제 | **없음** | 삭제 대상 없음 → 롤백 대상 없음 |

기존 파일 수정 0건. 완료된 pilot 및 기존 복구 4건(a3a15314 / 8559f49d / 1816e2c0 / d51bf08f)은 재실행·중복하지 않았다.

## 8. 미실행 항목과 사유

| 항목 | 사유 |
|---|---|
| disposable fixture DB 통합 시험(fences v2, 단일 트랜잭션, 멱등 reserve) | fixture DB 미준비, 운영 DB 사용 금지 |
| deploy.sh 동작 시험(skipped 경로, rollback, busy gate) | 빌드·배포·재시작 금지, 기존 시험이 문자열 검사뿐. 승인 후 Runner 빌드 검증 대상 |
| PRD v1.2(14.12 오류코드, T-명세) 대조 | 이번 고정 4문서 범위 밖 |
| 대시보드·타 프로젝트·운영 인프라(DB role, 네트워크, systemd retry unit) | 이 환경에서 읽을 수 없음 → 미검증 |
| B-04 DB 승인 이벤트, B-02/B-02R ACCEPT 조회 | 범위 밖 원장 |
| 최신 AAG 재색인 | 금지(reindex 불가). stale 스냅샷을 최신으로 취급하지 않음 |

## 9. 비용·기록

- 비용: **미측정** (LLM 토큰 비용 집계 수단 없음).
- handover: 아래 "기록 상태" 참조. `docs/HANDOVER.md` 는 다른 세션이 수정 중이라 건드리지 않았다.
- 변경 파일: `reports/20261003_approved_canonical_aag_crosscheck.csv`, `reports/20261003_approved_canonical_aag_crosscheck_RESULT.md` 두 개뿐(`git status` 로 확인).

## 10. SHA 변경 시 재검증 대상

복구 작업 등으로 기준 SHA(520b5326)가 바뀌면 이 결과는 520b5326 기준이며, 아래 파일 변경 여부를 먼저 확인해 해당 행을 재검증해야 한다.

- `deploy.sh`, `scripts/sync-standby.sh`, `scripts/classify_deploy_streams.py`, `app/services/deploy_observability.py` (Blue/Green 21행)
- `app/services/goal_policy_foundation.py`, `goal_policy_preconditions.py`, `goal_policy_rollout.py`, `goal_workflow_approval.py`, `migrations/20260919_goal_policy_foundation_*.sql`, `tests/unit/test_goal_policy_foundation_w14f.py` (계약 52행)
- `scripts/hooks/pre-commit`, `scripts/doc_drift_check.py`, `scripts/index_docs.py`, `migrations/20260919_goal_document_versions.sql`, `app/api/canonical_documents.py`, `app/routers/goals.py` (기획서·OHVIS PRD 22행)

현재 종료 시점 origin/main(68f92907)은 위 파일을 바꾸지 않았다.

## 11. 명시적 고지

- 본 문서는 **기능 구현이 아니며**, goal/milestone 완료나 전 서버 문서 반영을 의미하지 않는다.
- stale AAG 스냅샷 3d2432db(not_proven, coverage 0/0)에 기대어 "최신 검증 완료"나 "비구현"을 주장하지 않는다. "부재"로 적은 항목(DENY_AUTO, permit cache, scope_hash, review_pending_assignment 등)은 기준 SHA 에 대한 직접 grep 결과이며 AAG 부정 단정이 아니다.
- 대시보드·타 프로젝트 범위는 미검증이다.

## 12. 기록 상태

- handover 기록(AADS/verification/approved-canonical-aag-crosscheck-20261003): **미기록**. 이 세션에는 `handover_write` 도구가 없고 `/api/v1/handovers` 호출에 쓸 테넌트 인증 토큰이 주어지지 않았다(자격증명을 찾아 쓰지 않음). Runner 가 본 RESULT.md 를 근거로 기록해야 한다. 기록 시 포함할 값: 승인 포인터 시작·종료 동일(§1), 판정 MATCH 46 / MISMATCH 22 / UNVERIFIED 27, 최우선 후속 B-FR04K-1.
- `docs/HANDOVER.md` 는 수정하지 않았다.
