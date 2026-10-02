# M3 기존 문서 전수 판정표 — RESULT

TASK_ID: AADS-PROJECT-DOCUMENTS-M3-LEGACY-VERDICT-20261002
기준: 러너 worktree = origin/main `8483cf3d` (HEAD 와 동일), 조회 시각 2026-10-02.

## 1. 변경 파일

| 파일 | 구분 |
|---|---|
| `scripts/project_document_m3_inventory.py` | 수정(`--rows`/`--output` 추가, 판정 함수 추가) |
| `tests/unit/test_project_document_m3_legacy_verdict.py` | 신규(판정 단위 테스트 20건) |
| `reports/20261002_project_documents_m3_legacy_verdict.csv` | 신규(산출물, 204행) |
| `reports/20261002_project_documents_m3_legacy_verdict_RESULT.md` | 신규(이 문서) |

지시서에 없는 파일은 건드리지 않았다. `app/api/canonical_documents.py` 등 운영 API는 수정하지 않았다(inventory API의 LIMIT 100은 그대로, 스크립트에서 전수 조회).

## 2. STEP 0 분류 (기존 구현 → 유지/수정/신규)

| 항목 | 분류 | 비고 |
|---|---|---|
| `source_hash()` | 수정 | 본문 로더 `_load_source()`로 분리, `source_hash`는 그 위에서 동일 시그니처·동일 결과 반환. 기존 테스트 전부 통과 |
| `compare()`, `inventory()`, `GOALS_SQL` 등 기존 집계 SQL, `connection_params`, `classified_error` | 유지 | 집계 출력 불변 |
| `main()` | 수정 | `--rows`, `--output` 추가. `--project-key`는 `--rows` 없이는 여전히 필수 |
| `SECRET`, `CANONICAL_KINDS`, `VERDICTS`, `VERDICT_*_SQL`, `classify_path`, `judge`, `build_verdict_rows`, `summarize_verdicts`, `rows_to_csv`, `verdict_inventory` | 신규 | 행 단위 판정 |
| 삭제 | 없음 | |

`SECRET` 정규식은 `app/api/canonical_documents.py` 와 같은 패턴을 복사했고(스크립트가 FastAPI 를 import 하지 않도록), `test_secret_pattern_matches_canonical_api_pattern` 이 두 패턴이 어긋나면 실패한다.

## 3. 검증 결과

| 검증 기준 | 결과 |
|---|---|
| 판정표 총 행 수 == `SELECT count(*) FROM goal_documents` | **204 == 204** (같은 repeatable-read 읽기 전용 트랜잭션에서 조회. 불일치면 종료코드 1) |
| 판정 7종 건수 합 == 총 행 수 | 0+125+59+8+0+12+0 = **204** |
| 새 단위 테스트 | `bash scripts/run_unit_tests.sh tests/unit/test_project_document_m3_legacy_verdict.py tests/unit/test_project_document_m3_inventory.py` → **33 passed** (신규 20 + 기존 inventory 13) |
| ruff F821/F811, compileall, `scripts/dup_guard.py` | 통과 |
| DB 쓰기 0건 | 아래 4절 |

## 4. DB 접근 — SELECT 만 사용

세션은 `default_transaction_read_only=on` + `conn.transaction(readonly=True)` 로 열었다. 사용한 SQL 은 아래 6개뿐이고 INSERT/UPDATE/DELETE/DDL 은 없다. 테스트(`test_verdict_sql_is_select_only_and_unlimited`)가 SELECT 로만 시작하고 쓰기 키워드와 LIMIT 이 없음을 검사한다.

1. `SELECT d.id, d.goal_id, g.tenant_id, g.project, d.kind, d.doc_path, d.title, d.document_key, d.version, d.status, d.is_latest, d.change_summary FROM goal_documents d LEFT JOIN goals g ON g.id=d.goal_id ORDER BY d.id` (LIMIT 없음, 테넌트 좁힘 없음)
2. `SELECT count(*) FROM goal_documents`
3. `SELECT h.tenant_id, h.project_key, h.kind, r.version, r.source_path, r.content_hash FROM project_document_heads h JOIN project_document_revisions r ON r.head_id=h.id`
4. `SELECT goal_document_id FROM project_document_legacy_links`
5, 6. (`--project-key` 지정 시에만) 위 1·2 의 `WHERE g.project=$1` 변형

`project_document_legacy_links` 는 읽기만 했다(현재 0건). 정본 가져오기·승인·연결은 실행하지 않았다. 호스트에 asyncpg 가 없어 다른 세션 venv 의 site-packages 를 `PYTHONPATH` 로 얹어 실행했다(설치·변경 없음).

## 5. 판정별 건수

| 판정 | 건수 |
|---|---:|
| ready | 0 |
| needs_canonical_import | 125 |
| path_not_allowed | 59 |
| missing_file | 8 |
| secret_detected | 0 |
| superseded | 12 |
| duplicate_key | 0 |
| **합계** | **204** |

### 프로젝트별

| 프로젝트 | ready | needs_canonical_import | path_not_allowed | missing_file | secret_detected | superseded | duplicate_key | 합계 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| AADS | 0 | 23 | 7 | 0 | 0 | 9 | 0 | 39 |
| ACCT | 0 | 102 | 8 | 0 | 0 | 0 | 0 | 110 |
| FOOD | 0 | 0 | 2 | 0 | 0 | 0 | 0 | 2 |
| GO100 | 0 | 0 | 41 | 8 | 0 | 3 | 0 | 52 |
| NTV2 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 1 |
| 합계 | 0 | 125 | 59 | 8 | 0 | 12 | 0 | 204 |

### 경로 분류 × 판정

| 경로 분류 | 건수 | 판정 분포 |
|---|---:|---|
| docs 상대 | 142 | needs_canonical_import 125 / superseded 9 / missing_file 8 |
| 서버 절대경로 | 54 | path_not_allowed 51 / superseded 3 |
| app_static | 4 | path_not_allowed 4 |
| 외부 URL | 4 | path_not_allowed 4 |
| reports 상대 | 0 | — |

## 6. 판정 규칙 (우선순위 순서, 행마다 정확히 1개)

1. **secret_detected** — 파일 본문 또는 title/document_key/change_summary 가 정본 API 의 `SECRET` 패턴에 걸림. 본문은 출력하지 않고, 메타데이터에 걸리면 `document_key` 를 `[redacted]` 로 바꾼다.
2. **superseded** — `status != 'active'` 또는 `is_latest=false` (대체됨·보관 모두 포함).
3. **path_not_allowed** — 경로가 공개 `docs/`·`reports/` 상대경로가 아님(서버 절대경로, app_static, 외부 URL, `..`/숨김 경로, 심링크로 바깥 탈출 포함).
4. **missing_file** — 허용 경로인데 파일을 쓸 수 없음. 사유에 `missing_or_unreadable` / `empty` / `too_large`(262144B 초과) / `non_utf8` 를 구분해 적는다(지시서의 7종에 해당 칸이 없어 이 판정으로 묶었다. 현재 데이터는 전부 `missing_or_unreadable`).
5. **ready** — 이미 연결됨, 또는 같은 테넌트·프로젝트에 kind·version·source_path·sha256 이 모두 같은 정본 개정이 있음(= legacy-links API 의 409 검사 3종을 통과할 조건).
6. **duplicate_key** — 같은 (project, document_key) 에 **활성 최신 행이 2개 이상**. 대체된 이전 버전까지 세면 정상 버전 계보(AADS deployment-optimization, 4행)의 최신본이 중복으로 오판되므로 전체 행 수는 `key_row_count`, 판정 기준은 `key_active_count` 로 분리했다.
7. **needs_canonical_import** — 위에 모두 해당하지 않음. 사유에 같은 kind·version 정본 개정의 존재/해시 일치 여부를 적는다. 이 두 값은 kind·version 일치일 뿐 같은 문서라는 증거가 아니다.

우선순위는 "행 자체를 연결 대상으로 볼 수 있는가"(비밀값·대체됨)를 먼저, 그다음 "연결 가능한 경로·파일인가"를 본다. 한 행에 사유가 여러 개여도 대표 판정 하나만 남으므로, 판정 열만 보고 나머지 문제가 없다고 읽으면 안 된다(예: 대체된 행은 파일이 없어도 `superseded`).

## 7. 핵심 관찰

- **ready 가 0건**이다. 현재 정본 문서 헤드 7건(GO100 3·AADS 1·ACCT 3, 개정 15건)의 개정은 `source_path` 가 비어 있고(본문 직접 가져오기), legacy-links API 는 `source_path` 일치를 요구한다. 따라서 지금은 어떤 기존 문서도 바로 연결되지 않는다. `canonical_hash_match` 가 true 인 행도 0건이다.
- **GO100 52건 중 서버 절대경로가 43건**(그중 3건은 보관 처리라 `superseded`, 40건이 `path_not_allowed`; 외부 URL 1건 추가). 서버 절대경로에는 `/root/kis-autotrade-v4/`(다른 저장소) 등이 섞여 있다. docs 상대경로 중 8건은 aads-server 저장소에 파일이 없다(id 94–99, 101, 102 — `docs/technical/chart-module/*`, `docs/CHART-FEATURE-PLAN.md`; GO100 저장소 소속으로 보이나 추정이며 확인하지 않았다). 이 프로젝트는 경로 정책(저장소 밖 문서를 어떻게 정본에 들일지) 결정이 먼저다.
- **서버 절대경로 54건**(GO100 43·ACCT 5·AADS 3·FOOD 2·NTV2 1) 중 `/root/aads/aads-server/docs/...` 처럼 이 저장소 안을 가리키는 것은 docs 상대경로로 재등록하면 판정이 바뀔 여지가 있다. 이번 판정은 현재 DB 값 기준이다.
- **ACCT 102건**이 가져오기 대기다. 이 중 68건은 같은 kind·version 의 정본 개정이 이미 있다(OBYS-CAFE24 문서 계열; AADS 도 10건). 같은 문서로 단정할 수 없으므로 문서별 확인이 필요하다.
- **정합 한계**: 서버 절대경로·URL 행은 파일을 읽지 않으므로 `secret_detected` 가 `n/a` 다(경로 미검사 62행 + 파일 없음 8행 = 70행). 이들은 어차피 `path_not_allowed`/`superseded`/`missing_file` 이라 지금 연결 대상이 아니다. `prototype` kind(2건)는 정본 허용 kind 가 아니지만 둘 다 경로 단계에서 먼저 걸린다.
- 비밀값 패턴이 걸린 연결 후보는 0건이다. 참고로 저장소 `docs/` 의 md 406개 중 같은 패턴에 걸리는 파일이 14개 있으나 goal_documents 에 연결된 문서는 아니다.

## 8. 파일럿 연결 후보 5건

기준: `needs_canonical_import`, 활성 최신, 파일 존재, 비밀값 없음, 크기 작음(검토 비용 낮음), 검증하려는 시나리오가 서로 다름. 한꺼번에 연결하지 않고 이 5건을 먼저 문서별로 검증한다.

| # | goal_documents.id | 프로젝트 | kind·version | 크기 | 추천 사유 / 검증 포인트 |
|---|---:|---|---|---:|---|
| 1 | 92 | AADS | prd 1.3.0 | 1,446B | deployment-optimization 버전 계보(1.0.0~1.2.0 은 superseded)의 최신본. 계보에서 최신 하나만 연결되고 이전 버전은 건드리지 않는지 확인 |
| 2 | 109 | AADS | plan 1.0.0 | 2,493B | 같은 kind·version 정본이 없는 가장 단순한 사례. 기준선(가져오기→연결 전 과정) 검증용 |
| 3 | 110 | AADS | prd 1.0.0 | 1,673B | #109 와 같은 OHVIS 문서 세트의 PRD. AADS 에 prd 1.0.0 정본(`aads-current-authority-context`)이 이미 있어, kind·version 만 같고 다른 문서인 경우를 오인 없이 처리하는지 확인 |
| 4 | 74 | AADS | contract 1.0.0 | 8,970B | prd/plan 이 아닌 kind. 정본 허용 kind 전환과 중간 크기 문서 검증 |
| 5 | 119 | ACCT | spec 1.0.0 | 985B | 102건이 몰린 ACCT 계열의 대표. `spec` 은 ACCT 정본에 없고 문서가 가장 작아 대량 이관 전 반복 절차를 시험하기에 적합 |

제외 이유: 서버 절대경로·URL 행은 경로 정책이 먼저고, ACCT `tasks` 계열은 360B 안팎의 짧은 문서라 검증 가치가 낮으며, 49KB 급 PRD(id 214 등)는 첫 파일럿으로는 검토 비용이 크다.

## 9. 재현

```
PYTHONPATH=<asyncpg 가 있는 site-packages> python3 scripts/project_document_m3_inventory.py \
    --rows --output reports/20261002_project_documents_m3_legacy_verdict.csv
```

표준출력에 집계 JSON 이 나오고 `total_matches_db`/`verdict_sum_matches_total` 이 둘 다 true 여야 종료코드 0 이다. `--project-key AADS` 로 프로젝트별 조회도 된다. CSV 에는 본문·토큰·서버 절대경로가 없다(`/root/`·`http` 문자열 0건 확인). docs 상대경로는 `public_path` 로 노출하고, 그 외 경로는 `path_class` 와 `path_sha256` 만 남긴다.

CI/빌드 대상: 해당 없음(스크립트·테스트·리포트만 변경, `npm`/`docker build` 실행 안 함).
