# RESULT: web_search SearXNG 1순위 적용

## 구현 요약
`app/services/tool_executor.py`의 `_web_search()` 메서드에서 SearXNG를 1순위 검색 엔진으로 적용.
`engine=auto`일 때 SearXNG 먼저 호출하고, 실패 시 기존 Google/Naver/Kakao 폴백.

## 변경 파일
- `app/services/tool_executor.py` (491~577행)
  - docstring: "스마트 검색 — SearXNG 1순위, 실패 시 Google/Naver/Kakao 폴백"
  - 506행: SearXNG 1순위 호출 블록 (try/except)
  - 530행: SearXNG 실패 시 기존 폴백 로직 (한국어: Google+Naver, 영어: Google→Naver)

## 검증 체크리스트

### [x] 구현 목표
web_search 호출 시 SearXNG를 1순위로 사용, 실패 시 Google/Naver/Kakao 폴백

### [x] 검증 방법
```bash
docker exec aads-server python3 -c "
import asyncio, json
from app.services.tool_executor import ToolExecutor
te = ToolExecutor()
result = asyncio.get_event_loop().run_until_complete(
    te._web_search({'query': 'Python FastAPI 최신 버전', 'count': 3})
)
print(json.dumps(result, ensure_ascii=False, indent=2)[:800])
"
```

### [x] 완료 기준: SearXNG 결과 정상 반환
- **한국어 쿼리** (`Python FastAPI 최신 버전`):
  - `"source": "searxng"` ✅
  - `"engines_used": ["searxng"]` ✅
  - `citations` 3건 (fastapi.tiangolo.com 등) ✅
- **영어 쿼리** (`latest React 19 features`):
  - `"source": "searxng"` ✅
  - `"engines_used": ["searxng"]` ✅
  - 결과 정상 반환 ✅

### [x] 실패 기준: 해당 없음
- SearXNG 결과 없거나 `source`가 `google`/`naver` → ❌ (해당 없음, 정상 작동)

### [x] 서비스 재시작 확인
```
$ docker ps --filter "name=aads-server"
aads-server  Up 34 seconds (healthy)

$ docker ps --filter "name=searxng"
aads-searxng  Up 31 hours (healthy)
```
두 컨테이너 모두 running + healthy ✅

### [x] 에러 로그 0건
```
$ docker logs --since 60s aads-server 2>&1 | grep -i error
(출력 없음)
```
에러 로그 0건 ✅

## 로직 흐름
```
_web_search(inp) 호출
  ├─ engine 명시("google"/"naver"/"kakao"/"all") → 해당 엔진만 사용
  ├─ engine="auto" (기본값)
  │   ├─ 1순위: SearXNG (_search_searxng) 호출
  │   │   ├─ 성공 + results ≥ 1 → 즉시 반환 (source: "searxng")
  │   │   └─ 실패/에러/결과0건 → logger.warning 후 폴백
  │   └─ 2순위 폴백 (기존 로직)
  │       ├─ 한국어: Google + Naver 동시 → 실패 시 Kakao
  │       └─ 영어: Google → Naver 순차
```

## 날짜
2026-03-26
