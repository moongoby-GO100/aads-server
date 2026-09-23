from app.services.e2e_verify import screen_verification_required


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
