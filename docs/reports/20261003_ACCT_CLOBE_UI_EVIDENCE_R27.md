# ACCT-CLOBE-UI-EVIDENCE-UNBLOCK-20261003-R27 — 클로브 UI 승인 후보 화면증거 검증

작성 2026-10-03 · 원 job `runner-c856e5ff` · 후보 commit `1786e480139049b2642df3bfe3006fa39c373f85`

## 결론 (먼저)

| 항목 | 실제 상태 |
|---|---|
| 후보 SHA 의 격리 프리뷰 화면 검증 (합성 API) | **수행·통과 47/47** (데스크톱 1440·모바일 390, 만료·네트워크·권한·재승인·비로그인) |
| API 계약 대조 (UI 호출 ↔ 후보 SHA 라우터) | **수행·일치** (미일치 0) |
| 운영 origin 읽기 전용 probe | **수행** — 후보 UI 는 운영에 **아직 없음**(js 404, index 에 clobe 없음) |
| 인증 후 운영 화면 검증 (실로그인·실데이터) | **미수행·미완료** — 승인된 비라일론 계정이 Vault 에 없음 (아래 "막힌 지점") |
| 원 job 승인 게이트 | **여전히 차단** `screen_e2e_evidence_required` (이 작업은 `e2e_evidence` 를 쓰지 않았다) |
| 푸시된 SHA · 큐 등록 · 운영 배포 · 전체 자료 이전 | **모두 미수행** (이 작업의 범위 아님. 부모 승인으로 배포됐다고 보지 말 것) |

**게이트를 통과시키지 않은 이유.** `e2e_verify` 의 증거 스키마(`aads.e2e_verify.v1`)에는 후보 SHA·합성 여부·로그인 방식을 담는 필드가 없고, 게이트는 `passed/dom/screenshot` 만 본다. 합성 API 프리뷰를 그 경로로 기록하면 "실로그인 운영 화면이 검증됨"으로 읽히는 통과 행이 남는다 — 지시서가 금지한 합성 증거의 재사용이다. 또 로그인 화면만 보는 selector 로 `e2e_verify` 를 돌리면 타사이트 로그인 페이지 재사용과 같은 성격이다. 게이트 규칙·instruction·`task_logs` 는 건드리지 않았다.

## STEP 0 — 기존 구현 분류

| 대상 | 분류 | 비고 |
|---|---|---|
| `app/services/e2e_verify.py` (`run_e2e_verify`, `assert_screen_evidence_gate`, defer 경로) | 유지 | 읽기 전용으로 게이트만 호출. 변경 없음 |
| `app/api/pipeline_runner.py` approve (`POST /pipeline/jobs/{job_id}/approve`) | 유지 | 직접 호출·상태 UPDATE 없음 |
| 후보 6개 변경 (HANDOVER.md, index.html, clobe-collection.css/js, sw.js, 정적 테스트) | 유지 | 원 job·commit·산출물 그대로 보존, 수정·삭제 없음 |
| `app/api/obys_collections.py`, `clobe_integration.py` | 유지 | 계약 대조용 읽기만 |
| `scripts/verify_obys_clobe_candidate_e2e_r27.py` | **신규** | 후보 SHA 격리 프리뷰 + 계약 대조 + 운영 probe + 게이트 읽기 |
| 본 보고서 | **신규** | |
| 삭제 | 없음 | |

변경 파일은 TARGET_FILES 두 개뿐이다.

## 사전 확인 (읽기 전용)

- `runner-c856e5ff` = `awaiting_approval`, commit_hash = 후보 SHA, `actual_changed_files` 6개. 반려·리셋·삭제하지 않음.
- 동일 TASK_ID 중복: 없음(본 job `runner-6b721444` 하나). `runner-ff043498`(R25), `runner-6eee061d`(R26)는 `queued` 그대로. R25/R26 체인 복제 없음.
- 실행 중인 배포 job: 없음(running 은 대시보드 UI·GO100 작업 등 무관 건). 충돌 대상 직접 수정 없음.
- workspace ledger 는 이 환경에서 조회 수단을 찾지 못해 **재조회하지 못했다**(미수행). 대신 저장소 `git status` clean, pipeline_jobs 활성 목록으로 대체 확인.
- 후보 commit `1786e480` 은 이 worktree HEAD 의 조상이 아니고 로컬 브랜치에도 포함돼 있지 않다 → **pushedSHA 확인 불가**. 후속 릴리스는 정확한 pushedSHA 를 먼저 확정해야 한다.

## 검증 결과

실행: `timeout 500 /root/aads/aads-server/.venv/bin/python scripts/verify_obys_clobe_candidate_e2e_r27.py` → `TOTAL 47 PASS 47 FAIL 0`. 증거 JSON `/tmp/r27_clobe_preview/evidence.json`, 캡처 17장 `/tmp/r27_clobe_preview/shots/*.png`(sha256 기록, 저장소 밖).

### 증거 출처 구분 (provenance)

| 필드 | 값 |
|---|---|
| 후보 SHA | `1786e480139049b2642df3bfe3006fa39c373f85` (`git archive` 로 추출, 워크트리 생성 없음) |
| URL | `http://127.0.0.1:<임의포트>/static/apps/obys/index.html?legacy=1&view=legacy` — 격리 loopback, **운영 origin 아님** |
| 로그인 | **시뮬레이션**(localStorage 에 가짜 세션). 자격증명 미사용, 정식 인증 흐름 **미실행** |
| API | **합성**(실제 금융 데이터·토큰 없음). 외부 호스트 요청은 전부 차단 |
| 인증 정책 | Vault 조회·복호화·사용 0, 운영 로그인 0, 수집 실행·원장 확정·외부 쓰기 0 (프리뷰의 쓰기 호출 0건을 assert) |
| 운영 증거 여부 | **아님**. 후보 화면 근거일 뿐이다 |

### 계약 대조

UI 가 부르는 경로·메서드(`GET /status`, `GET /companies/{id}/runs`, `POST /companies/{id}/runs`, `GET /businesses/{id}/items`, `POST …/items/review`, `POST …/items/confirm`, `POST /integrations/clobe/reauth`, 관리자 경로)가 후보 SHA 의 `obys_collections.py`·`clobe_integration.py` 라우터에 전부 존재한다. 요청 본문 필드(`period_start/period_end/mode/kinds`, `item_ids/decision`)도 Pydantic 모델과 일치.

### 운영 probe (인증 없음, GET 만)

- `fb.newtalk.kr/static/apps/obys/index.html` 200, 단 `clobe-collection` 문자열 없음 → 후보 미배포.
- `…/modules/clobe-collection.js` 404.
- `…/api/v1/obys-collections/status` 와 존재하지 않는 `…/zz-not-a-route` 가 **둘 다 401** → 인증 미들웨어가 먼저 응답하므로 **401 은 라우트 배포 증거가 아니다.** 카페24 쪽 collection API·마이그레이션 적용 여부는 여전히 미확인.

### 프리뷰 화면 (합성)

- 자료현황(수집 상태·마지막 수집·받은 기간·부분 수신·오류·재시도) → 거래 검토함(5건·상세 서랍) → 경영요약(확정 건만 합산, 검토 중 건·현금/카드 제외 안내, 미수집 종류 경고), 데스크톱·모바일 가로 넘침 0, JS 예외 0.
- 복구: 세션 만료(401), 네트워크 끊김, 연결 승인 만료, 연결된 회사 없음 — 데스크톱·모바일에서 원인+복구 안내 표시. viewer 는 수집 실행·승인·확정 버튼 없음. 비로그인은 클로브 화면 4개 모두 비노출·API 호출 0.
- 합성 서버는 레거시 셸의 다른 API 를 404 로 두므로 데스크톱 캡처 상단의 "서버 응답 오류 (404)" 배지는 프리뷰 산물이며 후보 UI 결함이 아니다.

### 관찰 (후보 UI 는 수정하지 않음)

- **Escape 키로 상세 서랍이 닫히지 않는다**(닫기 버튼만 동작; 데스크톱·모바일 동일). 서랍이 열린 동안 상단 탭이 가려진다. 접근성 개선 후보 — 별도 작업으로 분리.
- 검증하지 못한 것: 확정·승인 쓰기 흐름(운영금지로 의도적 제외), 실제 API 응답 형상(합성), 실 인증/권한 매핑, 운영 서빙 경로.

## 막힌 지점 — 인증 후 운영 화면 (정확한 권한 항목)

`fb.newtalk.kr` 에 매칭되는 Vault 항목(메타데이터만 조회, 비밀 미열람):

1. agent_vault — CEO 소유 구글 비밀번호 관리자 가져오기 항목, work_key `aads-ceo-browser`, 정책 `ask`. 사람 계정이며 건별 승인 대상 → **사용하지 않음**(broad 보류승인을 ask 건별승인으로 치환하지 않음).
2. e2e_credentials — 라벨이 **라일론** 운영 E2E 계정 → 라일론 자료 금지 지시에 따라 **사용하지 않음**.

승인된 비라일론 테스트 계정은 없다. 필요한 것:

- 라일론·회사미지정 자료가 연결되지 않은 **테스트 전용 계정**을 Vault(agent_vault)에 origin `https://fb.newtalk.kr`, 전용 work_key, **정책 `allow`**(또는 CEO 의 건별 `ask` 승인)로 등록 — 등록은 CEO/관리자가 Vault UI 에서 직접(비밀은 이 세션·LLM 에 전달하지 않음).
- 후보가 운영 진입에 **배포된 뒤**(아니면 실제 API 를 붙인 격리 preview 를 CEO 가 별도 승인) `e2e_verify` 를 `job_id=runner-c856e5ff`, 그 work_key, 렌더 결과를 확인하는 selector(예: `#clobeOverviewView .clobe-kind-card`, `#clobeReviewView .clobe-item`, `#clobeSummaryView .clobe-metrics`)로 실행. 정적으로 항상 존재하는 `#clobe*View` 만 assert 하는 것은 증거가 아니다.

## 다음 승인 API (직접 상태 UPDATE·강제 approve 금지)

원 job 승인은 `POST /api/v1/pipeline/jobs/runner-c856e5ff/approve` 로, 두 길 중 CEO 가 택한다.

- **A. 증거 후 승인**: 위 자격 준비 → 후보 화면에서 `e2e_verify` 통과 → `{"action":"approve"}`. 게이트가 `e2e_evidence` 를 읽고 통과시킨다. 현재는 `screen_e2e_evidence_required` 로 400.
- **B. 기존 CEO 명시 defer 경로**(코드에 이미 있는 정식 경로, 기본 fail-closed): `{"action":"approve","defer_screen_evidence":true,"defer_reason":"<10자 이상 사유>"}`. 배포 후 60분 안에 통과 증거가 없으면 overdue 경보. 이 경로는 화면 증거를 **면제하는 것이 아니라 미루는 것**이며, 이번 작업은 호출하지 않았다.

재승인 가능 여부를 게이트로 읽기 확인한 결과(이 스크립트가 재현): `blocked:screen_e2e_evidence_required`, passing evidence 없음, deferral 기록 없음.

## 후속 릴리스 (이 작업에서 하지 않음)

운영 반영은 정확한 pushedSHA, 카페24 DB 마이그레이션 `migrations/20261003_obys_clobe_collection.sql` 적용 상태, 실제 서빙 entry route(현재 로그인 기본 진입은 V4.1, 새 화면은 `legacy=1` 경유), 승인된 ops 비동기 큐 순서로 진행. AADS API 릴리스가 따로 필요하면 AGENTS.md 블루/그린 계약(image 1회·`--no-build`·candidate health 후 짧은 nginx lock·DB ownership fence·drain 후 same digest standby·routed 실패 rollback·300초 P0/P1)을 따른다. 전체 compose·active 직접 restart·`--no-verify` 금지.

## 비용

미측정.
