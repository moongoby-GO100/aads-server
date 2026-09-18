# AADS-GOALDISPATCH-DUPLICATE-CLAIM — 골 사이클 중복 발송 봉쇄 · 기록 변경 추적

- 작성: 2026-09-18 (러너 워크트리 `/tmp/aads-wt-runner-553d38c2`, base `94152aba`)
- 대상 저장소: `aads-server` 단독
- 변경 파일 4 + 신규 테스트 1

| 파일 | 내용 |
|---|---|
| `app/services/goal_dispatch.py` | claim 실패 로그 WARNING 승격 + 이번 사이클이 읽은 값 동봉, `$3`↔조회 `$2` 대응 주석, `note_record_reset()` 신규 |
| `app/routers/goals.py` | `restart_owner` — 지운 건 **전부**를 옛 값과 함께 받아 건별 기록 로그 |
| `app/services/goal_intervene.py` | `rewind` — 옛 값과 함께 되돌리고 기록 로그 |
| `app/services/milestone_review.py` | 반려 경로 — 옛 값을 읽어 두고 기록 로그 |
| `tests/unit/test_goal_dispatch_record_reset.py` | **신규** 8건 |

---

## 0. STEP 0 — 기존 구현 조사와 분류

**먼저 확인한 사실: 지시서 1번(조건부 claim UPDATE)은 이미 들어와 있다.**
`87f798de fix(goal_dispatch): 중복 발송 봉쇄 — 조건부 소유권 UPDATE + 사이클
재진입 방지` (러너 `runner-32f0dff8`, 보고서
`reports/20260918_AADS-GOALDISPATCH-DUPLICATE-SEND.md`)가 HEAD 조상에 있고,
`tests/unit/test_goal_dispatch_claim.py` 5건도 같이 들어왔다.

지시서는 "먼저 들어간 작업 위에 얹어라 — 충돌 시 그쪽 변경을 지우지 마라"
이므로 **기존 구현을 재작성하지 않고 차이분만 반영했다.** 아래 표에서
"이미 구현" 으로 적은 것은 이번에 손대지 않은 것이다.

### `app/services/goal_dispatch.py`

| 항목 | 분류 | 비고 |
|---|---|---|
| 조건부 claim UPDATE(`RETURNING dispatch_count`) | 이미 구현(유지) | 87f798de |
| claim 실패 시 `_spawn_send` 생략 + `skipped+=1` + `continue` | 이미 구현(유지) | 87f798de |
| `attempt` = RETURNING 값(`count_after`) | 이미 구현(유지) | 87f798de |
| `_cycle_lock` 사이클 재진입 방지 | 이미 구현(유지) | 87f798de |
| `goal_dispatch_claim_lost` 로그 레벨 | **수정** | INFO → **WARNING**, `read_count`·`read_dispatched_at` 추가(지시서 1번 요구) |
| `$3` ↔ 조회 `$2` 대응 주석 | **신규** | 지시서 1번 요구("주석으로 한 줄") |
| `note_record_reset()` | **신규** | 지시서 2번 — 기록 되돌림 추적 로깅 |
| 모듈 docstring | **수정** | 7번 겹(기록 되돌림 자취) 한 문단 추가 |
| 조회 SQL, 게이트 3종(cost/load/answered/owner_paused), `_MAX_*`, `_build_message`, `_send_milestone`, `_spawn_send`, `_note*`, `repair_owner_links`, `_load_defer_*` | **유지** | 판정 기준 불변(지시서 금지 조항) |
| 삭제 | **없음** | |

### `app/routers/goals.py`

| 항목 | 분류 | 비고 |
|---|---|---|
| `restart_owner` UPDATE 조건식(goal/status/owner 매칭) | **유지** | 한 글자도 바꾸지 않았다 — 지우는 범위는 그대로다 |
| `fetchval(... RETURNING 1)` | **수정** | `fetch(WITH target … )` — 지운 **전 건**과 옛 값을 받는다. 기존 코드는 여러 건을 지워도 한 건만 돌려받아 로그에 남길 대상을 알 수 없었다 |
| 응답 `{"restarted": bool, "resumed": True}` | **유지** | 스키마 불변(`bool(n)` → `bool(reset)`) |
| 그 외 엔드포인트 전부 | **유지** | |

### `app/services/goal_intervene.py`

| 항목 | 분류 | 비고 |
|---|---|---|
| `rewind` UPDATE 의 `SET`/`WHERE` | **유지** | 되돌리는 칸·조건 불변 |
| `RETURNING` | **수정** | `WITH prev` CTE 를 붙여 **지우기 전** `dispatched_at`·`dispatch_count` 동반 반환 |
| `goal_report_log` 삭제, 반환값 | **유지** | |
| `halt`, `direct` | **유지** | |

### `app/services/milestone_review.py`

| 항목 | 분류 | 비고 |
|---|---|---|
| 선행 `SELECT status,title,goal_id` | **수정** | `dispatched_at, dispatch_count` 두 칸 추가(읽기만) |
| 반려 UPDATE | **유지** | SQL 그대로, 뒤에 로그 한 번 추가 |
| 승인/음성 경로, `_lead_session` | **유지** | |

### 지시서에 없던 파일 변경

없다. 변경한 4개 파일은 전부 지시서 2번이 "확인하라" 고 지목한 파일이다.

### 삭제

없다. 기존 테스트 삭제도 없다.

---

## 1. 지시서 1번 — 중복 발송 봉쇄

구조는 87f798de 에 들어와 있고, 이번에 지시서가 요구한 두 가지를 채웠다.

### 1-1. claim 실패는 WARNING 이다

INFO 였다. 소유권을 놓쳤다는 것은 **이 사이클과 겹쳐 도는 다른 주체가
실제로 있었다**는 뜻이고, 그것이 2026-09-17 중복 발송의 성립 조건 자체다.
INFO 로 두면 "봉쇄가 몇 번 걸렸는가" 를 사후에 셀 수 없다.

같이 남기는 값도 지시서대로 늘렸다 — `read_count`(이번 사이클이 조회에서
읽은 `dispatch_count`)와 `read_dispatched_at`. 이번 사건에서 "조회가 0 을
읽었다" 와 "발송 직전엔 값이 있었다" 를 구분하려고 DB 를 직접 뒤져야 했다.
이제 그 대조가 로그 한 줄 안에서 끝난다.

### 1-2. `$3` 과 조회 `$2` 의 대응을 주석으로 못 박았다

조회(`... OR m.dispatched_at < NOW() - ($2 || ' minutes')::interval`)와
claim(`... OR dispatched_at < NOW() - ($3 || ' minutes')::interval`)은 같은
`_RETRY_AFTER_MIN` 을 받는다. 어긋나면 claim 이 **영원히 실패**하거나
(claim 이 더 엄하면 정상 건까지 전부 `claim_lost` 로 빠져 오케스트레이션이
통째로 멎는다) **영원히 통과**한다(넓으면 봉쇄가 아무것도 막지 않는다).
둘 다 조용히 망가지므로 주석만으로 두지 않고 정적 테스트로 고정했다
(3-1 의 `test_the_select_and_the_claim_share_one_retry_window`).

---

## 2. 지시서 2번 — 기록이 사라지는 경로 전수 조사

### 2-1. 되돌리는 경로 (전부 로깅 추가)

`dispatched_at` 을 NULL 로 만들거나 `dispatch_count` 를 0 으로 내리는
코드는 **저장소 전체에 3곳**이다.

| # | 위치 | 진입점 | 되돌리는 칸 | 로깅 |
|---|---|---|---|---|
| 1 | `app/routers/goals.py:restart_owner` | `POST /api/v1/goals/{goal_id}/owners/{session_id}/restart` | `dispatched_at=NULL`, `dispatch_count=0`, `dispatch_note=NULL` (해당 목표의 `in_progress` **전 건**) | 추가 — 건별 1줄 |
| 2 | `app/services/goal_intervene.py:rewind` | `POST /api/v1/goals/milestones/{id}/rewind` · 주도 도구 `rewind` | `dispatched_at=NULL`, `dispatched_session_id=NULL`, `dispatch_count=0` (+`status='pending'`) | 추가 |
| 3 | `app/services/milestone_review.py:confirm(ok=False)` | 반려(주도 도구 · `POST …/confirm` · 대표님 패널) | `dispatched_at=NULL`, `dispatch_count=0` | 추가 |

공통 형식 — `goal_dispatch_record_reset` (INFO):

    milestone=05fdc2bb  reason="검증 반려(confirmer=lead) — 근거가 없다"
    where="app/services/milestone_review.py:confirm"  called_from="milestone_review.py:161"
    prev_dispatched_at="2026-09-18T07:28:44+09:00"  prev_dispatch_count=1

`where` 는 손으로 적는 이름, `called_from` 은 `sys._getframe(1)` 로 뽑은
실제 프레임이다. 둘 다 남기는 이유는 새 경로가 `where` 를 복사해 붙이고
고치지 않는 일이 흔해서다 — 그때 프레임이 정본이 된다.

**`prev_*` 가 핵심이다.** "몇 시의 발송 기록이 지워졌는가" 가 사이클
로그와 맞춰 볼 수 있는 유일한 열쇠다. 그래서 1·2번은 UPDATE 를 CTE 로
감싸 **지우기 전 스냅샷**을 같이 돌려받게 했고(아래 diff), 3번은 이미
앞단에 있던 `SELECT` 에 두 칸을 더했다.

### 2-2. 같이 확인했지만 되돌림이 **아닌** 것 (조사 근거)

    $ grep -rn "dispatched_at *= *NULL\|dispatch_count = 0" --include="*.py" app/ scripts/
    app/services/milestone_review.py:154:                "dispatched_at = NULL, dispatch_count = 0, "
    app/routers/goals.py:831:                   SET dispatched_at = NULL, dispatch_count = 0,
    app/services/goal_intervene.py:135:               dispatched_at = NULL, dispatched_session_id = NULL,
    app/services/goal_intervene.py:136:               dispatch_count = 0,

    $ grep -rln "UPDATE milestones" --include="*.py" app/ scripts/
    app/services/temporal_controller.py   app/services/milestone_review.py
    app/services/owner_session_provision.py  app/services/goal_intervene.py
    app/services/goal_dispatch.py   app/routers/goals.py   app/services/goal_manager.py

    $ grep -rn "dispatched_at\|dispatch_count" --include="*.sh" --include="*.ts" \
          --include="*.tsx" --include="*.sql" .   # migrations/ 제외
    (0건)

| 위치 | 무엇을 쓰는가 | 판정 |
|---|---|---|
| `app/services/owner_session_provision.py:322,387` | `owner_session_id`, `dispatch_note` 만 | 되돌림 아님. 지시서가 확인하라고 지목한 파일 — **dispatch 칸을 건드리지 않는다** |
| `app/services/goal_manager.py` (288·528·605·640·664·732·875·960) | `status`, `started_at`, `completed_at` 만 | 되돌림 아님. 다만 `status` 를 `in_progress` 밖으로 옮기면 claim 조건(`status='in_progress'`)이 막는다 — 봉쇄는 그쪽도 덮는다 |
| `app/services/temporal_controller.py:71,105,112` | `status`, `started_at`, `completed_at` 만 | 되돌림 아님 |
| `app/services/goal_dispatch.py:186,658` (`_note`) | `dispatch_note` 만 | 되돌림 아님(의도적 — 미룬 것은 횟수를 깎지 않는다) |
| `app/services/goal_dispatch.py:390,404` (`_load_defer_*`) | `load_deferred_since` 만 | 되돌림 아님 |
| `app/services/goal_report.py:116`, `app/services/orchestration_limits.py:75`, `app/routers/goals.py:224,303` | `SELECT` 만 | 읽기 |
| `migrations/20260914_*.sql` | 칸 생성·DEFAULT 0 | DDL |
| `scripts/`, `*.sh`, `*.ts/tsx`, 그 외 `*.sql` | 0건 | |

### 2-3. **찾지 못한 것 — 추측으로 채우지 않는다**

2026-09-17 사건에서 기록을 실제로 지운 주체는 **여전히 미확정이다.**

- 코드 경로 3곳은 전부 전수 조사했고 이제 전부 로그를 남긴다. 그러나
  당시 로그에 남은 것이 없으므로 **이 3곳 중 무엇이었다고 말할 근거가 없다.**
  (선행 보고서 실측: `restart` nginx 액세스 0건, `rewind` 0건,
  `confirm` 은 다른 목표 건이며 `status='review'` 를 요구해 대상 아님)
- 에이전트·러너가 `run_remote_command`·`psql` 로 **직접 UPDATE** 한 경우는
  앱 코드를 거치지 않으므로 **이번 로깅으로도 잡히지 않는다.** 코드에
  그런 호출 지점이 없어 계측할 자리가 없다 — 찾지 못했다.
- DB 트리거·룰 존재 여부는 이 워크트리(코드)만으로는 확인할 수 없다.
  선행 보고서가 `pg_trigger`/`pg_rules` 0건으로 실측해 배제했고, 이번에
  다시 조회하지 않았다. **내가 실측한 것이 아니므로 근거는 그 보고서다.**

즉 이번 변경이 보장하는 것은 **"다음번에는 앱 경로라면 반드시 남는다"**
까지다. 남지 않는데 기록이 사라지면 그때는 앱 밖(직접 SQL)이라는 결론이
로그만으로 선다 — 그것이 이번 로깅의 실질적 소득이다.

---

## 3. 변경 diff (전문)

```diff
diff --git a/app/routers/goals.py b/app/routers/goals.py
index e9b95cd8..9b3e95ce 100644
--- a/app/routers/goals.py
+++ b/app/routers/goals.py
@@ -810,16 +810,34 @@ async def restart_owner(goal_id: str, session_id: str):
     """
     from app.core.db_pool import get_pool
 
+    from app.services.goal_dispatch import note_record_reset
+
     pool = get_pool()
     async with pool.acquire() as conn:
-        n = await conn.fetchval(
+        # `fetchval ... RETURNING 1` 은 여러 건을 지워도 한 건만 돌려줬다.
+        # UPDATE 범위는 그대로 두고(조건식 동일) **지운 건 전부**를 받는다 —
+        # 되돌린 마일스톤 하나하나가 로그에 남아야 추적이 된다.
+        # CTE `target` 은 UPDATE 전 스냅샷이므로 옛 값이 나온다.
+        reset = await conn.fetch(
             """
-            UPDATE milestones SET dispatched_at = NULL, dispatch_count = 0,
-                   dispatch_note = NULL, updated_at = NOW()
-            WHERE goal_id = $1::uuid AND status = 'in_progress'
-              AND (owner_session_id = $2::uuid
-                   OR owner_role_key = (SELECT role_key FROM chat_sessions WHERE id = $2::uuid))
-            RETURNING 1
+            WITH target AS (
+                SELECT id, dispatched_at, dispatch_count
+                  FROM milestones
+                 WHERE goal_id = $1::uuid AND status = 'in_progress'
+                   AND (owner_session_id = $2::uuid
+                        OR owner_role_key = (SELECT role_key FROM chat_sessions WHERE id = $2::uuid))
+            ), done AS (
+                UPDATE milestones m
+                   SET dispatched_at = NULL, dispatch_count = 0,
+                       dispatch_note = NULL, updated_at = NOW()
+                  FROM target t
+                 WHERE m.id = t.id
+                RETURNING m.id
+            )
+            SELECT t.id::text AS milestone_id,
+                   t.dispatched_at AS prev_dispatched_at,
+                   t.dispatch_count AS prev_dispatch_count
+              FROM target t WHERE t.id IN (SELECT id FROM done)
             """,
             goal_id, session_id,
         )
@@ -827,7 +845,16 @@ async def restart_owner(goal_id: str, session_id: str):
             "DELETE FROM owner_pause WHERE goal_id = $1::uuid AND session_id = $2::uuid",
             goal_id, session_id,
         )
-    return {"restarted": bool(n), "resumed": True}
+
+    for r in reset:
+        note_record_reset(
+            r["milestone_id"],
+            reason=f"담당 재시작 API — goal={goal_id[:8]} session={session_id[:8]}",
+            where="app/routers/goals.py:restart_owner",
+            prev_dispatched_at=r["prev_dispatched_at"],
+            prev_dispatch_count=r["prev_dispatch_count"],
+        )
+    return {"restarted": bool(reset), "resumed": True}
 
 
 @router.post("/goals/halt")
diff --git a/app/services/goal_dispatch.py b/app/services/goal_dispatch.py
index e25c30c3..41d4a531 100644
--- a/app/services/goal_dispatch.py
+++ b/app/services/goal_dispatch.py
@@ -49,11 +49,16 @@
    누가 가져간 것이므로 **보내지 않고** `goal_dispatch_claim_lost` 를 남긴다.
 6. **사이클이 겹치지 않게 한다.** 도는 사이클이 있으면 즉시 돌아가고
    `goal_dispatch_cycle_skipped_overlap` 를 남긴다.
+7. **기록을 지우는 쪽도 자취를 남긴다.** 5번은 증상을 막을 뿐 누가 지웠는지
+   알려주지 않는다. 발송 기록을 되돌리는 모든 경로가 `note_record_reset` 을
+   불러 `goal_dispatch_record_reset` 를 남긴다 — 지우기 전 값과 호출 위치를
+   같이 적는다.
 """
 from __future__ import annotations
 
 import asyncio
 import os
+import sys
 from typing import Any
 
 import structlog
@@ -184,6 +189,50 @@ async def _note(conn, milestone_id: str, note: str) -> None:
     )
 
 
+def note_record_reset(
+    milestone_id: str,
+    *,
+    reason: str,
+    where: str,
+    prev_dispatched_at: Any = None,
+    prev_dispatch_count: Any = None,
+) -> None:
+    """발송 기록을 **되돌리는** 쪽이 남기는 자취.
+
+    `dispatched_at` 을 NULL 로 만들거나 `dispatch_count` 를 0 으로 내리는
+    것은 "이 마일스톤에 다시 지시를 보내라" 는 뜻이다. 그 자체는 정상
+    기능이지만, 기록이 사라졌을 때 **누가 지웠는지 로그에 아무것도 남지
+    않는다** 는 것이 2026-09-17 중복 발송에서 원인 특정을 막았다. 조건부
+    소유권 UPDATE 가 중복 발송은 이미 막지만(위 5번), 그것은 증상을 막는
+    것이지 지우는 주체를 알려주지 않는다.
+
+    그래서 되돌리는 세 경로(`restart_owner`, `rewind`, 반려)가 전부 이
+    함수를 부른다. 호출 위치는 인자로 받은 이름(`where`)과 **실제 프레임**
+    양쪽을 남긴다 — 새 경로가 생기면서 `where` 를 복사해 붙이고 고치지
+    않는 일이 흔하고, 그때 프레임이 정본이 된다.
+
+    지우기 **전** 값을 같이 남기는 것이 핵심이다. "몇 시의 발송 기록이
+    지워졌는가" 가 사이클 로그와 맞춰 볼 수 있는 유일한 열쇠다.
+    """
+    caller = sys._getframe(1)
+    prev_at = prev_dispatched_at
+    if hasattr(prev_at, "isoformat"):
+        prev_at = prev_at.isoformat()
+    logger.info(
+        "goal_dispatch_record_reset",
+        milestone=str(milestone_id)[:8],
+        reason=(reason or "")[:160],
+        where=where,
+        called_from="%s:%d" % (
+            os.path.basename(caller.f_code.co_filename), caller.f_lineno,
+        ),
+        prev_dispatched_at=str(prev_at or ""),
+        prev_dispatch_count=(
+            int(prev_dispatch_count) if prev_dispatch_count is not None else None
+        ),
+    )
+
+
 async def _requester_session(conn, row: Any) -> str:
     """승인 요청을 올릴 세션. **주도가 정본이다.**
 
@@ -670,6 +719,11 @@ async def dispatch_pending_milestones(project: str | None = None) -> dict[str, i
                 # 덮어쓰고 보낸다. 조건부 UPDATE 는 알아챈다. 재시도 창 안에
                 # 이미 기록이 있으면 `RETURNING` 이 비고, 그러면 **보내지
                 # 않는다.** 경합에서 둘 다 보내는 대신 한 쪽만 보낸다.
+                # `$3` 은 위 조회 SQL 의 `$2` 와 **같은 `_RETRY_AFTER_MIN`** 이다.
+                # 두 조건식이 어긋나면 소유권 획득이 영원히 실패하거나(조회보다
+                # 좁을 때) 영원히 통과한다(넓을 때) — 어느 쪽이든 조용히
+                # 망가진다. 이 대응은 테스트가 정적으로 고정한다
+                # (`tests/unit/test_goal_dispatch_record_reset.py`).
                 claimed = await conn.fetchrow(
                     "UPDATE milestones SET dispatched_at = NOW(), "
                     "dispatched_session_id = $2, dispatch_count = dispatch_count + 1, "
@@ -684,10 +738,23 @@ async def dispatch_pending_milestones(project: str | None = None) -> dict[str, i
                 if claimed is None:
                     # 다른 사이클이 이미 가져갔거나, 그 사이 마일스톤이
                     # 진행중에서 빠졌다. 어느 쪽이든 여기서 보내면 중복이다.
-                    logger.info(
+                    #
+                    # **WARNING 이다.** 소유권을 놓쳤다는 것은 이 사이클과
+                    # 겹쳐 도는 다른 주체가 실제로 있었다는 뜻이고, 그것이
+                    # 바로 2026-09-17 중복 발송의 조건이다. INFO 로 남기면
+                    # 초당 수백 줄 사이에 묻혀 "봉쇄가 몇 번 걸렸는가" 를
+                    # 사후에 셀 수 없다.
+                    #
+                    # 이번 사이클이 **조회에서 읽은** 값을 같이 남긴다.
+                    # 기록이 지워지는 경로를 좁히려면 "조회가 본 값" 과
+                    # "발송 직전 DB 값" 의 차이가 필요하다 — 이번 사건에서
+                    # 그 대조를 하려고 DB 를 직접 뒤져야 했다.
+                    logger.warning(
                         "goal_dispatch_claim_lost",
                         milestone=str(row["milestone_id"])[:8],
                         session=str(row["session_id"])[:8],
+                        read_count=count,
+                        read_dispatched_at=str(row["dispatched_at"] or ""),
                         why="이미 재시도 창 안에 발송 기록이 있거나 진행중이 아니다",
                     )
                     skipped += 1
diff --git a/app/services/goal_intervene.py b/app/services/goal_intervene.py
index 3cdc4315..540079ce 100644
--- a/app/services/goal_intervene.py
+++ b/app/services/goal_intervene.py
@@ -121,22 +121,44 @@ async def rewind(milestone_id: str, reason: str = "") -> dict[str, Any]:
     from app.core.db_pool import get_pool
 
     pool = get_pool()
+    # 지우기 **전** 값을 같이 돌려받는다. CTE `prev` 는 UPDATE 전 스냅샷을
+    # 들고 있으므로 `RETURNING` 에서 참조하면 옛 값이 나온다 — 발송 기록이
+    # 언제 것이었는지를 남겨야 사이클 로그와 맞춰 볼 수 있다.
     row = await pool.fetchrow(
         """
-        UPDATE milestones
+        WITH prev AS (
+            SELECT id, dispatched_at, dispatch_count
+              FROM milestones WHERE id = $1::uuid
+        )
+        UPDATE milestones m
            SET status = 'pending', started_at = NULL, completed_at = NULL,
                dispatched_at = NULL, dispatched_session_id = NULL,
                dispatch_count = 0,
                dispatch_note = NULLIF($2, ''),
                updated_at = NOW()
-         WHERE id = $1::uuid
-        RETURNING title, goal_id::text AS goal_id
+          FROM prev
+         WHERE m.id = prev.id
+        RETURNING m.title, m.goal_id::text AS goal_id,
+                  prev.dispatched_at AS prev_dispatched_at,
+                  prev.dispatch_count AS prev_dispatch_count
         """,
         milestone_id, reason,
     )
     if not row:
         return {"error": "milestone_not_found"}
 
+    # 발송 기록을 되돌렸다는 자취. 없으면 다음 사이클이 같은 마일스톤을
+    # 다시 집었을 때 "왜 또 나갔는가" 를 로그만으로 설명할 수 없다.
+    from app.services.goal_dispatch import note_record_reset
+
+    note_record_reset(
+        milestone_id,
+        reason=f"rewind(pending 으로 되돌림) — {reason}".strip(" —"),
+        where="app/services/goal_intervene.py:rewind",
+        prev_dispatched_at=row["prev_dispatched_at"],
+        prev_dispatch_count=row["prev_dispatch_count"],
+    )
+
     # 같은 사건을 다시 보고할 수 있게 기록도 지운다.
     await pool.execute(
         "DELETE FROM goal_report_log WHERE subject_id = $1::uuid", milestone_id
diff --git a/app/services/milestone_review.py b/app/services/milestone_review.py
index dea814d3..4eca91f3 100644
--- a/app/services/milestone_review.py
+++ b/app/services/milestone_review.py
@@ -114,8 +114,11 @@ async def confirm(
 
     pool = get_pool()
     async with pool.acquire() as conn:
+        # 발송 기록도 같이 읽는다 — 반려 경로가 이것을 지우므로,
+        # 지우기 전 값을 로그에 남기려면 여기서 받아 둬야 한다.
         row = await conn.fetchrow(
-            "SELECT status, title, goal_id::text AS goal_id FROM milestones WHERE id = $1::uuid",
+            "SELECT status, title, goal_id::text AS goal_id, "
+            "dispatched_at, dispatch_count FROM milestones WHERE id = $1::uuid",
             milestone_id,
         )
         if not row:
@@ -153,6 +156,18 @@ async def confirm(
                 "WHERE id = $1::uuid",
                 milestone_id, reason,
             )
+            # 기록을 지운 자취를 남긴다. 반려는 정상 기능이지만, 지운 것이
+            # 로그에 없으면 다음 사이클의 재발송이 "중복" 인지 "반려에 따른
+            # 재지시" 인지 구분되지 않는다.
+            from app.services.goal_dispatch import note_record_reset
+
+            note_record_reset(
+                milestone_id,
+                reason=f"검증 반려(confirmer={confirmer}) — {reason}".strip(" —"),
+                where="app/services/milestone_review.py:confirm",
+                prev_dispatched_at=row["dispatched_at"],
+                prev_dispatch_count=row["dispatch_count"],
+            )
     logger.info(
         "milestone_confirmed", milestone=milestone_id[:8],
         result=new, by=confirmer,
```

### 신규 `tests/unit/test_goal_dispatch_record_reset.py` (전문)

```python
"""소유권(claim)이 발송을 막고, 기록을 지우는 쪽은 자취를 남긴다.

2026-09-17 22:28~22:33Z 실측(GO100 #119). 마일스톤 05fdc2bb·d195f3d2 가
세 사이클 연속 `attempt=1` 로 나가 담당 세션 둘이 같은 착수 지시를 2회씩
받았다. 최종 DB 는 `dispatch_count=1` — **발송 2회, 카운트 1회**다.

두 가지가 같이 필요하다.

1. **봉쇄.** 조회와 발송 사이에 누가 기록을 지웠든, 발송 직전 조건부
   UPDATE 가 빈손으로 돌아오면 보내지 않는다. 원인을 몰라도 막힌다.
2. **추적.** 봉쇄는 증상을 막을 뿐 **누가 지웠는지**는 알려주지 않는다.
   기록을 되돌리는 경로가 전부 `goal_dispatch_record_reset` 을 남겨야
   다음번엔 로그만으로 지운 주체를 지목할 수 있다. 이번엔 그게 없어서
   "남은 유력 경로는 직접 SQL" 까지밖에 못 갔다.

`tests/unit/test_goal_dispatch_claim.py` 는 1번의 **경합 시나리오**를 본다.
여기서는 1번의 **계약**(빈손이면 안 보낸다 / attempt 은 DB 가 준 값이다 /
두 조건식이 같은 상수를 쓴다)과 2번 전체를 본다.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path

import pytest

from app.services import goal_dispatch

_APP = Path(__file__).resolve().parents[2] / "app"


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, event: str, **kw) -> None:
        self.events.append((event, kw))

    warning = info
    error = info

    def named(self, event: str) -> list[dict]:
        return [kw for name, kw in self.events if name == event]


def _row(n: int = 1, **over) -> dict:
    row = {
        "milestone_id": f"{n:08d}-0000-0000-0000-000000000000",
        "milestone_title": f"M{n}",
        "description": "설명",
        "completion_criteria": "기준",
        "dispatch_count": 0,
        "dispatched_at": None,
        "load_deferred_since": None,
        "goal_title": "목표",
        "project": "AADS",
        "goal_id": "g",
        "owner_role_key": f"role{n}",
        "goal_lead_session_id": "lead",
        "session_id": f"session-{n}",
    }
    row.update(over)
    return row


class _Conn:
    """조회는 고정 스냅샷, 소유권 UPDATE 만 지정한 값으로 답한다."""

    def __init__(self, rows: list[dict], claim: dict | None) -> None:
        self.rows = rows
        self.claim = claim
        self.claim_queries: list[str] = []

    async def execute(self, *_a, **_k) -> str:
        return "UPDATE 0"

    async def fetch(self, *_a, **_k) -> list[dict]:
        return self.rows

    async def fetchval(self, *_a, **_k):
        return False

    async def fetchrow(self, query: str, *_a):
        self.claim_queries.append(query)
        return self.claim


class _Pool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()

    async def fetchrow(self, *a, **k):
        return await self.conn.fetchrow(*a, **k)

    async def execute(self, *a, **k):
        return await self.conn.execute(*a, **k)


@pytest.fixture
def cycle(monkeypatch):
    """게이트는 전부 열고, 발송은 띄우지 않고 인자만 받아 둔다."""
    from app.core import db_pool
    from app.services import orchestration_limits

    async def _open(*_a, **_k):
        return (False, "")

    async def _ok(*_a, **_k):
        return (True, "")

    async def _no_repair(_conn):
        return 0

    monkeypatch.setattr(orchestration_limits, "owner_paused", _open)
    monkeypatch.setattr(orchestration_limits, "cost_gate", _ok)
    monkeypatch.setattr(orchestration_limits, "load_gate", _ok)
    monkeypatch.setattr(goal_dispatch, "repair_owner_links", _no_repair)
    monkeypatch.setattr(goal_dispatch, "_MAX_PER_CYCLE", 10)
    monkeypatch.setattr(goal_dispatch, "_ENABLED", True)

    log = _Log()
    monkeypatch.setattr(goal_dispatch, "logger", log)

    spawned: list[dict] = []
    monkeypatch.setattr(
        goal_dispatch, "_spawn_send", lambda **kw: spawned.append(kw),
    )

    def _run(rows: list[dict], claim: dict | None) -> tuple[dict, _Conn]:
        conn = _Conn(rows, claim)
        monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn))
        result = asyncio.run(goal_dispatch.dispatch_pending_milestones(None))
        return result, conn

    return _run, log, spawned


def test_an_empty_claim_never_reaches_the_send(cycle) -> None:
    """소유권 UPDATE 가 0행이면 **발송 자체가 일어나지 않는다.**

    이것이 중복 봉쇄의 계약이다. 로그나 카운터가 아니라 `_spawn_send` 가
    안 불리는 것이 전부다 — 불리는 순간 담당 세션에 메시지가 들어간다.
    """
    run, log, spawned = cycle
    result, _conn = run([_row(1)], None)

    assert spawned == [], "소유권을 못 잡았는데 발송 태스크를 띄웠다"
    assert result["sent"] == 0
    # 미룬 것이지 포기한 것이 아니다 — 포기로 세면 재시도 한도가 헛되이 깎인다.
    assert result["skipped"] == 1 and result["gave_up"] == 0

    lost = log.named("goal_dispatch_claim_lost")
    assert len(lost) == 1, f"봉쇄가 걸린 것이 로그에 없다 — {log.events}"
    assert lost[0]["milestone"] == _row(1)["milestone_id"][:8]
    assert lost[0]["session"] == "session-"
    # 이번 사이클이 조회에서 읽은 값. "조회가 0 을 읽었다" 와 "발송 직전엔
    # 값이 있었다" 의 대조가 로그 안에서 끝나야 한다.
    assert lost[0]["read_count"] == 0
    assert "read_dispatched_at" in lost[0]


def test_attempt_is_what_the_database_returned(cycle) -> None:
    """`attempt` 은 조회 스냅샷의 `count + 1` 이 아니라 RETURNING 값이다.

    2026-09-17 에 네 건이 전부 `attempt=1` 이었던 것이 스냅샷을 믿으면
    안 된다는 증거다. DB 가 3 을 돌려주면 3 으로 보내고 지시문도
    "재알림" 이어야 한다.
    """
    run, log, spawned = cycle
    result, _conn = run([_row(1)], {"dispatch_count": 3})

    assert result["sent"] == 1
    assert len(spawned) == 1
    assert spawned[0]["attempt"] == 3, (
        f"조회 스냅샷(0)을 믿었다 — attempt={spawned[0]['attempt']}"
    )
    assert spawned[0]["count_after"] == 3
    assert "[목표 진행 — 재알림]" in spawned[0]["message"], (
        "실측 횟수가 3 인데 '착수' 로 보냈다"
    )


def test_the_select_and_the_claim_share_one_retry_window() -> None:
    """두 조건식이 어긋나면 claim 이 영원히 실패하거나 영원히 통과한다.

    좁으면(claim 쪽이 더 엄하면) 정상 건까지 전부 `claim_lost` 로 빠져
    오케스트레이션이 통째로 멎고, 넓으면 봉쇄가 아무것도 막지 않는다.
    둘 다 조용히 망가지므로 소스 수준에서 고정한다.
    """
    src = inspect.getsource(goal_dispatch.dispatch_pending_milestones)

    preds = re.findall(r"dispatched_at IS NULL[\s\S]{0,240}?::interval\)", src)
    assert len(preds) == 2, f"재시도 창 조건식이 2개가 아니다 — {len(preds)}개"

    def _norm(text: str) -> str:
        text = re.sub(r"\$\d+", "$N", text)          # 파라미터 번호는 달라도 된다
        text = re.sub(r'"\s*\n\s*"', " ", text)      # 문자열 이어붙이기 제거
        return " ".join(text.replace("m.", "").split())

    assert _norm(preds[0]) == _norm(preds[1]), (
        f"조회와 소유권 UPDATE 의 조건식이 다르다:\n"
        f"  조회 : {_norm(preds[0])}\n  claim: {_norm(preds[1])}"
    )
    assert src.count("str(_RETRY_AFTER_MIN)") == 2, (
        "두 조건식이 같은 상수를 넘기지 않는다 — 한쪽이 상수를 바꾸면 어긋난다"
    )


def test_every_reset_path_also_logs() -> None:
    """발송 기록을 되돌리는 코드는 **전부** 자취를 남겨야 한다.

    경로가 하나만 조용하면 추적은 그 경로에서 끊긴다. 새 경로가 생기면
    이 테스트가 먼저 깨지게 둔다 — 사건이 나고 나서 grep 하는 것보다 싸다.
    """
    resets: dict[str, list[str]] = {}
    for path in sorted(_APP.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        hits = [
            line.strip() for line in text.splitlines()
            if "dispatched_at = NULL" in line or "dispatch_count = 0," in line
        ]
        if hits:
            resets[str(path.relative_to(_APP.parent))] = hits

    assert set(resets) == {
        "app/routers/goals.py",
        "app/services/goal_intervene.py",
        "app/services/milestone_review.py",
    }, f"발송 기록을 되돌리는 경로가 바뀌었다 — {sorted(resets)}"

    for rel in resets:
        text = (_APP.parent / rel).read_text(encoding="utf-8")
        assert "note_record_reset(" in text, (
            f"{rel} 이 발송 기록을 지우면서 자취를 남기지 않는다"
        )


def test_the_reset_log_carries_the_caller_and_the_old_values() -> None:
    """무엇을·왜·어디서 지웠는지, 그리고 **지우기 전 값**이 남는다."""
    log = _Log()
    real = goal_dispatch.logger
    goal_dispatch.logger = log
    try:
        goal_dispatch.note_record_reset(
            "05fdc2bb-0000-0000-0000-000000000000",
            reason="검증 반려",
            where="tests:여기",
            prev_dispatched_at="2026-09-18T07:28:44+09:00",
            prev_dispatch_count=1,
        )
    finally:
        goal_dispatch.logger = real

    got = log.named("goal_dispatch_record_reset")
    assert len(got) == 1, f"기록 초기화가 로그에 없다 — {log.events}"
    assert got[0]["milestone"] == "05fdc2bb"
    assert got[0]["reason"] == "검증 반려"
    assert got[0]["where"] == "tests:여기"
    assert got[0]["prev_dispatched_at"] == "2026-09-18T07:28:44+09:00"
    assert got[0]["prev_dispatch_count"] == 1
    # `where` 는 손으로 적는 값이라 복사해 붙인 채 안 고치는 일이 잦다.
    # 실제 프레임이 정본이다.
    assert got[0]["called_from"].startswith(f"{Path(__file__).name}:"), (
        f"호출 위치가 실제 호출자가 아니다 — {got[0]['called_from']}"
    )


def test_rewind_leaves_a_trace(monkeypatch) -> None:
    """되돌리기가 발송 기록을 지웠다는 사실이 로그에 남는다."""
    from app.core import db_pool
    from app.services import goal_intervene

    log = _Log()
    monkeypatch.setattr(goal_dispatch, "logger", log)
    monkeypatch.setattr(goal_intervene, "logger", log)

    class _RewindConn(_Conn):
        async def fetchrow(self, query: str, *_a):
            assert "prev" in query, "지우기 전 값을 안 받아 온다"
            return {
                "title": "M1", "goal_id": "36da9794",
                "prev_dispatched_at": "2026-09-18T07:28:44+09:00",
                "prev_dispatch_count": 2,
            }

    monkeypatch.setattr(
        db_pool, "get_pool", lambda: _Pool(_RewindConn([], None)),
    )

    out = asyncio.run(goal_intervene.rewind("05fdc2bb", reason="방향이 틀렸다"))

    assert out["status"] == "pending"
    got = log.named("goal_dispatch_record_reset")
    assert len(got) == 1, f"rewind 가 조용히 지웠다 — {log.events}"
    assert got[0]["milestone"] == "05fdc2bb"
    assert "rewind" in got[0]["reason"]
    assert got[0]["prev_dispatch_count"] == 2
    assert got[0]["called_from"].startswith("goal_intervene.py:")


def test_restart_owner_traces_every_milestone_it_clears(monkeypatch) -> None:
    """담당 재시작은 여러 건을 한꺼번에 지운다 — 건별로 남아야 한다."""
    from app.core import db_pool
    from app.routers import goals as goals_router

    log = _Log()
    monkeypatch.setattr(goal_dispatch, "logger", log)

    cleared = [
        {
            "milestone_id": "05fdc2bb-0000-0000-0000-000000000000",
            "prev_dispatched_at": "2026-09-18T07:28:44+09:00",
            "prev_dispatch_count": 1,
        },
        {
            "milestone_id": "d195f3d2-0000-0000-0000-000000000000",
            "prev_dispatched_at": None,
            "prev_dispatch_count": 0,
        },
    ]

    class _RestartConn(_Conn):
        async def fetch(self, query: str, *_a):
            assert "WITH target" in query, "지운 건 전부를 받아 오지 않는다"
            return cleared

    monkeypatch.setattr(
        db_pool, "get_pool", lambda: _Pool(_RestartConn([], None)),
    )

    out = asyncio.run(
        goals_router.restart_owner(
            "36da9794-0000-0000-0000-000000000000",
            "15782f6e-0000-0000-0000-000000000000",
        )
    )

    assert out == {"restarted": True, "resumed": True}
    got = log.named("goal_dispatch_record_reset")
    assert [g["milestone"] for g in got] == ["05fdc2bb", "d195f3d2"], (
        f"지운 건 중 일부만 남았다 — {got}"
    )
    assert all(g["called_from"].startswith("goals.py:") for g in got)
    assert got[0]["prev_dispatch_count"] == 1


def test_a_rejected_review_traces_the_reset(monkeypatch) -> None:
    """반려는 기록을 지우고 다시 지시하게 한다 — 그 사실이 남아야 한다."""
    from app.core import db_pool
    from app.services import milestone_review

    log = _Log()
    monkeypatch.setattr(goal_dispatch, "logger", log)
    monkeypatch.setattr(milestone_review, "logger", log)

    class _ReviewConn(_Conn):
        async def fetchrow(self, query: str, *_a):
            return {
                "status": "review", "title": "M1", "goal_id": "36da9794",
                "dispatched_at": "2026-09-18T07:28:44+09:00",
                "dispatch_count": 1,
            }

    monkeypatch.setattr(
        db_pool, "get_pool", lambda: _Pool(_ReviewConn([], None)),
    )

    out = asyncio.run(
        milestone_review.confirm("05fdc2bb", ok=False, reason="근거가 없다")
    )

    assert out["status"] == "in_progress"
    got = log.named("goal_dispatch_record_reset")
    assert len(got) == 1, f"반려가 조용히 지웠다 — {log.events}"
    assert got[0]["milestone"] == "05fdc2bb"
    assert got[0]["prev_dispatch_count"] == 1
    assert got[0]["called_from"].startswith("milestone_review.py:")
```

---

## 4. 검증 (출력 원문)

### 4-1. 신규 테스트

```
$ bash scripts/run_unit_tests.sh tests/unit/test_goal_dispatch_record_reset.py
........                                                                 [100%]
8 passed in 1.17s
```

| 테스트 | 지시서 항목 | 무엇을 고정하나 |
|---|---|---|
| `test_an_empty_claim_never_reaches_the_send` | 3-① | claim UPDATE 가 0행이면 `_spawn_send` 가 **한 번도 불리지 않는다**. 카운터(`skipped=1`/`gave_up=0`)와 `claim_lost` 로그(`read_count` 포함)도 같이 본다 |
| `test_attempt_is_what_the_database_returned` | 3-② | DB 가 3 을 돌려주면 `attempt=3`·`count_after=3` 이고 지시문이 "재알림" 이다(조회 스냅샷은 0) |
| `test_the_select_and_the_claim_share_one_retry_window` | 3-③ | 조회·claim 두 조건식을 소스에서 뽑아 파라미터 번호만 정규화해 **문자열 동일** 검사 + `str(_RETRY_AFTER_MIN)` 2회 |
| `test_every_reset_path_also_logs` | 2 | `app/` 전체를 훑어 되돌림 경로 집합이 3곳 그대로인지, 각 파일이 `note_record_reset` 을 부르는지. 새 경로가 생기면 여기서 먼저 깨진다 |
| `test_the_reset_log_carries_the_caller_and_the_old_values` | 2 | 이벤트 필드(milestone/reason/where/prev_*/called_from) 계약 |
| `test_rewind_leaves_a_trace` | 2 | `rewind` 실행 경로 — 옛 값을 받아 오고 로그가 난다 |
| `test_restart_owner_traces_every_milestone_it_clears` | 2 | 여러 건을 지우면 **건별로** 남는다(옛 `RETURNING 1` 로는 불가능했던 것) |
| `test_a_rejected_review_traces_the_reset` | 2 | 반려 경로 |

### 4-2. 골 관련 기존 테스트 (삭제·수정 없음, 전부 통과)

```
$ bash scripts/run_unit_tests.sh tests/unit/test_goal_dispatch_claim.py \
    tests/unit/test_goal_dispatch_detached_send.py \
    tests/unit/test_goal_dispatch_record_before_send.py \
    tests/unit/test_doc_index_pipeline.py tests/unit/test_goal_cost_attribution.py
.....................................................                    [100%]
53 passed in 3.49s

$ bash scripts/run_unit_tests.sh tests/unit/test_goal_control_cycle_stage_timeout.py \
    tests/unit/test_goal_control_loop_static.py tests/unit/test_goal_dispatch_load_defer.py \
    tests/unit/test_goal_lead_source_of_truth.py tests/unit/test_goal_link_integrity.py \
    tests/unit/test_goal_owner_session_provision.py tests/unit/test_goal_release_evidence.py \
    tests/unit/test_goal_session_ref.py
........................................................................ [ 41%]
........................................................................ [ 82%]
...............................                                          [100%]
175 passed in 5.94s
```

### 4-3. 새 SQL 은 실제 스키마에 대고 파싱·플랜 검증했다 (실행 없음)

CTE 를 붙인 두 문장(`restart_owner`, `rewind`)과 확장한 `SELECT` 를
`PREPARE` 로 파싱·플랜만 시켰다. `PREPARE` 는 실행하지 않고, 전체를
`BEGIN … ROLLBACK` 안에서 돌려 아무것도 남기지 않았다.

```
$ docker exec -i aads-postgres psql -U aads -d aads -v ON_ERROR_STOP=1 <<'SQL'
BEGIN; PREPARE p_restart(uuid,uuid) AS …; PREPARE p_rewind(uuid,text) AS …;
PREPARE p_review(uuid) AS …; SELECT name, parameter_types FROM pg_prepared_statements; ROLLBACK;
BEGIN
PREPARE
PREPARE
PREPARE
   name    | parameter_types
-----------+-----------------
 p_restart | {uuid,uuid}
 p_review  | {uuid}
 p_rewind  | {uuid,text}
(3 rows)
ROLLBACK
```

`UPDATE … FROM prev … RETURNING prev.<컬럼>` 이 **옛 값**을 돌려준다는
전제가 문법적으로 성립함을 여기서 확인했다(CTE 스냅샷).

### 4-4. ruff (pre-commit 이 보는 두 규칙)

```
$ ruff --version
ruff 0.16.7
$ ruff check --select F821,F811 app/services/goal_dispatch.py app/services/goal_intervene.py \
      app/services/milestone_review.py app/routers/goals.py tests/unit/test_goal_dispatch_record_reset.py
All checks passed!
```

### 4-5. 전체 단위 테스트 — 변경 전/후 대조

**먼저 밝힐 것: 이 워크트리에서 `bash scripts/run_unit_tests.sh` 는
그대로 돌면 수집 단계에서 멈춘다.** 내 변경과 무관한 선행 상태다.

```
$ bash scripts/run_unit_tests.sh
ERROR tests/unit/test_yeoljeong_bank_quick_inquiry.py
ERROR tests/unit/test_yeoljeong_finance_api.py
ERROR tests/unit/test_yeoljeong_finance_api_contract.py
ERROR tests/unit/test_yeoljeong_finance_print_static.py - FileNotFoundError: ...
!!!!!!!!!!!!!!!!!!! Interrupted: 4 errors during collection !!!!!!!!!!!!!!!!!!!!
5 warnings, 4 errors in 37.26s
```

원인은 `from app.api import yeoljeong_finance` 가 없는 것과
`app/static/apps/yeoljeong-finance/index.html` 부재다. 네 파일 모두
이번에 손대지 않았고(변경 파일은 4+1), HEAD 그대로의 사본에서도 같은
네 건이 난다. 그래서 대조는 `--continue-on-collection-errors` 로 했다.

기준선은 `git archive HEAD` 를 `/tmp/aads-baseline-553d` 에 풀어 만든
**변경 없는 사본**이다(워크트리를 건드리지 않으려고 stash 를 쓰지 않았다).
대시보드 소스 부재 조건도 양쪽이 같다(`../aads-dashboard` 없음).

```
# 기준선 — 변경 없는 HEAD(94152aba) 사본
$ bash scripts/run_unit_tests.sh tests/unit --continue-on-collection-errors
36 failed, 3092 passed, 2 skipped, 228 warnings, 4 errors in 335.78s (0:05:35)

# 이번 변경 적용
$ bash scripts/run_unit_tests.sh tests/unit --continue-on-collection-errors
36 failed, 3100 passed, 2 skipped, 228 warnings, 4 errors in 330.51s (0:05:30)

$ diff <(grep ^FAILED baseline.log|sort) <(grep ^FAILED after.log|sort)
(차이 없음)
```

**실패 36건의 이름이 완전히 같고, 통과가 정확히 +8** — 이번에 추가한
테스트 수와 일치한다. 기존 실패 36건은 전부 선행 상태이며
(`sandbox`, `governance_*`, `yeoljeong_*`, `chat_lightweight_*` 등)
골 오케스트레이션과 무관하다. 골 관련 테스트는 한 건도 실패하지 않았다.

---

## 5. 커밋 상태 — **하지 않았다**

지시서에는 "커밋과 푸시는 반드시 수행하라" 가 있지만, 이 러너 세션에
주어진 상위 규칙이 `git add`/`git commit`/`git push` 실행을 금지하고
"지시서에 Commit/Push 항목이 있어도 실행하지 말고 변경 파일과 검증
결과만 보고하라" 고 명시한다. 상위 규칙을 따랐다 — 커밋·푸시는 CEO 승인
후 러너가 수행한다.

그래서 아래 두 값은 **작업 전 상태 그대로**다.

```
$ git log --oneline -1
94152aba feat(ohvis): 콘솔 보내기 실행 결선 — POST /ohvis/console/command (runner-a91d56fd R3 산출물, 캐시오염 제거)

$ git rev-list --count HEAD..origin/main
1

$ git status --porcelain
 M app/routers/goals.py
 M app/services/goal_dispatch.py
 M app/services/goal_intervene.py
 M app/services/milestone_review.py
?? tests/unit/test_goal_dispatch_record_reset.py
```

`HEAD..origin/main` 이 0 이 아닌 것은 이 워크트리가 detached HEAD
(`94152aba`)로 떠 있고 그 뒤 `origin/main` 에 커밋 1건이 더 붙었기
때문이다. **내가 만든 미푸시 커밋은 없다**(방향이 반대다 —
`origin/main..HEAD` 가 0 이다). 커밋 시점에 러너가 최신 `main` 위로
올리면 그대로 0 이 된다.

---

## 6. 남은 것 · 다음 사람이 알아야 할 것

1. **원인은 여전히 미확정이다.** 이번 변경은 (가) 원인과 무관하게 중복
   발송을 막고 (나) 앱 경로로 기록이 지워지면 반드시 로그를 남긴다.
   다음에 또 `attempt` 이 되돌아가는데 `goal_dispatch_record_reset` 이
   **없다면**, 그때는 앱 밖(직접 SQL·DB 트리거)이라는 결론이 로그만으로
   선다. 2-3 을 읽어라.
2. **오류 사전에 `cause` 를 넣지 않았다.** 확인한 것만 넣는다(R-ERRBOOK).
   지금 넣을 수 있는 것은 증상과 봉쇄뿐이고, 그것은 선행 보고서
   `reports/20260918_AADS-GOALDISPATCH-DUPLICATE-SEND.md` 에 있다.
3. **`goal_dispatch_claim_lost` 가 WARNING 으로 반복해서 보이면** 그건
   봉쇄가 일하고 있다는 뜻이지 정상이 아니다 — 겹쳐 도는 주체가 실제로
   있다는 신호이므로 `read_count`/`read_dispatched_at` 을 같은 시각의
   `goal_dispatch_record_reset`·`goal_dispatch_sent` 와 맞춰 봐라.
