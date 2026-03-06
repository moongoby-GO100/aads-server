---
# L-001: Watchdog 서비스명 불일치 오탐 폭주

- 출처: AADS-117 (2026-03-06)
- 심각도: critical
- 적용 범위: 모든 watchdog/모니터링 서비스 등록 시

## 상황
monitored_services의 check_target이 실제 Docker 컨테이너명과 불일치

## 결과
30초마다 새 에러 INSERT → 6시간에 903건 → 디스크 94%→100% → API timeout

## 해결
check_target을 docker ps --filter name=xxx 방식으로 변경, error_hash UPSERT

## 예방법
서비스 감시 추가 시 docker ps/systemctl로 실제 이름 확인, 에러 로그는 항상 해시 중복방지
---
