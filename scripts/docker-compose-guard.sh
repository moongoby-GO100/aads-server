#!/bin/bash
set -euo pipefail

# AADS Docker Compose Guard
# 배경: 2026-08-20 인시던트 — 사람이 직접 `docker compose up -d --force-recreate`류
#       명령을 실행하여 Blue/Green 컨테이너가 동시에 강제 재생성, 약 26분 다운.
#
# 이 스크립트는 docker compose 직접 호출을 감싸서 위험 패턴을 사전 차단한다.
# 사용법: scripts/docker-compose-guard.sh <docker compose 인자들...>
#   예) scripts/docker-compose-guard.sh up -d --no-deps aads-server   → 허용
#       scripts/docker-compose-guard.sh up -d --force-recreate       → 차단
#       scripts/docker-compose-guard.sh up -d                        → 차단(서비스명 없음)
#
# 우회: AADS_ALLOW_DIRECT_COMPOSE=true scripts/docker-compose-guard.sh ...
#       (CEO 승인 하 점검 창구에서만 사용)

WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTIVE_CONTAINER_FILE="${WORKDIR}/.active_container"

ARGS=("$@")
ARGS_STR="$*"

if [ "${AADS_ALLOW_DIRECT_COMPOSE:-false}" = "true" ]; then
  echo "[WARN] AADS_ALLOW_DIRECT_COMPOSE=true — 가드를 우회하여 직접 실행합니다." >&2
  exec docker compose "${ARGS[@]}"
fi

# 1) --force-recreate 차단
if [[ "$ARGS_STR" == *"--force-recreate"* ]]; then
  echo "[BLOCKED] --force-recreate는 Blue/Green 동시 재생성 위험이 있어 차단됩니다." >&2
  echo "          무중단 배포는 'deploy.sh bluegreen'을 사용하세요." >&2
  echo "          부득이하게 필요하면 AADS_ALLOW_DIRECT_COMPOSE=true 를 설정하세요 (CEO 승인 필요)." >&2
  exit 1
fi

# 2) 서비스명 없는 bare `up -d` (전체 재기동) 차단
#    마지막 두 인자가 정확히 "up" "-d" 이면 뒤에 서비스명이 없는 전체 up으로 간주
_arg_count=${#ARGS[@]}
if [ "$_arg_count" -ge 2 ]; then
  _last_idx=$((_arg_count - 1))
  _prev_idx=$((_arg_count - 2))
  if [ "${ARGS[$_last_idx]}" = "-d" ] && [ "${ARGS[$_prev_idx]}" = "up" ]; then
    echo "[BLOCKED] 서비스명 없는 'docker compose up -d' 전체 재기동은 차단됩니다." >&2
    echo "          postgres/litellm/aads-server가 동시 재생성되어 서비스 전체가 중단될 수 있습니다." >&2
    echo "          단일 서비스를 지정하거나(--no-deps <service>) 'deploy.sh bluegreen'을 사용하세요." >&2
    exit 1
  fi
elif [ "$_arg_count" -eq 1 ] && [ "${ARGS[0]}" = "up" ]; then
  # `docker compose up` (단독, -d도 서비스명도 없음)
  echo "[BLOCKED] 서비스명 없는 'docker compose up' 전체 재기동은 차단됩니다." >&2
  echo "          단일 서비스를 지정하거나 'deploy.sh bluegreen'을 사용하세요." >&2
  exit 1
fi

# 3) 대상이 현재 ACTIVE 슬롯 컨테이너인 경우 경고만 표시 (차단하지 않음)
if [ -f "$ACTIVE_CONTAINER_FILE" ]; then
  ACTIVE_CONTAINER="$(cat "$ACTIVE_CONTAINER_FILE" 2>/dev/null || echo '')"
  if [ -n "$ACTIVE_CONTAINER" ] && [[ "$ARGS_STR" == *"$ACTIVE_CONTAINER"* ]]; then
    echo "[경고] 현재 ACTIVE 슬롯(${ACTIVE_CONTAINER})을 직접 대상으로 하는 명령입니다." >&2
    echo "        무중단 배포가 필요하면 'deploy.sh bluegreen' 사용을 권장합니다." >&2
  fi
fi

exec docker compose "${ARGS[@]}"
