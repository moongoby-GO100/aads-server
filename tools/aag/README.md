# AAG — AADS Architecture Guard

기록된 아키텍처를 **추출하고, 그림으로 만들고, 어긋나면 막는다.**
세 층이 각각 다른 것을 본다. 한 층만 있으면 나머지 두 종류의 사고는 안 잡힌다.

| 층 | 무엇을 보나 | 도구 | 어디서 도나 |
|---|---|---|---|
| L1/L2 | **구조** — 라우트·네임스페이스·테이블·프런트 호출 | `scan_aads.py` | pre-commit(조건부) + systemd 타이머 2시간 |
| L3 | **행위** — 실행 기록에 남은 설계 사고 | `behavior_check.py` | systemd 타이머 30분 |

## 실행

    # 스캔 + 리포트 생성
    python3 tools/aag/scan_aads.py

    # 고정선보다 결함이 늘었는지만 본다 (게이트가 쓰는 형태)
    python3 tools/aag/scan_aads.py --check-baseline --no-write

    # 자기검증 22건
    python3 tools/aag/selftest_aads.py

종료코드가 세 가지다. **2 를 0 과 섞으면 안 된다.**

| 코드 | 뜻 |
|---|---|
| 0 | 고정선 대비 증가 없음 |
| 1 | 결함이 늘었다 |
| 2 | **스캔 불가**(대상 파일 0개) — "위반 0건" 이 아니라 "점검 못함" |

## 고정선(baseline)

`baseline_aads.json` 의 숫자는 **허용치가 아니라 현재 빚**이다.
줄이는 방향으로만 갱신한다. 늘려서 통과시키려면 왜 늘었는지 먼저 적어라.

    python3 tools/aag/scan_aads.py --write-baseline

## 게이트가 두 벌인 이유

pre-commit 은 `app/api/`·`app/routers/`·`app/main.py`·`tools/aag/` 를 건드린
커밋에서만 돈다(전체 스캔 19.6초라 매 커밋에 걸지 않는다).

그런데 이 스캔의 절반은 **다른 저장소**(`/root/aads/aads-dashboard`)를 본다.
대시보드에서 `fetch` 경로 한 줄만 바꿔도 `ROUTE_MISSING` 이 느는데
aads-server 에는 커밋이 없어 pre-commit 이 돌지 않는다.
저장소 경계를 넘는 표류는 타이머(`aads-aag-struct-check.timer`)가 잡는다.

## 이 게이트가 실제로 잡은 것 (2026-09-16)

`ROUTE_MISSING` 12건은 전부 진짜였다 — 대시보드 화면이 부르는 경로가
백엔드에 **없다**. HTTP 로는 안 보인다(인증 미들웨어가 라우팅 전에 401 을
돌려주므로 404 와 구분되지 않는다). 소스에서만 보인다.

    /api/v1/ops/qa-results        → app/ 전체 grep 0건
    /api/v1/ops/design-reviews    → app/ 전체 grep 0건
    /api/v1/kakao-bot/settings    → kakao_bot.py 에 /settings 없음

`PATH_DRIFT` 1건은 `api.ts` 가 `/chat/messages` 를 POST 로 부르는데
백엔드에는 GET 만 있는 경우다.
