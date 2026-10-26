"""Regression coverage for the persistent mockup store and legacy read compatibility."""
import pytest

from app.services import mockup_review_service as svc

URI = "internal://mockup_assets/GO100/page.html"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "ROOT", tmp_path)
    monkeypatch.delenv("MOCKUP_REVIEW_ASSET_ROOT", raising=False)
    current = tmp_path / "app/data/mockup_assets"
    legacy = tmp_path / "data/mockup_assets"
    for root in (current, legacy):
        (root / "GO100").mkdir(parents=True)
    return current, legacy


def test_default_root_reads_persistent_store(roots):
    current, _ = roots
    assert svc.asset_roots()["mockup_assets"] == current
    assert svc.asset_roots()["uploaded_mockups"] == current / "html-uploads"
    (current / "GO100/page.html").write_bytes(b"persistent")
    assert svc.read_asset(URI) == b"persistent"


def test_legacy_fallback_and_primary_precedence(roots):
    current, legacy = roots
    (legacy / "GO100/page.html").write_bytes(b"legacy")
    assert svc.read_asset(URI) == b"legacy"
    (current / "GO100/page.html").write_bytes(b"persistent")
    assert svc.read_asset(URI) == b"persistent"
    assert (legacy / "GO100/page.html").read_bytes() == b"legacy"


def test_environment_override_does_not_fall_back(roots, tmp_path, monkeypatch):
    current, legacy = roots
    configured = tmp_path / "configured"
    (configured / "GO100").mkdir(parents=True)
    monkeypatch.setenv("MOCKUP_REVIEW_ASSET_ROOT", str(configured))
    assert svc.asset_roots()["mockup_assets"] == configured
    for root in (current, legacy):
        (root / "GO100/page.html").write_bytes(b"default")
    with pytest.raises(svc.AssetProblem) as error:
        svc.read_asset(URI)
    assert error.value.code == "asset_not_found"
    (configured / "GO100/page.html").write_bytes(b"configured")
    assert svc.read_asset(URI) == b"configured"


@pytest.mark.parametrize("slot", [0, 1])
def test_each_root_rejects_symlink_escape(roots, tmp_path, slot):
    outside = tmp_path / "outside.html"
    outside.write_bytes(b"private")
    (roots[slot] / "GO100/page.html").symlink_to(outside)
    # An unsafe primary must not silently serve even a valid legacy asset.
    if slot == 0:
        (roots[1] / "GO100/page.html").write_bytes(b"legacy")
    with pytest.raises(svc.AssetProblem) as error:
        svc.read_asset(URI)
    assert error.value.code == "asset_uri_not_allowed"


def test_traversal_is_rejected(roots):
    with pytest.raises(svc.AssetProblem) as error:
        svc.read_asset("internal://mockup_assets/../outside.html")
    assert error.value.code == "asset_uri_not_allowed"


def test_missing_in_both_roots(roots):
    with pytest.raises(svc.AssetProblem) as error:
        svc.read_asset(URI)
    assert error.value.code == "asset_not_found"
