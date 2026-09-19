from app.routers.goals import goal_owner_binding_policy


def test_local_project_owner_is_allowed() -> None:
    assert goal_owner_binding_policy(
        "AADS", "AADS", "project_owner", as_lead=True,
    ) == (True, None)


def test_foreign_project_owner_is_denied() -> None:
    allowed, reason = goal_owner_binding_policy(
        "AADS", "GO100", "project_owner", as_lead=False,
    )
    assert allowed is False
    assert reason == "cross_project_owner_denied"


def test_ceo_exception_is_coordination_only() -> None:
    assert goal_owner_binding_policy(
        "AADS", "CEO", "portfolio_coordinator", as_lead=False,
    ) == (True, None)
    assert goal_owner_binding_policy(
        "AADS", "CEO", "portfolio_coordinator", as_lead=True,
    ) == (False, "cross_project_owner_denied")
