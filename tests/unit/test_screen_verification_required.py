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


# runner-1994e9b0 / runner-5bd2c7a7: host unit/config files under a UI-looking path were misclassified.
@pytest.mark.parametrize(
    "path",
    [
        "scripts/systemd/aads-runner.service",
        "deploy/app/aads-watchdog.timer",
        "deploy/nginx/conf.d/aads.conf",
        "app/static/aads-runner.service",
        "frontend/components/nginx.conf",
        "aads-dashboard/src/app/deploy.timer",
    ],
)
@pytest.mark.parametrize("instruction", [SCREEN_MARKER, "화면 증거 게이트 오탐 교정", "러너 서비스 수정"])
def test_service_timer_conf_files_are_never_screen_work(path, instruction):
    assert _renders_nothing(path) is True
    assert screen_verification_required(instruction, [path, "tests/unit/test_x.py"]) is False


def test_instruction_keywords_are_ignored_when_changed_files_exist():
    assert screen_verification_required("UI 변경 화면 스크린샷", ["app/services/x.py"]) is False
    assert screen_verification_required("UI 변경 화면 스크린샷", ["app/services/x.py", "app/static/a.html"]) is True


def test_allow_list_rejects_screen_extension_outside_ui_tree():
    assert screen_verification_required("", ["docs/design/mock.html"]) is False
    assert screen_verification_required("", ["scripts/report.svg"]) is False


def test_svg_and_i18n_json_in_ui_tree_are_screen_work():
    assert screen_verification_required("", ["aads-dashboard/src/components/logo.svg"]) is True
    assert screen_verification_required("", ["aads-dashboard/src/app/locales/ko.json"]) is True
    assert screen_verification_required("", ["aads-dashboard/src/app/config.json"]) is False
    assert screen_verification_required("", ["deploy/locales/ko.json"]) is False


def test_static_scripts_count_only_under_static_tree():
    assert screen_verification_required("", ["app/static/apps/obys/a.js"]) is True
    assert screen_verification_required("", ["scripts/build.js"]) is False
