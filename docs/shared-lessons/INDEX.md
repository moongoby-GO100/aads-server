# 공유 교훈 INDEX (최종: 2026-03-06, 8건)

## infra (서버·디스크·Docker·네트워크)
- L-001: Watchdog 서비스명 불일치 오탐 폭주 [AADS-117] → infra/L-001_watchdog-false-positive.md
- L-002: 디스크 100% 도달 → PostgreSQL write 실패 연쇄 [AADS] → infra/L-002_disk-full-cascade.md
- L-003: Docker image 누적 → 주간 prune 필요 [AADS] → infra/L-003_docker-prune-schedule.md

## api (외부 API·토큰·웹훅·타임아웃)
- L-004: API 토큰 만료 전 자동갱신 필수 [KIS 9건] → api/L-004_token-refresh-pattern.md
- L-005: 외부 SaaS 웹훅 미지원 시 ACK+재전송 [GenSpark] → api/L-005_genspark-no-webhook.md

## deploy (배포·검증·롤백)
- L-006: 배포 후 5분 모니터링 의무 [AADS T-038 903건] → deploy/L-006_verify-before-next-task.md

## data (DB·마이그레이션·로깅)
- L-007: 에러 로그 해시 기반 중복 방지 [Watchdog] → data/L-007_error-hash-dedup.md

## patterns (재사용 코드 패턴)
- L-008: ACK+Retry 패턴 (외부 메시지 확인) [Bridge.py] → patterns/L-008_ack-retry-pattern.md
