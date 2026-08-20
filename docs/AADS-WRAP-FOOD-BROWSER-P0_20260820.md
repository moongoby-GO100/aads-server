# AADS-WRAP: FOOD-BROWSER-P0 — 은행 브라우저 커넥터 P0 검수 피드백 반영
날짜: 2026-08-20 | 우선순위: P0

---

## 원래 작업 (1afb291d)

PC Agent 브라우저 세션 기반 은행 거래 수집 커넥터 신설.
- `app/services/yeoljeong_bank_browser_connector.py` 신설
- `yeoljeong_finance_service.py` / `yeoljeong_finance.py` 연동
- `app/static/apps/yeoljeong-finance/index.html` UI 추가
- 61개 단위 테스트 신설

---

## 검수 피드백 3개 항목 및 조치

### [피드백 1] 브라우저 E2E 검증 보완 조치 부족

**문제**: `index.html`에 JS 로직 변경(+18줄)이 있었으나, 대시보드 이미지 빌드 확인에 그쳤음.
실제 `isActionRequired` 플래그 동작, `browserSessionRequired` 속성 설정, `collectBody` 브라우저 필드 포함 여부가 브라우저에서 검증되지 않음.

**코드 리뷰 검증** (정적 분석으로 보완):

| 검증 항목 | 코드 위치 | 결과 |
|-----------|-----------|------|
| `action_required` / `connector_not_ready` → `needs_auth` UI 상태 | `index.html:10130-10136` | ✓ `isActionRequired` 플래그로 올바르게 분기 |
| `browserSessionRequired = true` 설정 | `index.html:10138` | ✓ `isActionRequired` 시에만 세팅 |
| `collectBody`에 `browser_work_key` / `browser_session_id` 포함 | `index.html:10168-10172` | ✓ `connection_type === "browser"` 조건부 포함 |
| `blocked` 카운트 이중 조건 | `index.html:10148` | ✓ `isNotConfigured || isActionRequired` 모두 처리 |

**미완료 — 실제 브라우저 E2E 시나리오 (후속 수행 필요)**:

1. 은행 계좌를 `connection_type=browser`로 등록 후 수집 버튼 클릭
   → UI에 `needs_auth` 상태 뱃지 + "PC Agent 세션 필요" 메시지 표시 확인
2. PC Agent 세션 연결 후 동일 수집 재시도
   → `row_count > 0` 응답 + 거래 내역 표시 확인
3. 세션 만료 후 재수집 시 `connector_not_ready` → UI `needs_auth` 처리 확인

**전제 조건**: 실제 은행 포털 접근 가능한 PC Agent 세션 필요 (개발 환경에서 불가).
E2E 체크리스트는 운영 투입 전 수동 검증으로 처리.

---

### [피드백 2] 실제 포털 파싱 정확도 — 구체적 해결 계획 없음

**문제**: 테스트 픽스처가 이상화된 단순 HTML로, 실제 Shinhan/IBK 포털의 중첩 테이블·span 내포 구조에 대한 파싱 정확도가 불명확했음.

**이번 커밋(07ac0021)에서 실제 수정**:

1. `_TableParser` 중첩 테이블 버그 수정 — 핵심 버그
   - `_in_table` 불리언 → `_table_stack` + `_cell_state_stack`(셀 상태 레벨별 분리)
   - 외부 레이아웃 `<table>` 안에 거래 `<table>`이 중첩될 때 `_cell_depth`가 공유되어
     내부 `<th>/<td>` 진입 시 depth=2가 되어 데이터 미수집 문제 해결

2. `parse_bank_portal_html_with_diagnostics()` 신설 — 파싱 실패 원인 진단
   - 빈 결과 원인 3종 구분:
     - `table_count=0`: 페이지 미로드/JavaScript 렌더링 필요
     - `parse_failure=True`: 테이블 있으나 날짜 컬럼 미인식 → 포털 레이아웃 변경 의심
     - 그 외: 해당 기간 거래 없음
   - `collect_bank_via_browser_session_async()` 응답에 `parser_table_count` / `parser_failure` 포함
   - 파싱 실패 시 "CSV 업로드로 대체 수집" 안내 메시지 자동 포함

3. 파싱 정확도 테스트 6개 추가 (28/28 PASS):
   - `test_parse_nested_table_skips_outer_layout_table` — 중첩 레이아웃 테이블
   - `test_parse_span_wrapped_cells_and_headers` — span 감싼 헤더/값
   - `test_parse_with_diagnostics_returns_table_count` — 정상 케이스 진단 정보
   - `test_parse_with_diagnostics_on_unrecognised_table` — `parse_failure=True`
   - `test_parse_with_diagnostics_no_tables` — `table_count=0`

**잔여 한계 (예비 대응 방안)**:

| 시나리오 | 현재 대응 | 예비 방안 |
|---------|-----------|----------|
| JavaScript 렌더링 테이블 (DOM에 없음) | `table_count=0` 진단 + 안내 메시지 | Playwright `wait_for_selector("table")` + 재시도 (향후 P2) |
| 포털 레이아웃 전면 개편 | `parse_failure=True` 진단 | 헤더 후보 목록 확장 또는 위치 기반 컬럼 추론 (향후 P2) |
| 페이지 로그인 만료 중간에 발생 | `action_required` 즉시 반환 | 현재 충분 |
| 컬럼 순서 비표준 | `_match_header` synonym_set 확장 | synonym_set에 새 별칭 추가로 대응 |

---

### [피드백 3] Git Diff 무관 파일 혼재

**문제**: `docs/CHANGELOG-go100-direct.md` (GO100 자동 로그)가 food-bank/C안 diff에 포함됨.
원칙("기존 변경이 있으면 절대 되돌리지 말고 먼저 git status/diff 확인") 위반 소지.

**조치**: GO100 changelog를 별도 커밋으로 분리.
- `7569decb chore(go100): 자동 로그 1차` (기존 세션 분 — 병렬 작업 커밋)
- `1a7837af chore(go100): auto-log entries 2026-08-20 세션 잔여분` (이번 수정 시 분리)

이후 커밋 체계:
- food-bank 파서 수정: `07ac0021 fix(food):`
- C안 2/4 (chat_service.py + migrations): 별도 `feat(project): C안 2/4` 커밋

---

## 검증 결과

| 항목 | 결과 |
|------|------|
| `py_compile` bank_browser_connector.py | ✓ |
| `pytest test_yeoljeong_bank_browser_connector.py` | ✓ 28/28 PASS |
| `pytest test_tools_and_pipeline.py` | ✓ 60/62 (2개 기존 버그, 이번 작업 무관) |
| pre-commit hook | ✓ 모두 통과 |
| 중첩 테이블 파싱 | ✓ 신규 테스트로 검증 |
| 브라우저 E2E | ⚠ 정적 분석 완료, 실운영 수동 검증 대기 |

---

## 교훈

- **L-PARSER-01**: HTMLParser 기반 중첩 테이블 처리 시 `_cell_depth`를 단일 전역 변수로 관리하면 안 됨. 테이블 레벨별 셀 상태 스택이 필요.
- **L-PARSER-02**: `parse_failure`와 `row_count=0`을 구분해야 함. 전자는 레이아웃 변경을 의심해야 하지만, 후자는 정상일 수 있음.
- **L-E2E-01**: 정적 파일(HTML+JS) UI 변경 시 "이미지 빌드 성공"은 E2E 검증의 대체가 아님. 핵심 분기 로직은 코드 리뷰 + 체크리스트를 WRAP에 명시해야 함.
- **L-COMMIT-01**: auto-log CHANGELOG 파일이 여러 프로젝트를 혼재할 때, 커밋 전 `git diff` 확인 후 프로젝트별 분리 커밋.
