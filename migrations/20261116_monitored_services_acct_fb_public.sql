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
