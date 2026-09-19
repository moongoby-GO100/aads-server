from __future__ import annotations

from pathlib import Path

from app.services.work_recipe.registration import build_dry_run
from app.services.work_recipe.schema import RecipeInput, RecipeStep, WorkRecipe


def test_registration_dry_run_exposes_scope_risk_variables_and_evidence_without_values():
    recipe = WorkRecipe(
        name="orders",
        domain="shop.example.com",
        inputs=[RecipeInput(name="account", secret=True, description="vault account")],
        steps=[
            RecipeStep(
                seq=1,
                action="navigate",
                url="https://shop.example.com/orders",
                risk="WRITE_EXTERNAL",
                description="주문 화면 이동",
            )
        ],
    )

    preview = build_dry_run(recipe, proposed_version=4)

    assert preview["scope"] == "recipe_registration"
    assert preview["proposed_version"] == 4
    assert preview["max_risk"] == "WRITE_EXTERNAL"
    assert preview["permissions"] == ["WRITE_EXTERNAL"]
    assert preview["inputs"] == [
        {"name": "account", "secret": True, "description": "vault account"}
    ]
    assert preview["steps"][0]["evidence"] == "screenshot_or_step_audit"
    assert "value" not in preview["inputs"][0]


def test_registration_migration_is_additive_tenant_scoped_and_pending_unique():
    sql = (
        Path(__file__).parents[2]
        / "migrations"
        / "20260919_work_recipe_registration_requests.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS work_recipe_registration_requests" in sql
    assert "tenant_id       UUID NOT NULL REFERENCES tenants" in sql
    assert "recipe_id       UUID NULL REFERENCES work_recipes" in sql
    assert "WHERE status = 'pending'" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()
