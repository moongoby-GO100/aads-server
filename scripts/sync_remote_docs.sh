#!/usr/bin/env bash
# 원격 서버 문서를 contabo116 한 곳으로 모은다 — 색인 정본은 doc_chunks 한 벌이다.
#
# 2026-09-19 실측. doc_chunks 의 project 가 AADS·GO100 둘뿐이었다.
# KIS/SF/NTV2/NAS 문서는 한 건도 색인돼 있지 않았고, 그래서 채팅이 그 문서를
# 근거로 쓰지 못했다.
#
# 원격 서버에서 index_docs.py 를 돌리려면 그 서버마다 DB 접속 경로(PGHOST
# 터널)를 뚫고 스크립트 사본을 둬야 한다. 사본을 두지 말라는 규약과 정면으로
# 부딪힌다 — 2026-09-14 kiwoom_key_manager 가 사본이 갈라져서 난 사고다.
# 그래서 반대로 **문서를 당겨온다.** 색인기는 contabo116 한 곳에서만 돈다.
#
#   sync_remote_docs.sh
#
# 당긴 결과는 /root/aads/_remote_docs 아래 서버별로 쌓이고, index_docs.py 의
# ROOTS 가 그 경로를 프로젝트 라벨과 함께 읽는다. 원본은 건드리지 않는다
# (단방향 rsync, --delete 는 스테이징 쪽에만 걸린다).
set -uo pipefail

DEST_ROOT="${REMOTE_DOCS_ROOT:-/root/aads/_remote_docs}"
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=15"
# .md 만 가져온다. 디렉터리는 통과시키되(--include='*/') 나머지는 전부 뺀다.
FILTER=(--prune-empty-dirs --include='*/' --include='*.md' --exclude='*')
OPTS=(-az --timeout=120 --delete
      --exclude='node_modules/' --exclude='.git/' --exclude='.venvs/'
      --exclude='backups/' --exclude='backup/' --exclude='dist/' --exclude='build/')

pull() {  # pull <호스트> <원격경로> <스테이징 상대경로>
  local host="$1" src="$2" rel="$3"
  local dest="$DEST_ROOT/$rel"
  mkdir -p "$dest"
  if ! timeout 600 rsync "${OPTS[@]}" "${FILTER[@]}" -e "ssh $SSH_OPTS" \
        "root@${host}:${src}/" "${dest}/"; then
    echo "[sync_remote_docs] 실패: ${host}:${src}" >&2
    return 1
  fi
  printf '[sync_remote_docs] %-44s %s개\n' "$rel" "$(find "$dest" -name '*.md' | wc -l)"
}

fail=0
# contabo14 — KIS / GO100
pull 5.104.86.14    /root/kis-autotrade-v4/docs     contabo14/kis-autotrade-v4/docs    || fail=1
pull 5.104.86.14    /root/kis-autotrade-v4/report   contabo14/kis-autotrade-v4/report  || fail=1
pull 5.104.86.14    /root/kis-autotrade-v4/reports  contabo14/kis-autotrade-v4/reports || fail=1
# cafe24_114 — SF / NTV2 / NAS
pull 114.207.244.86 /data/shortflow/docs            cafe24_114/shortflow/docs          || fail=1
pull 114.207.244.86 /data/shortflow/reports         cafe24_114/shortflow/reports       || fail=1
pull 114.207.244.86 /srv/newtalk-v2/docs            cafe24_114/newtalk-v2/docs         || fail=1
pull 114.207.244.86 /srv/newtalk-v2/reports         cafe24_114/newtalk-v2/reports      || fail=1
pull 114.207.244.86 /srv/newtalk-v2/project-docs-repo/nas-image cafe24_114/nas-image   || fail=1

echo "[sync_remote_docs] 합계 $(find "$DEST_ROOT" -name '*.md' | wc -l)개 → index_docs.py index 로 색인한다"
exit $fail
