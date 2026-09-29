"""Exercise receipt preservation at the actual append-only SQL boundary."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from app.services.work_recipe import audit
from app.services.work_recipe.player import StepResult
from app.services.work_recipe.shinhan_corporate_manual import verify_receipt


def receipt(stage="open", **changes):
    body = {
        "stage": stage,
        "origin": "bank.shinhan.com",
        "dom_sha256": hashlib.sha256(b"screen").hexdigest(),
        "predecessor_sha256": "" if stage == "open" else "b" * 64,
    }
    body.update(changes)
    return {**body, "receipt_sha256": hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()}


async def persist(monkeypatch, evidence):
    captured = {}

    class Connection:
        async def execute(self, query, *args):
            assert "INSERT INTO recipe_run_steps" in query
            captured["output"] = json.loads(args[9])

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr(audit, "get_pool", lambda: SimpleNamespace(acquire=Acquire))
    step = StepResult(seq=1, action="snapshot", risk="READ", status="success",
                      evidence=evidence, output={"token": "not-for-storage", "sha256": "c" * 64})
    recorder = audit.GuardedRunRecorder(domain="bank.shinhan.com")
    await recorder.record_step(run_id="11111111-1111-1111-1111-111111111111", step=step)
    return captured["output"]


@pytest.mark.parametrize("stage", ["open", "authenticate", "verify"])
async def test_valid_receipt_remains_verifiable_after_sql_serialization(monkeypatch, stage):
    original = receipt(stage)
    output = await persist(monkeypatch, {"stage_receipt": original, "sha256": "a" * 64})
    saved = output["evidence"]["stage_receipt"]
    assert saved == original
    assert verify_receipt(saved)
    assert output["output"] == {"token": "***", "sha256": "***"}
    assert output["evidence"]["sha256"] == "***"
    assert original["dom_sha256"] != "***"  # Original caller payload is untouched.


@pytest.mark.parametrize("invalid", [
    {**receipt(), "extra": "private"},
    {**receipt(), "receipt_sha256": "0" * 64},
    receipt(dom_sha256="secret-password-value-not-a-fingerprint"),
    receipt(dom_sha256="A" * 64),
    receipt(dom_sha256="a" * 63),
    receipt(origin="other.example"),
    receipt(stage="unknown"),
    receipt(predecessor_sha256="a" * 64),
    receipt(stage="authenticate", predecessor_sha256=""),
    receipt(dom_sha256=None),
])
async def test_invalid_or_extended_receipt_uses_normal_masking(monkeypatch, invalid):
    output = await persist(monkeypatch, {"stage_receipt": invalid})
    saved = output["evidence"]["stage_receipt"]
    assert saved == audit.mask_audit_payload(invalid)
    assert saved["receipt_sha256"] == "***"


async def test_receipt_shape_at_untrusted_other_location_gets_no_exemption(monkeypatch):
    original = receipt()
    output = await persist(monkeypatch, {"other": original})
    assert output["evidence"]["other"]["receipt_sha256"] == "***"
