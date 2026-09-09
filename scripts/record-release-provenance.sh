#!/usr/bin/env bash
# record-release-provenance.sh — 배포 인증 시점에 커밋→릴리스 계보를 DB 에 못박는다.
#
# 왜 여기(호스트의 deploy.sh 경로)인가
# ------------------------------------
# .dockerignore 가 .git 을 제외하므로 실행 중인 aads-server 컨테이너에는
# /app/.git 이 없다. 런타임에서 `git merge-base --is-ancestor` 를 호출하는 설계는
# 프로덕션에서 성립할 수 없다. 그래서 Git 히스토리가 **살아 있는 유일한 지점**인
# 깨끗한 릴리스 워크트리에서 계보를 계산해 40자 full SHA 로 저장한다.
#
# 안전 규칙
# ---------
# * 접두사 금지: 40자 소문자 hex 가 아닌 후보는 Git 에 묻지도 않고 거부한다(fail closed).
# * 릴리스 HEAD 도 40자로 해석되지 않으면 아무 것도 기록하지 않고 종료한다.
# * 기록되는 INSERT 는 항상 deploy_runs 인증 조건(EXISTS 가드)을 SQL 안에 포함한다.
#   따라서 인증 전에 이 스크립트가 (실수로) 돌더라도 행이 생기지 않는다 —
#   "후보 관계를 먼저 계산하되 인증 전에는 사용 불가"라는 요구를 SQL 수준에서 보장한다.
# * ON CONFLICT DO NOTHING — 같은 배포를 다시 인증해도 중복 행이 생기지 않는다(멱등).
# * 이 스크립트는 컨테이너를 재시작하거나 이미지를 다시 만들지 않는다. 읽기(Git) +
#   INSERT 만 한다. nginx 락/배포 락 범위를 건드리지 않는다.
#
# 사용법:
#   record-release-provenance.sh --repo DIR --deploy-run-id N --project AADS [--release-ref HEAD]
#   record-release-provenance.sh ... --candidates-file FILE --emit-sql-only   # 테스트/감사용
set -uo pipefail

REPO=""
DEPLOY_RUN_ID=""
PROJECT="AADS"
COMPONENT="api"
RELEASE_REF="HEAD"
CANDIDATES_FILE=""
EMIT_SQL_ONLY="false"
MAX_CANDIDATES="${AADS_PROVENANCE_MAX_CANDIDATES:-500}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --deploy-run-id) DEPLOY_RUN_ID="$2"; shift 2 ;;
        --project) PROJECT="$2"; shift 2 ;;
        --component) COMPONENT="$2"; shift 2 ;;
        --release-ref) RELEASE_REF="$2"; shift 2 ;;
        --candidates-file) CANDIDATES_FILE="$2"; shift 2 ;;
        --emit-sql-only) EMIT_SQL_ONLY="true"; shift ;;
        *) echo "[provenance] unknown argument: $1" >&2; exit 64 ;;
    esac
done

log() { echo "[provenance] $*" >&2; }

[[ -n "$REPO" ]] || { log "FAIL: --repo is required"; exit 64; }
[[ -d "$REPO/.git" || -f "$REPO/.git" ]] || { log "SKIP: ${REPO} is not a git worktree"; exit 3; }
[[ "$DEPLOY_RUN_ID" =~ ^[0-9]+$ ]] || { log "SKIP: --deploy-run-id must be numeric (got '${DEPLOY_RUN_ID}')"; exit 3; }

sql_lit() { printf "'%s'" "${1//\'/\'\'}"; }

# ── 릴리스 HEAD 를 40자 full SHA 로 해석 (fail closed) ──────────────────────
RELEASE_SHA_FULL="$(git -C "$REPO" rev-parse --verify --quiet "${RELEASE_REF}^{commit}" 2>/dev/null || true)"
if [[ ! "$RELEASE_SHA_FULL" =~ ^[0-9a-f]{40}$ ]]; then
    log "FAIL: cannot resolve ${RELEASE_REF} to a full 40-char SHA in ${REPO} — no provenance recorded"
    exit 4
fi
log "release_sha=${RELEASE_SHA_FULL} deploy_run_id=${DEPLOY_RUN_ID} project=${PROJECT}"

db_exec() {
    timeout 20 docker exec aads-postgres psql -U aads -d aads -qAtc "$1" 2>/dev/null || true
}

# ── 후보 커밋 수집 ──────────────────────────────────────────────────────────
# 기본은 DB: 이 프로젝트의 파이프라인 작업 커밋 중 아직 이 배포에 대해 기록되지
# 않은 것들. 테스트/감사에서는 --candidates-file 로 대체한다.
CANDIDATES=""
if [[ -n "$CANDIDATES_FILE" ]]; then
    [[ -r "$CANDIDATES_FILE" ]] || { log "FAIL: cannot read ${CANDIDATES_FILE}"; exit 64; }
    CANDIDATES="$(cat "$CANDIDATES_FILE")"
else
    CANDIDATES="$(db_exec "
        SELECT DISTINCT j.commit_hash
        FROM pipeline_jobs j
        WHERE j.project = $(sql_lit "$PROJECT")
          AND j.commit_hash IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM deploy_release_provenance p
              WHERE p.deploy_run_id = ${DEPLOY_RUN_ID} AND p.task_sha = j.commit_hash
          )
        ORDER BY 1
        LIMIT ${MAX_CANDIDATES};
    ")"
fi

# ── 계보 판정 ───────────────────────────────────────────────────────────────
n_exact=0; n_ancestor=0; n_ambiguous=0; n_unknown=0; n_not_contained=0
VALUES=""

while IFS= read -r raw; do
    sha="$(printf '%s' "$raw" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
    [[ -n "$sha" ]] || continue

    # 모호한 접두사는 Git 에 물어보지도 않는다 — 확장 결과를 저장하면 감사 불가.
    if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
        n_ambiguous=$((n_ambiguous + 1))
        continue
    fi
    if ! git -C "$REPO" cat-file -e "${sha}^{commit}" 2>/dev/null; then
        n_unknown=$((n_unknown + 1))
        continue
    fi

    if [[ "$sha" == "$RELEASE_SHA_FULL" ]]; then
        relationship="exact"
        n_exact=$((n_exact + 1))
    elif git -C "$REPO" merge-base --is-ancestor "$sha" "$RELEASE_SHA_FULL" 2>/dev/null; then
        relationship="ancestor"
        n_ancestor=$((n_ancestor + 1))
    else
        n_not_contained=$((n_not_contained + 1))
        continue
    fi

    VALUES+="${VALUES:+,}(${DEPLOY_RUN_ID}, $(sql_lit "$PROJECT"), $(sql_lit "$COMPONENT"), $(sql_lit "$sha"), $(sql_lit "$RELEASE_SHA_FULL"), $(sql_lit "$relationship"), 'deploy.sh')"
done <<< "$CANDIDATES"

log "resolved exact=${n_exact} ancestor=${n_ancestor} rejected_ambiguous=${n_ambiguous} rejected_unknown=${n_unknown} rejected_not_contained=${n_not_contained}"

if [[ -z "$VALUES" ]]; then
    log "no provenance rows to record"
    exit 0
fi

# INSERT 는 항상 인증 게이트를 자기 안에 들고 다닌다.
# deploy_runs 가 success/completed 이고 두 슬롯 다이제스트가 같지 않으면 0행이 들어간다.
SQL="INSERT INTO deploy_release_provenance
        (deploy_run_id, project, component, task_sha, release_sha, relationship, resolved_by)
SELECT v.deploy_run_id, v.project, v.component, v.task_sha, v.release_sha, v.relationship, v.resolved_by
FROM (VALUES ${VALUES}) AS v(deploy_run_id, project, component, task_sha, release_sha, relationship, resolved_by)
WHERE EXISTS (
    SELECT 1 FROM deploy_runs d
    WHERE d.id = ${DEPLOY_RUN_ID}
      AND d.status = 'success'
      AND d.phase = 'completed'
      AND d.image_digest IS NOT NULL
      AND d.standby_digest IS NOT NULL
      AND d.image_digest = d.standby_digest
)
ON CONFLICT (deploy_run_id, task_sha) DO NOTHING;"

if [[ "$EMIT_SQL_ONLY" == "true" ]]; then
    printf '%s\n' "$SQL"
    exit 0
fi

INSERTED="$(db_exec "WITH ins AS (${SQL%;} RETURNING 1) SELECT count(*) FROM ins;")"
log "recorded rows=${INSERTED:-0}"
if [[ "${INSERTED:-0}" == "0" ]]; then
    log "NOTE: 0 rows — deploy_run ${DEPLOY_RUN_ID} is not certified yet, or all rows already existed"
fi
exit 0
