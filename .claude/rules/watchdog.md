# Watchdog 규칙
<!-- paths: **/watchdog*, **/error_log* -->
- 서비스 감시 추가 시: docker ps --filter name=xxx로 실제 이름 확인 (L-001)
- error_log INSERT: error_hash 기반 UPSERT 필수 (L-007)
- 배포 후 5분 모니터링 필수 (L-006)
- 10회 연속 실패 시 텔레그램 긴급알림
- CEO 승인 필요 항목: 원격 서버 복구, 서비스 재시작
