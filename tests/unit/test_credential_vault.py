import json

from app.core.credential_vault import _coerce_json_dict, _coerce_json_list, _normalize_json_fields


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
