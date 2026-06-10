import json
import inspect

import pytest

from app.core import credential_vault
from app.core.credential_vault import (
    _E2E_PROJECT_CONFIG,
    _coerce_json_dict,
    _coerce_json_list,
    login_session_completed,
    _normalize_json_fields,
)


def test_coerce_json_list_accepts_native_and_string_values():
    steps = [{"action": "fill", "selector": "input", "value": "{{username}}"}]

    assert _coerce_json_list(steps) == steps
    assert _coerce_json_list(json.dumps(steps)) == steps
    assert _coerce_json_list(json.dumps(json.dumps(steps))) == steps


def test_coerce_json_list_falls_back_to_empty_list_for_invalid_values():
    assert _coerce_json_list(None) == []
    assert _coerce_json_list("") == []
    assert _coerce_json_list("not-json") == []
    assert _coerce_json_list({"not": "a-list"}) == []


def test_coerce_json_dict_accepts_native_and_string_values():
    fields = {"source": "env", "role": "admin"}

    assert _coerce_json_dict(fields) == fields
    assert _coerce_json_dict(json.dumps(fields)) == fields
    assert _coerce_json_dict(json.dumps(json.dumps(fields))) == fields


def test_normalize_json_fields_updates_credential_item_in_place():
    item = {
        "extra_fields": json.dumps({"source": "vault"}),
        "login_steps": json.dumps([{"action": "wait", "ms": 1000}]),
    }

    assert _normalize_json_fields(item) is item
    assert item["extra_fields"] == {"source": "vault"}
    assert item["login_steps"] == [{"action": "wait", "ms": 1000}]


def test_ntv1_e2e_labels_match_registered_vault_labels():
    assert _E2E_PROJECT_CONFIG["NTV1_ADMIN"]["label"] == "V1 관리자"
    assert _E2E_PROJECT_CONFIG["NTV1_WHOLESALE"]["label"] == "V1 도매"
    assert _E2E_PROJECT_CONFIG["NTV1_RETAIL"]["label"] == "V1 소매"


def test_ntv2_e2e_supported_roles_include_main_permission_groups():
    roles = set(_E2E_PROJECT_CONFIG["NTV2"]["supported_roles"])

    assert {"admin", "wholesale", "retail", "md"}.issubset(roles)
    assert "{role}" in _E2E_PROJECT_CONFIG["NTV2"]["e2e_url"]


def test_credential_crud_requires_and_filters_tenant_scope():
    funcs = [
        credential_vault.list_credentials,
        credential_vault.get_credential,
        credential_vault.create_credential,
        credential_vault.update_credential,
        credential_vault.delete_credential,
        credential_vault.mark_used,
        credential_vault.mark_verified,
        credential_vault.get_login_credential,
    ]

    for fn in funcs:
        assert "tenant_id" in inspect.signature(fn).parameters

    source = "\n".join(inspect.getsource(fn) for fn in funcs)
    assert "tenant_scope_required:" in inspect.getsource(credential_vault._require_tenant_uuid)
    assert "tenant_id = $1" in source
    assert "WHERE id = $1 AND tenant_id = $2" in source
    assert "ON CONFLICT (tenant_id, service, COALESCE(project, '_ALL_'), label)" in source


class _FakeLocator:
    def __init__(self, visible: bool):
        self._visible = visible

    @property
    def first(self):
        return self

    async def is_visible(self, timeout: int = 0):
        return self._visible


class _FakePage:
    def __init__(self, url: str, visible_selectors: set[str] | None = None):
        self.url = url
        self.visible_selectors = visible_selectors or set()

    def locator(self, selector: str):
        visible = any(needle in selector for needle in self.visible_selectors)
        return _FakeLocator(visible)


@pytest.mark.asyncio
async def test_login_session_completed_rejects_login_url_even_without_visible_form():
    page = _FakePage("https://v2.newtalk.kr/login")

    assert await login_session_completed(page, "https://v2.newtalk.kr/login") is False


@pytest.mark.asyncio
async def test_login_session_completed_rejects_visible_login_form():
    page = _FakePage(
        "https://v2.newtalk.kr/dashboard",
        visible_selectors={"input[type='password']"},
    )

    assert await login_session_completed(page, "https://v2.newtalk.kr/login") is False


@pytest.mark.asyncio
async def test_login_session_completed_accepts_non_login_page_without_form():
    page = _FakePage("https://v2.newtalk.kr/dashboard")

    assert await login_session_completed(page, "https://v2.newtalk.kr/login") is True
