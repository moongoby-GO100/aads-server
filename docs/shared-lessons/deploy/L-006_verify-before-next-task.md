---
# L-006: 배포 후 5분 모니터링 의무

- 출처: AADS T-038 (2026-03-06)
- 심각도: critical
- 적용 범위: 모든 서비스 배포, Watchdog 배포

## 상황
Watchdog 배포 직후 검증 없이 다음 작업으로 이동

## 결과
서비스명 불일치 오탐을 6시간 동안 미발견, 903건 쓰레기 데이터

## 해결
사후 정리(DELETE + VACUUM)

## 예방법
배포 후 최소 5분 error_log/watchdog 모니터링 필수, FLOW Wrap up 의무화
---
