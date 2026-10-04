# AADS-RUNNER-WT-RECLAIM-ON-FINISH-20261005 — 종료 job 워크트리 즉시 회수 + 원격 회수기 배치 결함

## 1. STEP 0 — 기존 구현 분류

| 대상 | 분류 | 근거 |
|---|---|---|
| `scripts/reclaim_runner_worktrees.sh` 의 시간(24h)·수량(MAX_KEEP) 회수, patch 보존, `_path_busy`, 등록/경로 검증 | 유지 | 기존 가드를 그대로 재사용. 검증 로직만 `_validate_runner_worktree()` 로 추출 |
| 같은 스크립트의 즉시 회수 모드 (`RUNNER_WT_ONLY_JOB/STATUS`) | 신규 | 한 job 의 워크트리만, "깨끗한 것만" 삭제 |
| `pipeline-runner.sh` 의 error / read-only done / no_changes cancelled / deploy_job / reject_job 말단의 기존 워크트리 제거 | 유지 | 이미 즉시 제거하던 경로. 건드리지 않음 |
| review_failed / review_hold 보존 경로 | 유지 | 승인 대기·리뷰 재개에 필요 |
| `_cleanup_old_artifacts` 의 "reclaimer unavailable" 무음 로그 | 수정 | `_warn_reclaimer_unavailable` 로 교체 (경고 1회 + Telegram + 오류 사전) |
| `_reap_bg_jobs` | 수정 | 종료한 bg job 마다 `_reclaim_finished_worktree` 호출 (run/deploy/reject/review_hold 전부를 한 곳에서 잡는 catch-all) |
| `_reclaim_finished_worktree`, `_reclaimer_script_path`, `_warn_reclaimer_unavailable` | 신규 | |
| `scripts/sync_pipeline_runner_remote.sh` | 수정 | 러너 옆(`dirname $remote_runner`)에 회수기 설치 (0755). 재시작 사유(`changed`)에는 넣지 않음 |
| `scripts/runner_sync_launcher.sh` | 수정 | REQUIRED_FILES 에 회수기 추가 (EXPORT_PATHSPECS 가 상속) |
| `scripts/pipeline-runner.sh.local` | 수정 | 정본과 바이트 동일 유지(테스트가 강제) |
| `tests/unit/test_runner_wt_reclaim_on_finish.py` | 신규 | 39개 |
| `tests/unit/test_runner_sync_launcher.py`, `test_pipeline_runner_remote_sync.py` | 수정 | fake 원격이 새 파일을 알도록 픽스처 갱신 |
| `pre_commit_test_map.py` | 유지(범위 밖) | 브리프가 지목하지 않아 손대지 않음 |

지시서가 명시하지 않은 파일(`runner_sync_launcher.sh`, 테스트 픽스처 2개)을 고친 이유: 회수기를 sync 경로에 싣는 순간
런처의 REQUIRED_FILES 와 fake 원격 픽스처가 새 파일을 모르면 테스트가 깨지고, 구버전 런처는 파일을 export 하지 않기 때문이다.

### md5 불일치의 원인
`scripts/pipeline-runner.sh` 의 HEAD 본(md5 `aa3ca290…`)은 contabo14 설치본과 같다. "5993줄/`cc1ca87c`" 본은
공유 체크아웃 `/root/aads/aads-server` 의 낡은 작업본이다(origin/main 보다 뒤처짐 — `runner_sync_launcher.sh` 가 존재하는 이유). 코드 결함이 아니다.

## 2. 변경 요약

1. **즉시 회수**: 러너 메인 루프의 `_reap_bg_jobs` 가 bg job 종료를 회수할 때 `_reclaim_finished_worktree <job_id>` 를 부른다.
   DB 상태가 `done|error|cancelled|rejected_done|failed` 일 때만 회수기를 `timeout 45` 로 호출한다.
   `awaiting_approval`, `running`, review 계열, 상태 미확인은 `WORKTREE_RECLAIM_SKIP` 로 보존한다.
   `RUNNER_WT_IMMEDIATE_RECLAIM=0` 이면 비활성.
2. **회수기 즉시 모드**: 아래 중 하나라도 있으면 보존(아카이브도 하지 않고 기존 24h/patch 정책에 넘김).
   - 추적/미추적 변경, 재생성 불가 ignored 파일
   - origin/main 에 없는 커밋
   - 열린 프로세스(`lsof`/`fuser`)
   - 등록되지 않았거나 `common dir` 이 다른 디렉터리
   재생성 가능한 ignored 캐시(`__pycache__`, `.ruff_cache`, `.pytest_cache`, `.mypy_cache`, `.next`, `node_modules`, `*.pyc`,
   `tsconfig.tsbuildinfo`)만 남은 경우는 깨끗한 것으로 본다. 이 허용 목록이 없으면 dry-run 에서 종료 job 10개 중 9개가 캐시 때문에 보존되어
   즉시 회수가 사실상 동작하지 않았다(아래 §4). `.vault.key`, 데이터 파일, `.runner_full_diff.patch` 등은 허용 목록에 없다.
3. **원격 배치 결함**: sync 가 러너만 깔고 회수기를 깔지 않아 contabo14 에는 `/root/scripts/reclaim_runner_worktrees.sh` 가 **없다**(실측: `No such file`).
   sync 가 러너 옆에 0755 로 설치하도록 고쳤다.
4. **무음 로그 제거**: 회수기가 없으면 `WARN` 1줄 + Telegram + 오류 사전 후보 기록. 플래그 파일(`RECLAIMER_MISSING_FLAG`)과
   `RECLAIMER_WARN_INTERVAL_MIN`(기본 1440분)으로 반복 알림을 막는다.

## 3. 테스트 (원문)

`bash scripts/run_unit_tests.sh tests/unit/test_runner_wt_reclaim_on_finish.py`
```
39 passed in 7.59s
```

`bash scripts/run_unit_tests.sh tests/unit/test_pipeline_runner_*.py tests/unit/test_runner_*.py tests/unit/test_reclaim_*.py tests/unit/test_error_book_runner_failure_hook.py`
```
688 passed, 3 skipped in 114.67s (0:01:54)
```
허용 목록 추가 후 같은 묶음 재실행:
```
690 passed, 3 skipped in 119.88s (0:01:59)
```

| 요구 케이스 | 테스트 |
|---|---|
| clean + done 삭제 | `test_clean_worktree_is_removed_immediately_for_every_terminal_status` (5개 종료 상태), `test_runner_hook_removes_clean_terminal_worktree` |
| dirty + error 보존 | `test_uncommitted_change_is_preserved_without_archiving`, `test_runner_hook_preserves_dirty_error_worktree` |
| 미병합 커밋 보존 | `test_unmerged_commit_is_preserved_without_bundling`, `test_runner_hook_preserves_unmerged_commit` |
| awaiting_approval 보존 | `test_non_terminal_status_is_never_reclaimed`, `test_runner_hook_never_touches_awaiting_approval_or_other_non_terminal` |
| 회수기 없음 경고 | `test_missing_reclaimer_warns_once_with_telegram_and_error_book`, `…warns_again_after_interval` |
| 캐시 허용 목록 | `test_regenerable_ignored_caches_do_not_block_reclaim`, `test_cache_allowlist_does_not_hide_other_ignored_or_untracked_files` |

## 4. Dry-run 추정 (삭제 없음)

### 이 서버(68) — 실제 dry-run (`RUNNER_WT_ONLY_JOB/STATUS` + `DRY_RUN=1`, DB 상태는 읽기 전용 조회)
`/tmp/aads-wt-runner-*` 14개 중 자기 자신(`runner-c026084c`)은 제외한 13개.

| 결과 | 개수 | 용량 |
|---|---|---|
| 회수 후보(done, 깨끗) | 7 | **2,764 MB** (349+555+352+349+352+458+349) |
| 보존 — 재생성 불가 ignored 존재 (`139813c4` `.vault.key`·데이터, `d08c33b4`·`f0ab94ee` `.runner_full_diff.patch`) | 3 | — |
| 보존 — 비종료 상태 (awaiting_approval 1, running 1, 상태 미등록 1) | 3 | — |

허용 목록 적용 전에는 후보가 1개(352 MB)였다.

### contabo14 — 추정(상한), dry-run 아님
읽기 전용 ssh 실측: `/tmp/aads-wt-runner-*` **51개, 11,339 MB**, `/root/scripts/reclaim_runner_worktrees.sh` 없음.
회수기가 원격에 없어 dry-run 을 돌릴 수 없고, 원격에 파일을 쓰는 것은 금지라 하지 않았다.
68 서버의 종료(done) 워크트리 비율(10개 중 7개, 용량 기준 약 72%)을 단순 적용하면 **약 35개, 약 8 GB** 이나 근거가 약한 외삽이다.
실제 수치는 배포 후 첫 `WORKTREE_RECLAIM` 로그와 `df` 로 확인해야 한다.
cafe24_114 는 이 세션에서 호스트 이름이 해석되지 않아(`Could not resolve hostname`) 측정하지 못했다.

## 5. contabo14 배포 절차 (별도 CEO 승인 필요 — 이 작업은 수행하지 않음)

전제: 호스트에 `lsof` 또는 `fuser`, 러너와 같은 DB 접근, `/root/aads/aads-server` 가 `RUNNER_WT_REPO` 기본값으로 유효.

1. 이 커밋이 origin/main 에 반영된 것을 확인한다.
2. 런처 갱신(구버전 런처는 회수기를 export 하지 않는다): origin/main 의 `scripts/runner_sync_launcher.sh` 를
   `/usr/local/sbin/aads-runner-sync-launcher` 에 `install -m 0755` 로 교체한다.
3. sync 1회 실행: `systemctl start aads-pipeline-runner-sync.service`. 회수기는 `/root/scripts/reclaim_runner_worktrees.sh`(0755) 로 설치되고,
   회수기 설치 자체는 러너 재시작을 일으키지 않는다. 러너 본문이 바뀌었으므로 sync 가 정해진 절차로 재시작을 판단한다.
4. 확인: `ls -l /root/scripts/reclaim_runner_worktrees.sh`, 다음 job 종료 후 러너 로그의 `WORKTREE_RECLAIM job=…`.
5. 기존 누적분 삭제는 이 변경의 범위가 아니다. 필요하면 별도 승인으로 `DRY_RUN=1 bash /root/scripts/reclaim_runner_worktrees.sh` 먼저.

## 6. 롤백
해당 커밋을 revert 한다. 비상 시에는 러너 환경에 `RUNNER_WT_IMMEDIATE_RECLAIM=0` 으로 즉시 회수만 끌 수 있다(기존 24h 회수는 그대로).

## 7. 남은 위험
- 허용 목록은 알려진 캐시 이름만 본다. 새 종류의 캐시는 보수적으로 "보존"되어 기존 24h 정책으로 넘어간다(데이터 손실 방향이 아님).
- 즉시 모드는 job 이 끝난 뒤에만 돈다. 이미 쌓인 워크트리는 기존 시간·수량 정책이 처리한다.
- 원격(contabo14/cafe24_114)의 실제 회수량은 미실측이다.
