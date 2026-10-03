# AADS-CANONICAL-R5-RECOVER-R6-20261004 — 결과

- 유형: R5 증거 회수 + 교차 tenant/grant 잔여 검증 (P1, 규모 S, PUSH_ONLY, NO_DEPLOY)
- 실행: 2026-10-04 06:53 ~ 07:01 KST (실측), 작업 트리 `/tmp/aads-wt-runner-d08c33b4`, 기준 HEAD `1d19322f`
- 증거: `reports/20261004_canonical_search_verify_R6_evidence/` (신규), 회수분 `reports/20261003_canonical_search_verify_R5_evidence/`
- entry_key 제안: `aads-canonical-search-r6-recovery-20261004` (verification). **완료가 아니라 미검증 항목이 남은 상태**다.

## 0. 한 줄 결론

R5 산출물 전부(결과 문서 1 + 텍스트 증거 24 + 스크린샷 36)를 **복구**했고, gitleaks 오탐 5건을 **설정 완화 없이** 별칭화로 0건으로 만들었다. 현재 DB 는 정합(정본 260행 = 임베딩 260)이고 무인증은 401 이다. 그러나 **실제 다른 tenant 로그인 API 비노출**과 **grant 허용**은 이번에도 운영 API 로 검증하지 못했다. 원인을 확인했고(§4) 필요한 최소 승인안을 §5 에 남겼다.

## 1. STEP 0 — 기존 구현 분류

| 대상 | 분류 | 비고 |
|---|---|---|
| 서버 소스·스키마·`.gitleaks.toml`·hook·runner·색인·head/revision·grant·계정·goal | **유지** | 변경 없음. 정본 초안 순위도 변경하지 않음 |
| `reports/20261003_canonical_search_verify_R5_RESULT.md` | **수정** | 맨 위 "회수 주석" 1블록만 추가. 본문은 역사적 측정으로 보존 |
| `reports/20261003_canonical_search_verify_R5_evidence/**` | **신규(복구)** | 이전 job 이 만들었으나 origin 에 없던 파일. 일부 값만 별칭화(§2) |
| `reports/20261004_canonical_search_verify_R6_*` | **신규** | 본 문서 + 증거 |
| 삭제 | 없음 | `.runner_archive/runner-8300c642.*` 는 읽기만 했고 원본 변경·삭제 없음 |

변경 범위는 TARGET_FILES 안이다. 그 밖의 파일은 바꾸지 않았다.

## 2. R5 증거 회수

### 2.1 origin 반영 여부 (먼저 확인)

`git ls-tree origin/main` 에 `canonical_search_verify` 가 없었다(R5 결과·증거 모두 미반영). 원 worktree `/tmp/aads-wt-runner-8300c642` 는 없었다. 따라서 회수가 필요했다.

### 2.2 회수 방법과 결과

| 구분 | 수 | 방법 | 검증 |
|---|---|---|---|
| 결과 문서 | 1 | `.runner_archive/runner-8300c642.dirty.patch`(118069B) 의 텍스트 diff 를 `git apply --exclude='*/screens/*'` | `git apply --check` 통과 후 적용 |
| 텍스트 증거 | 24 (`01`~`20` + `.sql`) | 동일 | 〃 |
| 스크린샷 PNG | 36 (데스크톱 18 + 모바일 18) | 패치는 `Binary files … differ` 로 바이트가 없어 `git apply` 불가. 패치의 `index 00000000..<blob>` 접두로 **git 오브젝트 DB 에서 `cat-file blob`** 재구성 | 36개 모두 PNG 시그니처 확인, **`git hash-object` 가 패치 기록 blob 접두와 36/36 일치**, 크기 1440x900 등 확인 |
| 미복구 | 2 | `ui_e2e_pass3_log.json`, `ui_e2e_pass4_log.json` | R5 문서 §5 가 인용하지만 패치에 없고 `ignored.list` 에는 `.runner_full_diff.patch` 뿐이다. **복구하지 못했다.** 해당 로그가 뒷받침하던 문장(AADS 정본 목록 10건, 파일럿 표시 값)은 현재 스크린샷으로만 뒷받침된다 |

스크린샷 대표 2장(`mobile_01`, `desktop_13_pilot109`)을 열어 토큰·쿠키·URL 노출이 없음을 확인했다. 나머지 34장은 열지 않았다(미확인).

R5 문서가 이미 밝힌 증거 한계(첫 패스 `desktop_01` 덮어쓰기, `*_12_pilot92/109` 로딩 프레임, `*_13_*` sticky 헤더)는 그대로다.

### 2.3 gitleaks 와 별칭화

원인(실측): runner-8300c642 `approval_commit_failed` stderr=gitleaks generic-api-key. **DETACHED 는 원인이 아니다.**

재현: `gitleaks dir --redact -c .gitleaks.toml reports/20261003_canonical_search_verify_R5_evidence` (gitleaks 8.30.1). 복구 직후 5건은 `detect --no-git --source <dir>` 로 측정했고 최종 0건은 `dir` 과 `detect` 두 방식으로 모두 확인했다(`dir` 에는 `--no-git` 플래그가 없다).

| 시점 | 검출 | 위치 |
|---|---|---|
| 복구 직후 | **5건** generic-api-key | `15_…login.json` 6·13행, `16_…pass1.json` 6행(`tenant_id_of_credential` UUID), `12_api_acct_documents_list.json` 24·31행(`document_key`) |
| 별칭화 후 | **0건** | 결과 문서 단독 스캔도 0건 |

브리프가 적은 3건 외에 `12_…` 의 `document_key` 2건이 더 있었다. 이 두 줄은 UUID 가 아니라 문서 키 값(`OBYS-CAFE24-CONSOLIDATION-{PLAN,PRD}-20261002`)이 걸린 것이다(`…-TASKS-…` 줄은 걸리지 않음).

적용한 변경(`.gitleaks.toml` 완화·allowlist·hook 우회 없음):

| 원래 값 | 별칭 | 적용 범위 | 비고 |
|---|---|---|---|
| 내부 tenant UUID | `tenant-a` | `05_canonical_readonly_checks.out`, `15_…`, `16_…` 총 5곳 + R6 `r6_0*.out` | UUID sha256 앞 12자 `7e56f5c20d9e` |
| 고객 e2e tenant UUID | `tenant-b` | `15_…` 1곳 | sha256 앞 12자 `fa71cb139394` |
| `OBYS-CAFE24-CONSOLIDATION-PLAN-20261002` (JSON `document_key` 값만) | `acct-draft-plan` | `12_…` 24행 | |
| `OBYS-CAFE24-CONSOLIDATION-PRD-20261002` (JSON `document_key` 값만) | `acct-draft-prd` | `12_…` 31행 | |

의미 보존 확인: `15` 의 두 principal 은 별칭이 서로 다르고(`tenant-a`≠`tenant-b`, 스크립트로 UUID 비교 확인), `16` 의 `internal` 은 `15` 의 `internal` 과 같은 `tenant-a`(스크립트로 확인)이며 `05` 의 정본 3행과도 같다. 즉 "동일/다름" 비교가 유지된다. `canonical://ACCT/OBYS-…@<revision uuid>` 경로 표기는 검출되지 않아 그대로 두었다. 그래서 문서 키 원문은 `16`/`17` 에서 계속 확인된다.

**원본이 아님을 밝힌다:** 위 3종 파일과 `05` 는 이제 서버 출력의 바이트 그대로가 아니다(위 표의 치환 + 줄 끝 공백·파일 끝 빈 줄 제거: psql 패딩 때문에 `git diff --check` 가 실패했다). 값의 의미는 바꾸지 않았다.

`git diff --check`: 신규 파일별 `git diff --check --no-index /dev/null <file>` 로 전부 통과(수정 후).

## 3. 현재 실측 (읽기 전용, `default_transaction_read_only=on`) — 증거 `r6_00`, `r6_05`~`r6_08`

| 항목 | R5 당시(역사) | R6 현재 (2026-10-04 06:59 KST) |
|---|---|---|
| 전체 청크 | 28279 | **28368** |
| 정본 청크 / 임베딩 | 226 / 226 | **260 / 260** (AADS 137, ACCT 42, GO100 81) |
| 정본 max indexed_at | 2026-10-03 13:11:59 | 2026-10-03 19:10:20 +09 |
| 개정판 수 | 19 | **23** |
| head 누락·tenant/프로젝트 불일치·revision 누락·rev-head 불일치·해시 불일치·경로 불일치·stale 포인터 | 0 | **0** |
| 미색인 head / 미색인 승인·초안 revision / 고아 색인 | 0 | **0** (head 21개 모두 색인) |
| `(doc_path, chunk_index)` 중복 / 빈 청크 | 0 | **0** |
| 해시: revision 23건 `content_hash == sha256(content)`, legacy 링크 5건 일치 | — | **23/23, 5/5** |
| 청크 인덱스 연속 / 본문 50% 미만 | — | 23/23 / 0 |
| 청크 내 비밀 패턴 | 0 | **0** |
| 승인 포인터 / 최신 초안 | 5 / 14 | **5 / 18** |
| `project_document_grants` | 0 | **0** |

226 → 260 증가는 이 세션이 만든 것이 아니다(색인·쓰기 없음, 세션 DB 는 read-only). 증가분(AADS +34청크, +4 개정판/head)은 R5 이후 다른 작업이 등록한 문서로 보이나 **작성자를 확인하지 않았다.** R5 의 226 은 당시 정합 측정이므로 보고서에서 역사값으로 남긴다.

파일럿(`r6_06`): 74/92 최신=승인, 109/110 승인 v1.0.0 + 최신 초안 v1.2.0, 119(ACCT) 승인 — R5 와 같은 상태다. 파일럿 `goal_document_id` 이벤트는 0행(R5 도 같음).

릴리스(`r6_00`): `aads-server` 와 `aads-server-green` 모두 `aads-server:f20da86b056c`(digest `sha256:92e2776ba0fd…`, healthy), health-check 8100/8102/공개 모두 **200**. 배포 #5573 `success/completed` SHA `f20da86b056c`, 이미지·standby digest 동일(same_digest=true). 이후 #5577/#5581(backend, 비-이미지)·#5582/#5584(dashboard)가 성공했고 **#5585(dashboard, `build_candidate_image`)가 조사 중 실행 중**이었다 — 다른 러너의 배포이며 관여하지 않았다.

## 4. 교차 tenant 로그인·grant — 실제 API 결과와 미검증

### 4.1 운영 API 로 실제 확인한 것 (이번 실행)

| 항목 | 결과 | 증거 |
|---|---|---|
| 무인증 `GET /api/v1/project-docs/search?q=x` (공개 URL) | **401** | `r6_11_api_unauth.txt` |
| 무인증 `GET /api/v1/projects/ACCT/documents` | **401** | 〃 |

그 외 운영 API 호출은 하지 않았다. 내부 tenant 200 은 R5 에서 한 것이며 이번에 재실행하지 않았다(관리자 토큰을 새로 만들 수 없고 기존 토큰 값은 읽지 않는다).

### 4.2 미검증 (운영 API 로 확인하지 못함)

| 미검증 | 사유(확인된 사실) |
|---|---|
| **실제 다른 tenant 로그인 → 정본 비노출** | 아래 4.3 |
| **비관리자 + 프로젝트 grant 허용** | `project_document_grants` 0행. 새 grant 를 만들면 안 되므로 시도하지 않음 |
| 내부 tenant 200 / 화면 재확인 | 이번 실행에서 재실행하지 않음(R5 결과를 역사적 사실로만 인용) |
| ACCT/파일럿 119 화면 | R5 의 발견(정본 탭이 `scan` 결과로 버튼을 만들고 ACCT 는 대상이 아님)이 이번에 바뀌었는지 확인하지 않음 |

SQL(`19_sql_scope_probe_other_tenants.json`)과 유닛은 **운영 API 성공이 아니다.** R5 의 "다른 tenant 68개 × 관리자/비관리자 누출 0" 은 226행 시점의 SQL 술어 증거이며 260행 현재 상태에는 재실행하지 않았다. 따라서 이 항목들은 **미검증**이다.

### 4.3 자격증명 재조회 결과 (`r6_10`)

- 고객 tenant(`tenant-b`)에 속한 e2e 자격증명은 `e2e_credentials` 에 1건(aads-dashboard / AADS / "E2E 자동 검증용", `is_active=t`, `last_used`·`last_verified` 없음)이다. 이 한 건이 R5 의 401 자격증명이다.
- 기존 `agent_vault_credentials` 366건은 **전부 내부 tenant** 이고 다른 tenant 용은 없다. 따라서 "기존 Vault" 에 쓸 만한 다른 tenant 로그인 대안이 없다.
- 컨테이너 안에서 기존 `credential_vault.decrypt_value` 로 복호화해 비교(읽기 전용, 불리언만 출력): 복호화 성공, `saas_users` 계정 존재·active·미삭제, **저장된 비밀번호와 현재 `password_hash` 의 bcrypt 비교가 불일치**. → API 로그인은 401 이 될 것이 확실해 **호출하지 않았다**(잘못된 자격증명 반복 금지).
- 이전 판정 "고객 로그인 401" 은 **현재도 유효**하다(재조회로 원인 확인: 비밀번호 불일치). 이전 판정 "grants 0" 도 현재 0 이다.
- 확인하지 못한 것: vault 의 비밀번호가 왜 달라졌는지(누가 언제 바꿨는지), 그 계정이 tenant-b 소속인지(계정의 `default_tenant` 는 tenant-b 가 아니었으나 membership 은 조회하지 않았다).
- 하지 않은 것: 비밀번호 재설정, 새 계정/grant, `create_token`·`impersonate` 로 임의 토큰 서명, vault 행 수정.

## 5. 필요한 최소 승인안 (이 세션은 실행하지 않음)

둘 중 하나가 있어야 교차 tenant 를 운영 API 로 닫을 수 있다.

1. **자격증명 한 쌍**: tenant-b(비내부) 소속이며 현재 로그인 가능한 이메일+비밀번호를 CEO 가 vault(`e2e_credentials` 의 해당 행)에 갱신 → 그 후 `/auth/login` → `/project-docs/search` 1회 확인. 필요한 권한은 "해당 tenant member(역할 무관)" 이면 충분하다.
2. **grant 허용 검증**: 위 계정(또는 tenant-a 의 비관리자 member 한 명)에 `project_document_grants` 한 행(예: tenant-b / ACCT / read)을 승인받아 만들고, 허용 → 해제 후 차단 순서로 확인하고 **행을 되돌린다**. 승인에는 대상 계정·프로젝트·롤백(DELETE 1행)을 명시해야 한다.

대안(승인 필요): 기존 `POST /auth/admin/impersonate/{user_id}` 로 비관리자 토큰을 받는 방법이 코드에 있으나, 관리자 인증이 필요하고 사실상 토큰 발급이므로 이번 지시("임의토큰 서명 금지")에 따라 쓰지 않았다.

## 6. 정본 초안 검색 순위 — 재현 증거와 최소안 (순위 변경 안 함)

재현(R5 증거, 226행 시점): `17_api_search_pilots.json` 의 "pilot92"(`Blue/Green 무중단 배포 최적화 PRD`) 14건 중 정본 0건, GO100 `파동 리스 리비전 무결성 데이터엔진` 20건 중 정본 0건. 제목 그대로 검색하는 `18_…` 에서는 정본이 2건/1건 나온다. 가시성이 아니라 순위 문제다. **이번에 260행 상태로 재현하지 않았다.**

최소안(승인 시): 검색 응답이 정본을 별도 키(`canonical`)로 이미 구분하므로, 파일 청크 상위 N 과 별개로 정본 상위 k(예: 3)를 항상 포함하는 방식을 먼저 검토한다. 유사도 가중치 변경은 파일 검색 전체에 영향이 크므로 권하지 않는다. 이번 작업에서는 코드·순위를 바꾸지 않았다.

## 7. 이전 관리자 JWT 노출 (`r6_12`)

토큰 값은 읽거나 출력하지 않았다. 코드 근거: 토큰 만료 7일(`app/auth.py:36`), payload 에 `jti` 없음, `auth_revoked_tokens` 를 `app/` 에서 참조하지 않는다(grep 0건). 그래서 **개별 회수 수단이 없고** 추정 만료는 약 2026-10-10 14시대 KST(정확한 iat 는 확인하지 못함)다. 확실히 무효화하려면 JWT 서명키 교체(전체 세션 로그아웃)뿐이며 CEO 판단 사항이다. 이 세션은 키·자격증명을 바꾸지 않았다. 만료·회수 여부를 "안전하게 확인할 수 있는" 방법은 코드상 없다.

## 8. 오류 사전 (R-ERRBOOK)

`scripts/error_book.py register` 로 `git.gitleaks_flags_evidence_tenant_uuid` 를 등록했다(`PGHOST=` 를 비워 컨테이너 경로로 실행 — 이 호스트에 psql 이 없어 기본 경로가 `No such file 'psql'` 로 실패했다). 원인은 위 §2.3 의 재현으로 확인한 것만 적었다. fix 커밋 SHA 는 Runner 커밋 전이라 비어 있다 — **커밋 후 보완 필요.** 기존 항목 `git.gitleaks_blocks_masking_test_fixture`(재발 49회)는 테스트 픽스처 변종이고 이 건은 증거 JSON 변종이라 별도로 뒀다. 규칙(저장 전 별칭화·사전 스캔)은 문서일 뿐 코드 차단이 아니므로 **재발할 수 있다.**

## 9. 변경 파일 / 커밋 / 푸시 / 배포 — 실제 상태

- 변경: §1 표. 소스·스키마·설정·hook 변경 없음.
- **이 세션의 커밋·푸시: 하지 않았다**(규칙상 금지). R5 문서의 "커밋/푸시 하지 않음" 은 역사적 상태이며, 그 job 은 `approval_commit_failed` 로 종료되어 실제로 커밋되지 않았다. **새 커밋 영수증은 Runner 승인 후에만 생긴다**(이 문서 작성 시점에는 SHA 없음. 커밋 후 `git log -1` 과 푸시 결과로 확인해야 한다).
- hook: 이 세션은 스테이징을 하지 않으므로 pre-commit/pre-push 를 **실제로 통과시키지 못했다.** 대신 같은 검사 도구를 직접 돌렸다 — gitleaks `--redact`(§2.3, 0건), 파일별 `git diff --check --no-index`(통과). 실제 hook 결과는 Runner 커밋 단계에서만 확정된다. 이를 통과로 주장하지 않는다.
- 배포·빌드·재시작: 없음(NO_DEPLOY). 외부 알림: 없음. goal 자동 완료: 없음.
- 활성 `pipeline_jobs`: 조사 시점에 같은 파일을 다루는 다른 활성 job 없음(자기 자신 `runner-d08c33b4`만 확인).

## 10. HANDOVER 와 DB 핸드오버

- `HANDOVER.md`·`docs/HANDOVER.md` 는 TARGET_FILES 에 없고 공통 파일이라 다른 러너와 충돌하기 쉬워 **수정하지 않았다.**
- **DB `handover_write`: 기록하지 못했다.** 이 세션에 `handover_write` 도구가 없고, 인증된 `POST /api/v1/handovers` 를 쓰려면 토큰이 필요한데 임의 토큰 서명이 금지다. 직접 SQL 도 쓰지 않았다(R5 가 정상 경로만 썼던 방침 유지). Runner 또는 인증된 세션이 `entry_key=aads-canonical-search-r6-recovery-20261004`, project=AADS, entry_type=verification, status=active 로 아래 요약을 등록해야 한다.

호환 핸드오버 요약:
- R5 증거 회수: 결과 1 + 텍스트 24 + PNG 36 복구(PNG 는 git 오브젝트에서 해시 일치로 재구성). `ui_e2e_pass3/4_log.json` 2건은 미복구.
- gitleaks 오탐 5건(tenant UUID 3 + document_key 2) → 별칭화로 0건, 설정 완화 없음. 오류 사전 `git.gitleaks_flags_evidence_tenant_uuid` 등록(fix SHA 미기재).
- 현재: 전체 28368 / 정본 260 / 임베딩 260, 정합 불일치 0, grants 0, 무인증 401, 운영 f20da86b056c(8100/8102 healthy, same_digest).
- **미검증:** 실제 다른 tenant 로그인 API 비노출(고객 e2e 비밀번호 불일치로 401), grant 허용, ACCT/119 화면, 260행 상태의 SQL 교차 tenant 재측정.
- 다음: §5 의 승인(자격증명 한 쌍 또는 grant 한 행)이 있어야 닫힌다. JWT 서명키 교체는 CEO 판단.

## 11. 비용·시간

LLM·임베딩 호출 없음. 운영 API 호출은 무인증 401 2회뿐. 검색 API 의 서버측 임베딩 비용은 호출하지 않았으므로 해당 없음. **$ 는 측정하지 않았다**(로컬 읽기·gitleaks 위주, $5 게이트 대상 아님). 실측 시각 KST 06:53 시작, 증거 캡처 06:56~06:59.

## 12. 재현 명령

```
# 회수:      git apply --check --exclude='*/screens/*' .runner_archive/runner-8300c642.dirty.patch
# PNG:       git cat-file blob <패치 index 접두> > screens/<name>.png ; git hash-object 로 접두 일치 확인
# 스캔:      gitleaks dir --redact --no-banner -c .gitleaks.toml reports/20261003_canonical_search_verify_R5_evidence
# 읽기 SQL:  docker exec -i -e PGOPTIONS='-c default_transaction_read_only=on' aads-postgres psql -U aads -d aads -X < reports/20261003_canonical_search_verify_R5_evidence/0N_*.sql
# 오류 사전: PGHOST= python3 scripts/error_book.py register ...
```
