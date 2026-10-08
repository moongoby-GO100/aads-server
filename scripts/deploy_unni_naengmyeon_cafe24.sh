#!/bin/bash
# 언니냉면 화면(fb.newtalk.kr/unni-naengmyeon/)을 카페24(server-114) apache 로 옮긴다.
#
#   deploy_unni_naengmyeon_cafe24.sh plan       읽기 전용 사전 점검(기본값). 아무것도 바꾸지 않는다.
#   UNNI_CAFE24_APPROVED=1 deploy_unni_naengmyeon_cafe24.sh apply
#                                              스냅샷 업로드 → current 전환 → 문의 테이블 → apache 라우트 → 공개 확인
#   deploy_unni_naengmyeon_cafe24.sh verify     공개 URL 확인만
#   deploy_unni_naengmyeon_cafe24.sh rollback   live vhost 에서 BEGIN-UNNI 블록을 빼고, current 를 직전 릴리스로
#
# 왜 정적 스냅샷인가. 공개 화면 3개(/unni-naengmyeon, brand/logo, brand/banners)는 요청마다 바뀌는
# 데이터가 없고, 동적인 것은 문의 폼 POST 하나뿐이다. 카페24에 Next 컨테이너를 따로 두면 대시보드
# 전체를 빌드·운영해야 한다(이미지 수 GB, 블루그린, 메모리). 정적 파일은 apache 가 직접 내고
# 문의 POST(/api/v1/unni-naengmyeon/inquiries)와 직원용 조리법(/unni-naengmyeon/recipes, 로그인 필요)만
# 이미 떠 있는 ACCT 앱(app/yeoljeong_main.py)이 받는다.
#
# 원본은 contabo116 에서 지금 응답 중인 대시보드 컨테이너다(aads-dashboard 가 정본,
# aads-dashboard-unni 는 2026-07-24 에 멈춘 사본). HTML 과 /_next/static 을 같은 컨테이너에서
# 가져오고 BUILD_ID 가 HTML 의 빌드 ID 와 같은지 확인한다 — 다르면 청크가 어긋난다.
#
# contabo116 디스크가 빠듯하다(2026-10-08 97%). 자산(약 230MB)은 docker cp | ssh tar 로 흘려보내고
# contabo116 에는 HTML 3장(수백 KB)만 임시로 두었다가 종료 시 지운다.
#
# 종료코드: 2 인자 | 3 이미 실행 중 | 4 승인 없음 | 5 원본 점검 실패 | 6 업로드·자산 검증 실패(current 그대로)
#           7 문의 테이블 실패 | 8 apache 실패(복구함) | 9 live vhost 가 template 과 다름(손대지 않음) | 10 공개 확인 실패

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$REPO_ROOT/scripts/unni_naengmyeon_site.py"
TEMPLATE="${TEMPLATE:-$REPO_ROOT/config/apache/fb-cafe24.conf}"
RECIPES_HTML="$REPO_ROOT/app/sites/unni_naengmyeon/recipes.html"
INQUIRY_SQL="$REPO_ROOT/migrations/20260916_unni_naengmyeon_inquiries.sql"
CAFE24_SSH="${CAFE24_SSH:-server-114}"
FB_HOST="fb.newtalk.kr"
SRC_CONTAINER="${UNNI_SRC_CONTAINER:-aads-dashboard}"
REL_ROOT="${UNNI_REL_ROOT:-/srv/unni-naengmyeon}"
APACHE_SITE="${ACCT_APACHE_SITE:-/etc/apache2/sites-available/00-zz-fb.newtalk.kr.conf}"
APACHE_BACKUP_DIR="${ACCT_APACHE_BACKUP_DIR:-/root/fb-cutover-backups}"
PG_CONTAINER="${ACCT_PG_CONTAINER:-acct-pg}"
APP_CONTAINER="${ACCT_APP_CONTAINER:-}"   # 비우면 apache upstream 포트로 찾는다
INQUIRY_DB="${UNNI_INQUIRY_DB:-obys_auth}" # ACCT 앱의 공용 풀(DATABASE_URL)이 이 DB 다(app/core/obys_runtime.py)
LOCK="${UNNI_LOCK_FILE:-/tmp/aads-unni-cafe24.lock}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
PAGES=(/unni-naengmyeon /unni-naengmyeon/brand/logo /unni-naengmyeon/brand/banners)

WORKDIRS=()
cleanup() { local d; for d in "${WORKDIRS[@]}"; do rm -rf "$d"; done; }
trap cleanup EXIT
new_work() { mktemp -d /tmp/unni-snap.XXXXXX; }

say() { printf '[unni-cafe24] %s\n' "$*"; }
die() { local rc=$1; shift; printf '[unni-cafe24] FAIL: %s\n' "$*" >&2; exit "$rc"; }
ssh_c() { timeout -k 10 "${SSH_TIMEOUT:-120}" ssh "${SSH_OPTS[@]}" "$CAFE24_SSH" "$@"; }
pub_code() { curl -s -m 15 -o /dev/null -w '%{http_code}' -H 'Cache-Control: no-cache' "$1" || true; }

page_file() { # /unni-naengmyeon/brand/logo -> unni-naengmyeon/brand/logo/index.html
    printf '%s/index.html' "${1#/}"
}

source_port() {
    local p
    p="$(docker port "$SRC_CONTAINER" 2>/dev/null | sed -nE 's#^[0-9]+/tcp -> 127\.0\.0\.1:([0-9]+)$#\1#p' | head -1)"
    [[ $p =~ ^[0-9]+$ ]] || die 5 "cannot find the 127.0.0.1 port of $SRC_CONTAINER"
    echo "$p"
}

# HTML 3장을 받아 rewrite 한다. $1 = 작업 디렉터리. BUILD_ID 를 출력.
snapshot_pages() {
    local work=$1 port build f code
    port="$(source_port)"
    build="$(docker exec "$SRC_CONTAINER" cat /app/.next/BUILD_ID)"
    [[ $build =~ ^[A-Za-z0-9_-]+$ ]] || die 5 "unexpected BUILD_ID from $SRC_CONTAINER"
    for p in "${PAGES[@]}"; do
        f="$work/$(page_file "$p")"
        mkdir -p "$(dirname "$f")"
        code="$(curl -s -m 20 -o "$f.raw" -w '%{http_code}' -H "Host: $FB_HOST" "http://127.0.0.1:$port$p")"
        [[ $code == 200 ]] || die 5 "$SRC_CONTAINER $p -> $code"
        # RSC 페이로드에는 \"b\":\"<BUILD_ID>\" 로 박혀 있다.
        grep -qF "\\\"b\\\":\\\"$build\\\"" "$f.raw" || die 5 "$p was not rendered by build $build (stale slot?)"
        python3 "$HELPER" rewrite-html "$f.raw" "$f" || die 5 "rewrite failed for $p"
        rm -f "$f.raw"
    done
    echo "$build"
}

current_upstream() { # cafe24 apache 의 upstream 포트(주석 제외, 유일해야 함)
    ssh_c "grep -vE '^[[:space:]]*#' $(printf '%q' "$APACHE_SITE") | grep -oE 'http://[0-9.]+:[0-9]+' | sort -u"
}

cmd_plan() {
    local work build ups
    command -v python3 >/dev/null || die 5 "python3 missing"
    bash "$REPO_ROOT/scripts/cutover_fb_cafe24.sh" lint >/dev/null || die 5 "apache template lint failed"
    work="$(new_work)"
    WORKDIRS+=("$work")
    build="$(snapshot_pages "$work")"
    say "source $SRC_CONTAINER build=$build pages=${#PAGES[@]} OK"
    python3 "$HELPER" refs "$work"/unni-naengmyeon/index.html "$work"/unni-naengmyeon/brand/*/index.html "$RECIPES_HTML" > "$work/refs"
    say "referenced assets: $(wc -l <"$work/refs")"
    local missing=0
    while IFS= read -r r; do
        case $r in
            /_next/static/*) docker exec "$SRC_CONTAINER" test -f "/app/.next/static/${r#/_next/static/}" || { say "MISSING in source $r"; missing=1; } ;;
            /brands/*) docker exec "$SRC_CONTAINER" test -f "/app/public$r" || { say "MISSING in source $r"; missing=1; } ;;
        esac
    done <"$work/refs"
    [[ $missing == 0 ]] || die 5 "source container lacks referenced assets"
    ups="$(current_upstream)"
    [[ $(wc -l <<<"$ups") == 1 && $ups =~ ^http://[0-9.]+:[0-9]+$ ]] || die 5 "ambiguous cafe24 upstream: $ups"
    ssh_c "cat $(printf '%q' "$APACHE_SITE")" >"$work/live.conf"
    local rc=0
    python3 "$HELPER" render-vhost "$TEMPLATE" "$work/live.conf" "$work/new.conf" || rc=$?
    case $rc in
        0) say "apache: upstream $ups, change = $(diff "$work/live.conf" "$work/new.conf" | grep -c '^[<>]') lines (BEGIN-UNNI)" ;;
        3) die 9 "live vhost has changes that are not in config/apache/fb-cafe24.conf — reconcile first" ;;
        *) die 5 "render-vhost failed ($rc)" ;;
    esac
    say "cafe24 disk: $(ssh_c "df -h / | tail -1")"
    say "plan OK — nothing changed. Apply with UNNI_CAFE24_APPROVED=1 $0 apply"
}

install_vhost() { # $1 = new vhost file, $2 = backup tag. backup -> write -> configtest -> graceful; restore on failure
    ssh_c "set -u; site=$(printf '%q' "$APACHE_SITE"); bk=$(printf '%q' "$APACHE_BACKUP_DIR")/\$(basename \"\$site\").$2.\$(date +%Y%m%d_%H%M%S)
        mkdir -p $(printf '%q' "$APACHE_BACKUP_DIR") && cp -p \"\$site\" \"\$bk\" || exit 1
        restore() { cat \"\$bk\" > \"\$site\"; apache2ctl configtest && apache2ctl graceful; }
        cat > \"\$site\"
        apache2ctl configtest || { restore; exit 2; }
        apache2ctl graceful || { restore; exit 2; }
        echo BACKUP=\$bk" <"$1"
}

remote_verify_refs() { # $1 = remote release dir, stdin = refs
    ssh_c "rel=$(printf '%q' "$1"); m=0; while IFS= read -r r; do [ -f \"\$rel\$r\" ] || { echo \"MISSING \$r\"; m=1; }; done; exit \$m"
}

cmd_apply() {
    [[ ${UNNI_CAFE24_APPROVED:-} == 1 ]] || die 4 "apply needs CEO approval: UNNI_CAFE24_APPROVED=1"
    exec 9>"$LOCK"
    flock -n 9 || die 3 "another run holds $LOCK"
    cmd_plan
    local work build ts rel prev ups
    work="$(new_work)"
    WORKDIRS+=("$work")
    build="$(snapshot_pages "$work")"
    python3 "$HELPER" refs "$work"/unni-naengmyeon/index.html "$work"/unni-naengmyeon/brand/*/index.html "$RECIPES_HTML" > "$work/refs"
    ts="$(TZ=Asia/Seoul date +%Y%m%d_%H%M%S)"
    rel="$REL_ROOT/releases/${ts}-${build}"

    say "1/5 upload $rel"
    ssh_c "install -d -m 755 $(printf '%q' "$REL_ROOT") $(printf '%q' "$REL_ROOT/releases") $(printf '%q' "$rel/_next") $(printf '%q' "$rel/brands")" || die 6 "mkdir $rel"
    tar -C "$work" -cf - unni-naengmyeon | ssh_c "tar -C $(printf '%q' "$rel") --no-same-owner -xf -" || die 6 "page upload"
    docker cp "$SRC_CONTAINER:/app/.next/static" - | SSH_TIMEOUT=300 ssh_c "tar -C $(printf '%q' "$rel/_next") --no-same-owner -xf -" || die 6 "_next/static upload"
    docker cp "$SRC_CONTAINER:/app/public/brands/unni-naengmyeon" - | SSH_TIMEOUT=600 ssh_c "tar -C $(printf '%q' "$rel/brands") --no-same-owner -xf -" || die 6 "brand asset upload"
    ssh_c "chmod -R a+rX $(printf '%q' "$rel")" || die 6 "chmod"
    remote_verify_refs "$rel" <"$work/refs" || die 6 "release $rel is missing referenced assets (current unchanged)"

    say "2/5 switch $REL_ROOT/current"
    prev="$(ssh_c "readlink $(printf '%q' "$REL_ROOT/current") || true")"
    ssh_c "cd $(printf '%q' "$REL_ROOT") && ln -sfn $(printf '%q' "releases/${ts}-${build}") current.tmp && mv -Tf current.tmp current && echo $(printf '%q' "${prev:-none}") > previous" || die 6 "symlink switch"

    say "3/5 inquiry table in $INQUIRY_DB"
    if grep -nE -i '\b(DROP|TRUNCATE|DELETE)\b' "$INQUIRY_SQL"; then die 7 "inquiry migration contains a destructive statement"; fi
    ssh_c "docker exec -i $(printf '%q' "$PG_CONTAINER") sh -c 'psql -U \"\$POSTGRES_USER\" -d $(printf '%q' "$INQUIRY_DB") -X -q -v ON_ERROR_STOP=1'" <"$INQUIRY_SQL" || die 7 "inquiry migration"
    ups="$(current_upstream)"
    local app="${APP_CONTAINER}" role
    if [[ -z $app ]]; then
        app="$(ssh_c "docker ps --filter name=acct-app-candidate --format '{{.Names}}' | while read -r c; do docker inspect \"\$c\" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -qx 'APP_PORT=${ups##*:}' && echo \"\$c\"; done | head -1")"
    fi
    [[ $app =~ ^acct-app-candidate-r[0-9]+$ ]] || die 7 "cannot find the app container serving ${ups##*:}"
    # 앱 DB 역할 이름만 꺼낸다(비밀번호는 출력하지 않는다). obys_auth 에는 default privileges 가 없다.
    role="$(ssh_c "docker inspect $(printf '%q' "$app") --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -nE 's#^OBYS_AUTH_DATABASE_URL=postgres(ql)?://([^:@/]+).*#\\2#p'")"
    [[ $role =~ ^[a-z_][a-z0-9_]*$ ]] || die 7 "cannot read the app DB role name"
    printf 'GRANT INSERT ON unni_naengmyeon_inquiries TO %s;\nGRANT USAGE ON SEQUENCE unni_naengmyeon_inquiries_id_seq TO %s;\n' "$role" "$role" \
        | ssh_c "docker exec -i $(printf '%q' "$PG_CONTAINER") sh -c 'psql -U \"\$POSTGRES_USER\" -d $(printf '%q' "$INQUIRY_DB") -X -q -v ON_ERROR_STOP=1'" \
        || die 7 "grant to $role"
    say "    table ready, INSERT granted to $role ($app)"

    say "4/5 apache route"
    ssh_c "cat $(printf '%q' "$APACHE_SITE")" >"$work/live.conf"
    local rc=0
    python3 "$HELPER" render-vhost "$TEMPLATE" "$work/live.conf" "$work/new.conf" || rc=$?
    [[ $rc == 0 ]] || die 9 "render-vhost rc=$rc — live vhost left untouched"
    if cmp -s "$work/live.conf" "$work/new.conf"; then
        say "    vhost already current"
    else
        install_vhost "$work/new.conf" unni || die 8 "apache install failed — previous vhost restored"
        sleep 2
    fi

    say "5/5 public verify"
    cmd_verify || die 10 "public verify failed — rollback: $0 rollback"
    say "apply OK release=${ts}-${build} previous=${prev:-none}"
}

cmd_verify() {
    local fail=0 c
    chk() { c="$(pub_code "$2")"; if [[ $c =~ ^($3)$ ]]; then say "OK   $1 $c"; else say "FAIL $1 $c (want $3)"; fail=1; fi; }
    chk "unni home"       "https://$FB_HOST/unni-naengmyeon/" 200
    chk "unni logo"       "https://$FB_HOST/unni-naengmyeon/brand/logo/" 200
    chk "unni banners"    "https://$FB_HOST/unni-naengmyeon/brand/banners/" 200
    chk "unni logo image" "https://$FB_HOST/brands/unni-naengmyeon/bowlcut-logo-concepts-20260722/concept-h-wordmark-noodles.png" 200
    chk "recipes (anon)"  "https://$FB_HOST/unni-naengmyeon/recipes" 302
    chk "inquiry GET"     "https://$FB_HOST/api/v1/unni-naengmyeon/inquiries" 405
    chk "obys index"      "https://$FB_HOST/static/apps/obys/index.html" 200
    chk "old finance url" "https://$FB_HOST/static/apps/yeoljeong-finance/index.html" 301
    chk "health"          "https://$FB_HOST/health/live" 200
    local js
    js="$(curl -s -m 15 "https://$FB_HOST/unni-naengmyeon/" | grep -oE '/_next/static/chunks/[^"]+\.js' | head -1)"
    [[ -n $js ]] && chk "next chunk" "https://$FB_HOST$js" 200 || { say "FAIL no chunk reference on the unni page"; fail=1; }
    return "$fail"
}

cmd_rollback() {
    exec 9>"$LOCK"
    flock -n 9 || die 3 "another run holds $LOCK"
    local work
    work="$(new_work)"
    WORKDIRS+=("$work")
    # 백업 파일을 되돌리지 않는다 — 그 사이 deploy_acct_app_cafe24.sh 가 upstream 포트를 바꿨으면
    # 백업은 옛 컨테이너를 가리킨다. live 에서 BEGIN-UNNI 블록만 뺀다.
    ssh_c "cat $(printf '%q' "$APACHE_SITE")" >"$work/live.conf"
    python3 "$HELPER" strip-unni "$work/live.conf" "$work/new.conf" || die 8 "strip-unni failed"
    if cmp -s "$work/live.conf" "$work/new.conf"; then
        say "apache: no BEGIN-UNNI block, unchanged"
    else
        install_vhost "$work/new.conf" unni-rollback || die 8 "apache rollback failed — vhost restored to its pre-rollback state"
        say "apache: BEGIN-UNNI removed"
    fi
    ssh_c "set -u; root=$(printf '%q' "$REL_ROOT")
        prev=\$(cat \"\$root/previous\" 2>/dev/null || echo none)
        if [ \"\$prev\" != none ] && [ -d \"\$root/\$prev\" ]; then ln -sfn \"\$prev\" \"\$root/current.tmp\" && mv -Tf \"\$root/current.tmp\" \"\$root/current\" && echo \"current -> \$prev\"; fi" \
        || die 8 "release symlink rollback failed"
    say "rollback done (release files and the inquiry table are kept)"
}

case "${1:-plan}" in
    plan) cmd_plan ;;
    apply) cmd_apply ;;
    verify) cmd_verify ;;
    rollback) cmd_rollback ;;
    *) die 2 "usage: $0 plan|apply|verify|rollback" ;;
esac
