"""Mockup review: asset store security, manifest hashing, completeness and request contracts (no DB)."""
import asyncio
import copy
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.mockup_review import (
    STATES, ApproveReview, AssetInput, ChangeCreate, DocRefInput, ReviewCreate, RevisionCreate, RevokeReview,
)
from app.services import mockup_review_service as svc

NOW = "2026-10-03T00:00:00Z"
KEY = "idem-key-00001"
CSS = b"body{background:url('bg.png')}\n/* url(ignored.png) */"
JS = b"console.log('mock')"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "assets"
    (root / "m").mkdir(parents=True)
    monkeypatch.setenv("MOCKUP_REVIEW_ASSET_ROOT", str(root))
    return root / "m"


def put(store, name, data):
    (store / name).write_bytes(data if isinstance(data, bytes) else data.encode())
    return f"internal://mockup_assets/m/{name}"


def asset_dict(uri, data, mime, **extra):
    base = {"asset_id": extra.pop("asset_id"), "role": "child", "uri": uri, "sha256": svc.sha256_hex(data),
            "byte_size": len(data), "mime": mime}
    return {**base, **extra}


def primary(store, screen, phase, viewport, state, tag=None):
    name = f"{screen}-{phase}-{viewport}-{state}.html"
    html = f"<html><link rel=stylesheet href='style.css'><script src='app.js'></script><img src='bg.png'>{tag or name}"
    uri = put(store, name, html)
    return asset_dict(uri, html.encode(), "text/html", asset_id=name.replace(".html", ""), role="primary",
                      screen_id=screen, phase=phase, viewport=viewport, fixture_id="fx-1", state=state,
                      captured_at=NOW, capture_source="browser_capture")


def children(store):
    return [
        asset_dict(put(store, "style.css", CSS), CSS, "text/css", asset_id="style"),
        asset_dict(put(store, "app.js", JS), JS, "text/javascript", asset_id="app"),
        asset_dict(put(store, "bg.png", PNG), PNG, "image/png", asset_id="bg"),
    ]


def manifest(store, change_type="modify", states=True, before=True):
    assets = children(store)
    for viewport in ("desktop", "mobile"):
        assets.append(primary(store, "s1", "mockup", viewport, "default"))
        if before and change_type == "modify":
            assets.append(primary(store, "s1", "before", viewport, "default"))
    screen = {"screen_id": "s1", "title": "Screen", "route": "/s1", "requirement_ids": ["M01"],
              "states_not_applicable": {} if not states else {s: "not applicable here" for s in STATES[1:]}}
    return {"screens": [screen], "assets": assets, "design_tokens_version": "dt-1", "source_sha": "abcdef1",
            "evidence": [{"evidence_id": "ev1", "kind": "browser_capture", "screen_id": "s1", "route": "/s1",
                          "success": True, "recorded_at": NOW}]}


def revision(store, **kw):
    return RevisionCreate(idempotency_key=KEY, expected_generation=0, manifest=manifest(store, **kw))


def build(body, change_type="modify"):
    return asyncio.run(svc.build_manifest(body, change_type))


def refs(*roles):
    return [{"role": r, "document_key": r + "-doc", "revision_id": str(uuid4()), "content_hash": "a" * 64}
            for r in roles]


# ---------------------------------------------------------------- asset store

@pytest.mark.parametrize("uri", [
    "http://example.com/a.png", "https://169.254.169.254/latest", "file:///etc/passwd", "ftp://x/a.png",
    "internal://docs/../../etc/passwd", "internal://docs/a/../../b.html", "internal://docs/.hidden/a.png",
    "internal://docs//a.png", "internal://docs/a%2e%2e/b.png", "internal://docs/a\\b.png",
    "internal://docs/a.png?x=1", "internal://docs/a.png#f", "internal://unknown/a.png", "internal://docs",
    "internal://docs/", "/etc/passwd", "data:text/html,hi",
])
def test_asset_uri_rejects_unsafe_forms(uri):
    with pytest.raises(svc.AssetProblem) as error:
        svc.parse_asset_uri(uri)
    assert error.value.code == "asset_uri_not_allowed"


def test_asset_uri_accepts_allowlisted_roots():
    assert svc.parse_asset_uri("internal://docs/a/b.html") == ("docs", "a/b.html")
    assert svc.parse_asset_uri("internal://mockup_assets/x/y.png") == ("mockup_assets", "x/y.png")


def test_symlink_escape_is_rejected(store, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("outside")
    (store / "link.png").symlink_to(outside)
    with pytest.raises(svc.AssetProblem) as error:
        svc.read_asset("internal://mockup_assets/m/link.png")
    assert error.value.code == "asset_uri_not_allowed"


def test_missing_and_directory_assets(store):
    with pytest.raises(svc.AssetProblem) as error:
        svc.read_asset("internal://mockup_assets/m/none.png")
    assert error.value.code == "asset_not_found"
    (store / "dir").mkdir()
    with pytest.raises(svc.AssetProblem):
        svc.read_asset("internal://mockup_assets/m/dir")


# ------------------------------------------------------------ child references

def html_refs(html, uri="internal://mockup_assets/m/page.html"):
    return svc.child_references({"uri": uri, "mime": "text/html"}, html.encode())


def test_html_children_are_collected_and_normalized():
    found = html_refs(
        "<link rel=stylesheet href='css/../style.css?v=1'><script src=app.js></script>"
        "<img src='img/a.png' srcset='b.png 1x, c.png 2x'><div style=\"background:url(d.png)\"></div>"
        "<style>@import 'e.css'; .x{background:url(\"f.png\")}</style><img src='data:image/png;base64,AA=='>"
        "<a href='#top'>t</a><a href='https://example.com'>ignored anchors are not subresources</a>")
    base = "internal://mockup_assets/m/"
    assert found == {base + n for n in ("style.css", "app.js", "img/a.png", "b.png", "c.png", "d.png", "e.css", "f.png")}


@pytest.mark.parametrize("ref,code", [
    ("http://evil.example/x.js", "external_reference_not_allowed"),
    ("//evil.example/x.js", "external_reference_not_allowed"),
    ("javascript:alert(1)", "external_reference_not_allowed"),
    ("/abs/x.js", "absolute_reference_not_allowed"),
    ("../../../etc/passwd", "reference_escapes_root"),
    ("..%2f..%2fx.js", "reference_escapes_root"),
])
def test_child_reference_rejections(ref, code):
    with pytest.raises(svc.AssetProblem) as error:
        html_refs(f"<script src='{ref}'></script>")
    assert error.value.code == code


def test_css_children_resolve_against_the_stylesheet():
    found = svc.child_references({"uri": "internal://mockup_assets/m/css/s.css", "mime": "text/css"},
                                 b"@import 'base.css'; a{background:url(../img/x.png)}")
    assert found == {"internal://mockup_assets/m/css/base.css", "internal://mockup_assets/m/img/x.png"}
    with pytest.raises(svc.AssetProblem):
        svc.child_references({"uri": "internal://mockup_assets/m/s.css", "mime": "text/css"},
                             b"a{background:url(https://evil.example/x.png)}")


# ---------------------------------------------------------------- manifest hash

def test_manifest_hash_is_server_computed_and_order_independent(store):
    body = revision(store)
    first = svc.manifest_hash(build(body))
    shuffled = copy.deepcopy(body.model_dump(mode="json"))
    shuffled["manifest"]["assets"].reverse()
    second = svc.manifest_hash(build(RevisionCreate(**shuffled)))
    assert first == second and len(first) == 64


def test_any_child_byte_change_changes_nothing_silently(store):
    body = revision(store)
    build(body)
    (store / "style.css").write_bytes(CSS + b" ")
    with pytest.raises(HTTPException) as error:
        build(body)
    assert error.value.status_code == 422 and error.value.detail["code"] == "asset_hash_mismatch"
    assert error.value.detail["server_sha256"] == svc.sha256_hex(CSS + b" ")


def test_declared_hash_cannot_be_forged(store):
    data = revision(store).model_dump(mode="json")
    data["manifest"]["assets"][0]["sha256"] = "0" * 64
    with pytest.raises(HTTPException) as error:
        build(RevisionCreate(**data))
    assert error.value.detail["code"] == "asset_hash_mismatch"


def test_unhashed_child_reference_is_rejected(store):
    data = revision(store).model_dump(mode="json")
    data["manifest"]["assets"] = [a for a in data["manifest"]["assets"] if a["asset_id"] != "bg"]
    with pytest.raises(HTTPException) as error:
        build(RevisionCreate(**data))
    assert error.value.detail["code"] == "unhashed_child_reference"
    assert any(r.endswith("bg.png") for r in error.value.detail["references"])


def test_external_reference_inside_html_is_rejected(store):
    data = revision(store).model_dump(mode="json")
    html = b"<html><script src='https://evil.example/x.js'></script>"
    put(store, "s1-mockup-desktop-default.html", html)
    target = next(a for a in data["manifest"]["assets"] if a["asset_id"] == "s1-mockup-desktop-default")
    target.update(sha256=svc.sha256_hex(html), byte_size=len(html))
    with pytest.raises(HTTPException) as error:
        build(RevisionCreate(**data))
    assert error.value.detail["code"] == "external_reference_not_allowed"


def test_mime_must_match_extension_and_secrets_are_refused(store):
    data = revision(store).model_dump(mode="json")
    next(a for a in data["manifest"]["assets"] if a["asset_id"] == "app")["mime"] = "text/html"
    with pytest.raises(HTTPException) as error:
        build(RevisionCreate(**data))
    assert error.value.detail["code"] == "asset_mime_mismatch"

    data = revision(store).model_dump(mode="json")
    leaked = f"const {'api_' + 'key'} = '{'abcdefghijklmnop' + '1234'}'".encode()
    put(store, "app.js", leaked)
    app = next(a for a in data["manifest"]["assets"] if a["asset_id"] == "app")
    app.update(sha256=svc.sha256_hex(leaked), byte_size=len(leaked))
    with pytest.raises(HTTPException) as error:
        build(RevisionCreate(**data))
    assert error.value.detail["code"] == "credential_content_rejected"


def test_uri_outside_store_is_rejected_at_build(store):
    data = revision(store).model_dump(mode="json")
    data["manifest"]["assets"][0]["uri"] = "http://169.254.169.254/latest/meta-data"
    with pytest.raises(HTTPException) as error:
        build(RevisionCreate(**data))
    assert error.value.detail["code"] == "asset_uri_not_allowed"


def test_recheck_detects_tampering_and_deletion(store):
    built = build(revision(store))
    assert asyncio.run(svc.recheck_assets(built)) == []
    (store / "app.js").write_bytes(b"tampered")
    (store / "bg.png").unlink()
    changed = {c["asset_id"]: c["reason"] for c in asyncio.run(svc.recheck_assets(built))}
    assert changed == {"app": "hash_changed", "bg": "asset_not_found"}


# ------------------------------------------------------------------ completeness

def codes(issues):
    return {(i["code"], i.get("state") or i.get("screen_id") or i.get("role")) for i in issues}


def test_complete_modify_manifest_passes(store):
    built = build(revision(store))
    refs_ok = [{"role": r} for r in ("plan", "prd", "spec")]
    assert svc.completeness(built, refs_ok) == []


def test_missing_documents_states_and_before(store):
    built = build(revision(store, states=False, before=False))
    found = codes(svc.completeness(built, [{"role": "plan"}]))
    assert ("document_ref_missing", "prd") in found and ("document_ref_missing", "spec") in found
    assert {("state_missing", s) for s in STATES[1:]} <= found
    assert ("before_missing", "s1") in found


def test_new_screen_needs_no_before_and_backend_only_needs_rationale(store):
    built = build(revision(store, change_type="new", before=False), "new")
    assert svc.completeness(built, [{"role": r} for r in ("plan", "prd", "spec")]) == []
    backend = {"change_type": "none", "screens": [], "assets": [], "evidence": [], "backend_only_rationale": "short"}
    assert ("backend_only_rationale_missing", None) in codes(svc.completeness(backend, [{"role": "plan"}] * 3))
    backend["backend_only_rationale"] = "No UI surface changes in this backend-only task."
    assert svc.completeness(backend, [{"role": r} for r in ("plan", "prd", "spec")]) == []


def test_login_screen_cannot_be_before_evidence_and_redaction_pending_blocks(store):
    data = revision(store).model_dump(mode="json")
    for item in data["manifest"]["assets"]:
        if item["phase"] == "before":
            item["capture_source"] = "login_redirect"
        if item["asset_id"] == "s1-mockup-mobile-default":
            item["redaction_status"] = "pending"
    built = build(RevisionCreate(**data))
    found = {i["code"] for i in svc.completeness(built, [{"role": r} for r in ("plan", "prd", "spec")])}
    assert {"blocked_evidence", "redaction_pending"} <= found


def test_visual_evidence_required_per_screen(store):
    data = revision(store).model_dump(mode="json")
    data["manifest"]["evidence"][0]["kind"] = "http_fallback"
    found = {i["code"] for i in svc.completeness(build(RevisionCreate(**data)),
                                                 [{"role": r} for r in ("plan", "prd", "spec")])}
    assert "visual_evidence_missing" in found


def test_viewport_class():
    assert [svc.viewport_class(v) for v in ("desktop", "mobile", "390x844", "768x1024", "1440x900")] == [
        "desktop", "mobile", "mobile", "tablet", "desktop"]


# ------------------------------------------------------------------- contracts

def test_idempotency_key_and_generation_validation():
    with pytest.raises(ValidationError):
        ReviewCreate(title="t", change_type="new", idempotency_key="short")
    with pytest.raises(ValidationError):
        ChangeCreate(idempotency_key=KEY, expected_generation=-1, change_request_id=uuid4(),
                     base_revision_id=uuid4(), source_message_id=uuid4(), comment="x")


def test_approval_requires_explicit_confirmation_and_valid_hash():
    base = {"idempotency_key": KEY, "expected_generation": 1, "revision_id": str(uuid4()), "manifest_hash": "a" * 64}
    assert ApproveReview(**base, confirm=True)
    for bad in ({"confirm": False}, {}, {"confirm": True, "manifest_hash": "A" * 64}):
        with pytest.raises(ValidationError):
            ApproveReview(**{**base, **bad})


def test_doc_ref_requires_revision_or_policy_backed_exemption():
    assert DocRefInput(role="plan", document_key="k", revision_id=uuid4(), content_hash="a" * 64)
    assert DocRefInput(role="plan", exempt_reason="Backend only change", exempt_policy_ref="POLICY-1")
    for bad in ({"role": "plan"}, {"role": "plan", "exempt_reason": "Backend only change"},
                {"role": "plan", "document_key": "k", "revision_id": str(uuid4()), "content_hash": "a" * 64,
                 "exempt_reason": "Backend only change", "exempt_policy_ref": "P"}):
        with pytest.raises(ValidationError):
            DocRefInput(**bad)


def test_primary_asset_requires_capture_metadata():
    with pytest.raises(ValidationError):
        AssetInput(asset_id="a", role="primary", uri="internal://docs/a.png", sha256="a" * 64, byte_size=1,
                   mime="image/png")
    assert AssetInput(asset_id="a", role="child", uri="internal://docs/a.png", sha256="a" * 64, byte_size=1,
                      mime="image/png")
    assert datetime.fromisoformat(NOW.replace("Z", "+00:00")).tzinfo == timezone.utc


def test_revoke_and_resolution_contracts():
    with pytest.raises(ValidationError):
        RevokeReview(idempotency_key=KEY, expected_generation=1, approval_id=0, reason="valid reason")
    with pytest.raises(ValidationError):
        RevisionCreate(idempotency_key=KEY, expected_generation=0, manifest={}, resolves=[
            {"change_request_id": str(uuid4()), "outcome": "applied", "reason": "ok!"}] * 2)


def test_request_hash_distinguishes_actions_and_payloads():
    assert svc.request_hash("a", {"x": 1}) == svc.request_hash("a", {"x": 1})
    assert svc.request_hash("a", {"x": 1}) != svc.request_hash("b", {"x": 1})
    assert svc.request_hash("a", {"x": 1}) != svc.request_hash("a", {"x": 2})


def test_router_exposes_the_eight_contract_routes():
    from app.api.mockup_reviews import router
    routes = {(m, r.path) for r in router.routes for m in r.methods}
    base = "/projects/{project_key}/mockup-reviews"
    for method, suffix in (("POST", ""), ("GET", "/{review_id}"), ("POST", "/{review_id}/revisions"),
                           ("GET", "/{review_id}/revisions/{revision_id}"), ("POST", "/{review_id}/submit"),
                           ("POST", "/{review_id}/changes"), ("POST", "/{review_id}/approve"),
                           ("POST", "/{review_id}/revoke"), ("POST", "/{review_id}/verify")):
        assert (method, base + suffix) in routes, (method, suffix)


def test_main_mounts_the_router():
    source = open(os.path.join(os.path.dirname(__file__), "..", "..", "app", "main.py"), encoding="utf-8").read()
    assert "app.include_router(mockup_reviews_router" in source
