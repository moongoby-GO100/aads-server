---
# L-002: 디스크 100% 도달 → PostgreSQL write 실패 연쇄

- 출처: AADS (2026-03-06)
- 심각도: critical
- 적용 범위: DB 로그 테이블 설계, 디스크 모니터링

## 상황
error_log 테이블 무한 INSERT로 PostgreSQL WAL 급증

## 결과
디스크 100% → PostgreSQL read-only → API 500 → 전체 파이프라인 정지

## 해결
DELETE 오탐 데이터, VACUUM FULL, error_log에 UPSERT + occurrence_count

## 예방법
디스크 75% 경고/90% 긴급 알림, 로그 테이블은 항상 TTL 또는 max_rows 설정
---
