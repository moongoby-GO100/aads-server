# AADS-CANONICAL-SEARCH-VERIFY-R5-20261003 — 결과

> **회수 주석 (2026-10-04 KST, AADS-CANONICAL-R5-RECOVER-R6-20261004).** 이 문서는 runner-8300c642 가 만들었고 그 job 은 `approval_commit_failed`(gitleaks generic-api-key)로 종료되어 **당시에는 커밋·푸시되지 않았다.** 아래 §6 의 "커밋/푸시: 하지 않았다" 와 "226행"·"고객 로그인 401"·"grants 0" 은 **2026-10-03 14시대의 역사적 측정**이며 그대로 보존한다. 현재 사실과 새 커밋 영수증은 `20261004_canonical_search_verify_R6_RESULT.md` 에 있다. 증거 파일은 아카이브 패치(텍스트)와 git 오브젝트(스크린샷 36장)에서 복구했고, tenant UUID 2종과 문서 키 2건을 별칭으로 바꿨다(R6 §2). 본문의 `2d701a8c…`·`c1082af2…` 같은 축약 표기는 그대로 둔다.

- 유형: READONLY_VERIFICATION_PUSH_ONLY (P1, 규모 S, NO_DEPLOY)
- 실행 구간: 2026-10-03 14:13 ~ 14:40 KST
- 작업 트리: `/tmp/aads-wt-runner-8300c642` (HEAD `f20da86b056c`)
- 증거 디렉터리: `reports/20261003_canonical_search_verify_R5_evidence/`

## 0. 한 줄 결론

226행 정본 색인은 **정합**하다(시작 226 = 종료 226, 임베딩 226, 중복·누락·해시불일치 0). 운영 검색 API는 **무인증 거부**와 **내부 tenant 허용**을 실제로 확인했다. 그러나 아래 세 조합은 기존 데이터·권한으로는 **운영 API에서 검증하지 못했다.** 생성도 하지 않았다.

| 미검증 조합 | 이유 | 대체 증거(등급) |
|---|---|---|
| 실제 **다른 tenant** 로그인으로 정본 비노출 | 저장된 고객 tenant e2e 자격증명이 로그인 401, 다른 고객 자격증명 없음 | SQL 계층 운영 술어 증거(비-API) — 다른 tenant 68개 × 관리자/비관리자 누출 0 |
| **grant 허용**(비관리자 + 프로젝트 grant) | `project_document_grants` 0행 | 유닛 테스트(운영 아님) |
| **ACCT 화면(UI)**·파일럿 119 화면 | `/project-docs/scan` 이 ACCT 를 돌려주지 않아 정본 탭에 ACCT 버튼이 없음 | API 로 파일럿 119 승인 상태 확인(§5) |

AAG 안전검증 통과는 **주장하지 않는다**(브리프가 stale/not_proven 이며 negative_assertion_allowed=false).

## 1. STEP 0 — 기존 구현 분류

| 구현 | 분류 | 비고 |
|---|---|---|
| `scripts/index_docs.py` `index_canonical` / `--dry-run` | 유지 | 쓰기 전 반환(§3.4). 변경 없음 |
| `app/services/doc_index.py` `_visible_sql`, `_HEAD_JOIN` | 유지 | 운영 컨테이너 코드 = 워크트리(md5 동일) |
| `app/api/project_docs.py` `/project-docs/search` | 유지 | tenant 는 인증 컨텍스트에서만 결정 |
| `app/api/canonical_documents.py` | 유지 | 목록/상세만 호출. 승인·보관·검토 POST 미호출 |
| 대시보드 `/docs` 정본 탭 | 유지 | 수정·자동수정 없음. 발견사항만 §7 |
| 소스·스키마·색인·head/revision·grant·계정·goal | 변경 없음 | |
| 신규 | 신규 | 보고서 1건 + 증거 디렉터리만 |

## 2. 배포·릴리스 상태 (읽기 전용)

- 배포 #5557: `success / completed`, release `34007c6b391f`, 2026-10-03 09:19:54 → 09:29:25 KST. p0p1 모니터링 단계 309초 완료(`04_deploy_5557_phase_events.txt`).
- 이후 #5570(`a16ec3cfab91`) → #5572 → #5573(`f20da86b056c`)이 이어졌고, 시작 시점에는 #5573 이 `verifying/p0p1_monitoring` 이었다. 종료 시점(14:33)에는 `success`(`13_db_end_counts.txt`).
- 현재 운영: `aads-server` 와 `aads-server-green` 이 모두 `aads-server:f20da86b056c` (healthy), 공개 health-check HTTP 200(`14_release_state_end.txt`). 이 실행 중 슬롯이 한 번 더 바뀌었다(다른 러너의 배포, 나는 관여하지 않음).
- 조상 관계: `34007c6b` 는 현재 HEAD `f20da86b` 의 조상.
- tenant 가드: 양쪽 컨테이너의 `/app/app/services/doc_index.py`(md5 `962b33f9…`), `/app/app/api/project_docs.py`(md5 `72dc37ea…`)가 워크트리와 **동일**. `/project-docs/search` 는 `require_tenant_member` 로 보호된다(`03_runtime_code_guard.txt`, 종료 시점 재확인 완료).
- 참고: 내 것이 아닌 컨테이너 `vigorous_shirley`(`aads-server:a16ec3cfab91`)가 떠 있었다. 건드리지 않았다.

## 3. 정본 청크 읽기 검증 (DB 읽기 전용: `default_transaction_read_only=on`)

### 3.1 건수 (기준선 226)

| 시점 | 전체 | 정본 | 정본 임베딩 | 정본 max indexed_at |
|---|---|---|---|---|
| 시작 14:13:29 | 28279 | **226** | 226 | 2026-10-03 13:11:59 |
| 종료 14:33:33 | 28279 | **226** | 226 | 2026-10-03 13:11:59 |

시작과 종료가 같고 `indexed_at` 최댓값도 13:11:59 로 고정이다. 이 실행 중 색인 변경은 없었다. 다른 cron 의 변화도 관찰되지 않았다(`01`, `13`).

### 3.2 정합성 (`05_canonical_readonly_checks.out`)

- 프로젝트별: AADS 103청크/12문서, ACCT 42/4, GO100 81/3 → 합 226청크/19개정판. 모두 tenant `2d701a8c…`(내부), 라벨 `정본`, 서버 `vmi3267555`.
- head 누락·tenant/프로젝트 불일치·revision 누락·revision-head 불일치·해시 불일치·경로 불일치 **전부 0**. stale 포인터 0.
- 승인 포인터 5건 / 최신 초안 14건. 라벨 `승인` 5건, `초안` 14건, 라벨↔포인터 불일치 0.
- head 17개 모두 색인됨. 미색인 head 0, 미색인 승인/초안 revision 0, 고아 색인 0.
- `(doc_path, chunk_index)` 중복 0. 빈 내용 청크 0. 정본 라벨이 붙은 비정본 경로 0.
- 해시: 개정판 19건 모두 `content_hash == sha256(content)`. legacy 링크 5건 모두 `validated_hash` 일치(`08_hash_checks.out`). 청크 인덱스 연속 19/19. 청크 본문이 원문의 50% 미만인 개정판 0. 청크 내 비밀 패턴 0건.
- 복수 개정판이 색인된 head 2개는 의도된 구성이다(승인 v1.0.0 + 최신 초안 v1.2.0).

### 3.3 파일럿 5건 (`06`, `07`)

| 파일럿 | 프로젝트 | document_key | 상태 |
|---|---|---|---|
| 74 | AADS | `contract:b4ad294d9869` | 승인 v1.0.0, 최신=승인, 8청크 |
| 92 | AADS | `prd:e677db2cb301` | 승인 v1.3.0, 최신=승인, 2청크 |
| 109 | AADS | `plan:7d9f483b5dba` | 승인 v1.0.0 + 초안 v1.2.0(최신), 13청크 |
| 110 | AADS | `prd:848c81d57565` | 승인 v1.0.0 + 초안 v1.2.0(최신), 13청크 |
| 119 | ACCT | `spec:304b66102a6f` | 승인(revision `c1082af2…`) |

의미: **승인 포인터**(`approved_revision_id`)가 정본이고, `승인`/`초안` 라벨은 그 revision 이 승인 포인터인지의 표시다. 최신 개정판이 초안이어도 승인 포인터는 유지된다(109/110).

### 3.4 `index-canonical --dry-run`

실행했다(`09_index_canonical_dry_run.txt`): `정본 19건 중 변경/신규 0건, 제거 0건 (dry-run: 쓰지 않음)`, exit 0. 소스 증거: `index_canonical` 은 `dry_run or not (changed or stale)` 이면 어떤 쓰기도 하기 전에 반환한다. 세션에 `default_transaction_read_only=on` 을 강제했으므로 DB 쓰기도 불가능했다. 실제 `index-canonical` 은 실행하지 않았다.

## 4. 운영 검색 API 검증

인증 방식 확인: `get_current_user` 는 Bearer JWT/쿠키 또는 `x-monitor-key` 를 받는다. `/project-docs/search` 는 query 로 들어온 tenant 를 무시하고 인증 컨텍스트의 tenant 만 쓴다.

| 항목 | 결과 | 증거 |
|---|---|---|
| 무인증/잘못된 Bearer/잘못된 monitor key/`X-Tenant-ID` 위조 — 공개 URL, 8100, 8102 | **전부 401** | `10_api_unauth.txt` |
| 내부 tenant(소유자) 정상 검색 | **200**, 정본 히트 포함 | `16`, `17`, `18` |
| 프로젝트 필터 AADS/ACCT/GO100 | 200, 해당 프로젝트 정본만 | `16`, `17` |
| 다른 실제 tenant 비노출 | **운영 API 미검증**(위 표) | `15`: 고객 자격증명 로그인 401 |
| grant 차단/허용 | 차단은 SQL로 확인, 허용은 미검증 | `19`, 유닛 |

SQL 계층 증거(`19_sql_scope_probe_other_tenants.json`, 운영 코드의 `_visible_sql`+`_HEAD_JOIN` 을 컨테이너에서 import, 읽기 전용 연결): 원시 정본 226청크 중 내부 관리자 가시 226/19문서, **비관리자·grant 없음 → 0**, 다른 tenant 68개 × (관리자/비관리자) 누출 **0**, tenant NULL 정본 0. 이것은 검색 SQL 의 술어 증거이며 로그인한 다른 tenant 의 API 응답이 아니다.

유닛 테스트(운영 검증과 구분): `scripts/run_unit_tests.sh` 로 실행, exit 0.

| 파일 | 결과 |
|---|---|
| `test_doc_search_tenant_guard.py` | 20 passed |
| `test_doc_index_canonical.py` | 27 passed |
| `test_canonical_documents.py` | 13 passed |
| `test_doc_search_authority_metadata.py` | 40 passed |

검색 관찰: 파일럿 92 와 GO100 "구조 실사"는 제목 그대로 검색하면 잡히지만, 일반 의미 질의에서는 유사한 파일 청크 13개에 밀려 정본 초안이 순위권 밖이다(`17`, `18`). 가시성이 아니라 **순위** 문제다.

## 5. https://aads.newtalk.kr/docs 서버 Playwright

- smart_browser 승인 레시피: `/docs` 를 다루는 레시피는 없었다. 그래서 목록 조회 후 호스트 Playwright(Chromium)를 사용했다. 기존 레시피 재사용 대상이 없었다.
- 인증: 내부 tenant 의 기본 관리자 신원으로 `e2e-auth.html` 경유 로그인(저장된 e2e 자격증명은 401이라 사용하지 못함). 로그인 성공은 URL 이동이 아니라 **인증된 화면 내용**으로 판정했다(헤더 `HEALTHY · API OK · 로그아웃`, 정본 탭의 실제 문서 목록).
- 브라우저는 사용 가능했다. 따라서 "브라우저 E2E 미실행" 대체 문구는 해당 없음.

| 항목 | 데스크톱 | 모바일 | 증거 |
|---|---|---|---|
| 무인증 → 로그인 차단 화면 | 확인 | 확인 | `*_01_unauth_blocked_login.png` |
| 인증 후 정본 탭 | 확인 | 확인 | `*_03`, `*_04` |
| AADS 정본 목록 10건 | 확인 | 확인 | `*_11_canonical_list_AADS.png`, `ui_e2e_pass3_log.json` |
| GO100 정본 목록 3건(초안) | 확인 | 확인 | `*_11_canonical_list_GO100.png` |
| 정본 검색(OHVIS) | 확인 | 확인 | `*_05` |
| 파일 목록 탭, "내용(뜻으로)" 검색에서 정본 히트 표시 | 확인(승인·초안 각 개정판 4건, 비활성 표시) | 확인 | `*_02`, `*_10` |
| 승인/초안/개정판 표시(최신 개정판 줄 + "현재 승인된 정본" 패널) | 확인 | 확인 | `*_06`, `*_13_*_detail_loaded.png`, `ui_e2e_pass4_log.json` |
| 파일럿 74/92/109/110 읽기 전용 표시 | 확인 | 확인 | `*_13_pilot*` |
| 파일럿 119(ACCT) 화면 | **미확인** | **미확인** | §7 |

파일럿 표시 값(`ui_e2e_pass4_log.json`): 74 최신=승인 v1.0.0, 92 최신=승인 v1.3.0, 109/110 최신=초안 v1.2.0 + 승인 패널 v1.0.0. 활성 버튼은 74/92 는 `보관`, 109/110 은 `검토로 이동`·`보관` 뿐이고, **어느 버튼도 클릭하지 않았다.**

증거 한계 3건: (a) 첫 패스의 `desktop_01` 은 두 번째 패스에 덮어써졌다. (b) `*_12_pilot92/109` 스크린샷은 상세 영역이 로딩 중이거나 스크롤 밖인 프레임이다. 대신 로딩 완료 후 찍은 `*_13_*` 과 로그를 증거로 쓴다. (c) `*_13_*` 요소 캡처에는 sticky 헤더가 겹쳐 보인다.

## 6. 변경 파일 / 커밋 / 푸시 / 배포

- 변경: `reports/20261003_canonical_search_verify_R5_RESULT.md`(신규), `reports/20261003_canonical_search_verify_R5_evidence/**`(신규). 소스·스키마·설정·HANDOVER 변경 없음.
- 커밋/푸시: **이 세션은 하지 않았다.** 러너가 승인 후 수행한다. 커밋 URL 은 푸시 후에야 존재하므로 여기서는 기재하지 못한다.
- 배포/빌드/재시작: 없음(NO_DEPLOY). 기존 배포 상태는 §2.
- 시작 시점부터 있던 `?? reports/20261003_canonical_search_verify_R5_evidence/` 는 이 실행의 산출물이다.

## 7. 발견사항 (수정하지 않음)

1. **ACCT 정본이 대시보드에서 도달 불가.** 정본 탭의 프로젝트 버튼은 `/project-docs/scan` 결과(`AADS 3705, KIS 154, GO100 1736, SF 0, NTV2 0`, `11_project_docs_scan_projects.json`)에서 만들어지고, ACCT 는 스캔 대상이 아니라서 버튼이 없다. API `GET /api/v1/projects/ACCT/documents` 는 200 이고 4건을 반환한다(`spec:304b66102a6f` 승인, 나머지 3건 초안, `12_api_acct_documents_list.json`). UI 보완이 필요한 사항이며 자동수정하지 않았다.
2. **스캔이 느리다.** 정본 탭의 GO100 버튼은 `?force=true` 스캔 완료 후에야 나타난다(실측 약 46초, 이후 캐시 1.3초). 85초 대기 후에야 GO100/KIS 버튼이 보였다.
3. **검색 순위.** 정본 초안이 유사한 파일 청크에 밀린다(§4).
4. `ui_e2e_pass4_log.json` 의 `approved_line` 필드는 빈 값이다(스크립트의 추출 방식 한계). 승인 패널의 존재(`has_approved_panel=true`)와 스크린샷으로 판정했다.

## 8. 비밀 노출 공시

두 번째 Playwright 시도 중 `networkidle` 타임아웃 예외 문구에 내부 관리자 JWT 가 터미널 출력으로 한 번 노출되었다(만료 약 7일). 그 출력과 에러 파일은 즉시 삭제했고, 이후 스크립트는 예외 문구를 정제하고 토큰이 산출물에 없음을 assert 한다. 산출물에는 토큰이 없다(패턴 스캔 0건). 또한 자격증명 목록 조회 때 e2e 로그인 URL 의 앞 50자가 터미널에 출력되었으나 파일에는 저장하지 않았다. **내부 관리자 토큰은 이 세션 대화 기록에 남아 있으므로 CEO 가 교체 여부를 판단해야 한다.**

## 9. 오류 사전

새로 확인된 근본 원인이 없어 `scripts/error_book.py` 에 기록하지 않았다. 저장된 e2e 자격증명이 401 인 원인은 확인하지 못했으므로 추측으로 넣지 않았다.

## 10. HANDOVER 미변경 사유와 호환 핸드오버 요약

`HANDOVER.md` 와 `docs/HANDOVER.md` 는 브리프가 수정을 금지했고 공통 파일이라 다른 러너와 충돌하기 쉬워 **수정하지 않았다.**

호환 핸드오버 요약:
- AADS-CANONICAL-SEARCH-VERIFY-R5-20261003: 정본 색인 226행 정합(시작=종료 226, 임베딩 226, dry-run 변경 0). 무인증 401, 내부 tenant 검색 200 확인.
- 미검증: 실제 다른 tenant 로그인 응답, grant 허용, ACCT 화면/파일럿 119 화면.
- 다음: (1) 고객 tenant 테스트 계정이 승인되면 교차 tenant 검증 재개, (2) 정본 탭이 scan 과 무관하게 ACCT 등 정본 보유 프로젝트를 나열하도록 별도 승인된 작업으로 수정, (3) 정본 검색 순위 개선 검토.

## 11. DB 핸드오버(project_handover_entries)

인증된 `POST /api/v1/handovers`(내부 tenant 소유자 토큰, 정상 인증 경로)로 기록했다. 직접 SQL 은 쓰지 않았다.

- 결과: HTTP 201, `entry_key=aads-canonical-search-verify-r5-20261003`, project=AADS, entry_type=verification, priority=P1, status=active, **revision 2**, id `6258d9bd-5f53-4ca1-8e0f-e43789f7b71a`, 갱신 2026-10-03 14:37:30 KST.
- 주의: 같은 entry_key 가 이미 있었다(revision 1, 2026-10-03 13:43 KST 생성 — 취소된 R4 실행의 기록으로 추정, 확인하지 못함). 이 POST 는 그것을 갱신(upsert)해 revision 2 로 올렸다. 이전 revision 은 이력(`GET /api/v1/handovers/{id}/events`)에 남는다. 내용을 확인하지 않고 덮어썼다는 점을 공시한다.
- 읽기 전용 확인: `project_handover_entries` 조회로 위 값 확인.

## 12. 완료 / 미완료 / 다음 경로

- 완료: 릴리스·가드 확인, 정본 청크 정합 읽기 검증과 시작·종료 건수 비교, dry-run, 무인증 거부, 내부 tenant 허용, SQL 계층 교차 tenant·grant 차단 증거, 유닛 테스트 4파일, 데스크톱·모바일 UI(AADS/GO100, 파일럿 74/92/109/110).
- 미완료: 운영 API 교차 tenant, grant 허용, ACCT/파일럿 119 UI.
- 비용: LLM·임베딩 호출 없음(검색 API 는 서버측 임베딩을 사용할 수 있으나 호출 수는 약 25회 이하 소량, **$ 미측정**). 5달러 게이트 대상 아님.
- 자동 goal 완료 처리 없음.

## 13. 재현 명령 요약

```
# 읽기 전용 psql 래퍼: docker exec -e PGOPTIONS='-c default_transaction_read_only=on' aads-postgres psql -U aads -d aads -X
# 건수:        select count(*), count(*) filter (where doc_path like 'canonical://%') from doc_chunks;
# dry-run:     python3 scripts/index_docs.py index-canonical --dry-run   (읽기 전용 shim 경유)
# 무인증:      curl -s -o /dev/null -w '%{http_code}' https://aads.newtalk.kr/api/v1/project-docs/search?q=x   → 401
# 유닛:        bash scripts/run_unit_tests.sh tests/unit/test_doc_search_tenant_guard.py   → exit 0
# UI:          host Playwright (chromium), e2e-auth.html 경유, desktop 1440x900 / mobile 390x844
```
