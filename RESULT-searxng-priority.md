# RESULT: web_search에서 SearXNG 1순위 검색 엔진 적용

## 검증 체크리스트

- [x] **구현 목표**: `_web_search()` 메서드에서 SearXNG를 1순위 검색 엔진으로 사용, 실패 시 기존 Google→Naver→Kakao 폴백
- [x] **검증 방법**: `docker ps --filter "name=aads-server"` → healthy 확인 / `docker logs aads-server --since 60s | grep -i error` → 에러 0건
- [x] **완료 기준**: aads-server 컨테이너 healthy, 에러 로그 0건, `_web_search()` 메서드에 SearXNG 1순위 로직 삽입 완료
- [x] **실패 기준**: 컨테이너 unhealthy, 에러 로그 발생, SearXNG 호출 미삽입 — 해당사항 없음
- [x] **서비스 재시작 확인**: `docker ps` → `aads-server Up (healthy)` ✅
- [x] **에러 로그 0건**: `docker logs --since 60s | grep -i error` → 출력 없음 ✅

## 변경 사항

### 파일: `app/services/tool_executor.py`
- **위치**: 506~528행 (기존 auto 분기 전에 삽입)
- **내용**: SearXNG 1순위 호출 → 성공 시 즉시 반환, 실패 시 기존 폴백 로직 유지

### 변경 로직
```
web_search(auto 모드) 호출 흐름:
1. SearXNG 호출 (aads-searxng:8080, 무료/무제한)
   ├─ 성공 (results 1개+) → 즉시 반환 {text, citations, engines_used: ["searxng"]}
   └─ 실패 (에러/타임아웃/0건) → logger.warning → 아래 폴백
2. 한국어 쿼리: Google + Naver 동시 → Kakao 폴백 (기존 그대로)
3. 영어 쿼리: Google → Naver 폴백 (기존 그대로)
```

### 미변경 사항
- `engine` 파라미터 명시 지정(google/naver/kakao/all) 시 SearXNG 미경유
- 기존 Google/Naver/Kakao 폴백 로직 100% 유지
- `_search_searxng()` 메서드(568행) 그대로 사용
