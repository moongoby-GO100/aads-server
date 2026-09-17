# AADS-GOALDISPATCH-DUPLICATE-SEND — 골 사이클 중복 발송

- 작성: 2026-09-18 (러너 runner-32f0dff8)
- 대상 저장소: `aads-server` 단일
- 변경 파일: `app/services/goal_dispatch.py`, `tests/unit/test_goal_dispatch_detached_send.py`, `tests/unit/test_goal_dispatch_claim.py`(신규)

---

## 1. 원인 특정

### 1-1. 확정 — "사이클이 30분 창을 어긴 것" 이 아니다. 발송 기록이 지워졌다

지시서의 증상은 "매 사이클이 `dispatch_count` 를 0 으로 읽었다" 였다.
그 **읽은 값이 실제 DB 값이었다는 것**까지 확정했다.

**근거 1 — 사이클 카운터 분해 (로그 실측)**

`docker logs aads-server-green` 22:26~22:40Z 구간.

| 시각(UTC) | sent | skipped | gave_up | 합 |
|---|---|---|---|---|
| 22:26:44 | 0 | 19 | 1 | 20 |
| 22:27:43 | 0 | 19 | 1 | 20 |
| **22:28:44** | **2** | **8** | 0 | 10 |
| **22:29:46** | **2** | **8** | 0 | 10 |
| 22:30:48 | 0 | 18 | 2 | 20 |
| **22:31:42** | **2** | **8** | 0 | 10 |
| 22:32:49 | 0 | 19 | 1 | 20 |

조회는 `LIMIT 20` 이므로 정상 사이클은 항상 20건을 센다. 발송이 있는
사이클만 10건인 이유는 `sent >= _MAX_PER_CYCLE` 분기가 **어떤 카운터도
올리지 않고** `continue` 하기 때문이다(집계 누락이지 결함은 아니다).
즉 발송 사이클에서 **발송 전에 건너뛴 것이 정확히 8건**이다.

**근거 2 — 그 8건이 무엇인지 DB 로 대조**

정렬은 `ORDER BY m.dispatched_at NULLS FIRST, m.sequence_order` 다.
현재 DB에서 `dispatched_at IS NULL` 이면서 조회 조건을 만족하는 행은
정확히 8건이고, 전부 `sequence_order` 1~4 이며 전부 담당이 없어 skip 되는
건이다.

```
65fa8d74|1|담당 역할이 지정되지 않음        f0e612bd|1|담당 역할이 지정되지 않음
a61cc334|1|담당 역할이 지정되지 않음        fe309b7f|2|담당 역할이 지정되지 않음
5fd71db4|3|담당 역할이 지정되지 않음        af16f109|3|담당 역할이 지정되지 않음
6615ee97|3|주도 세션이 없어 승인 요청을…    21d4e100|4|담당 역할이 지정되지 않음
```

문제의 마일스톤은 목표 36da9794 의 `05fdc2bb`(seq 5), `d195f3d2`(seq 6),
`36fe6a2c`(seq 7) 다. **이들이 그 8건 바로 뒤(9·10번째)로 잡히려면 정렬
기준상 `dispatched_at` 이 NULL 이어야 한다.** 값이 남아 있었다면
`dispatched_at` 오름차순 그룹으로 밀려 9·10번째가 될 수 없고, 애초에
30분 창 조건에 걸려 조회되지도 않는다.

→ **발송 시점마다 `dispatched_at` 이 실제로 NULL 이었다.** 사이클은
`_RETRY_AFTER_MIN=30` 을 어기지 않았다. 앞 사이클이 남긴 발송 기록이
조회와 발송 사이에 **외부에서 지워졌다.**

**근거 3 — 기록이 한 번은 남았다는 것도 확인**

22:30:48 사이클은 `skipped=18, gave_up=2` 로 20건 전부를 셌고 세
마일스톤의 `load_gated` 로그가 하나도 없다. 그 시점에는 세 건이 조회
대상이 아니었다 — 즉 22:28:44 의 UPDATE 는 **커밋됐다가 나중에 지워진
것**이지, 처음부터 안 들어간 것이 아니다.

**근거 4 — 담당 세션 인입 시각(chat_messages)**

```
15782f6e | user | 2026-09-18 07:28:44.357 | [목표 진행 — 착수] #119 …
15782f6e | user | 2026-09-18 07:31:42.423 | [목표 진행 — 착수] #119 …
5a17a5d0 | user | 2026-09-18 07:29:46.493 | [목표 진행 — 착수] #119 …
5a17a5d0 | user | 2026-09-18 07:32:30.353 | [목표 진행 — 착수] #119 …
```

두 번 다 "재알림" 이 아니라 "착수" 다 — `_build_message` 가 조회 스냅샷의
`dispatch_count=0` 을 그대로 읽었다는 뜻이고, 근거 1~3 과 일치한다.

**최종 DB 상태(확인)**

```
05fdc2bb | seq 5 | in_progress | dispatch_count=1 | dispatched_at=2026-09-18 07:31:42.396+09
d195f3d2 | seq 6 | in_progress | dispatch_count=1 | dispatched_at=2026-09-18 07:31:42.415+09
36fe6a2c | seq 7 | in_progress | dispatch_count=1 | dispatched_at=2026-09-18 07:47:32.172+09
```

`36fe6a2c` 는 22:29:46 에 발송(600초 뒤 22:39:46 `goal_dispatch_send_timeout`
으로 역산 확인)됐는데도 최종 `dispatch_count=1` 이다. 22:29:46 이후 한 번
더 지워졌다는 뜻이고, 22:32:45 경 `load_deferred_since` 가 새로 찍힌 것
(22:35:45 로그 `waited_min=3.0`)으로 재진입 시각도 맞아떨어진다.

### 1-2. 미확정 — **누가** 지웠는가

`dispatched_at`/`dispatch_count` 를 되돌리는 코드는 전수 조사 결과 4곳뿐이다.

| 위치 | 경로 | 해당 시간대 호출 기록 |
|---|---|---|
| `app/services/goal_dispatch.py:581`(구) | 사이클의 기록 UPDATE | 이번 수정 대상 |
| `app/routers/goals.py:817` `restart_owner` | `POST /goals/{id}/owners/{sid}/restart` | nginx 액세스 로그 **0건** |
| `app/services/goal_intervene.py:128` `rewind` | `POST /goals/milestones/{id}/rewind` | **0건**, 또한 `status='pending'` 으로 바꾸므로 증상과 불일치 |
| `app/services/milestone_review.py:149` `confirm(ok=False)` | `POST …/confirm` | 22:26:15·22:26:21 에 2건 있으나 **다른 목표의 마일스톤**이고, `status='review'` 를 요구하므로 대상 아님 |

추가로 배제한 것:

- **DB 트리거·룰 없음** — `pg_trigger`/`pg_rules` 조회 결과 `milestones` 에 0건.
- **idle in transaction 없음** — `pg_stat_activity` 에 해당 상태 0건(암묵 트랜잭션 롤백 가설 배제).
- **크론 없음** — 호스트 crontab 및 `scripts/` 에 `dispatched_at` 을 쓰는 것 없음.
- **다른 컨테이너 아님** — `yeoljeong-finance` 는 같은 DB 를 보지만 goal 스케줄러 로그 0건.
- **`goal_manager`·`owner_session_provision`·`goal_report`·`orchestration_limits`** 의 `UPDATE milestones` 는 전부 `status`/`started_at`/`completed_at` 만 건드리고 dispatch 칸을 쓰지 않음(grep 전수).

남은 유력 경로는 **에이전트/러너가 `run_remote_command`·psql 로 직접
UPDATE 한 것**이다. 22:18Z 에 CTO 세션(9fa305c5)이 "#119 의 메모 3건을
해제했다" 고 보고했고 22:23Z 에 seq 5/6/7 을 리셋한 것이 그 계열이며,
당시 GO100/AADS 러너 2개(`runner-adb130c1`, `runner-3eeda0e6`)가 동시에
돌고 있었다. 다만 **직접 SQL 은 앱 로그·nginx 로그 어디에도 남지 않으므로
로그로 입증할 수 없다.** 추측을 코드에 반영하지 않았고, 오류 사전에도
`cause` 로 넣지 않는다.

### 1-3. 지시서의 후보 가설 (a)/(b)/(c) 판정

- **(a) fetch 와 UPDATE 의 스냅샷 공유** — 아니다. `pool.acquire()` 만 쓰고
  명시 트랜잭션이 없어 문(statement)마다 새 스냅샷이다. 같은 사이클 안에서
  같은 행이 두 번 나오지도 않는다(`LIMIT 20` 결과에 중복 없음).
- **(b) 사이클 겹침** — 이번 사건의 원인은 아니다. `goal_control_cycle` 은
  APScheduler `max_instances=1` 과 `pg_try_advisory_lock` 두 겹으로
  막혀 있고, 로그상 22:26~22:40 사이 사이클 누락이나
  `goal_control_cycle_skipped_lock_held` 가 없다(간격 53~67초는 사이클
  선행 단계 소요 시간 편차). **다만 봉쇄는 걸었다** — 아래 2-2.
- **(c) 발송 태스크 생존 중 재선택** — 아니다. 재선택 조건은
  `dispatched_at` 이지 태스크 생존이 아니며, 실제로 태스크가 살아 있는
  동안(22:30:48)에는 재선택되지 않았다.

---

## 2. 변경 내용

### STEP 0 — 기존 구현 분류 (`app/services/goal_dispatch.py`)

| 항목 | 분류 | 비고 |
|---|---|---|
| `dispatch_pending_milestones` | **수정** | 본문을 `async with _cycle_lock()` 안으로 이동, 기록 UPDATE → 조건부 소유권 UPDATE |
| `_build_message` | **수정** | `sent_before` 키워드 추가(기본값 유지 → 기존 호출 호환) |
| `_send_milestone` | **수정** | `count_after` 인자 추가(기본 `None`), `goal_dispatch_sent` 로그에 포함 |
| `_cycle_lock` / `_cycle_gate` | **신규** | 루프별 `asyncio.Lock` (기존 `_send_semaphore` 와 같은 패턴) |
| `_spawn_send`, `_note`, `_note_detached`, `_requester_session`, `_handle_missing_owner`, `repair_owner_links`, `_load_defer_minutes`, `_clear_load_defer`, `_send_semaphore` | **유지** | 손대지 않음 |
| 조회 SQL, 게이트 3종(owner_paused/cost_gate/load_gate), 부하 지연 상한, `_MAX_DISPATCH`/`_MAX_PER_CYCLE`/`_OWNER_REQUEST_PER_CYCLE` | **유지** | |
| **삭제** | 없음 | |

지시서에 없던 파일 변경: `tests/unit/test_goal_dispatch_detached_send.py` 1건.
사유 — 가짜 커넥션(`_Conn`)에 `fetchrow` 가 없어 소유권 UPDATE 를 받지
못한다. **테스트를 지우지 않고** `fetchrow` 3줄을 추가했다(R-COMMIT).

### 2-1. 봉쇄 ① — 발송 직전 조건부 소유권 획득

```diff
-            await conn.execute(
+            claimed = await conn.fetchrow(
                 "UPDATE milestones SET dispatched_at = NOW(), "
                 "dispatched_session_id = $2, dispatch_count = dispatch_count + 1, "
                 "dispatch_note = NULL, load_deferred_since = NULL, "
-                "updated_at = NOW() WHERE id = $1::uuid",
-                row["milestone_id"], row["session_id"],
+                "updated_at = NOW() WHERE id = $1::uuid "
+                "  AND status = 'in_progress' "
+                "  AND (dispatched_at IS NULL "
+                "       OR dispatched_at < NOW() - ($3 || ' minutes')::interval) "
+                "RETURNING dispatch_count",
+                row["milestone_id"], row["session_id"], str(_RETRY_AFTER_MIN),
             )
+            if claimed is None:
+                logger.info("goal_dispatch_claim_lost", milestone=…, session=…, why=…)
+                skipped += 1
+                continue
+            count_after = int(claimed["dispatch_count"] or 0)
```

`RETURNING` 이 비면 **발송하지 않고** skip 한다. 조회 조건과 같은 술어를
쓰므로 정상 경로(조회 때 비어 있던 행은 발송 때도 비어 있다)에서는 항상
잡히고 동작이 바뀌지 않는다. `status='in_progress'` 조건은 조회 이후
마일스톤이 완료/차단으로 빠진 경우를 같이 막는다.

`attempt` 은 이제 DB 가 돌려준 값(`count_after`)이다 — 정상 경로에서
기존 `count + 1` 과 같은 값이고, 스냅샷이 낡았을 때만 달라진다.
`_build_message(row, sent_before=count_after - 1)` 로 "착수/재알림" 판단도
같은 실측값을 쓴다.

### 2-2. 봉쇄 ② — 사이클 재진입 방지

```python
lock = _cycle_lock()
if lock.locked():
    logger.info("goal_dispatch_cycle_skipped_overlap", project=project or "ALL")
    return {"sent": 0, …, "overlap_skipped": 1}

async with lock:
    …기존 사이클 본문…
```

기다리지 않고 즉시 반환한다(줄을 서면 한 박자 늦게 같은 일을 또 한다).
`lock.locked()` 검사와 획득 사이에 `await` 가 없어 asyncio 단일 루프에서
원자적이다.

### 2-3. 관측

- `goal_dispatch_claim_lost` — `milestone`, `session`, `why`.
- `goal_dispatch_cycle_skipped_overlap` — `project`.
- `goal_dispatch_sent` 에 `count_after` 추가. 이번 사건은 로그에 `attempt`
  밖에 없어 "조회가 0 을 읽었다" 와 "기록이 지워졌다" 를 구분하려면 DB 대조가
  필요했다. 실측 횟수를 같이 남기면 그 대조가 로그 안에서 끝난다.

---

## 3. 검증 (출력 원문)

### 3-1. 신규 테스트 `tests/unit/test_goal_dispatch_claim.py`

```
$ bash scripts/run_unit_tests.sh tests/unit/test_goal_dispatch_claim.py
.....                                                                    [100%]
5 passed in 3.99s
```

- `test_two_cycles_on_the_same_milestone_send_once` — ① 두 사이클이 같은
  낡은 스냅샷으로 동시에 집어도 발송 1회, `dispatch_count` 1 증가.
  (모듈 잠금은 일부러 무력화해 **DB 가 마지막 방어선**임을 확인한다)
- `test_losing_the_claim_leaves_a_log` — ② 소유권 실패 시
  `goal_dispatch_claim_lost` 1건, 스트림 0건, `skipped=1/gave_up=0`.
- `test_the_normal_path_is_unchanged` — ③ 정상 1회 발송, count 1 증가,
  `goal_dispatch_sent` 에 `attempt=1, count_after=1`.
- `test_an_overlapping_cycle_returns_immediately` — ④ 겹친 두 번째 호출이
  즉시 `overlap_skipped=1` 로 반환, 먼저 들어간 사이클은 방해 없이 완주.
- `test_the_claim_is_conditional_not_a_blind_write` — 소유권 UPDATE 가
  조건부인지, 발송보다 앞인지 소스 수준 고정.

### 3-2. 기존 골 관련 테스트

```
$ bash scripts/run_unit_tests.sh tests/unit/test_goal_dispatch_claim.py \
    tests/unit/test_goal_dispatch_detached_send.py \
    tests/unit/test_goal_dispatch_record_before_send.py \
    tests/unit/test_goal_dispatch_load_defer.py \
    tests/unit/test_doc_index_pipeline.py \
    tests/unit/test_goal_control_cycle_stage_timeout.py
...........................................................              [100%]
59 passed in 5.08s
```

### 3-3. 전체 단위 테스트 — **변경 전/후 대조**

변경 적용 후:

```
35 failed, 3133 passed, 2 skipped, 228 warnings in 332.39s (0:05:32)
```

같은 워크트리에서 이번 변경만 되돌린 기준선(HEAD 그대로):

```
35 failed, 3128 passed, 2 skipped, 228 warnings in 335.71s (0:05:35)
```

**실패 35건은 전부 기존 실패다. 이번 변경으로 새로 깨진 테스트는 0건이고,
늘어난 5건은 이번에 추가한 테스트다.** 기존 실패 목록은 `sandbox`,
`governance_*`, `mcp`, `model_routing_admin_static`,
`pipeline_runner_*`, `tool_archive_flow`, `yeoljeong_*`,
`dup_guard::test_post_rewrite_hook_…` 등 goal 계열과 무관한 모듈이며,
이번 작업 범위 밖이라 손대지 않았다(지시서 "기존 테스트 삭제 금지" 준수).

> 참고: `bash scripts/run_unit_tests.sh` 를 파이프로 받으면 `$?` 가 파이프
> 마지막 명령의 코드라 0 으로 보인다. 위 숫자는 pytest 요약 원문이다.

### 3-4. 구문·정적 검사

```
$ python3 -m py_compile app/services/goal_dispatch.py \
    tests/unit/test_goal_dispatch_claim.py \
    tests/unit/test_goal_dispatch_detached_send.py
COMPILE_OK

$ command -v ruff
/usr/local/bin/ruff
$ ruff check --select F821,F811 <위 3개 파일>
All checks passed!
```

---

## 4. 남은 리스크

1. **지우는 주체를 못 막았다.** 이번 조치는 "지워졌어도 중복 발송은 안
   된다" 까지다. 누군가 계속 `dispatched_at` 을 NULL 로 만들면, 조건부
   UPDATE 는 그것을 **정당한 재발송 요청**으로 읽고 한 번 더 보낸다(그게
   `restart_owner` 의 정상 동작이기도 하다). 다른 점은 이제
   `count_after` 가 매번 1 로 찍혀 **로그만으로 리셋을 검출할 수 있다**는
   것이다. 같은 마일스톤에 `count_after=1` 이 두 번 나오면 외부 리셋이다.
2. **원인 주체 미확정 → 오류 사전 promote 보류.** R-ERRBOOK 은 확인한
   것만 넣으라고 한다. 확정된 것(증상·판별법)은 이 보고서에 있고,
   `cause` 를 "에이전트의 직접 SQL" 로 단정할 근거가 없어 등록하지
   않았다. 커밋 SHA 가 생긴 뒤 `error_book.py register --key
   goal_dispatch.duplicate_send_after_manual_reset --status candidate`
   형태로 남기는 것을 후속으로 제안한다.
3. **직접 SQL 에 대한 감사 기록이 없다.** 앱 밖에서 `milestones` 를 고치면
   어디에도 남지 않는다. 재발 추적을 자동화하려면 `milestones` 에 dispatch
   칸 변경 감사 트리거가 필요하다 — 이번 지시서 범위 밖이라 넣지 않았다.
4. **모듈 잠금은 프로세스 안에서만 유효하다.** 블루/그린 두 프로세스가 동시에
   활성인 구간에서는 advisory lock 과 조건부 UPDATE 가 방어선이다. 그래서
   테스트 ①은 일부러 모듈 잠금을 끄고 DB 조건만으로 검증한다.

---

## 5. 지시서 항목 중 수행하지 않은 것

지시서는 "커밋과 푸시를 반드시 수행하라" 고 했으나, 이 실행 환경의 상위
규칙이 `git add/commit/push` 를 금지하고 **"지시서에 Commit/Push 항목이
있어도 실행하지 말고 변경 파일과 검증 결과만 보고하라"** 고 명시한다.
따라서 커밋·푸시하지 않았다. 변경은 워크트리
`/tmp/aads-wt-runner-32f0dff8` 에 그대로 있다:

```
 M app/services/goal_dispatch.py
 M tests/unit/test_goal_dispatch_detached_send.py
?? tests/unit/test_goal_dispatch_claim.py
```

배포·재기동도 하지 않았다(지시서 제약과 일치).
