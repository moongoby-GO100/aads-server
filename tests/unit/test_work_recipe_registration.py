from __future__ import annotations

from pathlib import Path

from app.services.work_recipe import registration
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


async def test_list_registrations_is_tenant_scoped_pending_and_newest_first(monkeypatch):
    tenant_id = "2d701a8c-9596-4757-8588-faa4f7837112"
    captured: dict[str, object] = {}

    class Connection:
        async def fetch(self, query, *args):
            captured["query"] = query
            captured["args"] = args
            return [
                {
                    "id": "8c6c6931-70d1-48a8-9d59-b3d548e787cb",
                    "tenant_id": tenant_id,
                    "name": "orders",
                    "domain": "shop.example.com",
                    "spec": {"steps": [{"action": "navigate"}]},
                    "status": "pending",
                }
            ]

    class AcquiredConnection:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_):
            return False

    class Pool:
        def acquire(self):
            return AcquiredConnection()

    monkeypatch.setattr(registration, "get_pool", lambda: Pool())

    rows = await registration.list_registrations(tenant_id=tenant_id)

    assert rows[0]["id"] == "8c6c6931-70d1-48a8-9d59-b3d548e787cb"
    assert rows[0]["tenant_id"] == tenant_id
    assert captured["args"] == (registration._tenant_uuid(tenant_id), "pending")
    assert "WHERE tenant_id=$1 AND status=$2" in str(captured["query"])
    assert "ORDER BY requested_at DESC, id DESC" in str(captured["query"])
