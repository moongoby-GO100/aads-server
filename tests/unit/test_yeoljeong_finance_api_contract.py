import pytest
from pydantic import ValidationError

from app.api.yeoljeong_finance import AccountUpsertPayload


def test_account_password_is_write_only_and_hidden_from_repr():
    schema = AccountUpsertPayload.model_json_schema()
    payload = AccountUpsertPayload(service="baemin", username="owner", password="secret")

    assert schema["properties"]["password"]["writeOnly"] is True
    assert "password=" not in repr(payload)


def test_account_payload_rejects_secret_field_injection():
    with pytest.raises(ValidationError):
        AccountUpsertPayload(
            service="baemin",
            username="owner",
            password_enc="attacker-controlled-ciphertext",
        )
