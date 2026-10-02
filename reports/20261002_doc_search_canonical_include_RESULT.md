# 정본 문서를 문서 내용 검색 색인(doc_chunks)에 포함 — RESULT

TASK_ID: AADS-DOC-SEARCH-CANONICAL-INCLUDE-20261002
복구 TASK_ID: AADS-DOC-SEARCH-CANONICAL-RECOVER-20261003 (SUPERSEDES runner-c8469c20, approval_commit_failed)

재복구 TASK_ID: AADS-DOC-SEARCH-CANONICAL-RECOVER-R2-20261003 (SUPERSEDES runner-8559f49d, approval_commit_failed)

## 자동 재작업 라운드 1/2 (2026-10-03, runner-1b304c3c) — 배포 게이트 stale_base 교정
1. **지적: `deploy_isolated_push_state: stale_base`** — 처리함. 반려된 runner-a436812d 산출물(커밋 `e3d1edef`)의 base 는 `6fd5b531` 이었고 그 사이 origin/main 이 `8df4d4bc`(runner-5656b456 등 7개 파일 변경)까지 진행해 격리 push 의 base 가 낡았다. 재구현 없이 `git diff HEAD~1 HEAD` 패치(4파일, 758줄)를 추출해 **최신 origin/main `8df4d4bc` 의 clean worktree** 에 적용했다. `git apply --check` 결과 `HANDOVER.md` 만 상단 충돌(새 항목이 위에 쌓임)이라 나머지 3파일은 `git apply --exclude=HANDOVER.md` 로, HANDOVER 는 해당 항목만 최상단에 얹었다(기존 항목 불변). 나머지 3파일은 origin/main 쪽에서 `6fd5b531..8df4d4bc` 동안 바뀌지 않았다(`git diff --name-only` 로 확인: 겹치는 파일은 HANDOVER.md 뿐).
   - 이전 라운드 본문의 "현재 base `6fd5b531`" 은 그 시점 기록이며, 현재 base 는 `8df4d4bc` 다.
2. **재검증(이 base 위에서 다시 실제 실행)**
   - gitleaks 8.30.1: `gitleaks dir <4파일 복사본> --config .gitleaks.toml --no-banner --redact -v` → `no leaks found` (1.87 MB 스캔). allow 주석·`.gitleaksignore`·규칙 완화·전역 allow·`--no-verify` 없음.
   - `bash scripts/run_unit_tests.sh tests/unit/test_doc_index_canonical.py tests/unit/test_tools_and_pipeline.py` → **100 passed**, rc=0.
   - `ruff check --select F821,F811 scripts/index_docs.py tests/unit/test_doc_index_canonical.py` → All checks passed.
   - `git diff --check` rc=0, 신규 파일 줄끝 공백 grep 0건, `python3 scripts/dup_guard.py --paths <4파일>` rc=0.
3. **이 세션에서 하지 않은 것**: git add/commit/push(금지), pre-commit 훅 실제 통과(스테이징 불가 — Runner 승인 단계에서 훅이 돈다), 운영 DB·재색인·임베딩·배포, DB handover_write(인증 토큰 없음). commit/push 상태는 **미커밋**.

## 재복구 R2 (2026-10-03, runner-a436812d)
- **차단 원인(확인)**: runner-8559f49d 의 테스트 교정은 성공했다. 이번 차단은 **이 보고서 8행**이 원인을 설명하면서 옛 테스트의 가짜 자격증명 문자열(키 이름=값 형태)을 그대로 인용한 것이다. RuleID `generic-api-key`, Fingerprint `reports/20261002_doc_search_canonical_include_RESULT.md:generic-api-key:8`. 재현: 보존 워크트리의 3파일을 복사해 `gitleaks dir --config .gitleaks.toml` → 보고서 8행 1건 + `scripts/index_docs.py` 167행 1건(기존 주석, 아래).
- **재구현 없음**: `/tmp/aads-wt-runner-8559f49d`(HEAD `a7e3d1f8`, staged 4파일)가 살아 있어 `git diff --cached`(687줄)를 그대로 추출했다. 현재 base 는 `6fd5b531`(origin/main 은 `9ca05c58` 까지 진행, 이 4파일은 건드리지 않음). `HANDOVER.md` 만 상단 충돌이라 나머지 3파일을 `git apply --exclude=HANDOVER.md` 로 적용했고 HANDOVER 는 항목을 새로 얹었다. `.runner_archive` 폴백은 쓰지 않았다.
- **교정(이번 R2)**: (1) 보고서 8·9행에서 값이 있는 시크릿 모양 리터럴 제거 — "generic-api-key 규칙에 적중하는 키 이름=값 형태의 가짜 자격증명" 처럼 값 없이 서술. (2) `scripts/index_docs.py` 167행 기존 주석이 go100 클론 디렉터리 이름 두 개를 나열해 같은 규칙에 적중하므로(pre-commit 은 staged diff 만 스캔해 못 잡지만 파일 전체 스캔은 걸림) 이름 나열을 "go100- 또는 go100_ 접두 이름" 으로 바꿨다. 주석만 바뀌고 `_GO100_CLONE` 정규식은 그대로다. allow 주석·`.gitleaksignore`·규칙 완화·전역 allow·`--no-verify` 는 쓰지 않았다.
- **gitleaks 재스캔(실제 실행, 훅과 동일 설정 `.gitleaks.toml`, gitleaks 8.30.1)**
  - `gitleaks dir <3파일 복사본> --config .gitleaks.toml --no-banner --redact -v` → 교정 전 2건(보고서:8, index_docs.py:167) / 교정 후 최종 결과는 아래 "검증" 절.
  - 변경분 전체를 diff 로 만들어 `gitleaks stdin --config .gitleaks.toml --no-banner --redact` → `no leaks found`.

## 테넌트 경계 판정 (코드 근거, 라이브 호출 없음)
**판정: 위험 — 정본 청크는 비권한 테넌트에 반환될 수 있다.** 이 작업에서 `app/` 코드는 바꾸지 않았다.
1. `app/api/project_docs.py:1945-1950` `search_docs_semantic` 에는 `Depends(require_tenant_*)` 가 없고 `project` 선택 필터만 있다. 같은 파일의 테넌트 보호 엔드포인트(1058·1080·1183·1524·1702행)는 `require_tenant_member` 를 쓴다.
2. 전역 `jwt_auth_middleware`(`app/main.py:3797`)는 `/api/v1/project-docs/search` 를 면제하지 않는다(`_PUBLIC_READONLY_EXACT_PATHS` 는 `public-education-index` 하나뿐). 그래서 **미인증은 401** 이다. 그러나 유효한 JWT 한 장이면 어느 테넌트든 통과하고, 그 JWT 의 테넌트는 검사되지 않는다.
3. `/api/v1/auth/register`(`app/api/auth.py:150`)는 면제 경로이고 가입 즉시 **비내부 테넌트의 JWT** 를 발급한다. 즉 누구나 가입해 이 검색을 호출할 수 있다.
4. `doc_chunks` 에 tenant 칸이 없어 DB 단에서도 걸러지지 않는다(`app/services/doc_index.py` 에 tenant 언급 없음).
5. 별개 관찰(이번 범위 밖, 확인만): 미들웨어 5단계는 `x-monitor-key` 헤더가 **비어 있지 않기만 하면** 값을 검증하지 않고 통과시킨다(`app/main.py:3831`). 이 값으로 `/project-docs/search` 도 열린다. 이 줄의 의도(내부 호출)는 코드 주석에서 확인하지 못했다.

**이번 변경이 만드는 노출**: 색인기는 내부 테넌트 head 만 읽으므로 고객 테넌트 정본은 `doc_chunks` 에 들어가지 않는다 — 고객 간 유출은 만들지 않는다. 그러나 **내부 테넌트의 정본(승인본 + 미승인 초안)이 가입만 하면 누구나 검색 스니펫으로 볼 수 있게 된다.** 파일 문서(`docs/`, `reports/`)가 이미 같은 경로로 노출돼 있는 것과 같은 성질이지만, **초안**은 새로 노출되는 종류다.

**후속 작업 제안(분리)**: AADS-DOC-SEARCH-TENANT-GATE — ① `search_docs_semantic` 에 `Depends(require_tenant_member)` 추가 + `canonical://` 결과는 내부 테넌트 컨텍스트일 때만 반환(또는 `doc_chunks` 에 `tenant_id` 칸을 두고 필터), ② 그 전까지 운영 색인 단계의 허용 범위를 **승인본만**(초안 제외)으로 줄일지 CEO 결정, ③ `x-monitor-key` 비검증 통과 별도 점검. **권고: 위 ①이 들어가기 전에는 아래 운영 단계를 실행하지 않거나, 실행한다면 초안 제외·내부 승인본 한정임을 CEO 가 명시 수락한 뒤에만 한다.**

## 승인 후 운영 단계 (준비만 — 이 세션에서 실행하지 않음)
전제: 코드 머지 + 위 테넌트 판정에 대한 CEO 결정.
```
# 0) 전: 기준값 기록 (읽기 전용)
SELECT count(*) FILTER (WHERE label='정본') AS canon_chunks,
       count(*) FILTER (WHERE label<>'정본') AS file_chunks, count(*) AS total FROM doc_chunks;
SELECT count(*) AS heads, max(updated_at) FROM project_document_heads;
SELECT count(*) AS revisions FROM project_document_revisions;
SELECT count(*) AS approved_internal_heads FROM project_document_heads
 WHERE tenant_id = public.aads_internal_tenant_id() AND approved_revision_id IS NOT NULL;

# 1) 계획만 (쓰기 없음)
python3 scripts/index_docs.py index-canonical --dry-run

# 2) 실행 (doc_chunks 쓰기 — 승인 필요)
python3 scripts/index_docs.py index-canonical

# 3) 후: 같은 SELECT 재실행. file_chunks 동일, canon_chunks >= approved_internal_heads, heads/revisions/max(updated_at) 불변
# 4) 임베딩은 기존 embed 크론이 채움. 즉시 필요하면: python3 scripts/index_docs.py embed (임베딩 호출 비용 발생 — 별도 승인)
```
**파일럿 문서 노출 확인** — `goal_documents.id` 74·92·109·110·119 는 M3 파일럿 5건(`reports/20261002_project_documents_m3_legacy_pilot5_RESULT.md`)이며 정본 head 는 아래 document_key 다. 기대 `doc_path` 는 `canonical://{project}/{document_key}@{revision_id}`.

| goal_documents.id | 프로젝트 | document_key | revision_id |
|---:|---|---|---|
| 74 | AADS | `contract:b4ad294d9869` | ebda53f5-91b9-4517-a3c3-40c76540fb30 |
| 92 | AADS | `prd:e677db2cb301` | 79944e05-9c4a-4bd8-9e31-a81a8d017299 |
| 109 | AADS | `plan:7d9f483b5dba` | 6dfc2221-6219-4f75-bc11-47e505dd2b4c |
| 110 | AADS | `prd:848c81d57565` | 686c817c-c69e-41d3-a89a-0cba301e447b |
| 119 | ACCT | `spec:304b66102a6f` | c1082af2-a3ee-4222-a0c6-666502dc60ef |

```
SELECT doc_path, title, project, label, count(*) chunks, count(embedding) embedded
  FROM doc_chunks WHERE doc_path LIKE ANY (ARRAY[
   'canonical://AADS/contract:b4ad294d9869@%','canonical://AADS/prd:e677db2cb301@%',
   'canonical://AADS/plan:7d9f483b5dba@%','canonical://AADS/prd:848c81d57565@%',
   'canonical://ACCT/spec:304b66102a6f@%']) GROUP BY 1,2,3,4 ORDER BY 1;
-- 5행 기대, title 접두 [승인 v…] (승인 후 새 초안이 있으면 [초안 v…] 행이 추가로 보임), label='정본'
```
이 5건이 **내부 테넌트 소속인지는 확인하지 못했다**(head 테이블 조회 금지). 소속이 내부가 아니면 색인 대상이 아니라 위 SELECT 가 0행이어야 정상이다 — 그 경우 0행을 결함으로 보고하지 말고 `SELECT tenant_id FROM project_document_heads WHERE id IN (...)` 로 소속부터 확인한다.
검색 API(JWT 필요): `GET /api/v1/project-docs/search?q=<해당 문서 제목 일부>&project=AADS` — 결과 `path` 가 `canonical://…`, `title` 이 `[승인 v…]`/`[초안 v…]` 인지, 응답 `index.coverage_pct` 가 임베딩 진행을 반영하는지 확인. 임베딩 전에는 검색되지 않는다.
**되돌림**: `DELETE FROM doc_chunks WHERE label='정본';`(파일 청크 영향 없음, 행 수를 전후 SELECT 로 확인) + 코드 revert. 아래 "롤백" 절과 동일.

## 복구 경위 (2026-10-03)
- **보존 산출물 재사용**: `/tmp/aads-wt-runner-c8469c20` 는 없었지만 `/root/aads/aads-server/.runner_archive/runner-c8469c20.dirty.patch`(644줄, sha256 `3ee6f4426d46e638…`)가 남아 있었다. 3파일(index_docs.py 수정, 테스트·보고서 신규)이 전부 들어 있어 최신 base(`a7e3d1f8`, `scripts/index_docs.py` blob `3ae96e8e`)에 `git apply --check` 통과 후 그대로 적용했다. 재작성하지 않았다. origin/main 은 `520b5326` 까지 진행했으나 `scripts/index_docs.py` 는 base 와 동일(26a0517b 이후 변경 없음).
- **커밋 실패 원인(확인)**: 테스트 `test_secret_content_or_title_is_not_indexed` 가 소스에 적어 둔 "키 이름=값" 모양의 가짜 자격증명 문자열(키 이름 + 등호 + 12자 값)이 gitleaks 8.30.1 `generic-api-key` 규칙에 적중했다. 재현: 해당 3파일만 복사해 `gitleaks dir --config .gitleaks.toml` → 테스트 100행 1건. `index_docs.py` 쪽 `generic-api-key` 적중 1건은 이 패치가 건드리지 않은 기존 주석 줄(go100 클론 디렉터리 이름 나열)이라 pre-commit 의 staged-diff 스캔에는 잡히지 않는다(diff 만 `gitleaks stdin` 으로 스캔 → no leaks).
- **교정**: 시크릿 모양 문자열(키 이름=값 형태, 토큰 접두+값 형태)을 소스에 리터럴로 두지 않고 테스트 안에서 런타임에 조각을 이어 붙여 합성한다. 합성 문자열이 `SECRET` 에 실제로 적중함을 같은 테스트가 `assert` 로 확인하므로 탐지 회귀검증은 유지된다. 실제 비밀값 없음. `.gitleaks.toml`/`.gitleaksignore` 변경·전역 allow·인라인 allow 주석·`--no-verify` 모두 쓰지 않았다.
- **재검증(복구 후)**: 위 동일 방식 재스캔 → 테스트·보고서 파일 0건(남은 1건은 위 기존 줄), diff 스캔 no leaks, 1차 PATTERNS 9종 grep 0건.
- **충돌 확인**: 다른 `/tmp/aads-wt-runner-*` 워크트리 중 이 3파일을 수정 중인 것 없음. 공유 체크아웃 `/root/aads/aads-server` 의 `scripts/index_docs.py` 는 clean(읽기만, 건드리지 않음).
- **테넌트 경계 재확인**: `app/api/project_docs.py` `search_docs_semantic` 은 `Depends(require_tenant_*)` 가 없고 `project` 만 선택 필터한다(1946행 부근) — 아래 "보안/테넌트" 결정(내부 테넌트 head 만 색인)을 유지한다.

## 변경 파일
- `scripts/index_docs.py` — 정본 색인 단계 추가 (수정)
- `tests/unit/test_doc_index_canonical.py` — 신규 (21건)
- `reports/20261002_doc_search_canonical_include_RESULT.md` — 이 문서

`app/api/project_docs.py`, `app/api/canonical_documents.py`, 정본 테이블 스키마, search 응답 스키마는 **변경하지 않았다**.

## STEP 0 기존 구현 분류 (`scripts/index_docs.py`)
| 항목 | 분류 | 비고 |
|---|---|---|
| ROOTS / mirror 해석(`resolve_root`, `to_logical_path`) / `collect` | 유지 | 선행 runner-92e75d62 변경 그대로 |
| `chunk`, `lit`, `psql`, `record_run`, `cmd_embed`, `cmd_status`, `cmd_scan` | 유지 | 정본도 `chunk`·embed 크론 재사용 |
| `cmd_index` | 수정 | known 조회에서 `canonical://` 제외, 사라진 문서 판정을 `stale_paths(canonical=False)` 로, 종료 직전(무변경 조기반환 경로 포함)에 `run_canonical_stage` 호출 |
| 정본 색인(`pick_canonical_revisions`, `build_canonical_docs`, `index_canonical`, `stale_paths`, `canonical_chunks`, `fetch_canonical_heads`, `SECRET`) | 신규 | |
| `index-canonical [--dry-run]` 하위명령 | 신규 | `index` 도 자동 호출 |
| 삭제 | 없음 | |

`app/api/project_docs.py` / `doc_index.py` 는 읽기만 했다. search 는 `doc_chunks` 를 label 구분 없이 조회하므로 코드 변경 없이 정본이 검색된다.

## 동작
- **대상**: head 별로 `approved_revision_id`(승인본) + 최신 revision 이 승인본과 다르면 최신 초안. 승인된 적 없는 head 는 최신 초안만.
- **제외**: 보관(archived) head 전체. 보관 판정은 `approved_revision_id IS NULL` 이고 head 의 마지막 `approved/archived` 이벤트가 `archived` 인 경우(archive API 가 approved 를 NULL 로 만들기 때문).
- **label** = `'정본'` 단일. title = `[승인 v1.2.0] 제목` / `[초안 v1.3.0] 제목` (300자 절단).
- **doc_path** = `canonical://{project_key}/{document_key}@{revision_id}`, **doc_sha256** = revision `content_hash`, **project** = `head.project_key`, **server** = `server_name()`(파일 색인과 동일).
- **변경 감지**는 (sha, title) 쌍. 초안이 승인되면 revision·hash 는 같고 접두만 바뀌므로 title 도 비교해야 표시가 갱신된다.
- **prune 분리**: 파일 단계 known 조회는 `doc_path NOT LIKE 'canonical://%'`, 정본 단계 known 조회는 `label='정본' AND doc_path LIKE 'canonical://%'`. 양쪽 모두 Python 에서 `stale_paths()` 로 한 번 더 걸러 서로의 경로를 지우지 않는다. 보관·SECRET·새 승인본 전환으로 대상에서 빠진 `canonical://` 청크는 정본 단계가 정리한다.
- 정본 테이블이 없는 DB 면 정본 단계는 건너뛰고 아무것도 지우지 않는다. 정본 단계가 DB 오류로 죽으면 파일 색인은 이미 반영된 뒤이며 종료코드 2 로 드러난다.
- 정본이 짧아 `chunk()` 가 빈 목록을 주면(본문 80자 미만) 전체를 1조각으로 넣는다 — 승인 head 수 ≤ 정본 청크 수 검증이 깨지지 않게 한다.
- 임베딩은 기존 `embed` 크론이 `embedding IS NULL` 청크를 채우는 방식 그대로(정본 전용 경로 없음).

## 보안 / 테넌트 경계 (결정 사항)
- `SECRET` 은 `canonical_documents.py` 와 같은 정규식 사본(스크립트가 모든 서버 공용이라 app import 불가). 본문 또는 제목에 걸리면 그 revision 은 색인하지 않고 건수만 출력한다. `test_secret_pattern_matches_canonical_documents_module` 이 두 패턴 문자열 동일성을 검증한다.
- **노출 위험**: `doc_chunks` 에 tenant 칸이 없고, `GET /project-docs/search` 는 테넌트 권한 검사 없이 `project` 만 (선택) 필터한다. 고객 테넌트 정본을 넣으면 같은 project 값을 가진 다른 테넌트/미인증 호출자에게 그대로 노출된다.
- **조치(보수적)**: 정본 단계는 `tenant_id = public.aads_internal_tenant_id()` (내부 테넌트) head 만 읽는다. 고객 테넌트 정본은 초안·승인본 모두 색인하지 않는다. 내부 테넌트는 단일 운영자(CEO) 범위라 초안 포함을 유지했다. 고객 테넌트 정본까지 검색하려면 먼저 search 엔드포인트에 테넌트 인증/필터를 넣어야 한다(별도 작업).
- search 의 `project` 필터가 선택 사항이라, 필터 없이 검색하면 모든 프로젝트의 정본이 섞여 나온다. 이는 파일 문서와 동일한 기존 동작이다.
- Auto-RAG(`app/services/auto_rag.py`)는 현재 모든 문서에 `승인미확인` 을 고정 표시한다. 정본 청크도 이 표시를 받지만 title 접두 `[승인 vN]`/`[초안 vN]` 은 그대로 보인다. auto_rag 승격은 이 작업 범위 밖이라 건드리지 않았다.
- search 결과의 `name` 은 `basename(doc_path)` 라 정본은 `{document_key}@{revision_id}` 로 나오고, `path` 는 `canonical://...` 가상 경로라 파일로 열 수 없다. 응답 필드는 바꾸지 않았다(추가만 허용 조건). 대시보드 링크 처리는 별도 작업.

## 검증 (실제 실행)
- `bash scripts/run_unit_tests.sh tests/unit/test_doc_index_canonical.py tests/unit/test_doc_index_origin_mirror.py tests/unit/test_doc_index_pipeline.py tests/unit/test_dup_guard.py tests/unit/test_canonical_documents.py tests/unit/test_qwen3_doc_index.py` → **122 passed** (복구 후 재실행 동일)
- `bash scripts/run_unit_tests.sh tests/unit/test_doc_index_canonical.py` → **21 passed**
- `ruff check --select F821,F811 scripts/index_docs.py tests/unit/test_doc_index_canonical.py` → **All checks passed (0건)**
- `python3 scripts/dup_guard.py` → 출력 없음(차단 없음)
- 신규 테스트가 증명하는 것: 승인/초안 선택 규칙 5건, archived 제외, 가상 doc_path 형식, SECRET 제외, prune 상호 불간섭(파일 단계가 `canonical://` 를 안 지움 / 정본 단계가 파일 경로를 안 지움 / 보관 전환 시 정본 청크만 정리), 초안→승인 title 갱신, dry-run 무쓰기.
- **파일 색인 결과 불변 증명**: `test_file_chunks_identical_with_and_without_canonical_stage` — psql 대역으로 `cmd_index` 를 정본 0건/정본 있음 두 번 돌려, 정본이 아닌 label 의 INSERT 문이 완전히 동일함을 확인.
- **실행하지 않은 것**: 운영 DB 쓰기·재색인·`index-canonical` 실제 실행(금지 사항). 운영 DB 의 실제 head/revision 로 검증하는 것은 머지 후 운영 단계 몫이다.

## 운영 반영 절차 (머지 후 1회)
```
# 1) 계획만 확인 (쓰기 없음)
python3 scripts/index_docs.py index-canonical --dry-run
# 2) 정본 색인 (또는 정기 index 크론이 다음 회차에 자동 수행)
python3 scripts/index_docs.py index-canonical
# 3) 임베딩 (기존 embed 크론이 채움. 즉시 채우려면)
python3 scripts/index_docs.py embed
```

## 검증 SQL
```sql
-- 승인 head 수 (내부 테넌트, 보관 제외)
SELECT count(*) FROM project_document_heads
 WHERE tenant_id = public.aads_internal_tenant_id() AND approved_revision_id IS NOT NULL;

-- 정본 청크 (≥ 위 승인 head 수여야 함)
SELECT count(*) FROM doc_chunks WHERE label='정본';
SELECT doc_path, title, project, count(*) AS chunks, count(embedding) AS embedded
  FROM doc_chunks WHERE label='정본' GROUP BY 1,2,3 ORDER BY 1;

-- 파일 색인이 안 건드려졌는지: 정본 외 청크 수가 실행 전후 같아야 함
SELECT count(*) FROM doc_chunks WHERE label <> '정본';
```
검색 API: `GET /api/v1/project-docs/search?q=<정본 제목 일부>&project=AADS` → 결과에 `[승인 v…]` 접두 title 1건 이상. (임베딩이 채워진 뒤에 검색된다 — 응답의 `index.coverage_pct` 확인.)

## 배포 구분
- `scripts/index_docs.py` / 테스트 / 보고서만 변경. **API 코드(`app/`) 변경 없음 → API 배포·재시작 불필요.** CLI 는 머지 후 각 서버 체크아웃에서 바로 쓰인다(색인 크론이 다음 회차에 자동 수행).
- 이 작업에서 실행하지 않은 것: 운영 DB 조회·쓰기, `index-canonical` 실행, 재색인, 임베딩 호출, 빌드·배포, commit/push(Runner 승인 단계 몫).

## 승인 후 운영 검증 순서 (Runner/후속 단계)
1. 전: `SELECT count(*) FROM doc_chunks WHERE label<>'정본';` 와 `SELECT count(*) FROM doc_chunks WHERE label='정본';` 기록, 정본 불변 기준으로 `SELECT count(*), max(updated_at) FROM project_document_heads;`·`SELECT count(*) FROM project_document_revisions;` 기록.
2. `python3 scripts/index_docs.py index-canonical --dry-run` → 계획 확인(쓰기 없음).
3. `python3 scripts/index_docs.py index-canonical` → 후: 정본 외 청크 수 동일, 정본 청크 ≥ 내부 테넌트 승인 head 수, heads/revisions 수·`max(updated_at)` 불변.
4. 검색 API `GET /api/v1/project-docs/search?q=<정본 제목 일부>&project=AADS` 에 `[승인 v…]`/`[초안 v…]` 제목, 고객 테넌트 정본 0건 확인.
5. 되돌림: 아래 롤백(코드 revert + `DELETE FROM doc_chunks WHERE label='정본'`).

## 롤백
1. 코드: 이 변경을 revert (`index_docs.py`, 테스트 파일).
2. 데이터: `DELETE FROM doc_chunks WHERE label='정본';` — 정본 청크는 이 label 만 쓰므로 파일 청크는 영향 없다.
3. revert 만 하고 청크를 안 지우면 `canonical://` 청크가 검색에 남는다(구 `index` 는 known 조회에서 이를 "사라진 문서"로 지우려 하므로 다음 `index` 에서 정리되긴 하지만, 위 DELETE 를 같이 실행하는 것이 확실하다).
