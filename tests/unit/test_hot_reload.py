from app.api.hot_reload import _BLOCKED_MODULES, _is_reloadable


def test_pc_agent_manager_singleton_is_never_hot_reloaded():
    """importlib.reload()가 pc_agent_manager 싱글톤을 재생성하면 이미 이를 바인딩해 둔
    app.api.pc_agent(WebSocket 핸들러, diagnostics)는 옛 인스턴스를, app.main의 지연
    import 호출부는 새 빈 인스턴스를 보게 되어 온라인 판정이 영구히 어긋난다.
    (AADS-FOOD-QUEUE-DRAIN-AGENT-ONLINE-MISMATCH-P0)
    """
    assert "app.services.pc_agent_manager" in _BLOCKED_MODULES
    assert _is_reloadable("app.services.pc_agent_manager") is False


def test_other_services_modules_remain_reloadable():
    assert _is_reloadable("app.services.yeoljeong_finance_service") is True
