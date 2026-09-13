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
RUNTIME_CONTAINER="${AADS_TEST_IMAGE_SOURCE:-aads-server}"

targets=("$@")
if [ ${#targets[@]} -eq 0 ]; then
    targets=("tests/unit")
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "[run_unit_tests] docker 를 찾을 수 없습니다 — 단위 테스트를 실행할 수 없습니다." >&2
    exit 2
fi

IMAGE="$(docker inspect -f '{{.Config.Image}}' "$RUNTIME_CONTAINER" 2>/dev/null)"
if [ -z "$IMAGE" ]; then
    echo "[run_unit_tests] ${RUNTIME_CONTAINER} 컨테이너 이미지를 찾지 못했습니다 — 단위 테스트를 실행할 수 없습니다." >&2
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

# 원격 파일 도구 테스트는 host.docker.internal 로 SSH 를 건다.
# 운영 컨테이너가 가진 것과 같은 조건을 임시 컨테이너에도 준다.
docker run --rm \
    --add-host host.docker.internal:host-gateway \
    -v /root/.ssh:/root/.ssh:ro \
    -v "$REPO_ROOT":/app \
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
