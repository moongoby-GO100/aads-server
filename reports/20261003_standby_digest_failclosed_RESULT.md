# standby skipped 의 릴리스 성공 오판 교정 (AADS-DEPLOY-STANDBY-DIGEST-FAILCLOSED-R2-20261003)

- 기준 코드: origin/main 기반 isolated worktree, HEAD `ca75c842`. 변경은 미커밋(commit/push 는 승인 후 Runner).
- 근거: `reports/20261003_approved_canonical_aag_crosscheck_RESULT.md` §5 B-FR04K-1 / B-UF4.

## 판정 — 최신 코드(ca75c842)에서 P0 는 **미해결이었고, 이번 변경으로 해결**

수정 전 최신 코드에서 재현됨(직접 코드 경로 검증, 과거 520b5326 한정 아님):
`sync_standby_slot_after_drain` 의 lock busy / stale generation / ownership change 세 경로가 `return 0` →
호출부 `case 0)` 가 `deploy_phase_end ... "success"` 기록 → 최종 상태 `success`(인증). 양 slot digest 를 한 번도 비교하지 않았다.
(재현은 운영 컨테이너 없이 stub 시험으로 수행: 변이 시험에서 `return 3`→`return 0` 로 되돌리면 3개 시험이 실패한다.)

## STEP 0 분류

| 대상 | 분류 | 비고 |
|---|---|---|
| `sync_standby_slot_after_drain` — drain pending(rc 2), 실패(rc 1), health/memory/digest 검증, `--no-build --no-deps --force-recreate` | 유지 | |
| 같은 함수의 skipped 3경로 | **수정** | `return 0` → `return 3` + `STANDBY_SYNC_SKIP_REASON` |
| 같은 함수 inline digest 비교 | 수정 | 새 helper 호출로 대체(메시지 `active/standby image digest mismatch` 유지 — release-contract 스크립트가 grep) |
| `standby_same_digest_verified` | **신규** | 양 슬롯 `docker inspect .Image` 직접 비교, 한쪽이라도 못 읽으면 불일치 |
| `handle_standby_sync_result` | **신규** | 호출부 inline `case` 를 함수로 추출(시험 가능). rc 0 도 호출부에서 digest 를 독립 재검증 |
| `resolve_final_deploy_status` | **신규** | 최종 상태 결정을 함수로 추출 + bluegreen 이면 모니터링 뒤 digest 재확인 |
| `restart_old_slot_after_drain` | 유지 | 백그라운드 재시작 worker, 인증 경로 아님 |
| `scripts/sync-standby.sh` | 유지 | 이미 fail-closed(digest 불일치 `die`, busy → exit 3, 동일 digest 일 때만 `success_partial→success`). 변경 불필요 |
| `schedule_standby_sync_retry` | 유지 | rc 2/3 을 재시도로 해석 — 새 skipped 경로도 같은 worker 가 처리 |
| 삭제 | 없음 | |

지시서 외 파일: `tests/unit/test_deploy_stream_reconcile.py` — 호출부 inline `case` 를 함수로 옮겼기 때문에 그 문자열 분할에 의존하던
`test_standby_sync_failure_cannot_be_certified_as_partial_success` 만 `handle_standby_sync_result` 기준으로 갱신(검증 내용 동일, 완화 아님).

## 동작 변화 (상태·exit·관측 분리)

| sync 결과 | rc | phase 기록 | 최종 run 상태 |
|---|---|---|---|
| 동일 digest 동기화 + 호출부 재검증 통과 | 0 | success | `success` (모니터링 뒤 최종 digest 재확인도 통과해야) |
| rc 0 이지만 호출부 digest 재검증 실패 | 0→실패 | failed + `record_deploy failed` | exit 1 |
| drain pending(활성 스트림) | 2 | skipped | `success_partial`, 재시도 예약 |
| **lock busy / stale generation / ownership 변경** | **3 (신규)** | **skipped** (사유 기록) | **`success_partial`**, error `standby sync deferred: skipped: <사유>`, 재시도 예약 |
| digest 불일치 / health·memory 실패 | 1 | failed | `failed`, exit 1 |
| 5분 모니터링 뒤 digest drift | — | — | `success_partial` (`...digest mismatch at final certification`) |

`success_partial` 은 계보 기록(Phase 8)을 건너뛰고, sync-standby.sh 가 같은 release_sha·동일 digest 일 때만 `success` 로 승격한다(기존 계약).

## 보존한 기존 계약
one image per release SHA, `--no-build` 시작, nginx lock 은 routed health 직후 해제, DB owner lease/ownership 재확인, active slot 재시작 금지,
짧은 lock, routed-health 실패 시 롤백, 5분 모니터링 — 해당 코드는 손대지 않았다. `scripts/verify-bluegreen-release-contract.sh` PASS.

## 검증 (코드·단위 / 운영 / 브라우저 분리)

**코드·단위 (실행함)**
- 신규 `tests/unit/test_deploy_standby_digest_failclosed.py` 18건: deploy.sh 의 **실제 함수 본문**을 잘라 bash 서브프로세스에서 docker/flock/curl/nginx stub 으로 실행(소스 grep 아님).
  lock busy, stale generation, drain 후 ownership 변경, drain pending, digest mismatch, health 실패, same-digest 성공, rc 0 + digest 불일치/미확인,
  최종 digest drift, non-bluegreen 모드, **routed-health 실패 롤백**(실제 ④ 블록을 실행해 upstream 원복·active slot 복원·`record_deploy failed`·exit 1 확인)과 성공 경로.
- 변이 시험: `return 3`→`return 0` 로 되돌리면 3건 실패(시험이 버그를 실제로 잡음). 확인 후 복원.
- `bash scripts/run_unit_tests.sh` (운영 이미지): test_deploy_stream_reconcile / terminal_state_contract / sync_standby_contract / deploy_observability / deploy_safe / 신규 시험 + standby_session_ownership = **92 passed, 1 failed**.
  실패 1건 `test_standby_session_ownership.py::test_goal_dispatch_on_standby_never_opens_a_stream`(`goal_dispatch.py:543` SimpleNamespace `.info` 없음)은
  deploy.sh 와 무관(해당 시험은 deploy.sh 미참조, 이번 변경은 goal_dispatch 미수정)한 **기존 실패**이며 이번에 고치지 않았다 — 별도 담당 필요.
  deploy 관련 시험만 따로 돌린 결과(`test_deploy_standby_digest_failclosed` + `test_deploy_stream_reconcile`) 29 passed.
- `bash -n deploy.sh` OK, `scripts/verify-bluegreen-release-contract.sh` PASS, `scripts/dup_guard.py` 통과, `ruff --select F821,F811` 통과.
- 호스트 pytest 는 asyncpg 부재로 일부 실패하므로(환경 문제) 운영 이미지 러너 결과만 근거로 삼는다.

**운영 반영: 미실행.** 실제 build/restart/deploy, 운영 docker/라우팅 변경 없음. 운영에서의 실제 skipped 경로 동작은 검증되지 않았다.
**브라우저 검증: 해당 없음**(UI 변경 없음).

## 후속 ops release 허용 여부
이 변경이 승인·커밋·**반영된 뒤에만** 허용. 반영 전에는 deploy.sh 가 여전히 skipped 를 success 로 인증할 수 있으므로,
정본 검색 릴리스의 필수 안전 선행 조건은 "코드·단위 통과" 까지만 충족, "운영 반영"은 미충족이다.
반영 후 첫 bluegreen 릴리스에서 `deploy_phase_events` 에 skipped 사유가 남고 `success_partial` 로 닫히는지 관측할 것(미확인).

## 후속 triage (이번에 고치지 않음 — 무단 리팩터링 금지)
| 항목 | 영향 | 의존 / 담당 후보 |
|---|---|---|
| B-FR04J-2 retry worker 약 1시간(120×30s) 후 포기 | 이번 변경으로 skipped 도 이 worker 로 가므로 장기 lock busy 시 `success_partial` 이 오래 남을 수 있음(오인 success 는 아님) | ops release 담당; sync-standby.sh/DB lease 분류기 통일 |
| B-FR04H-2/B-NG3 `AADS_DEPLOY_ALLOW_BUSY_TARGET` | busy target 재생성 허용 | 운영 정책 결정 필요 |
| B-FR04L-3 rollback 사례 부재 | 이번에 제어흐름 stub 시험만 추가, 실제 사례는 없음 | ops |
| C-S1-02 HMAC 대칭키 한 벌 | executor 검증키 분리 계약과 불일치 | goal policy 담당, 운영 구성 확인 필요 |
| C-S3-01 / C-S4-03 / C-S5-06 / C-S6-06 (risk tier 매핑·precondition 재계산·hash 방식·단일 트랜잭션) | W-14F 정책 경로 미연결/불일치 | goal policy 담당, 서로 의존(C-S6-06 은 호출처 연결 선행) |
| 기존 실패 `test_standby_session_ownership` | 게이트 신뢰도 | goal_dispatch 담당 |

## 기록
- HANDOVER.md: 항목 추가함.
- DB handover / `POST /api/v1/handovers`: **미기록** — 이 세션에 handover_write 도구와 tenant 인증 토큰이 없다(자격증명 탐색 안 함). 제안 키 `deploy-standby-digest-failclosed-20261003`.
- error_book 등록: **미실행** — fix 커밋 SHA 가 아직 없고(Runner 커밋 후) 공유 DB 쓰기다. 원인은 코드 재현으로 확인됨. 커밋 후:
  `error_book.py register --key deploy.standby_skipped_certified_as_success --symptom "standby lock busy/stale generation/ownership change 인데 deploy phase success" --cause "sync_standby_slot_after_drain skipped 경로가 return 0 이라 호출부가 success 로 읽음" --prevention "skipped 는 별도 rc(3) 로 분리하고 호출부가 digest 를 독립 검증" --fix-commit <sha> --fix-file deploy.sh --fix-note "rc 3 + handle_standby_sync_result + resolve_final_deploy_status"`
- 파일: `deploy.sh`, `tests/unit/test_deploy_stream_reconcile.py`, `tests/unit/test_deploy_standby_digest_failclosed.py`(신규), `reports/20261003_standby_digest_failclosed_RESULT.md`(신규), `HANDOVER.md`.
- SHA / GitHub URL: 커밋 전이라 없음 — Runner 커밋 후 확정.
- 비용: $ 미측정(약 $5 한도 내 소규모 작업으로 추정, 실측값 아님).
- 목표/마일스톤 상태는 변경하지 않았다.
