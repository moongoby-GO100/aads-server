-- 롤백 정본: migrations/rollback/20261116_monitored_services_acct_fb_public.down.sql
-- 실행 주체: CEO 승인 후 운영 세션. psql 변수 created_service_id, apply_started_at, apply_finished_at 을 전달한다.
-- 증거: 적용기는 schema_migrations.note 에 precheck만 저장하며 행 id·시각은 저장하지 않는다.
-- 운영 세션이 적용 직후 SELECT 결과(기존 행 부재, 신규 id, 적용 시작·종료 시각)를 핸드오버 DB에 기록한다.
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
