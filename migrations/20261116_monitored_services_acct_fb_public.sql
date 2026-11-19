-- 롤백 정본: migrations/rollback/20261116_monitored_services_acct_fb_public.down.sql
-- 실행 주체: CEO 승인 후 운영 세션. psql 변수 created_service_id, apply_started_at, apply_finished_at 을 전달한다.
-- 증거 판정: schema_migrations.note 의 precheck 를 먼저 확인하되 이것만으로 적용 전 기존 행 부재를 판정하지 않는다.
-- release_migration_precheck.py 는 표·열·색인만 검사하며 이 INSERT 는 objects=[] 이므로 precheck=absent 도 행 부재 증거가 아니다.
-- CEO 승인 후 운영 세션이 적용 전에 SELECT clock_timestamp() AS apply_started_at; 및
-- SELECT id, created_at FROM monitored_services WHERE server = 'cafe24_114' AND service_name = 'acct-fb-public';
-- 결과를 핸드오버 DB에 기록하고 재조회한다. 기존 행이 있으면 down 삭제 대상이 아니다.
-- 적용기는 XS 이전 값이나 신규 서비스 id 를 저장하지 않는다. scripts/apply_migration.sh 는
-- 시작 clock_timestamp 를 세션 설정에만 두고 duration_ms 를 기록한다. schema_migrations.applied_at 은
-- DEFAULT now() 또는 충돌 갱신 now() 로 트랜잭션 시작 시각이며 실제 적용 종료 시각이 아니다.
-- 따라서 종료 시각은 적용 성공 반환 후 운영 세션이 SELECT clock_timestamp() AS apply_finished_at; 으로 기록한다.
-- 적용 직후 위 서비스 SELECT 의 신규 id·created_at 과 schema_migrations.note, applied_at, duration_ms 도
-- 핸드오버 DB에 기록하고 재조회한다. 시작·종료 시각은 운영 SELECT 기록값을 사용한다.
-- note.precheck 및 적용 전 SELECT 부재·적용 후 신규 행 증거가 모두 있어야 삭제 여부를 판정한다.
-- 기록 사이 동시 등록 가능성을 배제할 수 없으면 생성 소유권 미입증으로 down 을 실행하지 않는다.
-- 대상: monitored_services 의 cafe24_114 / acct-fb-public HTTP 감시 항목.
-- 조건: ON CONFLICT DO NOTHING 으로 멱등; 적용 전 기존 행 유무와 적용 시작·종료 시각 및 신규 행 id 를 기록한다.
-- 금지: 기존 행 수정·삭제, 자동 복구 활성화 및 승인 없는 원격 재시작 금지.
-- 롤백: 이번 적용으로 생긴 행만 삭제한다. 적용 전 행이 없었고 기록한 신규 id 및 server/service_name 이 일치하며
-- created_at 이 기록한 적용 시작 시각 이상·종료 시각 이하인 경우만 down 을 실행한다. 기존 행은 보존한다.
-- 생성 증거가 없거나 기존 행이 있었으면 삭제하지 않는다. down 의 psql 변수로 기록한 id 와 시각을 전달한다.
-- M5 운영 전환 인증: fb.newtalk.kr 상시 감시 등록 (자동 복구 없음)
--
-- 비파괴 근거: 기존 데이터를 변경(UPDATE)하거나 삭제(DELETE/DROP/TRUNCATE)하지 않으며,
--             cafe24_114 서버의 acct-fb-public 서비스 감시 항목이 없을 때만 신규 등록하는
--             멱등 추가(INSERT ... ON CONFLICT (server, service_name) DO NOTHING) 작업이다.
-- auto_recovery_command 는 NULL 이다. 원격 서버 복구·재시작은 CEO 승인 항목이므로 Healer에 의한 자동 복구를 차단한다.
-- check_type='http_health', check_target='https://fb.newtalk.kr/health/live'
-- check_interval=60, timeout=10, enabled=TRUE

INSERT INTO monitored_services (
    server,
    service_name,
    check_type,
    check_target,
    check_interval,
    timeout,
    auto_recovery_command,
    enabled
) VALUES (
    'cafe24_114',
    'acct-fb-public',
    'http_health',
    'https://fb.newtalk.kr/health/live',
    60,
    10,
    NULL,
    TRUE
)
ON CONFLICT (server, service_name) DO NOTHING;
