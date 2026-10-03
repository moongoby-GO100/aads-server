# 정본 재색인 및 /docs E2E 재개 (R3) — 결과: 선행 충족, 이 러너는 배포·색인 미실행

- TASK_ID: AADS-CANONICAL-SEARCH-REINDEX-E2E-R3-20261003
- 판정: **선행 2건은 충족. 배포·재색인·API/브라우저 E2E는 미실행.** 이 러너는 배포·빌드·commit/push를 금지당했고, 배포 이후 단계(재색인, 검색 재검증, 화면 검증)는 배포 결과에 의존하므로 같이 멈췄다. 어떤 항목도 통과로 적지 않았다.
- 이 파일 외에 코드·DB·운영 변경 없음. DB는 SELECT와 `apply_release_migrations.sh --plan`(아무것도 적용하지 않음)만 실행했다.

## 1. 선행 조건 실측 (2026-10-03, 읽기 전용)

`git ls-remote origin refs/heads/main` = `ec05ab03580a7965a33f2e8ff8322db5d10bdef4` (워크트리 HEAD와 동일, clean).

| 선행 | origin/main 커밋 | 내용 실측 | 판정 |
|---|---|---|---|
| search_auth `runner-d8de94d0` (AADS-DOC-SEARCH-TENANT-GUARD-20261003) | `ca75c842` | `app/api/project_docs.py`, `app/services/doc_index.py`, `auto_rag.py`, `scripts/index_docs.py`, `scripts/build_kg.py`, `migrations/20261003_doc_chunks_tenant_scope.sql`(+down), 테스트 3종, `reports/20261003_doc_search_tenant_guard_RESULT.md`. 해당 보고서: fake 단위 186 passed, 격리 PG 통합 31 passed | push 완료 확인 |
| digest_gate `runner-e61debec` (AADS-DEPLOY-STANDBY-DIGEST-FAILCLOSED-R2-20261003) | `ec05ab03` | `deploy.sh`(+146/-47), `tests/unit/test_deploy_standby_digest_failclosed.py`, `test_deploy_stream_reconcile.py`, RESULT 보고서 | push 완료 확인 |

주의: "AI 검수·승인"은 두 커밋이 Runner 승인 경로를 거쳐 origin/main에 들어왔다는 사실로만 확인했다. 이 러너에서 위 테스트를 재실행하지는 않았다(위 수치는 해당 보고서의 주장이지 이번 실측이 아니다).

## 2. 운영 현재 상태 (배포 전 기준선, SELECT)

| 항목 | 값 |
|---|---|
| `doc_chunks` tenant/head/revision 칸 | **없음** (`information_schema` 조회에서 `heading`만 일치). 마이그레이션 미적용 |
| `doc_chunks` 전체 / `canonical://` | 27,805행 / **0행** (R2 기준선과 동일) |
| `apply_release_migrations.sh --plan` | `PENDING migrations/20261003_doc_chunks_tenant_scope.sql`, `PENDING migrations/20261003_canonical_gate_events_chat_tool.sql` — summary pending=2 applied=0 skipped=270 drift=3 held=27 excluded=7. 즉 릴리스 경로가 tenant_scope 마이그레이션을 적용 대상으로 잡는다 |
| 최근 deploy_runs | 5554 `4bc4eb58` success, 5555 `bcdbe57f` cancelled, 5556 `b6122893` **failed(p0p1_monitoring)**. `ec05ab03` 대상 deploy_run 없음 |
| 운영 코드 | `ec05ab03`(tenant 가드·digest fail-closed)는 **아직 배포되지 않았다.** 현재 운영 검색은 보강 전 코드다 |

## 3. 실행하지 않은 것과 사유

| 항목 | 상태 | 사유 |
|---|---|---|
| `deploy.sh bluegreen` 릴리스 / deploy_run | 미실행 | 이 러너는 빌드·배포 명령 금지(승인 후 Runner 수행). deploy_run_id / 릴리스 SHA / digest / 300초 관측: **해당 없음(미수행)** |
| migration 적용 및 전후 `information_schema` | 전(前)만 기록(§2). 후(後) 미실행 | 적용은 릴리스 경로가 수행해야 한다 |
| `index_docs.py index-canonical --dry-run` 및 실행 | 미실행 | 권한 차단 운영 검증(배포 후) 이전 색인 금지. 보강 전 운영 DB에는 tenant 칸이 없어 색인기가 중단하는 것이 정상이다 |
| 정본 검색 A 성공/B 미노출/미인증 거부 | 미실행 | 위와 동일. 정본 청크 0건이므로 전후 비교도 불가 |
| `/docs` Playwright E2E (74/92/109/110 AADS, 119 ACCT) | 미실행 | 배포·색인 이전. "⚠️ 브라우저 E2E 미실행, API 검증으로 대체"도 아님 — API 검증 역시 미실행 |
| 기존 5건 연결 표시 검증 | 미실행 | 위와 동일. 쓰기는 하지 않았다 |

## 4. 릴리스 시 위험 (확인된 사실과 미확인 구분)

- **확인**: 직전 두 릴리스(5552 `dc409250`, 5556 `b6122893`)가 모두 P0/P1 관측에서 같은 오류로 failed 처리됐다 — `pipeline_runner.batch_submit_fail: could not determine data type of parameter $15`. `ec05ab03` 릴리스도 같은 관측 게이트를 통과해야 certified다.
- **미확인**: 이 오류의 원인과 현재 HEAD 코드에 남아 있는지. `app/api/pipeline_runner.py` 배치 제출 INSERT는 `$14::uuid`까지만 보여 `$15` 위치를 특정하지 못했고, 운영 컨테이너 최근 2시간 로그에서는 해당 문자열 0건이었다(운영 컨테이너는 아직 `4bc4eb58` 계열 코드). 원인을 밝히지 못했으므로 error_book에 등록하지 않았다(추측 금지).
- 릴리스 제출 시 이 관측에서 또 실패하면, 원인은 tenant 가드가 아니라 위 오류일 가능성이 있다. 그 경우 자동으로 재시도하지 말고 원인 조사를 먼저 하라.

## 5. 재개 순서 (승인 후)

1. 승인된 ops queue로 `deploy.sh bluegreen` 제출 — release SHA `ec05ab03580a7965a33f2e8ff8322db5d10bdef4`(또는 그 이후 clean SHA). 동일 SHA certified deploy_run이 있으면 재사용.
2. 적용 후 `doc_chunks`에 tenant/head/revision 3칸 + 부분 인덱스 2개 SELECT, candidate health, same-digest standby, 300초 P0/P1 관측 통과 확인.
3. tenant 가드 운영 검증(미인증 거부, B tenant 미노출) 통과 후에만 `python3 scripts/index_docs.py index-canonical --dry-run` → 계획 확인 → 실행.
4. 이상 시 해당 run이 만든 `canonical://` 청크만 식별해 최소 rollback. 광범위 DELETE/DROP/TRUNCATE 금지.
5. 서버 Playwright로 `https://aads.newtalk.kr/docs` 정본 탭·검색·revision 화면 검증(Vault credential, 토큰·비밀번호 미출력).

## 6. 기록·비용

- HANDOVER.md / DB handover 쓰기: 수행하지 않음 — 완료된 작업이 없어 "완료"로 기록하면 오기록이 된다. 이 보고서가 상태 기록이다.
- 목표/마일스톤 상태: 변경하지 않음. "정본 검색 E2E 운영 입증"을 뒷받침하는 증거는 아직 없다(canonical:// 0행, 운영은 보강 전 코드).
- error_book 등록: 없음(§4 사유).
- 변경 파일: `reports/20261003_canonical_search_reindex_e2e_R3_RESULT.md` (신규) 1개. SHA·GitHub URL은 Runner 커밋 후 확정.
- 테스트 재실행: 없음. 비용: $ 미측정.
