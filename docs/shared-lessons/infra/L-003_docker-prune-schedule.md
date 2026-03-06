---
# L-003: Docker image 누적 → 주간 prune 필요

- 출처: AADS (2026-03-06)
- 심각도: normal
- 적용 범위: Docker 운영 서버 전반

## 상황
Docker image/volume 누적으로 디스크 사용률 점진 증가

## 결과
수 주 후 디스크 경고 임계치 도달

## 해결
docker system prune -af --volumes 주간 크론

## 예방법
주간 크론 등록, 프로덕션 서버 모두 동일 적용
---
