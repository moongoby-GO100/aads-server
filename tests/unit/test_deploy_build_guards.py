from pathlib import Path


ROOT = Path(__file__).parents[2]
DEPLOY_SCRIPT = ROOT / "deploy.sh"
DOCKERFILE = ROOT / "Dockerfile"
COMPILE_SCRIPT = ROOT / "scripts" / "compile_requirements.sh"
PRUNE_SCRIPT = ROOT / "scripts" / "prune_aads_images.sh"


def test_dockerfile_uses_runtime_lock_and_wheelhouse():
    dockerfile = DOCKERFILE.read_text()

    assert "FROM python:3.12-slim AS wheelhouse" in dockerfile
    assert "FROM python:3.12-slim AS runtime" in dockerfile
    assert "requirements.runtime.lock" in dockerfile
    assert "pip wheel --wheel-dir /wheels -r requirements.runtime.lock" in dockerfile
    assert "pip install --no-index --find-links=/wheels -r requirements.runtime.lock" in dockerfile
    assert 'ARG INSTALL_PLAYWRIGHT=false' in dockerfile
    assert 'if [ "$INSTALL_PLAYWRIGHT" = "true" ]' in dockerfile


def test_deploy_script_has_p1_build_preflight_guards():
    script = DEPLOY_SCRIPT.read_text()

    assert "require_dependency_lock_freshness" in script
    assert "emit_release_context_manifest" in script
    assert "report_docker_retention_status" in script
    assert "--target" in script
    assert "AADS_DOCKER_TARGET" in script
    assert "AADS_INSTALL_PLAYWRIGHT" in script
    assert "AADS_DEPLOY_CONTEXT_MANIFEST_TOP_N" in script


def test_requirements_scripts_exist_and_are_safe_by_default():
    compile_script = COMPILE_SCRIPT.read_text()
    prune_script = PRUNE_SCRIPT.read_text()

    assert "piptools compile" in compile_script
    assert "requirements.runtime.lock" in compile_script
    assert "requirements.dev.lock" in compile_script
    assert "requirements.visual.lock" in compile_script
    assert 'MODE="dry-run"' in prune_script
    assert "--execute" in prune_script
    assert "container_refs" in prune_script
    assert "container_ids" in prune_script
    assert "deploy_runs" in prune_script
