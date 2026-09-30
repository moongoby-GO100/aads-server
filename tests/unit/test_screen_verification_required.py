import pytest

from app.services.e2e_verify import _renders_nothing, screen_verification_required


SCREEN_MARKER = "Please take a screenshot of this change"


def test_marker_with_only_backend_files_is_exempt():
    assert screen_verification_required(
        SCREEN_MARKER, ["app/services/sample_backend.py", "tests/unit/test_sample.py"]
    ) is False


def test_marker_with_empty_changed_files_requires_verification():
    assert screen_verification_required(SCREEN_MARKER, []) is True


def test_marker_with_none_changed_files_requires_verification():
    assert screen_verification_required(SCREEN_MARKER, None) is True


def test_marker_with_component_file_requires_verification():
    assert screen_verification_required(SCREEN_MARKER, ["src/components/Sample.tsx"]) is True


def test_marker_with_static_javascript_file_requires_verification():
    assert screen_verification_required(
        SCREEN_MARKER, ["app/static/apps/obys/modules/ledger-details.js"]
    ) is True


def test_marker_with_backend_and_static_javascript_requires_verification():
    assert screen_verification_required(
        SCREEN_MARKER,
        ["app/services/sample_backend.py", "app/static/apps/obys/modules/ledger-details.js"],
    ) is True


def test_without_marker_existing_template_rule_is_preserved():
    assert screen_verification_required("Update the backend", ["app/templates/sample.html"]) is True


@pytest.mark.parametrize(
    "path",
    [
        ".gitignore", ".dockerignore", ".gitattributes", "Dockerfile", "Makefile", "LICENSE",
        "CODEOWNERS", "scripts/reap_stale_processes", "app/static/Dockerfile", "frontend/.gitignore",
    ],
)
def test_extensionless_paths_render_nothing(path):
    assert _renders_nothing(path) is True


@pytest.mark.parametrize(
    "path",
    ["aads-dashboard/src/app/page.tsx", "app/static/style.css", "app/static/x.foo", "app/static/", "app/templates/config.json"],
)
def test_renderable_or_unknown_extension_paths_are_not_exempt(path):
    assert _renders_nothing(path) is False


def test_json_data_file_outside_ui_tree_still_renders_nothing():
    assert _renders_nothing("deploy/manifest.json") is True


def test_marker_with_only_extensionless_and_shell_files_is_exempt():
    assert screen_verification_required(SCREEN_MARKER, [".gitignore", "scripts/pipeline-runner.sh"]) is False
    assert screen_verification_required("화면 증거 게이트", [".gitignore", "scripts/pipeline-runner.sh"]) is False


def test_marker_with_tsx_among_extensionless_files_requires_verification():
    assert screen_verification_required(
        "화면 증거 게이트", [".gitignore", "aads-dashboard/src/app/page.tsx"]
    ) is True
