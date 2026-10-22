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

    # 자기검증 32건
    python3 tools/aag/selftest_aads.py

종료코드가 세 가지다. **2 를 0 과 섞으면 안 된다.**

| 코드 | 뜻 |
|---|---|
| 0 | 고정선 대비 증가 없음 |
| 1 | 결함이 늘었다 |
| 2 | **스캔 불가**(대상 파일 0개) — "위반 0건" 이 아니라 "점검 못함" |

## 코드 의존 간선 (S1)

규칙 파일에 `code_deps:` 섹션이 있으면 Python import(`imports`)·테스트(`tests`)·TS import 간선과
프런트 화면 노드(role/route)를 추가로 싣는다. 기존 노드 id·간선 종류는 그대로고 추가만 한다.
`AAG_SCAN_CODE_DEPS=0` 이면 추출을 건너뛰고 출력이 S1 이전과 같다. 구현은 `code_deps.py`.

## 운영 자산 간선 (S2)

규칙 파일에 `ops_assets:` 섹션이 있으면 스크립트·systemd·crontab 파일·nginx·compose 를 노드로,
`invokes`(스크립트→스크립트/모듈) · `runs`(timer→service→스크립트, crontab→스크립트, compose→엔트리) ·
`proxies`(nginx location→upstream/API 네임스페이스) · `uses_image` · `builds` 를 간선으로 더한다.
노드 kind 는 `script` `systemd_unit` `crontab` `nginx_location` `compose_service` `image` `upstream`
`dockerfile` 이 늘고, `.py` 스크립트는 `module` 노드에 `role:"script"` 를 붙인다. 기존 id·kind 는 그대로다.
`AAG_SCAN_OPS_ASSETS=0` 이면 추출을 건너뛰고 출력이 S1 스냅샷과 같다. 구현은 `ops_assets.py`.

- 저장소에 커밋된 상태만 본다. 해석하지 못한 경로는 간선 없이 `unresolved_refs` 로 센다(추측 금지).
  저장소 밖 절대경로(`/usr/bin/...`, 설치된 사본)는 `external_refs` 다.
- 간선은 명령의 **실행 대상**에만 생긴다. `bash a.sh b.sh` 에서 `b.sh` 는 인자, `docker cp x.sh …` 는 호출이 아니다.
  `__file__` 기준 경로(`Path(__file__).parents[1] / "x.sh"`)는 정확히 계산하고, 그 밖의 동적 접두
  (`f"{root}/x.sh"`)는 `dynamic_path` 로만 센다. 작업 디렉터리(`cwd=`, systemd `WorkingDirectory`,
  compose `working_dir`, `docker exec -w`, `cd`)가 저장소 밖이거나 미상이면 상대 경로를 저장소 파일에
  잇지 않는다(`unknown_cwd`). compose 의 실제 argv 는 `entrypoint + command` 이다.
- 운영 서버의 live crontab·활성 timer 는 이 그래프에 넣지 않는다. `ops_observation.py` +
  `scripts/aag_ops_collect.py` 가 별도 관측(`aag_ops_observations`)으로 다룬다(읽기 전용, 기본은 계획만 출력).
- `stats.ops_assets` 에 종류별 노드·간선 수와 해석 실패 표본, `coverage` 에 `scripts_total` /
  `scripts_as_nodes` / `schedule_nodes` 가 붙는다.

### 운영 수집 활성화 절차

1. 승인된 릴리스 절차에서 `migrations/20261022_aag_ops_observations.sql` 을 먼저 적용한다.
   기존 그래프 적재 테이블이나 latest pointer 를 변경하지 않는 추가 테이블이다.
2. 같은 릴리스의 추출기·규칙·수집기를 배포한다. `AAG_SCAN_OPS_ASSETS=0` 으로
   저장소 운영 자산 추출만 되돌릴 수 있다. 관측 테이블의 기존 데이터는 보존한다.
3. 각 서버에서 `python3 scripts/aag_ops_collect.py --plan` 으로 명령·형식을 검토한 뒤,
   운영자가 `--project PROJECT --host HOST --execute --out /tmp/aag-ops.json` 을 실행한다.
   수집기는 원격 접속을 하지 않으며 두 읽기 명령에 각각 20초 제한을 둔다.
4. 출력 JSON 을 중앙 적재 호스트로 전달해 `--from-json /tmp/aag-ops.json --sql` 로 검토하고
   `--from-json /tmp/aag-ops.json --load` 로 적재한다. DB 컨테이너·계정은
   `AAG_PG_CONTAINER`, `AAG_PG_USER`, `AAG_PG_DB` 로 지정한다.
5. `command_status` 의 성공 여부와 서버별 최신 `collected_at` 을 확인한다.
   같은 관측 JSON 재적재만 중복 제거한다. 같은 내용도 새 수집 시각이면 별도 행으로 보존한다.
   정기 실행 등록은 이 변경에 포함하지 않는다.

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

## ROUTE_SHADOWED — 2026-09-16 에 추가한 규칙

한 모듈이 같은 METHOD+경로를 두 번 등록하면 FastAPI 는 **먼저 등록된 쪽만**
쓴다. 예외도 경고도 없다. 첫 적발이 이거다.

    app/api/ops.py:2677  @router.get("/ops/codex-usage")  async def get_codex_cli_usage
    app/api/ops.py:2877  @router.get("/ops/codex-usage")  async def get_codex_usage

뒤엣것(마스킹 + 30초 캐시)은 등록조차 되지 않는다. 세 겹으로 안 보였다.

- **HTTP 로 안 보인다.** 앞엣것이 200 을 돌려주므로 호출하면 잘 된다.
- **ruff F811 이 안 잡는다.** 함수 이름이 달라서 재정의가 아니다.
- **`DOUBLE_MOUNT` 가 안 잡는다.** 그 규칙은 네임스페이스 소유 모듈이 2개
  이상일 때만 돌고 `exact_conflicts` 도 서로 다른 모듈일 때만 센다.
  한 모듈이 혼자 중복을 만들면 소유자가 1개라 검사 대상에서 아예 빠진다.

엔트리포인트별로 센다 — `app/main.py` 와 `app/yeoljeong_main.py` 는 서로 다른
ASGI 앱이라 같은 경로를 가져도 충돌이 아니다. 전체 880개 라우트 중 경로가
겹치는 76쌍이 바로 이 경우였고, 엔트리포인트를 안 나누면 전부 오탐이 된다.

승자 판정은 등록 순서에 달렸으므로 `parse_router_module` 이 라우트를 lineno 로
정렬한다. `ast.walk` 는 너비 우선이라 소스 순서를 보장하지 않는다 — 정렬하지
않으면 "어느 쪽이 죽었나" 를 거꾸로 말할 수 있다.
