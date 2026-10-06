# GO100 PRD 실행 작업의 재큐잉·산출물 보호

2026-10-06 · 오류사전 `runner.requeue_leaves_previous_attempt_running`

GO100 구현 작업의 end-to-end 검수에서 살아 있는 원격 Runner의 heartbeat가 오래됐다는 이유로 API 재시작이 작업을 재큐잉한 기록을 확인했다. 기존 작업 폴더를 같은 경로의 새 main 기준선으로 교체하는 동안 이전 실행이 끝나면, 이전 시작 SHA부터 새 HEAD까지의 다른 작업 변경을 자신의 산출물로 읽을 수 있다. 실제 #119 복구 작업의 승인 대기 diff가 DR02 커밋의 앞 5,000자와 일치했고 해당 #119 신규 파일은 작업 폴더에 없었다. 그 시점의 코드·시험·승인 대상은 일치하지 않으므로 승인할 수 없다.

## 변경

- API startup Phase0의 requeue·조회·error 확정 쿼리 모두 `runner_host IS NULL` 소유권 조건을 적용한다. 외부 host의 heartbeat 누락·만료는 미확인 생존 상태이며 외부 실행을 재큐잉할 권한이 아니다. 외부 실행의 복구는 해당 Runner/watchdog이 PID·attempt·산출물을 확인한 뒤 수행한다. 기존 API-local error 복구와 detached/restarting 경로는 유지한다. 기존 remote requeue 설정은 호환 목적으로 남지만 API startup에서는 원격 작업을 변경하지 않는다.
- 독립 검수에서 발견한 과거 remote error의 자동 재실행 누락도 막는다. Phase0.5 detached 결과 수거, Phase0a 과거 error 재실행, Phase1 배포/재시작 복구의 SELECT에 동일한 API 소유권 조건을 적용한다. API가 직접 관리하는 SSH 실행은 `runner_host IS NULL`로 기존 경로를 유지한다.
- 기존 worktree의 cwd를 사용하는 프로세스가 있으면 fetch와 제거 전에 차단하고, fetch 후에도 다시 확인한다. 프로세스·HEAD·inode·미커밋/미추적 파일을 보존하며 프로세스를 종료하지 않는다.
- `/proc/<pid>/cwd`의 삭제 표기 ` (deleted)`를 정규화하고 경로가 사라졌어도 이전 cwd를 검사한다. 경로가 없다는 이유로 새로운 worktree를 만들지 않는다. 삭제된 root/하위 cwd, 같은 경로의 재생성, 이름이 비슷한 별개 경로를 실제 임시 프로세스로 검증한다.
- 재큐잉 변경 범위 검사는 신규 A 및 미추적 파일까지 포함한다. 지시 범위를 벗어난 파일을 되돌리거나 삭제하지 않고 승인 전에 차단한다.
- 작업의 clean HEAD가 시작 기준선과 달라졌다는 사실만으로 산출물 commit을 채택하지 않는다. 시작 commit과의 ancestry 및 공용 main에 이미 포함된 HEAD 여부를 확인하며, 다른 main 기준선이면 provenance 오류로 차단한다. 정상 worker 자체 commit과 Runner commit 경로는 회귀로 유지한다.
- 준비·승인 commit 차단 시 `.out`/`.err`를 삭제하지 않아 원본 결과의 후속 대조가 가능하다.

## 검증

API의 실제 함수에서 생성한 SQL을 SQLite fixture로 실행했다. 후속 phase까지 포함한 기존 source는 22 failed / 14 passed이며, 수정 후 36건이 통과했다. 원격 host의 stale·missing·fresh heartbeat, PID 유무와 실행 phase, API-local error 복구 및 기존 제외 상태를 확인한다. SQL 전송과 애플리케이션 startup만 대체하며 운영 DB에는 연결하지 않는다. 대상 파일 해시는 별도 최종 영수증으로 기록한다.

Shell의 실제 함수와 임시 Git 저장소·살아 있는 cwd 프로세스로 39개 회귀를 검증한다. 최초 수정 31건에 삭제 cwd의 부재/재생성/하위 경로 반례 6건과 별개 경로의 경계 확인 2건을 추가했다. 최초 commit c30388e6은 새 반례 6건에서 실패했고 보강 source의 최종 결과를 별도 영수증에 남긴다. 신규/미추적 파일, 관계없는 main 기준선, 정상 독립 commit·신규 worktree, 차단 시 원본 파일·inode·HEAD 보존을 확인한다. Shell 2본의 내용이 같고 bash 문법·diff 검사를 통과해야 한다. 기존 호환 시험의 3 FAIL / 4 ERROR는 완전한 변경 전 source에서도 동일한 시험 ID로 재현되는 sweeper의 구형 문자열 경계 문제이며 별도 기록했다. 이 기준선 문제를 신규 회귀의 PASS로 바꾸지 않았다.

새 회귀 75건(API36+Shell39)은 정상 커밋 훅의 runner 시험 묶음에도 연결했다. 기존 승인 commit 시험은 새 provenance 검증 함수도 실제로 추출하며, 정상 worker의 독립 commit fixture와 배포 전용 무변경 경로를 함께 검증한다. 테스트 목적으로 만든 격리 worktree의 임시 설정만 사용하고 운영 설정은 변경하지 않는다. 커밋 훅과 별도 호환 시험의 결과는 구분하여 보존한다.

이 변경은 운영 적용 전 코드 교정이다. 실행 중 작업·worktree·DB 상태·환경변수·서비스를 변경하지 않았다. 현재 잘못된 승인 대기를 이 코드 commit만으로 복구 완료로 처리하지 않는다. 정확한 산출물과 단일 작성자가 확인된 후 좁은 재작업·재검수로 복구해야 한다.

## 남은 통합 조건

현재 두 번의 cwd 확인은 전체 작업 생명주기의 atomic lease가 아니다. 모든 host-side 재큐잉·write·승인 전이에는 attempt/runner PID를 결합한 CAS와 단일 작성자 잠금, 원본 산출물 보존 계약을 추가 검수해야 한다. 이 변경을 전체 attempt fencing 완료로 보고하지 않는다.

API와 실제 host Runner는 서로 다른 적용 대상이다. 정확한 release SHA와 source hash를 각각 확인하고, drain·불변 릴리스·정상 hook·건강검사 및 운영 검증을 통과한 뒤 적용해야 한다. main push나 branch push가 실제 API/Runner 적재를 의미하지 않는다. GO100의 38개 태스크·32개 요구사항·36개 인수 시나리오 및 3거래일 SLO는 별도 완료 원장으로 추적한다.

## main 통합 전 자동 동기화 보호

후속 main 통합에서 5분 주기 동기화 타이머가 origin/main을 export하고 원격 스크립트를 설치·재시작하는 경로를 확인했다. 기존 DB guard는 terminal job의 살아 있는 CLI를 보지 못하고, inactive 서비스면 고아 자식도 없다고 가정했다. 실제 GO100 Runner는 `KillMode=control-group`이며 삭제된 작업 cwd의 CLI가 같은 cgroup에 남아 있었다. main 게시를 보류하고 통합 커밋은 검토 브랜치에 보존했다.

추가 guard는 공용 `runner_busy_lib.sh`의 stdlib probe를 SSH stdin으로 전달한다. `/proc`의 실제 cwd(삭제 표기 포함), MainPID 자손, 서비스 cgroup을 함께 조회하고 작업자가 있거나 조회가 불완전하면 설치·재시작을 미룬다. DB terminal, inactive 상태, `--ignore-busy`도 이 프로세스 확인을 우회하지 못한다. 로컬 재시작 래퍼에도 같은 계약을 적용한다. 명확한 작업자 부재만 기존 DB 판정에 넘기며, 유휴 polling 자식까지 보수적으로 defer할 수 있다.

이 관측은 claim과 restart 사이의 atomic lease가 아니다. 기존 capacity flock은 일부 queued claim에만 적용되며 동기화·승인·반려 복구와 공유하지 않는다. 관측 직후 새 claim이 발생하는 경합은 남아 있다. 전체 유지보수/claim 직렬화와 실제 작업자 drain이 검증될 때까지 이 추가 commit도 main 게시·자동 runtime 적용 완료로 보고하지 않는다. 테스트는 실제 임시 PID와 삭제 cwd, cgroup/권한 오류 fixture, 실제 Bash 설치·재시작 gate를 검증하며 운영 프로세스를 중단하지 않는다.

## Host maintenance protocol v1 — source candidate, runtime held

The earlier `6fa7b22a` process/DB observation guard remains a conservative prefilter.
An idle observation is insufficient: a claim can begin immediately afterwards.
The new mandatory host protocol closes that gap for enrolled protocol-aware
Runner actors. It does not automatically bootstrap an existing legacy daemon.

Both general and LiteLLM engines use stable, root-owned files under
`/run/aads-runner-maintenance`. Lock files must never be unlinked while actors
are alive. Admission and lifetime locks are always acquired in that order.

1. Before credentials or startup DB recovery, the daemon acquires admission SH,
   then lifetime SH on FD 200, and closes admission. Every background worker and
   normal CLI descendant inherits lifetime ownership. This covers queued,
   approved, rejected and review-hold recovery claims, startup/periodic recovery,
   watchdog operations and shutdown handling for the daemon's entire lifetime.
2. A controller verifies actual PID, boot ID, start ticks, Bash source FD 255,
   lifetime FD/inode/READ lock, service startup path, and capability. It takes
   admission EX; this lock itself is the drain signal. The current daemon
   iteration may still claim before its next checkpoint; lifetime SH continues
   protecting those workers. The actual no-worker install boundary is lifetime
   EX, not the instant admission EX is acquired. No persisted drain flag,
   wall-clock expiry or stale-owner override can release a live actor's lease.
3. At its next checkpoint the daemon stops new claims and waits for tracked
   workers to finish. It records QUIESCENT, closes only its own FD 200 and waits
   for admission again. It stays alive: exiting under `KillMode=control-group`
   could kill orphan workers. It never calls shutdown requeue or sends signals
   as part of cooperative drain. Descendants retaining FD 200 still block EX.
4. The controller must acquire lifetime EX and then check actual proc/cgroup
   membership again. Only a verified quiescent MainPID and its exact admission
   `flock -s 201` control waiter are exempt; a process name alone is insufficient.
   A worker that dropped its inherited FD, an orphan/deleted cwd, an unknown
   member or incomplete observation still defers. A timeout releases admission
   and lets the daemon resume; it cannot force worker termination.
5. Remote sync stages one checksum-pinned bundle. One remote Bash transaction
   owns both EX locks through validation, atomic file replacement, optional
   daemon-reload and service restart. Before restart it pins the previous PID
   and installed source/helper hashes. While both EX locks remain held, success
   requires a different MainPID in WAIT_ADMISSION with exact boot/start identity,
   source FD 255 hash, helper hash and admission FD 201. systemctl is-active alone
   cannot certify startup. Verification failure returns failure without any
   forced kill or DB requeue. Per-file SCP followed by independent SSH
   install/restart is removed. Runner backups are retained by bundle hash;
   interrupted file application restores completed replacements while the
   lease is still held. An apply child inherits the controller's EX descriptors,
   so losing the controlling shell cannot expose an in-progress install.
6. Local restart and `safe_runner_restart_once.sh` use the same transaction.
   Self-reload is disabled while protocol v1 is active; updates use the verified
   controller path. Source fingerprints describe the running script inode,
   not a newly replaced pathname. `--no-restart` remains a source-only install;
   it is not evidence that the running daemon uses the new source.

The existing prefilter may defer on a live polling child or any active DB job.
This patch does not change that conservative admission policy: the lease handles
claims/workers that begin after those observations. It does not promise immediate
maintenance admission or proactively interrupt work to obtain a drain window.

### Bootstrap and acceptance boundary

Repository investigation found no enabled/drain/accepting-jobs control in
`pipeline_runner_hosts` or its status API. `owner_pause` gates goal dispatch,
not host claim SQL. Environment files are loaded at daemon start. FD 9 is a
legacy lifetime singleton, not a pause handshake; the optional FD 8 capacity
lock covers only queued claim SQL. The actual old claim function still reaches
its DB boundary while both proposed maintenance locks are exclusively held.

Active legacy actors without a valid capability return BOOTSTRAP_REQUIRED.
Inactive/failed legacy services also defer unless a prior protocol actor enrolled
that exact service and the installed source/helper hashes and ExecStart still
match. Inactive observation alone cannot prevent a concurrent legacy startup.
The enrollment and capability directory is host-root trusted metadata, not a
cryptographic authentication boundary against a malicious root administrator.

No automatic override is supplied. The initial transition requires a separately
authorized operational mechanism that prevents all legacy claims before stopping
or replacing the daemon. This source change does not establish that mechanism,
stop timers, signal production processes, change job status, push main or apply
runtime. These remain explicit release blockers. All normal automated restart
entrypoints in this repository are enrolled; arbitrary direct administrative
`systemctl` calls must not bypass the protocol.

API-owned SSH-detached work (`runner_host IS NULL`) and distributed attempt/SHA
CAS remain separate ownership work. The CLI auto-updater already calls the local
restart wrapper, but its preceding service drop-in write is a separate
configuration-install boundary, not covered by this Runner bundle transaction.

Tests use actual local Bash, flock locks, temporary PID descendants and real
script/fd identities. Synthetic systemctl/proc membership substitutes only the
host service boundary. The export fixture runs actual checksum/atomic-install
code and asserts both EX locks are held during its single apply SSH. Historical
359-test integration and 100-test observation-guard hook runs overlap; they must
not be added to the new run as unique acceptance cases. Runtime/full PRD
acceptance remains incomplete until the independently reviewed source, initial
bootstrap and deployed actor ownership evidence are all verified.
