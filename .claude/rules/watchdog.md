# Watchdog 규칙
- 서비스 등록 전 docker ps --filter name=xxx로 실제 이름 확인 (L-001)
- error_log INSERT는 반드시 error_hash UPSERT (L-007)
- 배포 후 5분 error_log 모니터링 필수 (L-006)
- 오탐 발생 시 DELETE + VACUUM, 근본 원인(서비스명 불일치) 해결
