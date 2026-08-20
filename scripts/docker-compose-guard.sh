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

option_takes_value() {
  case "$1" in
    -f|--file|-p|--project-name|--profile|--project-directory|--env-file|--parallel|--ansi|--progress)
      return 0
      ;;
    --file=*|--project-name=*|--profile=*|--project-directory=*|--env-file=*|--parallel=*|--ansi=*|--progress=*)
      return 1
      ;;
    *)
      return 1
      ;;
  esac
}

up_option_takes_value() {
  case "$1" in
    --scale|--timeout|--exit-code-from|--abort-on-container-exit|--abort-on-container-failure|--attach|--attach-dependencies|--menu)
      return 0
      ;;
    --scale=*|--timeout=*|--exit-code-from=*|--abort-on-container-exit=*|--abort-on-container-failure=*|--attach=*|--attach-dependencies=*|--menu=*)
      return 1
      ;;
    *)
      return 1
      ;;
  esac
}

is_service_optionless_up_flag() {
  case "$1" in
    -d|--detach|--build|--no-build|--no-deps|--pull|--force-recreate|--no-recreate|--remove-orphans|--renew-anon-volumes|-V|--wait|--quiet-pull|--always-recreate-deps)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

find_subcommand_index() {
  local i=0
  while [ "$i" -lt "${#ARGS[@]}" ]; do
    local token="${ARGS[$i]}"
    if option_takes_value "$token"; then
      i=$((i + 2))
      continue
    fi
    if [[ "$token" == --*=* ]]; then
      i=$((i + 1))
      continue
    fi
    if [[ "$token" == -* ]]; then
      i=$((i + 1))
      continue
    fi
    echo "$i"
    return 0
  done
  echo "-1"
}

up_service_count() {
  local sub_idx="$1"
  local i=$((sub_idx + 1))
  local count=0
  while [ "$i" -lt "${#ARGS[@]}" ]; do
    local token="${ARGS[$i]}"
    if up_option_takes_value "$token"; then
      i=$((i + 2))
      continue
    fi
    if [[ "$token" == --*=* ]]; then
      i=$((i + 1))
      continue
    fi
    if is_service_optionless_up_flag "$token" || [[ "$token" == -* ]]; then
      i=$((i + 1))
      continue
    fi
    count=$((count + 1))
    i=$((i + 1))
  done
  echo "$count"
}

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

SUBCOMMAND_INDEX="$(find_subcommand_index)"
SUBCOMMAND=""
if [ "$SUBCOMMAND_INDEX" -ge 0 ]; then
  SUBCOMMAND="${ARGS[$SUBCOMMAND_INDEX]}"
fi

# 2) 서비스명 없는 bare `up` 또는 --no-deps 없는 `up` 차단
if [ "$SUBCOMMAND" = "up" ]; then
  SERVICE_COUNT="$(up_service_count "$SUBCOMMAND_INDEX")"
  if [ "$SERVICE_COUNT" -eq 0 ]; then
    echo "[BLOCKED] 서비스명 없는 'docker compose up' 전체 재기동은 차단됩니다." >&2
    echo "          postgres/litellm/aads-server가 동시 재생성되어 서비스 전체가 중단될 수 있습니다." >&2
    echo "          단일 서비스를 지정하거나(--no-deps <service>) 'deploy.sh bluegreen'을 사용하세요." >&2
    exit 1
  fi
  if [[ "$ARGS_STR" != *"--no-deps"* ]]; then
    echo "[BLOCKED] '--no-deps' 없는 'docker compose up <service>'는 의존 컨테이너 재생성 위험이 있어 차단됩니다." >&2
    echo "          단일 서비스 직접 기동은 '--no-deps <service>'를 명시하거나 'deploy.sh bluegreen'을 사용하세요." >&2
    exit 1
  fi
elif [ "$SUBCOMMAND" = "down" ]; then
  echo "[BLOCKED] 'docker compose down'은 데이터/의존 컨테이너 중단 위험이 있어 차단됩니다." >&2
  echo "          무중단 배포는 'deploy.sh bluegreen'을 사용하세요." >&2
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

if [ "${AADS_COMPOSE_GUARD_DRY_RUN:-false}" = "true" ]; then
  echo "[DRY-RUN] docker compose ${ARGS_STR}"
  exit 0
fi

exec docker compose "${ARGS[@]}"
