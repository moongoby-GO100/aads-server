import json
import inspect

from app.core import credential_vault
from app.core.credential_vault import (
    _E2E_PROJECT_CONFIG,
    _coerce_json_dict,
    _coerce_json_list,
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
