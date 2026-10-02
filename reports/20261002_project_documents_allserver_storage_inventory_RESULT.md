# 전 서버 문서 저장공간 전수 인벤토리 — RESULT

TASK_ID: AADS-PROJECT-DOCUMENTS-M3-ALLSERVER-STORAGE-INVENTORY-20261002 · 수집 시각: 2026-10-02 (KST 오후, 2회차 수집 기준)

읽기 전용 작업이다. DB 연결·쓰기 없음, 원격 파일 수정 없음, 정본 가져오기·연결 없음. 원격 서버는 `ssh ... python3 - collect` 로 스크립트를 stdin 에 흘려 실행했고 원격에는 아무것도 쓰지 않았다.

## 1. 한눈에 보기

| 항목 | 값 |
|---|---:|
| 수집한 서버 | contabo116, contabo14, cafe24_114 (jinah244 는 미수집) |
| 수집한 루트 | 713 (ok 711 / missing 2) |
| 측정 합계 (루트별 counted 합) | **160,817** |
| 행 수 (collapse 전 전체 파일) | **160,817** |
| 측정 합계 == 행 수 | **일치** |
| 기본 CSV 행 + 접힌 사본 행 | 14,973 + 145,844 = 160,817 (**일치**) |
| 기획·설계·PRD 추정 (kind ∈ prd/plan/spec/design/contract) | 21,443 |
| goal_documents 미등록 | 160,501 (등록 일치 316행) |
| 미등록이면서 사본이 아닌 것 | 14,657 |
| 미등록·사본 아님·기획/설계/PRD 추정 | **1,503** |
| 원본과 sha256 동일한 사본 (copy_of_original) | 144,799 |
| 비원본 루트끼리 동일한 스냅샷 중복 | 1,045 |
| 비밀 패턴 적중 (경로 해시만 기록) | 5,614 |
| 심볼릭 링크 건너뜀 / 읽기 실패 | 0 / 0 |

수집 실패 루트: **deadline 0건**, missing 2건, git_error 12건(아래 5절), 미수집 서버 1곳(jinah244).

goal_documents 대조: 기존 판정표 204행 중 **185행**이 서버 파일에서 발견되었고 **19행**은 어느 서버에서도 찾지 못했다(수집 범위 밖 경로이거나 삭제된 파일). 판정표 행의 원본 doc_path 는 해시로만 보존되어 있어 상대경로 행은 `public_path`, 절대경로 행은 `path_sha256` 으로 비교했다.

## 2. 서버 요약

| 서버 | 파일 수 | 기획·설계·PRD 추정 | 미등록 | 미등록(사본 제외) | 미등록 기획·설계·PRD(사본 제외) | 사본(원본 존재) | 스냅샷 중복 |
|---|---:|---:|---:|---:|---:|---:|---:|
| cafe24_114 | 10,771 | 710 | 10,771 | 10,771 | 710 | 0 | 0 |
| contabo116 | 12,891 | 4,538 | 12,600 | 1,604 | 517 | 10,968 | 28 |
| contabo14 | 137,155 | 16,195 | 137,130 | 2,282 | 276 | 133,831 | 1,017 |
| jinah244 | 미수집 | 미수집 | 미수집 | 미수집 | 미수집 | 미수집 | 미수집 |
| **합계** | 160,817 | 21,443 | 160,501 | 14,657 | 1,503 | 144,799 | 1,045 |

## 3. 서버 × 루트 종류

| 서버 | 루트 종류 | 루트 수 | 파일 수 | 기획·설계·PRD 추정 | 미등록 | 미등록(사본 제외) | 미등록 기획·설계·PRD(사본 제외) | 사본 | 스냅샷 중복 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| cafe24_114 | original | 79 | 10,771 | 710 | 10,771 | 10,771 | 710 | 0 | 0 |
| contabo116 | original | 18 | 1,848 | 777 | 1,557 | 1,557 | 492 | 0 | 0 |
| contabo116 | mirror | 1 | 2,208 | 242 | 2,208 | 5 | 0 | 2,203 | 0 |
| contabo116 | release | 6 | 1,003 | 631 | 1,003 | 2 | 2 | 1,001 | 0 |
| contabo116 | worktree | 43 | 7,832 | 2,888 | 7,832 | 40 | 23 | 7,764 | 28 |
| contabo14 | original | 12 | 2,177 | 247 | 2,152 | 2,152 | 233 | 0 | 0 |
| contabo14 | release | 140 | 46,288 | 5,497 | 46,288 | 49 | 13 | 45,859 | 380 |
| contabo14 | worktree | 414 | 88,690 | 10,451 | 88,690 | 81 | 30 | 87,972 | 637 |

해석. 사본(워크트리·릴리스 스냅샷·미러)이 전체의 약 90% 를 차지한다. 사본에서도 원본에 없는 고유 문서가 있다(미등록·사본 아님 중 비원본 루트 합계 177건: contabo116 worktree 40 + release 2 + mirror 5, contabo14 worktree 81 + release 49). 이들은 원본 저장소에 반영되지 않은 작업 중 문서일 수 있어 판정표 반영 시 우선 확인 대상이다.

## 4. 기획·설계·PRD 추정 — 미등록 파일의 kind 분포(기본 CSV 행, 사본 제외)

kind 는 파일명 → 제목 → 디렉터리명 순서의 **추정**이며 CSV 의 `kind_is_estimate=true` 로 표시했다. 우선순위는 prd > contract > spec > design > plan.

| 서버 | prd | plan | spec | design | contract | other |
|---|---:|---:|---:|---:|---:|---:|
| cafe24_114 | 9 | 387 | 61 | 252 | 1 | 10,061 |
| contabo116 | 140 | 172 | 34 | 124 | 47 | 1,087 |
| contabo14 | 73 | 81 | 16 | 95 | 11 | 2,006 |

위 표는 기본 CSV 행 중 goal_documents 미등록인 것만 센 값이다. 사본을 포함한 전체 kind 분포는 prd 4,446 · plan 7,300 · spec 2,173 · design 6,767 · contract 757 · other 139,374 (합 160,817).

## 5. 수집 실패 루트와 사유

| 서버 | 루트 | 상태 | 사유 |
|---|---|---|---|
| contabo14 | `found:root/go100-wt-minute-tail-fix-20261002/backend/docs` | missing | 발견 후 수집 전에 워크트리가 삭제됨 |
| contabo14 | `found:root/go100-wt-minute-tail-fix-20261002/backend/reports` | missing | 위와 같음 |
| cafe24_114 | `found:home/claudebot/project-docs/*` (12개 루트: aads/reports, go100/docs, go100/reports, kis-autotrade-v4/{docs,plan,report,reports}, nas-image/reports, newtalk-v2-api/reports, shared/reports, shortflow/{plans,reports}) | git_error | `git rev-parse` 가 exit 128 `detected dubious ownership` 로 거부. 파일은 정상 수집(행 유지)했으나 `git_state=git_error`. 소유자가 달라 `safe.directory` 가 막은 것이다. 원격 git 설정은 고치지 않았다(읽기 전용 원칙) |
| jinah244 | `*` | not_collected | contabo116 에서 ssh 공개키 거부(지시서 기재). 별도 ACCT 러너 작업으로 수집 |

기본 CSV 에서 `git_state=git_error` 인 행은 1,612건이며 모두 위 cafe24 `/home/claudebot/project-docs` 하위 루트에 속한다(루트 별칭 `found:home/...` 로 확인).

## 6. 검증 결과 (실제 실행)

1. `bash scripts/run_unit_tests.sh tests/unit/test_project_document_storage_inventory.py tests/unit/test_project_document_m3_inventory.py tests/unit/test_project_document_m3_legacy_verdict.py` → **102 passed** (신규 69 + 기존 33).
2. `ruff check --select F821,F811` (스크립트·테스트) → All checks passed.
3. `python3 -m py_compile scripts/project_document_storage_inventory.py` → 통과. `ast.parse(feature_version=(3,8))` 통과(원격 서버 python3 호환).
4. 전체 수집 2회 실행. 1회차(상한 1500초)는 contabo14 의 357개 루트가 deadline 에 걸려 부분 수집이었고(합 88,895행), **그 결과는 폐기**했다. 2회차(상한 3000초)는 deadline 0건으로 완료(160,817행). RESULT 의 모든 수치는 2회차다.
5. 합계 검증: `measured_total(160,817) == total_rows(160,817)`, `csv_rows(14,973) + collapsed(145,844) == measured_total`. 스크립트가 불일치 시 종료코드 1 을 내도록 되어 있고 종료코드 0 이었다.
6. CSV 위생 검사: `/root/`·`/home/`·`/srv/`·`/opt/` 로 시작하는 절대경로 0건, `sk-ant`·`api_key`·`password` 0건, 비밀 적중 행의 `rel_path` 는 모두 빈 값(0건 노출), `path_sha256` 14,973건 모두 유일. `/data/` 3건은 상대경로 `shared-lessons/data/...` 의 일부로 절대경로가 아니다.
7. 출발점 대조: 지시서의 실측 출발점과 루트별 파일 수 일치(contabo116 docs 414·reports 87·static 96·dashboard 23·aads-docs 239, contabo14 docs 975·reports 445, cafe24 go100 567·newtalk-v2-srv 184·api-repo 118·newtalk-v2 99·shortflow 133·nas-image-auto 33·server116 7·blog-automation 3).

실행하지 않은 것: DB 대조(금지), 원격 파일 수정(금지), jinah244 수집(키 거부).

## 7. STEP 0 — 기존 구현 분류

| 대상 | 분류 | 비고 |
|---|---|---|
| `scripts/project_document_m3_inventory.py` (SECRET 패턴·path_sha256 정의·판정표 생성) | 유지 | 수정 없음. `path_sha256` 정의와 `public_path` 를 읽기만 함 |
| `tests/unit/test_project_document_m3_inventory.py`, `test_project_document_m3_legacy_verdict.py` | 유지 | 수정 없음, 102건 실행에 포함해 통과 |
| `reports/20261002_project_documents_m3_legacy_verdict.csv` / `_RESULT.md` | 유지 | 입력으로만 사용 |
| `app/api/canonical_documents.py` (SECRET 정규식 원본) | 유지 | 수정 없음. 동기화 테스트로 정규식 일치 검증 |
| `scripts/sync_remote_docs.sh` | 유지 | 참고만. 미러 루트(`_remote_docs`) 위치 확인 |
| `scripts/project_document_storage_inventory.py` | 신규 | 수집·병합·CSV·요약 |
| `tests/unit/test_project_document_storage_inventory.py` | 신규 | 69건 |
| `reports/..._allserver_storage_inventory.csv` | 신규 | 14,973행 |
| `reports/..._allserver_storage_inventory_copies_by_root.csv` | 신규 | 사본 접기 집계(지시서 외 산출물, 아래 8절) |
| `reports/..._allserver_storage_inventory_RESULT.md` | 신규 | 이 문서 |
| 삭제 | 없음 | 호출처 영향·롤백 불필요 |

## 8. 변경 파일 (전부 신규, 추적되지 않는 새 파일)

- `scripts/project_document_storage_inventory.py`
- `tests/unit/test_project_document_storage_inventory.py`
- `reports/20261002_project_documents_allserver_storage_inventory.csv`
- `reports/20261002_project_documents_allserver_storage_inventory_copies_by_root.csv` — **지시서에 없던 파일.** 사유: 160,817행을 전부 한 CSV 에 쓰면 초안(55,933행)이 20MB 였던 비율로 환산해 약 57MB 이상이 되어 저장소에 부적합하다. 그래서 원본과 해시가 같은 사본·스냅샷 중복은 `서버/루트/사본종류/원본` 단위로 집계해 이 파일에 두고, 기본 CSV 에는 고유 행만 남겼다. 모든 파일은 둘 중 하나에 들어가므로(14,973 + 145,844) 행 수 검증이 성립한다. 사본을 행 단위로 모두 보려면 `--detail-copies` 로 다시 만들 수 있다.
- `reports/20261002_project_documents_allserver_storage_inventory_RESULT.md`

기존 파일 수정 0건.

## 9. 한계와 주의

- **kind 는 추정이다.** 파일명·제목 기반이라 오분류가 있다(예: `other` 로 떨어진 실제 설계서, `plan` 으로 잡힌 작업 로그). CSV 의 `kind_basis` 로 근거를 볼 수 있다.
- **goal_documents 대조는 DB 를 보지 않고** 커밋된 판정표 CSV 로 한다. 상대경로 일치는 contabo116 + original 루트 + git 저장소 `aads-server` 로 한정했다. 절대경로 일치(44행)는 서버를 구분하지 않는 `path_sha256` 일치다. DB 에 판정표 이후 추가된 행은 반영되지 않았다.
- **수집 대상은 `.md`/`.html`** 이고 심볼릭 링크는 건너뛴다(이번엔 0건). 2MB 초과 파일은 해시만 계산하고 비밀 검사는 하지 않는다(`skipped_too_large`).
- **비밀 적중 5,614건**은 `rel_path` 를 비우고 `path_sha256` 만 남겼다. 값은 어디에도 출력하지 않았다.
- **수집 시점 차이.** contabo14 는 활성 워크트리가 계속 생기고 사라진다(수집 중 2개 루트가 사라짐). 서버마다 수집 시각이 다르다(세 서버를 순차 수집, 2회차 전체 약 50분).
- jinah244 는 이 작업에서 **미수집**이다. 판정표에 반영할 때 이 서버의 문서는 빠져 있다.

## 부록. 루트별 전체 표 (713행)

`루트 별칭` 의 `found:` 접두는 자동 발견 루트(경로의 `/` 는 그대로, 선행 `/` 만 제거)다. 알려진 루트는 접두가 없다.

| 서버 | 루트 별칭 | 종류 | 상태 | 파일 수 | 기획·설계·PRD 추정 | 미등록 | 미등록(사본 제외) | 사본(원본 존재) | 스냅샷 중복 |
|---|---|---|---|---:|---:|---:|---:|---:|---:|
| cafe24_114 | `blog-automation/docs` | original | ok | 3 | 2 | 3 | 3 | 0 | 0 |
| cafe24_114 | `data-project-docs` | original | ok | 1916 | 117 | 1916 | 1916 | 0 | 0 |
| cafe24_114 | `found:home/claudebot/project-docs/aads/reports` | original | ok | 67 | 0 | 67 | 67 | 0 | 0 |
| cafe24_114 | `found:home/claudebot/project-docs/go100/docs` | original | ok | 9 | 5 | 9 | 9 | 0 | 0 |
| cafe24_114 | `found:home/claudebot/project-docs/go100/reports` | original | ok | 464 | 19 | 464 | 464 | 0 | 0 |
| cafe24_114 | `found:home/claudebot/project-docs/kis-autotrade-v4/docs` | original | ok | 8 | 2 | 8 | 8 | 0 | 0 |
| cafe24_114 | `found:home/claudebot/project-docs/kis-autotrade-v4/plan` | original | ok | 1 | 1 | 1 | 1 | 0 | 0 |
| cafe24_114 | `found:home/claudebot/project-docs/kis-autotrade-v4/report` | original | ok | 10 | 0 | 10 | 10 | 0 | 0 |
| cafe24_114 | `found:home/claudebot/project-docs/kis-autotrade-v4/reports` | original | ok | 723 | 37 | 723 | 723 | 0 | 0 |
| cafe24_114 | `found:home/claudebot/project-docs/nas-image/reports` | original | ok | 30 | 0 | 30 | 30 | 0 | 0 |
| cafe24_114 | `found:home/claudebot/project-docs/newtalk-v2-api/reports` | original | ok | 137 | 1 | 137 | 137 | 0 | 0 |
| cafe24_114 | `found:home/claudebot/project-docs/shared/reports` | original | ok | 1 | 0 | 1 | 1 | 0 | 0 |
| cafe24_114 | `found:home/claudebot/project-docs/shortflow/plans` | original | ok | 5 | 5 | 5 | 5 | 0 | 0 |
| cafe24_114 | `found:home/claudebot/project-docs/shortflow/reports` | original | ok | 157 | 4 | 157 | 157 | 0 | 0 |
| cafe24_114 | `found:home/danharoo/www/docs` | original | ok | 5 | 2 | 5 | 5 | 0 | 0 |
| cafe24_114 | `found:home/danharoo/www/reports` | original | ok | 105 | 10 | 105 | 105 | 0 | 0 |
| cafe24_114 | `found:home/newpigup3/docs` | original | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| cafe24_114 | `found:home/newpigup3/reports` | original | ok | 151 | 16 | 151 | 151 | 0 | 0 |
| cafe24_114 | `found:root/aads-recovery/m1b3-runner-ace17fff/docs` | original | ok | 1 | 0 | 1 | 1 | 0 | 0 |
| cafe24_114 | `found:root/go100/report` | original | ok | 2 | 0 | 2 | 2 | 0 | 0 |
| cafe24_114 | `found:root/go100/reports` | original | ok | 193 | 6 | 193 | 193 | 0 | 0 |
| cafe24_114 | `found:root/go100/scripts/docs` | original | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| cafe24_114 | `found:root/go100/server-116/newpigup3/reports` | original | ok | 4 | 0 | 4 | 4 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2-api-repo/project-docs-repo/go100/docs` | original | ok | 7 | 4 | 7 | 7 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2-api-repo/project-docs-repo/go100/reports` | original | ok | 204 | 14 | 204 | 204 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2-api-repo/project-docs-repo/kis-autotrade-v4/docs` | original | ok | 2 | 0 | 2 | 2 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2-api-repo/project-docs-repo/kis-autotrade-v4/plan` | original | ok | 1 | 1 | 1 | 1 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2-api-repo/project-docs-repo/kis-autotrade-v4/reports` | original | ok | 240 | 22 | 240 | 240 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2-api-repo/project-docs-repo/nas-image/reports` | original | ok | 8 | 0 | 8 | 8 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2-api-repo/project-docs-repo/newtalk-v2-api/reports` | original | ok | 87 | 1 | 87 | 87 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2-api-repo/project-docs-repo/shortflow/plans` | original | ok | 5 | 5 | 5 | 5 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2-api-repo/project-docs-repo/shortflow/reports` | original | ok | 135 | 4 | 135 | 135 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2/project-docs-repo/go100/docs` | original | ok | 7 | 4 | 7 | 7 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2/project-docs-repo/go100/reports` | original | ok | 260 | 17 | 260 | 260 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2/project-docs-repo/kis-autotrade-v4/docs` | original | ok | 7 | 2 | 7 | 7 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2/project-docs-repo/kis-autotrade-v4/plan` | original | ok | 1 | 1 | 1 | 1 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2/project-docs-repo/kis-autotrade-v4/reports` | original | ok | 295 | 26 | 295 | 295 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2/project-docs-repo/nas-image/reports` | original | ok | 9 | 0 | 9 | 9 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2/project-docs-repo/newtalk-v2-api/reports` | original | ok | 93 | 1 | 93 | 93 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2/project-docs-repo/shortflow/plans` | original | ok | 5 | 5 | 5 | 5 | 0 | 0 |
| cafe24_114 | `found:root/newtalk-v2/project-docs-repo/shortflow/reports` | original | ok | 143 | 4 | 143 | 143 | 0 | 0 |
| cafe24_114 | `found:root/project-docs.bak.20260305/nas-image/reports` | original | ok | 2 | 0 | 2 | 2 | 0 | 0 |
| cafe24_114 | `found:root/project-docs.bak.20260305/newtalk-v2-api/reports` | original | ok | 8 | 0 | 8 | 8 | 0 | 0 |
| cafe24_114 | `found:root/project-docs/aads/reports` | original | ok | 91 | 0 | 91 | 91 | 0 | 0 |
| cafe24_114 | `found:root/project-docs/go100/docs` | original | ok | 9 | 5 | 9 | 9 | 0 | 0 |
| cafe24_114 | `found:root/project-docs/go100/reports` | original | ok | 478 | 19 | 478 | 478 | 0 | 0 |
| cafe24_114 | `found:root/project-docs/kis-autotrade-v4/docs` | original | ok | 8 | 2 | 8 | 8 | 0 | 0 |
| cafe24_114 | `found:root/project-docs/kis-autotrade-v4/plan` | original | ok | 1 | 1 | 1 | 1 | 0 | 0 |
| cafe24_114 | `found:root/project-docs/kis-autotrade-v4/report` | original | ok | 10 | 0 | 10 | 10 | 0 | 0 |
| cafe24_114 | `found:root/project-docs/kis-autotrade-v4/reports` | original | ok | 769 | 37 | 769 | 769 | 0 | 0 |
| cafe24_114 | `found:root/project-docs/nas-image/reports` | original | ok | 32 | 0 | 32 | 32 | 0 | 0 |
| cafe24_114 | `found:root/project-docs/newtalk-v2-api/reports` | original | ok | 166 | 1 | 166 | 166 | 0 | 0 |
| cafe24_114 | `found:root/project-docs/shared/reports` | original | ok | 1 | 0 | 1 | 1 | 0 | 0 |
| cafe24_114 | `found:root/project-docs/shortflow/plans` | original | ok | 5 | 5 | 5 | 5 | 0 | 0 |
| cafe24_114 | `found:root/project-docs/shortflow/reports` | original | ok | 211 | 4 | 211 | 211 | 0 | 0 |
| cafe24_114 | `found:root/report` | original | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| cafe24_114 | `found:root/server116/newpigup3/reports` | original | ok | 23 | 0 | 23 | 23 | 0 | 0 |
| cafe24_114 | `found:srv/newtalk-v2/frontend/public/plans` | original | ok | 1 | 1 | 1 | 1 | 0 | 0 |
| cafe24_114 | `found:srv/newtalk-v2/frontend/public/reports` | original | ok | 34 | 26 | 34 | 34 | 0 | 0 |
| cafe24_114 | `found:srv/newtalk-v2/project-docs-repo/go100/docs` | original | ok | 7 | 4 | 7 | 7 | 0 | 0 |
| cafe24_114 | `found:srv/newtalk-v2/project-docs-repo/go100/reports` | original | ok | 264 | 17 | 264 | 264 | 0 | 0 |
| cafe24_114 | `found:srv/newtalk-v2/project-docs-repo/kis-autotrade-v4/docs` | original | ok | 7 | 2 | 7 | 7 | 0 | 0 |
| cafe24_114 | `found:srv/newtalk-v2/project-docs-repo/kis-autotrade-v4/plan` | original | ok | 1 | 1 | 1 | 1 | 0 | 0 |
| cafe24_114 | `found:srv/newtalk-v2/project-docs-repo/kis-autotrade-v4/reports` | original | ok | 369 | 30 | 369 | 369 | 0 | 0 |
| cafe24_114 | `found:srv/newtalk-v2/project-docs-repo/nas-image/reports` | original | ok | 9 | 0 | 9 | 9 | 0 | 0 |
| cafe24_114 | `found:srv/newtalk-v2/project-docs-repo/newtalk-v2-api/reports` | original | ok | 98 | 1 | 98 | 98 | 0 | 0 |
| cafe24_114 | `found:srv/newtalk-v2/project-docs-repo/shortflow/plans` | original | ok | 5 | 5 | 5 | 5 | 0 | 0 |
| cafe24_114 | `found:srv/newtalk-v2/project-docs-repo/shortflow/reports` | original | ok | 143 | 4 | 143 | 143 | 0 | 0 |
| cafe24_114 | `found:srv/newtalk-v2/public/reports` | original | ok | 6 | 5 | 6 | 6 | 0 | 0 |
| cafe24_114 | `found:srv/newtalk-v2/reports` | original | ok | 15 | 3 | 15 | 15 | 0 | 0 |
| cafe24_114 | `found:srv/newtalk-v2/src/public/reports` | original | ok | 8 | 7 | 8 | 8 | 0 | 0 |
| cafe24_114 | `go100/docs` | original | ok | 567 | 8 | 567 | 567 | 0 | 0 |
| cafe24_114 | `nas-image-auto/docs` | original | ok | 33 | 2 | 33 | 33 | 0 | 0 |
| cafe24_114 | `newtalk-v2-api-repo/docs` | original | ok | 118 | 7 | 118 | 118 | 0 | 0 |
| cafe24_114 | `newtalk-v2-srv/docs` | original | ok | 184 | 44 | 184 | 184 | 0 | 0 |
| cafe24_114 | `newtalk-v2/docs` | original | ok | 99 | 6 | 99 | 99 | 0 | 0 |
| cafe24_114 | `project-docs-repo` | original | ok | 1348 | 113 | 1348 | 1348 | 0 | 0 |
| cafe24_114 | `server116/docs` | original | ok | 7 | 0 | 7 | 7 | 0 | 0 |
| cafe24_114 | `shortflow/docs` | original | ok | 133 | 12 | 133 | 133 | 0 | 0 |
| contabo116 | `_remote_docs` | mirror | ok | 2208 | 242 | 2208 | 5 | 2203 | 0 |
| contabo116 | `aads-dashboard/docs` | original | ok | 23 | 8 | 23 | 23 | 0 | 0 |
| contabo116 | `aads-docs` | original | ok | 239 | 60 | 239 | 239 | 0 | 0 |
| contabo116 | `aads-server/app-static-reports` | original | ok | 96 | 31 | 88 | 88 | 0 | 0 |
| contabo116 | `aads-server/docs` | original | ok | 414 | 287 | 266 | 266 | 0 | 0 |
| contabo116 | `aads-server/reports` | original | ok | 87 | 27 | 86 | 86 | 0 | 0 |
| contabo116 | `found:root/aads-worktrees/aads015-authority-ledger-20261001/app/reports` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo116 | `found:root/aads-worktrees/aads015-authority-ledger-20261001/docs` | worktree | ok | 412 | 287 | 412 | 0 | 412 | 0 |
| contabo116 | `found:root/aads-worktrees/aads015-authority-ledger-20261001/reports` | worktree | ok | 87 | 27 | 87 | 0 | 87 | 0 |
| contabo116 | `found:root/aads-worktrees/authority-contract-20261001/app/reports` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo116 | `found:root/aads-worktrees/authority-contract-20261001/docs` | worktree | ok | 414 | 289 | 414 | 2 | 412 | 0 |
| contabo116 | `found:root/aads-worktrees/authority-contract-20261001/reports` | worktree | ok | 87 | 27 | 87 | 0 | 87 | 0 |
| contabo116 | `found:root/aads-worktrees/authority-p0-impl-20261001/app/reports` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo116 | `found:root/aads-worktrees/authority-p0-impl-20261001/docs` | worktree | ok | 412 | 287 | 412 | 0 | 412 | 0 |
| contabo116 | `found:root/aads-worktrees/authority-p0-impl-20261001/reports` | worktree | ok | 87 | 27 | 87 | 0 | 87 | 0 |
| contabo116 | `found:root/aads-worktrees/authority-p0-reqlocal-20261001/app/reports` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo116 | `found:root/aads-worktrees/authority-p0-reqlocal-20261001/docs` | worktree | ok | 413 | 288 | 413 | 0 | 413 | 0 |
| contabo116 | `found:root/aads-worktrees/authority-p0-reqlocal-20261001/reports` | worktree | ok | 88 | 27 | 88 | 0 | 88 | 0 |
| contabo116 | `found:root/aads-worktrees/current-authority-docs-20261001/app/reports` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo116 | `found:root/aads-worktrees/current-authority-docs-20261001/docs` | worktree | ok | 412 | 287 | 412 | 0 | 412 | 0 |
| contabo116 | `found:root/aads-worktrees/current-authority-docs-20261001/reports` | worktree | ok | 87 | 27 | 87 | 0 | 87 | 0 |
| contabo116 | `found:root/aads-worktrees/rag-contract-violation-20261001/app/reports` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo116 | `found:root/aads-worktrees/rag-contract-violation-20261001/docs` | worktree | ok | 413 | 288 | 413 | 0 | 413 | 0 |
| contabo116 | `found:root/aads-worktrees/rag-contract-violation-20261001/reports` | worktree | ok | 88 | 27 | 88 | 0 | 88 | 0 |
| contabo116 | `found:root/aads/_poc/jev-ultrafast/docs` | original | ok | 4 | 1 | 4 | 4 | 0 | 0 |
| contabo116 | `found:root/aads/aads-core/docs` | original | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo116 | `found:root/aads/aads-core/reports` | original | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo116 | `found:root/aads/aads-dashboard-unni/docs` | worktree | ok | 4 | 1 | 4 | 0 | 4 | 0 |
| contabo116 | `found:root/aads/aads-dashboard-unni/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo116 | `found:root/aads/aads-dashboard/public/reports` | original | ok | 29 | 17 | 29 | 29 | 0 | 0 |
| contabo116 | `found:root/aads/aads-dashboard/reports` | original | ok | 7 | 3 | 7 | 7 | 0 | 0 |
| contabo116 | `found:root/aads/aads-server-release-2bfdc89b/app/reports` | release | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo116 | `found:root/aads/aads-server-release-2bfdc89b/docs` | release | ok | 410 | 286 | 410 | 2 | 408 | 0 |
| contabo116 | `found:root/aads/aads-server-release-2bfdc89b/reports` | release | ok | 87 | 27 | 87 | 0 | 87 | 0 |
| contabo116 | `found:root/aads/aads-server-release-c47e8f75/app/reports` | release | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo116 | `found:root/aads/aads-server-release-c47e8f75/docs` | release | ok | 416 | 289 | 416 | 0 | 416 | 0 |
| contabo116 | `found:root/aads/aads-server-release-c47e8f75/reports` | release | ok | 88 | 27 | 88 | 0 | 88 | 0 |
| contabo116 | `found:root/aads/aads-server/app/reports` | original | ok | 1 | 1 | 1 | 1 | 0 | 0 |
| contabo116 | `found:root/aads/go100-card310-b-9HO1fh/repo/docs` | worktree | ok | 779 | 101 | 779 | 15 | 764 | 0 |
| contabo116 | `found:root/aads/go100-card310-b-9HO1fh/repo/report` | worktree | ok | 487 | 26 | 487 | 2 | 485 | 0 |
| contabo116 | `found:root/aads/go100-card310-b-9HO1fh/repo/reports` | worktree | ok | 14 | 1 | 14 | 0 | 14 | 0 |
| contabo116 | `found:root/aads/go100-phase1-direct.njt3pt/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo116 | `found:root/aads/go100-phase1-direct.njt3pt/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo116 | `found:root/aads/go100-phase1-direct.njt3pt/docs` | worktree | ok | 784 | 104 | 784 | 1 | 772 | 11 |
| contabo116 | `found:root/aads/go100-phase1-direct.njt3pt/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo116 | `found:root/aads/go100-phase1-direct.njt3pt/reports` | worktree | ok | 15 | 1 | 15 | 0 | 15 | 0 |
| contabo116 | `found:root/aads/go100-phase1-direct.njt3pt/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo116 | `found:root/aads/go100-wave-direct-20260909/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo116 | `found:root/aads/go100-wave-direct-20260909/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo116 | `found:root/aads/go100-wave-direct-20260909/docs` | worktree | ok | 755 | 86 | 755 | 7 | 739 | 9 |
| contabo116 | `found:root/aads/go100-wave-direct-20260909/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo116 | `found:root/aads/go100-wave-direct-20260909/reports` | worktree | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo116 | `found:root/aads/go100-wave-direct-20260909/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo116 | `found:root/aads/go100/backend/docs` | original | ok | 43 | 2 | 43 | 43 | 0 | 0 |
| contabo116 | `found:root/aads/go100/docs` | original | ok | 380 | 22 | 380 | 380 | 0 | 0 |
| contabo116 | `found:root/aads/go100_recovery_patch/docs` | original | ok | 2 | 0 | 2 | 2 | 0 | 0 |
| contabo116 | `found:root/aads/mirrors/aads-server/docs` | original | ok | 416 | 289 | 282 | 282 | 0 | 0 |
| contabo116 | `found:root/aads/mirrors/aads-server/reports` | original | ok | 91 | 27 | 91 | 91 | 0 | 0 |
| contabo116 | `found:root/aads/wt-obys-v41-bizdocs/app/reports` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo116 | `found:root/aads/wt-obys-v41-bizdocs/docs` | worktree | ok | 410 | 286 | 410 | 0 | 408 | 2 |
| contabo116 | `found:root/aads/wt-obys-v41-bizdocs/reports` | worktree | ok | 87 | 27 | 87 | 0 | 87 | 0 |
| contabo116 | `found:root/aads/wt/sb-direct-closeout-20260929/docs` | worktree | ok | 396 | 277 | 396 | 13 | 381 | 2 |
| contabo116 | `found:root/aads/wt/sb-direct-closeout-20260929/reports` | worktree | ok | 86 | 27 | 86 | 0 | 86 | 0 |
| contabo116 | `found:root/aads/yeoljeong-dashboard-closeout-20260722-QjK6Q4/docs` | worktree | ok | 4 | 1 | 4 | 0 | 4 | 0 |
| contabo116 | `found:root/aads/yeoljeong-dashboard-closeout-20260722-QjK6Q4/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo116 | `found:root/edusrc-p1-rebuild/reports` | original | ok | 8 | 1 | 8 | 8 | 0 | 0 |
| contabo116 | `found:root/edusrc-p1-scratch/reports` | original | ok | 8 | 1 | 8 | 8 | 0 | 0 |
| contabo116 | `found:root/go100-incremental-live-20260930/docs` | worktree | ok | 1 | 0 | 1 | 0 | 1 | 0 |
| contabo14 | `found:opt/go100/backend-releases/08a0d3694/docs` | release | ok | 903 | 157 | 903 | 14 | 889 | 0 |
| contabo14 | `found:opt/go100/backend-releases/08a0d3694/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/08a0d3694/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/backend-releases/1845c42e0/docs` | release | ok | 889 | 154 | 889 | 1 | 887 | 1 |
| contabo14 | `found:opt/go100/backend-releases/1845c42e0/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/1845c42e0/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/backend-releases/32b002351/docs` | release | ok | 901 | 156 | 901 | 0 | 889 | 12 |
| contabo14 | `found:opt/go100/backend-releases/32b002351/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/32b002351/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/backend-releases/42d741f37/docs` | release | ok | 888 | 154 | 888 | 1 | 886 | 1 |
| contabo14 | `found:opt/go100/backend-releases/42d741f37/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/42d741f37/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/backend-releases/4da82fb26/docs` | release | ok | 903 | 157 | 903 | 0 | 889 | 14 |
| contabo14 | `found:opt/go100/backend-releases/4da82fb26/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/4da82fb26/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/backend-releases/4e28cb5eb/docs` | release | ok | 885 | 153 | 885 | 0 | 883 | 2 |
| contabo14 | `found:opt/go100/backend-releases/4e28cb5eb/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/4e28cb5eb/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/backend-releases/651f3af25/docs` | release | ok | 902 | 156 | 902 | 0 | 889 | 13 |
| contabo14 | `found:opt/go100/backend-releases/651f3af25/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/651f3af25/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/backend-releases/685d78ee1/docs` | release | ok | 910 | 158 | 910 | 8 | 889 | 13 |
| contabo14 | `found:opt/go100/backend-releases/685d78ee1/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/685d78ee1/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/backend-releases/75c6b7326/docs` | release | ok | 894 | 155 | 894 | 2 | 889 | 3 |
| contabo14 | `found:opt/go100/backend-releases/75c6b7326/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/75c6b7326/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/backend-releases/7d42dbeac/docs` | release | ok | 885 | 153 | 885 | 0 | 883 | 2 |
| contabo14 | `found:opt/go100/backend-releases/7d42dbeac/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/7d42dbeac/reports` | release | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:opt/go100/backend-releases/a7115356b/docs` | release | ok | 911 | 158 | 911 | 2 | 889 | 20 |
| contabo14 | `found:opt/go100/backend-releases/a7115356b/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/a7115356b/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/backend-releases/beb34d359/docs` | release | ok | 896 | 155 | 896 | 0 | 889 | 7 |
| contabo14 | `found:opt/go100/backend-releases/beb34d359/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/beb34d359/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/backend-releases/e1e23e8c0/docs` | release | ok | 903 | 157 | 903 | 0 | 889 | 14 |
| contabo14 | `found:opt/go100/backend-releases/e1e23e8c0/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/e1e23e8c0/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/backend-releases/e6a5d7f7e/docs` | release | ok | 896 | 155 | 896 | 0 | 889 | 7 |
| contabo14 | `found:opt/go100/backend-releases/e6a5d7f7e/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/e6a5d7f7e/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/backend-releases/e7a787806/docs` | release | ok | 896 | 155 | 896 | 0 | 889 | 7 |
| contabo14 | `found:opt/go100/backend-releases/e7a787806/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/e7a787806/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/backend-releases/f3b8da5ba/docs` | release | ok | 876 | 148 | 876 | 0 | 874 | 2 |
| contabo14 | `found:opt/go100/backend-releases/f3b8da5ba/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/f3b8da5ba/reports` | release | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:opt/go100/backend-releases/f408c52b3/docs` | release | ok | 902 | 156 | 902 | 0 | 889 | 13 |
| contabo14 | `found:opt/go100/backend-releases/f408c52b3/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/f408c52b3/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/backend-releases/fe0ec465a/docs` | release | ok | 911 | 158 | 911 | 1 | 889 | 21 |
| contabo14 | `found:opt/go100/backend-releases/fe0ec465a/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/backend-releases/fe0ec465a/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/bt-canonical-p0r2-20260914/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:opt/go100/bt-canonical-p0r2-20260914/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:opt/go100/bt-canonical-p0r2-20260914/docs` | worktree | ok | 791 | 108 | 791 | 5 | 776 | 10 |
| contabo14 | `found:opt/go100/bt-canonical-p0r2-20260914/report` | worktree | ok | 487 | 26 | 487 | 0 | 487 | 0 |
| contabo14 | `found:opt/go100/bt-canonical-p0r2-20260914/reports` | worktree | ok | 15 | 1 | 15 | 0 | 15 | 0 |
| contabo14 | `found:opt/go100/bt-canonical-p0r2-20260914/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:opt/go100/card119-releases/da291646e/docs` | release | ok | 750 | 85 | 750 | 5 | 732 | 13 |
| contabo14 | `found:opt/go100/card119-releases/da291646e/report` | release | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:opt/go100/card119-releases/da291646e/reports` | release | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:opt/go100/card119-releases/e70e9c9b6/docs` | release | ok | 750 | 85 | 750 | 1 | 732 | 17 |
| contabo14 | `found:opt/go100/card119-releases/e70e9c9b6/report` | release | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:opt/go100/card119-releases/e70e9c9b6/reports` | release | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:opt/go100/card310-releases/ef1013d2d/docs` | release | ok | 769 | 93 | 769 | 2 | 753 | 14 |
| contabo14 | `found:opt/go100/card310-releases/ef1013d2d/report` | release | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:opt/go100/card310-releases/ef1013d2d/reports` | release | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:opt/go100/frontend-release-b0455d66c-20261002-083951/backend/docs` | release | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:opt/go100/frontend-release-b0455d66c-20261002-083951/backend/reports` | release | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:opt/go100/frontend-release-b0455d66c-20261002-083951/docs` | release | ok | 905 | 157 | 905 | 0 | 889 | 16 |
| contabo14 | `found:opt/go100/frontend-release-b0455d66c-20261002-083951/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/frontend-release-b0455d66c-20261002-083951/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/frontend-release-b0455d66c-20261002-083951/scripts/docs` | release | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:opt/go100/frontend-release-fdb21c6a9-20261001-111335/backend/docs` | release | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:opt/go100/frontend-release-fdb21c6a9-20261001-111335/backend/reports` | release | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:opt/go100/frontend-release-fdb21c6a9-20261001-111335/docs` | release | ok | 895 | 155 | 895 | 0 | 889 | 6 |
| contabo14 | `found:opt/go100/frontend-release-fdb21c6a9-20261001-111335/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/frontend-release-fdb21c6a9-20261001-111335/reports` | release | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/frontend-release-fdb21c6a9-20261001-111335/scripts/docs` | release | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:opt/go100/learning-fixes/dateparam-20260911/docs` | worktree | ok | 769 | 93 | 769 | 2 | 753 | 14 |
| contabo14 | `found:opt/go100/learning-fixes/dateparam-20260911/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:opt/go100/learning-fixes/dateparam-20260911/reports` | worktree | ok | 14 | 1 | 14 | 0 | 14 | 0 |
| contabo14 | `found:opt/go100/learning-releases/34960fc58/docs` | release | ok | 771 | 93 | 771 | 1 | 757 | 13 |
| contabo14 | `found:opt/go100/learning-releases/34960fc58/report` | release | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:opt/go100/learning-releases/34960fc58/reports` | release | ok | 14 | 1 | 14 | 0 | 14 | 0 |
| contabo14 | `found:opt/go100/learning-releases/410b63645/docs` | release | ok | 769 | 93 | 769 | 0 | 753 | 16 |
| contabo14 | `found:opt/go100/learning-releases/410b63645/report` | release | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:opt/go100/learning-releases/410b63645/reports` | release | ok | 14 | 1 | 14 | 0 | 14 | 0 |
| contabo14 | `found:opt/go100/learning-releases/b87a7a4cf/docs` | release | ok | 771 | 93 | 771 | 1 | 756 | 14 |
| contabo14 | `found:opt/go100/learning-releases/b87a7a4cf/report` | release | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:opt/go100/learning-releases/b87a7a4cf/reports` | release | ok | 14 | 1 | 14 | 0 | 14 | 0 |
| contabo14 | `found:opt/go100/learning-releases/b882b7444/docs` | release | ok | 769 | 93 | 769 | 1 | 753 | 15 |
| contabo14 | `found:opt/go100/learning-releases/b882b7444/report` | release | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:opt/go100/learning-releases/b882b7444/reports` | release | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:opt/go100/mtf-releases/18a526126432e68ef04348aae780bb951b29a13b/docs` | release | ok | 1 | 0 | 1 | 0 | 1 | 0 |
| contabo14 | `found:opt/go100/mtf-releases/3b47389217a93cd03b0f5f8f3788cc5f58cf0cc3/docs` | release | ok | 1 | 0 | 1 | 1 | 0 | 0 |
| contabo14 | `found:opt/go100/mtf-releases/753c643030ae9492cb1608ee5d5423c28427e5ff/docs` | release | ok | 1 | 0 | 1 | 1 | 0 | 0 |
| contabo14 | `found:opt/go100/mtf-releases/789621d87e21fab487000b0e1cd546a13ca778d7-p0-r2/docs` | release | ok | 1 | 0 | 1 | 0 | 1 | 0 |
| contabo14 | `found:opt/go100/mtf-releases/b55322b21c40d40d516d6f62c228189ccf945097/docs` | release | ok | 1 | 0 | 1 | 0 | 0 | 1 |
| contabo14 | `found:opt/go100/mtf-releases/b61a0f745a7a4c7d1efb9def374ca94279f5f50b/docs` | release | ok | 1 | 0 | 1 | 1 | 0 | 0 |
| contabo14 | `found:opt/go100/push-2bcc1947c.uj6ndC/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:opt/go100/push-2bcc1947c.uj6ndC/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:opt/go100/push-2bcc1947c.uj6ndC/docs` | worktree | ok | 720 | 66 | 720 | 3 | 711 | 6 |
| contabo14 | `found:opt/go100/push-2bcc1947c.uj6ndC/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:opt/go100/push-2bcc1947c.uj6ndC/reports` | worktree | ok | 7 | 1 | 7 | 0 | 7 | 0 |
| contabo14 | `found:opt/go100/push-2bcc1947c.uj6ndC/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:opt/go100/push-eda4da255.Nkmx9y/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:opt/go100/push-eda4da255.Nkmx9y/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:opt/go100/push-eda4da255.Nkmx9y/docs` | worktree | ok | 720 | 66 | 720 | 1 | 711 | 8 |
| contabo14 | `found:opt/go100/push-eda4da255.Nkmx9y/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:opt/go100/push-eda4da255.Nkmx9y/reports` | worktree | ok | 7 | 1 | 7 | 0 | 7 | 0 |
| contabo14 | `found:opt/go100/push-eda4da255.Nkmx9y/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:opt/go100/worktrees/bt-canonical-p0-r2-20260914/docs` | worktree | ok | 789 | 107 | 789 | 1 | 775 | 13 |
| contabo14 | `found:opt/go100/worktrees/bt-canonical-p0-r2-20260914/report` | worktree | ok | 487 | 26 | 487 | 0 | 487 | 0 |
| contabo14 | `found:opt/go100/worktrees/bt-canonical-p0-r2-20260914/reports` | worktree | ok | 15 | 1 | 15 | 0 | 15 | 0 |
| contabo14 | `found:opt/go100/worktrees/c310-ops-direct-d7276f0a7/docs` | worktree | ok | 908 | 158 | 908 | 0 | 889 | 19 |
| contabo14 | `found:opt/go100/worktrees/c310-ops-direct-d7276f0a7/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/worktrees/c310-ops-direct-d7276f0a7/reports` | worktree | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/worktrees/c310-ops-minimal-5090/docs` | worktree | ok | 905 | 157 | 905 | 0 | 889 | 16 |
| contabo14 | `found:opt/go100/worktrees/c310-ops-minimal-5090/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/worktrees/c310-ops-minimal-5090/reports` | worktree | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/worktrees/chat-2648cf77-docs/docs` | worktree | ok | 854 | 134 | 854 | 2 | 851 | 1 |
| contabo14 | `found:opt/go100/worktrees/chat-2648cf77-docs/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/worktrees/chat-2648cf77-docs/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:opt/go100/worktrees/st5-ops-direct-20260922/docs` | worktree | ok | 837 | 126 | 837 | 12 | 823 | 2 |
| contabo14 | `found:opt/go100/worktrees/st5-ops-direct-20260922/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/worktrees/st5-ops-direct-20260922/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:opt/go100/worktrees/wave-history-direct-15782/docs` | worktree | ok | 844 | 128 | 844 | 4 | 834 | 6 |
| contabo14 | `found:opt/go100/worktrees/wave-history-direct-15782/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/worktrees/wave-history-direct-15782/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:opt/go100/wt-wave-breadth-canary-20261002/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:opt/go100/wt-wave-breadth-canary-20261002/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:opt/go100/wt-wave-breadth-canary-20261002/docs` | worktree | ok | 905 | 157 | 905 | 0 | 889 | 16 |
| contabo14 | `found:opt/go100/wt-wave-breadth-canary-20261002/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/wt-wave-breadth-canary-20261002/reports` | worktree | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/wt-wave-breadth-canary-20261002/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:opt/go100/wt-wave-selord-20261001/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:opt/go100/wt-wave-selord-20261001/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:opt/go100/wt-wave-selord-20261001/docs` | worktree | ok | 901 | 156 | 901 | 0 | 889 | 12 |
| contabo14 | `found:opt/go100/wt-wave-selord-20261001/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:opt/go100/wt-wave-selord-20261001/reports` | worktree | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:opt/go100/wt-wave-selord-20261001/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/aads-bluegreen-lease-release/server/docs` | release | ok | 2 | 0 | 2 | 1 | 1 | 0 |
| contabo14 | `found:root/aads-incident-hotfix/docs` | original | ok | 2 | 1 | 2 | 2 | 0 | 0 |
| contabo14 | `found:root/go100-data-engine-phase2-bf6f/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-data-engine-phase2-bf6f/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-data-engine-phase2-bf6f/docs` | worktree | ok | 847 | 129 | 847 | 1 | 842 | 4 |
| contabo14 | `found:root/go100-data-engine-phase2-bf6f/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/go100-data-engine-phase2-bf6f/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/go100-data-engine-phase2-bf6f/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/go100-data-engine-phase2-bf6f/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-data-engine-phase2-bf6f/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/go100-push-1235/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-push-1235/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-push-1235/docs` | worktree | ok | 866 | 144 | 866 | 1 | 864 | 1 |
| contabo14 | `found:root/go100-push-1235/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/go100-push-1235/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/go100-push-1235/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/go100-push-1235/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-push-1235/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/go100-push-1307/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-push-1307/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-push-1307/docs` | worktree | ok | 866 | 144 | 866 | 0 | 864 | 2 |
| contabo14 | `found:root/go100-push-1307/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/go100-push-1307/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/go100-push-1307/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/go100-push-1307/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-push-1307/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/go100-rel-ds/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-rel-ds/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-rel-ds/docs` | worktree | ok | 833 | 125 | 833 | 1 | 819 | 13 |
| contabo14 | `found:root/go100-rel-ds/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/go100-rel-ds/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/go100-rel-ds/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/go100-rel-ds/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-rel-ds/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/go100-rel-dsr/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-rel-dsr/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-rel-dsr/docs` | worktree | ok | 833 | 125 | 833 | 0 | 819 | 14 |
| contabo14 | `found:root/go100-rel-dsr/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/go100-rel-dsr/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/go100-rel-dsr/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/go100-rel-dsr/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-rel-dsr/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/go100-release-1605/backend/docs` | release | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-release-1605/backend/reports` | release | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-release-1605/docs` | release | ok | 857 | 136 | 857 | 1 | 853 | 3 |
| contabo14 | `found:root/go100-release-1605/frontend/public/reports` | release | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/go100-release-1605/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/go100-release-1605/reports` | release | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/go100-release-1605/scripts/docs` | release | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-release-1605/server-116/newpigup3/reports` | release | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/go100-release-st5-0929/backend/docs` | release | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-release-st5-0929/backend/reports` | release | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-release-st5-0929/docs` | release | ok | 853 | 133 | 853 | 0 | 849 | 4 |
| contabo14 | `found:root/go100-release-st5-0929/frontend/public/reports` | release | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/go100-release-st5-0929/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/go100-release-st5-0929/reports` | release | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/go100-release-st5-0929/scripts/docs` | release | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-release-st5-0929/server-116/newpigup3/reports` | release | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/go100-releases/c97083096/backend/docs` | release | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-releases/c97083096/backend/reports` | release | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-releases/c97083096/docs` | release | ok | 770 | 93 | 770 | 2 | 754 | 14 |
| contabo14 | `found:root/go100-releases/c97083096/report` | release | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/go100-releases/c97083096/reports` | release | ok | 14 | 1 | 14 | 0 | 14 | 0 |
| contabo14 | `found:root/go100-releases/c97083096/scripts/docs` | release | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-releases/eecc740b5/backend/docs` | release | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-releases/eecc740b5/backend/reports` | release | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-releases/eecc740b5/docs` | release | ok | 770 | 93 | 770 | 0 | 754 | 16 |
| contabo14 | `found:root/go100-releases/eecc740b5/report` | release | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/go100-releases/eecc740b5/reports` | release | ok | 14 | 1 | 14 | 0 | 14 | 0 |
| contabo14 | `found:root/go100-releases/eecc740b5/scripts/docs` | release | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-releases/f5a8dd193/backend/docs` | release | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-releases/f5a8dd193/backend/reports` | release | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-releases/f5a8dd193/docs` | release | ok | 770 | 93 | 770 | 1 | 754 | 15 |
| contabo14 | `found:root/go100-releases/f5a8dd193/report` | release | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/go100-releases/f5a8dd193/reports` | release | ok | 14 | 1 | 14 | 0 | 14 | 0 |
| contabo14 | `found:root/go100-releases/f5a8dd193/scripts/docs` | release | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-releases/fb23e84db/backend/docs` | release | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-releases/fb23e84db/backend/reports` | release | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-releases/fb23e84db/docs` | release | ok | 770 | 93 | 770 | 1 | 754 | 15 |
| contabo14 | `found:root/go100-releases/fb23e84db/report` | release | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/go100-releases/fb23e84db/reports` | release | ok | 14 | 1 | 14 | 0 | 14 | 0 |
| contabo14 | `found:root/go100-releases/fb23e84db/scripts/docs` | release | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-wave-srccount-20261001/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-wave-srccount-20261001/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-wave-srccount-20261001/docs` | worktree | ok | 894 | 155 | 894 | 0 | 889 | 5 |
| contabo14 | `found:root/go100-wave-srccount-20261001/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/go100-wave-srccount-20261001/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/go100-wave-srccount-20261001/reports` | worktree | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:root/go100-wave-srccount-20261001/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-wave-srccount-20261001/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/go100-wave-zerohash-20261001/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-wave-zerohash-20261001/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-wave-zerohash-20261001/docs` | worktree | ok | 891 | 155 | 891 | 1 | 888 | 2 |
| contabo14 | `found:root/go100-wave-zerohash-20261001/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/go100-wave-zerohash-20261001/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/go100-wave-zerohash-20261001/reports` | worktree | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:root/go100-wave-zerohash-20261001/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-wave-zerohash-20261001/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/go100-wave-zerohash-base-20261001/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-wave-zerohash-base-20261001/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-wave-zerohash-base-20261001/docs` | worktree | ok | 890 | 155 | 890 | 0 | 888 | 2 |
| contabo14 | `found:root/go100-wave-zerohash-base-20261001/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/go100-wave-zerohash-base-20261001/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/go100-wave-zerohash-base-20261001/reports` | worktree | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:root/go100-wave-zerohash-base-20261001/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-wave-zerohash-base-20261001/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/go100-wt-c310-mlgate40/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-wt-c310-mlgate40/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-wt-c310-mlgate40/docs` | worktree | ok | 852 | 132 | 852 | 0 | 849 | 3 |
| contabo14 | `found:root/go100-wt-c310-mlgate40/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/go100-wt-c310-mlgate40/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/go100-wt-c310-mlgate40/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/go100-wt-c310-mlgate40/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-wt-c310-mlgate40/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/go100-wt-cadence-15782-20261001/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-wt-cadence-15782-20261001/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-wt-cadence-15782-20261001/docs` | worktree | ok | 902 | 156 | 902 | 0 | 889 | 13 |
| contabo14 | `found:root/go100-wt-cadence-15782-20261001/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/go100-wt-cadence-15782-20261001/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/go100-wt-cadence-15782-20261001/reports` | worktree | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:root/go100-wt-cadence-15782-20261001/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-wt-cadence-15782-20261001/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/go100-wt-minute-tail-fix-20261002/backend/docs` | worktree | missing | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-wt-minute-tail-fix-20261002/backend/reports` | worktree | missing | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-wt-selord-p0-20261001/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/go100-wt-selord-p0-20261001/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/go100-wt-selord-p0-20261001/docs` | worktree | ok | 901 | 156 | 901 | 0 | 890 | 11 |
| contabo14 | `found:root/go100-wt-selord-p0-20261001/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/go100-wt-selord-p0-20261001/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/go100-wt-selord-p0-20261001/reports` | worktree | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:root/go100-wt-selord-p0-20261001/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/go100-wt-selord-p0-20261001/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/backfill-fetch-before-release-20260922/backend/docs` | release | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/backfill-fetch-before-release-20260922/backend/reports` | release | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/backfill-fetch-before-release-20260922/docs` | release | ok | 833 | 125 | 833 | 0 | 819 | 14 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/backfill-fetch-before-release-20260922/report` | release | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/backfill-fetch-before-release-20260922/reports` | release | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/backfill-fetch-before-release-20260922/scripts/docs` | release | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c119-orderbook-20260929/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c119-orderbook-20260929/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c119-orderbook-20260929/docs` | worktree | ok | 857 | 136 | 857 | 0 | 853 | 4 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c119-orderbook-20260929/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c119-orderbook-20260929/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c119-orderbook-20260929/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c310-consume-20260929/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c310-consume-20260929/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c310-consume-20260929/docs` | worktree | ok | 854 | 134 | 854 | 0 | 851 | 3 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c310-consume-20260929/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c310-consume-20260929/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c310-consume-20260929/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c310-srcts-20260929/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c310-srcts-20260929/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c310-srcts-20260929/docs` | worktree | ok | 852 | 132 | 852 | 0 | 849 | 3 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c310-srcts-20260929/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c310-srcts-20260929/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/c310-srcts-20260929/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/chart-rt-candles-20260909/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/chart-rt-candles-20260909/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/chart-rt-candles-20260909/docs` | worktree | ok | 762 | 89 | 762 | 3 | 746 | 13 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/chart-rt-candles-20260909/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/chart-rt-candles-20260909/reports` | worktree | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/chart-rt-candles-20260909/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/chat-fallback-20260720/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/chat-fallback-20260720/docs` | worktree | ok | 666 | 45 | 666 | 7 | 656 | 3 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/chat-fallback-20260720/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/chat-fallback-20260720/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/chat-fallback-20260720/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/go100-sync-docs-20260905/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/go100-sync-docs-20260905/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/go100-sync-docs-20260905/docs` | worktree | ok | 723 | 68 | 723 | 1 | 714 | 8 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/go100-sync-docs-20260905/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/go100-sync-docs-20260905/reports` | worktree | ok | 8 | 1 | 8 | 0 | 8 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/go100-sync-docs-20260905/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/r0-deploy-check-20260929/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/r0-deploy-check-20260929/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/r0-deploy-check-20260929/docs` | worktree | ok | 852 | 132 | 852 | 0 | 849 | 3 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/r0-deploy-check-20260929/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/r0-deploy-check-20260929/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/r0-deploy-check-20260929/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-supplement-20260909/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-supplement-20260909/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-supplement-20260909/docs` | worktree | ok | 752 | 85 | 752 | 2 | 733 | 17 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-supplement-20260909/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-supplement-20260909/reports` | worktree | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-supplement-20260909/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-20260909/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-20260909/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-20260909/docs` | worktree | ok | 752 | 85 | 752 | 1 | 733 | 18 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-20260909/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-20260909/reports` | worktree | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-20260909/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-prd-20260909/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-prd-20260909/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-prd-20260909/docs` | worktree | ok | 752 | 85 | 752 | 1 | 733 | 18 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-prd-20260909/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-prd-20260909/reports` | worktree | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-prd-20260909/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-r3-20260909/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-r3-20260909/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-r3-20260909/docs` | worktree | ok | 752 | 85 | 752 | 1 | 733 | 18 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-r3-20260909/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-r3-20260909/reports` | worktree | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-r3-20260909/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-recovery/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-recovery/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-recovery/docs` | worktree | ok | 751 | 85 | 751 | 1 | 732 | 18 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-recovery/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-recovery/reports` | worktree | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-core-v4-recovery/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-integrity-15782-20260928/docs` | worktree | ok | 157 | 75 | 157 | 3 | 152 | 2 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-intraday-primary-20260909/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-intraday-primary-20260909/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-intraday-primary-20260909/docs` | worktree | ok | 752 | 85 | 752 | 1 | 733 | 18 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-intraday-primary-20260909/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-intraday-primary-20260909/reports` | worktree | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wave-intraday-primary-20260909/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we12-fix-20260929/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we12-fix-20260929/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we12-fix-20260929/docs` | worktree | ok | 852 | 132 | 852 | 0 | 849 | 3 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we12-fix-20260929/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we12-fix-20260929/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we12-fix-20260929/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we13-direct-15782-20260929/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we13-direct-15782-20260929/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we13-direct-15782-20260929/docs` | worktree | ok | 855 | 134 | 855 | 4 | 848 | 3 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we13-direct-15782-20260929/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we13-direct-15782-20260929/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we13-direct-15782-20260929/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we13-review-15782-20260929/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we13-review-15782-20260929/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we13-review-15782-20260929/docs` | worktree | ok | 855 | 134 | 855 | 2 | 848 | 5 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we13-review-15782-20260929/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we13-review-15782-20260929/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/we13-review-15782-20260929/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wp04a-fix-20260929/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wp04a-fix-20260929/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wp04a-fix-20260929/docs` | worktree | ok | 852 | 132 | 852 | 0 | 849 | 3 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wp04a-fix-20260929/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wp04a-fix-20260929/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-worktrees/wp04a-fix-20260929/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-wt-310/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-wt-310/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-wt-310/docs` | worktree | ok | 730 | 73 | 730 | 4 | 719 | 7 |
| contabo14 | `found:root/kis-autotrade-v4-wt-310/frontend/public/reports` | worktree | ok | 73 | 30 | 73 | 2 | 71 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-wt-310/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-autotrade-v4-wt-310/reports` | worktree | ok | 8 | 1 | 8 | 0 | 8 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-wt-310/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4-wt-310/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/kis-autotrade-v4/backend/docs` | original | ok | 1 | 1 | 1 | 1 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4/backend/reports` | original | ok | 11 | 0 | 11 | 11 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4/frontend/public/reports` | original | ok | 213 | 35 | 209 | 209 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4/frontend/static/reports` | original | ok | 2 | 0 | 2 | 2 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4/report` | original | ok | 519 | 26 | 518 | 518 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4/scripts/docs` | original | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4/server-116/newpigup3/reports` | original | ok | 4 | 0 | 4 | 4 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4/static/reports` | original | ok | 2 | 0 | 2 | 2 | 0 | 0 |
| contabo14 | `found:root/kis-autotrade-v4/tmp/chart-rt-ws-impl/docs` | worktree | ok | 762 | 89 | 762 | 0 | 746 | 16 |
| contabo14 | `found:root/kis-autotrade-v4/tmp/chart-rt-ws-impl/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-autotrade-v4/tmp/chart-rt-ws-impl/reports` | worktree | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:root/kis-autotrade-v4/tmp/go100-ws-preservation-c5a54709/docs` | worktree | ok | 762 | 89 | 762 | 0 | 746 | 16 |
| contabo14 | `found:root/kis-autotrade-v4/tmp/go100-ws-preservation-c5a54709/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-autotrade-v4/tmp/go100-ws-preservation-c5a54709/reports` | worktree | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:root/kis-autotrade-v4/tmp/wave-direct-15782f6e/docs` | worktree | ok | 749 | 84 | 749 | 1 | 730 | 18 |
| contabo14 | `found:root/kis-autotrade-v4/tmp/wave-direct-15782f6e/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-autotrade-v4/tmp/wave-direct-15782f6e/reports` | worktree | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:root/kis-autotrade-v4/tmp/wave-direct-push-15782f6e/docs` | worktree | ok | 750 | 85 | 750 | 0 | 732 | 18 |
| contabo14 | `found:root/kis-autotrade-v4/tmp/wave-direct-push-15782f6e/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-autotrade-v4/tmp/wave-direct-push-15782f6e/reports` | worktree | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:root/kis-autotrade-v4/tmp/wave-final-direct-15782f6e/docs` | worktree | ok | 754 | 86 | 754 | 0 | 738 | 16 |
| contabo14 | `found:root/kis-autotrade-v4/tmp/wave-final-direct-15782f6e/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-autotrade-v4/tmp/wave-final-direct-15782f6e/reports` | worktree | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:root/kis-worktrees/go100-360-prov/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/kis-worktrees/go100-360-prov/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/kis-worktrees/go100-360-prov/docs` | worktree | ok | 730 | 74 | 730 | 3 | 720 | 7 |
| contabo14 | `found:root/kis-worktrees/go100-360-prov/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/kis-worktrees/go100-360-prov/reports` | worktree | ok | 11 | 1 | 11 | 0 | 11 | 0 |
| contabo14 | `found:root/kis-worktrees/go100-360-prov/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/project-docs/kis-autotrade-v4/reports` | original | ok | 3 | 0 | 3 | 3 | 0 | 0 |
| contabo14 | `found:root/wt-card310-cplan-5d/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-card310-cplan-5d/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-card310-cplan-5d/docs` | worktree | ok | 789 | 107 | 789 | 3 | 776 | 10 |
| contabo14 | `found:root/wt-card310-cplan-5d/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 74 | 1 |
| contabo14 | `found:root/wt-card310-cplan-5d/report` | worktree | ok | 487 | 26 | 487 | 0 | 487 | 0 |
| contabo14 | `found:root/wt-card310-cplan-5d/reports` | worktree | ok | 15 | 1 | 15 | 0 | 15 | 0 |
| contabo14 | `found:root/wt-card310-cplan-5d/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-card310-cplan-5d/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-chart-rt/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-chart-rt/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-chart-rt/docs` | worktree | ok | 762 | 89 | 762 | 3 | 746 | 13 |
| contabo14 | `found:root/wt-chart-rt/frontend/public/reports` | worktree | ok | 73 | 30 | 73 | 0 | 71 | 2 |
| contabo14 | `found:root/wt-chart-rt/report` | worktree | ok | 487 | 26 | 487 | 0 | 485 | 2 |
| contabo14 | `found:root/wt-chart-rt/reports` | worktree | ok | 13 | 1 | 13 | 0 | 13 | 0 |
| contabo14 | `found:root/wt-chart-rt/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-chart-rt/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-go100-baseline-4aa5a3639/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-go100-baseline-4aa5a3639/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-go100-baseline-4aa5a3639/docs` | worktree | ok | 788 | 107 | 788 | 1 | 777 | 10 |
| contabo14 | `found:root/wt-go100-baseline-4aa5a3639/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 74 | 1 |
| contabo14 | `found:root/wt-go100-baseline-4aa5a3639/report` | worktree | ok | 487 | 26 | 487 | 0 | 487 | 0 |
| contabo14 | `found:root/wt-go100-baseline-4aa5a3639/reports` | worktree | ok | 15 | 1 | 15 | 0 | 15 | 0 |
| contabo14 | `found:root/wt-go100-baseline-4aa5a3639/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-go100-baseline-4aa5a3639/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-go100-bt-canonical-20260913/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-go100-bt-canonical-20260913/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-go100-bt-canonical-20260913/docs` | worktree | ok | 789 | 107 | 789 | 0 | 775 | 14 |
| contabo14 | `found:root/wt-go100-bt-canonical-20260913/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 74 | 1 |
| contabo14 | `found:root/wt-go100-bt-canonical-20260913/report` | worktree | ok | 487 | 26 | 487 | 0 | 487 | 0 |
| contabo14 | `found:root/wt-go100-bt-canonical-20260913/reports` | worktree | ok | 15 | 1 | 15 | 0 | 15 | 0 |
| contabo14 | `found:root/wt-go100-bt-canonical-20260913/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-go100-bt-canonical-20260913/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-go100-kis-ws-appkey-fence-0930/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-go100-kis-ws-appkey-fence-0930/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-go100-kis-ws-appkey-fence-0930/docs` | worktree | ok | 866 | 144 | 866 | 0 | 864 | 2 |
| contabo14 | `found:root/wt-go100-kis-ws-appkey-fence-0930/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/wt-go100-kis-ws-appkey-fence-0930/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/wt-go100-kis-ws-appkey-fence-0930/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/wt-go100-kis-ws-appkey-fence-0930/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-go100-kis-ws-appkey-fence-0930/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-go100-minute-venue-0930/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-go100-minute-venue-0930/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-go100-minute-venue-0930/docs` | worktree | ok | 866 | 144 | 866 | 0 | 864 | 2 |
| contabo14 | `found:root/wt-go100-minute-venue-0930/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/wt-go100-minute-venue-0930/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/wt-go100-minute-venue-0930/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/wt-go100-minute-venue-0930/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-go100-minute-venue-0930/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-go100-sec-v4r/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-go100-sec-v4r/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-go100-sec-v4r/docs` | worktree | ok | 786 | 105 | 786 | 1 | 774 | 11 |
| contabo14 | `found:root/wt-go100-sec-v4r/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 74 | 1 |
| contabo14 | `found:root/wt-go100-sec-v4r/report` | worktree | ok | 487 | 26 | 487 | 0 | 487 | 0 |
| contabo14 | `found:root/wt-go100-sec-v4r/reports` | worktree | ok | 15 | 1 | 15 | 0 | 15 | 0 |
| contabo14 | `found:root/wt-go100-sec-v4r/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-go100-sec-v4r/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-m7-r5-b/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-m7-r5-b/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-m7-r5-b/docs` | worktree | ok | 853 | 133 | 853 | 0 | 849 | 4 |
| contabo14 | `found:root/wt-m7-r5-b/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/wt-m7-r5-b/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/wt-m7-r5-b/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/wt-m7-r5-b/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-m7-r5-b/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-m7-r5/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-m7-r5/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-m7-r5/docs` | worktree | ok | 854 | 133 | 854 | 1 | 849 | 4 |
| contabo14 | `found:root/wt-m7-r5/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/wt-m7-r5/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/wt-m7-r5/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/wt-m7-r5/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-m7-r5/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-wave-base/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-wave-base/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-wave-base/docs` | worktree | ok | 909 | 158 | 909 | 1 | 889 | 19 |
| contabo14 | `found:root/wt-wave-base/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/wt-wave-base/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/wt-wave-base/reports` | worktree | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:root/wt-wave-base/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-wave-base/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-wave-r3-direct/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-wave-r3-direct/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-wave-r3-direct/docs` | worktree | ok | 910 | 158 | 910 | 0 | 889 | 21 |
| contabo14 | `found:root/wt-wave-r3-direct/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/wt-wave-r3-direct/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/wt-wave-r3-direct/reports` | worktree | ok | 19 | 1 | 19 | 0 | 19 | 0 |
| contabo14 | `found:root/wt-wave-r3-direct/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-wave-r3-direct/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-wp01-base/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-wp01-base/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-wp01-base/docs` | worktree | ok | 852 | 132 | 852 | 0 | 849 | 3 |
| contabo14 | `found:root/wt-wp01-base/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/wt-wp01-base/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/wt-wp01-base/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/wt-wp01-base/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-wp01-base/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-wp01-integ/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-wp01-integ/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-wp01-integ/docs` | worktree | ok | 852 | 132 | 852 | 0 | 849 | 3 |
| contabo14 | `found:root/wt-wp01-integ/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/wt-wp01-integ/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/wt-wp01-integ/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/wt-wp01-integ/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-wp01-integ/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-wp01-r2-opus/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-wp01-r2-opus/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-wp01-r2-opus/docs` | worktree | ok | 852 | 132 | 852 | 0 | 849 | 3 |
| contabo14 | `found:root/wt-wp01-r2-opus/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/wt-wp01-r2-opus/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/wt-wp01-r2-opus/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/wt-wp01-r2-opus/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-wp01-r2-opus/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-wp01-r2/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-wp01-r2/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-wp01-r2/docs` | worktree | ok | 852 | 132 | 852 | 0 | 849 | 3 |
| contabo14 | `found:root/wt-wp01-r2/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/wt-wp01-r2/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/wt-wp01-r2/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/wt-wp01-r2/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-wp01-r2/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-wp01-self/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-wp01-self/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-wp01-self/docs` | worktree | ok | 852 | 132 | 852 | 0 | 849 | 3 |
| contabo14 | `found:root/wt-wp01-self/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/wt-wp01-self/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/wt-wp01-self/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/wt-wp01-self/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-wp01-self/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt-wp01/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt-wp01/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt-wp01/docs` | worktree | ok | 852 | 132 | 852 | 0 | 849 | 3 |
| contabo14 | `found:root/wt-wp01/frontend/public/reports` | worktree | ok | 75 | 31 | 75 | 0 | 75 | 0 |
| contabo14 | `found:root/wt-wp01/report` | worktree | ok | 488 | 26 | 488 | 0 | 488 | 0 |
| contabo14 | `found:root/wt-wp01/reports` | worktree | ok | 18 | 1 | 18 | 0 | 18 | 0 |
| contabo14 | `found:root/wt-wp01/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `found:root/wt-wp01/server-116/newpigup3/reports` | worktree | ok | 4 | 0 | 4 | 0 | 4 | 0 |
| contabo14 | `found:root/wt/sec-db-pw-v4r-20260912/backend/docs` | worktree | ok | 1 | 1 | 1 | 0 | 1 | 0 |
| contabo14 | `found:root/wt/sec-db-pw-v4r-20260912/backend/reports` | worktree | ok | 3 | 0 | 3 | 0 | 3 | 0 |
| contabo14 | `found:root/wt/sec-db-pw-v4r-20260912/docs` | worktree | ok | 786 | 105 | 786 | 0 | 774 | 12 |
| contabo14 | `found:root/wt/sec-db-pw-v4r-20260912/report` | worktree | ok | 487 | 26 | 487 | 0 | 487 | 0 |
| contabo14 | `found:root/wt/sec-db-pw-v4r-20260912/reports` | worktree | ok | 15 | 1 | 15 | 0 | 15 | 0 |
| contabo14 | `found:root/wt/sec-db-pw-v4r-20260912/scripts/docs` | worktree | ok | 0 | 0 | 0 | 0 | 0 | 0 |
| contabo14 | `kis-autotrade-v4/docs` | original | ok | 975 | 163 | 955 | 955 | 0 | 0 |
| contabo14 | `kis-autotrade-v4/reports` | original | ok | 445 | 21 | 445 | 445 | 0 | 0 |

| 서버 | 파일 수 | 기획·설계·PRD 추정 | 미등록 | 미등록(사본 제외) | 미등록 기획·설계·PRD(사본 제외) | 사본(원본 존재) | 스냅샷 중복 |
|---|---:|---:|---:|---:|---:|---:|---:|
| cafe24_114 | 10771 | 710 | 10771 | 10771 | 710 | 0 | 0 |
| contabo116 | 12891 | 4538 | 12600 | 1604 | 517 | 10968 | 28 |
| contabo14 | 137155 | 16195 | 137130 | 2282 | 276 | 133831 | 1017 |
| **합계** | 160817 | 21443 | 160501 | 14657 | 1503 | 144799 | 1045 |
