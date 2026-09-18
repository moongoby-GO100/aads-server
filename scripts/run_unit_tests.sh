#!/bin/bash
# 워킹트리 코드로 단위 테스트를 실행한다.
#
# 운영 런타임 이미지에는 pytest 가 없다(requirements.runtime.lock 에 미포함).
# 그래서 `docker exec aads-server python3 -m pytest` 는 "No module named pytest" 만
# 남기고 끝났고, pre-commit 은 그 출력을 실패로 읽지 못해 게이트가 조용히 통과했다.
#
# 여기서는 운영 이미지를 그대로 쓰되
#   - pytest 는 호스트 디렉터리에 따로 설치해 PYTHONPATH 로 얹고 (운영 site-packages 불변)
#   - 리포지터리를 /app 에 마운트해서 커밋하려는 코드를 검증한다
#     (컨테이너에 구워진 옛 코드가 아니라).
#
# 종료코드: 0 통과 / 1 테스트 실패 / 2 실행 불가(게이트 미작동 → 반드시 차단)

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTDEPS_DIR="${AADS_TESTDEPS_DIR:-/root/aads/.testdeps}"
ACTIVE_CONTAINER_FILE="${AADS_ACTIVE_CONTAINER_FILE:-/root/aads/aads-server/.active_container}"

targets=("$@")
if [ ${#targets[@]} -eq 0 ]; then
    targets=("tests/unit")
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "[run_unit_tests] docker 를 찾을 수 없습니다 — 단위 테스트를 실행할 수 없습니다." >&2
    exit 2
fi

# 운영 컨테이너는 블루그린이라 실제 이름이 aads-server-blue / aads-server-green 이다.
# 컷오버 창에는 별칭 aads-server 가 잠깐 사라질 수 있으므로 폴백 사슬로 찾는다:
#   $AADS_TEST_IMAGE_SOURCE → aads-server → .active_container 값 →
#   docker ps 로 찾은 healthy 한 aads-server-* 컨테이너 →
#   health 필터 없는 aads-server-* 컨테이너(교체 중이라 아직 starting 이어도
#   이미지 자체는 멀쩡하다) → 컨테이너가 하나도 없을 때 이미지 태그 직접 조회.
#
# 마지막 후보만 "image:" 접두를 달아 종류를 구분한다 — 컨테이너 후보는 이름 그대로
# docker inspect 로, image: 후보는 접두를 뗀 값을 이미지 참조로 바로 쓴다.
resolve_test_image_candidates() {
    local candidates=()

    if [ -n "${AADS_TEST_IMAGE_SOURCE:-}" ]; then
        candidates+=("$AADS_TEST_IMAGE_SOURCE")
    fi
    candidates+=("aads-server")

    if [ -f "$ACTIVE_CONTAINER_FILE" ]; then
        local active
        active="$(tr -d '[:space:]' < "$ACTIVE_CONTAINER_FILE" 2>/dev/null)"
        [ -n "$active" ] && candidates+=("$active")
    fi

    local bg_name
    while IFS= read -r bg_name; do
        [ -n "$bg_name" ] && candidates+=("$bg_name")
    done < <(docker ps --filter "name=aads-server-" --filter "health=healthy" --format '{{.Names}}' 2>/dev/null)

    # health 필터 없는 판 — 컷오버 중 새 컨테이너가 아직 starting 이어도 이미지는 쓸 수 있다.
    local bg_name_any
    while IFS= read -r bg_name_any; do
        [ -n "$bg_name_any" ] && candidates+=("$bg_name_any")
    done < <(docker ps --filter "name=aads-server-" --format '{{.Names}}' 2>/dev/null)

    # 컨테이너가 하나도 없을 때의 마지막 폴백 — 이미지 태그를 직접 조회한다.
    local latest_image
    latest_image="$(docker images --format '{{.Repository}}:{{.Tag}}' --filter "reference=aads-server:*" 2>/dev/null | head -1)"
    [ -n "$latest_image" ] && candidates+=("image:$latest_image")

    # 중복 제거(순서 보존) — 같은 후보를 두 번 조회하지 않는다.
    local seen=":" out=() c
    for c in "${candidates[@]}"; do
        case "$seen" in
            *":$c:"*) continue ;;
        esac
        seen="${seen}${c}:"
        out+=("$c")
    done
    printf '%s\n' "${out[@]}"
}

mapfile -t RUNTIME_CANDIDATES < <(resolve_test_image_candidates)
if [ ${#RUNTIME_CANDIDATES[@]} -eq 0 ]; then
    echo "[run_unit_tests] 기준 컨테이너 후보가 없습니다 — 단위 테스트를 실행할 수 없습니다." >&2
    exit 2
fi

# 컷오버 창이 대체로 30초 안쪽이라 5초 간격 3회(15초)로는 창을 못 넘길 때가 있었다.
# 6회(30초)로 늘려 창을 넘기도록 한다.
RETRY_ATTEMPTS=6
RETRY_INTERVAL_SECONDS=5

IMAGE=""
for attempt in $(seq 1 "$RETRY_ATTEMPTS"); do
    for candidate in "${RUNTIME_CANDIDATES[@]}"; do
        case "$candidate" in
            image:*)
                IMAGE="${candidate#image:}"
                ;;
            *)
                IMAGE="$(docker inspect -f '{{.Config.Image}}' "$candidate" 2>/dev/null)"
                ;;
        esac
        [ -n "$IMAGE" ] && break 2
    done
    [ "$attempt" -lt "$RETRY_ATTEMPTS" ] && sleep "$RETRY_INTERVAL_SECONDS"
done

if [ -z "$IMAGE" ]; then
    {
        echo "[run_unit_tests] 기준 이미지를 찾지 못했습니다 (${RETRY_INTERVAL_SECONDS}초 간격 ${RETRY_ATTEMPTS}회 재시도) — 시도한 후보와 실패 사유:"
        for candidate in "${RUNTIME_CANDIDATES[@]}"; do
            case "$candidate" in
                image:*)
                    ref="${candidate#image:}"
                    echo "  - image:$ref -> image not found"
                    ;;
                *)
                    if docker inspect "$candidate" >/dev/null 2>&1; then
                        echo "  - container:$candidate -> 컨테이너는 있으나 이미지 조회 실패"
                    else
                        echo "  - container:$candidate -> no such container"
                    fi
                    ;;
            esac
        done
        echo "[run_unit_tests] docker ps --filter name=aads-server 요약:"
        docker ps --filter "name=aads-server" --format '  {{.Names}}  {{.Status}}' 2>/dev/null
    } >&2
    exit 2
fi

mkdir -p "$TESTDEPS_DIR"
if ! docker run --rm -v "$TESTDEPS_DIR":/testdeps "$IMAGE" \
        python3 -c 'import sys; sys.path.insert(0, "/testdeps"); import pytest' >/dev/null 2>&1; then
    echo "[run_unit_tests] pytest 준비 중 (런타임 이미지 미포함 — 별도 경로에 설치)..."
    if ! docker run --rm -v "$TESTDEPS_DIR":/testdeps "$IMAGE" \
            pip install --no-cache-dir --target /testdeps \
            pytest==9.1.1 pytest-asyncio==1.4.0 >/dev/null 2>&1; then
        echo "[run_unit_tests] pytest 설치 실패 — 단위 테스트를 실행할 수 없습니다." >&2
        exit 2
    fi
fi

# 대시보드 정적 테스트는 리포지터리 밖의 ../aads-dashboard 를 읽는다.
# 마운트하지 않으면 경로가 없어 조용히 aads-server 안의 낡은 사본으로 폴백하고,
# 운영에 배포된 적 없는 코드를 검증하게 된다 — 게이트가 거짓말하는 또 하나의 경로다.
DASHBOARD_ROOT="${AADS_DASHBOARD_ROOT:-$(cd "$REPO_ROOT/../aads-dashboard" 2>/dev/null && pwd)}"
dashboard_mount=()
if [ -n "$DASHBOARD_ROOT" ] && [ -d "$DASHBOARD_ROOT" ]; then
    # 컨테이너 작업 디렉터리가 /app 이므로 ../aads-dashboard 는 /aads-dashboard 다.
    dashboard_mount=(-v "$DASHBOARD_ROOT":/aads-dashboard:ro)
else
    echo "[run_unit_tests] 대시보드 소스를 찾지 못했습니다 — 대시보드 정적 테스트는 낡은 사본을 검증하게 됩니다." >&2
fi

# 원격 파일 도구 테스트는 host.docker.internal 로 SSH 를 건다.
# 운영 컨테이너가 가진 것과 같은 조건을 임시 컨테이너에도 준다.
docker run --rm \
    --add-host host.docker.internal:host-gateway \
    -v /root/.ssh:/root/.ssh:ro \
    -v "$REPO_ROOT":/app \
    "${dashboard_mount[@]}" \
    -v "$TESTDEPS_DIR":/testdeps \
    -w /app \
    -e PYTHONPATH=/testdeps \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e JWT_SECRET_KEY="${JWT_SECRET_KEY:-unit-test-secret-not-for-production}" \
    "$IMAGE" python3 -m pytest "${targets[@]}" -q --tb=line -p no:cacheprovider
rc=$?

# pytest 5 = 수집된 테스트 없음. 게이트 입장에서는 검증이 안 된 것이므로 실행 불가로 본다.
if [ $rc -eq 5 ]; then
    echo "[run_unit_tests] 수집된 테스트가 없습니다 — 게이트가 아무것도 검증하지 못했습니다." >&2
    exit 2
fi
exit $rc
