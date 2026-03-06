# Ops API 규칙
<!-- paths: **/ops*, **/lifecycle*, **/cost* -->
- directive_lifecycle UPSERT: pre-computed timestamps 사용 (asyncpg 타입 이슈)
- health-check 응답에 신규 필드 추가 시 반드시 기존 필드 유지
- 유지보수 모드: Docker rebuild/migration 감지 시 자동 활성화
