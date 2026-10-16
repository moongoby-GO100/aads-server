# 계약서 PDF 재작업 결과

document_key: obys-contract-pdf-r2
판본: R4 (2026-10-16), 운영 반영 전 검증

아래 R3 내용은 계승한 이전 세션의 기록이다. 현재 세션 결과는 마지막 R4 RESULT가 우선한다.

정본 미등록(미완료), 열람 링크 미제공. 인증된 등록 API·등록 도구·DB 연결 정보가 이 세션에 없다. 핸드오버 DB 미기록. HANDOVER.md는 수정하지 않았다. 아래 미충족 항목 때문에 전체 완료로 보고하지 않는다.

## RESULT — STEP 0 조사와 적용 범위

시작 HEAD 및 로컬 origin/main은 e68ce1c8. 시작 dirty 파일 없음. git ls-remote origin refs/heads/main은 DNS 실패(exit 128)로 원격 최신 여부를 확인하지 못했다. workspace ledger 읽기도 연결 도구 부재(FileNotFoundError)로 불가하여 활성 runner와 충돌 없음을 보증하지 않는다. 운영 DB·스키마를 변경하지 않았다.

/root/aads/AGENTS.md, repo AGENTS.md, /root/aads/aads-server/logs/runner-instructions/runner-84a003a8.md를 읽었다. 현 worktree의 logs/runner-instructions와 선행 runner-ce240146 지시서·보존 worktree는 존재하지 않았다. 사용자 R3 지시, 보존 커밋 796fa410 및 /root/aads/runner-artifacts/runner-ce240146.patch 존재를 근거로 이어받았다. rejected 작업을 실행 의존성으로 지정하지 않았다. 796fa410의 17개 파일은 현재 main에 없어 파일별 변경을 이어받고 개선했다.

AAG에 지정된 project_document_heads 접점은 app/api/canonical_documents.py 그대로 유지. 새 조항 서비스가 AAG에 없는 이유는 기존 JS 조항 규칙의 Python 이식이기 때문이다. 기존 render_signed_contract_pdf가 직접 사용하며 새 API·마운트·스케줄러·DB 접점은 없다.

| 기존 항목 | 분류 | 조치 |
|---|---|---|
| ContractPdfError, snapshot_sha256, resolve_font_path, _seal_font_picker, _signature_png | 유지 | 봉인·서명·도장 검증 그대로 |
| _text, _mask_email, is_freelancer_snapshot, display_value, _is_zero_amount | 유지 | 기존 표시·마스킹 규칙 사용 |
| term_rows, extra_rows | 유지 | 삭제하지 않고 기존 봉인 기재사항 보완 출력에 재사용 |
| _register_font | 수정(선행 계승) | 실제 Bold 파일이 있을 때만 패밀리 등록; 가짜 굵기 없음 |
| render_signed_contract_pdf, para, table, rows_for | 수정 | 선행 미리보기 구조 계승 + 누락 약정 호환 출력 |
| SealFlowable.__init__/draw | 유지 | 실제 도장 경로 사용 |
| cell, signed_time, NumberedCanvas.__init__/showPage/save | 신규(선행 계승) | 인적사항·KST·쪽수·마지막 쪽 증적 |
| obys_contract_clauses.value/money/korean_date/contract_clauses | 신규(선행 계승 및 수정) | v3/v4 패리티 + 구·신 수습 약정 병합 |
| contractPreviewHtml | 수정 | 구·신 수습 및 특약을 모두 확인, 같은 값 중복 제거 |
| admin.html | 수정 | 실제 로드되는 contract-core 캐시 판본 r3 |
| 엔드포인트·스케줄러·DB 접점 | 유지 | 신규/삭제 없음 |
| 삭제 | 없음 | 삭제 def/class 0, AST 기존 심볼 차집합 0 |

추가 변경 사유: 선행 테스트 3개(types_clauses, freelancer_contract_labels, v41_contract_editor)와 gzip fixture는 선행 동작·캐시 판본 검증을 계승한다. 신규 pdf_preview 테스트는 실제 PDF 추출·봉인 불변·화면 패리티를 검증한다. 같은 기존 보고서 경로와 가상 PNG 7개는 이번 판본 결과로 갱신한다. 별도 날짜 사본은 만들지 않았다.

## RESULT — 누락 원인과 수정

선행 후보는 term_rows/extra_rows를 본문에서 제거하고 probation_period와 terms만 읽어 probation_terms/special_terms가 소실됐다. 수습 구 필드만 있으면 반대 기본 문장까지 출력했다.

- 수습은 probation_period와 probation_terms 원문을 순서대로 보존하고 동일 값만 합친다. legacy 약정이 있으면 알려진 두 가지 자동 생성 '수습기간 없음.' 기본값을 억제한다. 임의 사용자 약정은 덮어쓰지 않는다.
- freelancer/confidentiality도 실제 봉인 수습 약정이 있으면 기존 약정으로 명시한다. 기존 자동 기본값만 있는 용역/서약은 이전 화면을 유지한다.
- 추가 특약은 terms/special_terms 각각의 서로 다른 원문을 보존한다. 화면도 같은 규칙이다.
- 구 렌더러가 출력했던 term_rows/extra_rows 중 새 본문에 표현되지 않은 값은 '기존 봉인 약정 및 기재사항'에 보존한다. 기존 한글 라벨·내부키 제외·개인정보 마스킹 정책을 사용한다. 날짜의 한국어 표시도 비교해 중복을 줄였다.
- snapshot과 hash를 쓰거나 재봉인하지 않는다. 운영 직원·실제 서명본을 읽거나 수정하지 않았다. 계좌 원문 우선 정책은 부모와 동일하며 이 변경의 신규 회귀로 보고하지 않는다.

## RESULT — 실제 검증

호스트 대체 명령 접두사:
`PYTHONPATH=/tmp/obys-rank1-venv/lib/python3.12/site-packages:/root/aads/.testdeps timeout 60 /root/aads/aads-server/.venv/bin/python -m pytest`

| 명령/검사 | exit | 결과 |
|---|---:|---|
| bash scripts/run_unit_tests.sh: notify_pdf, types_clauses, v41_contract_editor, amendment_chain, autofill, pdf_preview | 2 | 기준 컨테이너/이미지 없음, 통과 아님 |
| 호스트: pdf_preview, types_clauses, v41_contract_editor, amendment_chain, autofill, freelancer_contract_labels, obys_company_seal_pdf | 0 | 167 PASS |
| 호스트: notify_pdf 비HTTP | 0 | 9 PASS, 5 deselected |
| 최종 합동 호스트: 위 8개 테스트 파일, 지정 HTTP 5개만 제외 | 0 | **176 PASS, 5 deselected**, 41.59초 |
| 호스트: notify_pdf 지정 HTTP 5개, timeout 25, faulthandler_timeout=10 | 124 | 첫 HTTP 요청에서 대기; 5개 통과 아님 |
| 최소 FastAPI TestClient GET smoke, timeout 15 | 124 | 앱 라우터 없이도 동일 대기 |
| socket.socketpair smoke | 0 | 검사 스크립트 정상 종료, 기능은 PermissionError errno=1로 실패 |
| Node/Python 패리티 | 0 | 기존 56 사례 완전 일치 |
| 이전 fixture 원본과 바이트 비교 | 0 | 52/56 동일, daily/v4 4개만 선행 보험 문구 수정 |
| PDF legacy 회귀 | 0 | 3종 × v3/v4 × 6조합 = 36 사례, 실제 PDF·브라우저용 HTML 원문 존재/반대 기본 문구 부재/해시·입력 불변/결정성 |
| python3 scripts/dup_guard.py 및 --paths 실제 변경 소스 | 0 | 새 중복 없음 |
| python3 -m compileall -q (PDF·조항·신규 테스트) | 0 | PASS |
| git diff --check | 0 | PASS |
| 삭제 def/class 행 및 AST 기존 심볼 차집합 | 0 | 모두 0 |
| 가상 7종 PDF→PNG 생성 및 텍스트 경계/마지막 쪽 증적 | 0 | 각 2쪽, 모든 텍스트 블록 페이지 안, 마지막 쪽 hash 앞 12자 |
| PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright Chromium smoke, timeout 20 | 1 | sandbox_host_linux shutdown Operation not permitted, SIGTRAP |
| Bold 원본 curl, timeout 20 | 6 | raw.githubusercontent.com DNS 실패, 로컬 Bold 미확보 |
| 오류 사전 match | 2 | 접속 불가, 기존 key 조회/보강 미수행 |

초기 합동 검증은 잘못 지정한 test_company_seal_pdf.py 경로로 exit 4(수집 0)였다. 실제 test_obys_company_seal_pdf.py로 바로잡아 위 검증을 실행했다. 중간 실행 137 PASS/2 FAIL은 용역 기본 수습값의 추가 출력으로 인한 패리티/바이트 회귀였다. 기본값과 실제 약정을 구별하도록 수정하고 기존 fixture·기대값을 완화하지 않은 최종 실행에서 해결했다.

지정 HTTP 제외 목록: notification_failure_does_not_fail_signature_request_api, resend_signature_notice_rate_limit_and_guards, pdf_is_bound_to_signed_snapshot_not_current_body, signed_pdf_download_access_and_delivery_record, unsigned_contract_pdf_download_is_conflict. AnyIO blocking portal에서 대기하며 socketpair 자체가 EPERM인 세션 제약을 확인했다. 정상 호스트에서 HTTP 5개 재검증은 남아 있다. 앱 테스트를 skip/완화하거나 운영 서비스·의존성을 수정하지 않았다.

## RESULT — 가상 시각 증거와 남은 항목

기존 docs/reports/계약서-PDF-검토-obys-contract-pdf-{part_time,regular,fixed_term,daily,freelancer,manager,confidentiality}.png 7개를 이번 코드로 다시 생성했다. 전부 가상 직원·가상 상호·example.invalid이며 '운영 반영 전 · 가상 데이터 · PDF만 검토 (브라우저 비교 미완료)' 표시를 넣었다. 정규직 두 쪽을 직접 열어 수습/특약, 사업주 도장, 자필서명, 쪽수와 마지막 쪽 증적을 확인했다. 페이지 밖 텍스트 블록은 없었다.

이 이미지는 PDF 검토 자료이며 실제 admin.html 브라우저 미리보기와 나란히 캡처한 증거가 아니다. Chromium 경로를 지정해 실제 실행했으나 이 세션에서 EPERM으로 실패했다. 단시간·정규직·기간제·일용·프리랜서 실제 브라우저 비교는 미완료다. Bold 자산도 미확보이므로 실제 굵기 일치 검증은 미완료이며 기존 OFL 파일은 유지했다.

정본 초안 등록·열람 참조·DB 핸드오버는 인증 접속 부재로 미완료다. 오류 사전 obys.pdf.legacy-sealed-fields-lost 보강은 조회 불가 및 최종 커밋 부재로 미수행이다. 정상 호스트 HTTP, 화면 비교, Bold, 등록을 통과로 표시하지 않는다.

금지된 commit/push/build/deploy/restart/스키마 변경은 실행하지 않았다. 롤백은 승인 후 최종 변경 커밋 revert이며 운영 미반영을 유지한다.


## R4 RESULT — 자동 재작업 리뷰 1/1

시작 HEAD와 로컬 origin/main: `3ba9aaad5336ad7f4b4e3fddfe3c2ffd4d9583c2`. 시작 dirty 없음. `git ls-remote origin refs/heads/main` exit 128(DNS 실패)로 원격 최신 SHA 확인 불가. 기존 `8ea56a97b128`의 17파일 패치를 `git apply --check` exit 0 후 적용했다. 금지된 git worktree/checkout/add/commit/push는 실행하지 않았다. rejected 작업은 코드 계승 출처이며 실행 선행 의존성으로 지정하지 않았다.

/root/aads/AGENTS.md, repo AGENTS.md, 선행 runner-84a003a8 지시를 읽었다. runner-ce240146 지시서는 지정 logs 경로에 없었고 사용자 제공 R3 지시 및 보존 패치로 범위를 확인했다. DATABASE_URL 미설정으로 DB pool 초기화 exit 2: 활성 runner·workspace ledger 읽기 및 충돌 검증 불가. 충돌 없음을 보증하지 않는다. 조회 과정에서 실제 직원/서명본은 읽지 않았다.

### STEP 0와 변경 사유

기존 함수/클래스·API·스케줄러·DB 분류는 위 R3 표를 계승한다. 이번 추가 변경은 `render_signed_contract_pdf`의 봉인 필드 출력/중복 판정(수정), `test_legacy_identity_and_independent_numeric_fields_survive`(신규), 같은 보고서 및 가상 PNG(수정)다. 그 외 선행 helper·엔드포인트·스케줄러·DB 접점은 유지한다. 삭제 없음. PDF가 실제 사용하는 단일 서비스 경로이며 새 마운트/테이블 경로를 만들지 않았다. AAG에 없는 조항 서비스는 기존 JS 조항 규칙의 Python 이식으로 기존 renderer에서 사용한다.

지시된 파일 외 추가 변경 없음. 선행 3개 기존 테스트/fixture/admin 캐시 수정은 8ea56a97의 범위를 그대로 계승했다. R4의 코드 변경은 PDF 서비스와 회귀 테스트에 한정한다. 기존 산출물을 통째 재작성하지 않았다.

### 리뷰 지적 처리

1. **P1 체류자격/등록번호 소실 — 수정.** EMPLOYEE_FIELDS 전체를 출력했다고 간주하던 rendered 문자열을 제거했다. compact 인적사항 표에 없는 `visa_status`, `foreign_registration_no_masked`를 기존 봉인 기재사항에 명시한다. foreign_worker 플래그가 없는 가상 봉인본으로 v3/v4 × regular/freelancer 4건에서 실제 PDF 텍스트 `체류자격/F-2`, `외국인등록번호/123456-*******` 존재를 검증한다. snapshot/hash 불변과 바이트 결정성도 함께 검사한다.
2. **P1 부분 문자열로 독립 필드 소실 — 수정.** `text not in rendered`와 날짜 부분 문자열 판정을 삭제했다. 기본적으로 기존 term_rows/extra_rows를 보존하고, 실제 출력된 동일 약정 필드 키만 제외한 복사본을 사용한다. 추가 특약·수습은 각 필드의 출력 경로가 있고, 다른 사용자 약정은 해당 제목의 조항 본문과 완전히 같은 경우에만 제외한다. 같은 값을 가진 다른 필드의 출력은 보존한다. weekly_hours=40/wage=400000 회귀에서 freelancer의 `수행 회차 기준` 다음 `40` 텍스트를 확인한다. generic rendered 변수 자체가 없으며, 봉인 snapshot은 수정하지 않는다. 기본 수습 반대 문구 억제도 필드 키로 판단한다.

### 이번 세션의 실제 검증 (이전 세션 결과와 구분)

호스트 접두사: `PYTHONPATH=/tmp/obys-rank1-venv/lib/python3.12/site-packages:/root/aads/.testdeps timeout 180 /root/aads/aads-server/.venv/bin/python -m pytest -q`

| 실제 명령/대상 | exit | 결과 |
|---|---:|---|
| timeout 45 bash scripts/run_unit_tests.sh notify_pdf pdf_preview types_clauses v41_contract_editor amendment_chain autofill (모두 tests/unit/test_obys_contract_*.py 실제 경로) | 2 | 기준 이미지/컨테이너 없음, 통과 아님 |
| 호스트 pdf_preview, types_clauses, v41_contract_editor, amendment_chain, autofill, freelancer_contract_labels, obys_company_seal_pdf | 0 | 171 PASS (74.08초), legacy 36건 및 신규 4건 포함 |
| 호스트 notify_pdf에서 지정 HTTP 5건 제외 | 0 | 9 PASS, 5 deselected |
| 호스트 notify_pdf 지정 HTTP 5건, timeout 25, faulthandler_timeout=10 | 124 | 첫 요청 AnyIO blocking portal 대기; 통과 아님 |
| 최소 FastAPI TestClient GET, timeout 15, 호스트 접두사 사용/미사용 각각 | 124/124 | 앱 라우터 없이도 동일 대기; 앱 고유 문제로 확정할 수 없음 |
| socket.socketpair smoke | 0 | 이번 세션은 PASS. 이전 세션의 EPERM 결과를 이번 원인으로 단정하지 않음 |
| Node/Python 조항 패리티 및 preview HTML fixture (위 pytest 포함) | 0 | 56개 패리티, 56개 현재 fixture 일치 |
| HEAD gzip fixture와 UTF-8 바이트 비교 | 0 | 52/56 동일; 기존 daily/v4 4개 변경만 계승 |
| 가상 7종 PDF PNG 재생성 / 텍스트 페이지 경계 / 마지막 페이지 hash | 0 | 전부 2쪽, 범위 및 증적 PASS; 정규직 도장·자필서명 직접 이미지 확인 |
| PLAYWRIGHT_BROWSERS_PATH 지정 Chromium 실제 실행, timeout 20 | 1 | sandbox host Operation not permitted/SIGTRAP. 브라우저 캡처 미완료 |
| timeout 20 curl OFL NanumGothic-Bold 원본 | 6 | DNS 실패, 자산 미확보/실제 굵기 미검증 |
| python3 scripts/dup_guard.py (전체 및 --paths PDF/조항/테스트) | 0 | PASS |
| python3 -m compileall -q PDF/조항/테스트 | 0 | PASS |
| git diff --check | 0 | PASS |
| HEAD 대비 AST def/class 이름 차집합 | 0 | 삭제 0 |
| timeout 15 python3 scripts/error_book.py match 테스트 로그 | 2 | psql 실행 파일 없음, 기존 key 조회/보강 불가 |

초기 중간 검증은 timeout 60으로 exit 124였고 중복 수습 출력 실패 표시도 있었다. 필드별 중복 제거에 probation_period/terms를 포함해 기존 기대값을 바꾸지 않고 해결했으며 최종 합동 검증 결과를 위에 적었다. 마지막 수습 기본값 판정을 필드 키로 정리한 후 동일 합동 목록을 재실행: exit 0, **171 PASS (54.09초)**. 마지막 코드 기준으로 비HTTP 알림 9 PASS와 합쳐 **180 PASS**, HTTP 5건은 별도 미완료다.

정상 호스트 HTTP 5개, admin.html 실제 미리보기와 PDF PNG의 동일 입력 5종 나란히 비교, Bold 실제 굵기 검증은 미완료다. 기존 PNG는 PDF 전용 검토임을 표시하며 화면 일치 증거로 주장하지 않는다. 가상 데이터만 사용했고 실제 데이터/봉인본/DB 스키마/운영 서비스를 변경하지 않았다.

등록 도구 중 AADS 정본 등록/핸드오버 도구는 없고 인증된 AADS API 자격 증명 및 DATABASE_URL도 없어 같은 document_key `obys-contract-pdf-r2`의 초안 등록을 완료하지 못했다. **정본 미등록(미완료), 열람 링크 미제공. 핸드오버 DB 미기록.** 오류 사전 `obys.pdf.legacy-sealed-fields-lost` fix-commit 보강도 접속 불가 및 최종 커밋 부재로 미수행이다.

빌드 명령은 미실행이며 승인 후 Runner 빌드 검증 대상. commit/push/배포/재시작/스키마 변경 및 HANDOVER.md 수정 없음. 롤백은 승인 후 최종 변경 커밋 revert. 전체 완료 조건 중 위 미충족 항목을 남긴 상태다.
