# 전 서버 인벤토리 산출물 재적재 · 재검산 · 누락 범위 검증 — RESULT

TASK_ID: AADS-PROJECT-DOCUMENTS-ALLSERVER-INVENTORY-RECOVER-20261003 · 실행 시각 2026-10-03 07:11~07:35 KST (runner-a3a15314, base `origin/main` 105acf85)

**결론: 전체 완료가 아니다.** 보존 산출물 5파일은 재적재했고 CSV 합계·접기·경로 위생은 코드로 재검산해 통과했다. 그러나 보고서의 "원본/사본" 구분은 **경로 이름 규칙뿐**이라 실제 원본 수를 부풀리고(아래 3절), 지시서가 요구한 범위 중 일부는 **미검증으로 남는다**(7절). 이 작업은 commit/push/DB/배포를 하지 않았다.

## 1. 실제 실행한 것 (분리)

| 구분 | 내용 |
|---|---|
| 보존 | 소스 5파일을 `/root/aads-preserved/project_documents_inventory_20261003/` 로 `cp -p` 후 `SHA256SUMS.txt` 기록, 이 worktree 로 재적재 뒤 `sha256sum -c` OK |
| 재검산 | `verify/recheck_csv.py`, `recheck_dups.py`, `recheck_mirror.py`, `recheck_result_md.py` (같은 보존 디렉터리, 읽기 전용, 본문 미출력) |
| 테스트 | `bash scripts/run_unit_tests.sh tests/unit/test_project_document_storage_inventory.py` → 재적재 직후 **69 passed**, 수정 후 **73 passed**. 같은 파일 + `test_project_document_m3_inventory.py` + `test_project_document_m3_legacy_verdict.py` → **106 passed** |
| 정적 검사 | `ruff check --select F821,F811` (스크립트·테스트) All checks passed · `py_compile` 통과 · `ast.parse(feature_version=(3,8))` 통과 · `scripts/dup_guard.py --paths ...` 종료 0 |
| 원격 읽기 전용 | ssh `contabo14`/`server-114`/`jinah244` 에서 `hostname`, `date`, `df -P`, 이 스크립트의 `collect`(jinah244만) 와 파일 개수·크기만 세는 coverage probe(`verify/payload.py`) 를 `nice -n 19`, `timeout` 으로 실행. contabo116 은 로컬 실행. 문서 본문·비밀 값 출력 없음, 원격 쓰기 없음 |
| 실행하지 않은 것 | **기존 전수 수집 재실행(금지)**, git add/commit/push, docker, 서비스 재시작, DB 쓰기, 정본 가져오기/승인/연결, 파일럿 74/92/109/110/119, 러너 제출·재시작 |

보존 해시(sha256, 재적재 원본):
- `scripts/project_document_storage_inventory.py` edc8b043…2e7ae → **수정 후 aecbf2e8…b34**
- `tests/unit/test_project_document_storage_inventory.py` 0b4a2188…c22 → **수정 후 8bfd7bca…c69**
- `..._storage_inventory.csv` fd045580…04e · `..._copies_by_root.csv` 893d6686…e09 · `..._RESULT.md` a725a9a8…639 (CSV 둘은 수정 없음)
- 변경 diff(스크립트+테스트 unified diff 연결) sha256 `4ec557fe…35a`

## 2. CSV 합계·접기·경로 위생 재검산 (코드)

미검증 산출물 주장으로 취급하고 다시 셌다.

| 항목 | 재계산 | 이전 보고 | 판정 |
|---|---:|---:|---|
| 기본 CSV 행 | 14,973 | 14,973 | 일치 |
| 접힌 사본 파일(1,384 집계행) | 145,844 (of_original 144,799 · snapshot 1,045) | 145,844 | 일치 |
| 합계 | 160,817 (contabo116 12,891 · contabo14 137,155 · cafe24 10,771) | 160,817 | 일치 |
| 부록 713 루트 × (CSV+집계) 파일 수·사본·스냅샷·종류 | 불일치 0, 파일 합 160,817 | — | 일치 |
| 등록 일치 행 / 서로 다른 goal id | 316 / 185 (absolute 44 · relative 272) | 316 / 185 | 일치 |
| 미등록(사본 아님) / 그 중 기획·설계·PRD 추정 | 14,657 / 1,503 | 14,657 / 1,503 | 일치 |
| 비원본 루트의 고유 미등록 | 177 (5+2+40+81+49) | 177 | 일치 |
| `path_sha256` 유일성 | 14,973 전부 유일 | 유일 | 일치 |
| 경로 위생(절대경로·`..`·제어문자·역슬래시·호스트 경로 조각·수식 접두) | **0건** | 0건 | 일치 |
| 비밀 적중 행의 `rel_path` | 전부 빈 값(0건 노출) | 0건 | 일치 |
| 접기 규칙(복사본의 원본 루트가 CSV 에 존재, 원본 루트는 접히지 않음, `sha256_group_size` ≥ 기본행 수) | 위반 0 | — | 통과 |

**검증 불가:** "비밀 적중 5,614" 는 기본 CSV 에 1,006건만 있다. 나머지 4,608건은 접힌 사본에 속해 집계 CSV 에서 복원할 수 없다. 원 수집의 raw JSONL 은 임시 디렉터리라 삭제되어 없다 → **재현하려면 전수 재수집이 필요하며 이번 범위 밖**이다.

## 3. "원본 / 미러 / 백업" 구분이 정확한가 — **부정확, 부풀림 확인**

`classify_root_kind` 는 경로 구성요소 이름만 본다(`release`, `wt-`/`go100-*`/`worktree`, `_remote_docs`, `backup`/`archive`). git 상태·내용 일치는 보지 않는다. 재검산 결과:

1. **cafe24 의 `project-docs` 계열이 전부 "original" 이다.** cafe24 original 10,763개(비어 있지 않은 것) 중 **3,065개는 다른 서버 original 과 해시가 같다**(contabo14 와 1,086 해시 공유). 예: `go100/docs` 567 중 563, `data-project-docs` 1,913 중 323, `project-docs-repo` 1,348 중 265, kis-autotrade-v4 사본 트리들. 그러면서 보고서는 cafe24 사본·스냅샷을 0 으로 적었다 — **cafe24 는 접기가 구조적으로 적용되지 않았다.**
2. **원본끼리의 중복이 접히지 않는다.** original 비어 있지 않은 14,788행의 고유 내용은 **5,457**개. 미등록 original 14,472행은 고유 **5,295**개, 그 중 기획·설계·PRD 추정은 1,435행 → 고유 **693**개. 즉 보고서의 "미등록·사본 아님 14,657" / "기획·설계·PRD 1,503" 은 **행 수**이지 고유 문서 수가 아니다. 의사결정에 쓰려면 "고유 내용 기준 약 5.3천 / 약 0.7천" 으로 읽어야 한다(둘 다 이번에 새로 센 값이며 kind 는 여전히 추정).
3. **"git 원본"이 아니다.** original 14,796행의 git 상태: committed_clean 12,046 · 커밋 안 됨 1,002(untracked 993 + modified 6 + staged 3; 서버별 contabo14 690, cafe24 293, contabo116 19) · git_error 1,612(전부 cafe24 `/home/claudebot/project-docs`, `dubious ownership`) · no_git 136 · committed_modified 6 · staged 3. "original" = 커밋된 원본이 아니라 "이름 규칙에 걸리지 않은 루트"다.
4. **백업 규칙 누락.** `*.bak.*` 이름(`/root/project-docs.bak.20260305`, jinah244 `app.bak-20261001-0737`)은 backup 으로 분류되지 않아 original 이었다(기본 CSV 10행 + jinah244 252행). `*recovery*`(contabo116 `go100_recovery_patch` 2행, cafe24 `aads-recovery` 1행)도 original 이다 — 이쪽은 의미가 모호해 규칙을 바꾸지 않았다.
5. **backup 으로 분류된 루트는 0개**, mirror 는 `_remote_docs` 1개뿐이다. `/root/backup/cli-auth-20260619_121026`(contabo14, md 3,159)은 인증 백업이라 읽지 않았고 개수만 센다.

**교정한 것(코드):** `.bak` 이름을 backup 으로 분류(테스트 3건 추가). 기존 CSV 는 옛 규칙으로 만든 값이라 **수정하지 않았다**(raw 가 없어 재생성 불가, 판정 CSV·운영 문서 DB 쓰기 금지). 영향 행은 위 10행뿐이다. **교정하지 않은 것:** cafe24/contabo14 미러 트리를 "mirror"로 재분류하는 규칙 — 내용 해시 기반 교차 서버 판정이 필요해 설계 결정 사항이다(권장: 서버 간에도 `copy_of_original` 을 적용하되 "대표 원본"을 서버 우선순위로 정한다).

## 4. 서버별 저장공간·문서 루트 실측 (2026-10-03 07:12~07:13 KST)

`df -P` 실측(문서와 무관한 파티션 포함, 부팅 파티션 제외).

| 서버 | 마운트 | 용량 | 사용 | 여유 | 사용률 |
|---|---|---:|---:|---:|---:|
| contabo116 | `/` | 197,316 MiB | 182,492 MiB | 14,808 MiB | **93%** |
| contabo14 | `/` | 395,700 MiB | 358,268 MiB | 37,417 MiB | **91%** |
| cafe24_114 | `/` | 895,593 MiB | 787,960 MiB | 98,508 MiB | **89%** |
| cafe24_114 | `/home/danharoo/www/data/files/goods/goodscode` (sdb1) | 11,354,968 MiB | 5,522,546 MiB | 5,260,094 MiB | 52% (상품 이미지 저장소, 문서 루트 아님) |
| jinah244 | `/` | 296,509 MiB | 117,310 MiB | 179,183 MiB | 40% |

문서 루트(coverage probe, 수집 시작 2026-10-03 07:13 KST). 루트는 알려진 목록 + `docs/reports/report/prd/plan/plans` 이름 발견(깊이 ≤4).

| 서버 | 루트 | 종류 | md+html(루트 안) | 어제 수집 대비 |
|---|---:|---|---:|---|
| contabo116 | 76 | original 25 · mirror 1 · worktree 41 · release 9 | 13,904 | 루트 68→76, 파일 12,891→13,904(+7.9%) |
| contabo14 | 424 | original 41 · release 155 · worktree 228 | 111,307 | 루트 566→424, 파일 137,155→111,307(−18.8%, 워크트리 삭제) |
| cafe24_114 | 79 | original 79 | 10,771 | 동일 |
| jinah244 | 5 | original 2 · backup 1 · release 2 (`.bak` 규칙 수정 후 분류) | 1,220 | 신규 |

**수집 시점 편차가 크다.** contabo14 는 하루 만에 루트 142개·파일 약 2.6만이 사라졌다. 어제 수치와 오늘 수치를 같은 표에 합산하지 말 것.

jinah244 문서 루트(전부): `/home/partner/obys/app/docs`(410), `/home/partner/obys/app/reports`(87), `/home/partner/obys/app.bak-20261001-0737/docs`(252, backup), `/opt/obys/releases/f9cc7f9c356efbd8afd89cecca536d415d412868/docs`(386), 같은 릴리스의 `reports`(85). 전체 루트 목록 JSON: 보존 디렉터리 `verify/raw/probe_*.json`.

## 5. jinah244 — 접근 가능, 기존 수집과 대조

- 지시서/기존 보고는 "ssh 공개키 거부"였으나 **2026-10-03 07:12 KST 에 `ssh jinah244`(사용자 `partner`, 기존 `~/.ssh/config` 항목)로 접속 성공**했다. 이전 사유는 낡은 것이다. 읽기 전용 `collect` 만 실행(3초), 원격 쓰기 없음.
- 1,220 파일 · 비밀 적중 42(경로 비움 대상) · 읽기 실패 0. 모든 파일이 읽혔다.
- 기존 CSV 와 대조(해시): **1,208/1,220 파일의 sha256 이 기존 CSV 기본 행에 존재**(거의 전부 contabo116, 3건은 contabo14). 고유 내용 514개 중 **11개만 기본 행에 없다**. 단 "기본 행에 없음"은 "어느 서버에도 없음"이 아니다 — 접힌 사본의 해시는 CSV 에 없으므로 이 11개는 접힌 사본에 있을 수 있다(미검증). jinah244 문서는 대부분 contabo116 OBYS 문서의 이관본이다.
- **버그 발견·수정:** jinah244 에는 stray `/.git` 이 있어 `find_git_top` 이 파일시스템 루트까지 올라가 **모든 파일이 `git_state=untracked`** 로 나왔다. 파일시스템 루트의 `.git` 은 저장소로 보지 않도록 수정(테스트 1건). 수정 후 `no_git` 1,220.
- 스크립트 `SSH_TARGETS`/`DEFAULT_SERVERS` 에 jinah244 를 추가하고 `DEFAULT_UNCOLLECTED` 를 비웠다(다음 `run` 부터 4서버 수집).
- **이관 문서 경로:** 원본 raw JSONL `verify/raw/jinah244.jsonl`(보존 디렉터리, 해시·메타만 있고 본문 없음). ACCT `runner-52a46b5e` 는 DB 조회 결과 **status=queued, phase=queued, runner_host 없음**(생성 2026-10-02 18:46:48 KST, 12시간 이상 대기). 중복 제출·재시작 하지 않았다. 이 작업은 그 러너 대신 읽기 전용 수집을 한 것이므로 **ACCT 쪽 jinah244 작업이 끝났다고 간주하지 말 것**(판정표 반영은 별도).
- 건강감시 제외(사용중단/이관대기) 상태를 바꾸지 않았다.

## 6. 누락 범위 — 읽기 전용 추가 수집 결과

| 항목 | contabo116 | contabo14 | cafe24_114 | jinah244 |
|---|---:|---:|---:|---:|
| pdf (루트 안) | 7 | 81 | 3 | 0 |
| pdf (발견 루트 밖, 아래 주 참조) | 85 | 31 | 244(**부분**) | 25 |
| md+html (루트 밖) | 28,369 | 73,625 | 55,041(**부분**) | 4,203 |
| 2MB 초과 md/html (루트 안) | 1개 / 11.1MB | 21개 / 253.3MB | 0 | 0 |
| 그 중 비밀 패턴 적중(스트리밍 검사, 청크 4KB 겹침) | 0 | 0 | 0 | 0 |

- 기본 CSV 의 `skipped_too_large` 22행 = 위 2MB 초과 22개(1+21)와 정확히 일치. 이 22개를 이번에 스트리밍으로 전체 검사해 **적중 0**이다(값 출력 없음).
- **루트 밖 문서는 범위 부족이 아니라 대부분 잡음이다.** 상위: `/root/.codex/.tmp`(도구 캐시), `/root/.codex-accounts`(인증 계정 디렉터리 — 개수만), `/home/danharoo/www`(웹 콘텐츠 md 20k·html 7k), `/srv/newtalk-v2/storage`(앱 저장소 md 16.7k), 저장소 루트의 README/CHANGELOG, 워크트리 루트 파일. 그러나 `/opt/go100/backend-releases`(contabo14, md 2,048·html 3,151), `/root/kis-autotrade-v4/.worktrees`(md 23k)·`.aads-worktrees`(13.8k) 같은 **릴리스/워크트리 안의 docs 가 아닌 위치**는 검토되지 않았다. 필요하면 별도 작업으로 대상 루트를 정해야 한다.
- 주: 루트 밖 개수는 `.git`/`node_modules`/`site-packages`/`.ssh`/`secret*` 등을 제외하고, `/root /srv /data /opt /home /var/www /mnt` 아래만 센 값이다. **cafe24 는 420초 상한에 걸려 루트 밖 개수가 부분값**이다(`outside_status=deadline`).
- 수집 후에도 CSV 는 `.md`/`.html` 전용이다. **pdf 는 여전히 인벤토리에 행이 없다**(개수만 이 표에 있다).

## 7. 미검증·한계 (명시)

1. **사본 분류 편향**: 3절. 이름 기반이며 교차 서버·git 상태를 보지 않는다. 기존 CSV 는 교정하지 않았다.
2. **프로젝트 키 귀속**: CSV 에 프로젝트 키 컬럼이 없다. 루트 별칭으로만 짐작할 수 있고 `go100/docs`, `kis-autotrade-v4/reports`, `project-docs-repo/*` 같은 트리가 어느 프로젝트에 귀속되는지 이번에 판정하지 않았다 — **미검증**.
3. **상대경로 매칭 편향**: goal_documents 상대경로(`docs/...`) 일치는 contabo116 · original · 저장소 `aads-server` 로 한정한다(272행). 그 밖의 프로젝트는 절대경로 해시(44행)만 비교한다. 따라서 "미등록"은 "판정표 204행과 이 두 규칙으로 매칭되지 않음"이지 "DB 에 없음"이 아니다. 판정표 비교 가능 행 중 미발견 19건은 분류했다: `missing_file` 8(`docs/...chart-module`, 판정표 단계에서 이미 파일 없음), `path_not_allowed` 11(서버 절대경로 7 + 외부 URL 4). 수집 누락이 아니라 판정표 쪽 사유다.
4. **kind 추정**: 파일명→제목→디렉터리명 규칙. 이번에도 표본 검수를 하지 않았다 — **미검증**.
5. **2MB 이하 비밀 검사 정확도**: 정규식 기반이며 `SECRET` 은 `canonical_documents.py` 와 동기화 테스트만 있다. 위양성/위음성 비율 미측정.
6. **수집 시점**: 기존 수집은 서버별로 순차·약 50분, 오늘 probe 와 최대 하루 차이. contabo14 는 워크트리 생성/삭제로 변동이 크다.
7. **비밀 적중 5,614 · 루트 713 · missing 2 · deadline 0** 같은 기존 수집 시점의 raw 지표는 raw 소실로 재현 불가(2절).
8. **비용**: LLM 비용·원격 CPU 시간 미측정($ 미측정).
9. 활성 runner 충돌: DB 상 `running` 은 이 러너 1건, 같은 파일명을 지시서에 가진 queued/running 작업 0건. `/tmp/aads-wt-runner-418bcf49`(pipeline-runner.sh, 신규 테스트)·`actual-model`(runner_cli_usage.py)은 다른 파일이다. `runner-ac46eda6` 는 error 상태, 그 worktree 의 5파일은 이번에 복사만 했고 원본은 그대로 둔다.

## 8. STEP 0 — 기존 구현 분류

| 대상 | 분류 | 비고 |
|---|---|---|
| `scripts/project_document_storage_inventory.py` `find_git_top` | **수정** | 파일시스템 루트 `.git` 무시 |
| 같은 파일 `classify_root_kind` | **수정** | `.bak` → backup |
| 같은 파일 `SSH_TARGETS`/`DEFAULT_SERVERS`/`DEFAULT_UNCOLLECTED` | **수정** | jinah244 접근 가능 |
| 같은 파일 나머지(collect/merge/mark_copies/split_rows/summarize/CSV) | 유지 | 재검산으로 확인 |
| `tests/unit/test_project_document_storage_inventory.py` | **수정(추가만)** | 69→73건, 기존 테스트 삭제 없음 |
| 기존 CSV 2종, 이전 RESULT.md | 유지 | 해시 불변. 판정 CSV·운영 DB 쓰기 없음 |
| `scripts/project_document_m3_inventory.py`, `canonical_documents.py`, 판정표 CSV | 유지 | 읽기만 |
| 신규 | `reports/20261003_project_documents_inventory_recovery_RESULT.md` | 이 문서 |
| 삭제 | 없음 | 호출처 영향·롤백 불필요(롤백 = 보존 디렉터리의 원본 두 파일로 복원) |

## 9. 상태 요약

- commit/push: **하지 않음**(Runner 승인 후). 배포·빌드·서비스 재시작: 없음. DB 쓰기: **없음** (읽기 SELECT 만: `pipeline_jobs` 상태 조회).
- **DB handover 기록은 미실행이다.** 이 세션에는 `handover_write` 도구도, 인증된 `POST /api/v1/handovers` 에 쓸 테넌트 자격도 없고, 서버 내부 함수를 직접 불러 인증을 우회하는 것은 하지 않았다. `HANDOVER.md` 상단 항목은 갱신했고, 같은 내용의 POST 본문(`HandoverUpsertRequest` 스키마, project_key=AADS, entry_key=작업 ID)을 `/root/aads-preserved/project_documents_inventory_20261003/handover_payload.json` 에 만들어 두었다 — Runner/CEO 가 인증 POST 로 올려야 R-001 이 충족된다.
- TODO 04614e91 갱신: 이 세션에 TODO 도구가 없어 **갱신하지 못했다**.
- 변경 파일: 스크립트·테스트 수정 2, 신규 RESULT 1, 미추적 5(재적재) — 추적되지 않는 새 파일로 존재, 기존 추적 파일 수정은 `HANDOVER.md` 만.
- 다음 후속(승인 필요): ① 교차 서버 `copy_of_original` 적용 설계, ② 필요 시 4서버 전수 재수집으로 raw 보존 + 위 수치 재생성, ③ ACCT runner-52a46b5e 처리.
