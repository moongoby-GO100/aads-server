from __future__ import annotations

from app.services.work_recipe import orchestrator
from app.services.work_recipe import recorder as recorder_module


async def test_recorder_replaces_credentials_before_saving(monkeypatch):
    saved = {}

    async def next_version(**kwargs):
        return 3

    async def save_recipe(recipe, **kwargs):
        saved["recipe"] = recipe
        return {"id": "recipe-1", "spec": recipe.to_dict()}

    monkeypatch.setattr(recorder_module.store, "next_version", next_version)
    monkeypatch.setattr(recorder_module.store, "save_recipe", save_recipe)

    recording = recorder_module.start_recording(
        "example_login", "https://www.example.com/login", "tenant-1"
    )
    recording.record_step({"action": "navigate", "url": "https://example.com/login"})
    recording.record_step(
        {"action": "fill", "selector": "input[type=password]", "value": "raw-password"}
    )
    await recording.finish_recording()

    recipe = saved["recipe"]
    serialized = recipe.to_yaml()
    assert "raw-password" not in serialized
    assert recipe.steps[1].value == "{{credential_1}}"
    assert recipe.inputs[0].secret is True
    assert recipe.domain == "example.com"
    assert recipe.version == 3


async def test_orchestrator_returns_none_without_candidate(monkeypatch):
    async def no_recipes(**kwargs):
        return []

    monkeypatch.setattr(orchestrator, "list_recipes", no_recipes)
    assert await orchestrator.resolve_recipe("처음 보는 사이트 작업", "tenant-1") is None


async def test_secret_input_requires_scoped_credential_reference():
    recipe = recorder_module.WorkRecipe(
        name="login",
        domain="example.com",
        inputs=[recorder_module.RecipeInput(name="password", secret=True)],
        steps=[recorder_module.RecipeStep(seq=1, action="snapshot")],
    )
    try:
        await orchestrator._scoped_inputs(recipe, {"password": "plain-secret"})
    except ValueError as exc:
        assert "credential_scope" in str(exc)
    else:
        raise AssertionError("plain secret input was accepted")
