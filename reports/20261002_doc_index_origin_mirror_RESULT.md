# 문서 색인 원본을 origin/main 전용 미러로 전환 — RESULT

TASK_ID: AADS-DOC-INDEX-ORIGIN-MIRROR-20261002

## 1. 한 일

| 파일 | 구분 | 내용 |
|---|---|---|
| `scripts/refresh_docs_mirror.sh` | 신규 | 미러를 `clone --no-checkout` / `fetch origin main` → `reset --hard origin/main` → `clean -fdq` 로 갱신. 경로 가드·flock·timeout·마지막 성공 SHA 로그. |
| `scripts/index_docs.py` | 수정 | `/root/aads/aads-server/{docs,reports}` 를 **미러에서 읽고**, `doc_chunks.doc_path` 는 기존 논리 경로로 되돌려 저장. 미러가 없으면 작업트리로 폴백 + stderr 경고. |
| `tests/unit/test_doc_index_origin_mirror.py` | 신규 | ROOTS 해석(미러 있음/없음), 식별 경로 불변, 스크립트 동작(15건). |
| `reports/20261002_doc_index_origin_mirror_RESULT.md` | 신규 | 이 문서. |

STEP 0 분류 (`scripts/index_docs.py`):

- 유지: `ROOTS` 목록 전체(항목·순서·라벨), `excluded()`, `chunk()`, `cmd_index/scan/embed/status`, `PG_PASSWORD`(45행, 범위 밖), DB 접점(`doc_chunks`, `doc_index_runs`).
- 수정: `collect()` — 읽기 경로만 `resolve_root()` 로 바꾸고 저장 경로는 `to_logical_path()` 로 복원. `excluded()` 는 논리 경로 기준이라 제외 규칙도 동일.
- 신규: `LIVE_TREE`, `MIRROR_SUBDIRS`, `mirror_dir()`, `mirror_ready()`, `resolve_root()`, `to_logical_path()`.
- 삭제: 없음.

미러 대상은 지시서대로 `aads-server/docs`·`aads-server/reports` 두 곳뿐이다. 같은 작업트리의 `AGENTS.md`·`CLAUDE.md`·`app/static/reports` 는 범위 밖이라 그대로 작업트리를 읽는다(같은 지연 문제가 있을 수 있으므로 필요하면 후속 작업).

설계 요점:

- `doc_path` 불변: `cmd_index` 는 `doc_path`+`doc_sha256` 로 변경 여부를 판단한다. 경로가 바뀌면 전 문서가 새로 INSERT 되고 옛 행은 "사라진 문서"로 삭제된다. 그래서 **읽는 위치만** 바꾼다. 테스트 `test_identity_path_is_unchanged_by_mirror`, `test_collect_reads_mirror_but_stores_logical_path`, `test_collect_mirror_and_fallback_yield_same_keys` 가 증명한다.
- 미러 판정: `<미러>/.git` 이 있고 `<미러>/docs` 가 디렉터리일 때만 사용(`mirror_ready`). 미러 위치는 `AADS_DOCS_MIRROR`(기본 `/root/aads/mirrors/aads-server`).
- 가드: `refresh_docs_mirror.sh` 는 미러 경로가 `/root/aads/aads-server` 와 같거나 포함 관계면 exit 2. 미러에 독립 `.git` 디렉터리가 없거나 toplevel 이 미러와 다르면 reset/clean 거부(부모 저장소를 때리는 사고 방지). clone 원본 URL 은 `DOCS_MIRROR_ORIGIN_URL`, 없으면 작업트리 `.git/config` 를 **텍스트로** 읽는다(그 트리에 git 명령을 실행하지 않는다). 새 시크릿 없음.
- 종료코드: 0 성공 / 1 git 실패·시간초과 / 2 설정·가드 위반 / 75 이미 실행 중. 실패 로그에 `마지막 성공 SHA` 를 남기고, 성공 SHA 는 `<미러>.last_ok`(미러 밖 — `git clean` 이 지우지 못하게)에 기록.

## 2. 검증 (실제 실행)

- `bash scripts/run_unit_tests.sh tests/unit/test_doc_index_origin_mirror.py tests/unit/test_doc_index_pipeline.py` → **46 passed**, exit 0.
- `ruff check --select F821,F811 scripts/index_docs.py tests/unit/test_doc_index_origin_mirror.py` → All checks passed (0건).
- `refresh_docs_mirror.sh` 를 임시 bare 저장소로 실행: 최초 clone·갱신·`clean` 으로 잡파일 제거·가드 거부(exit 2)·락 충돌(exit 75)·원본 접속 불가(exit 1, 마지막 성공 SHA 출력) 확인. 실제 `/root/aads/mirrors` 는 만들지 않았고 `/root/aads/aads-server` 에는 어떤 명령도 실행하지 않았다.
- 크론 sed 치환은 샘플 행으로만 확인했다. 실제 crontab 은 수정하지 않았다.

## 3. 운영 반영 절차 (머지 후, 별도 수행)

작업트리(`/root/aads/aads-server`)는 `behind 48` 이라 새 스크립트가 없다. **작업트리에 git 명령을 쓰지 않고** 미러를 직접 만든다.

### (a) 미러 최초 생성 (1회)

```bash
mkdir -p /root/aads/mirrors
git clone --no-checkout git@github.com:moongoby-GO100/aads-server.git /root/aads/mirrors/aads-server
bash /root/aads/mirrors/aads-server/scripts/refresh_docs_mirror.sh   # 체크아웃 + 이후 갱신 경로 검증
ls /root/aads/mirrors/aads-server/docs | head
```

이후 갱신은 `bash /root/aads/mirrors/aads-server/scripts/refresh_docs_mirror.sh`. 로그 마지막 줄 `성공: origin/main = <sha>` 와 `/root/aads/mirrors/aads-server.last_ok` 로 확인한다.

### (b) 크론 82행 교체

현재 형태: `10 */6 * * * cd /root/aads/aads-server && { ...; timeout 900 bash scripts/sync_remote_docs.sh; timeout 1800 python3 scripts/index_docs.py index; python3 scripts/index_docs.py status; } >> /var/log/index_docs.log`

`{ ... }` 안의 `...` 는 이 문서 작성 시점에 확인하지 못했다. 그 부분을 보존하도록 **치환식**으로 바꾼다(`cd` 를 미러로, refresh 를 맨 앞에 삽입). 실패해도 마지막 성공 미러로 색인은 계속한다.

```bash
crontab -l > /root/crontab.bak.$(date +%F)
crontab -l | sed -E '/scripts\/index_docs\.py index/ s#cd /root/aads/aads-server && \{#cd /root/aads/mirrors/aads-server \&\& { timeout 400 bash scripts/refresh_docs_mirror.sh || echo "[cron] refresh_docs_mirror 실패 - 마지막 성공 미러로 색인 계속";#' | crontab -
crontab -l | grep index_docs.py
```

결과(`...` 가 비었다고 가정한 형태):

```
10 */6 * * * cd /root/aads/mirrors/aads-server && { timeout 400 bash scripts/refresh_docs_mirror.sh || echo "[cron] refresh_docs_mirror 실패 - 마지막 성공 미러로 색인 계속"; timeout 900 bash scripts/sync_remote_docs.sh; timeout 1800 python3 scripts/index_docs.py index; python3 scripts/index_docs.py status; } >> /var/log/index_docs.log
```

`grep` 결과에 `refresh_docs_mirror.sh` 가 한 번, `cd /root/aads/mirrors/aads-server` 가 한 번 보여야 한다.

### (c) 즉시 재색인

```bash
cd /root/aads/mirrors/aads-server && timeout 1800 python3 scripts/index_docs.py scan    # 쓰기 없음, 먼저 규모 확인
cd /root/aads/mirrors/aads-server && timeout 1800 python3 scripts/index_docs.py index >> /var/log/index_docs.log 2>&1; python3 scripts/index_docs.py status
```

로그에 `경고: 미러 ... 없음` 이 보이면 폴백으로 돈 것이다(미러 상태 재확인). 개발 작업트리에만 있는 문서(ahead 4 커밋·미추적 파일)는 origin/main 에 없으면 이 실행에서 `사라진 문서 N개 색인 제거` 로 빠진다 — 의도된 동작이다.

### (d) 검증 SQL

`<SRV>` 는 색인 서버명(`AADS_SERVER_NAME` 또는 `hostname`). 컨테이너 경유:

```bash
SRV=$(hostname)
Q() { docker exec -i -e PGPASSWORD="${PGPASSWORD:?}" aads-postgres psql -U aads -d aads -c "$1"; }

# 재색인 전 기준값 기록 (b 이전에 실행)
Q "SELECT count(DISTINCT doc_path) AS docs, count(*) AS chunks FROM doc_chunks WHERE server='$SRV';"

# 1) 신규 5건의 doc_chunks >= 1
Q "SELECT k.key, count(c.*) AS chunks
   FROM (VALUES ('AADS-SEARCH-EVIDENCE-AUTHORITY-CONTRACT-v1.0'),('20261002_opus55_breaking_change_audit'),
                ('routing-ssot/DESIGN'),('routing-ssot/READ-MAP'),('20261001_AADS-P0P1MON-USER-SQL-EXCLUDE')) AS k(key)
   LEFT JOIN doc_chunks c ON c.server='$SRV' AND c.doc_path LIKE '/root/aads/aads-server/%' || k.key || '%'
   GROUP BY k.key ORDER BY k.key;"
# 기대: 5행 모두 chunks >= 1

# 2) 미러 경로가 키로 새지 않았나 — 0 이어야 한다
Q "SELECT count(*) FROM doc_chunks WHERE doc_path LIKE '/root/aads/mirrors/%';"

# 3) 문서 수 중복 증가 없음 — 재색인 후 docs 가 기준값 + (신규 약 5) - (작업트리 전용 문서)
#    수준이어야 하고, 두 배가 되면 실패
Q "SELECT count(DISTINCT doc_path) AS docs, count(*) AS chunks FROM doc_chunks WHERE server='$SRV';"

# 4) 같은 내용(sha)이 두 경로로 들어가지 않았나 — 0행이어야 한다
Q "SELECT doc_sha256, count(DISTINCT doc_path) FROM doc_chunks WHERE server='$SRV'
   GROUP BY doc_sha256 HAVING count(DISTINCT doc_path) > 1 LIMIT 10;"
```

`PGPASSWORD` 는 운영자가 환경에서 주입한다(이 문서에 비밀번호를 적지 않는다).

### (e) 롤백

```bash
crontab /root/crontab.bak.<적용일>                 # 1) 크론 원복 (b 에서 만든 백업)
# 2) 이 작업의 커밋을 revert → index_docs.py 가 다시 작업트리를 읽는다 (Runner 경유 커밋/배포)
rm -rf /root/aads/mirrors/aads-server /root/aads/mirrors/aads-server.last_ok   # 3) 미러 삭제 (미러 전용 디렉터리)
```

`doc_path` 키가 불변이라 롤백 시 재색인으로 데이터가 되돌아오며 별도 DB 복구는 필요 없다.

## 4. 오류 사전 (운영 반영·검증 후 실행 — 아직 실행하지 않았다)

```bash
scripts/error_book.py register --key docs.index_stale_worktree \
  --symptom "origin/main 신규 문서가 검색 0건" \
  --cause "index_docs.py가 behind 상태 개발 작업트리를 원본으로 읽음" \
  --prevention "색인 원본은 origin/main 전용 미러" \
  --fix-commit <이 커밋 sha>
```
