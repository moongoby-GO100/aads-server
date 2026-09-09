#!/bin/bash
# Replace only the FOOD store-assistant API from an already-built AADS image.

set -euo pipefail
trap '' HUP TERM  # RC4: ignore HUP/TERM — prevents parent process kill from aborting deploy

RELEASE_SHA="${1:-}"
RUN_ID="${2:-0}"
REPO="${AADS_DEPLOY_REPO_DIR:-/root/aads/aads-server}"
COMPOSE="${REPO}/docker-compose.prod.yml"
ENV_FILE="${AADS_RUNTIME_ENV_FILE:-${REPO}/.env}"
IMAGE="aads-server:${RELEASE_SHA}"
COMPOSE_IMAGE="aads-server-yeoljeong-finance:latest"
LOCK="/tmp/aads-food-store-assistant-deploy.lock"

if [[ ! "$RELEASE_SHA" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
    echo "invalid release SHA"
    exit 2
fi

exec 9>"$LOCK"
if ! flock -n 9; then
    echo "FOOD deploy already running"
    exit 3
fi

git -C "$REPO" cat-file -e "${RELEASE_SHA}^{commit}"
head_sha="$(git -C "$REPO" rev-parse HEAD)"
if [[ "$head_sha" != "$RELEASE_SHA" && "$head_sha" != "$RELEASE_SHA"* ]]; then
    echo "FOOD deploy blocked: host HEAD ${head_sha:0:12} != release ${RELEASE_SHA:0:12}"
    exit 4
fi

dirty="$(git -C "$REPO" status --porcelain -- app migrations scripts docker-compose.prod.yml)"
if [[ -n "$dirty" ]]; then
    echo "FOOD deploy blocked: release paths are dirty"
    exit 5
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "FOOD deploy blocked: immutable image missing: ${IMAGE}"
    exit 6
fi

previous_image="$(docker inspect yeoljeong-finance --format '{{.Image}}' 2>/dev/null || true)"
docker tag "$IMAGE" "$COMPOSE_IMAGE"

rollback() {
    if [[ -n "$previous_image" ]]; then
        echo "FOOD routed health failed; restoring previous image"
        docker tag "$previous_image" "$COMPOSE_IMAGE" || true
        AADS_RELEASE_SHA="$RELEASE_SHA" docker compose --env-file "$ENV_FILE" -f "$COMPOSE" \
            up -d --no-deps --no-build yeoljeong-finance || true
    fi
}

AADS_RELEASE_SHA="$RELEASE_SHA" docker compose --env-file "$ENV_FILE" -f "$COMPOSE" \
    up -d --no-deps --no-build yeoljeong-finance

for _attempt in $(seq 1 30); do
    if curl -fsS --max-time 5 http://127.0.0.1:8110/health/live >/dev/null; then
        break
    fi
    sleep 2
done

if ! curl -fsS --max-time 10 http://127.0.0.1:8110/health/live >/dev/null; then
    rollback
    exit 7
fi
if ! curl -fsS --max-time 15 https://fb.newtalk.kr/health/live >/dev/null; then
    rollback
    exit 8
fi

current_image="$(docker inspect yeoljeong-finance --format '{{.Image}}')"
expected_image="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
if [[ "$current_image" != "$expected_image" ]]; then
    echo "FOOD deploy failed: running image digest differs from release image"
    rollback
    exit 9
fi

echo "FOOD store-assistant certified: run=${RUN_ID} sha=${RELEASE_SHA:0:12} image=${current_image}"
