# AADS-SCHEDULE-TASK-BRIDGE-PERSIST-20261004

## 원인
MCP 브리지(세션별 프로세스)에는 `app.state.scheduler` 가 없어 `_ensure_scheduler()` 가 메모리
BackgroundScheduler 를 새로 만들었고, 예약은 브리지 종료와 함께 사라졌다(persisted=false).
영속 jobstore(SlotGatedJobStore)는 API 프로세스에만 있다.

## 변경
- `app/api/ceo_chat_tools_scheduler.py`: 로컬 스케줄러 생성 제거. 스케줄러가 없으면 API 에 위임
  (`POST /api/v1/internal/scheduler/{schedule,unschedule}`, `GET .../jobs`). 위임 실패는
  `persisted=false` + `error`(메모리 폴백 없음). API 프로세스 내 호출 경로는 동작 불변.
- `app/api/internal_scheduler.py`(신규): `require_internal_admin` + `AADS_MONITOR_KEY` 로 인증.
  공개 상수 `internal-pipeline-call` 은 `/pipeline/` 경로 한정이라 통과하지 못한다.
  같은 report_session_id 가 같은 name 으로 재등록하면 `replace_existing=True`(재시도 멱등),
  다른 세션/세션 없음이면 기존대로 중복 오류.
- `app/main.py`: 라우터 마운트 1줄. 슬롯 게이트(SlotGatedJobStore)·job id(`user_<name>`)는 그대로.

## 알려진 한계
- 위임 대상은 브리지가 있는 컨테이너 자신의 API(`AADS_API_BASE`, 기본 localhost:8080)다.
  스탠바이 슬롯에서 등록돼도 행은 공용 DB 에 들어가고, 액티브 슬롯 스케줄러는 30초 주기
  시스템 잡 때문에 다음 폴링에서 집어간다.

## 배포 후 검증 절차 (배포는 이 작업 범위 아님)
1. 브리지 세션에서 `schedule_task(name='probe', schedule_type='once', action_type='health_check',
   action_config={}, schedule_config={'delay_minutes': 60})` → 응답 `persisted=true`, `delegated=true`.
2. `SELECT count(*) FROM apscheduler_jobs;` 가 1 증가, `id='user_probe'`.
3. 다른 세션에서 `list_scheduled_tasks` 에 `user_probe`(persisted=true) 가 보임.
4. `unschedule_task(name='probe')` → 행 삭제 확인.
5. API 미응답 시 `persisted=false` + `scheduler_delegate_failed` 가 반환되는지(선택).
