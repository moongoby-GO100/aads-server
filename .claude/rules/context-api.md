# Context API 규칙
- directive_lifecycle: queued→running→completed/failed 전이 자동 기록
- cost_tracking: 토큰/비용 자동 기록
- commit_log: SHA 자동 추출
- lessons: POST /api/v1/lessons 로 교훈 등록 (Phase 3에서 구현)
- 유지보수 모드: POST /ops/maintenance/start 후 배포, 완료 후 /end
