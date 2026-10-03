#!/usr/bin/env bash
# fb.newtalk.kr public routing -> ACCT/OBYS app on cafe24_114.
#
#   lint              offline checks of this script, the Apache template and the edge rendering
#   preflight         read-only: candidate image/health/auth/cert/apache readiness on cafe24
#   apply-origin      install the fb vhost on cafe24 apache (configtest -> graceful -> verify -> auto rollback)
#   allow-edge-fw     (opt-in) let contabo116 (EDGE_IP/32) reach cafe24 origin TCP 443 ONLY (CONFIRM_FIREWALL_CHANGE=1)
#   revoke-edge-fw    remove exactly that rule (a broader legacy rule is only reported unless CONFIRM_REMOVE_BROAD_FW=1)
#   edge-fw-status    read-only: exact 443 rule vs broader fb-edge rules on cafe24 CF-WEB
#   apply-edge        contabo116 nginx fb.conf: yeoljeong_finance_api -> https://cafe24. Candidate/ssh prep happens
#                     BEFORE the nginx lock; the lock covers only config+marker, nginx -t, reload and one bounded
#                     local routed-health; cafe24 log proof, QA run after unlock and re-lock only to roll back.
#   maintenance       fb-only 503 on the edge (rollback target; never reverts to the retired jinah upstream)
#   monitor           public P0/P1 watch (default 300 s)
#   status            show where the edge and the origin currently point
#
# Cloudflare DNS origin flip is NOT done here: no Cloudflare credential is read or written.
# apply-edge needs cafe24 :443 reachable from contabo116, which the cafe24 iptables CF-WEB chain
# denies by default (Cloudflare ranges only). Either flip the Cloudflare origin for fb to
# 114.207.244.86 (preferred) or run allow-edge-fw first. Port 80 is never opened by this script.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${TEMPLATE:-$REPO_ROOT/config/apache/fb-cafe24.conf}"
CAFE24_SSH="${CAFE24_SSH:-server-114}"
CAFE24_IP="${CAFE24_IP:-114.207.244.86}"
APP_CONTAINER="${APP_CONTAINER:-acct-app-candidate-r8}"
NS_CONTAINER="${NS_CONTAINER:-acct-pg}"
APP_PORT="${APP_PORT:-8111}"
EXPECTED_DIGEST="${EXPECTED_DIGEST:-sha256:71e5e811c220efbe9bc527f36836e0cdc9638794ec01a6b1bbd5256ea5e8150e}"
FB_HOST="fb.newtalk.kr"
EDGE_CONF="${EDGE_CONF:-/etc/nginx/conf.d/fb.conf}"
EDGE_IP="${EDGE_IP:-5.104.86.116}"
NGINX_CONTAINER="${NGINX_CONTAINER:-aads-nginx}"
NGINX_SWITCH_LOCK="${NGINX_SWITCH_LOCK:-/tmp/aads-nginx-upstream.lock}"
NGINX_LOCK_MAX_HOLD="${NGINX_LOCK_MAX_HOLD:-30}"
FB_STATE_MARKER="${FB_STATE_MARKER:-/tmp/aads-fb-cutover.state}"
FW_CHAIN="CF-WEB"
FW_COMMENT="fb-edge-contabo116"
FW_PORT=443
CA_BUNDLE="${CA_BUNDLE:-/etc/ssl/certs/ca-certificates.crt}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)

say() { printf '[cutover-fb] %s\n' "$*"; }
die() { printf '[cutover-fb] FAIL: %s\n' "$*" >&2; exit 1; }
ssh_c() { ssh "${SSH_OPTS[@]}" "$CAFE24_SSH" "$@"; }

routes_block() { awk -v n="$2" '/# BEGIN-ROUTES/{i++} i==n{print} /# END-ROUTES/{if(i==n) exit}' "$1"; }

render_apache() { sed "s#__FB_UPSTREAM__#$1#g" "$TEMPLATE"; }

# Replace each yeoljeong_finance_api proxy_pass with the cafe24 HTTPS origin and add TLS verification.
# Locations that point at aads_dashboard are left untouched.
render_edge() {
  awk -v ip="$CAFE24_IP" -v host="$FB_HOST" -v ca="$CA_BUNDLE" '
    /proxy_pass[ \t]+http:\/\/yeoljeong_finance_api/ {
      sub(/http:\/\/yeoljeong_finance_api/, "https://" ip); print
      print "        proxy_ssl_server_name on;"
      print "        proxy_ssl_name " host ";"
      print "        proxy_ssl_protocols TLSv1.2 TLSv1.3;"
      print "        proxy_ssl_verify on;"
      print "        proxy_ssl_verify_depth 3;"
      print "        proxy_ssl_trusted_certificate " ca ";"
      next }
    { print }' "$1"
}

lint_envvars_regression() {
  local fx fn rc; fx="$(mktemp)"
  cat >"$fx" <<'EOF'
unset HOME
if [ "${APACHE_CONFDIR##/etc/apache2-}" != "${APACHE_CONFDIR}" ] ; then
	SUFFIX="-${APACHE_CONFDIR##/etc/apache2-}"
else
	SUFFIX=
fi
export APACHE_RUN_USER=www-data
export APACHE_RUN_GROUP=www-data
export APACHE_PID_FILE=/var/run/apache2$SUFFIX/apache2.pid
export APACHE_RUN_DIR=/var/run/apache2$SUFFIX
export APACHE_LOCK_DIR=/var/lock/apache2$SUFFIX
export APACHE_LOG_DIR=/var/log/apache2$SUFFIX
export LANG=C
EOF
  fn="$(sed -n '/^load_apache_env()/,/^}/p' <<<"$REMOTE_LIB")"
  [[ -n $fn ]] || { rm -f "$fx"; die "load_apache_env missing from REMOTE_LIB"; }
  if env -u APACHE_CONFDIR bash -c "set -u; . '$fx'" >/dev/null 2>&1; then
    rm -f "$fx"; die "envvars fixture no longer reproduces the unbound APACHE_CONFDIR failure"
  fi
  rc=0
  env -u APACHE_CONFDIR -u APACHE_LOG_DIR ENVVARS_FILE="$fx" bash -c "set -u; $fn
    load_apache_env
    [[ \$- == *u* ]] && [[ \$APACHE_LOG_DIR == /var/log/apache2 ]] && [[ \$APACHE_CONFDIR == /etc/apache2 ]]" >/dev/null 2>&1 || rc=$?
  rm -f "$fx"
  [[ $rc == 0 ]] || die "load_apache_env does not survive set -u with an unset APACHE_CONFDIR (rc=$rc)"
}

lint_vhost_order_regression() {
  local fn pre80 pre443 got
  fn="$(sed -n '/^vhost_order_check()/,/^}/p' <<<"$REMOTE_LIB")"
  [[ -n $fn ]] || die "vhost_order_check missing from REMOTE_LIB"
  pre80='         port 80 namevhost pick.newtalk.kr (/etc/apache2/sites-enabled/00-pick.newtalk.kr.conf:1)'
  pre443='         port 443 namevhost pick.newtalk.kr (/etc/apache2/sites-enabled/00-pick.newtalk.kr.conf:20)'
  local wild80 wild443 fb80 fb443 legacy80
  wild80=$'         port 80 namevhost newtalk.kr (/etc/apache2/sites-enabled/000-default.conf:1)\n                 wild alias *.newtalk.kr'
  wild443=$'         port 443 namevhost newtalk.kr (/etc/apache2/sites-enabled/default-ssl.conf:2)\n                 wild alias *.newtalk.kr'
  fb80='         port 80 namevhost fb.newtalk.kr (/etc/apache2/sites-enabled/00-zz-fb.newtalk.kr.conf:12)'
  fb443='         port 443 namevhost fb.newtalk.kr (/etc/apache2/sites-enabled/00-zz-fb.newtalk.kr.conf:56)'
  legacy80='         port 80 namevhost fb.newtalk.kr (/etc/apache2/sites-enabled/10-fb.newtalk.kr.conf:12)'
  _vo() { env FB=fb.newtalk.kr bash -c "$fn"$'\nvhost_order_check "$1" "$2"' _ "$1" "$2"; }
  got="$(_vo 80 "$pre80"$'\n'"$fb80"$'\n'"$wild80")";  [[ $got == ok ]] || die "vhost order fixture (00-zz-fb before wild, :80) -> $got"
  got="$(_vo 443 "$pre443"$'\n'"$fb443"$'\n'"$wild443")"; [[ $got == ok ]] || die "vhost order fixture (00-zz-fb before wild, :443) -> $got"
  got="$(_vo 80 "$wild80"$'\n'"$legacy80")";  [[ $got == foreign ]] || die "vhost order fixture (10-fb owner) -> $got"
  got="$(_vo 80 "$wild80"$'\n'"$fb80")";  [[ $got == shadowed ]] || die "vhost order fixture (wild :80 first) -> $got"
  got="$(_vo 443 "$wild443"$'\n'"$fb443")"; [[ $got == shadowed ]] || die "vhost order fixture (wild :443 first) -> $got"
  got="$(_vo 443 "$pre80"$'\n'"$fb80"$'\n'"$wild443")"; [[ $got == missing ]] || die "vhost order fixture (fb only on :80, check :443) -> $got"
  got="$(_vo 80 "$fb80"$'\n'"$wild443")"; [[ $got == ok ]] || die "vhost order fixture (wild on other port must not shadow) -> $got"
  got="$(_vo 80 "$pre80"$'\n'"${fb80//00-zz-fb/00-fb}"$'\n'"$wild80")"; [[ $got == foreign ]] || die "vhost order fixture (legacy 00-fb name) -> $got"
  lint_default_server_regression
}

lint_default_server_regression() {
  local dfn got pre post_ok post_bad
  local E=/etc/apache2/sites-enabled
  dfn="$(sed -n '/^default_server()/,/^}/p;/^default_server_check()/,/^}/p' <<<"$REMOTE_LIB")"
  [[ $(grep -c '^default_server' <<<"$dfn") == 2 ]] || die "default_server/default_server_check missing from REMOTE_LIB"
  _ds() { bash -c "$dfn"$'\n''"$@"' _ "$@"; }
  # mk_vs <fb-file-or-empty> <fb-is-default 0|1>: fb vhost inserted right after the pick vhost (apache load order)
  mk_vs() {
    local fbfile="$1" fbdef="$2" d80 d443 out=$'VirtualHost configuration:\n'
    d80="pick.newtalk.kr ($E/00-pick.newtalk.kr.conf:5)"; d443="pick.newtalk.kr ($E/00-pick-ssl.conf:3)"
    if [[ $fbdef == 1 ]]; then d80="fb.newtalk.kr ($E/$fbfile:12)"; d443="fb.newtalk.kr ($E/$fbfile:56)"; fi
    out+=$'*:80                   is a NameVirtualHost\n'"         default server $d80"$'\n'
    [[ $fbdef == 1 && -n $fbfile ]] && out+="         port 80 namevhost fb.newtalk.kr ($E/$fbfile:12)"$'\n'
    out+="         port 80 namevhost pick.newtalk.kr ($E/00-pick.newtalk.kr.conf:5)"$'\n'
    [[ $fbdef == 0 && -n $fbfile ]] && out+="         port 80 namevhost fb.newtalk.kr ($E/$fbfile:12)"$'\n'
    out+="         port 80 namevhost newtalk.kr ($E/000-default.conf:1)"$'\n'"                 wild alias *.newtalk.kr"$'\n'
    out+=$'*:443                  is a NameVirtualHost\n'"         default server $d443"$'\n'
    [[ $fbdef == 1 && -n $fbfile ]] && out+="         port 443 namevhost fb.newtalk.kr ($E/$fbfile:56)"$'\n'
    out+="         port 443 namevhost pick.newtalk.kr ($E/00-pick-ssl.conf:3)"$'\n'
    [[ $fbdef == 0 && -n $fbfile ]] && out+="         port 443 namevhost fb.newtalk.kr ($E/$fbfile:56)"$'\n'
    out+="         port 443 namevhost newtalk.kr ($E/default-ssl.conf:2)"$'\n'"                 wild alias *.newtalk.kr"
    printf '%s' "$out"
  }
  pre="$(mk_vs "" 0)"
  post_ok="$(mk_vs 00-zz-fb.newtalk.kr.conf 0)"   # 00-zz-fb sorts after 00-pick*: default servers stay, fb precedes the wild alias
  post_bad="$(mk_vs 00-fb.newtalk.kr.conf 1)"     # 00-fb sorts before 00-pick*: fb becomes the default server of both ports
  local p80="pick.newtalk.kr ($E/00-pick.newtalk.kr.conf)" p443="pick.newtalk.kr ($E/00-pick-ssl.conf)"
  got="$(_ds default_server 80 "$pre")";  [[ $got == "$p80" ]] || die "default server fixture (:80 parse) -> $got"
  got="$(_ds default_server 443 "$pre")"; [[ $got == "$p443" ]] || die "default server fixture (:443 parse) -> $got"
  got="$(_ds default_server 8080 "$pre")"; [[ $got == none ]] || die "default server fixture (unknown port) -> $got"
  got="$(_ds default_server_check 80 "$p80" "$post_ok")";  [[ $got == ok ]] || die "default server fixture (00-zz-fb keeps :80 default) -> $got"
  got="$(_ds default_server_check 443 "$p443" "$post_ok")"; [[ $got == ok ]] || die "default server fixture (00-zz-fb keeps :443 default) -> $got"
  got="$(_ds default_server_check 80 "$p80" "$post_bad")";  [[ $got == changed ]] || die "default server fixture (fb became :80 default must FAIL) -> $got"
  got="$(_ds default_server_check 443 "$p443" "$post_bad")"; [[ $got == changed ]] || die "default server fixture (fb became :443 default must FAIL) -> $got"
  got="$(_vo 80 "$post_ok")";  [[ $got == ok ]] || die "vhost order fixture (full -S, 00-zz-fb before wild alias, :80) -> $got"
  got="$(_vo 443 "$post_ok")"; [[ $got == ok ]] || die "vhost order fixture (full -S, 00-zz-fb before wild alias, :443) -> $got"
}

cmd_lint() {
  bash -n "${BASH_SOURCE[0]}"
  lint_envvars_regression
  lint_vhost_order_regression
  [[ $FW_PORT == 443 ]] || die "edge firewall must be limited to TCP 443 (FW_PORT=$FW_PORT)"
  grep -q -- '-p tcp -m tcp --dport "\$FW_PORT"' <<<"$FW_REMOTE" || die "FW_REMOTE exact rule lost its tcp/dport restriction"
  [[ -f $TEMPLATE ]] || die "template missing: $TEMPLATE"
  [[ $(grep -c '# BEGIN-ROUTES' "$TEMPLATE") == 2 && $(grep -c '# END-ROUTES' "$TEMPLATE") == 2 ]] \
    || die "template must have exactly two BEGIN/END-ROUTES blocks"
  diff <(routes_block "$TEMPLATE" 1) <(routes_block "$TEMPLATE" 2) >/dev/null \
    || die "port 80 and port 443 route blocks differ"
  grep -q '__FB_UPSTREAM__' "$TEMPLATE" || die "upstream placeholder missing"
  if grep -n -i -E '8210|jinah|jinah244|sk-ant|ANTHROPIC|BEGIN [A-Z ]*PRIVATE KEY' "$TEMPLATE"; then
    die "template references a retired upstream or secret material"
  fi
  render_apache "http://127.0.0.1:1" | grep -q '__FB_UPSTREAM__' && die "placeholder left after render"
  local src="$EDGE_CONF"
  [[ -f $src ]] || src="$REPO_ROOT/nginx-fb.conf"
  [[ -f $src ]] || die "no nginx fb.conf to test rendering against"
  local out; out="$(mktemp)"; trap 'rm -f "${out:-}"; trap - RETURN' RETURN
  render_edge "$src" >"$out"
  ! grep -q 'yeoljeong_finance_api' "$out" || die "edge render left a yeoljeong_finance_api proxy_pass"
  local pp ssl dash_in dash_out
  pp=$(grep -c "proxy_pass https://$CAFE24_IP" "$out"); ssl=$(grep -c 'proxy_ssl_verify on' "$out")
  dash_in=$(grep -c 'aads_dashboard' "$src"); dash_out=$(grep -c 'aads_dashboard' "$out")
  [[ $pp -ge 1 && $pp == "$ssl" ]] || die "edge render: proxy_pass=$pp ssl_verify=$ssl"
  [[ $dash_in == "$dash_out" ]] || die "edge render changed aads_dashboard locations"
  say "lint OK (source=$src, cafe24 proxy_pass=$pp, dashboard locations preserved=$dash_out)"
}

# ---- remote (cafe24) fragments; sent to `bash -s` over ssh -----------------------------------------
read -r -d '' REMOTE_LIB <<'EOS' || true
set -u
fail=0
ok()  { echo "OK   $*"; }
bad() { echo "FAIL $*"; fail=1; }
load_apache_env() { # Debian envvars reads $APACHE_CONFDIR unset-safe only without nounset
  local had_u=0; [[ $- == *u* ]] && had_u=1
  set +u
  : "${APACHE_CONFDIR:=/etc/apache2}"; export APACHE_CONFDIR
  . "${ENVVARS_FILE:-/etc/apache2/envvars}"
  if [[ $had_u == 1 ]]; then set -u; fi
  return 0
}
vhost_order_check() { # $1 = port, $2 = `apache2ctl -S` text; prints ok|missing|foreign|shadowed
  awk -v port="$1" -v fb="$FB" '
    $1 == "port" && $3 == "namevhost" {
      cur = ($2 == port)
      if (cur && $4 == fb && !fbline) { fbline = NR; fbok = ($0 ~ /00-zz-fb\.newtalk\.kr\.conf/) }
      next }
    cur && /alias[ \t]+\*\.newtalk\.kr([ \t]|$)/ && !wildline { wildline = NR }
    END {
      if (!fbline) print "missing"
      else if (!fbok) print "foreign"
      else if (wildline && wildline < fbline) print "shadowed"
      else print "ok" }' <<<"$2"
}
default_server() { # $1 = port, $2 = `apache2ctl -S` text; prints "host (conf-file)" of that port's default server, or none
  awk -v port="$1" '
    $1 ~ /:[0-9]+$/ { n = split($1, a, ":"); cur = (a[n] == port)
      if (cur && $2 != "is" && !found) { found = 1; d = $2 " " $3 }
      next }
    cur && $1 == "default" && $2 == "server" && !found { found = 1; d = $3 " " $4; next }
    END { sub(/:[0-9]+\)$/, ")", d); print (found ? d : "none") }' <<<"$2"
}
default_server_check() { # $1 = port, $2 = default server recorded before install, $3 = `apache2ctl -S` text; prints ok|changed
  [[ $(default_server "$1" "$3") == "$2" ]] && echo ok || echo changed
}
preflight() {
  [[ $(docker inspect -f '{{.State.Running}}' "$APP" 2>/dev/null) == true ]] && ok "container $APP running" || bad "container $APP not running"
  local dg; dg=$(docker inspect -f '{{.Image}}' "$APP" 2>/dev/null)
  [[ -n $EXPECTED && $dg == "$EXPECTED" ]] && ok "image digest $dg" || bad "image digest mismatch: $dg (expected $EXPECTED)"
  local nsid nm; nsid=$(docker inspect -f '{{.Id}}' "$NS" 2>/dev/null); nm=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$APP" 2>/dev/null)
  [[ -n $nsid && $nm == "container:$nsid" ]] && ok "app shares $NS network namespace" || bad "app network mode $nm != container:$NS"
  [[ -z $(docker port "$NS" 2>/dev/null) ]] && ok "$NS publishes no host port" || bad "$NS publishes host ports"
  IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$NS" 2>/dev/null | awk '{print $1}')
  [[ $IP =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && ok "upstream ip $IP" || { bad "cannot resolve $NS ip"; return; }
  local base="http://$IP:$PORT" body code
  body=$(curl -s -m 5 "$base/health/live"); [[ $body == *'"status":"ok"'* ]] && ok "health/live ok" || bad "health/live: $body"
  code=$(curl -s -m 8 -o /dev/null -w '%{http_code}' "$base/static/apps/obys/index.html"); [[ $code == 200 ]] && ok "obys index 200" || bad "obys index $code"
  code=$(curl -s -m 8 -o /dev/null -w '%{http_code}' "$base/api/v1/health"); [[ $code == 401 || $code == 200 ]] && ok "auth layer answers ($code, unauthenticated)" || bad "api/v1/health unauthenticated -> $code"
  local crt=/etc/ssl_newtalk_le/ssl.crt
  openssl x509 -in "$crt" -noout -checkend 1209600 >/dev/null && ok "certificate valid >14d" || bad "certificate expires within 14d"
  openssl x509 -in "$crt" -noout -ext subjectAltName 2>/dev/null | grep -q -E 'DNS:\*\.newtalk\.kr|DNS:fb\.newtalk\.kr' && ok "certificate SAN covers fb.newtalk.kr" || bad "certificate SAN does not cover fb.newtalk.kr"
  local mods; mods=$(apache2ctl -M 2>/dev/null)
  for m in proxy_module proxy_http_module rewrite_module headers_module ssl_module deflate_module; do
    grep -q " $m " <<<"$mods" && ok "apache $m" || bad "apache $m missing"
  done
  local owner; owner=$(apache2ctl -S 2>/dev/null | grep -E "namevhost $FB " | grep -v -E "/(00-zz-fb|10-fb|00-fb)\.newtalk\.kr\.conf" || true)
  [[ -z $owner ]] && ok "no foreign fb vhost" || bad "foreign fb vhost: $owner"
}
EOS

remote_env() { printf 'APP=%q NS=%q PORT=%q EXPECTED=%q FB=%q' "$APP_CONTAINER" "$NS_CONTAINER" "$APP_PORT" "$EXPECTED_DIGEST" "$FB_HOST"; }

cmd_preflight() {
  say "preflight on $CAFE24_SSH (read-only)"
  local out
  out="$(ssh_c "$(remote_env) bash -s" <<<"$REMOTE_LIB"$'\npreflight\nexit $fail')" || { printf '%s\n' "$out"; die "preflight failed"; }
  printf '%s\n' "$out"
}

read -r -d '' REMOTE_APPLY <<'EOS' || true
SITE=/etc/apache2/sites-available/00-zz-fb.newtalk.kr.conf
LINK=/etc/apache2/sites-enabled/00-zz-fb.newtalk.kr.conf
LEGACY=(10-fb.newtalk.kr.conf 00-fb.newtalk.kr.conf)
BK=/root/fb-cutover-backups; mkdir -p "$BK"; ts=$(date +%Y%m%d_%H%M%S)
preflight
[[ $fail == 0 ]] || { echo "preflight failed; nothing changed"; exit 1; }
tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
echo "$TEMPLATE_B64" | base64 -d | sed "s#__FB_UPSTREAM__#http://$IP:$PORT#g" >"$tmp"
grep -q __FB_UPSTREAM__ "$tmp" && { echo "FAIL placeholder left"; exit 1; }
load_apache_env
apache2 -t -C "Include $tmp" 2>&1 | grep -v '^$' ; [[ ${PIPESTATUS[0]} == 0 ]] || { echo "FAIL pre-install configtest"; exit 1; }
pre_vs=$(apache2ctl -S 2>&1) || { echo "FAIL cannot read apache2ctl -S before install; nothing changed"; exit 1; }
declare -A pre_def
for p in 80 443; do pre_def[$p]=$(default_server "$p" "$pre_vs"); done
echo "default server before install: :80 ${pre_def[80]} / :443 ${pre_def[443]}"
[[ ${pre_def[80]} != none && ${pre_def[443]} != none ]] || { echo "FAIL cannot determine default server on :80/:443; nothing changed"; exit 1; }
declare -A base
for h in pick.newtalk.kr shotflow.newtalk.kr v2.newtalk.kr; do
  base[$h]=$(curl -sk -m 10 -o /dev/null -w '%{http_code}' --resolve "$h:443:127.0.0.1" "https://$h/")
done
had_site=false; [[ -f $SITE ]] && { had_site=true; cp -p "$SITE" "$BK/00-zz-fb.newtalk.kr.conf.$ts"; }
declare -A leg_site_bk leg_link_kind leg_link_target leg_link_bk
legacy_found=""
for n in "${LEGACY[@]}"; do
  leg_site_bk[$n]=""; leg_link_kind[$n]=none; leg_link_target[$n]=""; leg_link_bk[$n]=""
  if [[ -f /etc/apache2/sites-available/$n ]]; then
    leg_site_bk[$n]="$BK/$n.legacy-available.$ts"; cp -p "/etc/apache2/sites-available/$n" "${leg_site_bk[$n]}"; legacy_found+=" $n"
  fi
  if [[ -L /etc/apache2/sites-enabled/$n ]]; then
    leg_link_kind[$n]=symlink; leg_link_target[$n]=$(readlink "/etc/apache2/sites-enabled/$n"); legacy_found+=" $n"
  elif [[ -f /etc/apache2/sites-enabled/$n ]]; then
    leg_link_kind[$n]=file; leg_link_bk[$n]="$BK/$n.legacy-enabled.$ts"; cp -p "/etc/apache2/sites-enabled/$n" "${leg_link_bk[$n]}"; legacy_found+=" $n"
  fi
done
rollback() {
  echo "ROLLBACK"
  if $had_site; then cp -p "$BK/00-zz-fb.newtalk.kr.conf.$ts" "$SITE"; else rm -f "$LINK" "$SITE"; fi
  for n in "${LEGACY[@]}"; do
    [[ -n ${leg_site_bk[$n]} ]] && cp -p "${leg_site_bk[$n]}" "/etc/apache2/sites-available/$n"
    case ${leg_link_kind[$n]} in
      symlink) ln -sfn "${leg_link_target[$n]}" "/etc/apache2/sites-enabled/$n" ;;
      file) cp -p "${leg_link_bk[$n]}" "/etc/apache2/sites-enabled/$n" ;;
    esac
  done
  apache2ctl configtest 2>&1 && apache2ctl graceful
}
[[ -z $legacy_found ]] || echo "legacy fb vhost file(s) found:$legacy_found; backed up to $BK and removed (replaced by 00-zz-fb)"
for n in "${LEGACY[@]}"; do rm -f "/etc/apache2/sites-enabled/$n" "/etc/apache2/sites-available/$n"; done
install -m 0644 "$tmp" "$SITE"; ln -sfn ../sites-available/00-zz-fb.newtalk.kr.conf "$LINK"
apache2ctl configtest 2>&1 || { rollback; exit 1; }
apache2ctl graceful; sleep 3
vs=$(apache2ctl -S 2>&1); vo=0
for p in 80 443; do
  r=$(vhost_order_check "$p" "$vs")
  if [[ $r == ok ]]; then echo "OK   vhost order port $p: 00-zz-fb.newtalk.kr.conf serves $FB before any *.newtalk.kr wild alias"
  else echo "FAIL vhost order port $p: $r"; vo=1; fi
done
for p in 80 443; do
  r=$(default_server_check "$p" "${pre_def[$p]}" "$vs")
  if [[ $r == ok ]]; then echo "OK   default server port $p unchanged: ${pre_def[$p]}"
  else echo "FAIL default server port $p changed: before=${pre_def[$p]} after=$(default_server "$p" "$vs")"; vo=1; fi
done
[[ $vo == 0 ]] || { echo "$vs" | grep -E "default server|namevhost|alias" ; rollback; exit 1; }
chk() { # name url expected-code-regex [extra grep on headers]
  local code; code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' --resolve "$FB:$4:127.0.0.1" "$2")
  [[ $code =~ $3 ]] && echo "OK   $1 -> $code" || { echo "FAIL $1 -> $code"; return 1; }
}
vf=0
for scheme in http https; do
  p=80; [[ $scheme == https ]] && p=443
  chk "$scheme /health/live" "$scheme://$FB/health/live" '^200$' $p || vf=1
  chk "$scheme /static/apps/obys/" "$scheme://$FB/static/apps/obys/index.html" '^200$' $p || vf=1
  chk "$scheme / redirect" "$scheme://$FB/" '^302$' $p || vf=1
  chk "$scheme legacy path redirect" "$scheme://$FB/static/apps/yeoljeong-finance/index.html" '^301$' $p || vf=1
  chk "$scheme api unauthenticated" "$scheme://$FB/api/v1/health" '^(401|200)$' $p || vf=1
done
loc=$(curl -s -m 10 -o /dev/null -w '%{redirect_url}' --resolve "$FB:443:127.0.0.1" "https://$FB/")
[[ $loc == https://$FB/static/apps/obys/index.html* ]] && echo "OK   redirect location $loc" || { echo "FAIL redirect location $loc"; vf=1; }
for h in "${!base[@]}"; do
  now=$(curl -sk -m 10 -o /dev/null -w '%{http_code}' --resolve "$h:443:127.0.0.1" "https://$h/")
  [[ $now == "${base[$h]}" ]] && echo "OK   baseline $h ${base[$h]}" || { echo "FAIL baseline $h ${base[$h]} -> $now"; vf=1; }
done
[[ $vf == 0 ]] || { rollback; exit 1; }
echo "APPLIED upstream=http://$IP:$PORT backup=$($had_site && echo "$BK/00-zz-fb.newtalk.kr.conf.$ts" || echo none)"
EOS

cmd_apply_origin() {
  cmd_lint
  [[ -n $EXPECTED_DIGEST ]] || die "EXPECTED_DIGEST required (fail closed)"
  local b64; b64="$(base64 -w0 "$TEMPLATE")"
  say "apply-origin on $CAFE24_SSH (graceful reload only, auto rollback)"
  ssh_c "$(remote_env) TEMPLATE_B64=$b64 bash -s" <<<"$REMOTE_LIB"$'\n'"$REMOTE_APPLY" || die "apply-origin failed (rolled back)"
}

# ---- cafe24 firewall: contabo116 -> origin TCP 443 only --------------------------------------------
# Remote side (bash -s on cafe24). Exact approved rule: -s EDGE_IP/32 -p tcp --dport 443, comment FW_COMMENT.
# "broad" = any other ACCEPT for that source (no tcp/443 restriction). Broad rules are never treated as the
# approved rule, never widened into one, and only deleted when the operator also sets CONFIRM_REMOVE_BROAD_FW=1
# (and then only if they carry our comment).
read -r -d '' FW_REMOTE <<'EOS' || true
set -u
EXACT=(-s "$EDGE_IP/32" -p tcp -m tcp --dport "$FW_PORT" -m comment --comment "$FW_COMMENT" -j ACCEPT)
broad_rules() {
  iptables -S "$FW_CHAIN" 2>/dev/null | awk -v c="$FW_COMMENT" -v ip="$EDGE_IP/32" -v port="$FW_PORT" '
    $1 == "-A" && / -j ACCEPT/ && (index($0, "--comment " c) || index($0, " -s " ip " ")) {
      if ($0 ~ / -p tcp / && $0 ~ (" --dport " port "( |$)")) next
      print }'
}
state() {
  if iptables -C "$FW_CHAIN" "${EXACT[@]}" 2>/dev/null; then EX=present; else EX=absent; fi
  BROAD="$(broad_rules)"; BN=0; [[ -n $BROAD ]] && BN=$(wc -l <<<"$BROAD")
  echo "FWSTATE exact_${FW_PORT}=$EX broad=$BN"
  [[ -z $BROAD ]] || { echo "FWBROAD (not approved, not reused):"; sed 's/^/  /' <<<"$BROAD"; }
}
case "$FW_ACTION" in
  add)
    state
    [[ $BN == 0 ]] || { echo "FAIL broad fb-edge rule present; refusing to treat it as the approved ${FW_PORT}-only rule. Reconcile: CONFIRM_FIREWALL_CHANGE=1 CONFIRM_REMOVE_BROAD_FW=1 revoke-edge-fw"; exit 3; }
    if [[ $EX == present ]]; then echo "OK   exact ${FW_PORT} rule already present (idempotent)"
    else iptables -I "$FW_CHAIN" 1 "${EXACT[@]}" || { echo "FAIL iptables insert"; exit 1; }; fi
    state
    [[ $EX == present && $BN == 0 ]] || { echo "FAIL rule not in the expected state after add"; exit 1; } ;;
  del)
    n=0
    while iptables -C "$FW_CHAIN" "${EXACT[@]}" 2>/dev/null && (( n < 10 )); do
      iptables -D "$FW_CHAIN" "${EXACT[@]}" || { echo "FAIL iptables delete"; exit 1; }
      n=$((n+1))
    done
    state
    if [[ $BN != 0 ]]; then
      if [[ ${FW_REMOVE_BROAD:-0} == 1 ]]; then
        while IFS= read -r line; do
          [[ $line == *"--comment $FW_COMMENT"* ]] || { echo "SKIP not our comment: $line"; continue; }
          read -ra parts <<<"${line#-A $FW_CHAIN }"
          iptables -D "$FW_CHAIN" "${parts[@]}" || { echo "FAIL deleting broad rule"; exit 1; }
          echo "REMOVED broad: $line"
        done <<<"$BROAD"
        state
      else
        echo "WARN broad rule(s) remain; only the approved ${FW_PORT} rule was revoked (CONFIRM_REMOVE_BROAD_FW=1 to remove ours)"
      fi
    fi
    [[ $EX == absent ]] || { echo "FAIL exact rule still present"; exit 1; } ;;
  audit)
    state
    [[ $BN == 0 ]] || exit 3 ;;
  *) echo "bad FW_ACTION"; exit 2 ;;
esac
EOS

fw_remote() { # $1 = add|del|audit
  [[ $EDGE_IP =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "bad EDGE_IP"
  local rb=0; [[ ${CONFIRM_REMOVE_BROAD_FW:-} == 1 ]] && rb=1
  ssh_c "EDGE_IP=$EDGE_IP FW_ACTION=$1 FW_REMOVE_BROAD=$rb FW_CHAIN=$FW_CHAIN FW_COMMENT=$FW_COMMENT FW_PORT=$FW_PORT bash -s" <<<"$FW_REMOTE"
}

fw_rule() { # action: add|del
  [[ ${CONFIRM_FIREWALL_CHANGE:-} == 1 ]] || die "set CONFIRM_FIREWALL_CHANGE=1 (changes cafe24 iptables $FW_CHAIN)"
  fw_remote "$1" || die "edge firewall $1 failed"
  [[ $1 == add ]] && say "rule is TCP $FW_PORT only, not persisted by this script; it vanishes on iptables reload/reboot"
  return 0
}

cmd_edge_fw_status() { fw_remote audit; }

# ---- contabo116 nginx -------------------------------------------------------------------------------
edge_locked() { # run "$@" while holding the shared nginx switch lock; nothing slow belongs inside
  exec 8>"$NGINX_SWITCH_LOCK"
  flock -w 300 8 || die "nginx switch lock busy"
  local rc=0 t0=$SECONDS held
  "$@" || rc=$?
  held=$((SECONDS - t0))
  flock -u 8
  exec 8>&-
  say "nginx switch lock held ${held}s"
  (( held <= NGINX_LOCK_MAX_HOLD )) || say "WARN nginx switch lock held longer than ${NGINX_LOCK_MAX_HOLD}s"
  return $rc
}

edge_marker() { # state marker of this cutover (tiny atomic write; also called inside the lock)
  local tmp; tmp="$(mktemp "$FB_STATE_MARKER.XXXXXX")" || return 1
  printf 'state=%s ts=%s\n' "$1" "$(date -Is)" >"$tmp" && mv -f "$tmp" "$FB_STATE_MARKER"
}

edge_install() { # $1 = rendered file; backs up, tests, reloads. Caller holds the lock.
  local ts bk; ts=$(date +%Y%m%d_%H%M%S); bk="$EDGE_CONF.bak.pre_cafe24_cutover_$ts"
  cp -p "$EDGE_CONF" "$bk" || return 1
  say "backup (audit only, never a rollback target): $bk"
  install -m 0644 "$1" "$EDGE_CONF" || return 1
  docker exec "$NGINX_CONTAINER" nginx -t || { cp -p "$bk" "$EDGE_CONF"; die "nginx -t failed; previous file restored"; }
  docker exec "$NGINX_CONTAINER" nginx -s reload || { say "nginx reload failed"; return 1; }
}

# In-lock check: one bounded request to the local edge, no sleep, no ssh.
routed_health_local() { # $1 = nonce
  local code
  code=$(curl -sk --connect-timeout 2 -m 5 --retry 4 --retry-delay 1 --retry-max-time 6 -o /dev/null -w '%{http_code}' --resolve "$FB_HOST:443:127.0.0.1" "https://$FB_HOST/health/live?probe=$1" || true)
  [[ $code == 200 ]] || { say "routed health -> $code"; return 1; }
}

# Post-unlock proof that the request really reached cafe24 apache (sleep + ssh: never inside the lock).
verify_served_by_cafe24() { # $1 = nonce
  local i
  for i in 1 2 3; do
    sleep 1
    if timeout 30 ssh "${SSH_OPTS[@]}" "$CAFE24_SSH" "grep -c 'probe=$1' /var/log/apache2/$FB_HOST-access.log" 2>/dev/null | grep -qx '[1-9][0-9]*'; then
      say "routed probe $1 seen in cafe24 apache access log"; return 0
    fi
  done
  say "routed probe $1 NOT seen on cafe24 (still served elsewhere)"; return 1
}

edge_qa() { # post-unlock QA through the local edge
  local p c
  for p in /health/live /static/apps/obys/index.html; do
    c=$(curl -sk -m 15 -o /dev/null -w '%{http_code}' --resolve "$FB_HOST:443:127.0.0.1" "https://$FB_HOST$p" || true)
    [[ $c == 200 ]] || { say "QA $p -> $c"; return 1; }
  done
  c=$(curl -sk -m 15 -o /dev/null -w '%{http_code}' --resolve "$FB_HOST:443:127.0.0.1" "https://$FB_HOST/api/v1/health" || true)
  [[ $c == 401 || $c == 200 ]] || { say "QA /api/v1/health -> $c"; return 1; }
}

cmd_apply_edge() {
  cmd_lint
  # ---- prepare: nothing in this block holds the nginx lock ----
  local code nonce out
  code=$(curl -s -m 8 -o /dev/null -w '%{http_code}' --resolve "$FB_HOST:443:$CAFE24_IP" "https://$FB_HOST/health/live" || true)
  [[ $code == 200 ]] || die "cafe24 vhost not reachable from this edge (HTTP $code). Apply the origin first; if TCP 443 times out the cafe24 CF-WEB firewall blocks this host - flip the Cloudflare origin or run allow-edge-fw"
  fw_remote audit >/dev/null || die "cafe24 firewall state unverifiable or a broad (non-443) fb-edge rule exists; run edge-fw-status, nothing changed"
  timeout 30 ssh "${SSH_OPTS[@]}" "$CAFE24_SSH" "test -r /var/log/apache2/$FB_HOST-access.log" || die "cafe24 access log not readable over ssh; nothing changed"
  out="$(mktemp)"; render_edge "$EDGE_CONF" >"$out"
  grep -q "proxy_pass https://$CAFE24_IP" "$out" || { rm -f "$out"; die "rendered edge config has no cafe24 proxy_pass; nothing changed"; }
  nonce="cutover$(date +%s)$RANDOM"
  # ---- lock window: config + marker, nginx -t, reload, one bounded local routed-health ----
  _edge_cutover_locked() {
    edge_marker cafe24-applying || return 1
    edge_install "$out" || return 1
    if ! routed_health_local "$nonce"; then
      say "routed check failed inside the lock -> fb maintenance response (not the retired jinah upstream)"
      edge_maintenance_locked
      return 1
    fi
    edge_marker cafe24-routed-ok
  }
  edge_locked _edge_cutover_locked || { rm -f "$out"; die "apply-edge failed"; }
  rm -f "$out"
  # ---- lock released: cafe24 log proof and QA; a failure re-locks only to roll back ----
  if ! verify_served_by_cafe24 "$nonce" || ! edge_qa; then
    say "post-unlock verification failed -> re-lock and roll back to fb maintenance"
    edge_locked edge_maintenance_locked || say "rollback to maintenance FAILED; edge may still point at cafe24"
    die "apply-edge failed after unlock (rolled back to maintenance)"
  fi
  edge_marker cafe24-verified || true
  say "edge now proxies fb to https://$CAFE24_IP (aads_dashboard locations unchanged)"
}

write_maintenance() {
  cat >"$EDGE_CONF" <<EOF
# fb maintenance response written by cutover_fb_cafe24.sh $(date -Is)
server {
    listen 80;
    server_name $FB_HOST;
    location / { add_header Retry-After 120 always; return 503 "fb.newtalk.kr is under maintenance\n"; }
}
server {
    listen 443 ssl;
    server_name $FB_HOST;
    ssl_certificate /etc/letsencrypt/live/newtalk.kr/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/newtalk.kr/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    location / { add_header Retry-After 120 always; return 503 "fb.newtalk.kr is under maintenance\n"; }
}
EOF
}

edge_maintenance_locked() { # caller holds the lock
  local bk="$EDGE_CONF.bak.pre_maintenance_$(date +%Y%m%d_%H%M%S)"
  cp -p "$EDGE_CONF" "$bk" || return 1
  write_maintenance
  docker exec "$NGINX_CONTAINER" nginx -t || { cp -p "$bk" "$EDGE_CONF"; say "nginx -t failed on maintenance config; restored"; return 1; }
  docker exec "$NGINX_CONTAINER" nginx -s reload || return 1
  edge_marker maintenance || true
}

cmd_maintenance() { edge_locked edge_maintenance_locked || die "maintenance failed"; }

cmd_monitor() {
  local dur="${MONITOR_SECONDS:-300}" end fails=0 n=0 base
  base=$(ssh_c "wc -l < /var/log/apache2/$FB_HOST-access.log" 2>/dev/null || echo 0)
  end=$(( $(date +%s) + dur ))
  say "monitoring https://$FB_HOST for ${dur}s"
  while (( $(date +%s) < end )); do
    for path in /health/live /static/apps/obys/index.html; do
      local c; c=$(curl -s -m 15 -o /dev/null -w '%{http_code}' "https://$FB_HOST$path" || true)
      n=$((n+1)); [[ $c == 200 ]] || { fails=$((fails+1)); say "$path -> $c"; }
    done
    sleep 10
  done
  local five; five=$(ssh_c "tail -n +$((base+1)) /var/log/apache2/$FB_HOST-access.log | awk '\$9 ~ /^5/' | wc -l" 2>/dev/null || echo "?")
  say "requests=$n failures=$fails origin_5xx=$five"
  [[ $fails == 0 && $five == 0 ]] || die "monitoring found errors"
}

cmd_status() {
  say "edge: $(grep -c "proxy_pass https://$CAFE24_IP" "$EDGE_CONF" 2>/dev/null || echo 0) cafe24 locations, $(grep -c 'yeoljeong_finance_api' "$EDGE_CONF" 2>/dev/null || echo 0) legacy upstream refs"
  ssh_c "ls -l /etc/apache2/sites-enabled/00-zz-fb.newtalk.kr.conf 2>&1; apache2ctl -S 2>/dev/null | grep -E 'namevhost $FB_HOST'" || true
}

case "${1:-}" in
  lint) cmd_lint ;;
  preflight) cmd_preflight ;;
  apply-origin) cmd_apply_origin ;;
  allow-edge-fw) fw_rule add ;;
  revoke-edge-fw) fw_rule del ;;
  edge-fw-status) cmd_edge_fw_status ;;
  apply-edge) cmd_apply_edge ;;
  maintenance) cmd_maintenance ;;
  monitor) cmd_monitor ;;
  status) cmd_status ;;
  *) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 2 ;;
esac
