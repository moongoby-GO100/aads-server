-- 롤백: monitored_services 에서 acct-fb-public 감시 항목 제거
DELETE FROM monitored_services
WHERE server = 'cafe24_114' AND service_name = 'acct-fb-public';
