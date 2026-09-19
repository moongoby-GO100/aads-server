from pathlib import Path


ROOT = Path(__file__).parents[2]
DEPLOY_SCRIPT = ROOT / "deploy.sh"
DOCKERFILE = ROOT / "Dockerfile"


def test_dockerfile_exposes_a_content_addressable_dependency_stage():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG AADS_RUNTIME_BASE=runtime-deps" in dockerfile
    assert "FROM python:3.12-slim AS runtime-deps" in dockerfile
    assert "FROM ${AADS_RUNTIME_BASE} AS runtime" in dockerfile
    assert dockerfile.index("pip install --no-index") < dockerfile.index(
        "FROM ${AADS_RUNTIME_BASE} AS runtime"
    )
    assert dockerfile.index("FROM ${AADS_RUNTIME_BASE} AS runtime") < dockerfile.index(
        "COPY . ."
    )


def test_deploy_reuses_immutable_dependency_image_before_release_build():
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert 'dependency_image="aads-server-deps:${dependency_key:0:24}"' in script
    assert '--target runtime-deps' in script
    assert '--label "io.aads.dependency-key=${dependency_key}"' in script
    assert '--build-arg "AADS_RUNTIME_BASE=${dependency_image}"' in script
    assert "immutable dependency image mismatch" in script


def test_release_image_is_still_built_once_and_labeled_by_release_sha():
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    build_section = script.split("build_release_image() {", 1)[1].split(
        "if [[ -x \"${COMPOSE_DIR}/scripts/verify-bluegreen-release-contract.sh\" ]]", 1
    )[0]

    assert build_section.count('--tag "aads-server:${AADS_RELEASE_SHA}"') == 1
    assert build_section.count('--label "org.opencontainers.image.revision=${AADS_RELEASE_SHA}"') == 1
    assert build_section.count("docker build") == 1
    assert "--target runtime-deps" not in build_section
    assert "dependency image missing" in build_section
    assert "warm-deps" in build_section
    assert "docker compose" not in build_section


def test_dependency_key_covers_locks_dockerfile_and_runtime_profile():
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "for dependency_file in Dockerfile requirements.runtime.lock requirements.visual.lock" in script
    assert 'sha256sum "${source_dir}/${dependency_file}"' in script
    assert "profile=%s\\nplaywright=%s\\n" in script
    assert 'git -C "$source_dir" show "HEAD:${dependency_file}"' in script


def test_dependency_warmup_is_an_explicit_non_release_mode():
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    warm_section = script.split("build_dependency_image() {", 1)[1].split(
        "build_release_image() {", 1
    )[0]

    assert "bluegreen|warm-deps|code|reload|build" in script
    assert 'if [[ "$MODE" == "warm-deps" ]]' in script
    assert warm_section.count("docker build") == 1
    assert "--target runtime-deps" in warm_section
    assert 'git -C "$COMPOSE_DIR" archive --format=tar HEAD' in script
    assert script.index("audit_control() {") < script.index(
        'if [[ "$MODE" == "warm-deps" ]]'
    )
    assert script.index('if [[ "$MODE" == "warm-deps" ]]') < script.index(
        'ACTIVE_PORT="$(get_active_port)"'
    )


def test_disk_gate_distinguishes_cold_and_warm_dependency_builds():
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert 'disk_profile="cold_dependency_build"' in script
    assert 'elif release_dependency_image_ready "$COMPOSE_DIR"' in script
    assert 'disk_profile="warm_dependency_image"' in script
    assert 'min_free_gb="8"' in script
    assert 'min_free_gb="20"' in script
