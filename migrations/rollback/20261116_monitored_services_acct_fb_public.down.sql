-- 롤백: 이번 적용에서 생성한 행만 제거하며 기존 행은 보존한다.
-- 적용 전 행이 없었고 INSERT 로 새로 생성되었음을 기록으로 확인한 경우에만 실행한다.
-- psql 변수 created_service_id, apply_started_at, apply_finished_at 을 적용 기록 값으로 전달한다.
-- 증거가 없으면 실행하지 않는다. 변수가 없으면 psql 은 SQL 오류로 삭제하지 않는다.
DELETE FROM monitored_services
WHERE server = 'cafe24_114' AND service_name = 'acct-fb-public'
  AND id = :'created_service_id'
  AND created_at >= :'apply_started_at'::timestamptz
  AND created_at <= :'apply_finished_at'::timestamptz;
