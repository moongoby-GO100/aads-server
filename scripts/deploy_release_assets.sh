#!/bin/bash
# Apply committed, non-destructive DB/config/prompt SQL from one release SHA.

set -euo pipefail

MODE="${1:-}"
RELEASE_SHA="${2:-}"
RUN_ID="${3:-0}"
REPO="${AADS_DEPLOY_REPO_DIR:-/root/aads/aads-server}"

case "$MODE" in
    db|config|prompt) ;;
    *) echo "mode must be db, config, or prompt"; exit 2 ;;
esac
if [[ ! "$RELEASE_SHA" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
    echo "invalid release SHA"
    exit 2
fi
git -C "$REPO" cat-file -e "${RELEASE_SHA}^{commit}"

release_dir="$(mktemp -d /tmp/aads-release-assets.XXXXXX)"
cleanup() {
    git -C "$REPO" worktree remove --force "$release_dir" >/dev/null 2>&1 || true
}
trap cleanup EXIT
git -C "$REPO" worktree add --detach "$release_dir" "$RELEASE_SHA" >/dev/null

base_sha="$(docker exec aads-postgres psql -U aads -d aads -qAtc \
    "SELECT release_sha FROM deploy_runs WHERE project='AADS' AND component='${MODE}' AND status='success' AND id<>${RUN_ID} ORDER BY id DESC LIMIT 1;" \
    2>/dev/null || true)"
if [[ -z "$base_sha" ]] || ! git -C "$REPO" cat-file -e "${base_sha}^{commit}" 2>/dev/null; then
    base_sha="${RELEASE_SHA}^"
fi
mapfile -t changed < <(git -C "$release_dir" diff --name-only "$base_sha" "$RELEASE_SHA" -- migrations scripts | sort -u)
selected=()
for relative in "${changed[@]}"; do
    [[ "$relative" == migrations/*.sql || "$relative" == scripts/*.sql ]] || continue
    file="$release_dir/$relative"
    [[ -f "$file" ]] || continue
    asset_class="db"
    if [[ "$relative" =~ (prompt|role|llmops) ]] || grep -Eqi '(prompt_assets|compiled_prompt_provenance)' "$file"; then
        asset_class="prompt"
    elif [[ "$relative" =~ (config|setting|routing|model|provider) ]] || grep -Eqi '(model_routing|settings|config)' "$file"; then
        asset_class="config"
    fi
    if [[ "$MODE" == "$asset_class" ]]; then
        selected+=("$relative")
    fi
done

before_tables="$(docker exec aads-postgres psql -U aads -d aads -qAtc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")"
before_prompts="$(docker exec aads-postgres psql -U aads -d aads -qAtc \
    "SELECT count(*) FROM prompt_assets;" 2>/dev/null || echo unavailable)"

for relative in "${selected[@]}"; do
    file="$release_dir/$relative"
    if grep -Eqi '(^|[[:space:];])(DROP|TRUNCATE)[[:space:]]' "$file"; then
        echo "blocked destructive SQL in ${relative}"
        exit 3
    fi
    echo "applying ${MODE} asset: ${relative}"
    docker exec -i aads-postgres psql -v ON_ERROR_STOP=1 -U aads -d aads < "$file"
done

after_tables="$(docker exec aads-postgres psql -U aads -d aads -qAtc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")"
after_prompts="$(docker exec aads-postgres psql -U aads -d aads -qAtc \
    "SELECT count(*) FROM prompt_assets;" 2>/dev/null || echo unavailable)"

if [[ "$MODE" == "prompt" || "$MODE" == "config" ]]; then
    docker exec aads-postgres psql -U aads -d aads -qAtc \
        "SELECT count(*) FROM prompt_assets WHERE enabled IS TRUE;" >/dev/null
    docker exec aads-postgres psql -U aads -d aads -qAtc \
        "SELECT count(*) FROM compiled_prompt_provenance WHERE COALESCE(provenance->>'compile_error','') <> '';" >/dev/null
fi

echo "release assets certified: mode=${MODE} run=${RUN_ID} files=${#selected[@]} tables=${before_tables}->${after_tables} prompts=${before_prompts}->${after_prompts}"
