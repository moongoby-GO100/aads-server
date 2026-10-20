# AAG V11-5 consumer inventory — phase 2 cutover

- 기준 SHA: `0fdcb4fa616a1c17ef4f94afbfc37cc553b1b686`
- 조사 시각: 2026-09-19 22:50 KST
- 판정: v1 폐기 불가. 이중 운영과 계측을 먼저 배포해야 한다.

| 소비자 | 현재 경로 | phase 1 조치 | 잔여 조치 |
|---|---|---|---|
| 세션 `aag_findings` | `app/services/aag_tools.py` legacy DB/local graph | 운영 기본값을 central v2로 전환, flag off 즉시 v1 rollback | telemetry로 v1 잔존 관측 |
| 세션 `aag_brief` | local `tools/aag/brief.py` | 운영 기본값을 pinned central v2로 전환, flag off 즉시 v1 rollback | telemetry로 v1 잔존 관측 |
| Pipeline Runner | `scripts/pipeline-runner.sh`가 local `brief.py` 직접 실행 | project-scoped credential 전용 `/aag/v2/runner-brief`와 명시적 skip 계약 구현 | 서버별 credential 발급 후 `AAG_V2_RUNNER_ENABLED=1` 전환 |
| CI | 중앙 AAG read API 직접 호출 없음 | 잔여 v1 consumer 아님 | v2 gate가 도입될 때 pinned snapshot/commit 계약 사용 |
| 목표 화면 | AAG read API 직접 호출 없음 | 잔여 v1 consumer 아님 | V11-7 4축 상태 화면에서 v2 aggregate 사용 |
| 외부 HTTP | `/api/v1/aag/findings`, `/projects` 호출자는 코드 검색만으로 확정 불가 | consumer header와 DB telemetry 추가 | 관측 기간 동안 이름 없는 소비자를 식별 |

V11-2 초기 배포 뒤 authoritative observation 3건이 존재했지만 latest pointer가 0건인
운영 공백이 확인됐다. V11-5 migration은 최신 verified authoritative observation만
pointer/ref-head로 백필하고, hourly pusher는 advisory transaction lock 안에서 observation과
pointer를 함께 기록한다. commit mismatch와 out-of-order 실행은 pointer를 갱신하지 않는다.

## Rollback

1. `AAG_V2_CONSUMERS_ENABLED=0`으로 세션 도구를 기존 v1 경로로 즉시 되돌린다.
2. `AAG_V2_ENABLED=0`으로 v2 HTTP 접근을 닫는다.
3. v1 route와 `aag_graph_snapshots`는 삭제·변경하지 않는다.
4. telemetry와 v2 additive table은 감사·재처리 근거로 보존한다.

Pipeline Runner는 `AAG_SCANNER_TOKEN_<PROJECT>`를 사용한다. v2가 켜진 상태에서
credential 또는 authoritative snapshot이 없으면 로컬 그래프로 조용히 강등하지 않고
브리프를 건너뛴다. 운영자가 `AAG_V2_RUNNER_ENABLED=0`으로 명시적으로 되돌린 경우에만
기존 repo/host `brief.py`를 사용한다. 이 구분은 stale local graph를 authoritative
evidence처럼 워커 프롬프트에 넣는 것을 방지한다.

## 완료 게이트

- v2 endpoint의 snapshot-pinned pagination 회귀 통과
- v1/v2 consumer telemetry migration 운영 적용
- 세션 v2 flag on 및 pinned central read 확인
- Pipeline Runner v2 경로·outage/skip 계약 구현, 서버별 credential 발급 및 실제 v2 telemetry 확인
- 관측 기간의 잔여 v1 request 0건
- v1 rollback 리허설 통과 후에만 폐기 승인

## 2026-10-20 갱신 — 잔여 소비자 전환 마무리 (runner-57f6e1d3)

이 절은 코드와 테스트로 확인한 것과 운영에서 확인하지 못한 것을 분리해 적는다.

### 소비자 현황

| 소비자 | 코드 상태 | 운영 상태 | 비고 |
|---|---|---|---|
| 세션 `aag_findings` | v2 경로 + flag off 시 v1 롤백. 빈 결과를 "결함 없음"으로 표현하지 않도록 수정, freshness·limitation 노출 | `aads-server` 컨테이너에 `AAG_V2_ENABLED`·`AAG_V2_CONSUMERS_ENABLED` 설정 확인(값 미기록) | 최근 7일 root 조회에서 v2 세션 브리프(`aag_brief`)와 findings(`aag_findings`) 계측이 모두 있음. 소비자 이름은 세션 도구 한정이며 실제 runner 계측은 없음 |
| 세션 `aag_brief` | 공용 매칭/식별 헬퍼 사용, pinned identity·stale·mismatch·fallback 은 build_brief_v2 가 판정 | 위와 동일 | 실제 `aag_brief=stale/not_proven`, inventory 0/0 이면 성공으로 표현되지 않음 |
| Pipeline Runner | `brief_text`(서버 렌더)만 프롬프트에 투입, fallback_used≠false·비JSON·HTTP 오류는 skip. skip 사유(`project_credential_missing`/`central_v2_unavailable_or_no_snapshot`)를 이벤트에 기록 | project credential 은 **발급됨/미연결**: DB 에 `pipeline-runner-go100-v11-5-staged`(id `1b8e9797-e1d6-47b4-9fcc-441e4001572e`, expires 2026-12-18, revoked NULL)가 있고 staged 파일(`/root/.config/aads-aag-go100.token`, mode 600)이 존재한다. 그러나 `AAG_V2_RUNNER_ENABLED`·`AAG_SCANNER_TOKEN_*` 는 러너 환경에 연결되지 않았고 **실제 runner 의 v2 소비 증거는 없다** | 발급 자체는 완료. 연결(flag 설치·원격 러너 재시작)은 범위 밖이며 우회하지 않음. 토큰 값은 이 문서·커밋에 기록하지 않는다 |
| CI | 중앙 AAG read API 호출 없음 (코드 검색 기준) | — | CI 전환 증거 없음. v2 gate 도입 시 pinned snapshot/commit 계약 사용 |
| 외부 HTTP v1 | `X-AAG-Consumer` 헤더·DB 계측 유지 | 헤더 없는 호출자는 `http-unknown` | 계측 없음은 미사용의 증거가 아니다 |

### 이번에 교정한 결함

1. `GET /aag/v2/latest` 가 `freshness.status="fresh"` 와 `limitation=[]` 를 하드코딩했다. 이제 verified_at 으로 fresh/stale/unknown 을 계산하고, 커버리지 미입증·미해결 참조·commit 불일치를 `limitation` 에 싣는다.
2. runner 는 작업 지시문 전체(최대 5만 자)를 `target` 으로 보내는데, 매칭이 "지시문 전체가 파일 경로의 부분 문자열인가"여서 실제로는 어떤 finding 도 선택되지 않았다. 지시문에서 경로 토큰을 추출해 경로 세그먼트 단위로 매칭한다(`aag.py` ↔ `app/api/aag.py`, `app/api` ↔ 하위 전체, `data_aag.py`·`aag.pyc` 는 불일치). 토큰 추출은 단어 단위·길이 상한으로 정규식 폭주를 막는다.
3. v2 brief 조립 코드가 세 곳에 복제돼 있었다. `aag_brief_v2` 의 공용 헬퍼로 합쳤다.
4. runner 는 `{"brief":…}` JSON 원문을 프롬프트에 넣던 것을 서버가 렌더한 `brief_text` 로 바꿨다.
5. 세션 findings 의 빈 결과 사유 "스냅샷에 결함이 없습니다."를 "필터 조건에 맞는 결함이 없다(무결함의 증거 아님)"으로 바꾸고 헤더에 `freshness`·`proof=not_proven` 을 붙인다.
6. pagination 중 pointer 가 갱신되면 모호한 "no authoritative snapshot" 대신 "restart pagination" 으로 fail-closed 한다.

### 남은 한계

- `load_authoritative_snapshot` 은 latest pointer 만 읽는다. cursor 의 snapshot 이 최신이 아니게 되면 pagination 은 계속되지 않고 재시작해야 한다(안전하지만 불변 스냅샷 직접 조회는 아님).
- runner 가 `AAG_REPOSITORY_ID_<PROJECT>`/`AAG_TARGET_REF_<PROJECT>` 를 설정하지 않으면 프로젝트의 가장 최근 verified pointer 를 쓴다. 다중 저장소 프로젝트는 두 변수를 지정해야 한다. brief 는 선택된 repo/ref/commit 을 출력에 고정한다.
- 매칭은 경로 토큰 기반이다. 테이블명 등 경로가 아닌 식별자는 지시문에 `a/b.c` 형태로 나타나지 않으면 선택되지 않는다.
- 이 문서의 "잔여 v1 request 0건" 완료 게이트는 아직 충족되지 않았다. 측정 기간 시작은 runner v2 가 실제로 켜진 시점이다.

## 2026-10-20 재작업 — 검수 지적 3건과 scanner JWT 선차단 (runner-4ce2b5dd)

원작업 커밋 `9c4258e6`(runner-57f6e1d3)의 diff 를 그대로 재사용하고 아래만 보완했다.

| 구분 | 내용 | 코드 근거 |
|---|---|---|
| 검수 1 | `scripts/pipeline-runner.sh.local` 을 이번 TARGET_FILES 에 명시하고 본 파일과 바이트 동일하게 유지 | `test_project_document_m2` 의 미러 동일성 |
| 검수 2 | `app/`, `./app/api/aag.py`, 저장소 절대경로가 기존 finding 을 찾도록 정규화. `./`·중복 `/` 제거, `..` 포함 토큰 폐기, 절대경로는 이 서버 체크아웃 또는 snapshot 의 `repository_id` 디렉터리 아래일 때만 재기준화. 그 밖의 절대경로(타 저장소·`/etc`)와 저장소 루트 단독은 매칭을 넓히지 않는다. 길이 상한·단어 분할은 유지 | `aag_brief_v2._repo_relative`, `target_tokens`, 호출처 3곳이 `repository_id` 전달 |
| 검수 3 | `render_brief_text` 가 자르기 전에 심각도(P0→P1→P2→기타) 안정 정렬. 생략 줄에 심각도별 건수와 snapshot/ref/commit 을 남기고, 생략분에 P0/P1 이 있으면 전체 조회 필수를 표시 | `aag_brief_v2.render_brief_text` |
| 운영 결함 | JWT 미들웨어가 `X-AAG-Scanner-Token` 요청을 `Bearer 토큰 제공` 401 로 먼저 차단해 `authenticate_scanner` 에 닿지 못했다. `POST /api/v1/aag/v2/runner-brief` 와 `POST /api/v1/aag/v2/snapshots` 두 (메서드, 정확경로) 쌍만 면제한다. `/aag` prefix 면제 없음, 헤더 존재만으로 통과시키지 않으며 누락·오류·만료·폐기·교차 project 는 라우트가 403 으로 거절한다. GET findings/latest/history/brief 와 접미사 변형은 JWT 401 유지 | `app/main.py:_SERVICE_AUTH_EXACT_ROUTES`, `tests/unit/test_aag_scanner_auth_boundary.py`(실제 `app` ASGI 요청) |

### 확인하지 못한 것 (운영 관측 미완료)

- staged credential 이 DB 의 해시와 실제로 일치하는지는 JWT 선차단 때문에 한 번도 검증되지 않았다. "부재"가 아니라 "미검증"이다. 이번 변경은 미들웨어를 통과시킬 뿐 운영 배포 전이라 실서버 응답은 관측하지 않았다.
- v2 runner 활성(flag·환경 연결)과 rollback 리허설의 운영 관측은 이번 범위에서 수행하지 않았다. 완료 게이트의 해당 항목은 그대로 미충족이다.
