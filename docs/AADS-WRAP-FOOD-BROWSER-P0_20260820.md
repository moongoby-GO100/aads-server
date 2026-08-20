# AADS-WRAP: FOOD-BROWSER-P0 — 은행 브라우저 커넥터 P0 검수 피드백 3차 반영
날짜: 2026-08-20 | 우선순위: P0

---

## 커밋 목록 (전체, 작업 범위)

| # | Hash | 내용 |
|---|------|------|
| 1 | `1afb291d` | feat(food): bank browser connector P0 — PC Agent session-based transaction collection |
| 2 | `07ac0021` | fix(food): bank portal HTML parser robustness + parse diagnostics |
| 3 | `1a7837af` | chore(go100): auto-log entries 2026-08-20 세션 잔여분 (분리 커밋) |
| WRAP-1 | `fc763bd1` | docs(wrap): FOOD-BROWSER-P0 검수 피드백 반영 WRAP 작성 |
| WRAP-2 | `249c6c8b` | docs(wrap): FOOD-BROWSER-P0 WRAP 검수 피드백 8개 항목 반영 |

> 작업 커밋 3개 (`1afb291d`, `07ac0021`, `1a7837af`) + WRAP 커밋 2개 = 총 5개

---

## 변경 파일 목록 (전체 통합 표)

| 파일 | 커밋 | 변경 | 비고 |
|------|------|------|------|
| `app/services/yeoljeong_bank_browser_connector.py` | 1 (신설) + 2 (수정) | +367 → +120/-30 | 브라우저 커넥터 핵심 모듈 |
| `app/services/yeoljeong_finance_service.py` | 1 | +190/-18 | browser 연결 타입 라우팅 추가 |
| `app/api/yeoljeong_finance.py` | 1 | +4 | browser_session_id/browser_work_key 페이로드 |
| `app/static/apps/yeoljeong-finance/index.html` | 1 | +49/-5 | 매장비서 정적 UI: isActionRequired 분기 + collectBody 브라우저 필드 |
| `deploy.sh` | 1 | +57/-1 | 다운타임 측정 프로브 |
| `tests/unit/test_bank_browser_connector.py` | 1 (신설) | +468 | 38개 단위 테스트 |
| `tests/unit/test_yeoljeong_bank_browser_connector.py` | 1 (신설) + 2 (수정) | +589 → +111 | 22→28개 (6개 추가) |

### 커밋 2 단독 변경 파일

| 파일 | 변경 | 비고 |
|------|------|------|
| `app/services/yeoljeong_bank_browser_connector.py` | +120/-30 | 중첩 테이블 버그 + diagnostics |
| `tests/unit/test_yeoljeong_bank_browser_connector.py` | +111 | 6개 테스트 추가 (22→28) |

---

## 테스트 결과 (실행 기준, 2026-08-20 실측)

### 파일별 단독 실행

| 테스트 파일 | 총 테스트 | 통과 | 실패 | 비고 |
|------------|----------|------|------|------|
| `test_bank_browser_connector.py` | 38 | 38 | 0 | 커밋 1 신설 |
| `test_yeoljeong_bank_browser_connector.py` | 28 | 28 | 0 | 커밋 1 신설 + 커밋 2에서 6개 추가 |
| `test_tools_and_pipeline.py` | 62 | 62 | 0 | 기존 회귀 없음 |
| **단독 실행 합계** | **128** | **128** | **0** | |

### 3파일 동시 실행 (`pytest test_bank_... test_yeoljeong_... test_tools_...`)

| 항목 | 수치 |
|------|------|
| 통과 | 128 |
| 실패 | 0 |

> `asyncio.get_event_loop().run_until_complete()` → `asyncio.run()` 으로 수정하여 이벤트 루프 상태 누출 해결.
> 3파일 동시 실행 시에도 128/128 PASS 확인.

---

## 운영 은행 실접속 검증 현황

| 검증 대상 | 방법 | 수행 여부 | 이유 |
|-----------|------|----------|------|
| Shinhan 간편서비스 포털 (실계정 로그인) | PC Agent 세션 + 브라우저 자동화 | **✗ 미수행** | 개발 환경에서 운영 계정 + PC Agent 세션 불가 |
| IBK 빠른서비스 포털 (실계정 로그인) | PC Agent 세션 + 브라우저 자동화 | **✗ 미수행** | 동일 |
| HTML 파서 (실포털 응답 HTML) | 실포털 응답 수집 후 parse_bank_portal_html 적용 | **✗ 미수행** | 실포털 응답 없이 mock HTML로만 검증 |
| mock HTML 파서 (단위 테스트) | test_bank_browser_connector.py | **✓ 수행** | 38개 PASS (신한/IBK 레이아웃 픽스처 포함) |
| isActionRequired 분기 (정적 코드 리뷰) | index.html JS 분기 코드 리뷰 | **✓ 수행** | 하단 매장비서 UI 체크리스트 참조 |
| 운영 E2E (브라우저 연결 타입 계좌 → 수집 → UI 표시) | 실서버 수동 테스트 | **✗ 미수행** | 배포 미완료 |

> **결론**: 모든 실운영 은행 접속 검증은 미수행. 운영 투입 전 수동 E2E 필수.

---

## 원래 지시의 핵심 요구사항 대비 이행 현황

| # | 원래 핵심 요구사항 | 이행 여부 | 근거 |
|---|-----------------|----------|------|
| 1 | PC Agent 세션 기반 브라우저 커넥터 구현 | ✓ 완료 | `yeoljeong_bank_browser_connector.py` 신설 (커밋 1) |
| 2 | **매장비서 정적 UI 연결** (isActionRequired 분기 + browser_work_key/session_id collectBody 포함) | ⚠ 코드 완료 / E2E 미검증 | `index.html` +49/-5 변경 완료; 실서버 E2E는 배포 후 수동 확인 필요 |
| 3 | HTML 파서 강화 (중첩 테이블 버그 수정 + diagnostics) | ✓ 완료 | 커밋 2; `_table_stack` 버그 수정 + 6개 파서 테스트 추가 |
| 4 | 사전 검수 피드백 3개 항목 반영 | ✓ 완료 | GO100 changelog 분리(1a7837af), 파서 정확도 계획 구체화(07ac0021), E2E 체크리스트 명시(fc763bd1) |

### 매장비서 정적 UI 연결 상세 확인

이번 P0 작업에서 `app/static/apps/yeoljeong-finance/index.html`에 다음 3가지를 구현했다:

| 변경 항목 | 코드 근거 | 코드 완료 여부 | 실E2E 여부 |
|-----------|----------|--------------|----------|
| `isActionRequired` 플래그로 `needs_auth` UI 분기 | `item.portalStatus = (isNotConfigured \|\| isActionRequired) ? "needs_auth" : ...` | ✓ | ✗ |
| `browserSessionRequired = true` 설정 (세션 요구 뱃지) | `if (isActionRequired) item.browserSessionRequired = true;` | ✓ | ✗ |
| `collectBody`에 `browser_work_key` / `browser_session_id` 포함 | `if (account.connection_type === "browser") { collectBody.browser_work_key = ... }` | ✓ | ✗ |

수동 E2E 체크리스트 (운영 투입 전 필수):
1. 계좌를 `connection_type=browser`로 등록 → 수집 버튼 → `needs_auth` 뱃지 + "PC Agent 세션 필요" 메시지 표시
2. PC Agent 세션 연결 후 재수집 → `row_count > 0` + 거래 내역 화면 표시
3. 세션 만료 → 재수집 → `connector_not_ready` → `needs_auth` 처리 재확인

---

## 미완료 항목 (이유 및 후속 처리)

| 항목 | 미완료 이유 | 영향 범위 | 후속 처리 |
|------|-----------|----------|----------|
| 실운영 은행 포털 E2E 검증 (Shinhan/IBK) | 개발 환경에서 PC Agent 세션 + 운영 은행 계정 접근 불가 (서버 68에 PC Agent 미연결) | 운영 투입 품질 | 운영 서버 배포 후 CEO/운영자 수동 검증 필수. 실패 시 롤백 기준: `parse_failure=True` 로그 확인. |
| 매장비서 UI 실E2E 검증 | 배포 미완료 상태에서 브라우저 접근 불가 | UI 분기 동작 확인 | 배포 후 위 3단계 체크리스트 수동 수행 |
| JavaScript 렌더링 테이블 대응 (Playwright `wait_for_selector`) | P0 범위 초과 (브라우저 자동화 레이어 추가 필요) | 동적 렌더링 포털 파싱 | P2 별도 작업으로 분리 (`feat(food): playwright portal parser`) |
| 포털 레이아웃 전면 개편 대응 (위치 기반 컬럼 추론) | P0 범위 초과. 현재 컬럼 헤더명 기반 파싱만 지원 | 포털 개편 시 파싱 실패 | `parse_failure=True` + diagnostics 로그 + CSV 업로드 대체 안내로 임시 대응. 실패 탐지 시 P1 즉시 대응. |
| ~~테스트 동시 실행 격리 실패 5건~~ | ~~asyncio.get_event_loop() 누출~~ | ~~해결됨~~ | `asyncio.run()` 으로 수정 → 128/128 PASS |
| C안 2/4 `chat_service.py` + migration 커밋 | FOOD-BROWSER-P0 범위와 분리 원칙 (커밋 혼재 방지) | 프로젝트 키 자동 파생 기능 | 별도 `feat(project): C안 2/4` 커밋으로 분리 예정 |

---

## 작업 범위 외 미커밋 파일 현황 및 처리 방향

현재 working tree에 FOOD-BROWSER-P0 작업과 **무관한** 변경분이 잔존:

| 파일 | 성격 | 상태 | 처리 방향 | 처리 기한 |
|------|------|------|----------|----------|
| `app/services/chat_service.py` | C안 2/4 — 프로젝트 키 자동 파생 | 미커밋 | `feat(project): C안 2/4` 별도 커밋에 포함 | 다음 세션 |
| `docs/CHANGELOG-direct-edit.md` | auto-log 미기록 | 미커밋 | `chore(log): 2026-08-20 세션 changelog` 별도 커밋 | 다음 세션 |
| `docs/CHANGELOG-go100-direct.md` | GO100 auto-log 미기록 | 미커밋 | 동일 chore 커밋 | 다음 세션 |
| `.deploy_downtime` | 배포 측정 임시 파일 (deploy.sh 생성) | 미커밋 | `.gitignore`에 `.deploy_downtime` 추가 | 즉시 (이번 WRAP 커밋) |
| `migrations/124_chat_workspaces_project_key.sql` | C안 2/4 마이그레이션 | 미커밋 | `feat(project): C안 2/4` 커밋에 포함 | 다음 세션 |
| `migrations/125_project_label_normalization.sql` | C안 마이그레이션 | 미커밋 | 동일 | 다음 세션 |

> 위 파일 6개는 FOOD-BROWSER-P0 커밋(1,2,3)에 포함하지 않았음 — 커밋 범위 오염 없음.
> `.deploy_downtime`은 `.gitignore`에 즉시 추가 후 이번 WRAP 커밋에 반영한다.

---

## 배포 / 푸시 상태

| 항목 | 상태 | 비고 |
|------|------|------|
| `git push` (원격 반영) | **✗ 미수행** | CEO 승인 후 수행 |
| `docker exec reload-api.sh` (무중단 코드 리로드) | **✗ 미수행** | 푸시 후 수행 |
| blue/green deploy | **✗ 미수행** | 이미지 리빌드 필요 시 `deploy.sh bluegreen` |

---

## 검증 결과 요약

| 항목 | 결과 |
|------|------|
| `py_compile` bank_browser_connector.py | ✓ |
| `test_bank_browser_connector.py` | ✓ 38/38 (단독 실행) |
| `test_yeoljeong_bank_browser_connector.py` | ✓ 28/28 (단독 실행) |
| `test_tools_and_pipeline.py` | ✓ 62/62 (단독 실행) |
| 3파일 동시 실행 | ✓ 128/128 (asyncio.run() 수정으로 격리 이슈 해결) |
| pre-commit hook (5단계) | ✓ 모두 통과 |
| 중첩 테이블 파싱 (신규 6개 테스트) | ✓ |
| 매장비서 index.html 정적 코드 리뷰 | ✓ (3개 변경 항목 확인) |
| 실운영 은행 포털 E2E | ✗ 미수행 — 배포 + 수동 검증 대기 |
| 매장비서 UI E2E | ✗ 미수행 — 배포 후 수동 체크리스트 필요 |
| git push | ✗ 미수행 |
| 배포 | ✗ 미수행 |

---

## 교훈

- **L-PARSER-01**: HTMLParser 기반 중첩 테이블 처리 시 `_cell_depth`를 단일 전역 변수로 관리하면 안 됨. 테이블 레벨별 셀 상태 스택(`_cell_state_stack`)이 필요.
- **L-PARSER-02**: `parse_failure`와 `row_count=0`을 구분해야 함. 전자는 레이아웃 변경 의심, 후자는 정상일 수 있음.
- **L-E2E-01**: 정적 파일(HTML+JS) UI 변경 시 "코드 완료"는 E2E 검증 대체가 아님. 핵심 분기 로직은 코드 리뷰 + 체크리스트를 WRAP에 명시해야 함.
- **L-COMMIT-01**: auto-log CHANGELOG 파일이 여러 프로젝트를 혼재할 때, 커밋 전 `git diff` 확인 후 프로젝트별 분리 커밋.
- **L-WRAP-01**: WRAP 파일에는 ① 전체 변경 파일 통합 표 ② 테스트 통과/실패 수 (단독+동시 실행 구분) ③ 운영 은행 실접속 여부 전용 표 ④ 모든 커밋 hash (WRAP 커밋 포함) ⑤ 미완료 항목 이유+후속처리 ⑥ 작업 범위 외 파일 처리 방향 ⑦ 원래 핵심 요구사항 대비 이행표를 명시.
- **L-TEST-ISOLATION-01**: `asyncio.get_event_loop().run_until_complete()`는 이벤트 루프를 재사용하여 동시 실행 시 상태 오염 발생. `asyncio.run()`으로 교체하면 매 호출마다 새 루프를 생성해 격리 보장. CI에서는 단독 실행뿐 아니라 관련 테스트 파일을 묶어 동시 실행도 검증.
