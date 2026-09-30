# AADS-LAYOUT — pre-commit 게이트 경로 의존 한계 보완안 (스테이징 밖 배포계약 파괴 탐지)

- TASK_ID: AADS-GATE-PATHDEP-CONTRACT-DESIGN-20260930
- 작성: 2026-09-30 (KST) · 기준 커밋: `04ac3153` (= origin/main, 작성 시점 HEAD)
- 범위: **설계 문서만.** scripts/, tests/, app/, `.git/hooks/`, crontab 은 이 작업에서 수정하지 않았다.
- 표기: **[실측]** = 이 세션에서 직접 재거나 실행한 값. **[산식]** = 실측값으로 계산한 값. **[미측정]** = 재지 못한 값(측정 방법 병기).

---

## 0. 요약

1. 현재 게이트(`scripts/pre_commit_test_map.py`)는 **스테이징 경로로 테스트를 고른다.** 배포계약 테스트 53개 파일 중 게이트에 연결된 것은 17개뿐이고 **36개는 어떤 경로를 바꿔도 돌지 않는다** [실측].
2. 이 설계를 쓰기 위해 전체 묶음을 한 번 돌렸더니 **origin/main 이 지금 이미 빨갛다**: `1 failed, 627 passed` [실측 2회]. 실패 테스트는 게이트에 연결돼 있지 않고, 단독으로 돌리면 통과하며(5 passed) 묶음으로 돌릴 때만 실패한다 — 다른 테스트 파일이 수집 시점에 `structlog` 를 가짜로 바꿔 놓기 때문이다. 즉 **경로 의존 선택은 "안 돌아서" 놓치고, 부분집합 선택은 "순서 의존"까지 놓친다.**
3. 권장: **C안(B 먼저 → pre-push 핵심 부분집합)**. 단, **선행 조건(0단계): 묶음을 초록으로 만든 뒤에 켠다.** 지금 A 또는 pre-push 차단형을 켜면 무관한 모든 push 가 막혀 `ALLOW_FORCE_PUSH=1` 우회가 습관이 된다(R-PUSH).

---

## 1. 문제 정의 — 경로 의존 선택의 실패 시나리오

### 1.1 현재 게이트가 하는 일 (STEP 0 조사)

| 대상 | 현재 동작 | 분류 |
|---|---|---|
| `scripts/hooks/pre-commit` Step 3 (303~322행) | `echo "$STAGED" \| python3 scripts/pre_commit_test_map.py --run`. rc 1·2 모두 커밋 차단 | 유지 |
| `scripts/pre_commit_test_map.py` `GROUPS` | tools / runner(11개) / deploy(6개) / hooks(2개). `Group.matches()` 는 경로 완전일치·glob·정규식 | 수정(전량·부분집합 정의 추가) |
| `scripts/run_unit_tests.sh` | 운영 이미지 + 워킹트리 `/app` 마운트로 pytest. rc 0/1/2 | 유지(작업 디렉터리 재지정 옵션만 검토) |
| `scripts/hooks/pre-push` | **테스트를 돌리지 않는다.** pre-commit 서명(`hook_verified/<sha>`)만 확인. `ALLOW_FORCE_PUSH=1` 이면 통과 | 수정(계약 단계 추가) |
| `scripts/hooks/post-rewrite` | rebase 시 서명만 새 SHA 로 이관 — **테스트는 다시 돌지 않는다** | 유지 |
| crontab | 검증 성격 항목: `pre_deploy_validate.sh`(문법, 5분), `check_dependency_vulns.py` 등. 배포계약 테스트 주기 실행 **없음** | 신규(항목 추가) |
| `tests/unit/test_dup_guard.py` | `.git/hooks` 설치본 == `scripts/hooks` 저장소본 검사 | 유지(계약 단계 마커 단정 추가) |

### 1.2 실측: 어떤 변경이 테스트를 0건 고르는가

`echo <경로> \| python3 scripts/pre_commit_test_map.py` (선택만 출력, 실행 안 함) [실측]

| 스테이징 경로 | 선택된 테스트 수 |
|---|---|
| `app/services/deploy_observability.py` | **0** |
| `app/services/error_book.py` | **0** |
| `app/core/anthropic_client.py` | **0** |
| `app/main.py` | **0** |
| `docker-compose.yml` | **0** |
| `migrations/178_x.sql` | **0** |
| `requirements.runtime.lock` | **0** |
| `tests/unit/test_deploy_safe.py` (테스트 파일만 수정) | **0** |
| `tests/unit/test_error_book_runner_failure_hook.py` (테스트 파일만 수정) | **0** |
| `scripts/deploy.sh` | 6 (deploy 묶음만, runner 묶음 아님) |
| `scripts/pipeline-runner.sh`, `app/api/pipeline_runner.py` | 11 (runner 묶음만) |

### 1.3 실패 시나리오

| # | 시나리오 | 예 | 지금 게이트가 통과시키는 이유 (한 줄) |
|---|---|---|---|
| S1 | **공유 모듈 변경** | `app/services/deploy_observability.py` 나 `app/core/*` 를 고쳐 `test_deploy_observability.py` 계약이 깨짐 | GROUPS 의 glob 은 `scripts/deploy*`·러너 3개 파일뿐이라 `app/services/deploy_*` 는 0건 선택 [실측 위 표] |
| S2 | **설정·환경·스키마 경유** | `docker-compose.yml` 환경변수, `migrations/*.sql`, `requirements.*.lock` 을 고쳐 `test_deploy_dependency_image_contract` / `test_deploy_release_migrations` 가 깨짐 | 이 경로들은 어떤 Group 에도 없다 → 0건 [실측] |
| S3 | **테스트 파일만 수정** | 계약 테스트의 기대값을 바꾸거나 삭제해 계약을 조용히 약화 | `Group.tests` 에 없는 테스트 파일은 자기 자신이 바뀌어도 선택되지 않는다 → 0건 [실측]. 연결된 17개조차 "파일 자기 자신"만 매칭 |
| S4 | **rebase 로 재작성된 커밋** | `git pull --rebase` 후 문법 충돌은 없지만 의미가 충돌(예: 다른 세션이 바꾼 함수 시그니처) | pre-commit 은 **rebase 전** 트리에서 돌았고, `post-rewrite` 는 서명만 옮긴다(R-PUSH). push 되는 트리는 한 번도 테스트되지 않았다 |
| S5 | **테스트 간 오염(순서 의존)** — **현재 main 에서 실제 발생 중** | `test_deploy_observability.py`·`test_deploy_adapters.py` 가 수집 시점에 `sys.modules.setdefault("structlog", 가짜)` 를 실행 → 이후 `app.api.pipeline_runner` 의 `logger` 가 `warning` 만 가진 가짜가 됨 → `test_error_book_runner_failure_hook.py::test_candidate_record_exception_does_not_escape_runner_terminal_flow` 가 `AttributeError: ... no attribute 'debug'` | 그 테스트는 GROUPS 에 없고, 게이트는 묶음을 **파일 그룹별로 따로** 돌려 오염 조합이 만들어지지 않는다. 단독 실행은 `5 passed` [실측] |
| S6 | **다른 세션이 깨뜨린 main 위에 쌓기** | 러너 여러 개가 병렬로 커밋(24시간 66건, 7일 133건 [실측 `git log origin/main`]) | 각 커밋은 자기 스테이징만 본다. 서로의 조합은 아무도 검증하지 않는다 |

S1~S3 은 "경로가 대응표에 없음", S4 는 "검증된 트리 ≠ push 되는 트리", S5·S6 은 "부분집합·개별 검증으로는 조합이 안 보임" — 원인이 셋으로 갈린다. 그래서 경로 매핑을 더 촘촘히 하는 것으로는 해결되지 않는다(매핑은 S1·S2 만 줄이고, 새 파일이 생길 때마다 다시 낡는다).

---

## 2. 실측 (배포계약 묶음 실행 시간)

측정 조건: 이 worktree(HEAD `04ac3153`, 청결), `bash scripts/run_unit_tests.sh <파일...>`, 호스트 load average 6.0~8.5(다른 러너 동시 가동 중 — **실제 개발자 체감과 같은 조건**이지만 분산이 있다). 파일 목록은 `ls tests/unit | grep -e deploy -e runner`(+`test_external_deploy_ledger_sync`, `test_error_book_runner_failure_hook`, `test_reclaim_runner_worktrees`), `.bak_aads` 제외.

| 묶음 | 파일 | 테스트 | wall-clock (docker 기동 포함) | pytest 내부 | 결과 |
|---|---|---|---|---|---|
| 기준선(가장 작은 게이트 실행, `test_dup_guard.py`) | 1 | 18 | **7.2 s** [실측] | 0.28 s | pass |
| **핵심 부분집합** = 현재 GROUPS runner(11)+deploy(6) | 17 | 262 | **28.8 s / 23.5 s** [실측 2회] | 21.2 / 17.1 s | 262 passed |
| **전량** 배포계약 묶음 | 53 | 628 | **75.1 s / 68.7 s** [실측 2회] | 66.8 / 61.1 s | **1 failed, 627 passed** (2회 동일) |

- 전량에서 36개 파일은 현재 게이트에 연결되지 않은 것이다(`comm` 실측: 전량 − 핵심 = 36, 핵심 − 전량 = 0).
- 가장 느린 테스트: `test_pipeline_runner_nochanges_guard_r5.py` 의 4건이 5.07 / 2.18 / 2.15 / 1.14 s 로 상위를 차지한다 [실측 `--durations`]. 이름(`recheck_wait`, `sigterm`)으로 보아 대기·시그널 기반이라 타이밍에 민감할 수 있으나, **플레이크 여부는 검증하지 않았다 [미측정]** (측정 방법: 단계 0 의 3회 연속 실행에서 결과 변동 확인).
- **[미측정]** pre-push 훅 자체의 오버헤드(`git push` 실측은 승인 범위 밖). 측정 방법: 구현 1단계에서 임시 bare 저장소로 `git push` 를 `time` 으로 잰다.
- **[미측정]** 푸시 대상 SHA 를 `git archive` 로 내보내 돌리는 경우의 추가 시간. 측정 방법: `time git archive HEAD \| tar -x -C $(mktemp -d)`.

### 2.1 push 락 안에서 도는 시간이 문제다 (코드 실측)

`scripts/pipeline-runner.sh:3983~4060`: 러너는 `flock -w 300 /tmp/pipeline-deploy-AADS.lock` 을 잡은 채 `git push origin <sha>:refs/heads/main` 을 실행한다. **pre-push 훅은 이 `git push` 안에서 돈다 = 락을 쥔 채 테스트가 돈다.** 락 대기 상한이 300 s 이고 러너 슬롯이 4개(`CLAUDE_LEASE_SLOTS="1 2 3 4"`, `test_runner_four_slot_lease.py:30`)다.

- 전량 75 s 를 훅에서 돌리면 4개가 동시에 push 할 때 마지막은 3 × 75 = **225 s [산식]** 대기 → 배포 빌드가 락을 쥐고 있으면 300 s 를 넘겨 `push_exit=75`(락 타임아웃)로 잡이 죽는다.
- 핵심 부분집합 29 s 면 3 × 29 = **87 s [산식]**.
- 이 값이 A안을 배제하는 두 번째 근거다(첫째는 §1 의 "지금 main 이 빨강").

### 2.2 크론 부하 상한

24시간 커밋 66건 [실측] → SHA 변경 시에만 실행하면 최대 66회 × 68.7~75.1 s = **약 76~83 분/일 [산식]**(중복 제거·버스트 병합 전 상한; 실제는 이보다 작다 — 최근 24시간 커밋 간격 65개 중 23개가 10분 미만 [실측]).

---

## 3. 후보 3안 비교

| 안 | 동작 방식 | 탐지 지연 | 개발자 체감 비용 (커밋·푸시 지연) | 거짓양성 위험 | 누락 위험 | 판정 |
|---|---|---|---|---|---|---|
| **A** pre-push 전량 | push 시 53개 파일 전량을 푸시 SHA 트리에 대해 실행, 실패 시 차단 | 0 (push 전 차단) | 커밋 +0 s, **push +69~75 s** [실측] + 락 안 직렬화 225 s [산식] (§2.1) | **매우 높음** — 도입 즉시 main 이 빨강(1 failed [실측])이라 **무관한 모든 push 가 막힌다.** 타이밍 민감 후보(§2), docker 이미지 해석 실패(rc=2) 시 push 차단 | 낮음(우회 시 전부 누락). 훅 미설치 환경(원격 서버 push, `--no-verify`)은 누락 | **비권장** |
| **B** 크론 전량 | 10분 tick, origin/main SHA 가 바뀐 때만 `git archive` 내보내기 → 전량 실행 → 실패 시 error_log UPSERT + error_book 후보 + 텔레그램(중복 억제) | **≤ 10분 + 75 s ≈ 11.3분** [산식: tick + 실측] | **0 s** (개발 경로에 없음) | 낮음 — 차단이 아니므로 우회 유인이 없다. 대신 **알림 스팸 위험**(§6에서 대책) | 사후 탐지 — 깨진 커밋이 main 에 들어간 뒤 최대 ~11분간 배포 큐에 노출. 크론 자체 정지 시 누락(심장박동 필요) | 단독은 **비권장**, C 의 1단계 |
| **C** A+B 혼합 | B 전부 + pre-push 에서 핵심 부분집합(17개)만 **"새로 생긴 실패만"** 차단 | pre-push 에서 부분집합 계약은 0, 나머지·조합은 ≤ 11.3분 | push **+24~29 s** [실측], 락 안 87 s [산식] | 중간 → **귀속 규칙**(§5)으로 낮춤: 크론이 아는 기존 빨강은 통과, 새 실패만 차단, 실행 불가(rc=2)는 fail-open | 낮음 — 부분집합이 놓친 것(36개 파일·조합 오염)은 크론이 잡는다. 크론 정지 시 pre-push 가 노란 경고 | **권장** |

C 가 A 를 대체하는 이유가 "싸서"만은 아니다: A 는 정확하지만 **지금 상태에서는 켜는 순간 게이트가 거짓 양성이 된다.** C 는 크론(차단 없음)이 먼저 현실을 재고, pre-push 는 그 결과를 기준선으로 삼는다.

---

## 4. 권장안 (R-APPROVAL-REC)

**권장: C안 — 크론 전량(B) 먼저, pre-push 핵심 부분집합은 경고형으로 시작해 승인 후 차단형 전환.**

### 4.1 권장 사유 (실측 근거)
1. 게이트 미연결 파일 36/53 [실측]. 경로 매핑 보강은 S1·S2 만 줄이고 신규 파일이 생기면 다시 낡는다 → 경로 무관 검증(크론 전량)이 필요.
2. main 이 지금 빨강(1 failed [실측])이고 원인이 **테스트 간 오염** — 부분집합·경로 선택 어느 쪽도 못 잡고 전량 단일 프로세스 실행만 잡았다. 전량 실행이 필수.
3. push 락 안 직렬화(§2.1)로 pre-push 는 29 s 이하여야 안전 → 전량은 크론, 부분집합만 pre-push.
4. 크론 부하 상한 76~83 분/일 [산식], 개발자 체감 0 s.

### 4.2 비권장 사유 (안별 1줄)
- **A**: 도입 즉시 전 push 차단(main 빨강 [실측]) + 락 안 225 s [산식] → 우회 습관과 `push_exit=75` 를 낳는다.
- **B 단독**: 사후 탐지라 깨진 커밋이 main 에 올라간 뒤 ≤ 11.3분 노출되고, 실패 지점(push)에 마찰이 없어 같은 사고가 반복돼도 사람이 늦게 안다.

### 4.3 승인 시 실제로 바뀌는 파일 (구현은 별도 러너 작업)
| 파일 | 변경 |
|---|---|
| `tests/unit/test_deploy_observability.py`, `tests/unit/test_deploy_adapters.py` | 0단계: 수집 시점 `sys.modules` 오염 제거(기준선 초록화) |
| `scripts/pre_commit_test_map.py` | `CONTRACT_FULL`(glob, `.bak_aads` 제외)·`CONTRACT_FAST`(= runner+deploy GROUPS 의 tests 합집합) 정의, `--list-contract full\|fast` 출력 모드 |
| `scripts/deploy_contract_watch.py` (**신규**) | 크론 작업자 + `--prepush` 진입점 |
| `scripts/hooks/pre-push` | 계약 단계 추가(모드 파일이 `off` 면 아무것도 안 함) |
| `scripts/run_unit_tests.sh` | 필요 시 `AADS_REPO_ROOT` 오버라이드(푸시 SHA 내보내기 트리용) |
| `tests/unit/test_deploy_contract_watch.py` (**신규**), `tests/unit/test_pre_commit_test_map.py`, `tests/unit/test_dup_guard.py` | 단정 추가 |
| crontab + `scripts/aads-crontab.txt` | 1줄 추가 (**CEO 승인 항목** — 아래 단계 3) |
| `CLAUDE.md` R-QUALITY 절 | "게이트가 실제로 도는 것" 목록 갱신(문서가 낡아 사고 났던 전례) |
| `.git/hooks/{pre-push,...}` | `cp scripts/hooks/... .git/hooks/` (설치본 동기화) |

### 4.4 롤백 경로
- pre-push: 상태 디렉터리의 `mode` 파일을 `off` 로 (커밋 불필요, 즉시). 완전 제거는 해당 커밋 revert + `cp scripts/hooks/pre-push .git/hooks/`.
- 크론: crontab 해당 줄을 `# DISABLED_<날짜>_<사유> ` 로 주석(기존 관례). 스크립트는 남겨도 무해.
- 0단계 테스트 수정: 커밋 revert. 단, revert 하면 묶음이 다시 빨강이 된다 — 롤백 시 크론도 함께 끈다.

### 4.5 승인하지 않을 때 유지되는 상태
- 게이트는 지금과 같다: 경로 의존, 17/53 연결, pre-push 는 서명만 확인.
- origin/main 의 `test_error_book_runner_failure_hook` 실패는 그대로 남고 **아무도 모른 채** 유지된다(이번에 우연히 발견됨). 최소한 이 1건은 승인 여부와 무관하게 별도 작업으로 고칠 것을 권한다.
- 재발 시나리오 S1~S6 은 모두 열려 있다.

---

## 5. 거짓양성 대책 (R-PUSH 반영)

교훈: 게이트가 거짓 양성을 내면 `ALLOW_FORCE_PUSH=1` 이 습관이 되고 진짜 위반도 같이 통과한다(2026-09-14 오진 → 우회 사례).

### 5.1 이 설계에서 예상되는 거짓양성 원인과 대책

| 원인 | 근거 | 대책 |
|---|---|---|
| 이미 빨간 main | 지금 `1 failed` [실측] | **귀속 규칙**: 크론 상태 파일의 `red_nodeids` 에 있는 실패는 "이번 push 탓 아님" → 노란 경고 후 통과. **새로 생긴 실패만 차단** |
| 테스트 간 오염 | S5 [실측] | 0단계에서 제거 + 크론 실패 시 "단독 재실행 통과 → `order_dependent` 태그" 로 원인 분류를 알림에 포함 |
| 실행 불가(rc=2): docker/이미지 해석 실패, 블루그린 컷오버 창 | `run_unit_tests.sh` 재시도 30 s 후 rc=2 | pre-push 는 **fail-open**(경고 + 기록). pre-commit 과 달리 크론이 이중 안전망이기 때문. 차단하면 인프라 장애가 곧 push 마비 |
| 타이밍 민감 가능 테스트 | 느린 상위 4건이 1~5 s 대기형으로 추정(§2, 미검증) | 크론: 실패 시 전량 1회 재실행, 같은 nodeid 집합이 다시 실패해야 "확정". 첫 실패는 기록만 |
| 부하 | 측정 시 load 6~8 | pre-push 부분집합에 timeout(기존 `GROUP_TIMEOUT_SEC=900` 재사용 금지 → 부분집합 실측의 3배 이내로 별도 설정, 초과 시 rc=2 로 fail-open) |
| 스테이지 트리 ≠ push 트리 | S4 | 푸시 SHA ≠ 작업트리 HEAD 이면 `git archive <sha>` 내보내기 트리로 실행(§2 [미측정] 비용 확인 후) |
| 크론 죽음 | — | 상태 파일의 `last_tick`; pre-push 가 1시간 초과 시 노란 경고 |

### 5.2 우회 환경변수: **새로 두지 않는다**
- 이유: 새 우회 변수는 그 자체가 습관이 된다(`ALLOW_DUP_COMMIT`, `ALLOW_ENV_DUP` 등 이미 다수). 정당한 통과 사유는 위 규칙(기존 빨강 귀속, 실행 불가 fail-open)이 코드로 흡수한다.
- 정말 필요한 비상 시에는 기존 `ALLOW_FORCE_PUSH=1` (CEO 승인) 만 쓴다. 단 다음을 추가한다: 이 변수로 통과하면 `$GIT_DIR/hook_bypass.log` 에 `시각 / 사용자 / 대상 SHA` 한 줄을 남긴다.
- **측정 가능한 종료 조건**: 이 로그의 `ALLOW_FORCE_PUSH` 사용이 계약 단계 도입 후 **주 3회 이상**이면 게이트를 거짓양성 판정하고 `mode=warn` 으로 되돌린 뒤 원인을 오류 사전에 등록한다(임계 3 은 설계상 초기값이며 첫 2주 로그로 조정).
- 차단 메시지 순서는 R-PUSH 와 동일: ① 새 실패 nodeid 목록 ② 크론 상태(`sha`, `age`) ③ 그 다음에야 우회 언급. `--no-verify` 를 단정하지 않는다.

---

## 6. 알림 정책 (크론 안)

전례: crontab 의 `DISABLED_20260605_TELEGRAM_SPAM` 항목들은 5분 주기 감시가 텔레그램 스팸으로 꺼진 것이다. **원인의 세부(중복 억제 부재 여부)는 이 세션에서 확인하지 못했다** — 기록된 것은 "스팸으로 꺼짐"뿐이다. 그래서 아래는 "가능한 스팸 경로를 모두 막는" 방향으로 설계한다.

### 6.1 기록 채널 (스팸 없음, 항상 기록)
1. `error_log` UPSERT — `error_hash = sha1(sorted(failed_nodeids))`, `ON CONFLICT (error_hash) DO UPDATE SET occurrence_count = occurrence_count + 1, last_seen = NOW()` (`migrations/052`, `app/services/model_selector.py:2801` 와 같은 패턴, watchdog 규칙 L-007). 같은 빨강 집합은 한 행.
2. 오류 사전 후보 — `python3 scripts/error_book.py match - --record --source deploy-contract-watch` (알 수 없는 오류는 `status=candidate`, 원인은 사람이 promote — R-ERRBOOK: 추측 기재 금지).
3. 로그 `/var/log/aads-deploy-contract-watch.log`, 상태 `/var/lib/aads-deploy-contract-watch/state.json`(`sha`, `failed_nodeids`, `last_tick`, `first_red_at`, `alerts_sent_today`).

### 6.2 텔레그램 (사람을 부르는 채널) — 엄격 억제
- **알림 채널**: `scripts/container_watchdog.sh:34~` 의 `send_telegram()` 패턴(`.env` 의 `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`, `curl -fsS`)을 재사용한다. 이 패턴은 이미 운영 중이다.
  - **쓰지 않는다**: `codex_auth_monitor.py:32` 의 `POST http://localhost:8080/api/v1/ops/alert`. 이 세션에서 `app/api/*.py` 에 `/alert` 라우트가 grep 에 안 잡혔고 `localhost:8080` POST 는 연결 실패(000)였다 [실측]. 예외를 삼키는 코드(`except Exception: pass`)라 죽은 채 있어도 티가 안 난다.
  - 구현 1회 `--selftest-alert` 로 테스트 메시지 1건을 보내 채널을 검증한다.
- **연속 실패 임계값**: 서로 다른 SHA 에서 **2회 연속** 같은 `failed_nodeids` 집합이 확정(재실행 확정 포함)될 때만 첫 알림. 1회성 실패(플레이크 후보)는 기록만.
- **중복 억제**: `error_hash` 당 24시간 1회. 집합이 바뀌면(새 hash) 새 알림. 빨강이 이어지면 24시간마다 최대 1회 리마인드.
- **전역 상한**: 이 작업 전체 텔레그램 **일 3건**. 초과분은 다음날 09:00 KST 요약 1건으로 합친다.
- **복구 알림**: 빨강 → 초록 전환 시 1건(같은 hash 재알림 방지 위해 `first_red_at` 초기화).
- **킬 스위치**: `/var/lib/aads-deploy-contract-watch/alerts.off` 파일이 있으면 텔레그램만 끄고 기록은 계속(크론을 끄지 않아도 스팸을 즉시 멈춤).
- **조용한 tick**: origin/main SHA 가 마지막 검사 SHA 와 같으면 로그 1줄 없이 종료 → 변경 없는 시간대에 알림/로그 없음.
- **배포와 충돌 방지**: `flock -n /tmp/pipeline-deploy-AADS.lock` 실패(배포·push 중)면 이번 tick 을 건너뛰고 로그 1줄(다음 tick 에 재시도). 건너뜀은 실패로 세지 않는다.
- **R-BG**: 모든 실행에 `timeout 900`, 내보내기 임시 디렉터리는 `trap` 으로 삭제.

### 6.3 스팸 재발 방지 근거 요약
| 스팸 경로 | 차단 장치 |
|---|---|
| 주기마다 같은 실패 재알림 | SHA 불변 시 종료 + hash 당 24 h 1회 |
| 플레이크 1회로 호출 | 2회 연속·재실행 확정 |
| 빨강이 길게 지속 | 24 h 1회 리마인드 + 일 3건 상한 |
| 인프라 장애(docker)로 연쇄 | rc=2 는 "실패"가 아니라 `unavailable` — 알림 대신 기록, 연속 6 tick(1시간)일 때만 1건 |
| 원인 불명으로 계속 울림 | 킬 스위치 파일 |

---

## 7. 구현 단계와 완료기준 (후속 러너용)

공통: 코드 변경 러너는 `bash scripts/run_unit_tests.sh <파일>` 로 검증한다. 각 단계는 독립 커밋. 단계 3 이 끝난 시점 = **B안 완성(체크포인트)**, 여기서 멈춰도 상태가 일관된다.

### 단계 0 — 기준선 초록화 (선행, 이것 없이는 어떤 단계도 켜지 않는다)
- 파일: `tests/unit/test_deploy_observability.py`(12행), `tests/unit/test_deploy_adapters.py`(12행). 수집 시점의 `sys.modules.setdefault("structlog", 가짜)` 를 제거하거나, 모듈 로드 직후 원래 값으로 복원.
- 완료기준:
  - `bash scripts/run_unit_tests.sh $(ls tests/unit/test_deploy*.py tests/unit/test_pipeline_runner*.py tests/unit/test_runner_*.py tests/unit/test_external_deploy_ledger_sync.py tests/unit/test_error_book_runner_failure_hook.py tests/unit/test_reclaim_runner_worktrees.py \| grep -v bak_aads)` → **exit 0, `628 passed`, `0 failed`** (개수는 그때 재측정).
  - 같은 명령을 **연속 3회** 돌려 모두 exit 0(플레이크 확인).
  - `bash scripts/run_unit_tests.sh tests/unit/test_deploy_adapters.py tests/unit/test_error_book_runner_failure_hook.py` → exit 0(오염 회귀 방지).

### 단계 1 — 묶음 정의 단일화
- 파일: `scripts/pre_commit_test_map.py`, `tests/unit/test_pre_commit_test_map.py`.
- 내용: `CONTRACT_FULL` = glob(`tests/unit/test_deploy*.py`, `test_pipeline_runner*.py`, `test_runner_*.py`, `test_external_deploy_ledger_sync.py`, `test_error_book_runner_failure_hook.py`, `test_reclaim_runner_worktrees.py`), `*.bak_aads` 제외. `CONTRACT_FAST` = GROUPS 의 runner+deploy `tests` 합집합. `--list-contract full|fast` 로 줄 단위 출력. 신규 테스트 파일이 자동 편입되도록 glob 으로 둔다(손 목록은 낡는다).
- 완료기준:
  - `python3 scripts/pre_commit_test_map.py --list-contract full \| wc -l` = 단계 0 의 파일 수(53 이상), `--list-contract fast \| wc -l` = 17 (`FAST ⊆ FULL` 을 테스트가 단정).
  - `bash scripts/run_unit_tests.sh tests/unit/test_pre_commit_test_map.py` → exit 0.
  - 기존 pre-commit 동작 불변: `echo scripts/deploy.sh \| python3 scripts/pre_commit_test_map.py` 가 종전과 같은 6줄.

### 단계 2 — 크론 작업자
- 파일: `scripts/deploy_contract_watch.py`(신규), `tests/unit/test_deploy_contract_watch.py`(신규).
- 내용: `git fetch origin main` → SHA 비교(변경 없으면 종료) → `flock -n` → `git archive` 로 임시 디렉터리 내보내기 → `run_unit_tests.sh` 전량(`timeout 900`) → 실패 시 전량 1회 재실행 확정 → §6 기록·알림·상태. `--once`, `--no-alert`, `--dry-run`, `--selftest-alert`, `--prepush` 옵션. `error_log` UPSERT 와 텔레그램 발송은 주입 가능한 함수로 분리해 단위 테스트에서 가짜로 검증.
- 단위 테스트가 단정할 것: SHA 불변 시 실행 안 함 / 락 점유 시 건너뜀 / 같은 hash 24 h 내 재알림 없음 / 2회 연속 미만은 알림 없음 / 일 3건 상한 / rc=2 는 `unavailable` / 킬 스위치 / 임시 디렉터리 정리.
- 완료기준:
  - `bash scripts/run_unit_tests.sh tests/unit/test_deploy_contract_watch.py` → exit 0.
  - `python3 scripts/deploy_contract_watch.py --once --no-alert` 실행 → exit 0(초록 기준선) 또는 rc 1 + 로그에 실패 nodeid; `/var/lib/aads-deploy-contract-watch/state.json` 의 `sha` 가 `git rev-parse origin/main` 과 일치.
  - `python3 scripts/deploy_contract_watch.py --once --no-alert` 를 즉시 다시 실행 → 로그 증가 0줄, 실행 시간 < 5 s(SHA 불변 조용한 종료).
  - 인위적 빨강 검증(임시 export 트리에서 테스트 1개를 깨뜨림, **저장소 파일은 건드리지 않음**): `error_log` 에 `error_hash` 행 1개, 재실행 시 `occurrence_count` 만 증가 — `psql` 로 `SELECT error_hash, occurrence_count FROM error_log WHERE source='deploy-contract-watch'`.

### 단계 3 — 크론 등록 (CEO 승인 후) → **B안 체크포인트**
- 파일: crontab, `scripts/aads-crontab.txt`. 줄: `*/10 * * * * cd /root/aads/aads-server && timeout 900 python3 scripts/deploy_contract_watch.py --once >> /var/log/aads-deploy-contract-watch.log 2>&1 # AADS_CONTRACT_WATCH`
- 완료기준:
  - `crontab -l \| grep AADS_CONTRACT_WATCH` 1줄. `scripts/aads-crontab.txt` 와 일치.
  - `python3 scripts/deploy_contract_watch.py --selftest-alert` → 텔레그램 테스트 1건 수신(exit 0).
  - 30분 후 `/var/log/aads-deploy-contract-watch.log` 에 tick 기록 존재, `pgrep -f deploy_contract_watch \| wc -l` = 0 으로 잔존 프로세스 없음(R-BG).
  - **3일 후 검토**: 로그의 실행 횟수·평균 소요·알림 건수를 집계해 §2.2 상한(≤ 83 분/일)과 알림 일 3건 상한을 만족하는지 확인. 미달이면 tick 주기 조정.

### 단계 4 — pre-push 핵심 부분집합 (경고형 시작)
- 파일: `scripts/hooks/pre-push`, `scripts/deploy_contract_watch.py --prepush`, `tests/unit/test_dup_guard.py`.
- 내용: 서명 검증 뒤 `mode` 파일(기본 `warn`, 값 `off|warn|block`)이 `off` 면 통과. 아니면 `CONTRACT_FAST` 실행(푸시 SHA ≠ HEAD 면 내보내기 트리) → rc 0 통과 / rc 2 fail-open+기록 / rc 1 이면 §5 귀속 규칙으로 판정: `warn` 은 출력만, `block` 은 새 실패에 한해 exit 1. `ALLOW_FORCE_PUSH=1` 경로에 `hook_bypass.log` 기록 추가. **새 우회 변수 없음.**
- 완료기준:
  - `bash scripts/run_unit_tests.sh tests/unit/test_dup_guard.py` → exit 0 (계약 단계 마커, `hook_bypass.log`, "새 우회 변수 없음" 단정 포함).
  - 임시 bare 저장소 대상 `git push` 실측으로 **[미측정] 항목 두 개(훅 오버헤드, 내보내기 시간)를 채워** 이 문서 §2 에 추기.
  - `mode=warn` 에서 인위적 실패로 push → exit 0 + 경고 출력; 기존 빨강 nodeid 만 실패 → 귀속 메시지; `mode=off` → 출력 없음.

### 단계 5 — 훅 설치본 동기화 및 문서 (hook 동기화 주의)
`scripts/hooks/` 가 정본이고 `.git/hooks/` 는 설치본이다. **저장소본만 고치면 게이트는 옛 코드로 돈다**(CLAUDE.md R-QUALITY).
- 명령: `cp scripts/hooks/{pre-commit,commit-msg,pre-push,post-rewrite} .git/hooks/ && chmod +x .git/hooks/{pre-commit,commit-msg,pre-push,post-rewrite}`
- 완료기준:
  - `bash scripts/run_unit_tests.sh tests/unit/test_dup_guard.py` → exit 0 (`test_installed_hooks_stay_synced_with_repo_copies` 통과 포함).
  - `for h in pre-commit commit-msg pre-push post-rewrite; do cmp scripts/hooks/$h .git/hooks/$h && echo ok $h; done` → 4줄 모두 `ok`.
  - `CLAUDE.md` R-QUALITY 의 "게이트가 실제로 도는 것" 문단에 크론·pre-push 계약 단계와 `mode` 파일 위치를 반영(문서가 낡아 사고 난 전례). `git diff --stat` 에 CLAUDE.md 포함.
  - 오류 사전 등록: `python3 scripts/error_book.py register --key gate.pathdep_contract_uncovered --symptom "배포계약 테스트가 경로 의존 선택 때문에 게이트 밖에서 실패한 채 방치" --cause "<확인된 것만: GROUPS 가 17/53 연결, structlog 수집 시점 오염>" --prevention "..." --signature "<정규식>" --fix-commit <단계 0 SHA> --fix-note "..."`, 이어서 `python3 scripts/error_book.py list \| grep gate.pathdep_contract_uncovered` 1건.

### 단계 6 — 차단형 전환 (별도 CEO 승인, 지금은 승인 대상 아님)
- 조건: 단계 3 이후 **2주간** (a) 크론 거짓양성 확정 0건, (b) `hook_bypass.log` 의 `ALLOW_FORCE_PUSH` 사용 주 3회 미만, (c) pre-push `warn` 출력 중 오탐 0건. 충족 시 `mode` 파일을 `block` 으로 바꾼다(커밋 불필요). 미충족 시 `warn` 유지.

---

## 8. 작업 중 남은 확인 사항 (추측 없이 남김)

- `DISABLED_20260605_TELEGRAM_SPAM` 항목이 실제로 어떤 경로(중복 미억제, 주기 과다, 오탐)로 스팸이 됐는지는 확인하지 못했다. §6 은 가능한 경로를 모두 막는 방식이며 원인 규명이 필요하면 별도 조사.
- 원격 서버에서 직접 push 하는 경로(훅 미설치)는 pre-push 로 잡히지 않는다. 크론이 유일한 안전망이다.
- 전량 묶음의 파일 구성은 `ls tests/unit \| grep ...` 패턴 기준이다. 배포계약에 속하지만 이름이 이 패턴에 안 맞는 테스트가 있는지는 조사하지 않았다(단계 1 의 glob 갱신으로 흡수).
- HANDOVER.md 는 이 작업의 승인 범위(문서 1개)에 없어 수정하지 않았다. 구현 러너가 R-001 에 따라 갱신한다.
