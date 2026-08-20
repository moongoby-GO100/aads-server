# AADS-WRAP: FOOD-BROWSER-P0 — 은행 브라우저 커넥터 P0 검수 피드백 반영
날짜: 2026-08-20 | 우선순위: P0

---

## 커밋 목록 (작업 범위)

| # | Hash | 내용 |
|---|------|------|
| 1 | `1afb291d` | feat(food): bank browser connector P0 — PC Agent session-based transaction collection |
| 2 | `07ac0021` | fix(food): bank portal HTML parser robustness + parse diagnostics |
| 3 | `1a7837af` | chore(go100): auto-log entries 2026-08-20 세션 잔여분 (분리 커밋) |
| WRAP | `fc763bd1` | docs(wrap): FOOD-BROWSER-P0 검수 피드백 반영 WRAP 작성 |

---

## 변경 파일 목록

### 커밋 1 (`1afb291d`) — P0 신설
| 파일 | 변경 |
|------|------|
| `app/services/yeoljeong_bank_browser_connector.py` | 신설 (+367줄): 브라우저 커넥터 핵심 모듈 |
| `app/services/yeoljeong_finance_service.py` | 수정 (+190/-18): browser 연결 타입 라우팅 |
| `app/api/yeoljeong_finance.py` | 수정 (+4): browser_session_id/browser_work_key 페이로드 필드 |
| `app/static/apps/yeoljeong-finance/index.html` | 수정 (+28/-0): isActionRequired UI 분기 |
| `deploy.sh` | 수정 (+57/-1): 다운타임 측정 프로브 |
| `tests/unit/test_bank_browser_connector.py` | 신설 (+468줄): 38개 단위 테스트 |
| `tests/unit/test_yeoljeong_bank_browser_connector.py` | 신설 (+589줄): 초기 22개 테스트 |

### 커밋 2 (`07ac0021`) — 파서 강화
| 파일 | 변경 |
|------|------|
| `app/services/yeoljeong_bank_browser_connector.py` | 수정 (+120/-30): 중첩 테이블 버그 수정 + diagnostics 신설 |
| `tests/unit/test_yeoljeong_bank_browser_connector.py` | 수정 (+111): 6개 테스트 추가 → 28개 |

---

## 테스트 결과

| 테스트 파일 | 통과 | 실패 | 비고 |
|------------|------|------|------|
| `test_bank_browser_connector.py` | 38 | 0 | 커밋 1에서 신설 |
| `test_yeoljeong_bank_browser_connector.py` | 28 | 0 | 커밋 1 신설 + 커밋 2에서 6개 추가 |
| `test_tools_and_pipeline.py` | 62 | 0 | 기존 테스트 회귀 없음 |
| **합계** | **128** | **0** | |

> **WRAP 1차 오류 수정**: WRAP 초안에 `test_tools_and_pipeline.py 60/62 (기존 버그)` 라고 기재했으나, 실제 실행 결과 62/62 전체 통과 확인. 초안 오기재 정정.

---

## 실제 운영 은행 접속 수행 여부 (명시)

**미수행 — 모든 검증은 테스트 더블(mock/stub) 기반**

| 검증 방식 | 수행 여부 | 비고 |
|-----------|----------|------|
| 단위 테스트 (mock PC Agent 세션) | ✓ 수행 | 128개 PASS |
| 정적 코드 리뷰 (index.html JS 분기) | ✓ 수행 | 하단 E2E 체크리스트 참조 |
| **실제 Shinhan 포털 접속** | **✗ 미수행** | PC Agent 세션 + 운영 계정 필요 |
| **실제 IBK 포털 접속** | **✗ 미수행** | 동일 |
| **운영 환경 E2E 브라우저 시나리오** | **✗ 미수행** | 개발 환경에서 불가 |

운영 투입 전 수동 E2E 체크리스트:
1. 계좌를 `connection_type=browser`로 등록 → 수집 버튼 클릭 → `needs_auth` UI 뱃지 + "PC Agent 세션 필요" 메시지 표시 확인
2. PC Agent 세션 연결 후 동일 수집 재시도 → `row_count > 0` + 거래 내역 표시 확인
3. 세션 만료 후 재수집 → `connector_not_ready` → UI `needs_auth` 처리 확인

---

## 배포 / 푸시 상태

| 항목 | 상태 |
|------|------|
| `git push` (원격 반영) | **미수행** |
| `docker compose up -d` (서버 재기동) | **미수행** |
| blue/green deploy | **미수행** |

> 이번 작업은 로컬 커밋 4개까지만 완료. 원격 푸시 및 배포는 CEO 승인 후 수행.

---

## 미완료 항목

| 항목 | 이유 | 후속 처리 |
|------|------|----------|
| 실제 운영 은행 포털 E2E 검증 | 개발 환경에서 PC Agent 세션 + 운영 계정 접근 불가 | 운영 투입 전 수동 검증 필수 |
| JavaScript 렌더링 테이블 대응 | Playwright `wait_for_selector` 미구현 | P2 별도 작업으로 분리 |
| 포털 레이아웃 전면 개편 대응 | 위치 기반 컬럼 추론 미구현 | `parse_failure=True` 진단 + CSV 업로드 대체 안내로 임시 대응 |
| C안 2/4 커밋 | chat_service.py + migration 미커밋 상태 | 별도 feat(project) 커밋으로 분리 예정 |

---

## 작업 범위 외 미커밋 파일 현황 (git status)

현재 working tree에 이번 FOOD-BROWSER-P0 작업과 무관한 변경분이 잔존:

| 파일 | 성격 | 처리 방향 |
|------|------|----------|
| `app/services/chat_service.py` | C안 2/4 (프로젝트 키 자동 파생) 미커밋 | `feat(project): C안 2/4` 별도 커밋 |
| `docs/CHANGELOG-direct-edit.md` | auto-log 미커밋 | 별도 chore 커밋 |
| `docs/CHANGELOG-go100-direct.md` | GO100 auto-log 미커밋 | 별도 chore 커밋 |
| `.deploy_downtime` | 배포 측정 임시 파일 | `.gitignore` 추가 또는 삭제 |
| `migrations/124_chat_workspaces_project_key.sql` | C안 2/4 마이그레이션 | `feat(project): C안 2/4` 커밋에 포함 |
| `migrations/125_project_label_normalization.sql` | C안 마이그레이션 | 동일 |

이 파일들은 FOOD-BROWSER-P0 커밋에 포함되지 않음 — 위 변경 파일 목록과 분리 완료.

---

## 원래 지시사항 대비 반영 확인

### 원래 검수 피드백 3개 항목

| 피드백 | 원래 지시 | 이번 반영 | 상태 |
|--------|----------|----------|------|
| 브라우저 E2E 검증 보완 | 실제 UI 동작 확인 | 정적 코드 리뷰 + 수동 체크리스트 작성 (실운영 수행 불가) | ⚠ 부분 완료 |
| 포털 파싱 정확도 — 구체적 계획 | 중첩 테이블·span 처리 | `_table_stack` 버그 수정 + diagnostics 신설 + 테스트 6개 추가 | ✓ 완료 |
| Git Diff 무관 파일 혼재 | GO100 changelog 분리 | `1a7837af chore(go100)` 별도 커밋으로 분리 | ✓ 완료 |

---

## 검증 결과 요약

| 항목 | 결과 |
|------|------|
| `py_compile` bank_browser_connector.py | ✓ |
| `test_bank_browser_connector.py` | ✓ 38/38 PASS |
| `test_yeoljeong_bank_browser_connector.py` | ✓ 28/28 PASS |
| `test_tools_and_pipeline.py` | ✓ 62/62 PASS |
| pre-commit hook (5단계) | ✓ 모두 통과 |
| 중첩 테이블 파싱 | ✓ 신규 테스트로 검증 |
| 브라우저 E2E (실운영) | ✗ 미수행 — 수동 검증 대기 |
| git push | ✗ 미수행 |
| 배포 | ✗ 미수행 |

---

## 교훈

- **L-PARSER-01**: HTMLParser 기반 중첩 테이블 처리 시 `_cell_depth`를 단일 전역 변수로 관리하면 안 됨. 테이블 레벨별 셀 상태 스택(`_cell_state_stack`)이 필요.
- **L-PARSER-02**: `parse_failure`와 `row_count=0`을 구분해야 함. 전자는 레이아웃 변경 의심, 후자는 정상일 수 있음.
- **L-E2E-01**: 정적 파일(HTML+JS) UI 변경 시 "이미지 빌드 성공"은 E2E 검증 대체가 아님. 핵심 분기 로직은 코드 리뷰 + 체크리스트를 WRAP에 명시해야 함.
- **L-COMMIT-01**: auto-log CHANGELOG 파일이 여러 프로젝트를 혼재할 때, 커밋 전 `git diff` 확인 후 프로젝트별 분리 커밋.
- **L-WRAP-01**: WRAP 파일에는 변경 파일 목록·테스트 통과/실패 수·커밋 hash·배포 미수행 여부·미완료 항목을 반드시 명시. 초안 작성 후 숫자는 실제 실행으로 재확인.
