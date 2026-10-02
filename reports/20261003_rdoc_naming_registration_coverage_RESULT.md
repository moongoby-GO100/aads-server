# R-DOC document_key 규칙·등록률·귀속 점검 RESULT (AADS-RDOC-NAMING-COVERAGE-20261003)

- 작업: AADS-RDOC-NAMING-COVERAGE-20261003 (P1, 크기 S, PROJECT AADS, MODE PUSH_ONLY)
- Runner: runner-3688577f / goal ac431290-66be-4d00-8bbb-9bf162917240 / 마일스톤 M7 29dd7c05-cc40-4209-b8ff-cc1765b40b7e (기존 goal·M7 재사용, 중복 goal 없음)
- 연결: PM 세션 8bf0405a, R-DOC goal 0361c451 (기존 연결 유지, 새 연결 없음)
- 승인 근거: CEO 2026-10-03 "보류건 권장안으로 진행해" [지시서 인용]. AAG 브리프는 정본이 아니며 근거로 삼지 않았다.
- 측정 시각: 2026-10-03 약 08:50 KST. 근거 표지: [DB조회] [git조회] [코드확인] [미측정].

## 1. STEP0 분류

| 파일 | 분류 | 내용 |
|---|---|---|
| `docs/specs/rdoc-storage-rule/spec.md` | 수정 | 1.1.0 → 1.2.0. 3항 2번 한 줄과 15항(RDOC-NAMING) 신설. 1~14항 의미 불변 |
| `docs/plans/20261003_AADS_RDOC_STORAGE_RULE_PLAN.md` | 수정(최소) | 88행 `spec(1.1.0)` → `spec(1.2.0)` 한 줄. 승인본 원문 앞부분 불변 |
| `docs/prd/20261003_AADS_RDOC_STORAGE_RULE_PRD.md` | 수정(최소) | 33행 spec 참조 한 줄. 승인본 원문 앞부분 불변 |
| `tests/unit/test_rdoc_docs.py` | 수정 | spec 버전 1.2.0, 15항 테스트 4건 추가 |
| `reports/20261003_rdoc_naming_registration_coverage_RESULT.md` | 신규 | 이 문서 |
| `HANDOVER.md` | 수정 | 이 작업 기록만 추가 |
| 삭제 | 없음 | R-DOC 원문·화면 절차는 고치지 않았다 |

app/tool/hooks 는 고치지 않았다.

## 2. document_key 규칙 (spec 15항)

- 새 키: `{주제}-{kind}`, 소문자 kebab, 날짜·슬래시·버전·대문자·밑줄·콜론 없음, 128자 이하. kind 는 plan|prd|spec|design|architecture|contract|tasks|report|reference.
- 기존 키 전부 grandfather. 이름 변경·이전·재키잉·삭제·재연결 없음. 기존 head 의 새 revision 은 검사 대상이 아니다.
- 파일명 날짜 규칙(`YYYYMMDD_`, 대문자·밑줄)과 `document_key`(날짜 없음, 소문자·하이픈)는 15.3 표로 구분했다. 변환 예는 `20261003_AADS_RDOC_STORAGE_RULE_PLAN.md` → `rdoc-storage-rule-plan`(새 head 일 때만).
- 이 규칙은 문서 규약이다. 어떤 API·훅도 지금 이 규칙을 검사하지 않는다.

### 2.1 예외 목록 [DB조회 2026-10-03]

spec 15.2 의 SELECT 를 실제로 실행했다. 13 heads 중 규칙 적합 0, 예외 13. 과거의 "9개"는 쓰지 않았다. 테넌트는 모두 2d701a8c.

| project | kind | document_key | 사유 | 채팅 도구 검사 | REST KEY 정규식 |
|---|---|---|---|---|---|
| AADS | prd | aads-current-authority-context | kind 접미 없음 | 통과 | 통과 |
| AADS | prd | acct-clobe-dual-collection-prd-20261003 | 날짜 | 거절 | 통과 |
| AADS | contract | contract:b4ad294d9869 | 콜론·해시형 | 거절 | 통과 |
| AADS | plan | plan:7d9f483b5dba | 콜론·해시형 | 거절 | 통과 |
| AADS | prd | prd:848c81d57565 | 콜론·해시형 | 거절 | 통과 |
| AADS | prd | prd:e677db2cb301 | 콜론·해시형 | 거절 | 통과 |
| ACCT | plan | OBYS-CAFE24-CONSOLIDATION-PLAN-20261002 | 대문자+날짜 | 거절 | 통과 |
| ACCT | prd | OBYS-CAFE24-CONSOLIDATION-PRD-20261002 | 대문자+날짜 | 거절 | 통과 |
| ACCT | tasks | OBYS-CAFE24-CONSOLIDATION-TASKS-20261002 | 대문자+날짜 | 거절 | 통과 |
| ACCT | spec | spec:304b66102a6f | 콜론·해시형 | 거절 | 통과 |
| GO100 | prd | go100-data-engine-optimization | kind 접미 없음 | 통과 | 통과 |
| GO100 | report | go100-data-wave-architecture-audit-20260929 | 날짜 | 거절 | 통과 |
| GO100 | prd | go100-wave-lease-revision-integrity | kind 접미 없음 | 통과 | 통과 |

도구 검사 열은 정규식 재현이며 [코드확인] 이다 (도구를 실행하지 않았다). 산식: 13 = 0(적합) + 13(예외), 거절 10 + 통과 3 = 13.

## 3. 채팅 등록 도구 validator 대조

대상: `validate_document_key` (`app/services/canonical_document_tools.py`, 미병합 커밋 1ccf4f78 = runner-d869f5bc 재작업, awaiting_approval). origin/main 에 없다 [git조회]. 수정하지 않았다.

| # | 불일치 | 영향 | 담당·후속 |
|---|---|---|---|
| 1 | kind 접미를 검사하지 않는다. spec 15.1 은 요구한다 | kind 없는 새 키가 통과(예외 13건 중 3건과 같은 형태) | runner-d869f5bc 검수 세션 / PM 8bf0405a 가 spec 1.2.0 반영 여부 결정 |
| 2 | head 조회 전에 키 형식을 검사한다. spec 15.2 는 기존 head 갱신을 거절하지 않는다고 한다 | 예외 13건 중 10건은 채팅 도구로 새 revision 을 올릴 수 없다. 정본 API 로는 가능 | 위와 같음. 기존 head 가 있으면 형식 검사를 건너뛰는 순서 변경은 도구 작업에서 판단 |

테스트를 풀거나 우회하지 않았다. 후속은 goal 0361c451 / M7 에서 추적한다.

## 4. R-DOC head 상태 [DB조회]

- plan head `plan:7d9f483b5dba`, prd head `prd:848c81d57565`: 각각 revision 1(1.0.0) 하나, latest = approved. 승인 포인터와 과거 revision 불변. 두 head 는 goal 0361c451 과 a6cc6511 에 연결되어 있다.
- runner-f47e305e 는 정본 등록을 하지 않았다. 중복 등록 없음.
- 전체 heads 13: approved 5, latest ≠ approved 8.
- 승인본 원문 앞부분 sha256 (파일 측 계산): plan `b0a54223627600eb18b549dd18cbf9627e4e8f31fca098f6fb4c7785bef750df`, prd `8cacc274a62a1108001211109139c32c13dbd1b57e4d09f594d344b011ab1d7f` (수정 전과 같음). 호스트 pytest 의 DB 대조 테스트도 통과.
- **후속 draft 미등록 사유**: 이 변경은 아직 검수·승인·push 전이다. 등록은 승인·push 뒤 정상 tenant 인증이 된 정본 API 로, 같은 두 head 에만 한다. 지금 이 세션에는 tenant 인증된 API 호출 수단이 없고, SQL 직접 쓰기·토큰 추출은 하지 않는다. 담당: Runner 승인 후 정본관리자(PM 8bf0405a 지시). 새 head·승인·L1 활성화는 하지 않았다.

## 5. 등록률 [git조회][DB조회]

창: 2026-09-26 08:45 ~ 2026-10-03 08:45 KST (7일, 측정 약 08:50 KST). 기존 `reports/20261002_project_documents_allserver_storage_inventory.csv` 를 재사용하되 "신규" 는 mtime 이 아니라 `git log --diff-filter=A` 로 셌다. "등록" 은 파일 sha256 = `content_hash` 일 때만. 제목 일치만으로는 확정하지 않았다.

| 범위 | 신규 후보 파일 | 고유(해시) | 내용 일치 등록 | 비고 |
|---|---|---|---|---|
| aads-server (origin/main ad3a81d6) | 15 | 15 | 1 | goal_documents 경로 일치 2건은 따로 센다(내용 일치 아님) |
| aads-dashboard (c104be80) | 0 | 0 | 0 | |
| aads-docs (b915d42f) | 0 | 0 | 0 | |
| contabo14 kis-autotrade-v4 (HEAD 253a31f03) | 21 | 20 | 3 파일 = 고유 head 2 | 사본 1쌍(해시 68de0622) |
| **측정 범위 합계** | 36 | 35 | 3 | 36 = 15+0+0+21, 고유 35 = 15+20 |

- 측정 범위 고유 35건 중 내용 일치 3건(8.6%, 3/35)이다. 미측정 범위가 있어 **전체 등록률이 아니다**.
- aads-server 의 신규 md/html 41건 중 26건은 "기타"(후보 외)다.
- R-DOC 3파일은 head 의 승인본과 앞부분만 일치해 revision 으로는 미등록이다.
- kis HEAD 는 2026-10-02 20:07 KST 이고 이후 커밋은 보지 못했다(저장소가 detached HEAD, origin/main 없음).

### 미측정 (분모에서 빼지 않고 표시)

| 범위 | 사유 | 담당 |
|---|---|---|
| ACCT 저장소 | runner-52a46b5e 가 queued 라 수집 전 | runner-52a46b5e, PM 8bf0405a |
| jinah244 | 기존 CSV 에 행 없음(이번 작업에서 재수집하지 않음) | 인벤토리 RECOVER 작업 이력 참조 |
| cafe24_114 (CSV 10,771행) | git 증거 없이 mtime 뿐이라 신규 판정 불가 | 전 서버 인벤토리 후속 |
| kis HEAD 이후 커밋 | 위 참조 | contabo14 후속 측정 |

## 6. acct-clobe-dual-collection-prd-20261003 귀속 [DB조회][코드확인]

- 현재 head: tenant 2d701a8c, project_key=AADS, kind=prd, revision 1(latest), approved 없음, source_path 와 생성 세션은 비어 있다.
- 내용: 연결 goal 4226a834 는 project AADS 이고, 본문이 실행 허브를 AADS 로 명시한다. 이름 접두사(`acct-`)만으로 귀속을 판단하지 않았다.
- **판정: 유지(keep).** 오귀속의 증거가 없다. 빈틈: source_path·생성 세션이 null, 원 작성 세션의 답을 받지 못했다(도달 가능한 세션 없음), ACCT 쪽 가시성 미확인.
- 영향: 유지해도 ACCT 에서 검색되지 않을 수 있는 가시성 문제가 남는다. 정정하면 AADS 의 goal 링크와 이벤트 이력이 끊긴다.
- 롤백: 지금은 변경 없음이라 롤백 불필요. 나중에 정정이 필요하면 DB 이동이 아니라 ACCT 에 새 revision 으로 등록하고 AADS head 는 남긴다(삭제·이동 없음). 키에 날짜가 있어 grandfather 대상.
- DB 이동, 문서·원본 수정은 하지 않았다.

## 7. M4/M6 "completed" 대조 (상태는 덮어쓰지 않음)

| 마일스톤 | 완료 기준 | 현재 증거 | 판단 |
|---|---|---|---|
| M4 | ① 4서버 수집 ② 배치 승인 | runner-52a46b5e queued, CSV 에 3서버(jinah244 없음) | ① 미충족. 증거 비어 있음 |
| M6 | ① 구현 ② 7일 shadow | canonical_gate_events 에 shadow 1행, 약 35분 경과 | ② 미충족. 7일 shadow 아님 |

둘 다 status 가 completed 이나 증거 필드가 비어 있다. 상태는 바꾸지 않았다. PM(8bf0405a)의 재판정을 권고한다. 참고: M5/M9 blocked, M3 review. M7 완료 기준 충족 여부는 PM 판단 몫이며 자동 변경하지 않았다.

## 8. 검증 (구분 표기)

| 구분 | 실행 | 결과 |
|---|---|---|
| 코드/단위 (호스트 pytest, DB 접속 가능) | `python3 -m pytest tests/unit/test_rdoc_docs.py` | 26 passed (DB 대조 포함) |
| 코드/단위 (운영 이미지 임시 컨테이너) | `bash scripts/run_unit_tests.sh tests/unit/test_rdoc_docs.py` | 23 passed, 3 skipped (DB 의존 3건은 PG 환경 없어 skip) |
| diff 검사 | `git diff --check` | 종료 0 |
| 정적 분석 | `ruff check --select F821,F811 tests/unit/test_rdoc_docs.py` | All checks passed |
| 중복 재적용 | `scripts/dup_guard.py --paths ...` | 종료 0 |
| prefix 해시 | 4절의 두 해시 | 수정 전과 일치 |
| 산술 | 36 = 15+0+0+21, 고유 35, 등록 3 | 일치 |
| 키 규칙 | 새 키 테스트(적합 2·부적합 7), SQL 예외 집합 = 정규식 평가(13건) | 통과 |

**운영/브라우저 검증: 하지 않았다.** 이 작업은 문서·테스트 변경이고 화면·API 변경이 없다. 운영 반영 검증과 브라우저 검증은 해당 없음이며 통과로 보고하지 않는다.

## 9. 실행하지 않은 것과 사유

- commit / push: 지시상 Runner 가 승인 후 수행. 아래 SHA·URL 은 Runner 가 채운다.
- 정본 API 로의 draft 등록: 4절 사유.
- DB 인수인계 기록(handover_write / POST /api/v1/handovers): 이 세션에는 tenant 인증된 호출 수단이 없어 기록하지 못했다. HANDOVER.md 항목으로 대신한다.
- 오류 사전(scripts/error_book.py) 등록: 확인된 원인이 없어 하지 않았다(추측 금지).
- 파일럿 74/92/109/110/119 재실행·재연결, 대량 등록·승인, L1 변경, enforce 활성화: 하지 않았다.

## 10. 변경 파일, SHA, URL, 비용

- 변경: spec.md, PLAN, PRD, test_rdoc_docs.py, 이 RESULT, HANDOVER.md
- 작업 파일 sha256: spec.md `05e01dfc0f8f90ebab5f68c0a280d84bf4a9c5bb41862b46c3812649fedc9f39`, test_rdoc_docs.py `23204727f738b076a624b968ed78980aab9f5761eae02d60d71cf2f9a339a0f0`
- 커밋 SHA: (Runner 가 커밋 후 기입)
- GitHub 브라우저 URL: (Runner 가 push 후 `https://github.com/moongoby-GO100/aads-server/blob/<SHA>/reports/20261003_rdoc_naming_registration_coverage_RESULT.md` 로 기입)
- 비용: $ 미측정 (예산 $5)
