# 오비서 V4.1 현재 관리자 화면 — 직원 계약 작성·서명요청 구현 검증 (2026-10-06)

기준 문서: `docs/prd/AADS-LAYOUT-025_obys-admin-contract-prd.md` v1.0.1, `docs/design/AADS-LAYOUT-024_obys-admin-contract-design.md` v1.0.1
(두 문서는 커밋 459dc268 에 있고 아직 origin 에 푸시되지 않았다.)

## 무엇을 바꿨나

| 파일 | 내용 |
|---|---|
| `app/static/apps/obys/modules/contract-core.js` | 기존 관리화면(index.html)의 계약 양식·표준 조항·검증·미리보기 코드를 그대로 옮긴 공용 코어 |
| `app/static/apps/obys/modules/contract-editor-v41.js` / `.css` | 현재 관리자 화면 계약 편집기(대상 확인 → 조건 입력 → 미리보기 → 저장·서명요청·재알림·PDF) |
| `app/static/apps/obys/mockup-v4-1.html` | 모듈 로드, 직원 현황·계약·인사증빙에 mount, 가입 승인 직후 목록 갱신 |
| `tests/unit/test_obys_v41_contract_editor.py` | 배선·기존 코드 일치·서버 validator 연동·누락 항목→입력칸 매핑 |
| `scripts/e2e/obys_v41_contract_e2e.js` | Playwright 모의 API E2E (운영 데이터 미사용) |

서버 API·DB 는 바꾸지 않았다. 기존 `/employees/approved`, `/contracts`, `/contracts/{id}/request-signature`,
`/resend-signature-notice`, `/signed-pdf`, `/signed-pdf/regenerate` 를 쓴다.

## 검증 결과

- 모의 API E2E: `e2e-results.json` — 1440×1000·390×844 각 16항목, 총 32/32 통과.
  승인 직원 → 계약 작성(legacy 이동 없음) → 빈 칸 차단 → 미리보기 → 서버 400 시 입력 보존 → 저장(ID 표시)
  → 서명요청 연속 클릭 1회 → 알림 실패를 성공으로 표시하지 않음 → 재알림 429 안내 → 새로고침 후 문맥 복구
  → 계약·인사증빙에서 서명본 PDF 실파일(%PDF-) 수신 → 가로 넘침 0px → 스크립트 오류 0건.
- 서버 validator 교차검증: 화면이 만든 저장 본문을 운영 컨테이너의 `_validate_contract_payload` 에 넣어 통과,
  임금 0 이면 `400 확정 임금` 으로 거부됨을 확인.
- 단위 테스트: 신규 + 기존 계약·서명·알림·PDF·membership 테스트 184 passed / 3 skipped.

## 이 검증이 아닌 것

- 운영(카페24) 화면에서 실제 직원 계정으로 가입→승인→계약→직원 서명→PDF 를 끝까지 한 검증이 아니다.
- 직원 본인 서명 화면은 기존 서명 링크 경로(index.html?yf_contract_token=)를 그대로 쓴다. 이번 변경 대상이 아니다.
